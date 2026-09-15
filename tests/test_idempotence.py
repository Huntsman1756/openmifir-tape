"""Offline tests for G0-A2 ingest idempotence (SECOND_RUN_IDEMPOTENT)."""

from pathlib import Path

from collectors.a2 import run_idempotence, state_digest
from collectors.storage import RawStore


def _seed(store: RawStore, root: Path):
    store.store(source_id="s", collection_date="2026-09-14", object_key="a.json", data=b'{"a":1}',
                publication_timestamp="t1", provenance_url="u1", observation_utc="2026-09-14T10:00:00+00:00")
    store.store(source_id="s", collection_date="2026-09-14", object_key="a.json", data=b'{"a":2}',
                publication_timestamp="t2", provenance_url="u2", observation_utc="2026-09-14T11:00:00+00:00")
    store.store(source_id="s", collection_date="2026-09-14", object_key="b.csv", data=b"x,y\n1,2\n",
                publication_timestamp="t3", provenance_url="u3", observation_utc="2026-09-14T12:00:00+00:00")


def test_idempotence_replay_is_zero_state_change(tmp_path: Path):
    store = RawStore(tmp_path)
    _seed(store, tmp_path)
    before = state_digest(tmp_path)
    report = run_idempotence(tmp_path)
    after = state_digest(tmp_path)
    assert report.before_digest == before
    assert report.after_digest == after
    assert report.before_digest == report.after_digest
    assert report.created_raw_objects == 0
    assert report.modified_raw_objects == 0
    assert report.deleted_raw_objects == 0
    assert report.new_metadata_records == 0
    assert report.changed_metadata == 0
    assert report.unexpected_state_diff is False
    assert report.replay_statuses.get("ALREADY_PRESENT") == 3
    assert report.replay_statuses.get("CREATED") is None


def test_idempotence_detects_tampered_raw_object(tmp_path: Path):
    store = RawStore(tmp_path)
    _seed(store, tmp_path)
    # tamper one stored raw file in place
    target = next(p for p in tmp_path.rglob("*") if p.is_file() and not p.name.endswith(".meta.yaml"))
    target.write_bytes(b"tampered-content")
    report = run_idempotence(tmp_path)
    assert report.unexpected_state_diff is True
    assert (report.created_raw_objects + report.modified_raw_objects + report.deleted_raw_objects) > 0


def test_state_digest_is_deterministic_and_content_sensitive(tmp_path: Path):
    store = RawStore(tmp_path)
    _seed(store, tmp_path)
    d1 = state_digest(tmp_path)
    d2 = state_digest(tmp_path)
    assert d1 == d2
    # adding content changes the digest
    store.store(source_id="s", collection_date="2026-09-14", object_key="c.csv", data=b"new",
                publication_timestamp="t4", provenance_url="u4", observation_utc="o4")
    assert state_digest(tmp_path) != d1
