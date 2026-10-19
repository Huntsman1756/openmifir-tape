import hashlib
import json
import os
from copy import deepcopy

import pytest

from metrics.g2_authority import AuthorityError
from metrics.g2_reference_observation import observation


def stored(root, name, content, observed="2026-10-01T10:00:00Z"):
    (root / name).write_bytes(content)
    return {"file": {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)},
            "observed_at": observed}


@pytest.fixture
def reference_input(tmp_path):
    scope = {"mode": "IDENTIFIERS", "identifier_kind": "ISIN", "identifiers": ["SYNTHETIC-ISIN"]}
    receipt = {"schema": "g2-acquisition-outcome-v1", "family": "FITRS", "acquisition_id": "synthetic-acquisition",
               "scope": scope, "source_provenance": "https://synthetic.invalid/reference",
               "outcome": "AVAILABLE", "reason": None}
    doc = {"schema": "g2-reference-observation-v1", "family": "FITRS", "acquisition_id": receipt["acquisition_id"],
           "scope": scope, "source_provenance": receipt["source_provenance"], "source_revision": "1990-01-01",
           "outcome": "AVAILABLE", "files": [stored(tmp_path, "synthetic.zip", b"synthetic bytes")],
           "receipt": stored(tmp_path, "outcome.json", json.dumps(receipt).encode(), "2026-10-01T11:00:00Z")}
    return doc, receipt


def test_local_durable_observation_is_not_source_date_or_mtime(tmp_path, reference_input):
    doc, _ = reference_input
    first = observation(tmp_path, doc)
    os.utime(tmp_path / "synthetic.zip", (0, 0))
    assert observation(tmp_path, doc) == first
    assert first["observed_at"] == "2026-10-01T11:00:00.000000Z"
    assert first["source_revision"] == "1990-01-01" and first["source_freshness"] == "UNKNOWN"


@pytest.mark.parametrize("outcome", ["ACQUISITION_FAILED", "UNAVAILABLE", "NOT_FOUND"])
def test_explicit_unavailability_is_valid_knowledge(tmp_path, reference_input, outcome):
    doc, receipt = reference_input
    doc["files"] = []
    doc["outcome"] = receipt["outcome"] = outcome
    receipt["reason"] = "synthetic recorded outcome"
    doc["receipt"] = stored(tmp_path, "outcome.json", json.dumps(receipt).encode())
    result = observation(tmp_path, doc)
    assert result["outcome"] == outcome and result["family_set"]["entries"] == []


def test_receipt_scope_and_outcome_must_match(tmp_path, reference_input):
    doc, receipt = reference_input
    receipt["scope"] = {"mode": "ALL_FILE_RECORDS", "identifier_kind": "ISIN", "identifiers": []}
    doc["receipt"] = stored(tmp_path, "outcome.json", json.dumps(receipt).encode())
    with pytest.raises(AuthorityError, match="contradicts"):
        observation(tmp_path, doc)


@pytest.mark.parametrize("fault", ["time", "missing", "hash"])
def test_invalid_declared_present_evidence_rejects(tmp_path, reference_input, fault):
    doc, _ = reference_input
    if fault == "time":
        doc["files"][0]["observed_at"] = "untimed"
    elif fault == "missing":
        (tmp_path / "synthetic.zip").unlink()
    else:
        (tmp_path / "synthetic.zip").write_bytes(b"different bytes")
    with pytest.raises(AuthorityError):
        observation(tmp_path, doc)


def test_unavailable_cannot_carry_files_from_an_older_snapshot(tmp_path, reference_input):
    doc, _ = reference_input
    doc["outcome"] = "ACQUISITION_FAILED"
    with pytest.raises(AuthorityError, match="declare none"):
        observation(tmp_path, doc)


def test_input_not_mutated_and_file_order_is_canonical(tmp_path, reference_input):
    doc, _ = reference_input
    doc["files"].append(stored(tmp_path, "another.zip", b"second synthetic file"))
    before = deepcopy(doc)
    first = observation(tmp_path, doc)
    assert doc == before
    doc["files"].reverse()
    assert observation(tmp_path, doc) == first


def test_duplicate_declared_file_and_nontext_family_reject(tmp_path, reference_input):
    doc, _ = reference_input
    doc["files"].append(deepcopy(doc["files"][0]))
    with pytest.raises(AuthorityError, match="ambiguous"):
        observation(tmp_path, doc)
    doc["family"] = []
    with pytest.raises(AuthorityError, match="text"):
        observation(tmp_path, doc)
