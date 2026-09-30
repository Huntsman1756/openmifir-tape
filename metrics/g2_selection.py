"""Exact attestation selection; G2 half-open and G1 inclusive stay distinct.

Authority: docs/gates/G2.md#G2-A, bounded selection unit of issue #47.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml

from normalization.evidence import _load_bloomberg_csv
from normalization.normalize import normalize_record

from .g2_authority import AuthorityError, checked_file, fingerprint, text, timestamp, utc
from .selection import CaptureVersionRef, SelectionError, _index_raw, _meta_check, _resolve_version


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML mappings cannot silently overwrite declared authority."""


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        try:
            if key in result:
                raise AuthorityError("duplicate metadata key")
            result[key] = loader.construct_object(value_node)
        except TypeError as exc:
            raise AuthorityError("metadata mapping keys must be scalar") from exc
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_metadata(path: Path) -> dict:
    try:
        # SafeLoader subclass rejects duplicate keys without enabling object tags.
        value = yaml.load(path.read_bytes(), Loader=UniqueKeyLoader)  # noqa: S506
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AuthorityError(f"invalid metadata: {path.name}") from exc
    if not isinstance(value, dict):
        raise AuthorityError("metadata must be a mapping")
    return value


def _parse_capture(version: CaptureVersionRef) -> tuple[list, str | None]:
    """Preserve G0 normalization, diagnosing shapes its loaders skip silently."""
    try:
        if version.source_id == "bme_apa":
            rows = json.loads(version.path.read_bytes())
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                return [], "INVALID_JSON_RECORD_CONTAINER"
        else:
            content = version.path.read_text(encoding="utf-8")
            if not any(line.lstrip().startswith("Trading date and time") for line in content.splitlines()):
                return [], "MISSING_CSV_HEADER"
            rows = _load_bloomberg_csv(version.path)
        return [normalize_record(version.source_id, r) for r in rows], None
    except (UnicodeDecodeError, ValueError, TypeError, AttributeError) as exc:
        return [], type(exc).__name__


def select_attestations(
    root: Path, entries: list[dict], raw_root: Path, *,
    start: datetime, end: datetime, sources: list[str], inclusive_end: bool = False,
) -> dict:
    """Read only authenticated manifests; filenames never establish time/identity.

    The inclusive switch is for the separately pinned G1 adapter only. Parse
    failures stay in the census, while authority/raw integrity errors reject.
    """
    start, end = utc(timestamp(start)), utc(timestamp(end))
    if start >= end:
        raise AuthorityError("nonempty observation interval required")
    raw_index, cv_index = _index_raw(raw_root)
    runs, attestations, captures = {}, {}, {}
    eligible, excluded = [], []
    for entry in entries:
        doc = load_metadata(checked_file(root, entry))
        result = doc.get("results")
        if not isinstance(result, dict):
            raise AuthorityError("manifest results required")
        run_id = text(doc.get("run_id"), "run_id")
        sid = text(result.get("source_id"), "source_id")
        observed = utc(result.get("observed_at"))
        run_key = (sid, run_id)
        authority = (entry["sha256"], timestamp(observed))
        if run_key in runs and runs[run_key] != authority:
            raise AuthorityError("conflicting manifest authority for source/run")
        runs[run_key] = authority
        selected = start <= observed and (observed <= end if inclusive_end else observed < end)
        if not selected:
            excluded.append(entry["name"])
            continue
        if sid not in sources:
            raise AuthorityError("eligible source not declared in profile")
        eligible.append(entry["name"])
        objects = result.get("objects")
        if (not isinstance(objects, list) or type(result.get("object_count")) is not int
                or result["object_count"] != len(objects)):
            raise AuthorityError("manifest object census mismatch")
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("source_id") != sid:
                raise AuthorityError("object source binding mismatch")
            key = text(obj.get("source_object_id"), "source_object_id")
            cv = fingerprint(obj.get("capture_version"))
            if obj.get("raw_sha256") != cv:
                raise AuthorityError("raw hash/capture identity mismatch")
            aid = (sid, run_id, key, cv)
            attestation = {
                "source_id": sid, "run_id": run_id, "source_object_id": key,
                "capture_version": cv, "manifest_sha256": entry["sha256"],
                "observed_at": timestamp(observed),
            }
            if aid in attestations and attestations[aid] != attestation:
                raise AuthorityError("conflicting attestation authority")
            attestations[aid] = attestation
            captures.setdefault((sid, key, cv), {})[aid] = observed
    versions = []
    failures, unidentified, records_total = [], 0, 0
    for (sid, key, cv), observations in sorted(captures.items()):
        try:
            path = _resolve_version(raw_index, cv_index, sid, key, cv)
            copies = raw_index[(sid, path.parent.name, cv)]
            for copy in copies:
                parts = copy.relative_to(raw_root).parts
                if (len(parts) != 4 or parts[0] != sid
                        or not copy.resolve().is_relative_to(raw_root.resolve())):
                    raise AuthorityError("raw copy escapes store")
                meta_path = copy.with_name(copy.name + ".meta.yaml")
                load_metadata(meta_path)  # Reject ambiguous sidecars before G1 binding checks.
                reason = _meta_check(meta_path, sid, key, cv, copy.parent.parent.name)
                if reason or hashlib.sha256(copy.read_bytes()).hexdigest() != cv:
                    raise AuthorityError("raw duplicate integrity/binding failure")
            version = CaptureVersionRef(sid, key, cv, path, min(observations.values()),
                                        max(observations.values()), len(observations))
            rows, error = _parse_capture(version)
        except (OSError, SelectionError) as exc:
            raise AuthorityError(f"raw admission failed: {sid}/{key}/{cv}") from exc
        versions.append(version)
        if error is not None:
            failures.append({"source_id": sid, "source_object_id": key, "capture_version": cv, "reason": error})
        else:
            records_total += len(rows)
            unidentified += sum(not (r.instrument_identification_code or "").strip() for r in rows)
    return {
        "attestations": [a for _, a in sorted(attestations.items())], "versions": versions,
        "excluded_manifests": sorted(excluded), "eligible_manifests": sorted(eligible),
        "parse_failures": failures,
        "census": {
            "manifests_total": len(entries), "manifests_eligible": len(eligible),
            "manifests_excluded": len(excluded), "object_attestations": len(attestations),
            "unique_captures": len(versions), "parse_failures": len(failures),
            "records_total": records_total, "records_without_identifier": unidentified,
        },
        "admission_verdict": "ADMITTED_WITH_PARSE_DIAGNOSTICS" if failures or unidentified else "ADMITTED",
    }
