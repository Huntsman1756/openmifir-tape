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
from .model import LEI_PATTERN, SecurityRecord


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

    # On a contradiction the frozen precedence needs a present LEI for the
    # identity-conflict branch to fire (a missing LEI is checked earlier).
    # The deterministic sorted-first attested value is carried ONLY for
    # that; ``Assembly.issuer_lei`` stays None so downstream output never
    # presents a contradictory value as resolved identity, and the GLEIF
    # lookup is skipped — no jurisdiction is consulted for a conflict.
    record_lei = lei
    if lei_ambiguous:
        record_lei = sorted(
            {f.issuer_lei for f in firds_instruments if f.issuer_lei})[0]

    gleif = (resolve_legal_jurisdiction(lei or "", gleif_entities)
             if lei else None)

    record = SecurityRecord(
        instrument_mifir_id=mifir_id,
        instrument_bond_type=bond_type,
        issuer_lei=record_lei,
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
    firds_by_isin: dict[str, list[FirdsInstrument]],
) -> set[str]:
    """LEIs that actually require a GLEIF jurisdiction outcome.

    A LEI is required iff it is the single distinct issuer LEI FIRDS yields
    for an observed ISIN and it is ISO 17442 shaped — exactly the inputs
    that can reach the resolver's jurisdiction step. Absent/ambiguous/
    invalid LEIs are decided earlier by the resolver and never require a
    lookup.
    """
    required: set[str] = set()
    for isin in isins:
        lei, ambiguous = single_distinct(
            f.issuer_lei for f in firds_by_isin.get(isin, []))
        if lei and not ambiguous:
            norm = lei.strip().upper()
            if LEI_PATTERN.match(norm):
                required.add(norm)
    return required
