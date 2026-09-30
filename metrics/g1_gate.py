"""Metadata evidence evaluation for frozen G1 section 7 (G1-D3).

Recorded evidence is not a replacement for running acquisition, replay or
CI. This evaluator checks committed receipts, hashes and their consistency;
every missing/invalid component remains REVIEW_REQUIRED.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

from .cross_source import scan_forbidden_claims
from .divergence import digest
from .evidence import _git_head

ARTIFACTS = {
    "A": "g1-a1/G1-A1_observed_instrument_set.yaml",
    "B": "g1-b1/G1-B1_observed_universe_disposition.yaml",
    "C1": "g1-c1/G1-C1_tape_materialization.yaml",
    "C2": "g1-c2/G1-C2_asof_reconstruction.yaml",
    "C3": "g1-c3/G1-C3_evidence_capsule.yaml",
    "C4": "g1-c4/G1-C4_manifest_verify.yaml",
    "D": "g1-d2/G1-D2_observed_fragmentation.yaml",
}
EXPECTED_JOBS = {f"test-{platform} ({version})" for platform in ("linux", "windows")
                 for version in ("3.11", "3.12", "3.13")} | {"wheel-smoke-linux", "wheel-smoke-windows"}


def ci_ready(receipts: list[dict]) -> bool:
    return bool(receipts) and all(
        r.get("status") == "completed" and r.get("conclusion") == "success"
        and re.fullmatch(r"[0-9a-f]{40}", r.get("target_sha", "")) is not None
        and {j["name"] for j in r.get("jobs", [])} == EXPECTED_JOBS
        and len(r["jobs"]) == len(EXPECTED_JOBS)
        and all(j.get("conclusion") == "success"
                and j.get("checkout_assertion") == "success"
                and j.get("checkout_sha") == r["target_sha"] for j in r["jobs"])
        for r in receipts)


def _get(doc: dict, *parts):
    for part in parts:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


def evaluate(evidence: Path, *, ci: list[dict], current_tape: dict,
             code_commit: str | None = None) -> dict:
    docs, hashes, errors = {}, {}, []

    def checked(relative: str) -> bytes:
        p = evidence / relative
        sums = p.with_suffix(".sha256.txt")
        if not sums.is_file():
            sums = p.parent / "SHA256SUMS.txt"
        try:
            body = p.read_bytes()
            expected = [line.split()[0] for line in sums.read_text().splitlines()
                        if len(line.split()) == 2 and line.split()[1].lstrip("*") == p.name]
            actual = hashlib.sha256(body).hexdigest()
            if expected != [actual]:
                raise ValueError("checksum mismatch or ambiguous checksum entry")
            hashes[relative] = actual
            return body
        except (OSError, ValueError) as exc:
            errors.append({"artifact": relative, "error": str(exc)})
            return b""

    for key, path in ARTIFACTS.items():
        body = checked(path)
        try:
            doc = yaml.safe_load(body) or {}
            if not isinstance(doc, dict):
                raise ValueError("metadata report must be a mapping")
            docs[key] = doc
        except (ValueError, yaml.YAMLError) as exc:
            errors.append({"artifact": path, "error": str(exc)})
            docs[key] = {}
    a, b, c1, c2, c3, c4, d = (docs[key] for key in ARTIFACTS)
    observed = _get(a, "observed_instrument_set") or {}
    b_rows = checked("g1-b1/G1-B1_disposition_rows.jsonl")
    rows = [json.loads(line) for line in b_rows.splitlines()] if b_rows else []
    isins = {row["isin"] for row in rows}
    observed_isins = {row["isin"] for row in observed.get("instruments", [])}
    branches = _get(b, "dispositions", "branch_counts") or {}
    outputs = c4.get("outputs") or {}
    reproducible = c4.get("reproducibility") or {}
    materializations = reproducible.get("materializations") or []
    ledger_bytes = checked("g1-d2/G1-D2_source_divergence.jsonl")
    ledger = [json.loads(line) for line in ledger_bytes.splitlines()] if ledger_bytes else []
    checks = []

    def criterion(number: int, title: str, passed: bool, sources: list[str]):
        checks.append({"criterion": number, "requirement": title,
                       "verdict": "PASS" if passed else "REVIEW_REQUIRED",
                       "evidence": [ARTIFACTS[s] for s in sources]})

    criterion(1, "Exact eligible sealed input and auditable census",
              bool(_get(d, "input", "census"))
              and _get(c4, "input", "a3_bundle_sha256") == _get(a, "evidence_basis", "bundle", "bundle_manifest_sha256")
              and _get(d, "input", "eligible_manifest_digest") == _get(c4, "input", "eligible_manifest_digest")
              and all(m.get("g1a_semantic_digest") == observed.get("semantic_digest")
                      for m in materializations), ["A", "C1", "C4", "D"])
    disposition_ok = (bool(isins) and len(isins) == len(rows) == observed.get("instrument_count")
                      and isins == observed_isins and sum(branches.values()) == len(rows)
                      and all(r["branch"] in {"INCLUDE", "EXCLUDE", "QUARANTINE", "CONFLICT"} for r in rows))
    criterion(2, "Exactly one explicit regulatory disposition per observed identifier", disposition_ok, ["A", "B"])
    criterion(3, "Frozen resolver authority; no overrides or silent identifier drops",
              disposition_ok and _get(d, "reference_basis_id") == b.get("reference_basis_id")
              and _get(d, "regulatory_dispositions", "branch_counts") == branches, ["B", "D"])
    criterion(4, "Equivalent independent tapes and byte-identical capsules",
              len(materializations) >= 2 and reproducible.get("dataset_semantic_digest_equal") is True
              and reproducible.get("capsule_bytes_byte_identical") is True
              and all(m.get("dataset_semantic_digest") == outputs.get("dataset_semantic_digest")
                      for m in materializations), ["C3", "C4"])
    criterion(5, "Every publication has source/capture/hash/observation trace",
              current_tape.get("self_verification") == "PASS"
              and current_tape.get("every_publication_trace_fields_valid") is True
              and current_tape.get("publication_rows") == outputs.get("publications_rows"), ["C1", "C3", "C4"])
    criterion(6, "G0-D authoritative chains and explicit supersession",
              _get(c1, "lifecycle_reconciliation", "ledger_entries") == outputs.get("publications_rows")
              and _get(c2, "replay_fidelity", "equals_materialized_chains") is True, ["C1", "C2"])
    criterion(7, "Deterministic point-in-time reconstruction without future evidence",
              c3.get("no_future_evidence") is True and _get(c2, "replay_fidelity", "equals_materialized_chains") is True
              and all(member["known_at"] <= d.get("known_at", "")
                      for row in ledger for member in row["members"]), ["C2", "C3", "D"])
    criterion(8, "Per-build manifest and full materialization replay",
              reproducible.get("tape_reproducible") is True
              and _get(c4, "full_provenance", "full_provenance") == "PASS"
              and len(materializations) >= 2, ["C4"])
    criterion(9, "Observed fragmentation and metadata source-divergence ledger",
              d.get("report") == "OBSERVED_FRAGMENTATION_MEASURED"
              and len(ledger) == _get(d, "source_divergence", "rows")
              and hashlib.sha256(ledger_bytes).hexdigest() == _get(d, "source_divergence", "sha256"), ["D"])
    affirmative = {k: v for k, v in (d.get("metrics") or {}).items() if k != "interpretation_limits"}
    required_limits = {"NO_COVERAGE_CLAIM", "NO_MARKET_SHARE_INFERENCE", "NO_ABSENCE_OF_TRADING_INFERENCE"}
    criterion(10, "Emission scan and mandatory interpretation limits",
              required_limits <= set(_get(d, "interpretation_limits", "labels") or [])
              and not scan_forbidden_claims(json.dumps(affirmative))
              and b"MISSING_FROM_" not in ledger_bytes, ["D"])
    # A correctly checksummed report must also authenticate its own semantic identity.
    if d and digest({k: v for k, v in d.items() if k != "semantic_digest"}) != d.get("semantic_digest"):
        errors.append({"artifact": ARTIFACTS["D"], "error": "semantic digest mismatch"})
    for check in checks:
        folders = {p.split("/")[0] for p in check["evidence"]}
        related = [e for e in errors if e["artifact"].split("/")[0] in folders]
        if related:
            check["verdict"] = "REVIEW_REQUIRED"
            check["evidence_errors"] = related
    ready = ci_ready(ci)
    passed = ready and not errors and all(c["verdict"] == "PASS" for c in checks)
    return {"issue_id": "G1-D3", "authority": "docs/gates/G1.md section 7; g1-freeze-v0",
            "generator_code_commit": code_commit or _git_head(),
            "verdict": "PASS" if passed else "REVIEW_REQUIRED",
            "criteria": checks, "artifact_sha256": hashes, "errors": errors,
            "ci_ready": ready, "ci": ci, "current_tape_verification": current_tape,
            "limits": {"corpus": "sealed September 2026 observed window",
                       "reference_basis": "BUILD_SNAPSHOT", "public_data_redistribution": "NOT_AUTHORIZED_BY_THIS_GATE",
                       "security_audit": "NOT_CLAIMED", "live_vps_health": "separate operational readback"}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--evidence", type=Path, default=Path("evidence"))
    ap.add_argument("--ci", type=Path, required=True, help="read-back GitHub CI receipts JSON")
    ap.add_argument("--current-tape", type=Path, required=True, help="current self-verification receipt JSON")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    report = evaluate(args.evidence, ci=json.loads(args.ci.read_text(encoding="utf-8-sig")),
                      current_tape=json.loads(args.current_tape.read_text(encoding="utf-8-sig")))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "G1-D3_gate_evaluation.yaml"
    path.write_bytes(yaml.safe_dump(report, sort_keys=False, allow_unicode=True).encode("utf-8"))
    checksum = f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
    (args.out / "SHA256SUMS.txt").write_text(checksum, encoding="utf-8")
    print(report["verdict"])
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
