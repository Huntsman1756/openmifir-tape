"""G0-E1 evidence generation (metadata only) — COVERAGE_MEASURED report.

The E1 input corpus is defined by window-eligible A3 evidence
(``metrics.selection``): run manifests whose ``results.observed_at`` lies
inside ``[operational_start, evidence_tail_end]`` attest the exact capture
versions to parse. The whole raw store is NOT the corpus — the bundle was
collected after evidence_tail_end and legitimately contains post-tail
captures. Source ``publication_timestamp`` is never a selection criterion.

Read-only over the locally-captured raw objects (gitignored ``data/`` — the
sealed A3 adjudication bundle). NEVER writes provider payload content to
evidence.

Reproducibility: same sealed bundle + same validated-scope artifact + same
generator code => identical metrics payload (deterministic ordering
everywhere). ``generator.code_commit`` records the exact commit the report
was generated from — generation MUST run from a checkout at that commit.

``no_coverage_claim`` is enforced at emission: the metrics payload is
scanned for forbidden completeness / market-share / absence-of-trading
language and the run FAILS rather than emit a claiming report. The
``interpretation_limits`` block is excluded from the scan — it necessarily
names the forbidden claims in negated form.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from normalization.evidence import _load_bloomberg_csv, _load_bme_json
from normalization.model import NormalizedRecord
from normalization.normalize import normalize_record
from universe.sample import load_frozen_sample
from universe.sample_validation import load_validated_scope

from .cross_source import compute_cross_source, scan_forbidden_claims
from .selection import SelectionError, select_eligible

SOURCES = ("bme_apa", "blb_apae")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_ts(text: str) -> datetime:
    dt = datetime.fromisoformat(text.strip())
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {text!r}")
    return dt.astimezone(UTC)


def _load_version(
    v,
) -> tuple[list[NormalizedRecord] | None, str | None]:
    """Parse+normalize one capture version; keep record->version linkage.

    Content integrity is verified: bytes whose sha256 diverges from the
    manifest-recorded capture_version abort the run (fail closed). A
    version whose bytes cannot be decoded returns ``(None, reason)``.
    """
    data = v.path.read_bytes()
    if _sha256_bytes(data) != v.capture_version:
        raise SelectionError(
            f"capture_version/content divergence: {v.source_id}/"
            f"{v.source_object_id}/{v.capture_version} at {v.path}")
    loader = _load_bme_json if v.source_id == "bme_apa" \
        else _load_bloomberg_csv
    try:
        rows = loader(v.path)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return None, type(exc).__name__
    return [normalize_record(v.source_id, r) for r in rows], None


def _load_eligible_records(
    selection: dict[str, Any],
) -> tuple[list[NormalizedRecord], list[dict[str, str]]]:
    """Parse+normalize every selected capture version.

    Content integrity is verified: bytes whose sha256 diverges from the
    manifest-recorded capture_version abort the run (fail closed). A version
    whose bytes cannot be decoded into source records (e.g. a legitimately
    captured empty payload) is an explicit diagnostic — identified by
    (source_id, source_object_id, capture_version) — never silently dropped.
    """
    records: list[NormalizedRecord] = []
    unparseable: list[dict[str, str]] = []
    for v in selection["versions"]:
        recs, err = _load_version(v)
        if err is not None:
            unparseable.append({
                "source_id": v.source_id,
                "source_object_id": v.source_object_id,
                "capture_version": v.capture_version,
                "reason": err,
            })
            continue
        records.extend(recs)
    return records, unparseable


def _git_head() -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv on a resolved git path
            [git, "rev-parse", "HEAD"], capture_output=True, check=True,
            text=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _bundle_basis(bundle_dir: Path | None) -> dict[str, Any]:
    """Metadata-only provenance block for the input evidence bundle."""
    if bundle_dir is None:
        return {}
    basis: dict[str, Any] = {"bundle_dir": str(bundle_dir)}
    meta = bundle_dir / "collection-meta.yaml"
    if meta.is_file():
        doc = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
        basis["bundle_id"] = doc.get("bundle_id")
        basis["collection_window_utc"] = doc.get("collection_window_utc")
        basis["deployed_head"] = doc.get("deployed_head")
    sums = bundle_dir / "SHA256SUMS.txt"
    if sums.is_file():
        basis["bundle_manifest_sha256"] = _sha256(sums)
    return basis


def build_report(
    raw_root: Path,
    runs_dir: Path,
    *,
    window: dict[str, str],
    bundle_dir: Path | None = None,
    a3_adjudication: dict[str, str] | None = None,
    date: str,
    generated_utc: str,
) -> dict[str, Any]:
    """Compute the COVERAGE_MEASURED report.

    ``window`` MUST carry ``operational_start`` and ``evidence_tail_end``:
    they select the eligible A3 manifests and thus define the input corpus.
    ``generated_utc`` is injected (not read from the clock) so the metrics
    payload stays reproducible — the timestamp lives outside it.
    """
    if not window or not window.get("operational_start") \
            or not window.get("evidence_tail_end"):
        raise ValueError(
            "window.operational_start and window.evidence_tail_end are "
            "required — they define the eligible-evidence corpus")
    start = _parse_ts(window["operational_start"])
    tail = _parse_ts(window["evidence_tail_end"])

    selection = select_eligible(runs_dir, raw_root, start=start,
                                tail_end=tail)
    records, unparseable = _load_eligible_records(selection)
    census = dict(selection["census"])
    census["raw_versions_parsed"] = \
        census["unique_capture_versions_eligible"] - len(unparseable)

    sample = load_frozen_sample()
    validated_scope = load_validated_scope(sample)
    metrics = compute_cross_source(records, validated_scope)
    metrics["unparseable_raw_objects"] = unparseable
    metrics["input_census"] = census

    report: dict[str, Any] = {
        "gate": "G0-E",
        "issue_id": "G0-E1",
        "report": "COVERAGE_MEASURED",
        "date": date,
        "generated_utc": generated_utc,
        "generator": {
            "module": "metrics.evidence",
            # Exact commit this report was generated from — the reviewable
            # code authority. Generation MUST run at this commit.
            "code_commit": _git_head(),
            "deterministic": True,
        },
        "evidence_basis": {
            "raw_root": str(raw_root),
            "runs_dir": str(runs_dir),
            "bundle": _bundle_basis(bundle_dir),
            "capture_window_utc": window,
            "a3_adjudication": a3_adjudication or {},
            "validated_scope_basis":
                "evidence/g0-b1/F-009_frozen_sample_validation.yaml",
            "frozen_sample_status": sample.status,
            "frozen_sample_manifest_sha256": sample.snapshot_sha256,
            "frozen_sample_isin_count": len(sample.isins),
        },
        "input_selection": {
            "rule": (
                "run manifest results.observed_at within "
                "[operational_start, evidence_tail_end]; input = unique "
                "(source_id, source_object_id, capture_version) attested "
                "by eligible manifests. publication_timestamp is NOT a "
                "selection criterion; pre-window and post-tail manifests "
                "never contribute records."),
            **census,
            "excluded_manifest_names": selection["excluded"],
        },
        "semantics": (
            "Descriptive metrics over an INTACT capture (G0-A3 "
            "COMPLETE_SUPPORTED), restricted to the validated INCLUDE "
            "scope. Observed/unobserved refer to this capture only."),
        "metrics": metrics,
        "hygiene": {
            "provider_payloads_in_repo": False,
            "raw_directory_tracked": False,
        },
    }

    # NO_COVERAGE_CLAIM: the metrics payload must not contain forbidden
    # completeness / market-share / absence-of-trading language. The scan
    # covers metrics + semantics; interpretation_limits inside metrics is
    # the only place such phrases may appear (negated).
    metrics_scan = {k: v for k, v in metrics.items()
                    if k != "interpretation_limits"}
    scan_text = yaml.safe_dump(
        {"semantics": report["semantics"], "metrics": metrics_scan})
    hits = scan_forbidden_claims(scan_text)
    if hits:
        raise ValueError(
            f"NO_COVERAGE_CLAIM violated — forbidden claim language in "
            f"report payload: {hits}")
    return report


def generate_report(
    raw_root: Path,
    runs_dir: Path,
    evidence_dir: Path,
    *,
    window: dict[str, str],
    bundle_dir: Path | None = None,
    a3_adjudication: dict[str, str] | None = None,
    date: str,
    generated_utc: str,
) -> Path:
    report = build_report(
        raw_root, runs_dir, window=window, bundle_dir=bundle_dir,
        a3_adjudication=a3_adjudication, date=date,
        generated_utc=generated_utc)
    ev = evidence_dir / "g0-e1"
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / "G0-E1_coverage_measured.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(report, fh, sort_keys=False)
    sha_path = ev / "G0-E1_coverage_measured.sha256.txt"
    sha_path.write_text(f"{_sha256(path)} *{path.name}\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-root", type=Path, required=True,
                    help="raw tree to measure (e.g. the sealed A3 bundle's "
                         "raw/ directory)")
    ap.add_argument("--runs-dir", type=Path, required=True,
                    help="A3 run manifests directory (e.g. the sealed "
                         "bundle's runs/ directory)")
    ap.add_argument("--bundle-dir", type=Path, default=None,
                    help="bundle root for provenance metadata "
                         "(collection-meta.yaml + SHA256SUMS.txt)")
    ap.add_argument("--out", type=Path, default=Path("evidence"),
                    help="evidence root (report lands in <out>/g0-e1/)")
    ap.add_argument("--date", required=True)
    ap.add_argument("--generated-utc", required=True)
    ap.add_argument("--window", required=True,
                    help="JSON object with the adjudicated window; MUST "
                         "include operational_start and evidence_tail_end, "
                         "e.g. {\"operational_start\": \"...\", "
                         "\"subject_period_end\": \"...\", "
                         "\"evidence_tail_end\": \"...\"}")
    ap.add_argument("--a3-adjudication", default=None,
                    help="JSON object: A3 adjudication provenance, e.g. "
                         '{"issue": "...", "verdict": "COMPLETE_SUPPORTED", '
                         '"adjudicator": "g0-a3-adjudicator-v2"}')
    args = ap.parse_args(argv)
    path = generate_report(
        args.raw_root, args.runs_dir, args.out,
        window=json.loads(args.window), bundle_dir=args.bundle_dir,
        a3_adjudication=(json.loads(args.a3_adjudication)
                         if args.a3_adjudication else None),
        date=args.date, generated_utc=args.generated_utc)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
