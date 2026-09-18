#!/usr/bin/env python3
"""Read-only G0-A3 capture-window adjudication (DRAFT v5 — pre-preregistration).

Adjudicates an A3 evidence bundle against the FROZEN contract
(docs/gates/G0.md §2 G0-A, baseline g0-freeze-v1) as implemented by the
DEPLOYED collector (efd2268). Deployed primitives are vendored in
``tools/a3_efd2268_compat.py`` so this adjudicator cannot drift when main
evolves.

Reads metadata only: the run journal (JSONL), run manifests (YAML), and
raw-store ``.meta.yaml`` sidecars. Payload bytes are opened exclusively to
recompute SHA-256; their content is never parsed. No normalization, no
market metrics, no coverage claims.

Verdicts:

    COMPLETE_SUPPORTED  every frozen criterion is supported by the evidence
    INCOMPLETE          a frozen criterion is demonstrably unmet
    REVIEW_REQUIRED     structural anomalies must be resolved before verdict
    COMPONENT_MODE      axis-level evaluation only; can never assert the gate

Sealed profile (ACTIVE_G0_A3_PROFILE):

    Gate mode (component=False) adjudicates exactly ONE preregistered
    window — profile parameters and descriptor fingerprints are checked
    before evaluation and any divergence is a structural anomaly, so
    COMPLETE_SUPPORTED is unreachable by bypassing preregistered
    decisions with different CLI parameters. The source descriptors are
    pinned by exact-byte SHA-256 at efd2268: the adjudicator cannot
    silently inherit a modified acquisition-endpoint policy.

Time model (all bounds preregistered, no post-hoc choice):

    operational_start     first declared invocation instant
    subject_period_end    end of the last candidate trading day
    evidence_tail_end     preregistered recovery tail. Chosen to include
                          the post-midnight polls, the scheduled 06:15Z
                          rolling rescan, and a following poll / small
                          observation margin. Evidence inside it may
                          only (a) observe/recover objects whose source
                          day belongs to a counted day, (b) resolve
                          faults originating in the subject period,
                          (c) demonstrate the final rescan/recovery
                          state. It can never create a sixth counted
                          day or repair unrelated faults.

Design rules (frozen-spec-subordinate):

- Polling bound is ``<= 1 hour`` (G0-A.5). Inter-invocation intervals >60min
  between ``mode=poll`` attempts over [start, tail_end] are scheduler-gap
  candidates; EXPLAINED only by operator explanations carrying a verifiable
  evidence artifact (path + sha256 inside the bundle). Rolling attempts live
  on a separate axis and cannot split a poll interval. Nominal cadence is
  diagnostic only.
- ``run_id`` (created at invocation) is the scheduler-attempt timestamp.
- A source FAILED/PARTIAL/NOT_RUN row is NOT a scheduler gap: the scheduler
  ran; acquisition failed. It feeds the coverage/recovery axes instead.
- SKIPPED_LOCKED is a scheduler attempt that ran no sources (own schema,
  no manifests expected); it counts on the axis of its recorded mode.
- Evidence is scoped: only journal rows and manifests inside
  [start, tail_end] are eligible. Pre-window smoke and post-tail runs can
  neither create coverage nor repair faults. Structural checks follow
  the same policy: problems on clearly-timestamped out-of-window records
  are diagnostics; problems inside the window — or on records with no
  parseable time at all — remain anomalies (fail closed).
- Exactly five distinct counted trading days are required for the gate
  verdict. Day-1 fallback is deterministic over the preregistered
  candidate set: if the setup day is demonstrated complete it is counted
  and the last candidate drops out; otherwise the first five non-setup
  candidates are counted.
- Manifests prove observation: every selected object appears in the run
  manifest with that run's observation_utc, including ALREADY_PRESENT.
  ``.meta.yaml`` is write-once metadata of the FIRST capture of that
  version at that collection_date and may legitimately predate the window
  (pre-operational smoke captures). Rules: ``meta.observation_utc`` must
  be <= the earliest known observation of that stored version at that
  collection_date; a meta claiming first write AFTER a manifest-recorded
  observation is a structural anomaly. Identity/integrity fields
  (source_id, source_object_id, capture_version, filename,
  collection_date, raw_sha256, size_bytes, write_once) reconcile against
  every observation; observational fields (publication_timestamp,
  provenance_url) are first-write records reconciled only against a
  manifest observation made at meta.observation_utc — later
  ALREADY_PRESENT observations may legitimately differ.
- BAPA-POST2 filename tokens have unverified semantics. They are treated
  with the deployed conservative margin (UNVERIFIED_TS_MARGIN = 6h): a
  demonstrated reach provably covers only [oldest+margin, newest-margin].
  Token-cadence gaps are anomaly detectors only; they never mark a day
  uncovered.
- Recovery states (deterministic):
      SCHEDULER_GAP_EXPLAINED / SCHEDULER_GAP_EXPLAINED_UNVERIFIED /
      SCHEDULER_GAP_UNEXPLAINED
      RECOVERED / UNRECOVERED / INDETERMINATE
  A later eligible run can repair coverage; it never erases the recorded
  failure. Known-key fetch/store faults: RECOVERED requires a later
  eligible manifest observation of the exact (source_id, object_key);
  relevant only if the key's day is a counted day.
  Discovery faults carry an at-risk interval:
      bme_apa   — deterministic object set: candidate daily files inside
                  the failed run's discovery lookback. Recovery is
                  temporal: RECOVERED iff every at-risk COUNTED day's
                  file has >=1 eligible observation AFTER the failure
                  instant (a pre-failure snapshot cannot demonstrate
                  post-failure content coverage — the file mutates
                  intraday). All observed only pre-failure =>
                  AT_RISK_OBJECT_ALREADY_CAPTURED_BEFORE_FAILURE
                  (explicit, not recovery); any missing => UNRECOVERED.
      blb_apae  — at-risk interval = (latest previous successful discovery
                  of that source, failure instant], lower-bounded by the
                  declared operational start (and the failed run's own
                  window_cutoff when no earlier success exists). RECOVERED
                  iff a later successful rescan's demonstrated reach
                  conservatively covers the whole at-risk interval under
                  the unverified-token margin; else INDETERMINATE.
- Rolling operation is per source: at least one effective rolling rescan
  (SUCCEEDED, or PARTIAL whose faults all recovered) is required within
  the window. Failed invocations are historical incidents; when their
  at-risk intervals are demonstrably recovered the mechanism is treated
  as operating (ROLLING_INCIDENT_RECOVERED). Day-level continuity is
  diagnostic only.
- Provenance policy is per-source and restricted to the descriptor fields
  the deployed adapters actually use for acquisition (entrypoint
  listing_url / public_data_page / base_url). Legal or informational URLs
  are never payload allowlists.
- All timestamps must be explicit UTC ISO-8601; naive timestamps are
  anomalies, not assumptions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlparse

import yaml

from tools.a3_efd2268_compat import (
    META_SUFFIX,
    UNVERIFIED_TS_MARGIN,
    parse_publication_timestamp,
    raw_meta_path,
    raw_object_path,
    sanitize_filename,
)

POLL_BOUND = timedelta(hours=1)                  # frozen G0-A.5: poll <= 1h
EXPECTED_POLL_INTERVAL = timedelta(minutes=30)   # diagnostic only, never gate
BLB_GAP_FACTOR = 4                               # anomaly detector only
BLB_GAP_MIN = timedelta(hours=1)
REQUIRED_COUNTED_DAYS = 5                        # frozen G0-A.8

RUN_ID_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BME_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-bmea-posttrade\.json$")
BLB_TOKEN_RE = re.compile(r"^BAPA-POST2-(\d{8})-")
FETCH_ERR_RE = re.compile(r"^fetch (\S+):")
STORE_ERR_RE = re.compile(r"^store (\S+):")
DISCOVER_ERR_RE = re.compile(r"^discover")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVIDENCE_REF_RE = re.compile(r"^[^/\\]+$")       # plain bundle filename

VALID_MODES = {"poll", "rolling"}
RUN_STATUSES = {"SUCCEEDED", "PARTIAL", "FAILED", "NOT_RUN"}

# Descriptor fields the DEPLOYED adapters actually use for acquisition
# (bme: entrypoint.listing_url / entrypoint.base_url fallback;
#  blb: entrypoint.public_data_page / entrypoint.base_url).
# Legal/informational fields (source_url, info_page, ...) are never
# payload-provenance allowlists.
_ACQ_ENTRYPOINT_KEYS = ("listing_url", "public_data_page", "base_url")

# ---------------------------------------------------------------
# SEALED adjudication profile — the preregistered active G0-A3 window.
# Gate mode (component=False) adjudicates THIS profile only: any CLI
# divergence is a structural anomaly and COMPLETE_SUPPORTED is
# unreachable. Component mode remains parameterized for axis testing.
#
# evidence_tail_end rationale: the tail must include the post-midnight
# polls, the scheduled 06:15Z rolling rescan, and a following poll /
# small observation margin — NOT merely "first poll + margin".
#
# descriptor_sha256: exact-byte fingerprints of the source descriptors
# at the DEPLOYED revision efd2268. The adjudicator claims to evaluate
# efd2268; a modified descriptor changes which acquisition hosts are
# acceptable, so a mismatch is REVIEW_REQUIRED, never silent use.
ACTIVE_G0_A3_PROFILE = {
    "profile_id": "g0-a3-es-corporate-bonds-2026-09",
    "operational_start": "2026-09-16T09:42:46+00:00",
    "subject_period_end": "2026-09-23T23:59:59+00:00",
    "evidence_tail_end": "2026-09-24T07:00:00+00:00",
    "candidate_days": ["2026-09-16", "2026-09-17", "2026-09-18",
                       "2026-09-21", "2026-09-22", "2026-09-23"],
    "setup_day": "2026-09-16",
    "descriptor_sha256": {
        "bme_apa.yaml":
            "c1f9a355bdc0f1f8e196af2d18c932f3c37b1a713a1e5484d9b574b0d0606e56",
        "bloomberg_apae.yaml":
            "b02049d01c83d3532c8e2e23a385452a5558b541d16b6f9b23c5eb27070322fd",
    },
}
PROFILE_SHA256 = hashlib.sha256(
    json.dumps(ACTIVE_G0_A3_PROFILE, sort_keys=True).encode()).hexdigest()


def _iso(s) -> datetime | None:
    """Strict ISO-8601 with explicit timezone; naive input returns None."""
    try:
        dt = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(UTC)


def _day(s) -> date | None:
    if isinstance(s, str) and DAY_RE.match(s):
        return date.fromisoformat(s)
    return None


def _run_id_time(run_id: str) -> datetime | None:
    m = RUN_ID_RE.match(run_id or "")
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(g) for g in m.groups())
    return datetime(y, mo, d, hh, mm, ss, tzinfo=UTC)


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _day_of_key(key: str) -> str | None:
    m = BME_DAY_RE.match(key or "") or BLB_TOKEN_RE.match(key or "")
    if not m:
        return None
    t = m.group(1)
    return t if "-" in t else f"{t[:4]}-{t[4:6]}-{t[6:8]}"


def _acq_hosts(conf: dict) -> set[str]:
    """Payload hosts a source may legitimately use: only descriptor fields
    the deployed adapters consume for acquisition."""
    hosts = set()
    entry = conf.get("entrypoint") or {}
    if isinstance(entry, dict):
        for k in _ACQ_ENTRYPOINT_KEYS:
            v = entry.get(k)
            if isinstance(v, str) and "://" in v:
                h = urlparse(v).hostname
                if h:
                    hosts.add(h)
    return hosts


def _attempt_time(row: dict) -> datetime | None:
    """run_id is the invocation timestamp; observed_at is the fallback."""
    t = _run_id_time(row.get("run_id") or "")
    return t if t is not None else row.get("_t")


def _day_bounds(d: str) -> tuple[datetime, datetime]:
    dd = _day(d)
    start = datetime(dd.year, dd.month, dd.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _scoped(anomalies: list[str], diagnostics: dict, msg: str,
            t: datetime | None, t0: datetime, tail: datetime) -> None:
    """Temporal scoping policy: structural problems inside the eligible
    window are gate-relevant anomalies; clearly timestamped records
    outside it are diagnostics only; records with NO parseable time are
    unscopable and stay anomalies (fail closed)."""
    if t is None or t0 <= t <= tail:
        anomalies.append(msg)
    else:
        diagnostics.setdefault("out_of_window_structural", []).append(msg)


# ------------------------------------------------------------------ loading

def load_journal(path: Path, anomalies: list[str], diagnostics: dict,
                 t0: datetime, tail: datetime) -> list[dict]:
    """Byte-level read: invalid UTF-8 per line is an anomaly, not a crash.
    Rows whose only scoping signal (run_id) is outside the window are
    diagnostics; unscopable rows stay anomalies."""
    rows = []
    try:
        blob = path.read_bytes()
    except OSError as exc:
        anomalies.append(f"journal unreadable: {exc}")
        return rows
    for i, bline in enumerate(blob.split(b"\n"), 1):
        if not bline.strip():
            continue
        try:
            line = bline.decode("utf-8")
        except UnicodeDecodeError:
            anomalies.append(f"journal line {i}: invalid UTF-8")
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            anomalies.append(f"journal line {i}: malformed JSON ({exc.msg})")
            continue
        if not isinstance(row, dict):
            anomalies.append(f"journal line {i}: not a JSON object")
            continue
        row["_line"] = i
        row["_t"] = _iso(row.get("observed_at"))
        if row["_t"] is None:
            _scoped(anomalies, diagnostics,
                    f"journal line {i}: missing/naive/unparseable "
                    f"observed_at {row.get('observed_at')!r}",
                    _run_id_time(row.get("run_id") or ""), t0, tail)
        rows.append(row)
    return rows


def load_manifests(runs_dir: Path, anomalies: list[str], diagnostics: dict,
                   t0: datetime, tail: datetime) -> dict[tuple, dict]:
    """Manifests keyed (run_id, source_id); duplicates flagged, not merged.
    Filename must equal '<run_id>__<source_id>__manifest.yaml' of contents.
    Problems are scoped by observed_at, else run_id (content or filename):
    inside the window = anomaly, outside = diagnostic, unscopable =
    anomaly."""
    manifests: dict[tuple, dict] = {}

    def _scope_time(m, fname):
        if isinstance(m, dict):
            t = _iso((m.get("results") or {}).get("observed_at"))
            if t is not None:
                return t
            rid_ = m.get("run_id")
            if rid_:
                return _run_id_time(str(rid_))
        return _run_id_time(str(fname).split("__")[0])

    for p in sorted(runs_dir.glob("*__manifest.yaml")):
        m = None
        try:
            m = yaml.safe_load(p.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            _scoped(anomalies, diagnostics,
                    f"manifest {p.name}: unreadable/malformed ({exc})",
                    _scope_time(None, p.name), t0, tail)
            continue
        if not isinstance(m, dict) or not isinstance(m.get("results"), dict):
            _scoped(anomalies, diagnostics,
                    f"manifest {p.name}: missing 'results' object",
                    _scope_time(m, p.name), t0, tail)
            continue
        key = (m.get("run_id"), m["results"].get("source_id"))
        if None in key:
            _scoped(anomalies, diagnostics,
                    f"manifest {p.name}: missing run_id/source_id",
                    _scope_time(m, p.name), t0, tail)
            continue
        if p.name != f"{key[0]}__{key[1]}__manifest.yaml":
            _scoped(anomalies, diagnostics,
                    f"manifest filename/content mismatch: {p.name}",
                    _scope_time(m, p.name), t0, tail)
            continue
        if _iso(m["results"].get("observed_at")) is None:
            _scoped(anomalies, diagnostics,
                    f"manifest {p.name}: missing/naive/unparseable "
                    f"observed_at", _scope_time(m, p.name), t0, tail)
            continue
        if key in manifests:
            _scoped(anomalies, diagnostics,
                    f"duplicate manifest for {key}: {p.name}",
                    _scope_time(m, p.name), t0, tail)
            continue
        m["_path"] = p.name
        manifests[key] = m
    return manifests


# ------------------------------------------------------- structural checks

def check_journal_shape(rows: list[dict], sources: set[str],
                        eligible: set[int], anomalies: list[str],
                        diagnostics: dict) -> dict:
    """Schema + duplicate detection BEFORE any dict conversion. Problems
    on eligible rows are anomalies; clearly out-of-window rows are
    diagnostics; unscopable rows stay anomalies."""
    run_rows, skipped = [], []

    def _emit(msg, r):
        if _attempt_time(r) is None or id(r) in eligible:
            anomalies.append(msg)
        else:
            diagnostics.setdefault("out_of_window_structural",
                                   []).append(msg)

    for r in rows:
        st = r.get("status")
        mode = r.get("mode")
        if mode is not None and mode not in VALID_MODES:
            _emit(f"journal line {r['_line']}: unknown mode {mode!r}", r)
        if st == "SKIPPED_LOCKED":
            if r.get("source_id"):
                _emit(f"journal line {r['_line']}: SKIPPED_LOCKED carries "
                      f"source_id", r)
            skipped.append(r)
            continue
        if r.get("source_id"):
            for f in ("run_id", "observed_at", "mode", "status"):
                if r.get(f) is None:
                    _emit(f"journal line {r['_line']}: missing field "
                          f"{f!r}", r)
            if st not in RUN_STATUSES:
                _emit(f"journal line {r['_line']}: unknown status {st!r}",
                      r)
            run_rows.append(r)
        else:
            _emit(f"journal line {r['_line']}: row has neither source_id "
                  f"nor SKIPPED_LOCKED status", r)

    counts = Counter((r["run_id"], r["source_id"]) for r in run_rows
                     if r.get("run_id") and r.get("source_id"))
    for (rid_, sid), n in counts.items():
        if n > 1:
            grp = [r for r in run_rows if r.get("run_id") == rid_
                   and r.get("source_id") == sid]
            modes = sorted({r.get("mode") for r in grp})
            msg = (f"duplicate journal rows for ({rid_}, {sid}) "
                   f"modes={modes}: {n}")
            if any(id(r) in eligible or _attempt_time(r) is None
                   for r in grp):
                anomalies.append(msg)
            else:
                diagnostics.setdefault("out_of_window_structural",
                                       []).append(msg)

    skipped_ids = {r.get("run_id") for r in skipped}
    # per-run source census restricted to eligible rows
    by_run: dict[str, set] = defaultdict(set)
    for r in run_rows:
        if id(r) in eligible:
            by_run[r.get("run_id")].add(r["source_id"])
    for rid_, seen in by_run.items():
        missing = sources - seen
        if missing and rid_ not in skipped_ids:
            anomalies.append(f"run {rid_}: missing source rows "
                             f"{sorted(missing)}")
    for s in sorted({r["source_id"] for r in run_rows} - sources):
        grp = [r for r in run_rows if r["source_id"] == s]
        msg = f"journal references unconfigured source {s!r}"
        if any(id(r) in eligible or _attempt_time(r) is None for r in grp):
            anomalies.append(msg)
        else:
            diagnostics.setdefault("out_of_window_structural",
                                   []).append(msg)
    return {"run_rows": run_rows, "skipped": skipped}


def crosscheck(rows: list[dict], manifests: dict[tuple, dict],
               anomalies: list[str]) -> None:
    """1:1 journal<->manifest on ELIGIBLE evidence only."""
    jkeys = {(r.get("run_id"), r.get("source_id")): r
             for r in rows if r.get("source_id")}
    for key in set(jkeys) | set(manifests):
        if key not in jkeys:
            anomalies.append(f"manifest without journal row: {key}")
        elif key not in manifests:
            anomalies.append(f"journal row without manifest: {key}")
        else:
            res = manifests[key]["results"]
            if jkeys[key].get("observed_at") != res.get("observed_at"):
                anomalies.append(
                    f"observed_at mismatch journal vs manifest: {key} "
                    f"({jkeys[key].get('observed_at')!r} != "
                    f"{res.get('observed_at')!r})")
            if jkeys[key].get("status") != res.get("status"):
                anomalies.append(
                    f"status mismatch journal vs manifest: {key} "
                    f"({jkeys[key].get('status')!r} != "
                    f"{res.get('status')!r})")
            if jkeys[key].get("mode") != \
                    (res.get("context") or {}).get("mode"):
                anomalies.append(
                    f"mode mismatch journal vs manifest: {key}")


# ---------------------------------------------------------- explained gaps

def load_explained(path: Path | None) -> tuple[list[dict], list[str]]:
    """Explanations must reference a sealed evidence artifact inside the
    adjudication bundle: a plain filename + verifiable SHA-256. Free text
    alone never marks a gap EXPLAINED."""
    entries, problems = [], []
    if path is None:
        return entries, problems
    try:
        data = yaml.safe_load(path.read_bytes().decode("utf-8")) or []
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return [], [f"explained-gaps file unreadable: {exc}"]
    for i, e in enumerate(data if isinstance(data, list) else [], 1):
        s, t = _iso(e.get("start")), _iso(e.get("end"))
        ok = s is not None and t is not None and s < t
        if not ok:
            problems.append(f"explained-gaps entry {i}: bad start/end")
            continue
        rec = {"start": s, "end": t, "reason": str(e.get("reason", "")),
               "evidenced": False, "evidence": None}
        ref, sha, etype = (e.get("evidence_ref"), e.get("evidence_sha256"),
                           e.get("evidence_type"))
        if not (e.get("reason") and ref and etype):
            entries.append(rec)
            continue
        if not isinstance(ref, str) or not EVIDENCE_REF_RE.match(ref) \
                or ref.startswith("."):
            problems.append(f"explained-gaps entry {i}: evidence_ref "
                            f"{ref!r} is not a bundle filename")
        elif not isinstance(sha, str) or not SHA256_RE.match(sha):
            problems.append(f"explained-gaps entry {i}: bad sha256")
        else:
            f = path.parent / ref
            if not f.is_file():
                problems.append(f"explained-gaps entry {i}: evidence "
                                f"artifact missing: {ref!r}")
            elif _sha256(f) != sha:
                problems.append(f"explained-gaps entry {i}: evidence "
                                f"hash mismatch for {ref!r}")
            else:
                rec["evidenced"] = True
                rec["evidence"] = {"type": str(etype), "ref": ref,
                                   "sha256": sha}
        entries.append(rec)
    return entries, problems


# --------------------------------------------------------- scheduler axes

def poll_axis(rows: list[dict], start: datetime, end: datetime,
              explained: list[dict]) -> dict:
    """>60min intervals between POLL attempts only (G0-A.5). Rolling
    attempts never split a poll interval."""
    times = sorted({_attempt_time(r) for r in rows
                    if r.get("mode") == "poll"} - {None})
    gaps = []
    for t0, t1 in pairwise([start, *times, end]):
        if t1 - t0 <= POLL_BOUND:
            continue
        cover = [e for e in explained if e["start"] <= t0 and e["end"] >= t1]
        state = "SCHEDULER_GAP_UNEXPLAINED"
        reason = evidence = None
        if cover:
            state = ("SCHEDULER_GAP_EXPLAINED" if cover[0]["evidenced"]
                     else "SCHEDULER_GAP_EXPLAINED_UNVERIFIED")
            reason = cover[0]["reason"]
            evidence = cover[0]["evidence"]
        gaps.append({"kind": "poll_interval", "start": t0.isoformat(),
                     "end": t1.isoformat(),
                     "minutes": round((t1 - t0).total_seconds() / 60, 1),
                     "state": state, "reason": reason,
                     "evidence": evidence})
    return {
        "attempts": len(times),
        "expected_attempts_diagnostic": round(
            (end - start).total_seconds()
            / EXPECTED_POLL_INTERVAL.total_seconds(), 1),
        "gaps": gaps,
        "unexplained": [g for g in gaps
                        if g["state"] == "SCHEDULER_GAP_UNEXPLAINED"],
        "unverified": [g for g in gaps
                       if g["state"] == "SCHEDULER_GAP_EXPLAINED_UNVERIFIED"],
    }


def rolling_operation(manifests: dict[tuple, dict], sources: set[str],
                      recovery: list[dict]) -> dict:
    """Per-source rolling-rescan operation.

    ROLLING_EFFECTIVE            >=1 effective run, no failed invocations
    ROLLING_INCIDENT_RECOVERED   failed invocations, all resolved
    ROLLING_INCIDENT_UNRESOLVED  failed invocations with unresolved faults
    ROLLING_NOT_OPERATING        no effective rolling rescan in window

    'Effective' = SUCCEEDED with no recorded errors, or PARTIAL whose
    faults are all RECOVERED. Day-level continuity is diagnostic only.
    """
    faults: dict[tuple, list] = defaultdict(list)
    for r in recovery:
        faults[(r["run_id"], r["source"])].append(r)

    out = {}
    for sid in sorted(sources):
        runs = [(t, m) for (rid, msid), m in manifests.items()
                if msid == sid
                and (m["results"].get("context") or {}).get("mode")
                == "rolling"
                for t in [_iso(m["results"].get("observed_at"))] if t]
        runs.sort(key=lambda x: x[0])
        effective, incidents = [], []
        for _t, m in runs:
            res = m["results"]
            errs = res.get("errors") or []
            if res.get("status") == "SUCCEEDED" and not errs:
                effective.append(m)
            else:
                fr = faults.get((m["run_id"], sid), [])
                if res.get("status") == "PARTIAL" and fr \
                        and all(r["state"] == "RECOVERED" for r in fr):
                    effective.append(m)
                else:
                    incidents.append(m)
        unresolved = [r for m in incidents
                      for r in faults.get((m["run_id"], sid), [])
                      if r["state"] != "RECOVERED"]
        if not effective:
            state = "ROLLING_NOT_OPERATING"
        elif not incidents:
            state = "ROLLING_EFFECTIVE"
        elif unresolved:
            state = "ROLLING_INCIDENT_UNRESOLVED"
        else:
            state = "ROLLING_INCIDENT_RECOVERED"
        out[sid] = {
            "state": state,
            "invocations": len(runs),
            "effective": len(effective),
            "incidents": len(incidents),
            "unresolved_faults": len(unresolved),
            "days_observed_diagnostic": sorted({
                t.date().isoformat() for t, _m in runs}),
        }
    return out


# ----------------------------------------------------------- recovery axis

def manifest_observations(manifests: dict[tuple, dict]) -> dict:
    """(source_id, object_key) -> sorted observation instants."""
    obs: dict[tuple, list] = defaultdict(list)
    for m in manifests.values():
        t = _iso(m["results"].get("observed_at"))
        for o in m["results"].get("objects") or []:
            if o.get("source_object_id") and t:
                obs[(m["results"]["source_id"],
                     o["source_object_id"])].append(t)
    return {k: sorted(v) for k, v in obs.items()}


def _selected_reach(m: dict) -> tuple[datetime | None, datetime | None]:
    ctx = m["results"].get("context") or {}
    return (parse_publication_timestamp(
                ctx.get("oldest_selected_publication_timestamp"))[0],
            parse_publication_timestamp(
                ctx.get("newest_selected_publication_timestamp"))[0])


def _clean_success(m: dict) -> bool:
    """A run may serve as recovery evidence only if it demonstrably
    captured everything it selected: SUCCEEDED, no recorded errors, and
    object_count consistent with selected_count when both are present."""
    res = m["results"]
    if res.get("status") != "SUCCEEDED" or res.get("errors"):
        return False
    sel, cnt = res.get("selected_count"), res.get("object_count")
    return sel is None or cnt is None or sel == cnt


def _reach_covers(m: dict, lo: datetime, hi: datetime) -> bool:
    """Demonstrated reach conservatively covers [lo, hi]: unverified BAPA
    tokens carry UNVERIFIED_TS_MARGIN uncertainty, so the provable reach
    is [oldest+margin, newest-margin]."""
    old, new = _selected_reach(m)
    return (old is not None and new is not None
            and old + UNVERIFIED_TS_MARGIN <= lo
            and new - UNVERIFIED_TS_MARGIN >= hi)


def _interval_hits_counted(lo: datetime, hi: datetime,
                           counted: set[str]) -> bool:
    for d in counted:
        ds, de = _day_bounds(d)
        if lo < de and hi > ds:
            return True
    return False


def recovery_axis(manifests: dict[tuple, dict], relevant_days: set[str],
                  counted: set[str], op_start: datetime,
                  subject_end: datetime) -> list[dict]:
    """Every acquisition fault classified RECOVERED / UNRECOVERED /
    INDETERMINATE using only ELIGIBLE (in-window incl. tail) evidence.
    A later success repairs coverage; it never erases the recorded failure.
    """
    obs = manifest_observations(manifests)
    ok_runs: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    ok_discover: dict[str, list[datetime]] = defaultdict(list)
    for (_rid, sid), m in manifests.items():
        res = m["results"]
        t = _iso(res.get("observed_at"))
        if t is None:
            continue
        if _clean_success(m):
            ok_runs[sid].append((t, m))
        if not any(DISCOVER_ERR_RE.match(e)
                   for e in res.get("errors") or []):
            ok_discover[sid].append(t)
    for v in ok_runs.values():
        v.sort(key=lambda x: x[0])
    for v in ok_discover.values():
        v.sort()

    def _prev_discovery(sid: str, t_fail: datetime) -> datetime | None:
        """Latest instant before the failure at which that source's
        discovery demonstrably ran (any run without a discover error)."""
        prev = [t for t in ok_discover.get(sid, []) if t < t_fail]
        return prev[-1] if prev else None

    out = []
    for (rid, sid), m in sorted(manifests.items(), key=lambda kv: kv[0][0] or ""):
        res = m["results"]
        t_fail = _iso(res.get("observed_at"))
        if t_fail is None:
            continue
        fail_day = t_fail.date().isoformat()
        ctx = res.get("context") or {}
        cutoff = _iso(ctx.get("window_cutoff_utc"))
        lookback_h = float(ctx.get("window_hours") or 24.0)
        for err in res.get("errors") or []:
            fm = FETCH_ERR_RE.match(err) or STORE_ERR_RE.match(err)
            base = {"source": sid, "run_id": rid,
                    "at": res.get("observed_at")}
            if fm:
                key = fm.group(1)
                key_day = _day_of_key(key)
                seen = [t for t in obs.get((sid, key), []) if t > t_fail]
                relevant = (key_day in counted if key_day
                            else t_fail <= subject_end)
                out.append({**base, "kind": "fetch_or_store", "key": key,
                            "affected_day": key_day or fail_day,
                            "state": "RECOVERED" if seen else "UNRECOVERED",
                            "gate_relevant": relevant})
            elif DISCOVER_ERR_RE.match(err):
                if sid == "bme_apa":
                    # source-semantic: BME's expected object set is the
                    # deterministic daily file inside the discovery
                    # lookback. Recovery is TEMPORAL: a post-failure
                    # eligible observation is required — a file captured
                    # only BEFORE the fault proves no loss for that
                    # snapshot but cannot demonstrate post-failure
                    # content coverage (the daily file mutates intraday).
                    back = timedelta(hours=lookback_h)
                    days = {d for d in relevant_days
                            if (t_fail - back).date() <= _day(d)
                            <= t_fail.date()}
                    days_c = sorted(days & counted)
                    missing, pre_only = [], []
                    for d in days_c:
                        ob = obs.get((sid, f"{d}-bmea-posttrade.json"), [])
                        if not ob:
                            missing.append(d)
                        elif not any(t > t_fail for t in ob):
                            pre_only.append(d)
                    if not missing and not pre_only:
                        state = "RECOVERED"
                    elif missing:
                        state = "UNRECOVERED"
                    else:
                        state = ("AT_RISK_OBJECT_ALREADY_CAPTURED_"
                                 "BEFORE_FAILURE")
                    out.append({**base, "kind": "discover",
                                "affected_day": fail_day,
                                "days_at_risk": sorted(days),
                                "days_at_risk_counted": days_c,
                                "days_pre_failure_only": pre_only or None,
                                "state": state,
                                "gate_relevant": bool(days_c)})
                else:
                    # at-risk interval: publications after the latest
                    # previous successful discovery, up to the failure
                    # instant; lower-bounded by op start (and by the run's
                    # own cutoff when no earlier success exists).
                    prev = _prev_discovery(sid, t_fail)
                    lo = max(op_start, prev) if prev else \
                        max(op_start, cutoff) if cutoff else op_start
                    proof = [m2["_path"] for t2, m2 in ok_runs.get(sid, [])
                             if t2 > t_fail
                             and _reach_covers(m2, lo, t_fail)]
                    relevant = _interval_hits_counted(lo, t_fail, counted)
                    out.append({**base, "kind": "discover",
                                "affected_day": fail_day,
                                "at_risk": {"start": lo.isoformat(),
                                            "end": t_fail.isoformat()},
                                "state": "RECOVERED" if proof
                                else "INDETERMINATE",
                                "gate_relevant": relevant,
                                "evidence": proof})
            else:
                out.append({**base, "kind": "other", "error": err,
                            "affected_day": fail_day,
                            "state": "INDETERMINATE",
                            "gate_relevant": t_fail <= subject_end})
    return out


# ----------------------------------------------------------- coverage axis

def coverage_axis(manifests: dict[tuple, dict], raw_root: Path,
                  counted: list[str], recovery: list[dict]) -> dict:
    """Per counted trading day and source: CAPTURED / INDETERMINATE /
    UNRECOVERED, using eligible observations only (in-window incl. tail).

    bme_apa: CAPTURED iff the daily file object appears in >=1 eligible
    manifest (CREATED or ALREADY_PRESENT; multiple capture_versions are
    never required). The file may legitimately be first captured during
    the recovery tail (it is published after day close).
    blb_apae: tokens cannot prove publication completeness; a day degrades
    only on acquisition evidence — an unrecovered fetch/store fault for
    that day's keys (UNRECOVERED) or an indeterminate discover fault /
    zero observed objects on a counted day (INDETERMINATE). Token-cadence
    gaps are reported as diagnostics and never feed the state.
    """
    obs = manifest_observations(manifests)
    faults: dict[tuple, list] = defaultdict(list)
    for r in recovery:
        if r["affected_day"] and r["state"] != "RECOVERED":
            faults[(r["source"], r["affected_day"])].append(r["state"])

    days = {}
    for day in counted:
        per = {}
        key = f"{day}-bmea-posttrade.json"
        seen = obs.get(("bme_apa", key), [])
        bf = faults.get(("bme_apa", day), [])
        if seen:
            versions = {p.name for d in raw_root.glob(
                f"bme_apa/*/{sanitize_filename(key)}")
                if d.is_dir() for p in d.iterdir()
                if not p.name.endswith(META_SUFFIX)}
            per["bme_apa"] = {
                "state": "CAPTURED", "object": key,
                "observations": len(seen),
                "distinct_capture_versions": len(versions),
                "first_observed": seen[0].isoformat(),
                "last_observed": seen[-1].isoformat(),
                "fault_evidence": bf,
            }
        else:
            per["bme_apa"] = {
                "state": "UNRECOVERED" if "UNRECOVERED" in bf
                else "INDETERMINATE",
                "object": key, "observations": 0, "fault_evidence": bf,
            }

        pref = "BAPA-POST2-" + day.replace("-", "")
        toks = sorted(k for (s, k) in obs
                      if s == "blb_apae" and k.startswith(pref))
        times = sorted(t for k in toks
                       if (t := parse_publication_timestamp(
                           k[len("BAPA-POST2-"):].removesuffix(".csv"))[0]))
        deltas = [b - a for a, b in pairwise(times)]
        med = sorted(deltas)[len(deltas) // 2] if deltas else timedelta(0)
        lim = max(BLB_GAP_FACTOR * med, BLB_GAP_MIN) if med else BLB_GAP_MIN
        cand = [{"from": a.isoformat(), "to": b.isoformat(),
                 "minutes": round((b - a).total_seconds() / 60, 1)}
                for a, b in pairwise(times) if b - a > lim]
        bf = faults.get(("blb_apae", day), [])
        state = ("UNRECOVERED" if "UNRECOVERED" in bf
                 else "INDETERMINATE" if (bf or not toks)
                 else "CAPTURED")
        per["blb_apae"] = {
            "state": state, "objects": len(toks),
            "median_interval_min": round(med.total_seconds() / 60, 1),
            "candidate_gaps_diagnostic_only": cand,
            "fault_evidence": bf,
        }
        days[day] = per
    return days


# ------------------------------------------------------- setup-day proof

def setup_day_proof(setup_day: str, start: datetime,
                    manifests: dict[tuple, dict],
                    recovery: list[dict]) -> dict:
    """Deterministic Day-1 policy, with unverified-token uncertainty.

    bme_apa: the daily file object observed in >=1 eligible manifest.
    blb_apae: an eligible rolling run must demonstrate conservative
    coverage of the entire pre-activation interval [00:00Z, activation]:
    mode=rolling AND historical_page_scanned AND clean SUCCEEDED AND
    reach_oldest <= day_start - MARGIN AND reach_newest >= activation +
    MARGIN (the deployed unverified-token uncertainty), with no unresolved
    blb_apae discover fault in the window.

    What this proves: a successful rescan produced selected/captured
    evidence whose observed source-token reach conservatively spans the
    at-risk interval — supporting acquisition recovery under the gate's
    observable evidence model. What it does NOT prove: absolute Bloomberg
    publication completeness for the interval (token semantics remain
    unverified).
    """
    obs = manifest_observations(manifests)
    bme_seen = obs.get(("bme_apa", f"{setup_day}-bmea-posttrade.json"), [])
    day0 = datetime.combine(_day(setup_day), datetime.min.time(), UTC)
    lo = day0 - UNVERIFIED_TS_MARGIN
    hi = start + UNVERIFIED_TS_MARGIN

    # Fault relevance is interval-aware, NOT window-global: the setup
    # proof covers the pre-activation interval [00:00Z, activation], so
    # only faults whose at-risk interval intersects it can block. A
    # discover fault on a later candidate day (e.g. Sep-23) must NOT
    # make Sep-16 fail its proof — that would create a circular
    # dependency (fault on candidate N forces fallback onto day N).
    # Within the eligible-evidence model at-risk lower bounds are
    # clamped to operational_start, so discover faults cannot reach
    # the pre-activation interval; fetch/store faults on setup-day
    # objects still block via affected_day.
    unresolved = []
    for r in recovery:
        if r["source"] != "blb_apae" or r["state"] == "RECOVERED":
            continue
        if r["kind"] == "discover":
            lo2 = _iso((r.get("at_risk") or {}).get("start"))
            hi2 = _iso((r.get("at_risk") or {}).get("end"))
            hits = (lo2 is not None and hi2 is not None
                    and lo2 < start and hi2 > day0)
        else:
            hits = r["affected_day"] == setup_day
        if hits:
            unresolved.append(r)
    proof = []
    for m in manifests.values():
        res = m["results"]
        ctx = res.get("context") or {}
        if res["source_id"] != "blb_apae" or ctx.get("mode") != "rolling" \
                or not ctx.get("historical_page_scanned") \
                or not _clean_success(m):
            continue
        old, new = _selected_reach(m)
        if old is not None and old <= lo \
                and new is not None and new >= hi:
            proof.append({"manifest": m["_path"],
                          "reach_guaranteed": {
                              "start": (old + UNVERIFIED_TS_MARGIN)
                              .isoformat(),
                              "end": (new - UNVERIFIED_TS_MARGIN)
                              .isoformat()}})
    blb_ok = bool(proof) and not unresolved
    complete = bool(bme_seen) and blb_ok
    return {
        "day": setup_day,
        "result": "SETUP_DAY_CAPTURE_COMPLETE" if complete
                  else "NOT_COUNTED_SETUP_DAY",
        "bme_daily_file_observed": bool(bme_seen),
        "required_guaranteed_reach": {"start": day0.isoformat(),
                                      "end": start.isoformat(),
                                      "margin_hours":
                                          UNVERIFIED_TS_MARGIN
                                          .total_seconds() / 3600},
        "blb_reach_proofs": proof,
        "blb_unresolved_faults": len(unresolved),
    }


# ----------------------------------------------------------- raw integrity

def raw_integrity(raw_root: Path, manifests: dict[tuple, dict],
                  all_manifests: dict[tuple, dict],
                  acq_hosts: dict[str, set], anomalies: list[str],
                  diagnostics: dict, start: datetime, end: datetime,
                  obs_recs: dict) -> dict:
    """Per-object integrity for ELIGIBLE manifests, resolved at the exact
    deployed path raw/<sid>/<observed_at[:10]>/<sanitized key>/<cv>.

    Orphan checks are attributed to the window via collection_date /
    meta.observation_utc; pre-existing raw outside the window is
    diagnostic, never a gate failure.

    meta.observation_utc is first-write metadata: it may legitimately
    predate the window (pre-operational captures). Rule: meta.observation_utc
    <= earliest known observation of that stored version at that
    collection_date; a meta claiming first write AFTER a manifest-recorded
    observation is an anomaly.
    """
    checked = failed = 0
    seen_paths = set()
    referenced_paths = set()

    def _meta_observed_in_window(meta: dict | None, coldate: str) -> bool:
        t = _iso((meta or {}).get("observation_utc"))
        if t is not None:
            return start <= t <= end
        d = _day(coldate)
        return bool(d and start.date() <= d <= end.date())

    # all manifests (eligible or not) count as "recording" a raw object
    for (_rid, sid), m in all_manifests.items():
        t = _iso(m["results"].get("observed_at"))
        cd = t.date().isoformat() if t else None
        for o in m["results"].get("objects") or []:
            if o.get("source_object_id") and o.get("capture_version") and cd:
                referenced_paths.add(
                    raw_object_path(raw_root, sid, cd,
                                    o["source_object_id"],
                                    o["capture_version"]))

    # -- manifest-referenced objects: exact path + full reconciliation
    for (rid, sid), m in manifests.items():
        res = m["results"]
        run_obs = res.get("observed_at")
        run_t = _iso(run_obs)
        coldate = run_t.date().isoformat() if run_t else None
        for o in res.get("objects") or []:
            for f in ("source_object_id", "capture_version", "raw_sha256",
                      "filename", "size_bytes", "publication_timestamp",
                      "provenance_url", "observation_utc", "status"):
                if o.get(f) is None:
                    anomalies.append(
                        f"manifest object missing field {f!r}: "
                        f"{rid}/{sid}")
            key = o.get("source_object_id")
            cv = o.get("capture_version")
            if key is None or cv is None or coldate is None:
                failed += 1
                continue
            if o.get("observation_utc") != run_obs:
                anomalies.append(
                    f"object observation_utc != run observed_at: "
                    f"{rid}/{sid}/{key}")
                failed += 1
            f = raw_object_path(raw_root, sid, coldate, key, cv)
            if (sid, coldate, key, cv) in seen_paths:
                continue
            seen_paths.add((sid, coldate, key, cv))
            checked += 1
            if not f.is_file():
                anomalies.append(f"manifest object missing raw: "
                                 f"{rid}/{sid}/{key} @ {f}")
                failed += 1
                continue
            digest = _sha256(f)
            if digest != o.get("raw_sha256") or cv != o.get("raw_sha256") \
                    or f.name != cv:
                anomalies.append(
                    f"sha256/capture_version/filename divergence: "
                    f"{rid}/{sid}/{key}")
                failed += 1
            if f.stat().st_size != o.get("size_bytes"):
                anomalies.append(f"size mismatch {rid}/{sid}/{key}")
                failed += 1
            host = urlparse(o.get("provenance_url") or "").hostname or ""
            if host not in acq_hosts.get(sid, set()):
                anomalies.append(
                    f"provenance host {host!r} outside {sid} acquisition "
                    f"hosts {sorted(acq_hosts.get(sid, set()))}")
                failed += 1
            mp = raw_meta_path(f)
            if not mp.is_file():
                anomalies.append(f"manifest object without meta: "
                                 f"{rid}/{sid}/{key}")
                failed += 1
                continue
            try:
                meta = yaml.safe_load(mp.read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                anomalies.append(f"meta unreadable/corrupt {mp}: {exc}")
                failed += 1
                continue
            if not isinstance(meta, dict):
                anomalies.append(f"meta not a mapping: {mp}")
                failed += 1
                continue
            # Immutable identity/integrity fields reconcile against EVERY
            # observation (same bytes => same identity, always).
            expected = {"source_id": sid, "source_object_id": key,
                        "capture_version": cv, "filename": f.name,
                        "collection_date": coldate, "raw_sha256": digest,
                        "size_bytes": o.get("size_bytes"),
                        "write_once": True}
            for f2, want in expected.items():
                if meta.get(f2) != want:
                    anomalies.append(
                        f"meta field {f2!r} mismatch {rid}/{sid}/{key}: "
                        f"{meta.get(f2)!r} != {want!r}")
                    failed += 1
            # first-write semantics: meta.observation_utc may legitimately
            # predate the window, but must never be later than the earliest
            # known observation of this version at this collection_date.
            recs = obs_recs.get((sid, key, cv), [])
            known = [t for t, _o in recs
                     if t.date().isoformat() == coldate]
            meta_obs = _iso(meta.get("observation_utc"))
            if meta_obs is None:
                anomalies.append(
                    f"meta observation_utc missing/naive/unparseable: "
                    f"{rid}/{sid}/{key}")
                failed += 1
            elif known and meta_obs > min(known):
                anomalies.append(
                    f"meta observation_utc later than first known "
                    f"observation at {coldate}: {rid}/{sid}/{key}")
                failed += 1
            # Observational fields (publication_timestamp, provenance_url)
            # are first-write records: they reconcile ONLY against the
            # manifest observation made at meta.observation_utc (the
            # first-write record itself). Later ALREADY_PRESENT
            # re-observations may legitimately carry different
            # observational metadata for identical bytes — never compare
            # them blindly.
            fw = [of for t, of in recs
                  if t == meta_obs
                  and t.date().isoformat() == coldate]
            if fw:
                for f2 in ("publication_timestamp", "provenance_url"):
                    if meta.get(f2) != fw[0].get(f2):
                        anomalies.append(
                            f"meta field {f2!r} vs first-write record "
                            f"mismatch {rid}/{sid}/{key}: "
                            f"{meta.get(f2)!r} != {fw[0].get(f2)!r}")
                        failed += 1

    # -- orphans attributed to the window (both directions)
    files = [p for p in raw_root.rglob("*") if p.is_file()]
    for p in files:
        rel = p.relative_to(raw_root)
        if len(rel.parts) != 4:
            anomalies.append(f"non-conforming path in raw store: {p}")
            failed += 1
            continue
        coldate = rel.parts[1]
        is_meta = p.name.endswith(META_SUFFIX)
        mate = (p.with_name(p.name[:-len(META_SUFFIX)]) if is_meta
                else raw_meta_path(p))
        meta = None
        if is_meta:
            try:
                meta = yaml.safe_load(p.read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                meta = {}
        in_window = _meta_observed_in_window(meta, coldate)
        problem = None
        if not mate.is_file():
            problem = ("meta without raw object" if is_meta
                       else "raw object without meta")
        elif not is_meta and p not in referenced_paths:
            if p.name != _sha256(p):
                problem = "raw filename != sha256(bytes)"
            else:
                problem = "raw object never recorded in a manifest"
        if problem:
            if in_window:
                anomalies.append(f"{problem}: {p}")
                failed += 1
            else:
                diagnostics.setdefault("out_of_window_orphans",
                                       []).append(f"{problem}: {p}")
    diagnostics["out_of_window_raw_objects"] = sum(
        1 for p in files if not p.name.endswith(META_SUFFIX)
        and p not in referenced_paths
        and not _meta_observed_in_window(
            None, p.relative_to(raw_root).parts[1]
            if len(p.relative_to(raw_root).parts) == 4 else ""))
    return {"objects_checked": checked, "failures": failed}


# ------------------------------------------------------------------ verdict

def verdict(rep: dict, component: bool) -> str:
    if rep["anomalies"] or rep["scheduler"]["poll"]["unverified"] \
            or rep["integrity"]["failures"]:
        return "REVIEW_REQUIRED"
    if rep["verdict_reasons"]:
        return "INCOMPLETE"
    # component mode evaluates axes only; it can never assert the gate
    return "COMPONENT_MODE" if component else "COMPLETE_SUPPORTED"


def _collect_reasons(rep: dict):
    reasons = list(rep["anomalies"])
    for g in rep["scheduler"]["poll"]["gaps"]:
        if g["state"] == "SCHEDULER_GAP_UNEXPLAINED":
            reasons.append(f"unexplained poll gap {g['start']}..{g['end']} "
                           f"({g['minutes']}min)")
        elif g["state"] == "SCHEDULER_GAP_EXPLAINED_UNVERIFIED":
            reasons.append(f"gap explanation lacks evidence reference: "
                           f"{g['start']}..{g['end']}")
    for sid, r in rep["rolling"].items():
        if r["state"] == "ROLLING_NOT_OPERATING":
            reasons.append(f"rolling rescan never demonstrated operating "
                           f"for {sid}")
        elif r["state"] == "ROLLING_INCIDENT_UNRESOLVED":
            reasons.append(f"rolling incident(s) unresolved for {sid}")
    for r in rep["recovery"]:
        if r.get("gate_relevant") and r["state"] != "RECOVERED":
            reasons.append(f"{r['state']} {r['kind']} fault: "
                           f"{r['source']} run {r['run_id']}")
    for day, per in rep["coverage"].items():
        for s, c in per.items():
            if c["state"] != "CAPTURED":
                reasons.append(f"{day}/{s}: {c['state']}")
    if rep["integrity"]["failures"]:
        reasons.append(f"{rep['integrity']['failures']} integrity failures")
    return reasons


# -------------------------------------------------------------------- main

def verify(journal: Path, runs: Path, raw: Path, config: Path,
           start: str, end: str, candidate_days: list[str],
           setup_day: str | None = None,
           explained_gaps: Path | None = None,
           tail_end: str | None = None,
           component: bool = False) -> dict:
    t0, t1 = _iso(start), _iso(end)
    if not t0 or not t1 or t1 <= t0:
        raise ValueError("invalid --start/--end (must be explicit UTC "
                         "ISO-8601, start < end)")
    tail = _iso(tail_end) if tail_end else t1
    if tail is None or tail < t1:
        raise ValueError("invalid --tail-end (must be explicit UTC "
                         "ISO-8601, >= --end)")

    anomalies: list[str] = []
    diagnostics: dict = {}

    # ---- sealed profile: gate mode adjudicates ONE preregistered window
    profile_anoms: list[str] = []
    if not component:
        prof = ACTIVE_G0_A3_PROFILE
        if t0 != _iso(prof["operational_start"]):
            profile_anoms.append(
                f"profile: operational_start != preregistered "
                f"{prof['operational_start']}")
        if t1 != _iso(prof["subject_period_end"]):
            profile_anoms.append(
                f"profile: subject_period_end != preregistered "
                f"{prof['subject_period_end']}")
        if tail_end is None:
            profile_anoms.append(
                "profile: evidence_tail_end is mandatory for the active "
                "G0-A3 adjudication and was omitted")
        elif tail != _iso(prof["evidence_tail_end"]):
            profile_anoms.append(
                f"profile: evidence_tail_end != preregistered "
                f"{prof['evidence_tail_end']}")
        if setup_day is None:
            profile_anoms.append(
                "profile: setup_day is mandatory for the active G0-A3 "
                "adjudication (six candidates, deterministic fallback)")
        elif setup_day != prof["setup_day"]:
            profile_anoms.append(
                f"profile: setup_day {setup_day!r} != preregistered "
                f"{prof['setup_day']!r}")
        if sorted(candidate_days) != prof["candidate_days"]:
            profile_anoms.append(
                f"profile: candidate_days != preregistered "
                f"{prof['candidate_days']}")
        anomalies.extend(profile_anoms)

    sources, acq_hosts = set(), {}
    for p in sorted(config.glob("*.yaml")):
        conf = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if conf.get("source_id"):
            sources.add(conf["source_id"])
            acq_hosts[conf["source_id"]] = _acq_hosts(conf)
    if not sources:
        raise ValueError(f"no source descriptors under {config}")
    for sid, hosts in acq_hosts.items():
        if not hosts:
            anomalies.append(f"descriptor {sid}: no acquisition endpoint "
                             f"hosts derivable")

    # ---- descriptor fingerprints: the adjudicator claims to evaluate
    # efd2268; a modified descriptor silently changes which acquisition
    # hosts are acceptable. Exact-byte match is the frozen authority.
    desc_fp = {}
    expected_desc = ACTIVE_G0_A3_PROFILE["descriptor_sha256"]
    for name, want in sorted(expected_desc.items()):
        p = config / name
        got = _sha256(p) if p.is_file() else None
        desc_fp[name] = {"expected_sha256": want, "observed_sha256": got,
                         "match": got == want}
        if got != want:
            anomalies.append(
                f"descriptor fingerprint mismatch {name}: observed "
                f"{got or 'MISSING'} != efd2268 {want}")
    extra_desc = sorted(p.name for p in config.glob("*.yaml")
                        if p.name not in expected_desc)
    if extra_desc:
        anomalies.append(
            f"unexpected source descriptors not in efd2268 profile: "
            f"{extra_desc}")

    explained, exp_problems = load_explained(explained_gaps)
    anomalies.extend(exp_problems)

    rows_all = load_journal(journal, anomalies, diagnostics, t0, tail)
    mans_all = load_manifests(runs, anomalies, diagnostics, t0, tail)

    # evidence scoping: only evidence inside [start, tail_end] is eligible
    def _eligible_row(r):
        t = _attempt_time(r)
        return t is not None and t0 <= t <= tail

    rows = [r for r in rows_all if _eligible_row(r)]
    eligible_ids = {id(r) for r in rows}
    mans = {k: m for k, m in mans_all.items()
            if (t := _iso(m["results"].get("observed_at")))
            and t0 <= t <= tail}

    shape = check_journal_shape(rows_all, sources, eligible_ids,
                                anomalies, diagnostics)
    crosscheck(rows, mans, anomalies)

    sched = {"poll": poll_axis(rows, t0, tail, explained)}

    # ---- deterministic counted-day derivation (candidate period only)
    cands, bad = [], []
    for d in candidate_days:
        (cands.append if _day(d) else bad.append)(d)
    if bad:
        anomalies.append(f"unparseable candidate days: {bad}")
    if len(set(cands)) != len(cands):
        anomalies.append("duplicate candidate days")
    cands = sorted(set(cands))

    setup_rep = None
    if setup_day and setup_day not in cands:
        anomalies.append(f"setup day {setup_day} not in candidates")
    if component:
        counted = sorted(set(cands))
        fallback = False
    elif setup_day and setup_day in cands:
        # computed after recovery below; provisional counted set needed
        # for gate relevance — derive pessimistically first
        counted = cands[:REQUIRED_COUNTED_DAYS]
        fallback = False
    else:
        if len(cands) != REQUIRED_COUNTED_DAYS:
            anomalies.append(
                "candidate set must contain exactly "
                f"{REQUIRED_COUNTED_DAYS} days when no setup day is "
                f"declared (got {len(cands)})")
        counted = cands[:REQUIRED_COUNTED_DAYS]
        fallback = False

    # provisional counted set for gate relevance (counted ⊂ cands either way)
    provisional = set(counted) if len(counted) == REQUIRED_COUNTED_DAYS \
        else set(cands)
    recovery = recovery_axis(mans, set(cands), provisional, t0, t1)

    if setup_day and setup_day in cands:
        setup_rep = setup_day_proof(setup_day, t0, mans, recovery)
        if not component:
            if setup_rep["result"] == "SETUP_DAY_CAPTURE_COMPLETE":
                counted = cands[:REQUIRED_COUNTED_DAYS]
                fallback = False
            else:
                counted = [d for d in cands if d != setup_day][
                    :REQUIRED_COUNTED_DAYS]
                fallback = True
            # relevance may change with the final counted set
            for r in recovery:
                r["gate_relevant"] = _fault_relevant(r, set(counted), t1)

    valid_counted = (
        len(counted) == REQUIRED_COUNTED_DAYS
        and len(set(counted)) == REQUIRED_COUNTED_DAYS
        and all(t0.date() <= _day(d) <= t1.date() for d in counted))
    if not component and not valid_counted:
        anomalies.append(
            f"counted_days invalid: {counted} (need exactly "
            f"{REQUIRED_COUNTED_DAYS} distinct days inside the window)")

    rolling = rolling_operation(mans, sources, recovery)

    obs_recs: dict[tuple, list] = defaultdict(list)
    for m in mans_all.values():
        t = _iso(m["results"].get("observed_at"))
        for o in m["results"].get("objects") or []:
            if o.get("source_object_id") and o.get("capture_version") and t:
                obs_recs[(m["results"]["source_id"],
                          o["source_object_id"],
                          o["capture_version"])].append((t, o))

    integrity = raw_integrity(raw, mans, mans_all, acq_hosts, anomalies,
                              diagnostics, t0, tail, obs_recs)
    coverage = coverage_axis(mans, raw, counted, recovery)

    rep = {
        "authority": {"frozen": "g0-freeze-v1", "deployed_sha": "efd2268",
                      "note": "main-line fixes are NOT assumed deployed"},
        "profile": {
            "id": ACTIVE_G0_A3_PROFILE["profile_id"],
            "sha256": PROFILE_SHA256,
            "mode": "component" if component else "gate",
            "parameters_match": (True if component
                                 else not profile_anoms)},
        "descriptor_fingerprints": desc_fp,
        "window": {"operational_start": t0.isoformat(),
                   "subject_period_end": t1.isoformat(),
                   "evidence_tail_end": tail.isoformat(),
                   "candidate_days": list(candidate_days)},
        "evidence": {"journal_rows_total": len(rows_all),
                     "journal_rows_eligible": len(rows),
                     "manifests_total": len(mans_all),
                     "manifests_eligible": len(mans)},
        "skipped_locked": len(shape["skipped"]),
        "expected_acq_hosts": {s: sorted(h) for s, h in acq_hosts.items()},
        "anomalies": anomalies,
        "diagnostics": diagnostics,
        "scheduler": sched,
        "rolling": rolling,
        "recovery": recovery,
        "coverage": coverage,
        "setup_day": setup_rep,
        "counted_days": counted,
        "fallback_applied": fallback,
        "integrity": integrity,
    }
    rep["verdict_reasons"] = _collect_reasons(rep)
    rep["verdict"] = verdict(rep, component)
    return rep


def _fault_relevant(r: dict, counted: set[str],
                    subject_end: datetime) -> bool:
    if r["kind"] == "fetch_or_store":
        return r["affected_day"] in counted
    if r["kind"] == "discover":
        if r["source"] == "bme_apa":
            return bool(set(r.get("days_at_risk") or []) & counted)
        lo = _iso((r.get("at_risk") or {}).get("start"))
        hi = _iso((r.get("at_risk") or {}).get("end"))
        return bool(lo and hi and _interval_hits_counted(lo, hi, counted))
    t = _iso(r.get("at"))
    return t is not None and t <= subject_end


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--runs", type=Path, required=True)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True,
                    help="config/sources dir of the DEPLOYED revision")
    ap.add_argument("--start", required=True,
                    help="operational start, explicit UTC ISO-8601")
    ap.add_argument("--end", required=True,
                    help="subject-period end (end of last candidate day), "
                         "explicit UTC ISO-8601")
    ap.add_argument("--tail-end", default=None,
                    help="evidence/recovery tail end, explicit UTC ISO-8601 "
                         "(>= --end); scheduled operation is expected to "
                         "run through it")
    ap.add_argument("--candidate-days", required=True,
                    help="comma list of preregistered candidate trading days")
    ap.add_argument("--setup-day", default=None,
                    help="optional Day-1 candidate (e.g. 2026-09-16); when "
                         "given, the verifier deterministically selects the "
                         "five counted days")
    ap.add_argument("--explained-gaps", type=Path, default=None,
                    help="YAML [{start,end,reason,evidence_type,"
                         "evidence_ref,evidence_sha256}] — operator "
                         "justifications sealed to bundle artifacts")
    ap.add_argument("--component", action="store_true",
                    help="axis-level evaluation only; can never emit "
                         "COMPLETE_SUPPORTED")
    args = ap.parse_args(argv)
    rep = verify(args.journal, args.runs, args.raw, args.config,
                 args.start, args.end, args.candidate_days.split(","),
                 args.setup_day, args.explained_gaps, args.tail_end,
                 args.component)
    print(yaml.safe_dump(rep, sort_keys=False, allow_unicode=True))
    return 0 if rep["verdict"] == "COMPLETE_SUPPORTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
