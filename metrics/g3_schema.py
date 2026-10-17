"""ISO 20022 CTP schema authority for G3-A1; offline validation only (issue #76).

Frozen G3 §2. These helpers authenticate a pinned, single-direction schema
snapshot and plan its chronological admission. They perform no acquisition and
no activation: the schema files stay in the operator's local store, and the
repository keeps digests and provenance only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .g2_authority import (
    AuthorityError,
    checked_file,
    digest,
    file_entry,
    mapping,
    text,
    timestamp,
    utc,
)

DIRECTIONS = ("INPUT", "OUTPUT")
PUBLICATION_STATUSES = ("FINAL", "DRAFT")
ACQUISITION_OUTCOMES = ("SUCCEEDED", "FAILED", "PARTIAL")


def _definitions(entries: Any, direction: Any) -> list[dict]:
    if direction not in DIRECTIONS:
        raise AuthorityError("snapshot must declare exactly one message direction")
    if not isinstance(entries, list) or not entries:
        raise AuthorityError("at least one message definition is required")
    out: list[dict] = []
    names: set[str] = set()
    for value in entries:
        entry = mapping(value, {"name", "sha256", "bytes", "status", "direction", "code_sets"})
        file_entry({k: entry[k] for k in ("name", "sha256", "bytes")})
        if entry["direction"] != direction:
            raise AuthorityError("hybrid snapshot: definition direction differs from the snapshot")
        if entry["status"] not in PUBLICATION_STATUSES:
            raise AuthorityError("unsupported publication status")
        code_sets = entry["code_sets"]
        if (not isinstance(code_sets, list)
                or any(not isinstance(c, str) or not c.strip() or c != c.strip() for c in code_sets)
                or len(set(code_sets)) != len(code_sets)):
            raise AuthorityError("code sets must be unique nonempty unpadded identifiers")
        name = entry["name"]
        if name in names:
            raise AuthorityError("definition names must be unique")
        names.add(name)
        out.append({"name": name, "sha256": entry["sha256"], "bytes": entry["bytes"],
                    "status": entry["status"], "direction": direction,
                    "code_sets": sorted(code_sets)})
    if [e["name"] for e in out] != sorted(names):
        raise AuthorityError("definitions must be sorted by name")
    return out


def _acquisition(value: Any) -> dict:
    doc = mapping(value, {"acquisition_id", "outcome", "provenance"})
    if doc["outcome"] not in ACQUISITION_OUTCOMES:
        raise AuthorityError("unsupported acquisition outcome")
    return {"acquisition_id": text(doc["acquisition_id"], "acquisition_id"),
            "outcome": doc["outcome"],
            "provenance": text(doc["provenance"], "provenance")}


def _set_digest(definitions: list[dict]) -> str:
    return digest([{k: e[k] for k in ("name", "sha256", "bytes")} for e in definitions])


def _compose(submission_id: Any, message_set_id: Any, direction: Any,
             definitions: Any, observed_at: Any, acquisition: Any) -> dict:
    defs = _definitions(definitions, direction)
    body = {
        "schema": "g3-schema-snapshot-v1",
        "submission_id": text(submission_id, "submission_id"),
        "message_set_id": text(message_set_id, "message_set_id"),
        "direction": direction,
        "definitions": defs,
        "definition_set_sha256": _set_digest(defs),
        "observed_at": timestamp(utc(observed_at)),
        "acquisition": _acquisition(acquisition),
    }
    return {**body, "snapshot_id": digest(body), "schema_known_at": body["observed_at"]}


def schema_snapshot(root: Path, value: Any) -> dict:
    """Validate one single-direction snapshot and authenticate every declared file."""
    doc = mapping(value, {"schema", "submission_id", "message_set_id", "direction",
                          "definitions", "observed_at", "acquisition"})
    if doc["schema"] != "g3-schema-snapshot-v1":
        raise AuthorityError("unsupported schema snapshot")
    snapshot = _compose(doc["submission_id"], doc["message_set_id"], doc["direction"],
                        doc["definitions"], doc["observed_at"], doc["acquisition"])
    for entry in snapshot["definitions"]:
        checked_file(root, {k: entry[k] for k in ("name", "sha256", "bytes")})
    return snapshot


def identity(snapshot: dict) -> dict:
    """Rebuild a snapshot from its own fields; any mismatch means it was altered."""
    return _compose(snapshot["submission_id"], snapshot["message_set_id"], snapshot["direction"],
                    snapshot["definitions"], snapshot["observed_at"], snapshot["acquisition"])


def census(snapshot: dict) -> dict:
    """Pinned-definition census: counts, draft labels and referenced code sets."""
    defs = snapshot["definitions"]
    return {
        "direction": snapshot["direction"],
        "definitions": len(defs),
        "final": sum(1 for e in defs if e["status"] == "FINAL"),
        "draft": sum(1 for e in defs if e["status"] == "DRAFT"),
        "draft_names": sorted(e["name"] for e in defs if e["status"] == "DRAFT"),
        "code_sets": sorted({c for e in defs for c in e["code_sets"]}),
        "definition_set_sha256": snapshot["definition_set_sha256"],
    }


def _timeline(snapshots: list) -> dict:
    body = {"schema": "g3-schema-timeline-v1", "snapshots": snapshots}
    return {**body, "semantic_digest": digest(body)}


def empty_timeline() -> dict:
    return _timeline([])


def _validate_timeline(state: dict) -> None:
    if state != _timeline(state["snapshots"]):
        raise AuthorityError("schema timeline integrity failure")
    seen: set[str] = set()
    last = None
    for snapshot in state["snapshots"]:
        if snapshot != identity(snapshot):
            raise AuthorityError("accepted schema snapshot identity failure")
        known = utc(snapshot["schema_known_at"])
        if snapshot["snapshot_id"] in seen or (last is not None and known <= last):
            raise AuthorityError("accepted schema timeline chronology is ambiguous")
        seen.add(snapshot["snapshot_id"])
        last = known


def plan_snapshot_admission(previous: dict, snapshot: dict) -> dict:
    """Append only; exact replay is a no-op and failures leave caller state intact."""
    state = json.loads(json.dumps(previous))
    _validate_timeline(state)
    if snapshot != identity(snapshot):
        raise AuthorityError("schema snapshot identity failure")
    for old in state["snapshots"]:
        if old["snapshot_id"] == snapshot["snapshot_id"]:
            if old != snapshot:
                raise AuthorityError("conflicting accepted schema snapshot")
            return state
    known = utc(snapshot["schema_known_at"])
    if state["snapshots"]:
        last = utc(state["snapshots"][-1]["schema_known_at"])
        if known == last:
            raise AuthorityError("distinct snapshots at the same knowledge time are ambiguous")
        if known < last:
            raise AuthorityError("older schema snapshot requires a versioned correction proposal")
    state["snapshots"].append(snapshot)
    return _timeline(state["snapshots"])


def select_snapshot(timeline: dict, cutoff: str) -> dict | None:
    """Latest snapshot knowable at cutoff; never a future or absent snapshot."""
    _validate_timeline(timeline)
    moment = utc(cutoff)
    eligible = [s for s in timeline["snapshots"] if utc(s["schema_known_at"]) <= moment]
    return json.loads(json.dumps(eligible[-1])) if eligible else None
