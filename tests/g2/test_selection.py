import hashlib
from datetime import timedelta

import pytest
import yaml

from collectors.storage import RawStore
from metrics.g2_authority import AuthorityError, utc
from metrics.g2_selection import load_metadata, select_attestations


@pytest.fixture
def observations(tmp_path):
    raw = tmp_path / "raw"
    obj = RawStore(raw).store(
        source_id="bme_apa", collection_date="2026-10-01", object_key="synthetic.json",
        data=b"[]", publication_timestamp="2026-09-01T00:00:00Z",
        provenance_url="https://synthetic.invalid/example", observation_utc="2026-10-01T00:00:00Z",
    ).to_manifest()

    def manifest(name, observed="2026-10-01T00:00:00Z", run="synthetic-run"):
        doc = {"run_id": run, "results": {"source_id": "bme_apa", "observed_at": observed,
                                         "objects": [obj], "object_count": 1}}
        content = yaml.safe_dump(doc).encode()
        (tmp_path / name).write_bytes(content)
        return {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}

    return raw, manifest


def select(tmp_path, entries, raw, **kwargs):
    return select_attestations(tmp_path, entries, raw, start=utc("2026-10-01T00:00:00Z"),
                               end=utc("2026-10-03T00:00:00Z"), sources=["bme_apa"], **kwargs)


def test_alias_is_not_an_observation(tmp_path, observations):
    raw, manifest = observations
    result = select(tmp_path, [manifest("a"), manifest("b")], raw)
    assert result["census"]["manifests_eligible"] == 2
    assert result["census"]["object_attestations"] == 1
    assert result["versions"][0].observation_count == 1


def test_half_open_and_legacy_inclusive_are_distinct(tmp_path, observations):
    raw, manifest = observations
    entries = [manifest("at-end", "2026-10-03T00:00:00Z")]
    assert select(tmp_path, entries, raw)["census"]["unique_captures"] == 0
    assert select(tmp_path, entries, raw, inclusive_end=True)["census"]["unique_captures"] == 1


def test_run_authority_conflicts_reject(tmp_path, observations):
    raw, manifest = observations
    entries = [manifest("a"), manifest("b", "2026-10-02T00:00:00Z")]
    with pytest.raises(AuthorityError, match="conflicting manifest"):
        select(tmp_path, entries, raw)


def test_unknown_time_cannot_use_filename(tmp_path, observations):
    raw, manifest = observations
    entries = [manifest("20261001T000000Z__manifest.yaml", "untimed")]
    with pytest.raises(AuthorityError, match="UTC"):
        select(tmp_path, entries, raw)


def test_every_duplicate_raw_copy_is_authenticated(tmp_path, observations):
    raw, manifest = observations
    entries = [manifest("a")]
    RawStore(raw).store(source_id="bme_apa", collection_date="2026-10-02", object_key="synthetic.json",
                        data=b"[]", publication_timestamp="old", provenance_url="https://synthetic.invalid")
    copies = sorted(raw.rglob(hashlib.sha256(b"[]").hexdigest()))
    copies[1].write_bytes(b"corrupted duplicate")
    with pytest.raises(AuthorityError, match="duplicate integrity"):
        select(tmp_path, entries, raw)


def test_new_run_retains_first_known_time(tmp_path, observations):
    raw, manifest = observations
    result = select(tmp_path, [manifest("a"), manifest("b", "2026-10-02T00:00:00Z", "later-run")], raw)
    version = result["versions"][0]
    assert version.observation_count == 2
    assert version.last_observed_at - version.first_observed_at == timedelta(days=1)


def test_duplicate_yaml_keys_rejected(tmp_path):
    p = tmp_path / "ambiguous.yaml"
    p.write_bytes(b"run_id: first\nrun_id: second\n")
    with pytest.raises(AuthorityError, match="duplicate"):
        load_metadata(p)


@pytest.mark.parametrize("payload", [b"{}", b"[3]", b"not json"])
def test_unparseable_capture_retained_in_census(tmp_path, payload):
    raw = tmp_path / "raw"
    obj = RawStore(raw).store(source_id="bme_apa", collection_date="2026-10-01", object_key="bad.json",
                              data=payload, publication_timestamp="old", provenance_url="https://synthetic.invalid")
    doc = {"run_id": "run", "results": {"source_id": "bme_apa", "observed_at": "2026-10-01T00:00:00Z",
                                        "object_count": 1, "objects": [obj.to_manifest()]}}
    content = yaml.safe_dump(doc).encode()
    (tmp_path / "manifest").write_bytes(content)
    result = select(tmp_path, [{"name": "manifest", "sha256": hashlib.sha256(content).hexdigest(),
                                "bytes": len(content)}], raw)
    assert result["census"]["unique_captures"] == result["census"]["parse_failures"] == 1
    assert result["admission_verdict"] == "ADMITTED_WITH_PARSE_DIAGNOSTICS"


def test_missing_eligible_raw_rejects(tmp_path, observations):
    raw, manifest = observations
    entries = [manifest("a")]
    version = next(p for p in raw.rglob(hashlib.sha256(b"[]").hexdigest()))
    version.unlink()
    with pytest.raises(AuthorityError, match="raw admission failed"):
        select(tmp_path, entries, raw)
