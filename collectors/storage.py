"""Immutable, write-once raw storage with SHA-256 provenance.

Identity model (per operator review, separating the logical source object from
its immutable capture/version):

- ``source_object_id``  : the logical remote object published by a source, e.g.
  ``BAPA-POST2-20260914-20:28:55.836-02.csv``. This is NOT assumed immutable.
- ``capture_version``   : the SHA-256 of the exact captured bytes for one
  observation. This IS immutable and is the physical capture identity.

Physical layout (exact names are not semantically significant):

    data/raw/<source_id>/<publication_date>/<source_object_id>/<capture_version>
    data/raw/<source_id>/<publication_date>/<source_object_id>/<capture_version>.meta.yaml

Invariants (enforced):

    same source_object_id + same bytes
        -> ALREADY_PRESENT      (zero state change; original metadata retained)

    same source_object_id + different bytes
        -> NEW version CREATED  (a new immutable capture; original preserved)

    same capture_version identity + different bytes
        -> WriteOnceConflict    (tamper / hash collision; original preserved)

This keeps SOURCE REOBSERVATION (G0-A1 capture model) distinct from INGEST
IDEMPOTENCE (G0-A2). It never overwrites and never deletes.

The raw directory is the gitignored local data directory (`data/`). This module
never writes provider payloads anywhere under the repository tree.
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
    """Reduce a name to a safe, reversible file-name component."""
    out = "".join(c if (c.isalnum() or c in ".-_") else "_" for c in name)
    out = out.strip("._")
    return out or "unnamed"


@dataclass
class CaptureRecord:
    """Metadata (never payload) describing one captured raw observation."""

    source_id: str
    source_object_id: str       # logical remote object (may change over time)
    capture_version: str        # immutable SHA-256 version identity
    filename: str               # physical capture file name (= capture_version)
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
            "source_object_id": self.source_object_id,
            "capture_version": self.capture_version,
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
    """Raised when the SAME capture/version identity holds DIFFERENT bytes.

    This is a tamper or hash-collision condition, not a normal re-observation.
    The original object is never overwritten; the failure is recorded explicitly
    (fail closed, per G0-A.6).
    """


class RawStore:
    """Write-once raw object store under a gitignored local data directory."""

    def __init__(self, raw_root: Path):
        self.raw_root = raw_root

    def store(
        self,
        *,
        source_id: str,
        collection_date: str,
        object_key: str,            # source_object_id
        data: bytes,
        publication_timestamp: str,
        provenance_url: str,
        observation_utc: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CaptureRecord:
        """Persist exact captured bytes as one immutable version.

        Returns a CaptureRecord; raises WriteOnceConflict only when the same
        capture/version identity holds different bytes.
        """
        obs = observation_utc or utc_now_iso()
        digest = sha256_bytes(data)
        source_object_id = sanitize_filename(object_key)
        obj_dir = self.raw_root / source_id / collection_date / source_object_id
        obj_dir.mkdir(parents=True, exist_ok=True)

        version_file = obj_dir / digest
        meta_path = obj_dir / f"{digest}.meta.yaml"

        created = False
        try:
            # O_BINARY (no-op on POSIX) prevents Windows text-mode newline
            # translation, which would otherwise corrupt the exact captured bytes.
            fd = os.open(str(version_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o644)
        except FileExistsError:
            pass
        else:
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            created = True

        if created:
            status = "CREATED"
        else:
            existing = version_file.read_bytes()
            if sha256_bytes(existing) == digest:
                status = "ALREADY_PRESENT"
            else:
                raise WriteOnceConflict(
                    f"{version_file} (capture_version={digest}) already exists with "
                    f"different bytes; refusing to overwrite "
                    f"(source_object_id={object_key!r})"
                )

        meta = {
            "source_id": source_id,
            "source_object_id": object_key,
            "capture_version": digest,
            "filename": str(version_file.name),
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
            with open(meta_path, "w", encoding="utf-8", newline="\n") as fh:
                yaml.safe_dump(meta, fh, sort_keys=False)

        return CaptureRecord(
            source_id=source_id,
            source_object_id=object_key,
            capture_version=digest,
            filename=str(version_file.name),
            raw_sha256=digest,
            observation_utc=obs,
            publication_timestamp=publication_timestamp,
            provenance_url=provenance_url,
            size_bytes=len(data),
            status=status,
            path=str(version_file),
            meta_path=str(meta_path),
            extra=extra or {},
        )
