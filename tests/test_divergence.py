"""Synthetic point-in-time G1-D candidate and membership counterexamples."""

import json

import pytest
from test_metrics import _bme

from metrics.divergence import DivergenceError, build_divergence, reconstruct
from metrics.g1c2 import G1C2Error
from normalization import normalize_record

T1 = "2026-09-17T10:00:00Z"
T2 = "2026-09-17T12:00:00Z"
T3 = "2026-09-17T14:00:00Z"


def publication(source="bme_apa", known=T1, **fields):
    # Both source adapters' records enter the same replay contract. Altering
    # source here is a synthetic independent publication, not real data.
    rec = normalize_record("bme_apa", _bme(**fields))
    rec.source = source
    return {"source": source, "isin": rec.instrument_identification_code,
            "source_object_id": f"synthetic-{known}", "capture_version": "a" * 64,
            "raw_sha256": "a" * 64, "capture_record_ordinal": 0,
            "known_at": known, "universe_disposition": "INCLUDE",
            "normalized_evidence_json": json.dumps(rec.to_evidence())}


def report(pubs, at=T3):
    return build_divergence(reconstruct(pubs, at))


def test_temporal_membership_and_no_future_cancellation():
    pubs = [publication(), publication("blb_apae", T2),
            publication(known=T3, canc="true", flags="CANC",
                        publication_date_and_time="2026-09-17T13:00:05Z")]
    early = report(pubs, T1)
    assert len(early["rows"]) == 1
    assert early["rows"][0]["membership"] == "OBSERVED_BME_ONLY"
    middle = report(pubs, T2)
    assert len(middle["rows"]) == 2
    assert all(r["membership"] == "OBSERVED_BOTH" for r in middle["rows"])
    assert "CANCELLED" not in json.dumps(middle)
    late = report(pubs)
    assert all("CANCELLATION_DIVERGENCE" in r["comparisons"][0]["classes"] for r in late["rows"])
    assert len(late["rows"]) == 2  # never merge source chains


def test_amendment_state_and_publication_lag():
    pubs = [publication(), publication("blb_apae", T1,
            publication_date_and_time="2026-09-17T10:01:05Z"),
            publication(known=T2, amnd="true", flags="AMND",
                        publication_date_and_time="2026-09-17T11:00:05Z")]
    for row in report(pubs)["rows"]:
        assert set(row["comparisons"][0]["classes"]) == {
            "PUBLICATION_LAG", "STATE_DIVERGENCE", "AMENDMENT_DIVERGENCE"}


def test_instrument_membership_is_independent_of_economic_relation():
    result = report([publication(), publication("blb_apae", price="123.45")])
    assert not result["candidate_groups"]
    assert all(r["membership"] == "OBSERVED_BOTH" and not r["relation_flags"] for r in result["rows"])


def test_reordering_offsets_and_future_content_do_not_change_result():
    a, b = publication(), publication("blb_apae", T2)
    assert report([a, b]) == report([b, a])
    b["known_at"] = "2026-09-17T14:00:00+02:00"
    assert report([a, b]) == report([a, publication("blb_apae", T2)])
    future = publication(known="2026-09-18T12:00:00Z")
    future["normalized_evidence_json"] = "broken"
    assert report([a, future]) == report([a])


def test_fail_closed_on_identity_missing_time_and_duplicate():
    bad = publication()
    bad["isin"] = "OTHER"
    with pytest.raises(DivergenceError, match="identity"):
        report([bad])
    bad = publication()
    bad["known_at"] = None
    with pytest.raises(G1C2Error, match="missing"):
        report([bad])
    with pytest.raises(DivergenceError, match="duplicate"):
        report([publication(), publication()])


def test_non_include_and_missing_economics_do_not_create_candidates():
    excluded = publication("blb_apae")
    excluded["universe_disposition"] = "CONFLICT"
    assert len(report([publication(), excluded])["rows"]) == 1
    assert not report([publication(price=""), publication("blb_apae", price="")])["candidate_groups"]


def test_metadata_does_not_emit_economic_values_or_replay_substrate():
    text = json.dumps(report([publication(price="123.45")]))
    assert "123.45" not in text
    assert "normalized_evidence_json" not in text
    assert "BUILD_SNAPSHOT" in text
