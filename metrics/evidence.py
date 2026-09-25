"""G0-E1 evidence generation (metadata only) — COVERAGE_MEASURED report.

Reads locally-captured raw objects (gitignored ``data/`` — the sealed A3
adjudication bundle), normalizes them, restricts to the VALIDATED INCLUDE
subset of the frozen 15+5 sample, and emits the cross-source metrics report
to ``evidence/g0-e1/``. NEVER writes provider payload content to evidence.

Reproducibility: same raw tree + same validated-scope artifact + same code
=> identical report bytes (deterministic ordering everywhere). The report's
``no_coverage_claim`` invariant is enforced at emission time: the metrics
payload is scanned for forbidden completeness / market-share /
absence-of-trading language and the run FAILS rather than emit a claiming
report. The ``interpretation_limits`` block is excluded from the scan — it
necessarily names the forbidden claims in negated form.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from normalization.evidence import _load_bloomberg_csv, _load_bme_json
from normalization.model import NormalizedRecord
from normalization.normalize import normalize_record
from universe.sample import load_frozen_sample
from universe.sample_validation import load_validated_scope

from .cross_source import compute_cross_source, scan_forbidden_claims

SOURCES = ("bme_apa", "blb_apae")


def _iter_raw_records(
    raw_root: Path,
) -> tuple[list[NormalizedRecord], list[dict[str, str]]]:
    """Normalize every raw object under raw_root/<source>/… (any capture
    version). Read-only; identical bytes may legitimately recur across
    capture versions — deduplication is a metric concern (chains), never a
    file concern.

    Returns ``(records, unparseable)``: files whose bytes cannot be decoded
    into source records (e.g. a legitimately captured empty response) yield
    no records and are surfaced as explicit diagnostics — never silently
    dropped, never fatal to the metric run.
    """
    records: list[NormalizedRecord] = []
    unparseable: list[dict[str, str]] = []
    for source in SOURCES:
        source_dir = raw_root / source
        if not source_dir.is_dir():
            continue
        for raw in sorted(source_dir.rglob("*")):
            if not raw.is_file() or raw.name.endswith(".meta.yaml"):
                continue
            loader = _load_bme_json if source == "bme_apa" \
                else _load_bloomberg_csv
            try:
                rows = loader(raw)
            except (json.JSONDecodeError, UnicodeDecodeError,
                    ValueError) as exc:
                unparseable.append({
                    "source": source,
                    "relpath": str(raw.relative_to(raw_root)),
                    "error": type(exc).__name__,
                })
                continue
            records.extend(normalize_record(source, r) for r in rows)
    return records, unparseable


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    *,
    bundle_dir: Path | None = None,
    window: dict[str, str] | None = None,
    a3_adjudication: dict[str, str] | None = None,
    date: str,
    generated_utc: str,
) -> dict[str, Any]:
    """Compute the COVERAGE_MEASURED report. ``generated_utc`` is injected
    (not read from the clock) so the metrics payload stays reproducible —
    the timestamp lives outside the metrics block.
    """
    records, unparseable = _iter_raw_records(raw_root)
    sample = load_frozen_sample()
    validated_scope = load_validated_scope(sample)
    metrics = compute_cross_source(records, validated_scope)
    metrics["unparseable_raw_objects"] = unparseable

    report: dict[str, Any] = {
        "gate": "G0-E",
        "issue_id": "G0-E1",
        "report": "COVERAGE_MEASURED",
        "date": date,
        "generated_utc": generated_utc,
        "generator": {
            "module": "metrics.evidence",
            "repo_commit": _git_head(),
            "deterministic": True,
        },
        "evidence_basis": {
            "raw_root": str(raw_root),
            "bundle": _bundle_basis(bundle_dir),
            "capture_window_utc": window or {},
            "a3_adjudication": a3_adjudication or {},
            "validated_scope_basis":
                "evidence/g0-b1/F-009_frozen_sample_validation.yaml",
            "frozen_sample_status": sample.status,
            "frozen_sample_manifest_sha256": sample.snapshot_sha256,
            "frozen_sample_isin_count": len(sample.isins),
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
    # covers metrics + semantics + basis strings; interpretation_limits
    # inside metrics is the only place such phrases may appear (negated).
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
    evidence_dir: Path,
    *,
    bundle_dir: Path | None = None,
    window: dict[str, str] | None = None,
    a3_adjudication: dict[str, str] | None = None,
    date: str,
    generated_utc: str,
) -> Path:
    report = build_report(
        raw_root, bundle_dir=bundle_dir, window=window,
        a3_adjudication=a3_adjudication, date=date,
        generated_utc=generated_utc)
    ev = evidence_dir / "g0-e1"
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / "G0-E1_coverage_measured.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(report, fh, sort_keys=False)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-root", type=Path, required=True,
                    help="raw tree to measure (e.g. the sealed A3 bundle's "
                         "raw/ directory)")
    ap.add_argument("--bundle-dir", type=Path, default=None,
                    help="bundle root for provenance metadata "
                         "(collection-meta.yaml + SHA256SUMS.txt)")
    ap.add_argument("--out", type=Path, default=Path("evidence"),
                    help="evidence root (report lands in <out>/g0-e1/)")
    ap.add_argument("--date", required=True)
    ap.add_argument("--generated-utc", required=True)
    ap.add_argument("--window", default=None,
                    help="JSON object: adjudicated capture window, e.g. "
                         '{"operational_start": "...", "subject_period_end":'
                         ' "...", "evidence_tail_end": "..."}')
    ap.add_argument("--a3-adjudication", default=None,
                    help="JSON object: A3 adjudication provenance, e.g. "
                         '{"issue": "...", "verdict": "COMPLETE_SUPPORTED", '
                         '"adjudicator": "g0-a3-adjudicator-v2"}')
    args = ap.parse_args(argv)
    path = generate_report(
        args.raw_root, args.out, bundle_dir=args.bundle_dir,
        window=json.loads(args.window) if args.window else None,
        a3_adjudication=(json.loads(args.a3_adjudication)
                         if args.a3_adjudication else None),
        date=args.date, generated_utc=args.generated_utc)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
