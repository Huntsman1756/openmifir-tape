"""G1-A1 — OBSERVED_INSTRUMENT_SET (frozen docs/gates/G1.md §2).

Instrument-level census of the window-eligible A3 corpus: which instrument
identifiers appeared at all, in which sources, and over which eligible
observation span. This is NOT a trade tape and NOT a regulatory
disposition — ``es_legal_issuer_v1`` is NOT applied here.

Per-instrument observation span: ``first_observed_at`` is the minimum
``first_observed_at`` (canonical known_at basis, frozen G1 §4.2) over all
eligible capture versions containing the instrument;
``last_observed_at`` is the maximum ``last_observed_at`` likewise.
Instrument granularity deliberately collapses records — G1-A counts the
observed surface, not trades.

Canonical ordering: instruments sorted by identifier. Semantic digest:
sha256 over canonical JSON lines of the instrument entries — no absolute
paths, no machine-specific material.

Metadata-only: identifiers, counts, timestamps, digests — never provider
payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .evidence import (
    _bundle_basis,
    _git_head,
    _load_version,
    _parse_ts,
)
from .selection import select_eligible


def _filtered_bundle_basis(bundle_dir: Path | None) -> dict[str, Any]:
    """Bundle provenance minus operator-specific absolute paths."""
    basis = _bundle_basis(bundle_dir)
    basis.pop("bundle_dir", None)
    return basis


def _canon_ts(dt: datetime) -> str:
    """Canonical UTC timestamp: microseconds + 'Z'."""
    return (dt.astimezone(UTC)
            .strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z")


def _digest_entries(entries: list[dict[str, Any]]) -> str:
    """Semantic digest: sha256 over canonical JSON lines — no machine
    paths, stable key order, one line per instrument."""
    canon = "\n".join(
        json.dumps(e, sort_keys=True, separators=(",", ":"))
        for e in entries)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def build_observed_instrument_set(
    raw_root: Path,
    runs_dir: Path,
    *,
    window: dict[str, str],
) -> dict[str, Any]:
    """Build the instrument-level observed set + census. Deterministic.

    Fails closed through ``select_eligible``/``_load_version`` on
    malformed or missing evidence. Unparseable payloads and records
    without an instrument identifier are explicit diagnostics.
    """
    start = _parse_ts(window["operational_start"])
    tail = _parse_ts(window["evidence_tail_end"])
    selection = select_eligible(runs_dir, raw_root, start=start,
                                tail_end=tail)

    per_isin: dict[str, dict[str, Any]] = {}
    unparseable: list[dict[str, str]] = []
    records_total = 0
    records_no_isin = 0
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
        # One capture identity counts once per instrument, regardless of
        # how many rows for that instrument the capture contains.
        capture_isins: set[str] = set()
        for r in recs:
            records_total += 1
            isin = (r.instrument_identification_code or "").strip()
            if not isin:
                records_no_isin += 1
                continue
            capture_isins.add(isin)
        for isin in capture_isins:
            slot = per_isin.setdefault(isin, {
                "sources": set(), "first": v.first_observed_at,
                "last": v.last_observed_at, "cvs": 0})
            slot["sources"].add(v.source_id)
            if v.first_observed_at is not None and (
                    slot["first"] is None
                    or v.first_observed_at < slot["first"]):
                slot["first"] = v.first_observed_at
            if v.last_observed_at is not None and (
                    slot["last"] is None
                    or v.last_observed_at > slot["last"]):
                slot["last"] = v.last_observed_at
            slot["cvs"] += 1

    instruments = [{
        "isin": isin,
        "sources": sorted(d["sources"]),
        "first_observed_at": _canon_ts(d["first"]) if d["first"] else None,
        "last_observed_at": _canon_ts(d["last"]) if d["last"] else None,
        # distinct (source_id, source_object_id, capture_version) tuples
        # containing >=1 record for this instrument
        "eligible_capture_identities": d["cvs"],
    } for isin, d in sorted(per_isin.items())]

    srcs = sorted({s for d in per_isin.values() for s in d["sources"]})
    by_source = {s: sum(1 for d in per_isin.values() if s in d["sources"])
                 for s in srcs}
    both = sum(1 for d in per_isin.values() if len(d["sources"]) > 1)

    return {
        "identifier_field": "instrument_identification_code",
        "instrument_count": len(instruments),
        "by_source_instrument_counts": by_source,
        "instruments_in_multiple_sources": both,
        "instruments": instruments,
        "semantic_digest": _digest_entries(instruments),
        "input_census": selection["census"],
        "excluded_manifest_names": selection["excluded"],
        "unparseable_raw_objects": unparseable,
        "records_total_in_scope_files": records_total,
        "records_without_instrument_identifier": records_no_isin,
    }


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
    observed = build_observed_instrument_set(raw_root, runs_dir,
                                             window=window)
    return {
        "gate": "G1",
        "issue_id": "G1-A1",
        "report": "OBSERVED_INSTRUMENT_SET",
        "date": date,
        "generated_utc": generated_utc,
        "frozen_authority": {
            "spec": "docs/gates/G1.md#G1-A",
            "freeze_tag": "g1-freeze-v0",
            "freeze_commit": "2a5e4684b9a3f6066e6d2a53cbc37b920e2c9ffa",
        },
        "generator": {
            "module": "metrics.g1a",
            "code_commit": _git_head(),
            "deterministic": True,
        },
        "evidence_basis": {
            # Logical layout only — committed evidence must be portable
            # across filesystem locations; no machine-specific paths.
            "input_layout": "sealed_bundle/{runs,raw}",
            "bundle": _filtered_bundle_basis(bundle_dir),
            "capture_window_utc": window,
            "a3_adjudication": a3_adjudication or {},
        },
        "semantics": (
            "Instrument-level census of window-eligible A3 evidence only; "
            "no regulatory disposition applied, no coverage inference. "
            "Membership describes what was observed in an intact capture."),
        "observed_instrument_set": observed,
        "hygiene": {
            "provider_payloads_in_repo": False,
            "raw_directory_tracked": False,
        },
    }


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
    report = build_report(raw_root, runs_dir, window=window,
                          bundle_dir=bundle_dir,
                          a3_adjudication=a3_adjudication,
                          date=date, generated_utc=generated_utc)
    ev = evidence_dir / "g1-a1"
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / "G1-A1_observed_instrument_set.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(report, fh, sort_keys=False)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    (ev / "G1-A1_observed_instrument_set.sha256.txt").write_text(
        f"{sha} *{path.name}\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-root", type=Path, required=True)
    ap.add_argument("--runs-dir", type=Path, required=True)
    ap.add_argument("--bundle-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=Path("evidence"))
    ap.add_argument("--date", required=True)
    ap.add_argument("--generated-utc", required=True)
    ap.add_argument("--window", required=True,
                    help="JSON with operational_start + evidence_tail_end")
    ap.add_argument("--a3-adjudication", default=None)
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
