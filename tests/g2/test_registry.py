from copy import deepcopy

import pytest

from metrics.g2_authority import AuthorityError
from metrics.g2_registry import G1_BUNDLE_SHA256, G1_END, G1_START, admit, empty_registry, window_record


def origin():
    return window_record("G1_ORIGIN", "a" * 64, G1_BUNDLE_SHA256, G1_START, G1_END, ["bme_apa", "blb_apae"])


def prospective(start="2026-10-01T00:00:00Z", end="2026-10-03T00:00:00Z"):
    return window_record("G2_PROSPECTIVE", "b" * 64, "c" * 64, start, end, ["bme_apa"])


def attestation(observed=G1_END, run="origin-run", cv="d" * 64):
    return {"source_id": "bme_apa", "run_id": run, "source_object_id": "synthetic.json",
            "capture_version": cv, "manifest_sha256": "e" * 64, "observed_at": observed}


def test_exact_replay_and_failed_admission_preserve_state():
    state = admit(empty_registry(), origin(), [attestation()])
    assert admit(state, origin(), [attestation()]) == state
    snapshot = deepcopy(state)
    with pytest.raises(AuthorityError, match="correction"):
        admit(state, origin(), [attestation(run="new-old-run")])
    assert state == snapshot


def test_origin_overlap_retains_links_counts_once():
    state = admit(empty_registry(), origin(), [attestation()])
    window = prospective("2026-09-24T06:00:00Z", "2026-09-25T00:00:00Z")
    state = admit(state, window, [attestation()])
    assert len(state["attestations"]) == 1 and len(state["memberships"]) == 2
    assert next(iter(state["captures"].values()))["observation_count"] == 1
    assert all(s["relation"] == "ORIGIN_OVERLAP" for s in state["inter_window_spans"])


def test_new_observation_updates_diagnostics_without_backdating():
    state = admit(empty_registry(), origin(), [attestation()])
    state = admit(state, prospective(), [attestation("2026-10-01T00:00:00Z", "new-run")])
    c = next(iter(state["captures"].values()))
    assert c["known_at"].startswith("2026-09-24") and c["observation_count"] == 2
    assert len(state["inter_window_spans"]) == 2
    assert all(s["status"] == "NOT_ASSESSED" for s in state["inter_window_spans"])


def test_prospective_overlap_and_reordering_reject():
    state = admit(admit(empty_registry(), origin(), []), prospective(), [])
    with pytest.raises(AuthorityError, match="overlap"):
        admit(state, prospective("2026-10-02T00:00:00Z", "2026-10-04T00:00:00Z"), [])


def test_new_attestation_cannot_rewrite_origin_interval():
    state = admit(empty_registry(), origin(), [])
    with pytest.raises(AuthorityError, match="correction"):
        admit(state, prospective("2026-09-24T06:00:00Z", "2026-09-25T00:00:00Z"), [attestation()])


def test_conflicting_authority_and_future_observation_reject():
    state = admit(empty_registry(), origin(), [attestation()])
    bad = attestation()
    bad["manifest_sha256"] = "f" * 64
    with pytest.raises(AuthorityError, match="conflicting"):
        admit(state, origin(), [bad])
    with pytest.raises(AuthorityError, match="outside"):
        admit(state, prospective(), [attestation("2026-10-03T00:00:00Z", "future")])


def test_registry_digest_tampering_and_originless_import_reject():
    with pytest.raises(AuthorityError, match="first"):
        admit(empty_registry(), prospective(), [])
    state = admit(empty_registry(), origin(), [])
    state["semantic_digest"] = "f" * 64
    with pytest.raises(AuthorityError, match="integrity"):
        admit(state, prospective(), [])


def test_backfill_and_changed_run_authority_reject():
    state = admit(empty_registry(), origin(), [attestation()])
    with pytest.raises(AuthorityError, match="backfill"):
        admit(state, prospective("2026-09-15T00:00:00Z", "2026-09-25T00:00:00Z"), [])
    with pytest.raises(AuthorityError, match="source/run"):
        admit(state, prospective(), [attestation("2026-10-01T00:00:00Z", "origin-run", "f" * 64)])


def test_second_prospective_cannot_overlap_origin():
    state = admit(empty_registry(), origin(), [])
    state = admit(state, prospective("2026-09-20T00:00:00Z", "2026-09-21T00:00:00Z"), [])
    with pytest.raises(AuthorityError, match="first prospective"):
        admit(state, prospective("2026-09-21T00:00:00Z", "2026-09-25T00:00:00Z"), [])
