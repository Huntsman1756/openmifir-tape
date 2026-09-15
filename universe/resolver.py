"""Deterministic disposition resolver for ``es_legal_issuer_v1`` (G0-B1).

Resolution precedence is explicit and total. Each branch decision is produced
wholly from the frozen semantics in ``docs/gates/G0.md`` §3 G0-B. No fuzzy
matching, no automatic overrides, no heuristic deletion or merge.

Precedence (first match wins):

1. instrument MiFIR ID != CRPB        -> EXCLUDE / NOT_CRPB
2. missing issuer LEI                  -> QUARANTINE / MISSING_ISSUER_LEI
3. LEI not an exact ISO 17442 shape   -> QUARANTINE / INVALID_ISSUER_LEI
4. explicit identity conflict OR two+
   distinct observed jurisdictions     -> CONFLICT / IDENTITY_CONFLICT
5. GLEIF unresolved OR jurisdiction
   country missing                     -> QUARANTINE / UNRESOLVABLE_LEGAL_JURISDICTION
6. resolved jurisdiction != ES         -> CONFLICT / NON_ES_LEGAL_JURISDICTION
7. otherwise                           -> INCLUDE / IN_ES_CRPB_UNIVERSE
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from .model import (
    INSTRUMENT_MIFIR_ID,
    LEGAL_JURISDICTION_FILTER,
    LEI_PATTERN,
    PROFILE,
    UNIVERSE_ID,
    Branch,
    Disposition,
    Reason,
    SecurityRecord,
)


def _as_str(value) -> str:
    """Coerce an identity field to a string, treating None as empty.

    ``str()`` (not ``repr``) so a numeric LEI yields its plain digits and is then
    rejected by the exact ISO 17442 shape check. Nothing is ever guessed.
    """
    return "" if value is None else str(value)


def _norm_mifir_id(value) -> str:
    return _as_str(value).strip().upper()


def _norm_lei(value) -> str:
    return _as_str(value).strip().upper()


def _norm_country(value) -> str | None:
    if value is None:
        return None
    country = _as_str(value).strip().upper()
    return country or None


def resolve_disposition(record: SecurityRecord) -> Disposition:
    """Resolve one record to a single frozen branch (deterministic)."""
    mifir_id = _norm_mifir_id(record.instrument_mifir_id)
    lei = _norm_lei(record.issuer_lei)

    if mifir_id != INSTRUMENT_MIFIR_ID:
        return Disposition(mifir_id, lei or None, None, Branch.EXCLUDE, Reason.NOT_CRPB)

    if not lei:
        return Disposition(mifir_id, None, None, Branch.QUARANTINE, Reason.MISSING_ISSUER_LEI)

    if not LEI_PATTERN.match(lei):
        return Disposition(mifir_id, lei, None, Branch.QUARANTINE, Reason.INVALID_ISSUER_LEI)

    distinct_jurisdictions = {
        country for country in (record.conflicting_jurisdictions or ()) if _norm_country(country)
    }
    if record.identity_conflict or len(distinct_jurisdictions) > 1:
        return Disposition(mifir_id, lei, None, Branch.CONFLICT, Reason.IDENTITY_CONFLICT)

    country = _norm_country(record.gleif_legal_jurisdiction_country)
    if not record.gleif_resolved or not country:
        return Disposition(
            mifir_id, lei, None, Branch.QUARANTINE, Reason.UNRESOLVABLE_LEGAL_JURISDICTION
        )

    if country != LEGAL_JURISDICTION_FILTER:
        return Disposition(mifir_id, lei, country, Branch.CONFLICT, Reason.NON_ES_LEGAL_JURISDICTION)

    return Disposition(mifir_id, lei, country, Branch.INCLUDE, Reason.IN_ES_CRPB_UNIVERSE)


def build_disposition_table(records: list[SecurityRecord]) -> list[Disposition]:
    """Resolve every record, preserving input order."""
    return [resolve_disposition(record) for record in records]


def branch_counts(dispositions: list[Disposition]) -> dict[str, int]:
    counts = Counter(d.branch.value for d in dispositions)
    for branch in Branch:
        counts.setdefault(branch.value, 0)
    return {branch.value: counts[branch.value] for branch in Branch}


def _sorted_rows(dispositions: list[Disposition]) -> list[dict[str, str | None]]:
    rows = [d.to_canonical() for d in dispositions]
    key = lambda row: (  # noqa: E731
        row["instrument_mifir_id"] or "",
        row["issuer_lei"] or "",
        row["legal_jurisdiction_country"] or "",
        row["branch"] or "",
        row["reason"] or "",
    )
    return sorted(rows, key=key)


def snapshot_payload(
    dispositions: list[Disposition],
    *,
    sample_snapshot_sha256: str | None = None,
) -> bytes:
    """Canonical, deterministic bytes for the frozen disposition table.

    ``sort_keys`` + compact separators + fixed field order make the digest
    reproducible across machines, runs, and Python versions.
    """
    obj = {
        "universe_id": UNIVERSE_ID,
        "profile": PROFILE,
        "instrument_mifir_id": INSTRUMENT_MIFIR_ID,
        "issuer_lei_source": "FIRDS_RTS23_FIELD5",
        "legal_jurisdiction_source": "GLEIF_LegalJurisdiction.country",
        "legal_jurisdiction_filter": LEGAL_JURISDICTION_FILTER,
        "isin_prefix_heuristics": "prohibited",
        "dispositions": _sorted_rows(dispositions),
    }
    if sample_snapshot_sha256 is not None:
        obj["sample_snapshot_sha256"] = sample_snapshot_sha256
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def universe_snapshot_sha256(
    dispositions: list[Disposition],
    *,
    sample_snapshot_sha256: str | None = None,
) -> str:
    """SHA-256 over the canonical disposition table (content-sensitive)."""
    return hashlib.sha256(snapshot_payload(dispositions, sample_snapshot_sha256=sample_snapshot_sha256)).hexdigest()
