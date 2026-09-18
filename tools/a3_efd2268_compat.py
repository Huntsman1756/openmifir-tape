"""Immutable compatibility layer for A3 adjudication (deployed efd2268).

The A3 verifier adjudicates evidence produced by the DEPLOYED collector at
commit efd2268. The primitives below are vendored verbatim from
``collectors/a3.py`` and ``collectors/storage.py`` at that SHA so that the
adjudicator's semantics cannot drift when ``main`` evolves. Any change here
must be a deliberate, reviewed decision — never an indirect side effect of
main-line edits.

Contents:

- ``UNVERIFIED_TS_MARGIN`` / ``parse_publication_timestamp`` — the deployed
  treatment of source publication timestamps, including the conservative
  6-hour uncertainty applied to unverified BAPA tokens.
- ``sanitize_filename`` / raw-store layout helpers — the deployed raw path
  rule ``raw/<source_id>/<collection_date>/<sanitized key>/<sha256>``.
- ``store_capture`` — a fixture writer replicating the deployed write-once
  store semantics, for tests that must not depend on the current RawStore.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

# -- vendored from collectors/a3.py @efd2268 ---------------------------------

UNVERIFIED_TS_MARGIN = timedelta(hours=6)

_BAPA_TOKEN_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})-(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:[+-]\d{2})?$"
)


def parse_publication_timestamp(ts: str | None) -> tuple[datetime | None, bool]:
    """Parse a source publication timestamp for windowing.

    Returns (instant, verified). ``verified`` is False when the timestamp was
    derived from an unverified source token (window selection then applies
    UNVERIFIED_TS_MARGIN). Returns (None, False) when unparseable — such
    objects are always included in a rescan window (fail toward capture).
    """
    text = (ts or "").strip()
    if not text:
        return None, False
    # The BAPA-POST2 filename token (e.g. "20260914-20:22:55.841-02") happens to
    # be valid ISO-8601 basic format, but config/sources/bloomberg_apae.yaml
    # marks its exact semantics unconfirmed, so it must NOT be trusted as a
    # verified instant: parse the wall-clock portion and flag it unverified.
    m = _BAPA_TOKEN_RE.match(text)
    if m:
        year, mon, day, hh, mm, ss, frac = m.groups()
        micros = int((frac or "0").ljust(6, "0")[:6])
        dt = datetime(int(year), int(mon), int(day), int(hh), int(mm), int(ss),
                      micros, tzinfo=UTC)
        return dt, False
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC), True
    except ValueError:
        return None, False


# -- vendored from collectors/storage.py @efd2268 ----------------------------

def sanitize_filename(name: str) -> str:
    out = "".join(c if (c.isalnum() or c in ".-_") else "_" for c in name)
    out = out.strip("._")
    return out or "unnamed"


META_SUFFIX = ".meta.yaml"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_object_dir(raw_root: Path, source_id: str, collection_date: str,
                   object_key: str) -> Path:
    """Deployed layout: raw/<sid>/<collection_date>/<sanitized key>/."""
    return (Path(raw_root) / source_id / collection_date
            / sanitize_filename(object_key))


def raw_object_path(raw_root: Path, source_id: str, collection_date: str,
                    object_key: str, capture_version: str) -> Path:
    return raw_object_dir(raw_root, source_id, collection_date,
                          object_key) / capture_version


def raw_meta_path(raw_file: Path) -> Path:
    return raw_file.with_name(raw_file.name + META_SUFFIX)


def store_capture(raw_root: Path, *, source_id: str, collection_date: str,
                  object_key: str, data: bytes, publication_timestamp: str,
                  provenance_url: str,
                  observation_utc: str) -> dict:
    """Replicates RawStore.store @efd2268 (write-once, meta first-write).

    Used by the synthetic test corpus so fixtures stay pinned to deployed
    semantics regardless of changes to collectors/storage.py on main.
    Returns the manifest-style object dict (metadata only).
    """
    raw_root = Path(raw_root)
    digest = sha256_bytes(data)
    obj_dir = raw_object_dir(raw_root, source_id, collection_date, object_key)
    obj_dir.mkdir(parents=True, exist_ok=True)

    version_file = obj_dir / digest
    meta_path = obj_dir / f"{digest}{META_SUFFIX}"

    created = False
    try:
        with open(version_file, "xb") as fh:
            fh.write(data)
        created = True
    except FileExistsError:
        pass

    if created:
        status = "CREATED"
    else:
        existing = version_file.read_bytes()
        if sha256_bytes(existing) == digest:
            status = "ALREADY_PRESENT"
        else:
            raise ValueError(
                f"{version_file} already exists with different bytes "
                f"(capture_version={digest}); refusing to overwrite")

    # efd2268 meta: source_object_id stores the UNSANITIZED key; meta is
    # written only if absent (first write wins).
    meta = {
        "source_id": source_id,
        "source_object_id": object_key,
        "capture_version": digest,
        "filename": str(version_file.name),
        "collection_date": collection_date,
        "raw_sha256": digest,
        "size_bytes": len(data),
        "observation_utc": observation_utc,
        "publication_timestamp": publication_timestamp,
        "provenance_url": provenance_url,
        "write_once": True,
    }
    if not meta_path.exists():
        with open(meta_path, "w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(meta, fh, sort_keys=False)

    return {
        "source_id": source_id,
        "source_object_id": object_key,
        "capture_version": digest,
        "filename": str(version_file.name),
        "raw_sha256": digest,
        "observation_utc": observation_utc,
        "publication_timestamp": publication_timestamp,
        "provenance_url": provenance_url,
        "size_bytes": len(data),
        "status": status,
    }
