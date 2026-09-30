"""Authenticated G1-D report integration, offline and synthetic."""

import hashlib
import json

import pytest
import yaml
from test_divergence import T3
from test_g1c3 import _three_stage

from metrics.divergence import DivergenceError
from metrics.g1d import LEDGER_NAME, REPORT_NAME, build_report, write_report


def environment(tmp_path):
    tape, _pubs, _chains, kw = _three_stage(tmp_path)
    manifest = {"input": {"capture_window_utc": {"evidence_tail_end": T3},
                          "eligible_manifest_digest": "1" * 64, "eligible_manifests": 3},
                "outputs": {"dataset_semantic_digest": kw["expected_dataset_digest"]},
                "references": {"basis_id": kw["expected_g1b_basis_id"]}}
    m = tape / "TAPE_MANIFEST.yaml"
    m.write_bytes(yaml.safe_dump(manifest).encode())
    c1 = tmp_path / "census.yaml"
    c1.write_bytes(yaml.safe_dump({"diagnostics": {"input_census": {
        "manifests_eligible": 3, "unique_capture_versions_eligible": 3,
        "pre_window_manifests_excluded": 0, "post_tail_manifests_excluded": 0},
        "unparseable_capture_versions": []}}).encode())
    kw.update(c1_report_path=c1,
              expected_c1_sha256=hashlib.sha256(c1.read_bytes()).hexdigest(),
              expected_manifest_sha256=hashlib.sha256(m.read_bytes()).hexdigest(),
              code_commit="f" * 40)
    return tape, kw


def test_report_reproducible_counts_cancellations_and_metadata(tmp_path):
    tape, kw = environment(tmp_path)
    report, ledger = build_report(tape, at=T3, **kw)
    assert build_report(tape, at=T3, **kw) == (report, ledger)
    assert report["metrics"]["scope"]["count"] == 1
    assert "sample_observation_rate" not in report["metrics"]
    assert report["metrics"]["notional_by_source"]["bme_apa"]["cancelled_heads"] == 1
    assert report["source_divergence"]["rows"] == 1
    assert report["full_provenance"].startswith("NOT_RUN")
    assert json.loads(ledger)["membership"] == "OBSERVED_BME_ONLY"
    assert b'"price"' not in ledger and b"normalized_evidence_json" not in ledger
    output = tmp_path / "metadata"
    write_report(output, report, ledger)
    assert {p.name for p in output.iterdir()} == {REPORT_NAME, LEDGER_NAME, "SHA256SUMS.txt"}


@pytest.mark.parametrize("which", ["manifest", "census"])
def test_unverified_metadata_cannot_supply_census_or_inputs(tmp_path, which):
    tape, kw = environment(tmp_path)
    p = tape / "TAPE_MANIFEST.yaml" if which == "manifest" else kw["c1_report_path"]
    p.write_bytes(p.read_bytes() + b"# mutated\n")
    with pytest.raises(DivergenceError, match="unverified"):
        build_report(tape, at=T3, **kw)


def test_historical_core_is_separate_from_full_window_census(tmp_path):
    tape, kw = environment(tmp_path)
    with pytest.raises(DivergenceError, match="evidence tail"):
        build_report(tape, at="2026-09-17T10:00:00Z", **kw)
