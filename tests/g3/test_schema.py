import copy
import hashlib

import pytest

from metrics.g2_authority import AuthorityError, digest
from metrics.g3_schema import (
    census,
    empty_timeline,
    identity,
    plan_snapshot_admission,
    schema_snapshot,
    select_snapshot,
)


def test_valid_snapshot_pins_definitions_and_direction(snapshot_factory):
    root, doc = snapshot_factory(names=("msg-b.json", "msg-a.json"),
                                 code_sets={"msg-a.json": ["MiFIR", "ISO-4217"]})
    snapshot = schema_snapshot(root, doc)
    assert snapshot["direction"] == "OUTPUT"
    assert [e["name"] for e in snapshot["definitions"]] == ["msg-a.json", "msg-b.json"]
    assert len(snapshot["snapshot_id"]) == 64
    assert snapshot["schema_known_at"] == "2026-10-01T12:00:00.000000Z"
    assert census(snapshot) == {
        "direction": "OUTPUT", "definitions": 2, "final": 2, "draft": 0, "draft_names": [],
        "code_sets": ["ExternalCodeSet", "ISO-4217", "MiFIR"],
        "definition_set_sha256": snapshot["definition_set_sha256"],
    }


def test_hybrid_direction_is_rejected(snapshot_factory):
    root, doc = snapshot_factory(names=("msg-a.json", "msg-b.json"))
    doc["definitions"][0]["direction"] = "INPUT"
    with pytest.raises(AuthorityError, match="hybrid"):
        schema_snapshot(root, doc)


def test_direction_must_be_exactly_declared(snapshot_factory):
    root, doc = snapshot_factory()
    doc["direction"] = "BOTH"
    with pytest.raises(AuthorityError, match="exactly one message direction"):
        schema_snapshot(root, doc)


def test_definition_bytes_are_authenticated(snapshot_factory):
    root, doc = snapshot_factory()
    (root / "msg-a.json").write_bytes(b"tampered")
    with pytest.raises(AuthorityError, match="integrity failure"):
        schema_snapshot(root, doc)


def test_duplicate_or_unsorted_definitions_reject(snapshot_factory):
    root, doc = snapshot_factory(names=("msg-a.json", "msg-b.json"))
    doc["definitions"].reverse()
    with pytest.raises(AuthorityError, match="sorted by name"):
        schema_snapshot(root, doc)
    root2, doc2 = snapshot_factory(episode="s2", names=("msg-a.json",))
    doc2["definitions"].append(copy.deepcopy(doc2["definitions"][0]))
    with pytest.raises(AuthorityError, match="unique"):
        schema_snapshot(root2, doc2)


def test_duplicate_code_sets_reject(snapshot_factory):
    root, doc = snapshot_factory(code_sets={"msg-a.json": ["MiFIR", "MiFIR"]})
    with pytest.raises(AuthorityError, match="code sets"):
        schema_snapshot(root, doc)


def test_draft_definitions_are_labelled(snapshot_factory):
    root, doc = snapshot_factory(names=("final.json", "draft.json"),
                                 statuses={"draft.json": "DRAFT"})
    snapshot = schema_snapshot(root, doc)
    assert census(snapshot)["draft_names"] == ["draft.json"]
    assert census(snapshot)["final"] == 1


def test_exact_replay_is_noop_and_selection_is_point_in_time(snapshot_factory):
    root, doc = snapshot_factory()
    snapshot = schema_snapshot(root, doc)
    state = plan_snapshot_admission(empty_timeline(), snapshot)
    assert plan_snapshot_admission(state, snapshot) == state
    assert select_snapshot(state, "2026-10-01T11:59:59Z") is None
    assert select_snapshot(state, "2026-10-01T12:00:00Z") == snapshot


def test_ambiguous_and_older_snapshots_reject_without_mutation(snapshot_factory):
    root, doc = snapshot_factory()
    state = plan_snapshot_admission(empty_timeline(), schema_snapshot(root, doc))
    before = copy.deepcopy(state)
    for episode, observed, message in [("same-time", "2026-10-01T12:00:00Z", "ambiguous"),
                                       ("old-time", "2026-09-30T12:00:00Z", "correction")]:
        other_root, other_doc = snapshot_factory(episode=episode, observed_at=observed)
        with pytest.raises(AuthorityError, match=message):
            plan_snapshot_admission(state, schema_snapshot(other_root, other_doc))
    assert state == before


def test_changed_bytes_change_the_set_digest(snapshot_factory):
    root_a, doc_a = snapshot_factory(episode="a")
    root_b, doc_b = snapshot_factory(episode="b")
    (root_b / "msg-a.json").write_bytes(b"different schema bytes")
    payload = (root_b / "msg-a.json").read_bytes()
    entry = doc_b["definitions"][0]
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    entry["bytes"] = len(payload)
    first = schema_snapshot(root_a, doc_a)
    second = schema_snapshot(root_b, doc_b)
    assert first["definition_set_sha256"] != second["definition_set_sha256"]


def test_tampered_accepted_snapshot_rejects(snapshot_factory):
    root, doc = snapshot_factory()
    snapshot = schema_snapshot(root, doc)
    state = plan_snapshot_admission(empty_timeline(), snapshot)
    state["snapshots"][0]["submission_id"] = "tampered"
    with pytest.raises(AuthorityError, match="integrity"):
        plan_snapshot_admission(state, snapshot)


def test_reordered_timeline_rejects_even_if_rehashed(snapshot_factory):
    first_root, first_doc = snapshot_factory(episode="one")
    second_root, second_doc = snapshot_factory(episode="two", observed_at="2026-10-02T12:00:00Z")
    state = plan_snapshot_admission(empty_timeline(), schema_snapshot(first_root, first_doc))
    state = plan_snapshot_admission(state, schema_snapshot(second_root, second_doc))
    state["snapshots"].reverse()
    state["semantic_digest"] = digest({k: v for k, v in state.items() if k != "semantic_digest"})
    with pytest.raises(AuthorityError, match="chronology"):
        select_snapshot(state, "2026-10-03T12:00:00Z")


def test_identity_rebuilds_are_stable(snapshot_factory):
    root, doc = snapshot_factory(names=("msg-a.json", "msg-b.json"))
    snapshot = schema_snapshot(root, doc)
    assert identity(snapshot) == snapshot
