"""Offline tests for the G0-A3 scheduled capture runner (collectors/a3.py).

No network: adapters are fakes and the HTTP getter is a stub, per the repo's
offline-CI rule. Window/verification semantics tested against synthetic
publication timestamps.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from collectors import sources
from collectors.a3 import (
    append_journal,
    parse_publication_timestamp,
    run_scheduled,
    select_window,
    write_run_evidence,
)
from collectors.sources.base import DiscoveredObject, SourceFetchError
from collectors.storage import RawStore

FAKE3 = "fake_a3"
NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class _FakeA3Adapter:
    SOURCE_ID = FAKE3
    objects: list[DiscoveredObject] = []
    hist_objects: list[DiscoveredObject] = []
    payloads: dict[str, bytes] = {}
    fail_fetch: dict[str, int] = {}   # object_key -> remaining failures
    fetch_calls: list[str] = []

    @classmethod
    def reset(cls):
        cls.objects = []
        cls.hist_objects = []
        cls.payloads = {}
        cls.fail_fetch = {}
        cls.fetch_calls = []

    @classmethod
    def discover(cls, conf, http_get):
        entry = conf.get("entrypoint") or {}
        page = entry.get("public_data_page", "")
        if page == entry.get("historical_page") and entry.get("historical_page"):
            return list(cls.hist_objects)
        return list(cls.objects)

    @classmethod
    def fetch(cls, conf, http_get, obj):
        cls.fetch_calls.append(obj.object_key)
        if cls.fail_fetch.get(obj.object_key, 0) > 0:
            cls.fail_fetch[obj.object_key] -= 1
            raise SourceFetchError("transient 503")
        return cls.payloads.get(obj.object_key, b"data:" + obj.object_key.encode())


sources.ADAPTERS[FAKE3] = _FakeA3Adapter


def _conf(**over):
    conf = {
        "source_id": FAKE3,
        "entrypoint": {"public_data_page": "https://example.test/"},
        "retrieval_interval": {"rescan": {"daily_window_hours": 24, "rolling_days": 7}},
        "retry_policy": {"max_attempts": 3, "backoff_seconds": [10, 60, 300]},
    }
    conf.update(over)
    return conf


def _obj(key: str, pub: str) -> DiscoveredObject:
    return DiscoveredObject(source_id=FAKE3, object_key=key,
                            url=f"https://example.test/{key}", publication_timestamp=pub)


def test_parse_publication_timestamp():
    dt, verified = parse_publication_timestamp("2026-09-14T10:00:00+00:00")
    assert verified and dt == datetime(2026, 9, 14, 10, tzinfo=timezone.utc)
    dt, verified = parse_publication_timestamp("2026-09-14T10:00:00Z")
    assert verified and dt.utcoffset() == timedelta(0)
    # Bloomberg BAPA-POST2 token: wall-clock portion parsed, unverified.
    dt, verified = parse_publication_timestamp("20260914-20:22:55.841-02")
    assert not verified
    assert dt == datetime(2026, 9, 14, 20, 22, 55, 841000, tzinfo=timezone.utc)
    assert parse_publication_timestamp("garbage") == (None, False)
    assert parse_publication_timestamp("") == (None, False)


def test_select_window_boundaries():
    cutoff = NOW - timedelta(hours=24)
    objs = [
        _obj("new", _iso(NOW - timedelta(hours=1))),                 # inside
        _obj("edge", _iso(cutoff)),                                  # boundary: inside
        _obj("old", _iso(cutoff - timedelta(seconds=1))),            # outside
        _obj("nopub", ""),                                           # unparseable: included
        _obj("tok_in", "20260917-20:00:00.000-02"),                  # token inside
        _obj("tok_margin", _iso(cutoff - timedelta(hours=5))
             .replace("-", "").replace(":", "")[:0] or
             (cutoff - timedelta(hours=5)).strftime("%Y%m%d-%H:%M:%S.000-02")),  # within margin
        _obj("tok_out", (cutoff - timedelta(hours=7)).strftime("%Y%m%d-%H:%M:%S.000-02")),
    ]
    selected, skipped, unverified = select_window(objs, cutoff)
    keys = {o.object_key for o in selected}
    assert keys == {"new", "edge", "nopub", "tok_in", "tok_margin"}
    assert skipped == 2
    assert unverified == 4  # nopub + tok_in + tok_margin + tok_out


def test_poll_rescans_24h_and_is_idempotent(tmp_path: Path):
    _FakeA3Adapter.reset()
    _FakeA3Adapter.objects = [
        _obj("today.json", _iso(NOW - timedelta(hours=2))),
        _obj("old.json", _iso(NOW - timedelta(hours=30))),
    ]
    store = RawStore(tmp_path)
    r1 = run_scheduled(FAKE3, _conf(), store, lambda u: b"", mode="poll", now=NOW)
    assert r1.status == "SUCCEEDED"
    assert r1.object_count == 1
    assert r1.objects[0].status == "CREATED"
    assert r1.context["skipped_outside_window"] == 1
    assert set(_FakeA3Adapter.fetch_calls) == {"today.json"}

    # Second poll: same bytes -> ALREADY_PRESENT, zero state change.
    r2 = run_scheduled(FAKE3, _conf(), store, lambda u: b"", mode="poll",
                       now=NOW + timedelta(hours=1))
    assert r2.objects[0].status == "ALREADY_PRESENT"


def test_poll_captures_intraday_mutation_as_new_version(tmp_path: Path):
    _FakeA3Adapter.reset()
    obj = _obj("daily.json", _iso(NOW - timedelta(hours=3)))
    _FakeA3Adapter.objects = [obj]
    store = RawStore(tmp_path)
    _FakeA3Adapter.payloads = {"daily.json": b"v1"}
    r1 = run_scheduled(FAKE3, _conf(), store, lambda u: b"", mode="poll", now=NOW)
    _FakeA3Adapter.payloads = {"daily.json": b"v2-mutated"}
    r2 = run_scheduled(FAKE3, _conf(), store, lambda u: b"", mode="poll",
                       now=NOW + timedelta(hours=1))
    assert r2.objects[0].status == "CREATED"
    assert r2.objects[0].capture_version != r1.objects[0].capture_version


def test_fetch_retry_then_success(tmp_path: Path):
    _FakeA3Adapter.reset()
    _FakeA3Adapter.objects = [_obj("flaky.json", _iso(NOW - timedelta(hours=1)))]
    _FakeA3Adapter.fail_fetch = {"flaky.json": 2}
    sleeps: list[float] = []
    r = run_scheduled(FAKE3, _conf(), RawStore(tmp_path), lambda u: b"",
                      mode="poll", now=NOW, sleep=sleeps.append)
    assert r.status == "SUCCEEDED" and r.object_count == 1
    assert _FakeA3Adapter.fetch_calls.count("flaky.json") == 3
    assert sleeps == [10.0, 60.0]
    assert not r.errors


def test_fetch_exhausts_retries_fails_closed(tmp_path: Path):
    _FakeA3Adapter.reset()
    _FakeA3Adapter.objects = [_obj("dead.json", _iso(NOW - timedelta(hours=1)))]
    _FakeA3Adapter.fail_fetch = {"dead.json": 99}
    r = run_scheduled(FAKE3, _conf(), RawStore(tmp_path), lambda u: b"",
                      mode="poll", now=NOW, sleep=lambda s: None)
    assert r.status == "FAILED"
    assert _FakeA3Adapter.fetch_calls.count("dead.json") == 3
    assert any("exhausted" in e for e in r.errors)


def test_rolling_mode_uses_7d_window_and_historical_page(tmp_path: Path):
    _FakeA3Adapter.reset()
    _FakeA3Adapter.objects = [_obj("recent.json", _iso(NOW - timedelta(hours=5)))]
    _FakeA3Adapter.hist_objects = [
        _obj("recent.json", _iso(NOW - timedelta(hours=5))),          # deduped
        _obj("d5.json", _iso(NOW - timedelta(days=5))),               # inside 7d
        _obj("d9.json", _iso(NOW - timedelta(days=9))),               # outside 7d
    ]
    conf = _conf(entrypoint={"public_data_page": "https://example.test/",
                             "historical_page": "https://example.test/hist"})
    r = run_scheduled(FAKE3, conf, RawStore(tmp_path), lambda u: b"",
                      mode="rolling", now=NOW)
    assert r.status == "SUCCEEDED"
    assert {o.source_object_id for o in r.objects} == {"recent.json", "d5.json"}
    assert r.context["skipped_outside_window"] == 1
    assert r.context["historical_page_scanned"] is True


def test_weekend_poll_with_nothing_in_window_succeeds(tmp_path: Path):
    _FakeA3Adapter.reset()
    _FakeA3Adapter.objects = [_obj("friday.json", _iso(NOW - timedelta(hours=40)))]
    r = run_scheduled(FAKE3, _conf(), RawStore(tmp_path), lambda u: b"",
                      mode="poll", now=NOW)
    assert r.status == "SUCCEEDED"
    assert r.object_count == 0
    assert r.context["selected_count"] == 0


def test_evidence_and_journal_are_metadata_only(tmp_path: Path):
    _FakeA3Adapter.reset()
    _FakeA3Adapter.objects = [_obj("o.json", _iso(NOW - timedelta(hours=1)))]
    store = RawStore(tmp_path / "raw")
    r = run_scheduled(FAKE3, _conf(), store, lambda u: b"", mode="poll", now=NOW)
    mp = write_run_evidence(tmp_path / "ev", r, "20260918T120000Z")
    text = mp.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    assert loaded["gate"] == "G0-A3"
    assert loaded["results"]["context"]["mode"] == "poll"
    assert loaded["results"]["context"]["window_hours"] == 24
    assert "data:o.json" not in text

    journal = tmp_path / "journal.jsonl"
    append_journal(journal, r, "20260918T120000Z")
    import json
    rec = json.loads(journal.read_text().strip())
    assert rec["source_id"] == FAKE3 and rec["mode"] == "poll"
    assert rec["status"] == "SUCCEEDED" and rec["created"] == 1
