"""Timed reference acquisition evidence; frozen G2-B (#48).

Validates metadata and exact bytes only. Content parsability and dated
classification are separate bounded B1 units; nothing is acquired here.
"""

from __future__ import annotations

import json
from pathlib import Path

from universe.reference import ReferenceSetError, canonical_reference_set

from .g2_authority import AuthorityError, checked_file, digest, file_entry, mapping, strict_pairs, text, timestamp, utc

FAMILIES = {"FITRS": "ISIN", "FIRDS": "ISIN", "GLEIF": "LEI"}
OUTCOMES = {"AVAILABLE", "NOT_FOUND", "ACQUISITION_FAILED", "UNAVAILABLE"}


def scope(value: dict, family: str) -> dict:
    doc = mapping(value, {"mode", "identifier_kind", "identifiers"})
    mode = text(doc["mode"], "scope mode")
    if mode not in {"ALL_FILE_RECORDS", "IDENTIFIERS"} or doc["identifier_kind"] != FAMILIES[family]:
        raise AuthorityError("reference scope/family mismatch")
    identifiers = doc["identifiers"]
    if not isinstance(identifiers, list):
        raise AuthorityError("scope identifiers must be a list")
    for identifier in identifiers:
        text(identifier, "requested identifier")
    if identifiers != sorted(set(identifiers)):
        raise AuthorityError("scope identifiers must be sorted and unique")
    if (doc["mode"] == "IDENTIFIERS") != bool(identifiers):
        raise AuthorityError("explicit scope requires identifiers; whole-file scope has none")
    return {**doc, "identifiers": list(identifiers)}


def timed_file(root: Path, value: dict) -> dict:
    doc = mapping(value, {"file", "observed_at"})
    entry = file_entry(doc["file"])
    if Path(entry["name"]).name != entry["name"]:
        raise AuthorityError("reference file logical name must be a basename")
    checked_file(root, entry)
    return {"file": entry, "observed_at": timestamp(utc(doc["observed_at"]))}


def observation(root: Path, value: dict) -> dict:
    """Bind each durable file and outcome receipt to acquisition/scope/time.

    Source dates are preserved provenance, never a local knowledge clock.
    The returned identity is independent of absolute paths and file mtimes.
    """
    doc = mapping(value, {
        "schema", "family", "acquisition_id", "scope", "source_provenance",
        "source_revision", "outcome", "files", "receipt",
    })
    family = text(doc["family"], "family")
    if doc["schema"] != "g2-reference-observation-v1" or family not in FAMILIES:
        raise AuthorityError("unsupported reference observation schema/family")
    acquisition = text(doc["acquisition_id"], "acquisition_id")
    provenance = text(doc["source_provenance"], "source_provenance")
    revision = doc["source_revision"]
    if revision is not None:
        text(revision, "source_revision")
    requested = scope(doc["scope"], family)
    outcome_name = text(doc["outcome"], "outcome")
    if outcome_name not in OUTCOMES or not isinstance(doc["files"], list):
        raise AuthorityError("invalid acquisition outcome/files")
    files = sorted((timed_file(root, f) for f in doc["files"]), key=lambda f: f["file"]["name"])
    if (doc["outcome"] == "AVAILABLE") != bool(files):
        raise AuthorityError("available acquisitions require files; unavailable outcomes declare none")
    try:
        family_set = canonical_reference_set(f["file"] for f in files)
    except ReferenceSetError as exc:
        raise AuthorityError("ambiguous canonical reference files") from exc
    receipt = timed_file(root, doc["receipt"])
    if receipt["file"]["name"] in {f["file"]["name"] for f in files}:
        raise AuthorityError("outcome receipt cannot alias a reference payload")
    try:
        outcome = json.loads(checked_file(root, receipt["file"]).read_bytes(), object_pairs_hook=strict_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityError("unparseable acquisition outcome receipt") from exc
    outcome = mapping(outcome, {
        "schema", "family", "acquisition_id", "scope", "source_provenance", "outcome", "reason",
    })
    expected = {"schema": "g2-acquisition-outcome-v1", "family": family,
                "acquisition_id": acquisition, "scope": requested, "source_provenance": provenance,
                "outcome": doc["outcome"]}
    if any(outcome[k] != v for k, v in expected.items()):
        raise AuthorityError("outcome receipt contradicts declared acquisition authority")
    reason = outcome["reason"]
    if reason is not None:
        text(reason, "acquisition reason")
    if doc["outcome"] != "AVAILABLE" and reason is None:
        raise AuthorityError("unavailable acquisition requires an explicit reason")
    body = {**doc, "scope": requested, "files": files, "receipt": receipt,
            "family_set": family_set, "reason": reason,
            "observed_at": max(f["observed_at"] for f in [*files, receipt]),
            "source_freshness": "UNKNOWN"}
    return {**body, "observation_id": digest(body)}
