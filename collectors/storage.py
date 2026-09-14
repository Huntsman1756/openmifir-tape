"""Immutable, write-once raw storage with SHA-256 provenance.

Rules from docs/gates/G0.md §2 G0-A.3 and AGENTS.md hard rule 6:
- raw objects are written once and never overwritten;
- raw_sha256 is computed from the exact captured bytes;
- re-runs must not alter existing raw objects;
- if the same source object is encountered again, the existing object is
  preserved (no state change) and the capture is reported as already present.

The raw directory is the gitignored local data directory (`data/`). This
module never writes provider payloads anywhere under the repository tree.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_filename(name: str) -> str:
    """Reduce a source object key to a safe, reversible file name."""
    out = "".join(c if (c.isalnum() or c in ".-_") else "_" for c in name)
    out = out.strip("._")
    return out or "unnamed"


@dataclass
class CaptureRecord:
    """Metadata (never payload) describing one captured raw object."""

    source_id: str
    object_key: str
    filename: str
    raw_sha256: str
    observation_utc: str
    publication_timestamp: str
    provenance_url: str
    size_bytes: int
    status: str                 # CREATED | ALREADY_PRESENT
    path: str
    meta_path: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        """Evidence-safe representation: metadata only, never payload bytes."""
        return {
            "source_id": self.source_id,
            "object_key": self.object_key,
            "filename": self.filename,
            "raw_sha256": self.raw_sha256,
            "observation_utc": self.observation_utc,
            "publication_timestamp": self.publication_timestamp,
            "provenance_url": self.provenance_url,
            "size_bytes": self.size_bytes,
            "status": self.status,
            **self.extra,
        }


class WriteOnceConflict(Exception):
    """Raised when a raw object already exists with DIFFERENT bytes.

    The original object is never overwritten; the failure is recorded
    explicitly (fail closed, per G0-A.6).
    """


class RawStore:
    """Write-once raw object store under a gitignored local data directory."""

    def __init__(self, raw_root: Path):
        self.raw_root = raw_root

    def _dir_for(self, source_id: str, collection_date: str) -> Path:
        d = self.raw_root / source_id / collection_date
        d.mkdir(parents=True, exist_ok=True)
        return d

    def store(
        self,
        *,
        source_id: str,
        collection_date: str,
        object_key: str,
        data: bytes,
        publication_timestamp: str,
        provenance_url: str,
        observation_utc: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CaptureRecord:
        """Persist exact captured bytes once, immutably, and record SHA-256."""
        obs = observation_utc or utc_now_iso()
        digest = sha256_bytes(data)
        filename = sanitize_filename(object_key)
        obj_dir = self._dir_for(source_id, collection_date)
        raw_path = obj_dir / filename
        meta_path = obj_dir / f"{filename}.meta.yaml"

        raw_written = False
        try:
            fd = os.open(str(raw_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            pass
        else:
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            raw_written = True

        if raw_written:
            status = "CREATED"
        else:
            existing = raw_path.read_bytes()
            if sha256_bytes(existing) == digest:
                status = "ALREADY_PRESENT"
            else:
                raise WriteOnceConflict(
                    f"{raw_path} already exists with different bytes; "
                    f"refusing to overwrite (object_key={object_key!r})"
                )

        meta = {
            "source_id": source_id,
            "object_key": object_key,
            "filename": filename,
            "raw_sha256": digest,
            "size_bytes": len(data),
            "observation_utc": obs,
            "publication_timestamp": publication_timestamp,
            "provenance_url": provenance_url,
            "write_once": True,
        }
        if extra:
            meta.update(extra)

        if not meta_path.exists():
            with open(meta_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(meta, fh, sort_keys=False)

        record = CaptureRecord(
            source_id=source_id,
            object_key=object_key,
            filename=filename,
            raw_sha256=digest,
            observation_utc=obs,
            publication_timestamp=publication_timestamp,
            provenance_url=provenance_url,
            size_bytes=len(data),
            status=status,
            path=str(raw_path),
            meta_path=str(meta_path),
            extra=extra or {},
        )
        return record
