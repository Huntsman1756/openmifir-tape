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

Identity binding: a manifest reference resolves ONLY under the physical
namespace of the recorded logical object —
``raw/<sid>/<coldate>/<sanitize_filename(source_object_id)>/<cv>`` — and
the sibling ``<cv>.meta.yaml`` (RawStore write-once provenance) must
independently attest the exact UNSANITIZED identity. ``sanitize_filename``
is not injective; the path check alone is insufficient (G1-A prerequisite,
issue #17).

Fail closed: an eligible manifest referencing raw bytes absent from the
store, resolvable only under a different logical object key, failing meta
reconciliation, or whose bytes diverge from the recorded capture_version
sha256 aborts the run — never a silent skip, never a cross-key fallback.
A malformed manifest that cannot be scoped to a time outside the window
likewise aborts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from collectors.storage import sanitize_filename

MANIFEST_GLOB = "*__manifest.yaml"
META_SUFFIX = ".meta.yaml"
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


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _index_raw(
    raw_root: Path,
) -> tuple[dict[tuple[str, str, str], list[Path]],
           dict[tuple[str, str], list[Path]]]:
    """Single-pass index over raw/<sid>/<collection_date>/<keydir>/<cv>.

    Returns ``(index, cv_index)``:

    - ``index[(sid, sanitized_key_dir, cv)] -> [Path...]`` (sorted) —
      candidate copies for one logical capture identity;
    - ``cv_index[(sid, cv)] -> [Path...]`` — diagnostics only, used to
      distinguish "missing" from "exists only under a different object
      key"; it NEVER satisfies a reference.
    """
    index: dict[tuple[str, str, str], list[Path]] = {}
    cv_index: dict[tuple[str, str], list[Path]] = {}
    for source_id in ("bme_apa", "blb_apae"):
        sid_dir = raw_root / source_id
        if not sid_dir.is_dir():
            continue
        for p in sorted(sid_dir.rglob("*")):
            if p.is_file() and not p.name.endswith(META_SUFFIX) \
                    and _HEX64.match(p.name):
                index.setdefault((source_id, p.parent.name, p.name),
                                 []).append(p)
                cv_index.setdefault((source_id, p.name), []).append(p)
    return index, cv_index


def _meta_check(meta_path: Path, sid: str, key: str, cv: str,
                collection_date: str) -> str | None:
    """Reconcile a candidate's write-once meta against the EXACT logical
    identity attested by the manifest. Returns a rejection reason or None.
    Every contract field must match exactly — no inference, no fuzzy match.
    """
    if not meta_path.is_file():
        return "missing .meta.yaml"
    try:
        meta = yaml.safe_load(meta_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return f"malformed meta ({exc})"
    if not isinstance(meta, dict):
        return "meta is not a mapping"
    for field, expected in (
            ("source_id", sid),
            ("source_object_id", key),       # exact UNSANITIZED identity
            ("capture_version", cv),
            ("raw_sha256", cv),
            ("collection_date", collection_date)):
        if meta.get(field) != expected:
            return f"meta {field}={meta.get(field)!r} != {expected!r}"
    return None


def _resolve_version(
    raw_index: dict[tuple[str, str, str], list[Path]],
    cv_index: dict[tuple[str, str], list[Path]],
    sid: str, key: str, cv: str,
) -> Path:
    """Resolve one eligible (source_id, source_object_id, capture_version)
    to an exact-identity raw path — or fail closed."""
    candidates = raw_index.get((sid, sanitize_filename(key), cv), [])
    valid: list[Path] = []
    first_reject: str | None = None
    for p in candidates:
        reason = _meta_check(p.with_name(p.name + META_SUFFIX),
                             sid, key, cv, p.parent.parent.name)
        if reason is None:
            valid.append(p)
        elif first_reject is None:
            first_reject = reason
    if valid:
        return sorted(valid)[0]
    ident = f"{sid}/{key}/{cv}"
    if candidates:
        raise SelectionError(
            f"{ident}: every candidate failed meta reconciliation "
            f"({first_reject})")
    if cv_index.get((sid, cv)):
        raise SelectionError(
            f"{ident}: capture_version exists only under a different "
            f"source_object_id — refusing cross-key fallback")
    raise SelectionError(
        f"{ident}: eligible manifest references missing raw bytes")


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

    raw_index, cv_index = _index_raw(raw_root)
    versions: list[CaptureVersionRef] = []
    for sid, key, cv in sorted(unique):
        versions.append(CaptureVersionRef(
            sid, key, cv, _resolve_version(raw_index, cv_index,
                                           sid, key, cv)))

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
