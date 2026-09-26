"""Shared ``es_legal_issuer_v1`` record assembly (G0-B1 / G1-B1).

Extracts the observation-to-``SecurityRecord`` step so the frozen sample
validator and the G1-B batch resolver construct resolver inputs with
IDENTICAL semantics — while the branch decision itself stays exclusively
with ``universe.resolver.resolve_disposition``.

Assembly semantics (fail closed, no fuzzy matching):

- multiple FITRS records for one ISIN must agree on MiFIR ID / bond type;
  disagreement leaves the field ``None`` (undetermined) and records an
  ambiguity flag — the resolver then QUARANTINES via MISSING_*;
- multiple FIRDS records must agree on issuer LEI; disagreement is an
  explicit ``identity_conflict`` (resolver -> CONFLICT);
- SACL code and Desc label are retained as diagnostics; the parser-level
  SACL/Desc contradiction handling in ``universe.fitrs`` already yields
  ``bond_type=None``;
- GLEIF lookup runs only for a present, single issuer LEI; an absent or
  ambiguous LEI never reaches the lookup.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .firds import FirdsInstrument
from .fitrs import FitrsRecord
from .gleif import GleifEntity, resolve_legal_jurisdiction
from .model import Branch, Reason, SecurityRecord
from .resolver import resolve_disposition


def single_distinct(values: Iterable[str | None]) -> tuple[str | None, bool]:
    """Return (the single distinct non-empty value, ambiguous).

    Two or more distinct non-empty values are a contradiction: the field is
    undetermined (``None``) and ``ambiguous`` is True. Zero or one distinct
    value is unambiguous.
    """
    distinct = {v for v in values if v}
    if len(distinct) == 1:
        return next(iter(distinct)), False
    if len(distinct) > 1:
        return None, True
    return None, False


@dataclass(frozen=True)
class Assembly:
    """One ISIN's assembled resolver input plus its diagnostics."""

    record: SecurityRecord
    fitrs_count: int = 0
    firds_count: int = 0
    mifir_id: str | None = None
    bond_type: str | None = None
    sacl: str | None = None
    desc: str | None = None
    mifir_ambiguous: bool = False
    bond_type_ambiguous: bool = False
    issuer_lei: str | None = None
    lei_conflict: bool = False
    gleif: GleifEntity | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def assemble_security_record(
    isin: str,
    fitrs_records: list[FitrsRecord],
    firds_instruments: list[FirdsInstrument],
    gleif_entities: dict[str, GleifEntity],
) -> Assembly:
    """Assemble the ``SecurityRecord`` for one ISIN (deterministic, offline).

    ``gleif_entities`` is the pinned reference index; a required LEI absent
    from it resolves as unresolvable (the batch layer must separately prove
    acquisition completeness — this function performs no acquisition
    inference of its own).
    """
    notes: list[str] = []
    mifir_id, mifir_ambiguous = single_distinct(
        r.instrument_mifir_id for r in fitrs_records)
    bond_type, type_ambiguous = single_distinct(
        r.bond_type for r in fitrs_records)
    sacl, _ = single_distinct(r.sub_asset_class for r in fitrs_records)
    desc, _ = single_distinct(r.bond_type_label for r in fitrs_records)
    if mifir_ambiguous:
        notes.append("FITRS records disagree on MiFIR ID -> undetermined "
                     "(fail closed)")
    if type_ambiguous:
        notes.append("FITRS records disagree on bond type -> undetermined "
                     "(fail closed)")

    lei, lei_ambiguous = single_distinct(
        f.issuer_lei for f in firds_instruments)
    if lei_ambiguous:
        notes.append("FIRDS records disagree on issuer LEI -> "
                     "IDENTITY_CONFLICT")

    # Frozen precedence is preserved verbatim: a contradiction carries NO
    # representative value — ``issuer_lei=None`` + ``identity_conflict=True``
    # — and the unchanged ``resolve_disposition`` precedence decides (a
    # missing LEI is therefore QUARANTINE / MISSING_ISSUER_LEI, checked
    # before the conflict branch). No GLEIF lookup is performed for an
    # ambiguous LEI. Any change to that ordering belongs to an explicit
    # spec freeze, not to record assembly.
    gleif = (resolve_legal_jurisdiction(lei or "", gleif_entities)
             if lei else None)

    record = SecurityRecord(
        instrument_mifir_id=mifir_id,
        instrument_bond_type=bond_type,
        issuer_lei=lei,
        gleif_legal_jurisdiction_country=(
            gleif.legal_jurisdiction_country if gleif else None),
        gleif_resolved=gleif.resolved if gleif else False,
        identity_conflict=lei_ambiguous,
        instrument_isin=isin,
        source_report_id=(fitrs_records[0].source_report_id
                          if fitrs_records else None),
    )

    # Note strings are byte-identical to the frozen G0-B1/F-009 assembly so
    # both consumers share one semantic; the INVALID_ISSUER_LEI outcome is
    # carried by the resolver's reason code, not a note.
    if not fitrs_records:
        notes.append("ISIN absent from FITRS full file "
                     "(cannot demonstrate BOND+CRPB)")
    if not firds_instruments:
        notes.append("ISIN absent from FIRDS full file "
                     "(issuer LEI unresolved)")

    return Assembly(
        record=record,
        fitrs_count=len(fitrs_records),
        firds_count=len(firds_instruments),
        mifir_id=mifir_id,
        bond_type=bond_type,
        sacl=sacl,
        desc=desc,
        mifir_ambiguous=mifir_ambiguous,
        bond_type_ambiguous=type_ambiguous,
        issuer_lei=lei,
        lei_conflict=lei_ambiguous,
        gleif=gleif,
        notes=tuple(notes),
    )


def required_gleif_leis(
    isins: Iterable[str],
    fitrs_by_isin: dict[str, list[FitrsRecord]],
    firds_by_isin: dict[str, list[FirdsInstrument]],
) -> set[str]:
    """LEIs that actually require a GLEIF jurisdiction outcome.

    Computed by PROBING the frozen resolver, not by re-implementing its
    precedence: each ISIN is assembled against an EMPTY GLEIF index, which
    leaves every earlier frozen decision intact while making the
    jurisdiction step unresolvable. An ISIN's LEI is required iff the probe
    returns QUARANTINE / UNRESOLVABLE_LEGAL_JURISDICTION — i.e. the frozen
    chain demonstrably reaches that step. ISINs decided earlier
    (NOT_BOND/NOT_CRPB/MISSING_*/INVALID_ISSUER_LEI/IDENTITY_CONFLICT)
    never require a lookup, so a failed acquisition for their LEI can never
    manufacture a false HOLD.
    """
    required: set[str] = set()
    for isin in isins:
        assembled = assemble_security_record(
            isin,
            fitrs_by_isin.get(isin) or [],
            firds_by_isin.get(isin) or [],
            {},
        )
        probe = resolve_disposition(assembled.record)
        if (probe.branch is Branch.QUARANTINE
                and probe.reason is Reason.UNRESOLVABLE_LEGAL_JURISDICTION):
            required.add(assembled.issuer_lei.strip().upper())
    return required
