#!/usr/bin/env python3
"""Read-only G0-A3 capture-window adjudication (DRAFT — pre-preregistration).

Evaluates accrued A3 evidence against the FROZEN contract
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

Design rules (frozen-spec-subordinate):

- Polling bound is ``<= 1 hour`` (G0-A.5). An inter-invocation interval
  >60min is a scheduler-gap CANDIDATE; it is EXPLAINED only by evidence the
  operator supplies (systemd/journalctl excerpts passed via
  ``--explained-gaps``), never by later data recovery. Nominal cadence
  (30min timers, ~48 polls/day) is diagnostic only.
- ``run_id`` (created at invocation) is the scheduler-attempt timestamp.
- A source FAILED/PARTIAL/NOT_RUN row is NOT a scheduler gap: the scheduler
  ran; acquisition failed. It feeds the coverage/recovery axes instead.
- SKIPPED_LOCKED is a scheduler attempt that ran no sources (own schema,
  no manifests expected).
- Manifests prove observation: every selected object appears in the run
  manifest with that run's observation_utc, including ALREADY_PRESENT.
  ``.meta.yaml`` is write-once metadata of the FIRST capture of a hash and
  is used only for immutable-byte integrity.
- BAPA-POST2 filename tokens have unverified semantics. Token-cadence gaps
  are anomaly detectors only; they never mark a day uncovered by themselves.
- Recovery states (deterministic):
      SCHEDULER_OK / SCHEDULER_GAP_EXPLAINED / SCHEDULER_GAP_UNEXPLAINED
      SOURCE_RUN_OK / SOURCE_RUN_FAILED / SOURCE_RUN_PARTIAL
      RECOVERED / UNRECOVERED / INDETERMINATE
  A later run can repair coverage but cannot erase the historical fact that
  an earlier run failed. For known-key fetch failures, RECOVERED requires a
  later manifest observation of that exact object key. For discovery
  failures (missed keys unknowable), RECOVERED requires a later successful
  rescan whose selected publication reach overlaps the failed instant;
  otherwise INDETERMINATE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlparse

import yaml

from collectors.a3 import parse_publication_timestamp  # same logic at efd2268
from collectors.storage import sanitize_filename  # deployed layout rule

POLL_BOUND = timedelta(hours=1)                  # frozen G0-A.5: poll <= 1h
EXPECTED_POLL_INTERVAL = timedelta(minutes=30)   # diagnostic only, never gate
BLB_GAP_FACTOR = 4                               # anomaly detector only
BLB_GAP_MIN = timedelta(hours=1)

RUN_ID_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$")
BME_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-bmea-posttrade\.json$")
BLB_TOKEN_RE = re.compile(r"^BAPA-POST2-(\d{8})-")
FETCH_ERR_RE = re.compile(r"^fetch (\S+):")
STORE_ERR_RE = re.compile(r"^store (\S+):")
DISCOVER_ERR_RE = re.compile(r"^discover")

META_SUFFIX = ".meta.yaml"
SOURCE_RUN_STATES = {"SUCCEEDED": "SOURCE_RUN_OK",
                     "PARTIAL": "SOURCE_RUN_PARTIAL",
                     "FAILED": "SOURCE_RUN_FAILED",
                     "NOT_RUN": "SOURCE_RUN_FAILED"}


def _iso(s) -> datetime | None:
    """Parse ISO-8601 ('Z' is native in 3.11+); naive input read as UTC."""
    try:
        dt = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _run_id_time(run_id: str) -> datetime | None:
    m = RUN_ID_RE.match(run_id or "")
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(g) for g in m.groups())
    return datetime(y, mo, d, hh, mm, ss, tzinfo=UTC)


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _descriptor_hosts(conf, _depth=0) -> set[str]:
    """All hostnames appearing in URL-valued fields of a source descriptor."""
    hosts = set()
    if _depth > 4:
        return hosts
    if isinstance(conf, str) and "://" in conf:
        h = urlparse(conf).hostname
        if h:
            hosts.add(h)
    elif isinstance(conf, dict):
        for v in conf.values():
            hosts |= _descriptor_hosts(v, _depth + 1)
    elif isinstance(conf, list):
        for v in conf:
            hosts |= _descriptor_hosts(v, _depth + 1)
    return hosts


def _day_of_key(key: str) -> str | None:
    m = BME_DAY_RE.match(key) or BLB_TOKEN_RE.match(key)
    if not m:
        return None
    tok = m.group(1)
    return tok if "-" in tok else f"{tok[:4]}-{tok[4:6]}-{tok[6:8]}"


# ------------------------------------------------------------------ loading

def load_journal(path: Path, anomalies: list[str]) -> list[dict]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
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
            anomalies.append(f"journal line {i}: unparseable observed_at "
                             f"{row.get('observed_at')!r}")
        rows.append(row)
    return rows


def load_manifests(runs_dir: Path, anomalies: list[str]) -> dict[tuple, dict]:
    """Manifests keyed (run_id, source_id); duplicates flagged, not merged."""
    manifests: dict[tuple, dict] = {}
    for p in sorted(runs_dir.glob("*__manifest.yaml")):
        try:
            m = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            anomalies.append(f"manifest {p.name}: malformed YAML ({exc})")
            continue
        if not isinstance(m, dict) or not isinstance(m.get("results"), dict):
            anomalies.append(f"manifest {p.name}: missing 'results' object")
            continue
        key = (m.get("run_id"), m["results"].get("source_id"))
        if None in key:
            anomalies.append(f"manifest {p.name}: missing run_id/source_id")
            continue
        if key in manifests:
            anomalies.append(f"duplicate manifest for {key}: {p.name}")
            continue
        m["_path"] = p.name
        manifests[key] = m
    return manifests


# ------------------------------------------------------- structural checks

def check_journal_shape(rows: list[dict], sources: set[str],
                        anomalies: list[str]) -> dict:
    """Duplicate detection BEFORE any dict conversion; per-run source census."""
    run_rows = [r for r in rows if r.get("source_id")]
    skipped = [r for r in rows if r.get("status") == "SKIPPED_LOCKED"]

    counts = Counter((r.get("run_id"), r.get("source_id"), r.get("mode"))
                     for r in run_rows)
    for key, n in counts.items():
        if n > 1:
            anomalies.append(f"duplicate journal rows for {key}: {n}")

    skipped_ids = {r.get("run_id") for r in skipped}
    by_run: dict[str, set] = defaultdict(set)
    for r in run_rows:
        by_run[r.get("run_id")].add(r["source_id"])
    for rid, seen in by_run.items():
        missing = sources - seen
        if missing and rid not in skipped_ids:
            anomalies.append(f"run {rid}: missing source rows {sorted(missing)}")

    for s in sorted({r["source_id"] for r in run_rows} - sources):
        anomalies.append(f"journal references unconfigured source {s!r}")

    return {"run_rows": run_rows, "skipped": skipped}


def crosscheck(rows: list[dict], manifests: dict[tuple, dict],
               anomalies: list[str]) -> None:
    jkeys = {(r.get("run_id"), r.get("source_id")): r
             for r in rows if r.get("source_id")}
    for key in set(jkeys) | set(manifests):
        if key not in jkeys:
            anomalies.append(f"manifest without journal row: {key}")
        elif key not in manifests:
            anomalies.append(f"journal row without manifest: {key}")
        elif jkeys[key].get("observed_at") != \
                manifests[key]["results"].get("observed_at"):
            anomalies.append(f"observed_at mismatch journal vs manifest: "
                             f"{key} ({jkeys[key].get('observed_at')!r} != "
                             f"{manifests[key]['results'].get('observed_at')!r})")


# --------------------------------------------------------- scheduler axis

def scheduler_axis(rows: list[dict], start: datetime, end: datetime,
                   explained: list[tuple[datetime, datetime, str]],
                   anomalies: list[str]) -> dict:
    """Invocation attempts keyed by run_id creation time (G0-A.5: <=1h)."""
    attempts: dict[datetime, set] = defaultdict(set)
    for r in rows:
        t = _run_id_time(r.get("run_id") or "")
        if t is None:
            anomalies.append(f"unparseable run_id {r.get('run_id')!r} "
                             f"(journal line {r.get('_line')})")
            t = r.get("_t")
        if t is None or not (start <= t <= end):
            continue
        attempts[t].add(r.get("mode"))

    gaps = []
    times = sorted(attempts)
    edges = [start, *times, end]
    for t0, t1 in pairwise(edges):
        if t1 - t0 <= POLL_BOUND:
            continue
        cover = [e for e in explained if e[0] <= t0 and e[1] >= t1]
        gaps.append({
            "kind": "poll_interval",
            "start": t0.isoformat(), "end": t1.isoformat(),
            "minutes": round((t1 - t0).total_seconds() / 60, 1),
            "state": "SCHEDULER_GAP_EXPLAINED" if cover
                     else "SCHEDULER_GAP_UNEXPLAINED",
            "reason": cover[0][2] if cover else None,
        })

    # G0-A.4: a 7-day rolling rescan must be operating — >=1 rolling attempt
    # per calendar day inside the window (any source row or SKIPPED_LOCKED).
    rolling_days = {r["_t"].date() for r in rows
                    if r.get("mode") == "rolling" and r.get("_t")}
    day = start.date()
    while day <= end.date():
        probe = datetime(day.year, day.month, day.day, 6, 15, tzinfo=UTC)
        if probe < start:
            day += timedelta(days=1)   # slot predates the declared op start
            continue
        if day not in rolling_days:
            cover = [e for e in explained if e[0] <= probe <= e[1]]
            gaps.append({
                "kind": "rolling_missing",
                "start": day.isoformat(), "end": day.isoformat(),
                "minutes": None,
                "state": "SCHEDULER_GAP_EXPLAINED" if cover
                         else "SCHEDULER_GAP_UNEXPLAINED",
                "reason": cover[0][2] if cover else None,
            })
        day += timedelta(days=1)

    elapsed = (end - start).total_seconds()
    return {
        "attempts": len(times),
        "expected_attempts_diagnostic": round(
            elapsed / EXPECTED_POLL_INTERVAL.total_seconds(), 1),
        "gaps": gaps,
        "unexplained": [g for g in gaps
                        if g["state"] == "SCHEDULER_GAP_UNEXPLAINED"],
    }


def source_census(rows: list[dict]) -> dict:
    census: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if not r.get("source_id"):
            continue
        census[r["source_id"]][
            SOURCE_RUN_STATES.get(r.get("status"), r.get("status") or "?")] += 1
    return {s: dict(c) for s, c in census.items()}


# ----------------------------------------------------------- recovery axis

def manifest_observations(manifests: dict[tuple, dict]) -> dict:
    """object_key -> sorted observation instants (manifests are the authority
    for re-observation: .meta.yaml records only the FIRST capture of a hash)."""
    obs: dict[str, list[datetime]] = defaultdict(list)
    for m in manifests.values():
        t = _iso(m["results"].get("observed_at"))
        for o in m["results"].get("objects") or []:
            if o.get("source_object_id") and t:
                obs[o["source_object_id"]].append(t)
    return {k: sorted(v) for k, v in obs.items()}


def _selected_reach(m: dict) -> tuple[datetime | None, datetime | None]:
    ctx = m["results"].get("context") or {}
    old = parse_publication_timestamp(
        ctx.get("oldest_selected_publication_timestamp"))[0]
    new = parse_publication_timestamp(
        ctx.get("newest_selected_publication_timestamp"))[0]
    return old, new


def recovery_axis(manifests: dict[tuple, dict]) -> list[dict]:
    """Every acquisition failure classified RECOVERED / UNRECOVERED /
    INDETERMINATE. A later success repairs coverage; it never erases the
    recorded failure."""
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
        for err in res.get("errors") or []:
            fm = FETCH_ERR_RE.match(err) or STORE_ERR_RE.match(err)
            if fm:
                key = fm.group(1)
                seen = [t for t in obs.get(key, []) if t_fail and t > t_fail]
                out.append({"kind": "fetch_or_store", "source": sid,
                            "run_id": rid, "key": key,
                            "state": "RECOVERED" if seen else "UNRECOVERED"})
            elif DISCOVER_ERR_RE.match(err):
                proof = []
                for t2, m2 in ok_runs.get(sid, []):
                    if t_fail is None or t2 <= t_fail:
                        continue
                    old, new = _selected_reach(m2)
                    if old is not None and old <= t_fail \
                            and (new is None or new >= t_fail):
                        proof.append(m2["_path"])
                out.append({"kind": "discover", "source": sid, "run_id": rid,
                            "at": res.get("observed_at"),
                            "state": "RECOVERED" if proof else "INDETERMINATE",
                            "evidence": proof})
            else:
                out.append({"kind": "other", "source": sid, "run_id": rid,
                            "error": err, "state": "INDETERMINATE"})
    return out


# ----------------------------------------------------------- coverage axis

def coverage_axis(manifests: dict[tuple, dict], raw_root: Path,
                  trading_days: list[str], recovery: list[dict]) -> dict:
    """Per trading day and source: CAPTURED / INDETERMINATE / UNRECOVERED.

    bme_apa: day D is CAPTURED iff its daily-file object appears in >=1
    manifest (CREATED or ALREADY_PRESENT both prove observation; multiple
    capture_versions are NOT required by the frozen spec).
    blb_apae: filename tokens cannot prove publication completeness, so a
    day degrades only on acquisition evidence: an unrecovered fetch/store
    fault for that day's keys (UNRECOVERED) or an indeterminate discover
    fault / zero observed objects on a trading day (INDETERMINATE).
    Token-cadence gaps are reported as diagnostics and never feed the state.
    """
    obs = manifest_observations(manifests)
    faults: dict[tuple, list] = defaultdict(list)
    for r in recovery:
        day = _day_of_key(r["key"]) if r.get("key") else \
            (r.get("at") or "")[:10]
        if day and r["state"] != "RECOVERED":
            faults[(r["source"], day)].append(r["state"])

    days = {}
    for day in trading_days:
        per = {}
        key = f"{day}-bmea-posttrade.json"
        seen = obs.get(key, [])
        bf = faults.get(("bme_apa", day), [])
        if seen:
            versions = {p.name for d in
                        raw_root.glob(f"bme_apa/*/{sanitize_filename(key)}")
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
                "object": key, "observations": 0,
                "fault_evidence": bf,
            }

        pref = "BAPA-POST2-" + day.replace("-", "")
        toks = sorted(k for k in obs if k.startswith(pref))
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


# ----------------------------------------------------------- raw integrity

def raw_integrity(raw_root: Path, manifests: dict[tuple, dict],
                  allowed_hosts: set[str], anomalies: list[str]) -> dict:
    """Two-directional raw<->meta check plus per-manifest-object verification
    against the deployed layout
    raw/<source>/<collection_date>/<sanitized key>/<capture_version>."""
    files = [p for p in raw_root.rglob("*") if p.is_file()]
    raws = {p for p in files if not p.name.endswith(META_SUFFIX)}
    metas = {p for p in files if p.name.endswith(META_SUFFIX)}
    checked = failed = 0

    manifest_digests = {o.get("capture_version") for m in manifests.values()
                        for o in m["results"].get("objects") or []}
    for r in raws:
        if not r.with_name(r.name + META_SUFFIX).is_file():
            anomalies.append(f"raw object without meta: {r}")
            failed += 1
        elif r.name != _sha256(r):
            anomalies.append(f"raw filename != sha256(bytes): {r}")
            failed += 1
        elif r.name not in manifest_digests:
            anomalies.append(f"raw object never recorded in a manifest: {r}")
            failed += 1
    meta_by_raw = {}
    for mp in metas:
        raw = mp.with_name(mp.name[:-len(META_SUFFIX)])
        if not raw.is_file():
            anomalies.append(f"meta without raw object: {mp}")
            failed += 1
            continue
        try:
            meta = yaml.safe_load(mp.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            anomalies.append(f"meta unreadable/corrupt: {mp} ({exc})")
            failed += 1
            continue
        meta_by_raw[raw] = meta if isinstance(meta, dict) else {}

    seen = set()
    for (rid, sid), m in manifests.items():
        for o in m["results"].get("objects") or []:
            key = (rid, sid, o.get("source_object_id"), o.get("capture_version"))
            if key in seen:
                continue
            seen.add(key)
            checked += 1
            cv = o.get("capture_version")
            hits = list(raw_root.glob(
                f"{sid}/*/{sanitize_filename(o.get('source_object_id') or '')}"
                f"/{cv}"))
            if len(hits) != 1:
                anomalies.append(f"manifest object unresolvable in raw store: "
                                 f"{key} ({len(hits)} matches)")
                failed += 1
                continue
            f = hits[0]
            digest = _sha256(f)
            if digest != o.get("raw_sha256"):
                anomalies.append(f"sha256 mismatch {key}: file {digest}")
                failed += 1
            if cv != o.get("raw_sha256") or f.name != cv:
                anomalies.append(f"capture_version/filename/sha divergence: "
                                 f"{key}")
                failed += 1
            if o.get("size_bytes") is not None \
                    and f.stat().st_size != o["size_bytes"]:
                anomalies.append(f"size mismatch {key}")
                failed += 1
            host = urlparse(o.get("provenance_url") or "").hostname or ""
            if host not in allowed_hosts:
                anomalies.append(f"provenance host outside allowed set: "
                                 f"{host!r} ({key})")
                failed += 1
            meta = meta_by_raw.get(f)
            if meta is None:
                anomalies.append(f"manifest object without meta: {key}")
                failed += 1
            elif meta.get("raw_sha256") != o.get("raw_sha256"):
                anomalies.append(f"meta raw_sha256 mismatch: {key}")
                failed += 1
    return {"objects_checked": checked, "failures": failed,
            "raw_files": len(raws), "meta_files": len(metas)}


# ------------------------------------------------------------------ verdict

def verdict(rep: dict) -> tuple[str, list[str]]:
    reasons = []
    if rep["scheduler"]["unexplained"]:
        reasons.append(f"{len(rep['scheduler']['unexplained'])} unexplained "
                       f"scheduler gaps")
    unrec = [r for r in rep["recovery"] if r["state"] == "UNRECOVERED"]
    indet = [r for r in rep["recovery"] if r["state"] == "INDETERMINATE"]
    if unrec:
        reasons.append(f"{len(unrec)} unrecovered acquisition faults")
    if indet:
        reasons.append(f"{len(indet)} indeterminate acquisition faults")
    for day, per in rep["coverage"].items():
        for s, c in per.items():
            if c["state"] != "CAPTURED":
                reasons.append(f"{day}/{s}: {c['state']}")
    if rep["integrity"]["failures"]:
        reasons.append(f"{rep['integrity']['failures']} raw integrity failures")
    if rep.get("setup_day_blocking"):
        reasons.append(rep["setup_day_blocking"])
    if rep["anomalies"]:
        reasons.insert(0, f"{len(rep['anomalies'])} structural anomalies")
        return "REVIEW_REQUIRED", reasons
    if reasons:
        return "INCOMPLETE", reasons
    return "COMPLETE_SUPPORTED", []


def verify(journal: Path, runs: Path, raw: Path, config: Path,
           start: str, end: str, trading_days: list[str],
           setup_day: str | None = None,
           explained_gaps: Path | None = None) -> dict:
    t0, t1 = _iso(start), _iso(end)
    if not t0 or not t1 or t1 <= t0:
        raise ValueError("invalid start/end")

    sources, allowed_hosts = set(), set()
    for p in sorted(config.glob("*.yaml")):
        conf = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if conf.get("source_id"):
            sources.add(conf["source_id"])
            # Deployed descriptors (efd2268) have no allowed_hosts field —
            # that allowlist is a main-line hardening. The expected host set
            # is therefore derived from every URL in the descriptor itself.
            allowed_hosts.update(_descriptor_hosts(conf))
    if not sources:
        raise ValueError(f"no source descriptors under {config}")

    explained = []
    if explained_gaps:
        for e in yaml.safe_load(
                explained_gaps.read_text(encoding="utf-8")) or []:
            s, t = _iso(e.get("start")), _iso(e.get("end"))
            if s and t:
                explained.append((s, t, str(e.get("reason", ""))))

    anomalies: list[str] = []
    rows = load_journal(journal, anomalies)
    manifests = load_manifests(runs, anomalies)
    shape = check_journal_shape(rows, sources, anomalies)
    crosscheck(rows, manifests, anomalies)

    rep = {
        "authority": {"frozen": "g0-freeze-v1", "deployed_sha": "efd2268",
                      "note": "main-line fixes are NOT assumed deployed"},
        "window": {"start": t0.isoformat(), "end": t1.isoformat(),
                   "trading_days": list(trading_days)},
        "journal_rows": len(rows), "manifests": len(manifests),
        "skipped_locked": len(shape["skipped"]),
        "anomalies": anomalies,
        "scheduler": scheduler_axis(rows, t0, t1, explained, anomalies),
        "source_runs": source_census(rows),
    }
    rep["recovery"] = recovery_axis(manifests)
    rep["coverage"] = coverage_axis(manifests, raw, trading_days,
                                  rep["recovery"])
    rep["integrity"] = raw_integrity(raw, manifests, allowed_hosts, anomalies)

    if setup_day:
        sd = coverage_axis(manifests, raw, [setup_day], rep["recovery"])
        # Policy (operator-fixed): the setup day counts only if the
        # pre-activation interval is demonstrably covered — either objects
        # from before activation were actually recovered, or a successful
        # rescan proves the source exposed nothing older than activation.
        obs = manifest_observations(manifests)
        pre_toks = [k for k in obs
                    if k.startswith("BAPA-POST2-" + setup_day.replace("-", ""))
                    and (t := parse_publication_timestamp(
                        k[len("BAPA-POST2-"):].removesuffix(".csv"))[0])
                    and t < t0]
        nothing_older = False
        for (_rid, sid), m in manifests.items():
            ctx = m["results"].get("context") or {}
            if sid != "blb_apae" or ctx.get("mode") != "rolling" \
                    or not ctx.get("historical_page_scanned") \
                    or m["results"].get("status") != "SUCCEEDED":
                continue
            old, _new = _selected_reach(m)
            if old is not None and old >= t0:
                nothing_older = True
        pre_ok = bool(pre_toks) or nothing_older
        complete = all(c["state"] == "CAPTURED"
                       for c in sd[setup_day].values()) and pre_ok
        rep["setup_day"] = {
            "day": setup_day,
            "result": "SETUP_DAY_CAPTURE_COMPLETE" if complete
                      else "NOT_COUNTED_SETUP_DAY",
            "pre_activation": {
                "recovered_pre_activation_objects": len(pre_toks),
                "rescan_proves_nothing_older": nothing_older,
            },
            "detail": sd[setup_day],
        }
        if not complete and setup_day in trading_days:
            rep["setup_day_blocking"] = (
                f"setup day {setup_day} is a required trading day but "
                f"complete capture is undemonstrated")

    rep["verdict"], rep["verdict_reasons"] = verdict(rep)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--runs", type=Path, required=True)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True,
                    help="config/sources dir of the DEPLOYED revision")
    ap.add_argument("--start", required=True,
                    help="declared operational start, ISO-8601 UTC")
    ap.add_argument("--end", required=True,
                    help="window close instant, ISO-8601 UTC")
    ap.add_argument("--trading-days", required=True,
                    help="comma list of preregistered trading days YYYY-MM-DD")
    ap.add_argument("--setup-day", default=None,
                    help="optional candidate day evaluated separately "
                         "(e.g. 2026-09-16 partial start)")
    ap.add_argument("--explained-gaps", type=Path, default=None,
                    help="YAML [{start,end,reason}] justified by operator "
                         "evidence (systemd/journalctl); never auto-derived")
    args = ap.parse_args(argv)
    rep = verify(args.journal, args.runs, args.raw, args.config,
                 args.start, args.end, args.trading_days.split(","),
                 args.setup_day, args.explained_gaps)
    print(yaml.safe_dump(rep, sort_keys=False, allow_unicode=True))
    return 0 if rep["verdict"] == "COMPLETE_SUPPORTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
