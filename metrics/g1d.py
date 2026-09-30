"""G1-D2 authenticated observed fragmentation; frozen G1 section 5."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import yaml

from universe._paths import repo_file

from .cross_source import INTERPRETATION_LIMITS, compute_cross_source, scan_forbidden_claims
from .divergence import SOURCES, DivergenceError, build_divergence, canonical_bytes, digest, reconstruct
from .evidence import _git_head
from .g1c2 import EXPECTED_C1_REPORT_SHA256, _parse_at
from .g1c3 import _ctx

EXPECTED_MANIFEST_SHA256 = "c83eca8be81312a815dca09875f029ff6012f884eefbb86d48bdfd90d668e659"
REPORT_NAME = "G1-D2_observed_fragmentation.yaml"
LEDGER_NAME = "G1-D2_source_divergence.jsonl"


def _pinned_yaml(path: Path, expected: str) -> dict:
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected:
        raise DivergenceError(f"unverified metadata authority: {path.name}")
    return yaml.safe_load(body)


def build_report(tape_dir: Path, *, at: str, c1_report_path: Path | None = None,
                 expected_c1_sha256: str = EXPECTED_C1_REPORT_SHA256,
                 expected_manifest_sha256: str = EXPECTED_MANIFEST_SHA256,
                 code_commit: str | None = None, **authority) -> tuple[dict, bytes]:
    """Bind tape, reference basis and eligible-input census before computing.

    The canonical C4 manifest and C1 census are independently hash-pinned;
    C3 authenticates C2, G1-B rows/links/report, and physical tape digests.
    Provider bytes are never opened. Full byte-level input verification is
    the separately reported C4 operation, not a claim made by this report.
    """
    manifest = _pinned_yaml(Path(tape_dir) / "TAPE_MANIFEST.yaml", expected_manifest_sha256)
    if _parse_at(at) < _parse_at(manifest["input"]["capture_window_utc"]["evidence_tail_end"]):
        raise DivergenceError("full-window report requires known_at at/after the evidence tail; "
                              "use the divergence core for historical reconstructions")
    census_report = _pinned_yaml(c1_report_path or repo_file(
        "evidence", "g1-c1", "G1-C1_tape_materialization.yaml"), expected_c1_sha256)
    ctx = _ctx(Path(tape_dir), authority.pop("c2_report_path", None),
               authority.pop("g1b_rows_path", None), authority.pop("g1b_report_path", None),
               authority.pop("g1b_links_path", None), **authority)
    if (manifest["outputs"]["dataset_semantic_digest"] != ctx.tape["dataset_semantic_digest"]
            or manifest["references"]["basis_id"] != ctx.g1b["reference_basis_id"]):
        raise DivergenceError("manifest/tape/reference authority mismatch")
    for row in ctx.tape["publications"]:
        disp = ctx.g1b["dispositions"].get(row["isin"])
        if not disp or (row["universe_disposition"], row["universe_disposition_reason"], row["issuer_lei"]) != (
                disp["branch"], disp["reason"], disp["lei"]):
            raise DivergenceError("publication differs from regulatory disposition authority")
    observation = reconstruct(ctx.tape["publications"], at)
    divergence = build_divergence(observation)
    observed = frozenset(r["isin"] for r in observation.publications)
    metrics = compute_cross_source(observation.records, observed)
    # G0's fixed-sample observation rate does not belong in G1. The only
    # denominator here is the observed INCLUDE instrument set at stated T.
    metrics.pop("sample_observation_rate")
    metrics["scope"] = {"basis": "OBSERVED_INCLUDE_INSTRUMENT_SET",
                        "observed_include_isins": sorted(observed), "count": len(observed)}
    metrics["trades"] = {
        "economic_candidate_groups": divergence["candidate_groups"],
        "source_exclusive_chains": dict(Counter(r["source"] for r in divergence["rows"]
                                                if not r["relation_flags"]))}
    for source in SOURCES:
        slot = metrics["notional_by_source"][source]
        # Count cancellation states even when notional is unavailable.
        slot["cancelled_heads"] = sum(r["source"] == source and r["head_state"] == "CANCELLED"
                                      for r in divergence["rows"])
    ledger = b"".join(canonical_bytes(r) for r in divergence["rows"])
    branches = Counter(d["branch"] for d in ctx.g1b["dispositions"].values())
    reasons = Counter(d["reason"] for d in ctx.g1b["dispositions"].values()
                      if d["branch"] in {"QUARANTINE", "CONFLICT"})
    report = {
        "issue_id": "G1-D2", "report": "OBSERVED_FRAGMENTATION_MEASURED",
        "authority": "docs/gates/G1.md sections 4.2/5; g1-freeze-v0",
        "known_at": observation.at, "reference_basis": "BUILD_SNAPSHOT",
        "generator": {"module": "metrics.g1d", "code_commit": code_commit or _git_head()},
        "input": {**manifest["input"], "census": census_report["diagnostics"]["input_census"],
                  "unparseable_capture_versions": census_report["diagnostics"]["unparseable_capture_versions"]},
        "dataset_semantic_digest": ctx.tape["dataset_semantic_digest"],
        "reference_basis_id": ctx.g1b["reference_basis_id"],
        "reference_set_digests": ctx.g1b["set_digests"],
        "authority_hashes": {"tape_manifest": expected_manifest_sha256,
                             "c1_census_report": expected_c1_sha256,
                             "g1b_rows": ctx.g1b["rows_digest"],
                             "g1b_links": ctx.g1b["links_sha256"]},
        "regulatory_dispositions": {"basis": "ALL_OBSERVED_IDENTIFIERS_BUILD_SNAPSHOT",
                                    "branch_counts": dict(sorted(branches.items())),
                                    "quarantine_conflict_reasons": dict(sorted(reasons.items()))},
        "metrics": metrics,
        "source_divergence": {"file": LEDGER_NAME, "rows": len(divergence["rows"]),
                              "sha256": hashlib.sha256(ledger).hexdigest(),
                              "semantic_digest": divergence["semantic_digest"],
                              "membership_basis": divergence["membership_basis"],
                              "candidate_basis": divergence["candidate_basis"],
                              "class_counts": dict(sorted(Counter(label for row in divergence["rows"]
                                  for comparison in row["comparisons"] for label in comparison["classes"]).items()))},
        "interpretation_limits": {"labels": ["NO_COVERAGE_CLAIM", "NO_MARKET_SHARE_INFERENCE",
                                             "NO_ABSENCE_OF_TRADING_INFERENCE"],
                                  **INTERPRETATION_LIMITS},
        "full_provenance": "NOT_RUN; use omt-verify with the sealed bundle and reference files",
    }
    # Scan only affirmative output, excluding the mandatory negated limits.
    scanned_metrics = {k: v for k, v in metrics.items() if k != "interpretation_limits"}
    if scan_forbidden_claims(json.dumps(scanned_metrics)) or b"MISSING_FROM_" in ledger:
        raise DivergenceError("forbidden claim in emitted observations")
    report["semantic_digest"] = digest(report)
    return report, ledger


def write_report(out: Path, report: dict, ledger: bytes) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / REPORT_NAME
    path.write_bytes(yaml.safe_dump(report, sort_keys=False, allow_unicode=True).encode("utf-8"))
    (out / LEDGER_NAME).write_bytes(ledger)
    names = (REPORT_NAME, LEDGER_NAME)
    (out / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}\n" for name in names), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tape-dir", type=Path, default=Path("data/g1/tape"))
    ap.add_argument("--at", required=True, help="offset-aware knowledge time")
    ap.add_argument("--out", type=Path, required=True, help="metadata-only report directory")
    ap.add_argument("--authority-dir", type=Path,
                    help="portable root containing the curated evidence/ layout")
    args = ap.parse_args(argv)
    kw = {}
    if args.authority_dir:
        for key, folder, name in (
                ("c1_report_path", "g1-c1", "G1-C1_tape_materialization.yaml"),
                ("c2_report_path", "g1-c2", "G1-C2_asof_reconstruction.yaml"),
                ("g1b_rows_path", "g1-b1", "G1-B1_disposition_rows.jsonl"),
                ("g1b_report_path", "g1-b1", "G1-B1_observed_universe_disposition.yaml"),
                ("g1b_links_path", "g1-b1", "G1-B1_reference_links.jsonl")):
            kw[key] = args.authority_dir / "evidence" / folder / name
    report, ledger = build_report(args.tape_dir, at=args.at, **kw)
    print(write_report(args.out, report, ledger))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
