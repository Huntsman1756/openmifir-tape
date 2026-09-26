"""Canonical reference-set representation for G1 BUILD_SNAPSHOTs (G1 §4.4).

A reference family (FITRS / FIRDS / GLEIF) is a multi-file pinned snapshot.
Its canonical representation is a deterministic, sorted list of
``{name, sha256, bytes}`` entries plus a ``set_digest`` computed over that
canonical metadata — never a single-file hash.

Guarantees:

- ``name`` is a logical entry name (file basename or an explicit key like
  ``<LEI>.json``), never an absolute or operator-local filesystem path;
- entries are sorted by ``name`` so input ordering does not matter;
- ``set_digest`` is stable across filesystem roots and process runs: same
  entry names + same bytes -> same digest.

This module computes metadata only; it never parses provider payloads.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

_REQUIRED_KEYS = ("name", "sha256", "bytes")


class ReferenceSetError(Exception):
    """Fail-closed error: malformed entry, duplicate name, bad digest."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reference_entry(name: str, data: bytes) -> dict:
    """One canonical ``{name, sha256, bytes}`` entry."""
    return {"name": name, "sha256": sha256_bytes(data), "bytes": len(data)}


def canonical_reference_set(entries: Iterable[dict]) -> dict:
    """Canonicalize ``{name, sha256, bytes}`` entries and compute set_digest.

    Entries are sorted by ``name``; every entry must carry exactly the
    canonical keys with a non-empty name, a 64-hex sha256 and a non-negative
    byte count. Duplicate names fail closed.
    """
    seen: set[str] = set()
    canon: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != set(_REQUIRED_KEYS):
            raise ReferenceSetError(
                f"reference entry must be exactly {_REQUIRED_KEYS}: {entry!r}")
        name = entry["name"]
        sha = entry["sha256"]
        size = entry["bytes"]
        if not isinstance(name, str) or not name or name != Path(name).name:
            raise ReferenceSetError(
                f"reference entry name must be a plain basename: {name!r}")
        if not isinstance(sha, str) or len(sha) != 64 \
                or sha != sha.lower() \
                or any(c not in "0123456789abcdef" for c in sha):
            raise ReferenceSetError(
                f"reference entry sha256 must be lowercase hex: {sha!r}")
        if not isinstance(size, int) or size < 0:
            raise ReferenceSetError(
                f"reference entry bytes must be a non-negative int: {size!r}")
        if name in seen:
            raise ReferenceSetError(f"duplicate reference entry name: {name!r}")
        seen.add(name)
        canon.append({"name": name, "sha256": sha, "bytes": size})
    canon.sort(key=lambda e: e["name"])
    digest = hashlib.sha256(
        json.dumps(canon, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"entries": canon, "set_digest": digest}


def reference_set_from_files(paths: Iterable[Path]) -> dict:
    """Canonical set over explicit files; entry name = file basename."""
    return canonical_reference_set(
        reference_entry(p.name, p.read_bytes()) for p in paths)


def reference_set_from_dir(directory: Path, *, exclude_names:
                         Iterable[str] = ()) -> dict:
    """Canonical set over every regular file directly inside ``directory``.

    ``exclude_names`` skips local bookkeeping files (e.g. an acquisition
    manifest) that are NOT part of the reference basis.
    """
    excluded = set(exclude_names)
    files = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.name not in excluded)
    return reference_set_from_files(files)
