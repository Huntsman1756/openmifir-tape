#!/usr/bin/env python3
"""Read-only G0-A3 capture-window adjudication (DRAFT v3 — pre-preregistration).

Adjudicates an A3 evidence bundle against the FROZEN contract
(docs/gates/G0.md §2 G0-A, baseline g0-freeze-v1) as implemented by the
DEPLOYED collector (efd2268). Later fixes on main are NOT assumed to have
applied to the deployed build.

Reads metadata only: the run journal (JSONL), run manifests (YAML), and
raw-store ``.meta.yaml`` sidecars. Payload bytes are opened exclusively to
recompute SHA-256; their content is never parsed. No normalization, no
market metrics, no coverage claims.

Verdicts:

    COMPLETE_SUPPORTED  every frozen criterion is supported by the evidence
    INCOMPLETE          a frozen criterion is demonstrably unmet
    REVIEW_REQUIRED     structural anomalies must be resolved before verdict
    COMPONENT_MODE      axis-level evaluation only; can never assert the gate

Design rules (frozen-spec-subordinate):

- Polling bound is ``<= 1 hour`` (G0-A.5). An inter-invocation interval
  >60min between POLL attempts is a scheduler-gap candidate; EXPLAINED only
  by operator-supplied, evidence-referenced justifications
  (``--explained-gaps``), never by later data recovery. Rolling attempts
  live on a separate axis and cannot split a poll interval. Nominal cadence
  (30min timers, ~48 polls/day) is diagnostic only.
- ``run_id`` (created at invocation) is the scheduler-attempt timestamp.
- A source FAILED/PARTIAL/NOT_RUN row is NOT a scheduler gap: the scheduler
  ran; acquisition failed. It feeds the coverage/recovery axes instead.
- SKIPPED_LOCKED is a scheduler attempt that ran no sources (own schema,
  no manifests expected); it counts on the axis of its recorded mode.
- Evidence is scoped: only observations inside the preregistered
  [start, end] window are eligible for coverage, recovery, and
  crosschecking. Pre-window smoke and post-window runs are ineligible:
  they can neither create coverage nor repair faults.
- Exactly five distinct counted trading days are required for the gate
  verdict. Day-1 fallback is deterministic over the preregistered
  candidate set: if the setup day is demonstrated complete it is counted
  and the last candidate drops out; otherwise the first five non-setup
  candidates are counted. No operator choice after evidence inspection.
- Manifests prove observation: every selected object appears in the run
  manifest with that run's observation_utc, including ALREADY_PRESENT.
  ``.meta.yaml`` is write-once metadata of the FIRST capture at that
  collection_date and is reconciled field-by-field for integrity.
- BAPA-POST2 filename tokens have unverified semantics. Token-cadence
  gaps are anomaly detectors only; they never mark a day uncovered.
- Recovery states (deterministic):
      SCHEDULER_OK / SCHEDULER_GAP_EXPLAINED /
      SCHEDULER_GAP_EXPLAINED_UNVERIFIED / SCHEDULER_GAP_UNEXPLAINED
      SOURCE_RUN_OK / SOURCE_RUN_FAILED / SOURCE_RUN_PARTIAL
      RECOVERED / UNRECOVERED / INDETERMINATE
  A later in-window run can repair coverage but cannot erase the recorded
  failure. Known-key fetch/store faults: RECOVERED requires a later
  eligible manifest observation of the exact (source_id, object_key).
  Discovery faults are source-semantic: for bme_apa the expected object
  set is deterministic (the daily file), so a later observation of the
  day's file proves recovery; for blb_apae, RECOVERED requires a later
  successful rescan whose selected publication reach covers the failure
  instant; otherwise INDETERMINATE.
- Rolling rescan operation is evaluated per source per day: a successful
  (or fully-recovered PARTIAL) rolling run on a counted day is required;
  non-counted-day rolling continuity is diagnostic only.
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

from collectors.a3 import parse_publication_timestamp  # same logic at efd2268
from collectors.storage import sanitize_filename  # deployed layout rule

POLL_BOUND = timedelta(hours=1)                  # frozen G0-A.5: poll <= 1h
EXPECTED_POLL_INTERVAL = timedelta(minutes=30)   # diagnostic only, never gate
ROLLING_SLOT = (6, 15)                           # ops/systemd timer schedule
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

META_SUFFIX = ".meta.yaml"
VALID_MODES = {"poll", "rolling"}
RUN_STATUSES = {"SUCCEEDED", "PARTIAL", "FAILED", "NOT_RUN"}
SOURCE_RUN_STATES = {"SUCCEEDED": "SOURCE_RUN_OK",
                     "PARTIAL": "SOURCE_RUN_PARTIAL",
                     "FAILED": "SOURCE_RUN_FAILED",
                     "NOT_RUN": "SOURCE_RUN_FAILED"}

# Descriptor fields the DEPLOYED adapters actually use for acquisition
# (bme: entrypoint.listing_url / entrypoint.base_url fallback;
#  blb: entrypoint.public_data_page / entrypoint.base_url).
# Legal/informational fields (source_url, info_page, ...) are never
# payload-provenance allowlists.
_ACQ_ENTRYPOINT_KEYS = ("listing_url", "public_data_page", "base_url")


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


# ------------------------------------------------------------------ loading

def load_journal(path: Path, anomalies: list[str]) -> list[dict]:
    """Byte-level read: invalid UTF-8 per line is an anomaly, not a crash."""
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
            anomalies.append(
                f"journal line {i}: missing/naive/unparseable observed_at "
                f"{row.get('observed_at')!r}")
        rows.append(row)
    return rows


def load_manifests(runs_dir: Path, anomalies: list[str]) -> dict[tuple, dict]:
    """Manifests keyed (run_id, source_id); duplicates flagged, not merged.
    Filename must equal '<run_id>__<source_id>__manifest.yaml' of contents."""
    manifests: dict[tuple, dict] = {}
    for p in sorted(runs_dir.glob("*__manifest.yaml")):
        try:
            m = yaml.safe_load(p.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            anomalies.append(f"manifest {p.name}: unreadable/malformed ({exc})")
            continue
        if not isinstance(m, dict) or not isinstance(m.get("results"), dict):
            anomalies.append(f"manifest {p.name}: missing 'results' object")
            continue
        key = (m.get("run_id"), m["results"].get("source_id"))
        if None in key:
            anomalies.append(f"manifest {p.name}: missing run_id/source_id")
            continue
        if p.name != f"{key[0]}__{key[1]}__manifest.yaml":
            anomalies.append(f"manifest filename/content mismatch: {p.name}")
            continue
        if _iso(m["results"].get("observed_at")) is None:
            anomalies.append(
                f"manifest {p.name}: missing/naive/unparseable observed_at")
            continue
        if key in manifests:
            anomalies.append(f"duplicate manifest for {key}: {p.name}")
            continue
        m["_path"] = p.name
        manifests[key] = m
    return manifests


# ------------------------------------------------------- structural checks

def check_journal_shape(rows: list[dict], sources: set[str],
                        eligible: set[int], anomalies: list[str]) -> dict:
    """Schema + duplicate detection BEFORE any dict conversion."""
    run_rows, skipped = [], []
    for r in rows:
        st = r.get("status")
        mode = r.get("mode")
        if mode is not None and mode not in VALID_MODES:
            anomalies.append(f"journal line {r['_line']}: unknown mode "
                             f"{mode!r}")
        if st == "SKIPPED_LOCKED":
            if r.get("source_id"):
                anomalies.append(f"journal line {r['_line']}: "
                                 f"SKIPPED_LOCKED carries source_id")
            skipped.append(r)
            continue
        if r.get("source_id"):
            for f in ("run_id", "observed_at", "mode", "status"):
                if r.get(f) is None:
                    anomalies.append(f"journal line {r['_line']}: "
                                     f"missing field {f!r}")
            if st not in RUN_STATUSES:
                anomalies.append(f"journal line {r['_line']}: unknown "
                                 f"status {st!r}")
            run_rows.append(r)
        else:
            anomalies.append(f"journal line {r['_line']}: row has neither "
                             f"source_id nor SKIPPED_LOCKED status")

    counts = Counter((r["run_id"], r["source_id"]) for r in run_rows
                     if r.get("run_id") and r.get("source_id"))
    for (rid_, sid), n in counts.items():
        if n > 1:
            modes = sorted({r.get("mode") for r in run_rows
                            if r.get("run_id") == rid_
                            and r.get("source_id") == sid})
            anomalies.append(f"duplicate journal rows for "
                             f"({rid_}, {sid}) modes={modes}: {n}")

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
        anomalies.append(f"journal references unconfigured source {s!r}")
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
    """Explanations must reference operational evidence; a bare reason is
    not sufficient to mark a gap EXPLAINED."""
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
        evidenced = bool(e.get("reason")) and bool(e.get("evidence_type")) \
            and bool(e.get("evidence_ref"))
        sha = e.get("evidence_sha256")
        if evidenced and sha is not None:
            if not isinstance(sha, str) or not SHA256_RE.match(sha):
                evidenced = False
                problems.append(f"explained-gaps entry {i}: bad sha256")
            else:
                ref = path.parent / str(e["evidence_ref"])
                if not ref.is_file() or _sha256(ref) != sha:
                    evidenced = False
                    problems.append(
                        f"explained-gaps entry {i}: evidence hash mismatch "
                        f"or missing file {e['evidence_ref']!r}")
        entries.append({"start": s, "end": t,
                        "reason": str(e.get("reason", "")),
                        "evidenced": evidenced})
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
        reason = None
        if cover:
            state = ("SCHEDULER_GAP_EXPLAINED" if cover[0]["evidenced"]
                     else "SCHEDULER_GAP_EXPLAINED_UNVERIFIED")
            reason = cover[0]["reason"]
        gaps.append({"kind": "poll_interval", "start": t0.isoformat(),
                     "end": t1.isoformat(),
                     "minutes": round((t1 - t0).total_seconds() / 60, 1),
                     "state": state, "reason": reason})
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


def rolling_axis(manifests: dict[tuple, dict], sources: set[str],
                 start: datetime, end: datetime,
                 counted: set[str], fault_states: dict) -> dict:
    """Effective rolling operation per source per day: SUCCEEDED (or
    PARTIAL with all faults RECOVERED) within the daily slot. Required on
    counted trading days; diagnostic elsewhere."""
    out = {}
    day = start.date()
    while day <= end.date():
        slot = datetime(day.year, day.month, day.day,
                        *ROLLING_SLOT, tzinfo=UTC)
        if start <= slot <= end:
            per = {}
            for sid in sorted(sources):
                st = "ABSENT"
                for (_rid, msid), m in manifests.items():
                    res = m["results"]
                    if msid != sid or (res.get("context") or {}) \
                            .get("mode") != "rolling":
                        continue
                    t = _iso(res.get("observed_at"))
                    if not t or t.date() != day:
                        continue
                    if res.get("status") == "SUCCEEDED":
                        st = "EFFECTIVE"
                        break
                    if res.get("status") == "PARTIAL" and fault_states.get(
                            m["_path"]) == "ALL_RECOVERED":
                        st = "EFFECTIVE"
                        break
                    st = "INEFFECTIVE"
                per[sid] = st
            per["gate_relevant"] = day.isoformat() in counted
            out[day.isoformat()] = per
        day += timedelta(days=1)
    return out


def _attempt_time(row: dict) -> datetime | None:
    """run_id is the invocation timestamp; observed_at is the fallback."""
    t = _run_id_time(row.get("run_id") or "")
    return t if t is not None else row.get("_t")


# ----------------------------------------------------------- recovery axis

def manifest_observations(manifests: dict[tuple, dict]) -> dict:
    """(source_id, object_key) -> sorted observation instants.
    Manifests are the authority for re-observation: .meta.yaml records only
    the first capture at a collection_date."""
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


def recovery_axis(manifests: dict[tuple, dict],
                  relevant_days: set[str]) -> list[dict]:
    """Every acquisition fault classified RECOVERED / UNRECOVERED /
    INDETERMINATE using only ELIGIBLE (in-window) evidence. A later success
    repairs coverage; it never erases the recorded failure.

    relevant_days: the preregistered candidate set — a discovery failure
    can only have missed objects for days that could actually carry
    gate-relevant publications (a BME daily file for a non-candidate
    weekend does not exist and must not force INDETERMINATE)."""
    obs = manifest_observations(manifests)
    ok_runs: dict[str, list[tuple[datetime, dict]]] = defaultdict(list)
    for (_rid, sid), m in manifests.items():
        t = _iso(m["results"].get("observed_at"))
        if t and m["results"].get("status") == "SUCCEEDED":
            ok_runs[sid].append((t, m))
    for v in ok_runs.values():
        v.sort(key=lambda x: x[0])

    out = []
    for (rid, sid), m in sorted(manifests.items(), key=lambda kv: kv[0][0] or ""):
        res = m["results"]
        t_fail = _iso(res.get("observed_at"))
        fail_day = t_fail.date().isoformat() if t_fail else None
        for err in res.get("errors") or []:
            fm = FETCH_ERR_RE.match(err) or STORE_ERR_RE.match(err)
            base = {"source": sid, "run_id": rid,
                    "at": res.get("observed_at")}
            if fm:
                key = fm.group(1)
                seen = [t for t in obs.get((sid, key), [])
                        if t_fail and t > t_fail]
                out.append({**base, "kind": "fetch_or_store", "key": key,
                            "affected_day": _day_of_key(key) or fail_day,
                            "state": "RECOVERED" if seen else "UNRECOVERED"})
            elif DISCOVER_ERR_RE.match(err):
                if sid == "bme_apa" and fail_day:
                    # source-semantic: BME's expected object set is the
                    # deterministic daily file; later observation of it
                    # proves recovery even though token reach cannot apply.
                    prev = (t_fail - timedelta(days=1)).date().isoformat()
                    days = {fail_day, prev} & relevant_days
                    missing = [d for d in days if not any(
                        t > t_fail for t in obs.get(
                            (sid, f"{d}-bmea-posttrade.json"), []))]
                    out.append({**base, "kind": "discover",
                                "affected_day": fail_day,
                                "days_at_risk": sorted(days),
                                "state": "INDETERMINATE" if missing
                                else "RECOVERED"})
                else:
                    proof = []
                    for t2, m2 in ok_runs.get(sid, []):
                        if t_fail is None or t2 <= t_fail:
                            continue
                        old, new = _selected_reach(m2)
                        if old is not None and old <= t_fail \
                                and (new is None or new >= t_fail):
                            proof.append(m2["_path"])
                    out.append({**base, "kind": "discover",
                                "affected_day": fail_day,
                                "state": "RECOVERED" if proof
                                else "INDETERMINATE",
                                "evidence": proof})
            else:
                out.append({**base, "kind": "other", "error": err,
                            "affected_day": fail_day,
                            "state": "INDETERMINATE"})
    return out


# ----------------------------------------------------------- coverage axis

def coverage_axis(manifests: dict[tuple, dict], raw_root: Path,
                  counted: list[str], recovery: list[dict]) -> dict:
    """Per counted trading day and source: CAPTURED / INDETERMINATE /
    UNRECOVERED, using eligible observations only.

    bme_apa: CAPTURED iff the daily file object appears in >=1 eligible
    manifest (CREATED or ALREADY_PRESENT; multiple capture_versions are
    never required).
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
    """Deterministic Day-1 policy.

    bme_apa: the daily file object observed in >=1 eligible manifest.
    blb_apae: an eligible rolling run must demonstrate historical reach
    covering the entire pre-activation interval [00:00Z, activation]:
    mode=rolling AND historical_page_scanned AND SUCCEEDED AND
    oldest_selected <= day_start AND newest_selected >= activation, with
    no unresolved acquisition fault affecting the setup day.

    What this proves: the rescan demonstrably listed/selected objects
    spanning the whole interval, i.e. whatever the source exposed for it
    was within reach and captured. What it does NOT prove: that the source
    actually published every interval file in that range (BAPA token
    semantics are unverified).
    """
    obs = manifest_observations(manifests)
    bme_seen = obs.get(("bme_apa", f"{setup_day}-bmea-posttrade.json"), [])
    day0 = datetime.combine(_day(setup_day), datetime.min.time(), UTC)

    unresolved = [r for r in recovery
                  if r["source"] == "blb_apae" and r["state"] != "RECOVERED"
                  and (r["kind"] == "discover"
                       or r["affected_day"] == setup_day)]
    proof = []
    for m in manifests.values():
        res = m["results"]
        sid = res["source_id"]
        ctx = res.get("context") or {}
        if sid != "blb_apae" or ctx.get("mode") != "rolling" \
                or not ctx.get("historical_page_scanned") \
                or res.get("status") != "SUCCEEDED":
            continue
        old, new = _selected_reach(m)
        if old is not None and old <= day0 \
                and new is not None and new >= start:
            proof.append({"manifest": m["_path"],
                          "oldest": old.isoformat(),
                          "newest": new.isoformat()})
    blb_ok = bool(proof) and not unresolved
    complete = bool(bme_seen) and blb_ok
    return {
        "day": setup_day,
        "result": "SETUP_DAY_CAPTURE_COMPLETE" if complete
                  else "NOT_COUNTED_SETUP_DAY",
        "bme_daily_file_observed": bool(bme_seen),
        "blb_reach_proofs": proof,
        "blb_unresolved_faults": len(unresolved),
    }


# ----------------------------------------------------------- raw integrity

def raw_integrity(raw_root: Path, manifests: dict[tuple, dict],
                  acq_hosts: dict[str, set], anomalies: list[str],
                  diagnostics: dict, start: datetime, end: datetime,
                  obs_index: dict) -> dict:
    """Per-object integrity for ELIGIBLE manifests, resolved at the exact
    deployed path raw/<sid>/<observed_at[:10]>/<sanitized key>/<cv>.
    Orphan checks are attributed to the window via collection_date /
    meta.observation_utc; pre-existing raw outside the window is
    diagnostic, never a gate failure."""
    checked = failed = 0
    seen_paths = set()

    def _meta_observed_in_window(meta: dict | None, coldate: str) -> bool:
        t = _iso((meta or {}).get("observation_utc"))
        if t is not None:
            return start <= t <= end
        d = _day(coldate)
        return bool(d and start.date() <= d <= end.date())

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
            f = (raw_root / sid / coldate / sanitize_filename(key) / cv)
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
            mp = f.with_name(f.name + META_SUFFIX)
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
            expected = {"source_id": sid, "source_object_id": key,
                        "capture_version": cv, "filename": f.name,
                        "collection_date": coldate, "raw_sha256": digest,
                        "size_bytes": o.get("size_bytes"),
                        "publication_timestamp":
                            o.get("publication_timestamp"),
                        "provenance_url": o.get("provenance_url"),
                        "write_once": True}
            for f2, want in expected.items():
                if meta.get(f2) != want:
                    anomalies.append(
                        f"meta field {f2!r} mismatch {rid}/{sid}/{key}: "
                        f"{meta.get(f2)!r} != {want!r}")
                    failed += 1
            # meta.observation_utc = first capture instant at THIS
            # collection_date (deployed meta is write-once per dir)
            first = [t for t in obs_index.get((sid, key, cv), [])
                     if t.date().isoformat() == coldate]
            meta_obs = _iso(meta.get("observation_utc"))
            if meta_obs is None or not first or meta_obs != min(first):
                anomalies.append(
                    f"meta observation_utc does not match first in-window "
                    f"capture at {coldate}: {rid}/{sid}/{key}")
                failed += 1

    # -- orphans attributed to the window (both directions)
    files = [p for p in raw_root.rglob("*") if p.is_file()]
    manifest_paths = {raw_root / sid / cd / sanitize_filename(k) / cv
                      for (sid, cd, k, cv) in seen_paths}
    for p in files:
        rel = p.relative_to(raw_root)
        if len(rel.parts) != 4:
            anomalies.append(f"non-conforming path in raw store: {p}")
            failed += 1
            continue
        coldate = rel.parts[1]
        is_meta = p.name.endswith(META_SUFFIX)
        mate = (p.with_name(p.name[:-len(META_SUFFIX)]) if is_meta
                else p.with_name(p.name + META_SUFFIX))
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
        elif not is_meta and p not in manifest_paths:
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
        and p not in manifest_paths
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
    for day, per in rep.get("rolling", {}).items():
        if per.get("gate_relevant"):
            for sid, st in per.items():
                if sid != "gate_relevant" and st != "EFFECTIVE":
                    reasons.append(f"rolling {st} for {sid} on counted day "
                                   f"{day}")
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
           component: bool = False) -> dict:
    t0, t1 = _iso(start), _iso(end)
    if not t0 or not t1 or t1 <= t0:
        raise ValueError("invalid --start/--end (must be explicit UTC "
                         "ISO-8601, start < end)")

    sources, acq_hosts = set(), {}
    anomalies: list[str] = []
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

    explained, exp_problems = load_explained(explained_gaps)
    anomalies.extend(exp_problems)

    rows_all = load_journal(journal, anomalies)
    mans_all = load_manifests(runs, anomalies)

    # evidence scoping: only in-window evidence is eligible
    def _eligible_row(r):
        t = _attempt_time(r)
        return t is not None and t0 <= t <= t1

    rows = [r for r in rows_all if _eligible_row(r)]
    eligible_ids = {id(r) for r in rows}
    mans = {k: m for k, m in mans_all.items()
            if (t := _iso(m["results"].get("observed_at")))
            and t0 <= t <= t1}

    shape = check_journal_shape(rows_all, sources, eligible_ids, anomalies)
    crosscheck(rows, mans, anomalies)

    sched = {"poll": poll_axis(rows, t0, t1, explained)}

    # ---- deterministic counted-day derivation
    cands, bad = [], []
    for d in candidate_days:
        (cands.append if _day(d) else bad.append)(d)
    if bad:
        anomalies.append(f"unparseable candidate days: {bad}")
    if len(set(cands)) != len(cands):
        anomalies.append("duplicate candidate days")
    cands = sorted(set(cands))

    recovery = recovery_axis(mans, set(cands))

    setup_rep = None
    if setup_day:
        if setup_day not in cands:
            anomalies.append(f"setup day {setup_day} not in candidates")
        else:
            setup_rep = setup_day_proof(setup_day, t0, mans, recovery)

    if component:
        counted = sorted(set(cands))
        fallback = False
    elif setup_day and setup_rep:
        if setup_rep["result"] == "SETUP_DAY_CAPTURE_COMPLETE":
            counted = cands[:REQUIRED_COUNTED_DAYS]
            fallback = False
        else:
            counted = [d for d in cands if d != setup_day][
                :REQUIRED_COUNTED_DAYS]
            fallback = True
    else:
        if len(cands) != REQUIRED_COUNTED_DAYS:
            anomalies.append(
                "candidate set must contain exactly "
                f"{REQUIRED_COUNTED_DAYS} days when no setup day is "
                f"declared (got {len(cands)})")
        counted = cands[:REQUIRED_COUNTED_DAYS]
        fallback = False

    valid_counted = (
        len(counted) == REQUIRED_COUNTED_DAYS
        and len(set(counted)) == REQUIRED_COUNTED_DAYS
        and all(t0.date() <= _day(d) <= t1.date() for d in counted))
    if not component and not valid_counted:
        anomalies.append(
            f"counted_days invalid: {counted} (need exactly "
            f"{REQUIRED_COUNTED_DAYS} distinct days inside the window)")

    # gate relevance
    counted_set = set(counted)
    for r in recovery:
        r["gate_relevant"] = (r["affected_day"] in counted_set
                              if r["affected_day"] else True)

    # PARTIAL rolling whose faults all recovered -> effective
    fault_states = {}
    for (rid_, sid), m in mans.items():
        errs = m["results"].get("errors") or []
        if not errs:
            continue
        states = [r["state"] for r in recovery
                  if r["run_id"] == rid_ and r["source"] == sid]
        if states and all(s == "RECOVERED" for s in states):
            fault_states[m["_path"]] = "ALL_RECOVERED"
    rolling = rolling_axis(mans, sources, t0, t1, counted_set, fault_states)

    obs_index: dict[tuple, list] = defaultdict(list)
    for m in mans.values():
        t = _iso(m["results"].get("observed_at"))
        for o in m["results"].get("objects") or []:
            if o.get("source_object_id") and o.get("capture_version") and t:
                obs_index[(m["results"]["source_id"],
                           o["source_object_id"],
                           o["capture_version"])].append(t)

    diagnostics: dict = {}
    integrity = raw_integrity(raw, mans, acq_hosts, anomalies, diagnostics,
                              t0, t1, obs_index)
    coverage = coverage_axis(mans, raw, counted, recovery)

    rep = {
        "authority": {"frozen": "g0-freeze-v1", "deployed_sha": "efd2268",
                      "note": "main-line fixes are NOT assumed deployed"},
        "window": {"start": t0.isoformat(), "end": t1.isoformat(),
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--runs", type=Path, required=True)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True,
                    help="config/sources dir of the DEPLOYED revision")
    ap.add_argument("--start", required=True,
                    help="declared operational start, explicit UTC ISO-8601")
    ap.add_argument("--end", required=True,
                    help="window close instant, explicit UTC ISO-8601")
    ap.add_argument("--candidate-days", required=True,
                    help="comma list of preregistered candidate trading days")
    ap.add_argument("--setup-day", default=None,
                    help="optional Day-1 candidate (e.g. 2026-09-16); when "
                         "given, the verifier deterministically selects the "
                         "five counted days")
    ap.add_argument("--explained-gaps", type=Path, default=None,
                    help="YAML [{start,end,reason,evidence_type,"
                         "evidence_ref,evidence_sha256?}] — operator "
                         "justifications backed by collected evidence")
    ap.add_argument("--component", action="store_true",
                    help="axis-level evaluation only; can never emit "
                         "COMPLETE_SUPPORTED")
    args = ap.parse_args(argv)
    rep = verify(args.journal, args.runs, args.raw, args.config,
                 args.start, args.end, args.candidate_days.split(","),
                 args.setup_day, args.explained_gaps, args.component)
    print(yaml.safe_dump(rep, sort_keys=False, allow_unicode=True))
    return 0 if rep["verdict"] == "COMPLETE_SUPPORTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
