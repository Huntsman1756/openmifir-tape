"""Offline window/package validation under frozen G2 §2 (issue #47).

These functions establish input integrity, not operational completeness.
Provider bytes remain in the operator's local store.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any


class AuthorityError(ValueError):
    """Reject ambiguous or unauthenticated G2 authority."""


def digest(value: Any) -> str:
    """Canonical metadata identity, independent of dictionary iteration."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def utc(value: Any) -> datetime:
    """Require an explicitly UTC, timezone-aware observation."""
    if not isinstance(value, str):
        raise AuthorityError("timestamp must be UTC text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AuthorityError("invalid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise AuthorityError("timestamp must have explicit UTC offset")
    return parsed.astimezone(UTC)


def timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AuthorityError(f"{field}: nonempty unpadded text required")
    return value


def strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuthorityError("duplicate journal key")
        result[key] = value
    return result


def fingerprint(value: Any, size: int = 64) -> str:
    if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{size}}}", value):
        raise AuthorityError(f"expected lowercase {size}-hex fingerprint")
    return value


def mapping(value: Any, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise AuthorityError(f"expected fields: {sorted(fields)}")
    return value


def profile(value: Any) -> dict:
    """Validate a prospective profile; legacy G1 imports use their own adapter."""
    doc = mapping(value, {
        "schema", "profile_version", "sources", "descriptor_sha256", "start", "end",
        "frozen_at", "expected_trading_dates", "calendar_basis", "schedule_authority",
        "evidence_tail_policy",
    })
    if doc["schema"] != "g2-window-profile-v1":
        raise AuthorityError("unsupported profile schema")
    sources = doc["sources"]
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(s, str) or s not in {"bme_apa", "blb_apae"} for s in sources)
            or len(set(sources)) != len(sources)):
        raise AuthorityError("unique supported sources required")
    descriptors = mapping(doc["descriptor_sha256"], set(sources))
    for sha in descriptors.values():
        fingerprint(sha)
    start, end, frozen = (utc(doc[k]) for k in ("start", "end", "frozen_at"))
    if not frozen < start < end:
        raise AuthorityError("profile must be frozen before a nonempty interval")
    tail = mapping(doc["evidence_tail_policy"], {"subject_period_end", "duration_seconds", "authority"})
    duration = tail["duration_seconds"]
    subject_end = utc(tail["subject_period_end"])
    if (type(duration) is not int or duration < 0 or not start < subject_end <= end
            or (end - subject_end).total_seconds() != duration):
        raise AuthorityError("declared evidence tail must match end")
    text(tail["authority"], "tail authority")
    dates = doc["expected_trading_dates"]
    if not isinstance(dates, list) or not dates or any(not isinstance(d, str) for d in dates):
        raise AuthorityError("explicit trading dates required")
    try:
        parsed_dates = [date.fromisoformat(d) for d in dates]
    except ValueError as exc:
        raise AuthorityError("invalid trading date") from exc
    if dates != sorted(set(dates)) or any(d.isoformat() != s for d, s in zip(parsed_dates, dates, strict=True)):
        raise AuthorityError("trading dates must be canonical and unique")
    if doc["schedule_authority"] != "docs/gates/G0.md#G0-A":
        raise AuthorityError("schedule/rescan/retry authority must remain G0-A")
    text(doc["profile_version"], "profile_version")
    text(doc["calendar_basis"], "calendar_basis")
    # Copy nested caller-owned data; schema forbids unrecognized semantic fields.
    return json.loads(json.dumps(doc))


def file_entry(value: Any) -> dict:
    entry = mapping(value, {"name", "sha256", "bytes"})
    name = text(entry["name"], "metadata name")
    path = PurePosixPath(name)
    if (path.is_absolute() or "\\" in name or ":" in name
            or any(p in {"", ".", ".."} for p in name.split("/"))):
        raise AuthorityError("metadata names must be safe relative POSIX paths")
    fingerprint(entry["sha256"])
    if type(entry["bytes"]) is not int or entry["bytes"] < 0:
        raise AuthorityError("byte count must be a nonnegative integer")
    return dict(entry)


def checked_file(root: Path, value: Any) -> Path:
    """Authenticate each physical copy, including aliases and symbolic links."""
    entry = file_entry(value)
    base = root.resolve()
    path = (base / entry["name"]).resolve()
    if not path.is_relative_to(base):
        raise AuthorityError("metadata path escapes bundle root")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise AuthorityError(f"missing/unreadable metadata: {entry['name']}") from exc
    if len(content) != entry["bytes"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
        raise AuthorityError(f"metadata integrity failure: {entry['name']}")
    return path


def sealed_bundle(root: Path, value: Any, window: Any) -> dict:
    """Validate all declared metadata before selecting any observation."""
    window = profile(window)
    doc = mapping(value, {
        "schema", "profile_sha256", "manifests", "journal", "acquisition_revision",
        "deployment_revision", "sealed_at",
    })
    if doc["schema"] != "g2-window-bundle-v1" or doc["profile_sha256"] != digest(window):
        raise AuthorityError("bundle/profile authority mismatch")
    if utc(doc["sealed_at"]) < utc(window["end"]):
        raise AuthorityError("bundle cannot seal before the window ends")
    fingerprint(doc["acquisition_revision"], 40)
    fingerprint(doc["deployment_revision"], 40)
    if not isinstance(doc["manifests"], list):
        raise AuthorityError("manifest declarations must be a list")
    entries = [file_entry(e) for e in doc["manifests"]]
    journal = file_entry(doc["journal"])
    names = [e["name"] for e in entries]
    if names != sorted(set(names)) or journal["name"] in names:
        raise AuthorityError("metadata names must be sorted and unique")
    for entry in [*entries, journal]:
        checked_file(root, entry)
    return json.loads(json.dumps(doc))
