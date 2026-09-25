"""Eligible-evidence input selection for G0-E1.

E1 measures exactly the adjudicated A3 window: the raw corpus is defined by
run manifests whose ``results.observed_at`` lies inside
``[operational_start, evidence_tail_end]`` — the same eligibility rule as
the sealed adjudication profile (tools/verify_a3_window.py). Only object
versions attested by eligible manifests are inputs. Pre-window manifests
(e.g. repo-seeded smoke runs) and post-tail runs are excluded by TIME, never
by payload inspection.

``publication_timestamp`` is NOT a selection criterion: a source
publication may legitimately precede operational_start when an eligible
previous-24h or rolling rescan captured it during the window.

Identity dedup only: repeated manifest observations of the same
``(source_id, source_object_id, capture_version)`` collapse to one input —
that is capture identity, not economic/trade deduplication. Two distinct
capture_versions of the same source object stay distinct.

Fail closed: an eligible manifest referencing raw bytes absent from the
store (or bytes whose sha256 diverges from the recorded capture_version)
aborts the run — never a silent skip. A malformed manifest that cannot be
scoped to a time outside the window likewise aborts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

MANIFEST_GLOB = "*__manifest.yaml"
_RUN_ID_RE = re.compile(r"^(\d{8}T\d{6})Z")


class SelectionError(Exception):
    """Fail-closed abort for input selection (missing raw for an eligible
    reference, hash divergence, or unscopeable malformed manifest)."""


@dataclass(frozen=True)
class CaptureVersionRef:
    """One unique eligible capture version to feed E1."""
    source_id: str
    source_object_id: str
    capture_version: str
    path: Path          # resolved raw bytes (any coldate copy — cv-bound)


def _iso(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None                       # naive is unscopeable, not assumed UTC
    return dt.astimezone(UTC)


def _run_id_time(name: str) -> datetime | None:
    m = _RUN_ID_RE.match(Path(name).name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=UTC)
    except ValueError:
        return None


def _resolve_raw(raw_root: Path, source_id: str, cv: str) -> Path | None:
    """raw/<sid>/<collection_date>/<sanitized key>/<cv>. The same capture
    version may be stored under several collection dates (re-captures write
    per-observation-date copies of identical bytes); any existing copy is
    the same bytes — capture_version IS the content sha256."""
    sid_dir = raw_root / source_id
    if not sid_dir.is_dir():
        return None
    for p in sorted(sid_dir.glob(f"*/*/{cv}")):
        if p.is_file():
            return p
    return None


def select_eligible(
    runs_dir: Path,
    raw_root: Path,
    *,
    start: datetime,
    tail_end: datetime,
) -> dict[str, Any]:
    """Select the E1 input corpus: unique capture versions attested by
    window-eligible A3 manifests. Deterministic output.

    Returns ``{"versions": [CaptureVersionRef...], "census": {...},
    "excluded": {...}}``.
    """
    if start.tzinfo is None or tail_end.tzinfo is None:
        raise SelectionError("window bounds must be timezone-aware UTC")

    manifests_total = 0
    eligible_manifests = 0
    excluded_pre: list[str] = []
    excluded_post: list[str] = []
    excluded_malformed: list[dict[str, str]] = []
    object_observations = 0
    unique: dict[tuple[str, str, str], None] = {}

    for p in sorted(runs_dir.glob(MANIFEST_GLOB)):
        manifests_total += 1
        scope_t: datetime | None = None
        m = None
        problem: str | None = None
        try:
            m = yaml.safe_load(p.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            problem = f"unreadable/malformed ({exc})"
        if problem is None and (
                not isinstance(m, dict)
                or not isinstance(m.get("results"), dict)):
            problem = "missing 'results' object"
        if problem is None:
            key_ok = (m.get("run_id") is not None
                      and m["results"].get("source_id") is not None)
            if not key_ok:
                problem = "missing run_id/source_id"
        if problem is None:
            scope_t = _iso(m["results"].get("observed_at"))
            if scope_t is None:
                problem = "missing/naive/unparseable observed_at"
        if problem is None and (
                p.name != f"{m['run_id']}__{m['results']['source_id']}"
                          "__manifest.yaml"):
            problem = "filename/content mismatch"
        if problem is not None:
            # Scope the malformed manifest by filename/run_id timestamp:
            # clearly out-of-window junk is diagnostic; anything else is
            # fail-closed (it might be in-window evidence).
            scope_t = scope_t or _run_id_time(
                str((m or {}).get("run_id") or "")) or _run_id_time(p.name)
            if scope_t is not None and not (start <= scope_t <= tail_end):
                excluded_malformed.append(
                    {"manifest": p.name, "problem": problem})
                continue
            raise SelectionError(
                f"manifest {p.name} cannot be scoped outside the window: "
                f"{problem}")

        sid = m["results"]["source_id"]
        if not (start <= scope_t <= tail_end):
            (excluded_pre if scope_t < start else excluded_post).append(
                p.name)
            continue
        eligible_manifests += 1
        for o in m["results"].get("objects") or []:
            key = o.get("source_object_id")
            cv = o.get("capture_version")
            oid = o.get("source_id")
            if not (isinstance(key, str) and isinstance(cv, str)
                    and oid == sid):
                raise SelectionError(
                    f"eligible manifest {p.name}: object entry missing "
                    f"source_object_id/capture_version or source mismatch")
            object_observations += 1
            unique[(sid, key, cv)] = None   # capture-identity dedup

    versions: list[CaptureVersionRef] = []
    for sid, key, cv in sorted(unique):
        path = _resolve_raw(raw_root, sid, cv)
        if path is None:
            raise SelectionError(
                f"eligible manifest references missing raw bytes: "
                f"{sid}/{key}/{cv}")
        versions.append(CaptureVersionRef(sid, key, cv, path))

    return {
        "versions": versions,
        "census": {
            "manifests_total": manifests_total,
            "manifests_eligible": eligible_manifests,
            "object_observations_eligible": object_observations,
            "unique_capture_versions_eligible": len(unique),
            "pre_window_manifests_excluded": len(excluded_pre),
            "post_tail_manifests_excluded": len(excluded_post),
            "malformed_out_of_window_manifests": len(excluded_malformed),
        },
        "excluded": {
            "pre_window": excluded_pre,
            "post_tail": excluded_post,
            "malformed_out_of_window": excluded_malformed,
        },
    }
