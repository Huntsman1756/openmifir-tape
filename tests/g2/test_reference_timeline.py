import hashlib
import json
from copy import deepcopy

import pytest

from metrics.g2_authority import AuthorityError, digest
from metrics.g2_reference_observation import FAMILIES
from metrics.g2_reference_timeline import empty_timeline, plan_reference_admission, prepare_bundle, select_reference


@pytest.fixture
def reference_bundle(tmp_path):
    def make(episode="episode-one", known="2026-10-01T12:00:00Z", failed=None):
        root = tmp_path / episode
        declarations = {}
        for family, kind in FAMILIES.items():
            folder = root / family
            folder.mkdir(parents=True, exist_ok=True)
            content = b"synthetic reference bytes"
            (folder / "reference.bin").write_bytes(content)
            entry = {"name": "reference.bin", "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            requested = {"mode": "IDENTIFIERS", "identifier_kind": kind, "identifiers": ["SYNTHETIC-ID"]}
            receipt = {"schema": "g2-acquisition-outcome-v1", "family": family, "acquisition_id": episode + family,
                       "scope": requested, "source_provenance": "https://synthetic.invalid/reference",
                       "outcome": "ACQUISITION_FAILED" if failed == family else "AVAILABLE",
                       "reason": "synthetic failure" if failed == family else None}
            blob = json.dumps(receipt).encode()
            (folder / "outcome.json").write_bytes(blob)
            doc = {"schema": "g2-reference-observation-v1", **{k: receipt[k] for k in (
                "family", "acquisition_id", "scope", "source_provenance", "outcome")}, "source_revision": None,
                "files": [] if failed == family else [{"file": entry, "observed_at": known}],
                "receipt": {"file": {"name": "outcome.json", "sha256": hashlib.sha256(blob).hexdigest(),
                                     "bytes": len(blob)}, "observed_at": known}}
            blob = json.dumps(doc).encode()
            (folder / "observation.json").write_bytes(blob)
            declarations[family] = [{"name": family + "/observation.json",
                                     "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}]
        manifest = {"schema": "g2-reference-bundle-v1", "episode_id": episode, "observations": declarations}
        return root, manifest, prepare_bundle(root, manifest)
    return make


def test_exact_replay_and_no_future_or_family_mixing(reference_bundle):
    _, _, first = reference_bundle()
    state = plan_reference_admission(empty_timeline(), first["bundle"], [])
    assert plan_reference_admission(state, first["bundle"], []) == state
    assert select_reference(state, "2026-10-01T11:59:59Z") is None
    _, _, second = reference_bundle("episode-two", "2026-10-02T12:00:00Z", failed="FITRS")
    state = plan_reference_admission(state, second["bundle"], [])
    selected = select_reference(state, "2026-10-02T12:00:00Z")
    assert selected == second["bundle"]
    assert selected["family_sets"]["FITRS"]["entries"] == []
    assert select_reference(state, "2026-10-01T12:00:00Z") == first["bundle"]


def test_fresh_episode_same_bytes_is_new_bundle(reference_bundle):
    _, _, first = reference_bundle()
    _, _, second = reference_bundle("episode-two", "2026-10-02T12:00:00Z")
    assert first["bundle"]["family_sets"] == second["bundle"]["family_sets"]
    assert first["bundle"]["bundle_id"] != second["bundle"]["bundle_id"]


def test_ambiguous_and_older_bundles_reject_without_mutation(reference_bundle):
    _, _, first = reference_bundle()
    state = plan_reference_admission(empty_timeline(), first["bundle"], [])
    before = deepcopy(state)
    for episode, known, message in [("same-time", "2026-10-01T12:00:00Z", "ambiguous"),
                                     ("old-time", "2026-09-30T12:00:00Z", "correction")]:
        _, _, candidate = reference_bundle(episode, known)
        with pytest.raises(AuthorityError, match=message):
            plan_reference_admission(state, candidate["bundle"], [])
    assert state == before


def test_new_bundle_in_sealed_window_rejects(reference_bundle):
    _, _, candidate = reference_bundle()
    windows = [{"kind": "G2_PROSPECTIVE", "start": "2026-10-01T00:00:00Z", "end": "2026-10-02T00:00:00Z"}]
    with pytest.raises(AuthorityError, match="sealed interval"):
        plan_reference_admission(empty_timeline(), candidate["bundle"], windows)


def test_metadata_packaging_order_does_not_change_identity(reference_bundle):
    root, manifest, first = reference_bundle()
    manifest["observations"] = dict(reversed(list(manifest["observations"].items())))
    assert prepare_bundle(root, manifest)["bundle"] == first["bundle"]
    manifest["observations"].pop("GLEIF")
    with pytest.raises(AuthorityError):
        prepare_bundle(root, manifest)


def test_existing_episode_cannot_advance_its_availability(reference_bundle):
    _, _, first = reference_bundle()
    state = plan_reference_admission(empty_timeline(), first["bundle"], [])
    _, _, later = reference_bundle(known="2026-10-02T12:00:00Z")
    with pytest.raises(AuthorityError, match="rewritten"):
        plan_reference_admission(state, later["bundle"], [])


def test_reordered_timeline_rejects_even_if_rehashed(reference_bundle):
    _, _, first = reference_bundle()
    _, _, second = reference_bundle("episode-two", "2026-10-02T12:00:00Z")
    state = plan_reference_admission(empty_timeline(), first["bundle"], [])
    state = plan_reference_admission(state, second["bundle"], [])
    state["bundles"].reverse()
    state["semantic_digest"] = digest({k: v for k, v in state.items() if k != "semantic_digest"})
    with pytest.raises(AuthorityError, match="chronology"):
        select_reference(state, "2026-10-03T12:00:00Z")
