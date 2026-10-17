"""Integrated offline input admission; frozen G2-A1 (#47).

Use import_origin first, then admit_window in prospective chronological order.
This library plans metadata; it neither acquires data nor activates C1 builds.
"""

from __future__ import annotations

import json
from pathlib import Path

from .g2_authority import (
    AuthorityError,
    checked_file,
    digest,
    profile,
    sealed_bundle,
    strict_pairs,
    text,
    timestamp,
    utc,
)
from .g2_registry import admit, window_record
from .g2_selection import load_metadata, select_attestations

DESCRIPTOR_NAMES = {"bme_apa": "bme_apa.yaml", "blb_apae": "bloomberg_apae.yaml"}


def _journal(root: Path, bundle: dict, window: dict) -> dict:
    """Reconcile source attempts with manifest authority, including empty runs."""
    start, end = utc(window["start"]), utc(window["end"])
    manifests = {}
    for entry in bundle["manifests"]:
        doc = load_metadata(checked_file(root, entry))
        result = doc["results"]
        if not start <= utc(result["observed_at"]) < end:
            continue
        key = (result["source_id"], doc["run_id"])
        mode = (result.get("context") or {}).get("mode")
        manifests[key] = (timestamp(utc(result["observed_at"])), result.get("status"), mode)
    rows, seen, locked = {}, set(), 0
    try:
        content = checked_file(root, bundle["journal"]).read_text(encoding="utf-8")
        for line in content.splitlines():
            if not line.strip():
                continue
            row = json.loads(line, object_pairs_hook=strict_pairs)
            if not isinstance(row, dict):
                raise AuthorityError("journal row must be an object")
            observed = utc(row.get("observed_at"))
            if not start <= observed < end:
                continue
            run = text(row.get("run_id"), "journal run_id")
            if row.get("mode") not in {"poll", "rolling"}:
                raise AuthorityError("unknown journal acquisition mode")
            key = (row.get("source_id"), run)
            if key in seen:
                raise AuthorityError("duplicate journal source/run")
            seen.add(key)
            if row.get("status") == "SKIPPED_LOCKED":
                if row.get("source_id") is not None:
                    raise AuthorityError("locked attempt cannot attest a source capture")
                locked += 1
                continue
            if row.get("source_id") not in window["sources"] or row.get("status") not in {
                "SUCCEEDED", "PARTIAL", "FAILED", "NOT_RUN", "BLOCKED",
            }:
                raise AuthorityError("invalid source/status in journal")
            rows[key] = (timestamp(observed), row["status"], row["mode"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityError("invalid journal metadata") from exc
    if manifests != rows:
        raise AuthorityError("journal and eligible manifest authority differ")
    return {"source_attempts": len(rows), "locked_attempts": locked}


def admit_window(root: Path, raw_root: Path, window: dict, bundle: dict, previous: dict) -> dict:
    """Authenticate a prospective bundle and return an immutable admission plan."""
    window = profile(window)
    bundle = sealed_bundle(root, bundle, window)
    for sid, sha in window["descriptor_sha256"].items():
        name = "config/sources/" + DESCRIPTOR_NAMES[sid]
        try:
            size = (root / name).stat().st_size
        except OSError as exc:
            raise AuthorityError("missing source descriptor authority") from exc
        checked_file(root, {"name": name, "sha256": sha, "bytes": size})
    selection = select_attestations(root, bundle["manifests"], raw_root, start=utc(window["start"]),
                                   end=utc(window["end"]), sources=window["sources"])
    journal_census = _journal(root, bundle, window)
    record = window_record("G2_PROSPECTIVE", digest(window), digest(bundle), window["start"],
                           window["end"], window["sources"])
    return {"window": record, "profile": window, "bundle": bundle, "selection": selection,
            "journal_census": journal_census, "registry": admit(previous, record, selection["attestations"]),
            "operational_verdict": "NOT_ASSESSED"}
