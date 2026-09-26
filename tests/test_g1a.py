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
    assert e["eligible_capture_versions"] == 1
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
    assert e["eligible_capture_versions"] == 2
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
