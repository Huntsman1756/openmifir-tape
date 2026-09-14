"""G0-A capture orchestration: discover -> fetch -> store -> record metadata.

Dies at the boundary the spec demands (G0-A.1). It performs no normalization,
no universe logic, no dedup beyond capture identity. Outcomes are recorded as
metadata-only SourceRunResult objects for the evidence manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sources import get_adapter
from .sources.base import HttpGetter
from .storage import CaptureRecord, RawStore


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SourceRunResult:
    source_id: str
    attempted: bool
    status: str                      # SUCCEEDED | FAILED | NOT_RUN | BLOCKED
    observed_at: str
    objects: list[CaptureRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"

    @property
    def object_count(self) -> int:
        return len(self.objects)

    def to_evidence(self) -> dict[str, Any]:
        """Metadata-only evidence. NEVER includes provider payload bytes."""
        return {
            "source_id": self.source_id,
            "attempted": self.attempted,
            "status": self.status,
            "observed_at": self.observed_at,
            "object_count": self.object_count,
            "created_count": sum(1 for o in self.objects if o.status == "CREATED"),
            "already_present_count": sum(1 for o in self.objects if o.status == "ALREADY_PRESENT"),
            "objects": [o.to_manifest() for o in self.objects],
            "errors": self.errors,
            "log": self.log,
        }


def run_source(
    source_id: str,
    conf: dict[str, Any],
    store: RawStore,
    http_get: HttpGetter,
    *,
    now: str | None = None,
    max_objects: int = 1,
    keep_errors: bool = True,
) -> SourceRunResult:
    """Run a single source through the G0-A capture pipeline one time."""
    observed_at = now or _utc_now_iso()
    adapter = get_adapter(source_id)
    result = SourceRunResult(source_id=source_id, attempted=True, status="SUCCEEDED", observed_at=observed_at)

    try:
        discovered = adapter.discover(conf, http_get)
    except Exception as exc:  # noqa: BLE001 - record failure, fail closed
        result.status = "FAILED" if keep_errors else "FAILED"
        result.errors.append(f"discover: {exc}")
        result.log.append(f"{source_id}: discover failed: {exc}")
        return result

    if not discovered:
        result.status = "NOT_RUN"
        result.errors.append("discover: zero objects resolved")
        result.log.append(f"{source_id}: no objects discovered (NOT_RUN)")
        return result

    # Newest is last (adapters return sorted by publication_timestamp).
    selected = discovered[-max_objects:] if max_objects is not None else discovered
    result.log.append(f"{source_id}: discovered {len(discovered)}, selecting {len(selected)}")

    for obj in selected:
        try:
            data = adapter.fetch(conf, http_get, obj)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"fetch {obj.object_key}: {exc}")
            result.log.append(f"{source_id}: fetch failed for {obj.object_key}")
            continue

        pub = obj.publication_timestamp or obj.object_key
        extra: dict[str, Any] = {}
        if not obj.publication_timestamp:
            extra["publication_timestamp_derivation"] = "source_token_unverified"
        try:
            record = store.store(
                source_id=source_id,
                collection_date=observed_at[:10],
                object_key=obj.object_key,
                data=data,
                publication_timestamp=pub,
                provenance_url=obj.url,
                observation_utc=observed_at,
                extra=extra or None,
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"store {obj.object_key}: {exc}")
            result.log.append(f"{source_id}: store failed for {obj.object_key}")
            continue

        result.objects.append(record)
        result.log.append(f"{source_id}: {record.status} {obj.object_key} sha256={record.raw_sha256}")

    if result.errors and (not result.objects):
        result.status = "FAILED"
    elif result.errors and result.objects:
        # Partial: at least some objects captured; record status accordingly.
        result.status = "FAILED" if not result.objects else "PARTIAL"
    return result


def write_evidence(evidence_dir: Path, source_id: str, result: SourceRunResult, run_id: str) -> Path:
    """Persist metadata-only evidence (manifest + sanitized log). No payloads."""
    evidence_dir = evidence_dir / "g0-a1"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "source_id": result.source_id,
        "attempted": result.attempted,
        "status": result.status,
        "observed_at": result.observed_at,
        "object_count": result.object_count,
        "results": result.to_evidence(),
    }
    manifest_path = evidence_dir / f"{run_id}__{source_id}__manifest.yaml"
    log_path = evidence_dir / f"{run_id}__{source_id}__run.log"
    import yaml

    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False)
    with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
        for line in result.log:
            fh.write(f"{line}\n")
    return manifest_path
