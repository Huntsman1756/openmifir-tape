"""Offline tests for G1-A1 OBSERVED_INSTRUMENT_SET (metrics/g1a.py).

Synthetic fixtures only — no provider data, no network. Covers: instrument
membership per source, dedup over repeated capture observations,
first/last eligible observation spans, pre/post-window exclusion,
deterministic canonical digest, and fail-closed propagation of the #17
identity binding.
"""

import pytest
from test_metrics import (
    BLB,
    BME,
    WINDOW,
    _blb,
    _bme,
    _fixture,
    _manifest,
    _obj,
    _store,
)

from metrics.g1a import build_observed_instrument_set, build_report
from metrics.selection import SelectionError


def test_one_instrument_one_source(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    assert res["instrument_count"] == 1
    e = res["instruments"][0]
    assert e["isin"] == "ISIN-A" and e["sources"] == [BME]
    assert res["by_source_instrument_counts"] == {BME: 1}
    assert res["instruments_in_multiple_sources"] == 0


def test_same_instrument_both_sources(tmp_path):
    raw, runs = _fixture(tmp_path)
    ob = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [ob])
    row = _blb()
    csv_data = "hdr\n" + ",".join(row) + "\n" + ",".join(row.values()) + "\n"
    oc = _store(raw, BLB, "2026-09-18", "f.csv", csv_data)
    _manifest(runs, "20260918T100000Z", BLB, "2026-09-18T10:00:00Z", [oc])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    assert res["instrument_count"] == 1
    e = res["instruments"][0]
    assert e["sources"] == [BLB, BME]   # sorted: blb_apae < bme_apa
    assert res["instruments_in_multiple_sources"] == 1


def test_repeated_capture_observations_do_not_duplicate(tmp_path):
    """Same capture attested by N eligible manifests: one instrument, and
    first/last observation spans the attestations."""
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    for hh in ("10", "11", "12"):
        _manifest(runs, f"20260917T{hh}0000Z", BME,
                  f"2026-09-17T{hh}:00:00Z", [o])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    assert res["instrument_count"] == 1
    e = res["instruments"][0]
    assert e["eligible_capture_identities"] == 1
    assert e["first_observed_at"].startswith("2026-09-17T10:00:00")
    assert e["last_observed_at"].startswith("2026-09-17T12:00:00")


def test_first_last_observation_across_versions(tmp_path):
    raw, runs = _fixture(tmp_path)
    o1 = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    o2 = _store(raw, BME, "2026-09-21", "d.json",
                [_bme(price="98.60", notional_amount="98600")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o1])
    _manifest(runs, "20260921T150000Z", BME, "2026-09-21T15:00:00Z", [o2])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    e = res["instruments"][0]
    assert e["eligible_capture_identities"] == 2
    assert e["first_observed_at"].startswith("2026-09-17")
    assert e["last_observed_at"].startswith("2026-09-21")


def test_pre_post_tail_manifests_do_not_affect_output(tmp_path):
    raw, runs = _fixture(tmp_path)
    in_win = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z",
              [in_win])
    pre = _store(raw, BME, "2026-09-16", "pre.json",
                 [_bme(instrument_identification_code="ISIN-PRE")])
    _manifest(runs, "20260916T050000Z", BME, "2026-09-16T05:00:00Z", [pre])
    post = _store(raw, BME, "2026-09-24", "post.json",
                  [_bme(instrument_identification_code="ISIN-POST")])
    _manifest(runs, "20260924T120000Z", BME, "2026-09-24T12:00:00Z", [post])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    assert {e["isin"] for e in res["instruments"]} == {"ISIN-A"}
    assert res["input_census"]["pre_window_manifests_excluded"] == 1
    assert res["input_census"]["post_tail_manifests_excluded"] == 1


def test_distinct_instruments_stay_distinct(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json",
               [_bme(), _bme(instrument_identification_code="ISIN-B",
                            transaction_identification_code="TX-2")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    assert [e["isin"] for e in res["instruments"]] == ["ISIN-A", "ISIN-B"]


def test_deterministic_ordering_and_digest(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json",
               [_bme(instrument_identification_code="ISIN-Z"),
                _bme(instrument_identification_code="ISIN-A")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    a = build_observed_instrument_set(raw, runs, window=WINDOW)
    b = build_observed_instrument_set(raw, runs, window=WINDOW)
    assert [e["isin"] for e in a["instruments"]] == ["ISIN-A", "ISIN-Z"]
    assert a["semantic_digest"] == b["semantic_digest"]
    assert len(a["semantic_digest"]) == 64


def test_malformed_eligible_evidence_fails_closed(tmp_path):
    raw, runs = _fixture(tmp_path)
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z",
              [_obj(BME, "missing.json", "0" * 64)])
    with pytest.raises(SelectionError):
        build_observed_instrument_set(raw, runs, window=WINDOW)


def test_wrong_key_binding_propagates(tmp_path):
    """#17 binding: a capture existing only under another logical key must
    fail closed inside the G1-A builder too."""
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "k2.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z",
              [_obj(BME, "k1.json", obj["capture_version"])])
    with pytest.raises(SelectionError):
        build_observed_instrument_set(raw, runs, window=WINDOW)


def test_report_provenance_and_metadata(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    rep = build_report(raw, runs, window=WINDOW, date="d",
                       generated_utc="t")
    assert rep["report"] == "OBSERVED_INSTRUMENT_SET"
    assert rep["gate"] == "G1" and rep["issue_id"] == "G1-A1"
    assert rep["frozen_authority"]["freeze_tag"] == "g1-freeze-v0"
    assert rep["observed_instrument_set"]["instrument_count"] == 1
    assert rep["hygiene"]["provider_payloads_in_repo"] is False


# ---- PR #21 findings: capture-identity counting + path-independent artifact

def test_capture_identity_counts_once_per_isin(tmp_path):
    """Finding 1-A: one capture version with two rows for the same ISIN
    counts as ONE eligible capture identity."""
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json",
               [_bme(), _bme(transaction_identification_code="TX-9",
                            publication_date_and_time="2026-09-17T10:01:00.000Z")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    assert res["instrument_count"] == 1
    assert res["instruments"][0]["eligible_capture_identities"] == 1
    assert res["records_total_in_scope_files"] == 2


def test_capture_identity_two_distinct_versions(tmp_path):
    """Finding 1-B: same ISIN across two distinct capture identities → 2."""
    raw, runs = _fixture(tmp_path)
    o1 = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    o2 = _store(raw, BME, "2026-09-18", "d.json",
                [_bme(price="98.60", notional_amount="98600")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o1])
    _manifest(runs, "20260918T100000Z", BME, "2026-09-18T10:00:00Z", [o2])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    assert res["instruments"][0]["eligible_capture_identities"] == 2


def test_capture_identity_multi_row_capture(tmp_path):
    """Finding 1-C: one capture with N rows for ISIN-A + 1 row for ISIN-B —
    both instruments exist, each with exactly one capture identity."""
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json",
               [_bme(),
                _bme(transaction_identification_code="TX-9",
                     publication_date_and_time="2026-09-17T10:01:00.000Z"),
                _bme(instrument_identification_code="ISIN-B",
                     transaction_identification_code="TX-B")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    assert res["instrument_count"] == 2
    for e in res["instruments"]:
        assert e["eligible_capture_identities"] == 1


def test_repeated_attestations_do_not_inflate_identity_count(tmp_path):
    """Finding 1-D: N manifests attesting the SAME tuple → one identity;
    first/last observation still span the attestations."""
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    for hh in ("10", "11", "12"):
        _manifest(runs, f"20260917T{hh}0000Z", BME,
                  f"2026-09-17T{hh}:00:00Z", [o])
    res = build_observed_instrument_set(raw, runs, window=WINDOW)
    e = res["instruments"][0]
    assert e["eligible_capture_identities"] == 1
    assert e["first_observed_at"].startswith("2026-09-17T10")
    assert e["last_observed_at"].startswith("2026-09-17T12")


def _bundle_files(root):
    """Minimal bundle provenance files under a root dir."""
    bundle = root / "bundle"
    (bundle / "runs").mkdir(parents=True)
    (bundle / "raw").mkdir()
    (bundle / "collection-meta.yaml").write_text(
        "bundle_id: test-bundle\ndeployed_head: abc123\n"
        "collection_window_utc: {start: '2026-09-24T00:00:00Z'}\n",
        encoding="utf-8")
    (bundle / "SHA256SUMS.txt").write_text("deadbeef *collection-meta.yaml\n",
                                           encoding="utf-8")
    return bundle


def test_cross_path_reproducibility(tmp_path):
    """Finding 2: identical inputs under different root paths produce an
    identical report — no machine-specific paths in the artifact."""
    import yaml as _yaml

    roots = [tmp_path / "loc_a", tmp_path / "loc_b"]
    reports = []
    for root in roots:
        raw = root / "raw"
        runs = root / "runs"
        runs.mkdir(parents=True)
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z",
                  [o])
        bundle = _bundle_files(root)
        rep = build_report(raw, runs, window=WINDOW, bundle_dir=bundle,
                           date="d", generated_utc="t")
        reports.append(rep)
    assert _yaml.safe_dump(reports[0]) == _yaml.safe_dump(reports[1])
    text = _yaml.safe_dump(reports[0])
    assert str(tmp_path) not in text
    assert "loc_a" not in text and "loc_b" not in text
    basis = reports[0]["evidence_basis"]["bundle"]
    assert basis["bundle_id"] == "test-bundle"
    assert "bundle_dir" not in basis
