"""Synthetic ISO 20022 schema snapshots only; no live or ISO 20022 data."""

import hashlib
from pathlib import Path

import pytest


def _file(root: Path, name: str, payload: bytes) -> dict:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {"name": name, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


@pytest.fixture
def snapshot_factory(tmp_path):
    """Build a synthetic single-direction snapshot together with its files."""

    def make(*, episode="s1", direction="OUTPUT", observed_at="2026-10-01T12:00:00Z",
             names=("msg-a.json",), statuses=None, code_sets=None, outcome="SUCCEEDED"):
        root = tmp_path / episode
        root.mkdir(parents=True, exist_ok=True)
        statuses, code_sets = statuses or {}, code_sets or {}
        definitions = []
        for name in names:
            entry = _file(root, name, f"synthetic {name}".encode())
            definitions.append({**entry, "status": statuses.get(name, "FINAL"), "direction": direction,
                                "code_sets": code_sets.get(name, ["ExternalCodeSet"])})
        definitions.sort(key=lambda d: d["name"])
        doc = {"schema": "g3-schema-snapshot-v1", "submission_id": "ESMA-CTP-266",
               "message_set_id": "ctp-data-reporting-1", "direction": direction,
               "definitions": definitions, "observed_at": observed_at,
               "acquisition": {"acquisition_id": episode, "outcome": outcome,
                               "provenance": "https://synthetic.invalid/iso20022"}}
        return root, doc

    return make
