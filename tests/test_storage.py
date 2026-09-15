"""Deterministic, offline tests for immutable raw storage (G0-A.3 identity model).

Covers the three storage invariants:
- same source_object_id + same bytes     -> ALREADY_PRESENT (zero state change)
- same source_object_id + different bytes -> NEW version CREATED (original preserved)
- same capture_version + different bytes  -> WriteOnceConflict
"""

from pathlib import Path

import pytest

from collectors.storage import CaptureRecord, RawStore, WriteOnceConflict, sanitize_filename, sha256_bytes


def test_sha256_of_exact_bytes():
    assert sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_bytes(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_sanitize_filename_keeps_safe_chars():
    assert sanitize_filename("BAPA-POST2-20260914.csv") == "BAPA-POST2-20260914.csv"
    assert sanitize_filename("a/b\\c:d?e") == "a_b_c_d_e"
    assert sanitize_filename("...") == "unnamed"


def test_store_creates_immutable_version_and_meta(tmp_path: Path):
    store = RawStore(tmp_path)
    data = b'{"trade":"x"}'
    rec = store.store(
        source_id="src",
        collection_date="2026-09-14",
        object_key="daily.json",
        data=data,
        publication_timestamp="2026-09-14T10:00:00Z",
        provenance_url="https://example.test/daily.json",
        observation_utc="2026-09-14T12:00:00+00:00",
    )
    assert rec.status == "CREATED"
    assert rec.raw_sha256 == sha256_bytes(data)
    assert rec.source_object_id == "daily.json"
    assert rec.capture_version == sha256_bytes(data)
    p = tmp_path / "src" / "2026-09-14" / "daily.json" / rec.capture_version
    assert p.read_bytes() == data
    meta = tmp_path / "src" / "2026-09-14" / "daily.json" / f"{rec.capture_version}.meta.yaml"
    assert meta.exists()
    assert "raw_sha256" in meta.read_text(encoding="utf-8")


def test_store_preserves_exact_bytes_no_newline_translation(tmp_path: Path):
    # Regression: on Windows os.open defaults to text mode and would translate
    # LF->CRLF. Raw capture must preserve the exact captured bytes.
    store = RawStore(tmp_path)
    data = b"header\r\nrow1\nrow2\n"
    rec = store.store(source_id="s", collection_date="d", object_key="f.csv", data=data,
                      publication_timestamp="t", provenance_url="u", observation_utc="o")
    assert rec.status == "CREATED"
    assert rec.raw_sha256 == sha256_bytes(data)
    p = tmp_path / "s" / "d" / "f.csv" / rec.capture_version
    assert p.read_bytes() == data  # byte-for-byte identical, no CRLF inflation


def test_same_object_same_bytes_already_present_no_state_change(tmp_path: Path):
    store = RawStore(tmp_path)
    data = b"same-bytes"
    r1 = store.store(source_id="s", collection_date="d", object_key="f.csv", data=data,
                     publication_timestamp="t", provenance_url="u", observation_utc="o1")
    r2 = store.store(source_id="s", collection_date="d", object_key="f.csv", data=data,
                     publication_timestamp="t", provenance_url="u", observation_utc="o2")
    assert r1.status == "CREATED"
    assert r2.status == "ALREADY_PRESENT"
    assert r1.capture_version == r2.capture_version
    p = tmp_path / "s" / "d" / "f.csv" / r1.capture_version
    assert p.read_bytes() == data
    # only one version file exists
    assert len(list((tmp_path / "s" / "d" / "f.csv").glob("*"))) == 2  # file + meta


def test_same_object_different_bytes_creates_new_version_preserves_original(tmp_path: Path):
    store = RawStore(tmp_path)
    a = b"version-A"
    b = b"version-B"
    r1 = store.store(source_id="s", collection_date="d", object_key="f.csv", data=a,
                     publication_timestamp="t", provenance_url="u", observation_utc="o1")
    r2 = store.store(source_id="s", collection_date="d", object_key="f.csv", data=b,
                     publication_timestamp="t", provenance_url="u", observation_utc="o2")
    assert r1.status == "CREATED"
    assert r2.status == "CREATED"
    assert r1.capture_version != r2.capture_version
    assert (tmp_path / "s" / "d" / "f.csv" / r1.capture_version).read_bytes() == a
    assert (tmp_path / "s" / "d" / "f.csv" / r2.capture_version).read_bytes() == b
    # both versions coexist; original preserved
    assert len(list((tmp_path / "s" / "d" / "f.csv").glob("*"))) == 4


def test_same_capture_version_different_bytes_conflicts(tmp_path: Path):
    store = RawStore(tmp_path)
    data = b"x"
    digest = sha256_bytes(data)
    obj_dir = tmp_path / "s" / "d" / "f.csv"
    obj_dir.mkdir(parents=True)
    (obj_dir / digest).write_bytes(b"tampered")
    with pytest.raises(WriteOnceConflict):
        store.store(source_id="s", collection_date="d", object_key="f.csv", data=data,
                    publication_timestamp="t", provenance_url="u", observation_utc="o")
    assert (obj_dir / digest).read_bytes() == b"tampered"  # original preserved


def test_record_to_manifest_has_metadata_no_payload(tmp_path: Path):
    rec = CaptureRecord(
        source_id="s", source_object_id="k", capture_version="aa", filename="aa",
        raw_sha256="aa", observation_utc="o", publication_timestamp="p",
        provenance_url="u", size_bytes=3, status="CREATED",
        path=str(tmp_path), meta_path=str(tmp_path),
    )
    m = rec.to_manifest()
    assert m["source_object_id"] == "k"
    assert m["capture_version"] == "aa"
    assert m["raw_sha256"] == "aa"
    assert m["status"] == "CREATED"
    assert "data" not in m and "bytes" not in m
