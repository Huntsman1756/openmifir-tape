"""G0-A2 ingest idempotence: SECOND_RUN_IDEMPOTENT.

Per docs/gates/G0.md §2 G0-A.7 and issue G0-A2:
- a second ingest of ALREADY-CAPTURED objects MUST produce zero unintended state
  changes;
- this is proven with a before/after state digest and explicit counts, not
  inferred from an ``ALREADY_PRESENT`` status.

The ingest REPLAYS the exact captured bytes read from the gitignored local raw
directory. It does NOT re-fetch from BME/Bloomberg (that would be source
re-observation, which belongs to G0-A1/D1, not to idempotence).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .storage import RawStore

_META_SUFFIX = ".meta.yaml"


def _rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scan(raw_root: Path) -> dict[str, dict[str, str]]:
    """Map every raw+meta file's posix-relative path to its content sha256."""
    state: dict[str, dict[str, str]] = {}
    if not raw_root.is_dir():
        return state
    for p in sorted(raw_root.rglob("*")):
        if not p.is_file():
            continue
        state[_rel(p, raw_root)] = {"sha256": _sha(p.read_bytes())}
    return state


def state_digest(raw_root: Path) -> str:
    """Deterministic digest over the whole raw store (paths + content hashes)."""
    state = _scan(raw_root)
    parts = [f"{rel}\t{info['sha256']}" for rel, info in sorted(state.items())]
    return _sha("\n".join(parts).encode("utf-8"))


def _raw_files(raw_root: Path) -> list[Path]:
    if not raw_root.is_dir():
        return []
    return [p for p in sorted(raw_root.rglob("*")) if p.is_file() and not p.name.endswith(_META_SUFFIX)]


@dataclass
class IdempotenceReport:
    before_digest: str
    after_digest: str
    created_raw_objects: int
    modified_raw_objects: int
    deleted_raw_objects: int
    new_metadata_records: int
    changed_metadata: int
    unexpected_state_diff: bool
    replay_statuses: dict[str, int]

    def to_evidence(self) -> dict[str, Any]:
        return {
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "created_raw_objects": self.created_raw_objects,
            "modified_raw_objects": self.modified_raw_objects,
            "deleted_raw_objects": self.deleted_raw_objects,
            "new_metadata_records": self.new_metadata_records,
            "changed_metadata": self.changed_metadata,
            "unexpected_state_diff": self.unexpected_state_diff,
            "replay_statuses": self.replay_statuses,
        }


def _diff(before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]) -> dict[str, int]:
    before_keys = set(before)
    after_keys = set(after)
    created = len(after_keys - before_keys)
    deleted = len(before_keys - after_keys)
    modified = sum(1 for k in (before_keys & after_keys) if before[k]["sha256"] != after[k]["sha256"])
    return {"created": created, "modified": modified, "deleted": deleted}


def _split(state: dict[str, dict[str, str]]) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    raw = {k: v for k, v in state.items() if not k.endswith(_META_SUFFIX)}
    meta = {k: v for k, v in state.items() if k.endswith(_META_SUFFIX)}
    return raw, meta


def run_idempotence(raw_root: Path, *, now_utc: str | None = None) -> IdempotenceReport:
    """Replay the exact captured objects through the ingest pipeline.

    Returns a report; raises nothing on a PASS. Any unintended change is
    reported in the diff (the caller decides fail-closed).
    """
    before = _scan(raw_root)
    before_digest = _sha("\n".join(f"{rel}\t{v['sha256']}" for rel, v in sorted(before.items())).encode())

    store = RawStore(raw_root)
    statuses: dict[str, int] = {}
    for raw_path in _raw_files(raw_root):
        meta_path = raw_path.parent / (raw_path.name + _META_SUFFIX)
        if not meta_path.is_file():
            continue
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        # Derive the structural identity from the path so replay reproduces the
        # exact store call even for objects captured before collection_date was
        # recorded in the meta. Path layout is
        #   <raw_root>/<source_id>/<collection_date>/<source_object_id>/<capture_version>
        parts = raw_path.relative_to(raw_root).parts
        source_id = meta.get("source_id") or parts[0]
        collection_date = meta.get("collection_date") or (parts[1] if len(parts) > 1 else "")
        source_object_id = meta.get("source_object_id") or (parts[2] if len(parts) > 2 else raw_path.name)
        data = raw_path.read_bytes()
        try:
            rec = store.store(
                source_id=source_id,
                collection_date=collection_date,
                object_key=source_object_id,
                data=data,
                publication_timestamp=meta.get("publication_timestamp", ""),
                provenance_url=meta.get("provenance_url", ""),
                observation_utc=meta.get("observation_utc"),
            )
        except Exception:  # noqa: BLE001 - a replay error is an unintended change
            statuses["error"] = statuses.get("error", 0) + 1
            continue
        statuses[rec.status] = statuses.get(rec.status, 0) + 1

    after = _scan(raw_root)
    after_digest = _sha("\n".join(f"{rel}\t{v['sha256']}" for rel, v in sorted(after.items())).encode())

    before_raw, before_meta = _split(before)
    after_raw, after_meta = _split(after)
    dr = _diff(before_raw, after_raw)
    dm = _diff(before_meta, after_meta)
    unexpected = bool(dr["created"] or dr["modified"] or dr["deleted"]
                      or dm["created"] or dm["modified"] or dm["deleted"]
                      or statuses.get("error"))
    return IdempotenceReport(
        before_digest=before_digest,
        after_digest=after_digest,
        created_raw_objects=dr["created"],
        modified_raw_objects=dr["modified"],
        deleted_raw_objects=dr["deleted"],
        new_metadata_records=dm["created"],
        changed_metadata=dm["modified"],
        unexpected_state_diff=unexpected,
        replay_statuses=statuses,
    )


def write_evidence(evidence_dir: Path, report: IdempotenceReport, run_id: str) -> Path:
    ev = evidence_dir / "g0-a2"
    ev.mkdir(parents=True, exist_ok=True)
    manifest = {"run_id": run_id, "gate": "G0-A2", "result": report.to_evidence()}
    path = ev / f"{run_id}__idempotence__manifest.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omt-g0a2", description="G0-A2 ingest idempotence")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    args = parser.parse_args(argv)

    if not args.raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {args.raw_dir}", file=sys.stderr)
        return 2

    from datetime import datetime, timezone

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = run_idempotence(args.raw_dir)
    path = write_evidence(args.evidence_dir, report, run_id)
    print(f"before_digest={report.before_digest}")
    print(f"after_digest ={report.after_digest}")
    print(f"unexpected_state_diff={report.unexpected_state_diff}")
    print(f"replay_statuses={report.replay_statuses}")
    print(f"evidence={path}")
    return 0 if not report.unexpected_state_diff else 1


if __name__ == "__main__":
    raise SystemExit(main())
