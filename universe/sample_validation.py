"""F-009: validate the frozen 15+5 observation sample against live data.

For each of the 20 frozen ISINs this resolves the full ``es_legal_issuer_v1``
chain from real regulatory reference data:

- FITRS auth.045: ``instrument_mifir_id`` (BOND) + ``bond_type`` (CRPB),
  SACL-primary per ``universe/fitrs.py``;
- FIRDS auth.036: issuer LEI (RTS 23 field 5);
- GLEIF: ``LegalJurisdiction.country`` == ES.

Each ISIN's disposition comes from the SAME frozen resolver used by the
fixture conformance table — no separate semantics. An ISIN that fails any
link is kept in place and its disposition documented; the frozen sample is
never re-drawn (preregistration). Metadata-only: ISINs, LEIs, codes, counts
and digests — never provider payloads.

Offline by construction: this module consumes already-parsed records and
entities. Acquisition (ESMA file downloads, GLEIF API calls) is an operator
step; payloads live in the gitignored ``data/`` directory and enter the
evidence artifact only as SHA-256 + byte counts.
"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from .firds import FirdsInstrument, iter_firds
from .fitrs import FitrsRecord, parse_fitrs
from .gleif import GleifEntity, parse_gleif, resolve_legal_jurisdiction
from .model import Branch, SecurityRecord
from .resolver import resolve_disposition
from .sample import DEFAULT_SAMPLE_MANIFEST, FrozenSample, load_frozen_sample

DEFAULT_VALIDATION_ARTIFACT = (
    Path(__file__).resolve().parents[1]
    / "evidence" / "g0-b1" / "F-009_frozen_sample_validation.yaml"
)


class SampleValidationError(Exception):
    """Raised when the validation artifact is missing, malformed, or stale."""


def load_validated_scope(
    sample: FrozenSample | None = None,
    path: Path = DEFAULT_VALIDATION_ARTIFACT,
) -> frozenset[str]:
    """ISINs whose documented disposition is INCLUDE — the authoritative
    ``es-corporate-bonds`` profile scope for G0-E / G0 PASS.

    The frozen 20-ISIN observation sample stays intact for preregistration
    audit; this scope is its validated INCLUDE subset (e.g. BOND+CVTB
    convertibles documented EXCLUDE are NOT profile scope).

    Fail closed: a missing/malformed artifact raises, and the artifact's
    recorded ``sample_snapshot_sha256`` MUST equal the current manifest's —
    a stale artifact after a re-freeze is never silently accepted.
    """
    if sample is None:
        sample = load_frozen_sample(DEFAULT_SAMPLE_MANIFEST)
    if not path.exists():
        raise SampleValidationError(f"validation artifact not found: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    frozen = doc.get("frozen_sample") or {}
    if frozen.get("sample_snapshot_sha256") != sample.snapshot_sha256:
        raise SampleValidationError(
            "validation artifact sample_snapshot_sha256 does not match the "
            "frozen manifest — stale artifact, re-run validation"
        )
    results = doc.get("results")
    if not isinstance(results, list) or not results:
        raise SampleValidationError("validation artifact has no 'results' list")
    scope: set[str] = set()
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("isin"), str):
            raise SampleValidationError("validation artifact row missing 'isin'")
        if row.get("disposition") == "INCLUDE" and row["isin"].strip():
            scope.add(row["isin"].strip())
    return frozenset(scope)


@dataclass(frozen=True)
class SampleRow:
    """One frozen-sample ISIN's observations and resolved disposition."""

    isin: str
    bucket: str
    fitrs_observed: bool
    fitrs_mifir_id: str | None
    fitrs_bond_type: str | None
    fitrs_sacl: str | None
    fitrs_desc: str | None
    fitrs_record_count: int
    firds_observed: bool
    issuer_lei: str | None
    firds_record_count: int
    gleif_resolved: bool
    gleif_country: str | None
    branch: str
    reason: str
    notes: tuple[str, ...] = ()

    def to_manifest(self) -> dict:
        return {
            "isin": self.isin,
            "bucket": self.bucket,
            "fitrs_bond": self.fitrs_mifir_id,
            "fitrs_crbp": self.fitrs_bond_type,
            "fitrs_sacl": self.fitrs_sacl,
            "fitrs_desc": self.fitrs_desc,
            "fitrs_records": self.fitrs_record_count,
            "firds_issuer_lei": self.issuer_lei,
            "firds_records": self.firds_record_count,
            "gleif_es": self.gleif_country,
            "gleif_resolved": self.gleif_resolved,
            "disposition": self.branch,
            "reason": self.reason,
            "notes": list(self.notes),
        }


@dataclass
class SampleValidation:
    rows: list[SampleRow]
    branch_counts: dict[str, int]
    notes: list[str] = field(default_factory=list)


def _single(values: Iterable[str | None]) -> tuple[str | None, bool]:
    """Return (the single distinct non-empty value, ambiguous)."""
    distinct = {v for v in values if v}
    if len(distinct) == 1:
        return next(iter(distinct)), False
    if len(distinct) > 1:
        return None, True
    return None, False


def _classify_isin(
    isin: str,
    bucket: str,
    fitrs_records: dict[str, list[FitrsRecord]],
    firds_instruments: dict[str, list[FirdsInstrument]],
    gleif_entities: dict[str, GleifEntity],
) -> SampleRow:
    notes: list[str] = []
    frecs = fitrs_records.get(isin) or []
    freps = firds_instruments.get(isin) or []

    mifir_id, mifir_ambiguous = _single(r.instrument_mifir_id for r in frecs)
    bond_type, type_ambiguous = _single(r.bond_type for r in frecs)
    sacl, _ = _single(r.sub_asset_class for r in frecs)
    desc, _ = _single(r.bond_type_label for r in frecs)
    if mifir_ambiguous:
        notes.append("FITRS records disagree on MiFIR ID -> undetermined (fail closed)")
    if type_ambiguous:
        notes.append("FITRS records disagree on bond type -> undetermined (fail closed)")

    lei, lei_ambiguous = _single(f.issuer_lei for f in freps)
    identity_conflict = lei_ambiguous
    if lei_ambiguous:
        notes.append("FIRDS records disagree on issuer LEI -> IDENTITY_CONFLICT")

    gleif = resolve_legal_jurisdiction(lei or "", gleif_entities) if lei else None

    record = SecurityRecord(
        instrument_mifir_id=mifir_id,
        instrument_bond_type=bond_type,
        issuer_lei=lei,
        gleif_legal_jurisdiction_country=gleif.legal_jurisdiction_country if gleif else None,
        gleif_resolved=gleif.resolved if gleif else False,
        identity_conflict=identity_conflict,
        instrument_isin=isin,
        source_report_id=(frecs[0].source_report_id if frecs else None),
    )
    disp = resolve_disposition(record)

    if not frecs:
        notes.append("ISIN absent from FITRS full file (cannot demonstrate BOND+CRPB)")
    if not freps:
        notes.append("ISIN absent from FIRDS full file (issuer LEI unresolved)")

    return SampleRow(
        isin=isin,
        bucket=bucket,
        fitrs_observed=bool(frecs),
        fitrs_mifir_id=mifir_id,
        fitrs_bond_type=bond_type,
        fitrs_sacl=sacl,
        fitrs_desc=desc,
        fitrs_record_count=len(frecs),
        firds_observed=bool(freps),
        issuer_lei=lei,
        firds_record_count=len(freps),
        gleif_resolved=gleif.resolved if gleif else False,
        gleif_country=gleif.legal_jurisdiction_country if gleif else None,
        branch=disp.branch.value,
        reason=disp.reason.value,
        notes=tuple(notes),
    )


def validate_frozen_sample(
    sample: FrozenSample,
    fitrs_records: Iterable[FitrsRecord],
    firds_instruments: Iterable[FirdsInstrument],
    gleif_entities: dict[str, GleifEntity],
) -> SampleValidation:
    """Resolve every frozen-sample ISIN through the regulatory chain.

    Deterministic and offline. ``gleif_entities`` must already cover every
    issuer LEI that FIRDS yields; a missing LEI resolves as unresolvable
    (fail closed), which the row records.
    """
    fitrs_by_isin: dict[str, list[FitrsRecord]] = {}
    for r in fitrs_records:
        if r.instrument_isin in sample.isins:
            fitrs_by_isin.setdefault(r.instrument_isin, []).append(r)
    firds_by_isin: dict[str, list[FirdsInstrument]] = {}
    for f in firds_instruments:
        if f.instrument_isin in sample.isins:
            firds_by_isin.setdefault(f.instrument_isin, []).append(f)

    rows = [
        _classify_isin(isin, sample.bucket_of(isin), fitrs_by_isin, firds_by_isin, gleif_entities)
        for isin in sample.isin_order
    ]
    counts = Counter(r.branch for r in rows)
    return SampleValidation(
        rows=rows,
        branch_counts={b.value: counts.get(b.value, 0) for b in Branch},
    )


def _file_meta(path: Path) -> dict:
    data = path.read_bytes()
    return {"file": path.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def main(argv: list[str]) -> int:
    """Operator-local runner: local files only, no network.

    Usage: sample_validation.py <out.yaml> --fitrs <zip...> --firds <zip...>
           --gleif-cache <dir> [--manifest <universe_manifest.yaml>]
    """
    args = list(argv)
    out = Path(args.pop(0))

    def _take(flag: str) -> list[str]:
        taken: list[str] = []
        while flag in args:
            i = args.index(flag)
            args.pop(i)
            taken.append(args.pop(i))
        return taken

    fitrs_paths = [Path(p) for p in _take("--fitrs")]
    firds_paths = [Path(p) for p in _take("--firds")]
    gleif_dirs = [Path(p) for p in _take("--gleif-cache")]
    manifest_args = _take("--manifest")
    if args:
        raise SystemExit(f"unexpected arguments: {args}")
    if not fitrs_paths or not firds_paths or not gleif_dirs or not manifest_args:
        raise SystemExit("missing required --fitrs/--firds/--gleif-cache/--manifest")
    manifest = Path(manifest_args[0])

    sample = load_frozen_sample(manifest)

    fitrs_records: list[FitrsRecord] = []
    fitrs_meta = []
    for zp in fitrs_paths:
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                if not name.lower().endswith(".xml"):
                    continue
                with z.open(name) as fh:
                    fitrs_records.extend(parse_fitrs(fh))
        fitrs_meta.append(_file_meta(zp))

    firds_instruments: list[FirdsInstrument] = []
    firds_meta = []
    for zp in firds_paths:
        with zipfile.ZipFile(zp) as z:
            for name in z.namelist():
                if not name.lower().endswith(".xml"):
                    continue
                with z.open(name) as fh:
                    firds_instruments.extend(iter_firds(fh))
        firds_meta.append(_file_meta(zp))

    gleif_entities: dict[str, GleifEntity] = {}
    gleif_meta = []
    for cache_dir in gleif_dirs:
        for payload in sorted(cache_dir.iterdir()):
            if not payload.is_file():
                continue
            raw = payload.read_bytes()
            entities = parse_gleif(raw)
            gleif_entities.update(entities)
            gleif_meta.append(
                {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "leis": sorted(entities),
                }
            )

    validation = validate_frozen_sample(sample, fitrs_records, firds_instruments, gleif_entities)

    doc = {
        "finding": "F-009",
        "artifact": "frozen_sample_validation",
        "status": "METADATA_ONLY",
        "frozen_sample": {
            "manifest": str(manifest),
            "status": sample.status,
            "isin_count": len(sample.isins),
            "sample_snapshot_sha256": sample.snapshot_sha256,
        },
        "semantics": (
            "es_legal_issuer_v1 (docs/gates/G0.md §3, F-008 corrected): "
            "MiFIR ID=BOND (FITRS FinInstrmClssfctn) AND Bond Type=CRPB "
            "(SACL criterion only; Desc is a cross-check and never sufficient "
            "alone) AND issuer LEI (FIRDS field 5) AND GLEIF "
            "LegalJurisdiction.country=ES. Frozen resolver decides every "
            "branch; no ISIN is ever re-drawn."
        ),
        "inputs": {
            "fitrs_files": fitrs_meta,
            "firds_files": firds_meta,
            "gleif_payloads": gleif_meta,
        },
        "summary": {
            "isin_count": len(validation.rows),
            "fitrs_observed": sum(1 for r in validation.rows if r.fitrs_observed),
            "firds_observed": sum(1 for r in validation.rows if r.firds_observed),
            "gleif_resolved": sum(1 for r in validation.rows if r.gleif_resolved),
            "branch_counts": validation.branch_counts,
        },
        "results": [r.to_manifest() for r in validation.rows],
        "hygiene": {
            "provider_payloads_in_repo": False,
            "note": "Raw FITRS/FIRDS/GLEIF bytes live in gitignored data/; only "
                    "file names, SHA-256 and byte counts are recorded here.",
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# F-009: frozen 15+5 sample validated against live FITRS+FIRDS+GLEIF\n"
        "# Metadata-only evidence. Generated by universe/sample_validation.py.\n"
        + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
