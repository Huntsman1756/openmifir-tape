import hashlib
import json
from copy import deepcopy

import pytest
import yaml

from collectors.storage import RawStore
from metrics.g2_authority import AuthorityError, digest
from metrics.g2_ingest import admit_window
from metrics.g2_registry import G1_BUNDLE_SHA256, G1_END, G1_START, admit, empty_registry, window_record


def file(root, name, content):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


@pytest.fixture
def prospective_input(tmp_path, window):
    descriptor = b"synthetic descriptor authority\n"
    file(tmp_path, "config/sources/bme_apa.yaml", descriptor)
    window["descriptor_sha256"]["bme_apa"] = hashlib.sha256(descriptor).hexdigest()
    raw = tmp_path / "raw"
    obj = RawStore(raw).store(source_id="bme_apa", collection_date="2026-10-01", object_key="synthetic.json",
                              data=b"[]", publication_timestamp="old", provenance_url="https://synthetic.invalid")
    result = {"source_id": "bme_apa", "observed_at": window["start"], "status": "SUCCEEDED",
              "context": {"mode": "poll"}, "object_count": 1, "objects": [obj.to_manifest()]}
    manifests = [file(tmp_path, "run.yaml", yaml.safe_dump({"run_id": "run", "results": result}).encode())]
    row = {"source_id": "bme_apa", "run_id": "run", "observed_at": window["start"],
           "status": "SUCCEEDED", "mode": "poll"}
    journal = file(tmp_path, "journal.jsonl", (json.dumps(row) + "\n").encode())
    bundle = {"schema": "g2-window-bundle-v1", "profile_sha256": digest(window), "manifests": manifests,
              "journal": journal, "acquisition_revision": "a" * 40, "deployment_revision": "b" * 40,
              "sealed_at": window["end"]}
    origin = window_record("G1_ORIGIN", "c" * 64, G1_BUNDLE_SHA256, G1_START, G1_END, ["bme_apa", "blb_apae"])
    previous = admit(empty_registry(), origin, [])
    return raw, window, bundle, previous, row


def test_integrated_admission_and_exact_replay(tmp_path, prospective_input):
    raw, window, bundle, previous, _ = prospective_input
    result = admit_window(tmp_path, raw, window, bundle, previous)
    assert result["operational_verdict"] == "NOT_ASSESSED"
    assert result["selection"]["census"]["unique_captures"] == 1
    assert result["journal_census"] == {"source_attempts": 1, "locked_attempts": 0}
    assert admit_window(tmp_path, raw, window, bundle, result["registry"])["registry"] == result["registry"]
    assert len(previous["windows"]) == 1


@pytest.mark.parametrize("fault", ["missing", "time", "status", "duplicate"])
def test_journal_fault_does_not_change_accepted_state(tmp_path, prospective_input, fault):
    raw, window, bundle, previous, row = prospective_input
    snapshot = deepcopy(previous)
    if fault == "time":
        row["observed_at"] = "2026-10-02T00:00:00Z"
    elif fault == "status":
        row["status"] = "FAILED"
    content = b"" if fault == "missing" else (json.dumps(row) + "\n").encode()
    if fault == "duplicate":
        content *= 2
    bundle["journal"] = file(tmp_path, "journal.jsonl", content)
    with pytest.raises(AuthorityError, match="journal"):
        admit_window(tmp_path, raw, window, bundle, previous)
    assert previous == snapshot


def test_changed_descriptor_and_selected_manifest_reject(tmp_path, prospective_input):
    raw, window, bundle, previous, _ = prospective_input
    (tmp_path / "config/sources/bme_apa.yaml").write_bytes(b"changed descriptor")
    with pytest.raises(AuthorityError, match="integrity"):
        admit_window(tmp_path, raw, window, bundle, previous)
