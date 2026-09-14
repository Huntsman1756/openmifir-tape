"""Deterministic, offline tests for immutable raw storage (G0-A.3)."""

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


def test_store_creates_immutable_object_and_meta(tmp_path: Path):
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
    p = tmp_path / "src" / "2026-09-14" / "daily.json"
    assert p.read_bytes() == data
    meta = tmp_path / "src" / "2026-09-14" / "daily.json.meta.yaml"
    assert meta.exists()
    assert "raw_sha256" in meta.read_text(encoding="utf-8")
    assert meta.read_text(encoding="utf-8").count("monthly.json") == 0


def test_store_twice_identical_is_already_present_no_overwrite(tmp_path: Path):
    store = RawStore(tmp_path)
    data = b"same-bytes"
    store.store(source_id="s", collection_date="d", object_key="f.csv", data=data,
                publication_timestamp="t", provenance_url="u", observation_utc="o")
    rec2 = store.store(source_id="s", collection_date="d", object_key="f.csv", data=data,
                       publication_timestamp="t", provenance_url="u", observation_utc="o")
    assert rec2.status == "ALREADY_PRESENT"
    p = tmp_path / "s" / "d" / "f.csv"
    assert p.read_bytes() == data


def test_store_conflict_different_bytes_refuses_overwrite(tmp_path: Path):
    store = RawStore(tmp_path)
    store.store(source_id="s", collection_date="d", object_key="f.csv", data=b"original",
                publication_timestamp="t", provenance_url="u", observation_utc="o")
    with pytest.raises(WriteOnceConflict):
        store.store(source_id="s", collection_date="d", object_key="f.csv", data=b"tampered",
                    publication_timestamp="t", provenance_url="u", observation_utc="o")
    p = tmp_path / "s" / "d" / "f.csv"
    assert p.read_bytes() == b"original"


def test_record_to_manifest_has_metadata_no_payload(tmp_path: Path):
    rec = CaptureRecord(
        source_id="s", object_key="k", filename="k.csv", raw_sha256="aa",
        observation_utc="o", publication_timestamp="p", provenance_url="u",
        size_bytes=3, status="CREATED", path=str(tmp_path), meta_path=str(tmp_path),
    )
    m = rec.to_manifest()
    assert m["raw_sha256"] == "aa"
    assert m["status"] == "CREATED"
    assert "data" not in m and "bytes" not in m
