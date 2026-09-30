import hashlib

import pytest
import yaml

import metrics.g2_origin as origin
import metrics.g2_registry as registry
from collectors.storage import RawStore
from metrics.g2_authority import AuthorityError


@pytest.fixture
def sealed_origin(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    obj = RawStore(raw).store(source_id="bme_apa", collection_date="2026-09-24", object_key="synthetic.json",
                              data=b"[]", publication_timestamp="old", provenance_url="https://synthetic.invalid")
    runs = tmp_path / "runs"
    runs.mkdir()
    name = "20260924T070000Z__bme_apa__manifest.yaml"
    doc = {"run_id": "20260924T070000Z", "results": {"source_id": "bme_apa", "observed_at": registry.G1_END,
                                                   "object_count": 1, "objects": [obj.to_manifest()]}}
    (runs / name).write_bytes(yaml.safe_dump(doc).encode())
    (tmp_path / "collection-meta.yaml").write_bytes(yaml.safe_dump({"bundle_id": origin.G1_BUNDLE_ID}).encode())
    lines = [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.relative_to(tmp_path).as_posix()
             for p in sorted(tmp_path.rglob("*")) if p.is_file()]
    content = ("\n".join(lines) + "\n").encode()
    (tmp_path / "SHA256SUMS.txt").write_bytes(content)
    # Synthetic pin injected only in this test process; production pins stay exact.
    pin = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(origin, "G1_BUNDLE_SHA256", pin)
    monkeypatch.setattr(registry, "G1_BUNDLE_SHA256", pin)
    return tmp_path


def test_original_end_is_included_and_replay_is_exact(sealed_origin):
    result = origin.import_origin(sealed_origin)
    assert result["selection"]["census"]["unique_captures"] == 1
    assert result["origin_binding"]["reference_classification"] == "LEGACY_BUILD_SNAPSHOT"
    assert origin.import_origin(sealed_origin, result["registry"])["registry"] == result["registry"]


def test_changed_seal_or_original_file_rejects(sealed_origin):
    (sealed_origin / "collection-meta.yaml").write_bytes(b"altered metadata")
    with pytest.raises(AuthorityError, match="integrity"):
        origin.import_origin(sealed_origin)
    (sealed_origin / "SHA256SUMS.txt").write_bytes(b"not the verified bundle")
    with pytest.raises(AuthorityError, match="fingerprint"):
        origin.import_origin(sealed_origin)
