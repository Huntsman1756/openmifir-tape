"""G1-B1 — OBSERVED_UNIVERSE_DISPOSITION (frozen docs/gates/G1.md §3).

Batch resolution of the verified G1-A OBSERVED_INSTRUMENT_SET through the
frozen ``es_legal_issuer_v1`` chain (docs/gates/G0.md §3, unchanged):

    FITRS auth.045:  MiFIR ID = BOND AND Bond Type = CRPB (SACL primary)
        -> FIRDS RTS 23 field 5: issuer LEI
        -> GLEIF: LegalJurisdiction.country = ES
        -> INCLUDE / EXCLUDE / QUARANTINE / CONFLICT

Branch decisions come EXCLUSIVELY from
``universe.resolver.resolve_disposition`` — this module assembles the
resolver's inputs (shared ``universe.assemble`` semantics) and records the
outcomes; it implements no precedence of its own.

Input authority is pinned, not recomputed: the G1-A artifact MUST carry the
verified semantic digest or the run fails closed. Reference inputs are
frozen BUILD_SNAPSHOT sets cited per family as canonical
``{name, sha256, bytes}`` entries + ``set_digest`` (``universe.reference``,
G1 §4.4 representation — the same shape G1-C capsules will cite).

GLEIF completeness rule (hard): ``LEI absent from local cache`` is NOT
"authoritatively unresolved". Every LEI that reaches the resolver's
jurisdiction step must have an explicit acquisition outcome —
RESOLVED / NOT_FOUND(authoritative) / ACQUISITION_FAILED / NOT_ATTEMPTED.
Any FAILED/NOT_ATTEMPTED required LEI raises ``AcquisitionIncomplete`` —
a HOLD, never a fabricated QUARANTINE.

Output: metadata-only report YAML + canonical ISIN-sorted disposition rows
(JSONL). No provider payload bytes, no filesystem paths, deterministic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from universe._paths import repo_file
from universe.assemble import assemble_security_record, required_gleif_leis
from universe.firds import FirdsInstrument, iter_firds
from universe.fitrs import FitrsRecord, parse_fitrs
from universe.gleif import GleifEntity
from universe.gleif_acquire import (
    MANIFEST_NAME,
    OUTCOME_ACQUISITION_FAILED,
    OUTCOME_NOT_ATTEMPTED,
    OUTCOME_NOT_FOUND,
    OUTCOMES,
    load_acquired_entities,
)
from universe.model import Branch
from universe.reference import (
    reference_set_from_dir,
    reference_set_from_files,
)
from universe.resolver import resolve_disposition

from .evidence import _git_head

# Verified G1-A1 authority (issue #20). The batch run consumes exactly this
# observed set — anything else fails closed before any resolution happens.
G1A_ARTIFACT = repo_file(
    "evidence", "g1-a1", "G1-A1_observed_instrument_set.yaml")
EXPECTED_G1A_SEMANTIC_DIGEST = \
    "f87b1b2fe5c35a506885036ad153494d93a67cedc7fe74062e893223c5139f71"

ROWS_NAME = "G1-B1_disposition_rows.jsonl"
REPORT_NAME = "G1-B1_observed_universe_disposition.yaml"

# Row-schema legend (JSONL rows stay compact to remain well inside the
# evidence size budget; every key is documented here).
ROW_SCHEMA = {
    "isin": "observed instrument identifier (frozen G1-A input)",
    "fitrs": {
        "n": "FITRS records observed for the ISIN",
        "mifir": "resolved MiFIR ID (null = absent/undetermined)",
        "bond": "resolved bond type (null = absent/undetermined)",
        "sacl": "observed SACL criterion",
        "desc": "observed Desc label (emitted only when it diverges "
                "from the SACL-mapped label or SACL is absent)",
        "amb": "FITRS fields with contradictory observations",
    },
    "firds": {
        "n": "FIRDS records observed for the ISIN",
        "lei": "resolved issuer LEI (null = absent/contradictory)",
        "conflict": "FIRDS issuer-LEI contradiction flag",
    },
    "gleif": {
        "acq": "acquisition outcome for the resolved LEI "
               "(RESOLVED/NOT_FOUND; null = lookup not required)",
        "ok": "GLEIF entity resolved with a LegalJurisdiction country",
        "cc": "LegalJurisdiction.country (ISO 3166 alpha-2)",
    },
    "branch": "frozen disposition (INCLUDE/EXCLUDE/QUARANTINE/CONFLICT)",
    "reason": "frozen reason code",
    "notes": "diagnostics (absent = none)",
}


class G1BError(Exception):
    """Fail-closed abort for G1-B (input-authority mismatch, broken
    invariants, missing inputs)."""


def load_observed_isins(
    artifact_path: Path = G1A_ARTIFACT,
    *,
    expected_digest: str = EXPECTED_G1A_SEMANTIC_DIGEST,
) -> list[str]:
    """Load the verified G1-A observed set; fail closed on any deviation.

    The semantic digest IS the authority check: a regenerated or mutated
    observed set cannot pass. ISINs are returned canonically sorted.
    """
    if not artifact_path.is_file():
        raise G1BError(f"G1-A artifact not found: {artifact_path}")
    doc = yaml.safe_load(artifact_path.read_bytes().decode("utf-8"))
    observed = (doc or {}).get("observed_instrument_set") or {}
    digest = observed.get("semantic_digest")
    if digest != expected_digest:
        raise G1BError(
            f"G1-A semantic digest mismatch: {digest!r} != "
            f"{expected_digest!r} — refusing to resolve an unverified "
            "observed set")
    instruments = observed.get("instruments") or []
    isins = [e.get("isin") for e in instruments if isinstance(e, dict)]
    if any(not isinstance(i, str) or not i.strip() for i in isins):
        raise G1BError("G1-A artifact contains an empty/non-string ISIN")
    declared = observed.get("instrument_count")
    if declared != len(instruments):
        raise G1BError(
            f"G1-A instrument_count {declared!r} != {len(instruments)} rows")
    if len(set(isins)) != len(isins):
        raise G1BError("G1-A artifact contains duplicate ISINs")
    return sorted(isins)


def scan_fitrs(
    zip_paths: list[Path],
    wanted: set[str],
) -> dict[str, list[FitrsRecord]]:
    """Stream the pinned FITRS zips; keep records for observed ISINs only.

    ``parse_fitrs`` iterparses each member, so multi-hundred-MB XML is never
    materialized as a DOM; records outside the observed set are dropped
    immediately.
    """
    by_isin: dict[str, list[FitrsRecord]] = {}
    for zp in sorted(zip_paths, key=lambda p: p.name):
        if not zp.is_file():
            raise G1BError(f"FITRS snapshot missing: {zp}")
        with zipfile.ZipFile(zp) as z:
            for name in sorted(z.namelist()):
                if not name.lower().endswith(".xml"):
                    continue
                with z.open(name) as fh:
                    for record in parse_fitrs(fh):
                        if record.instrument_isin in wanted:
                            by_isin.setdefault(
                                record.instrument_isin, []).append(record)
    return by_isin


def scan_firds(
    zip_paths: list[Path],
    wanted: set[str],
) -> dict[str, list[FirdsInstrument]]:
    """Stream the pinned FIRDS zips; keep records for observed ISINs only."""
    by_isin: dict[str, list[FirdsInstrument]] = {}
    for zp in sorted(zip_paths, key=lambda p: p.name):
        if not zp.is_file():
            raise G1BError(f"FIRDS snapshot missing: {zp}")
        with zipfile.ZipFile(zp) as z:
            for name in sorted(z.namelist()):
                if not name.lower().endswith(".xml"):
                    continue
                with z.open(name) as fh:
                    for instrument in iter_firds(fh):
                        if instrument.instrument_isin in wanted:
                            by_isin.setdefault(
                                instrument.instrument_isin, []).append(
                                    instrument)
    return by_isin


def _desc_is_diagnostic(assembled) -> bool:
    """Emit ``desc`` only where it carries evidence the code path used:
    SACL absent/unknown, or a label that could not be cross-checked."""
    if assembled.desc is None:
        return False
    return assembled.sacl is None or assembled.bond_type is None


def _row(isin: str, assembled, disp, gleif_acquisition: str | None) -> dict:
    """One canonical disposition row (see ROW_SCHEMA)."""
    fitrs: dict[str, Any] = {
        "n": assembled.fitrs_count,
        "mifir": assembled.mifir_id,
        "bond": assembled.bond_type,
        "sacl": assembled.sacl,
    }
    if _desc_is_diagnostic(assembled):
        fitrs["desc"] = assembled.desc
    ambiguous = [f for f, flag in (
        ("mifir", assembled.mifir_ambiguous),
        ("bond", assembled.bond_type_ambiguous)) if flag]
    if ambiguous:
        fitrs["amb"] = ambiguous
    firds: dict[str, Any] = {
        "n": assembled.firds_count,
        "lei": assembled.issuer_lei,
    }
    if assembled.lei_conflict:
        firds["conflict"] = True
    gleif: dict[str, Any] = {
        "acq": gleif_acquisition,
        "ok": bool(assembled.gleif and assembled.gleif.resolved),
        "cc": (assembled.gleif.legal_jurisdiction_country
               if assembled.gleif else None),
    }
    row: dict[str, Any] = {
        "isin": isin,
        "fitrs": fitrs,
        "firds": firds,
        "gleif": gleif,
        "branch": disp.branch.value,
        "reason": disp.reason.value,
    }
    if assembled.notes:
        row["notes"] = list(assembled.notes)
    return row


def canonical_rows_bytes(rows: list[dict[str, Any]]) -> bytes:
    """Canonical serialization: ISIN-sorted, one compact JSON object per
    line — the semantic-digest material."""
    ordered = sorted(rows, key=lambda r: r["isin"])
    return ("\n".join(
        json.dumps(r, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False) for r in ordered) + "\n") \
        .encode("utf-8")


def build_dispositions(
    isins: list[str],
    fitrs_by_isin: dict[str, list[FitrsRecord]],
    firds_by_isin: dict[str, list[FirdsInstrument]],
    gleif_entities: dict[str, GleifEntity],
    gleif_census: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve every observed ISIN; return (rows, diagnostics).

    100% disposition invariant — enforced here before any evidence exists:
    exactly one row per distinct input ISIN, every row carrying one frozen
    branch + one frozen reason. Any violation raises ``G1BError``.
    """
    if len(set(isins)) != len(isins):
        raise G1BError("duplicate input ISINs rejected before resolution")
    rows: list[dict[str, Any]] = []
    diagnostics = {
        "fitrs_mifir_id_ambiguous_isins": 0,
        "fitrs_bond_type_ambiguous_isins": 0,
        "firds_issuer_lei_conflict_isins": 0,
        "isins_absent_from_fitrs": 0,
        "isins_absent_from_firds": 0,
        "gleif_resolved_isins": 0,
        "gleif_not_found_isins": 0,
    }
    for isin in sorted(isins):
        assembled = assemble_security_record(
            isin,
            fitrs_by_isin.get(isin) or [],
            firds_by_isin.get(isin) or [],
            gleif_entities,
        )
        disp = resolve_disposition(assembled.record)
        lei = assembled.issuer_lei
        gleif_acq = None
        if lei:
            gleif_acq = gleif_census.get(lei.strip().upper())
        rows.append(_row(isin, assembled, disp, gleif_acq))
        if assembled.mifir_ambiguous:
            diagnostics["fitrs_mifir_id_ambiguous_isins"] += 1
        if assembled.bond_type_ambiguous:
            diagnostics["fitrs_bond_type_ambiguous_isins"] += 1
        if assembled.lei_conflict:
            diagnostics["firds_issuer_lei_conflict_isins"] += 1
        if not assembled.fitrs_count:
            diagnostics["isins_absent_from_fitrs"] += 1
        if not assembled.firds_count:
            diagnostics["isins_absent_from_firds"] += 1
        if assembled.gleif and assembled.gleif.resolved:
            diagnostics["gleif_resolved_isins"] += 1
        if gleif_acq == OUTCOME_NOT_FOUND:
            diagnostics["gleif_not_found_isins"] += 1

    branches = Counter(r["branch"] for r in rows)
    reasons = Counter(r["reason"] for r in rows)
    unknown = set(branches) - {b.value for b in Branch}
    if unknown:
        raise G1BError(f"unknown disposition branches emitted: {unknown}")
    if len(rows) != len(isins) or {r["isin"] for r in rows} != set(isins):
        raise G1BError("100% disposition invariant violated")
    if sum(branches.values()) != len(isins):
        raise G1BError("disposition branch counts do not cover every ISIN")
    diagnostics["branch_counts"] = {
        b.value: branches.get(b.value, 0) for b in Branch}
    diagnostics["reason_counts"] = dict(sorted(reasons.items()))
    diagnostics["fitrs_observed_isins"] = \
        len(isins) - diagnostics["isins_absent_from_fitrs"]
    diagnostics["firds_observed_isins"] = \
        len(isins) - diagnostics["isins_absent_from_firds"]
    return rows, diagnostics


def build_report(
    observed_path: Path,
    fitrs_paths: list[Path],
    firds_paths: list[Path],
    gleif_dir: Path,
    *,
    date: str,
    generated_utc: str,
    expected_g1a_digest: str = EXPECTED_G1A_SEMANTIC_DIGEST,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the OBSERVED_UNIVERSE_DISPOSITION report + canonical rows.

    Raises ``AcquisitionIncomplete`` (HOLD) when any required LEI lacks a
    terminal acquisition outcome; raises ``G1BError`` on input-authority or
    invariant violations.
    """
    isins = load_observed_isins(observed_path,
                                expected_digest=expected_g1a_digest)
    wanted = set(isins)

    fitrs_set = reference_set_from_files(fitrs_paths)
    firds_set = reference_set_from_files(firds_paths)
    fitrs_by_isin = scan_fitrs(fitrs_paths, wanted)
    firds_by_isin = scan_firds(firds_paths, wanted)

    required = required_gleif_leis(isins, firds_by_isin)
    gleif_entities, gleif_census = load_acquired_entities(gleif_dir, required)
    gleif_set = reference_set_from_dir(gleif_dir,
                                     exclude_names=(MANIFEST_NAME,))

    rows, diagnostics = build_dispositions(
        isins, fitrs_by_isin, firds_by_isin, gleif_entities, gleif_census)
    rows_bytes = canonical_rows_bytes(rows)
    disposition_digest = hashlib.sha256(rows_bytes).hexdigest()

    acquisition_counts = {o: sum(1 for v in gleif_census.values() if v == o)
                          for o in OUTCOMES}
    complete = not (acquisition_counts[OUTCOME_ACQUISITION_FAILED]
                    or acquisition_counts[OUTCOME_NOT_ATTEMPTED])
    if not complete:
        raise G1BError(  # defensive: load_acquired_entities already raised
            "GLEIF acquisition census is incomplete")

    report: dict[str, Any] = {
        "gate": "G1",
        "issue_id": "G1-B1",
        "report": "OBSERVED_UNIVERSE_DISPOSITION",
        "date": date,
        "generated_utc": generated_utc,
        "frozen_authority": {
            "spec": "docs/gates/G1.md#G1-B + docs/gates/G0.md §3 "
                    "(es_legal_issuer_v1)",
            "freeze_tag": "g1-freeze-v0",
            "freeze_commit": "2a5e4684b9a3f6066e6d2a53cbc37b920e2c9ffa",
        },
        "input_authority": {
            "artifact": "evidence/g1-a1/G1-A1_observed_instrument_set.yaml",
            "semantic_digest": expected_g1a_digest,
            "observed_identifier_count": len(isins),
        },
        "generator": {
            "module": "metrics.g1b",
            "code_commit": _git_head(),
            "deterministic": True,
        },
        "reference_basis": "BUILD_SNAPSHOT",
        "reference_sets": {
            "fitrs": fitrs_set,
            "firds": firds_set,
            "gleif": gleif_set,
        },
        "reference_binding": (
            "every row in the disposition rows file resolved against the "
            "three pinned family sets above; later capsules cite the row "
            "plus these set_digests"),
        "reference_acquisition": {
            "gleif": {
                "interface": "GLEIF API v1 /lei-records/{lei} "
                             "(api.gleif.org)",
                "required_leis": len(required),
                "outcome_counts": acquisition_counts,
                "complete": complete,
            },
        },
        "row_schema": ROW_SCHEMA,
        "semantics": (
            "Batch es_legal_issuer_v1 resolution over the verified G1-A "
            "OBSERVED_INSTRUMENT_SET. Every observed identifier received "
            "exactly one frozen disposition via universe.resolver."
            "resolve_disposition; QUARANTINE and CONFLICT are valid domain "
            "outcomes, not run failures. No ISIN-prefix heuristics, no "
            "fuzzy issuer matching, no manual overrides, no provider "
            "payload bytes."),
        "dispositions": {
            "identifier_count": len(rows),
            "rows_file": ROWS_NAME,
            "rows_sha256": disposition_digest,
            "disposition_semantic_digest": disposition_digest,
            **diagnostics,
        },
        "hygiene": {
            "provider_payloads_in_repo": False,
            "raw_directory_tracked": False,
        },
    }
    return report, rows


def generate_report(
    observed_path: Path,
    fitrs_paths: list[Path],
    firds_paths: list[Path],
    gleif_dir: Path,
    evidence_dir: Path,
    *,
    date: str,
    generated_utc: str,
) -> Path:
    report, rows = build_report(
        observed_path, fitrs_paths, firds_paths, gleif_dir,
        date=date, generated_utc=generated_utc)
    ev = evidence_dir / "g1-b1"
    ev.mkdir(parents=True, exist_ok=True)
    rows_path = ev / ROWS_NAME
    rows_path.write_bytes(canonical_rows_bytes(rows))
    path = ev / REPORT_NAME
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(report, fh, sort_keys=False)
    for artifact in (path, rows_path):
        sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        sidecar = artifact.parent / f"{artifact.stem}.sha256.txt"
        sidecar.write_text(f"{sha} *{artifact.name}\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--observed-set", type=Path, default=G1A_ARTIFACT,
                    help="verified G1-A OBSERVED_INSTRUMENT_SET artifact")
    ap.add_argument("--fitrs", type=Path, nargs="+", required=True,
                    help="pinned FITRS snapshot zip(s)")
    ap.add_argument("--firds", type=Path, nargs="+", required=True,
                    help="pinned FIRDS snapshot zip(s)")
    ap.add_argument("--gleif-dir", type=Path, required=True,
                    help="pinned GLEIF acquisition directory "
                         "(<LEI>.json / <LEI>.notfound.json)")
    ap.add_argument("--out", type=Path, default=Path("evidence"))
    ap.add_argument("--date", required=True)
    ap.add_argument("--generated-utc", required=True)
    args = ap.parse_args(argv)
    path = generate_report(
        args.observed_set, args.fitrs, args.firds, args.gleif_dir,
        args.out, date=args.date, generated_utc=args.generated_utc)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
