"""Offline orchestration + fail-closed + evidence-sanitization tests."""

from pathlib import Path

import yaml

from collectors import sources
from collectors.harness import run_source, write_evidence
from collectors.storage import RawStore

FAKE_ID = "fake_src"


class _FakeAdapter:
    SOURCE_ID = FAKE_ID
    discovery_error: Exception | None = None
    fetch_error: Exception | None = None
    objects = None  # type: ignore

    def __init__(self):
        type(self).errors_reset()

    @classmethod
    def errors_reset(cls):
        cls.discovery_error = None
        cls.fetch_error = None
        cls.objects = None

    @classmethod
    def discover(cls, conf, http_get):
        if cls.discovery_error:
            raise cls.discovery_error
        if cls.objects is None:
            from collectors.sources.base import DiscoveredObject
            cls.objects = [DiscoveredObject(source_id=FAKE_ID, object_key="obj.json",
                                            url="https://example.test/obj.json",
                                            publication_timestamp="2026-09-14T10:00:00Z")]
        return cls.objects

    @classmethod
    def fetch(cls, conf, http_get, obj):
        if cls.fetch_error:
            raise cls.fetch_error
        return b'{"fake": "payload"}'


sources.ADAPTERS[FAKE_ID] = _FakeAdapter


def test_harness_success_with_duplicate_idempotence(tmp_path: Path):
    conf = {"source_id": FAKE_ID, "entrypoint": {"base_url": "https://example.test"}}
    store = RawStore(tmp_path)
    r1 = run_source(FAKE_ID, conf, store, lambda u: b"", now="2026-09-14T12:00:00+00:00")
    assert r1.succeeded and r1.status == "SUCCEEDED"
    assert r1.object_count == 1
    assert r1.objects[0].status == "CREATED"
    assert r1.objects[0].raw_sha256

    r2 = run_source(FAKE_ID, conf, store, lambda u: b"", now="2026-09-14T12:30:00+00:00")
    assert r2.succeeded
    assert r2.objects[0].status == "ALREADY_PRESENT"
    # object bytes unchanged after second run
    assert (tmp_path / FAKE_ID / "2026-09-14" / "obj.json").read_bytes() == b'{"fake": "payload"}'


def test_harness_discover_fail_closed(tmp_path: Path):
    from collectors.sources.base import SourceDiscoveryError
    _FakeAdapter.errors_reset()
    _FakeAdapter.discovery_error = SourceDiscoveryError("cannot resolve")
    result = run_source(FAKE_ID, {"source_id": FAKE_ID}, RawStore(tmp_path), lambda u: b"",
                        now="2026-09-14T12:00:00+00:00")
    assert result.status == "FAILED"
    assert not result.objects
    assert any("discover" in e.lower() for e in result.errors)


def test_harness_zero_discovered_not_run(tmp_path: Path):
    _FakeAdapter.errors_reset()
    _FakeAdapter.objects = []
    result = run_source(FAKE_ID, {"source_id": FAKE_ID}, RawStore(tmp_path), lambda u: b"",
                        now="2026-09-14T12:00:00+00:00")
    assert result.status == "NOT_RUN"
    assert result.object_count == 0


def test_harness_fetch_fail_closed(tmp_path: Path):
    _FakeAdapter.errors_reset()
    _FakeAdapter.fetch_error = RuntimeError("http 503")
    result = run_source(FAKE_ID, {"source_id": FAKE_ID}, RawStore(tmp_path), lambda u: b"",
                        now="2026-09-14T12:00:00+00:00")
    assert result.status == "FAILED" and not result.objects
    assert any("fetch" in e.lower() for e in result.errors)


def test_evidence_manifest_excludes_payload_bytes(tmp_path: Path):
    _FakeAdapter.errors_reset()
    result = run_source(FAKE_ID, {"source_id": FAKE_ID}, RawStore(tmp_path), lambda u: b"",
                        now="2026-09-14T12:00:00+00:00")
    manifest_path = write_evidence(tmp_path, FAKE_ID, result, run_id="r1")
    text = manifest_path.read_text(encoding="utf-8")
    # Metadata present:
    loaded = yaml.safe_load(text)
    assert loaded["results"]["object_count"] == 1
    assert loaded["results"]["objects"][0]["raw_sha256"]
    # Payload bytes / provider data NEVER written to evidence:
    assert '{"fake": "payload"}' not in text
    assert "payload" not in text
