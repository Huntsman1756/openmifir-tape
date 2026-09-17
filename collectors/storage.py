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

Note: the payload and its ``.meta.yaml`` are two separate writes, not an
atomic pair. A crash between them can leave a raw object whose meta is
written on the next observation of the same bytes (self-healing), but no
guarantee is made that a payload can never exist without metadata.

The raw directory is the gitignored local data directory (`data/`). This module
never writes provider payloads anywhere under the repository tree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
            # "xb" = exclusive create (fail if exists) + binary write. Binary
            # mode prevents newline translation on Windows (exact bytes) and is
            # portable across Windows and POSIX. Unlike os.O_BINARY, this symbol
            # is not Windows-only.
            with open(version_file, "xb") as fh:
                fh.write(data)
            created = True
        except FileExistsError:
            created = False

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
            "collection_date": collection_date,
            "raw_sha256": digest,
            "size_bytes": len(data),
            "observation_utc": obs,
            "publication_timestamp": publication_timestamp,
            "provenance_url": provenance_url,
            "write_once": True,
        }
        if extra:
            meta.update(extra)

        # Write-once meta: exclusive create so a concurrent or repeated capture
        # can never overwrite the provenance recorded at first capture. A
        # racing loser REUSES the winner's meta after verifying it describes
        # this exact capture identity; a mismatch is corruption, not a race.
        try:
            with open(meta_path, "x", encoding="utf-8", newline="\n") as fh:
                yaml.safe_dump(meta, fh, sort_keys=False)
        except FileExistsError:
            try:
                existing_meta = yaml.safe_load(
                    meta_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                existing_meta = None
            if not isinstance(existing_meta, dict):
                existing_meta = {}
            if existing_meta.get("raw_sha256") != digest:
                # FileExistsError is control flow here, not the causal error.
                raise WriteOnceConflict(
                    f"{meta_path} exists but records raw_sha256="
                    f"{existing_meta.get('raw_sha256')!r}, expected {digest!r}; "
                    f"refusing to proceed (source_object_id={object_key!r})"
                ) from None

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
