"""Synthetic adversarial corpus for tools/verify_a3_window.py.

Every fixture is generated: raw payloads are arbitrary bytes written through
the real RawStore (so the raw/meta layout matches the deployed collector),
and journal/manifests reproduce the deployed schema. No provider data and
no accrued A3 evidence is ever read.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from collectors.storage import RawStore
from tools.verify_a3_window import verify

SOURCES = ("bme_apa", "blb_apae")
HOSTS = {"bme_apa": "www.bolsasymercados.es",
         "blb_apae": "www.bloombergapa.com"}
BME = "bme_apa"
BLB = "blb_apae"


def iso(t: datetime) -> str:
    return t.isoformat()


def rid(t: datetime) -> str:
    return t.strftime("%Y%m%dT%H%M%SZ")


def token(t: datetime, suffix: int = 1) -> str:
    """Bare BAPA timestamp token as stored in publication_timestamp."""
    return f"{t:%Y%m%d}-{t:%H:%M}:{t:%S}.000-{suffix:02d}"


def tok(t: datetime, suffix: int = 1) -> str:
    """Synthetic BAPA-POST2 object key (arbitrary token, not real data)."""
    return f"BAPA-POST2-{token(t, suffix)}.csv"


def bme_key(day: str) -> str:
    return f"{day}-bmea-posttrade.json"


class Corpus:
    """Deterministic synthetic A3 evidence tree under a tmp dir."""

    def __init__(self, root: Path):
        self.root = root
        self.raw = root / "raw"
        self.runs = root / "runs"
        self.cfg = root / "config"
        self.journal = root / "journal.jsonl"
        self.store = RawStore(self.raw)
        self.jrows: list[dict] = []
        self.mans: list[dict] = []
        for d in (self.raw, self.runs, self.cfg):
            d.mkdir(parents=True, exist_ok=True)
        for sid in SOURCES:
            # efd2268-era descriptor: no allowed_hosts; hosts come from URLs
            (self.cfg / f"{sid}.yaml").write_text(yaml.safe_dump(
                {"source_id": sid,
                 "source_url": f"https://{HOSTS[sid]}/post-trade-data.html",
                 "entrypoint": {"listing_url":
                                f"https://{HOSTS[sid]}/listing.json"}}))

    # -- raw objects ----------------------------------------------------
    def capture(self, source, key, t, data=None, pub=None):
        data = data if data is not None else b"synthetic:" + key.encode()
        if pub is None:
            # deployed adapters store the BARE token (no BAPA-POST2- prefix)
            pub = (key[len("BAPA-POST2-"):].removesuffix(".csv")
                   if key.startswith("BAPA-POST2-")
                   else key.split("-bmea-")[0])
        rec = self.store.store(
            source_id=source,
            collection_date=t.date().isoformat(),
            object_key=key,
            data=data,
            publication_timestamp=pub,
            provenance_url=f"https://{HOSTS[source]}/{key}",
            observation_utc=iso(t))
        return rec.to_manifest()

    # -- runs ------------------------------------------------------------
    def run(self, t, mode="poll", objs=(), errs=None, status=None,
            sources=SOURCES, reach=None):
        errs = errs or {}
        for sid in sources:
            objects = [o for o in objs if o.get("source_id") == sid]
            e = list(errs.get(sid, []))
            st = status or ("FAILED" if e and not objects
                            else "PARTIAL" if e else "SUCCEEDED")
            pubs = [o["publication_timestamp"] for o in objects]
            man = {
                "run_id": rid(t), "gate": "G0-A3", "purpose": "synthetic",
                "results": {
                    "source_id": sid, "attempted": True, "status": st,
                    "observed_at": iso(t), "object_count": len(objects),
                    "created_count": sum(o["status"] == "CREATED"
                                         for o in objects),
                    "already_present_count": sum(
                        o["status"] == "ALREADY_PRESENT" for o in objects),
                    "objects": objects, "errors": e, "log": [],
                    "context": {
                        "mode": mode, "window_hours": 24.0,
                        "window_cutoff_utc": iso(t - timedelta(hours=24)),
                        "historical_page_scanned": mode == "rolling",
                        "discovered_count": len(objects),
                        "selected_count": len(objects),
                        "oldest_selected_publication_timestamp":
                            reach[0] if reach else (pubs[0] if pubs else None),
                        "newest_selected_publication_timestamp":
                            reach[1] if reach else (pubs[-1] if pubs else None),
                    },
                },
            }
            self.mans.append(man)
            self.jrows.append({
                "run_id": rid(t), "observed_at": iso(t), "source_id": sid,
                "mode": mode, "window_hours": 24.0, "status": st,
                "discovered": len(objects), "selected": len(objects),
                "created": man["results"]["created_count"],
                "already_present":
                    man["results"]["already_present_count"],
                "errors": e,
            })

    # -- output ----------------------------------------------------------
    def flush(self):
        self.journal.write_text(
            "\n".join(json.dumps(r) for r in self.jrows) + "\n",
            encoding="utf-8")
        for m in self.mans:
            name = f"{m['run_id']}__{m['results']['source_id']}__manifest.yaml"
            (self.runs / name).write_text(yaml.safe_dump(m), encoding="utf-8")

    def verify(self, start, end, days, setup=None, explained=None):
        self.flush()
        ep = None
        if explained is not None:
            ep = self.root / "explained.yaml"
            ep.write_text(yaml.safe_dump(explained), encoding="utf-8")
        return verify(self.journal, self.runs, self.raw, self.cfg,
                      iso(start), iso(end), list(days), setup, ep)


# -------------------------------------------------------- corpus builder

def fill_window(c: Corpus, t0: datetime, t1: datetime, trading_days,
                step=timedelta(minutes=30), blb=True, rolling=True):
    """Fill [t0, t1] with nominal 30-min polls (both sources) and a 06:15
    rolling run per calendar day. BME re-observes its daily file; BLB
    captures one new interval token per poll when blb=True."""
    n = 0
    t = t0
    while t <= t1:
        day = t.date().isoformat()
        objs = []
        if day in trading_days:
            objs.append(c.capture(BME, bme_key(day), t))
        if blb:
            n += 1
            objs.append(c.capture(BLB, tok(t, n % 90 + 1), t))
        c.run(t, objs=objs)
        t += step
    if not rolling:
        return
    day = t0.date()
    while day <= t1.date():
        rt = datetime(day.year, day.month, day.day, 6, 15, tzinfo=UTC)
        if rt >= t0:
            robjs = []
            if day.isoformat() in trading_days:
                robjs.append(c.capture(BME, bme_key(day.isoformat()), rt))
            c.run(rt, mode="rolling", objs=robjs)
        day += timedelta(days=1)


@pytest.fixture
def one_day(tmp_path):
    """One synthetic trading day, fully captured, 00:00-23:30 UTC."""
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill_window(c, t0, t1, ["2026-09-17"])
    return c, t0, t1


# -------------------------------------------------------------- scenarios

def test_clean_window_passes(one_day):
    c, t0, t1 = one_day
    rep = c.verify(t0, t1, ["2026-09-17"])
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]
    assert rep["scheduler"]["unexplained"] == []
    assert rep["coverage"]["2026-09-17"][BME]["state"] == "CAPTURED"
    assert rep["coverage"]["2026-09-17"][BLB]["state"] == "CAPTURED"
    assert rep["integrity"]["failures"] == 0


def test_unexplained_scheduler_gap_fails(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill_window(c, t0, datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
                ["2026-09-17"], rolling=False)
    fill_window(c, datetime(2026, 9, 17, 12, 0, tzinfo=UTC), t1,
                ["2026-09-17"], rolling=False)
    c.run(datetime(2026, 9, 17, 6, 15, tzinfo=UTC), mode="rolling")
    rep = c.verify(t0, t1, ["2026-09-17"])
    assert rep["verdict"] == "INCOMPLETE"
    assert any(g["state"] == "SCHEDULER_GAP_UNEXPLAINED"
               for g in rep["scheduler"]["gaps"])


def test_reboot_explained_gap_recovers(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill_window(c, t0, datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
                ["2026-09-17"], rolling=False)
    fill_window(c, datetime(2026, 9, 17, 12, 0, tzinfo=UTC), t1,
                ["2026-09-17"], rolling=False)
    c.run(datetime(2026, 9, 17, 6, 15, tzinfo=UTC), mode="rolling")
    rep = c.verify(t0, t1, ["2026-09-17"], explained=[{
        "start": "2026-09-17T10:00:00Z", "end": "2026-09-17T12:00:00Z",
        "reason": "reboot per journalctl --list-boots",
    }])
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]
    assert rep["scheduler"]["gaps"][0]["state"] == "SCHEDULER_GAP_EXPLAINED"


def test_skipped_locked_is_attempt_not_gap(one_day):
    c, t0, t1 = one_day
    c.jrows.insert(2, {"run_id": rid(t0 + timedelta(minutes=15)),
                       "observed_at": iso(t0 + timedelta(minutes=15)),
                       "mode": "poll", "status": "SKIPPED_LOCKED",
                       "reason": "run_lock_busy timeout",
                       "lock_wait_seconds": 120})
    rep = c.verify(t0, t1, ["2026-09-17"])
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]
    assert rep["skipped_locked"] == 1


def test_failed_source_run_is_not_scheduler_gap(tmp_path):
    """A FAILED/PARTIAL source row still proves the scheduler attempted the
    run; the failure must surface on the recovery/coverage axis instead."""
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill_window(c, t0, t1, ["2026-09-17"])
    key = "BAPA-POST2-20260917-09:55:00.000-01.csv"
    ft = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    for r in c.jrows:
        if r["run_id"] == rid(ft) and r["source_id"] == BLB:
            r["status"] = "PARTIAL"
            r["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    rep = c.verify(t0, t1, ["2026-09-17"])
    assert rep["scheduler"]["unexplained"] == []
    fault = [r for r in rep["recovery"] if r.get("key") == key]
    assert fault and fault[0]["state"] == "UNRECOVERED"
    assert rep["coverage"]["2026-09-17"][BLB]["state"] == "UNRECOVERED"
    assert rep["verdict"] == "INCOMPLETE"


def test_partial_run_recovered_by_later_manifest(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill_window(c, t0, t1, ["2026-09-17"])
    key = "BAPA-POST2-20260917-09:55:00.000-01.csv"
    ft = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    rt = datetime(2026, 9, 17, 13, 15, tzinfo=UTC)
    obj = c.capture(BLB, key, rt)
    c.run(rt, mode="rolling", objs=[obj],
          reach=[key[len("BAPA-POST2-"):].removesuffix(".csv"),
                 token(rt, 1)])
    rep = c.verify(t0, t1, ["2026-09-17"])
    fault = [r for r in rep["recovery"] if r.get("key") == key]
    assert fault and fault[0]["state"] == "RECOVERED"
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_unrecoverable_discovery_failure_is_indeterminate(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill_window(c, t0, t1, ["2026-09-17"])
    ft = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BLB:
            # discover failed: nothing was ever fetched, so the raw object
            # captured by fill_window for this run must not exist either
            import shutil
            for o in m["results"]["objects"]:
                for d in c.raw.glob(
                        f"blb_apae/*/*/{o['capture_version']}"):
                    shutil.rmtree(d.parent)
            m["results"]["status"] = "FAILED"
            m["results"]["errors"] = ["discover: HTTP 503"]
            m["results"]["objects"] = []
    rep = c.verify(t0, t1, ["2026-09-17"])
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"]
    assert disc and disc[0]["state"] == "INDETERMINATE"
    assert rep["verdict"] == "INCOMPLETE"


def test_manifest_without_journal_detected(one_day):
    c, t0, t1 = one_day
    c.jrows = [r for r in c.jrows if r["run_id"] != rid(t0)]
    rep = c.verify(t0, t1, ["2026-09-17"])
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("manifest without journal row" in a for a in rep["anomalies"])


def test_journal_without_manifest_detected(one_day):
    c, t0, t1 = one_day
    c.mans = [m for m in c.mans if m["run_id"] != rid(t0)]
    rep = c.verify(t0, t1, ["2026-09-17"])
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("journal row without manifest" in a for a in rep["anomalies"])


def test_duplicate_journal_row_detected(one_day):
    c, t0, t1 = one_day
    c.jrows.append(dict(c.jrows[0]))
    rep = c.verify(t0, t1, ["2026-09-17"])
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("duplicate journal rows" in a for a in rep["anomalies"])


def test_malformed_journal_line_detected(one_day):
    c, t0, t1 = one_day
    c.flush()
    with c.journal.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(t0), iso(t1),
                 ["2026-09-17"])
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("malformed JSON" in a for a in rep["anomalies"])


def test_raw_without_meta_detected(one_day):
    c, t0, t1 = one_day
    c.flush()
    d = c.raw / "bme_apa" / "2026-09-17" / "orphan"
    d.mkdir(parents=True)
    (d / "deadbeef").write_bytes(b"x")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(t0), iso(t1),
                 ["2026-09-17"])
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("raw object without meta" in a for a in rep["anomalies"])


def test_meta_without_raw_detected(one_day):
    c, t0, t1 = one_day
    c.flush()
    d = c.raw / "bme_apa" / "2026-09-17" / "orphan"
    d.mkdir(parents=True)
    (d / "deadbeef.meta.yaml").write_text(yaml.safe_dump(
        {"raw_sha256": "0" * 64}))
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(t0), iso(t1),
                 ["2026-09-17"])
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("meta without raw object" in a for a in rep["anomalies"])


def test_hash_mismatch_detected(one_day):
    c, t0, t1 = one_day
    c.flush()
    rawf = next(p for p in c.raw.rglob("*")
                if p.is_file() and not p.name.endswith(".meta.yaml"))
    rawf.write_bytes(b"tampered")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(t0), iso(t1),
                 ["2026-09-17"])
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("sha256 mismatch" in a or "filename != sha256" in a
               for a in rep["anomalies"])


def test_provenance_host_mismatch_detected(one_day):
    c, t0, t1 = one_day
    for m in c.mans:
        hit = [o for o in m["results"]["objects"]
               if o["source_id"] == BLB]
        if hit:
            hit[0]["provenance_url"] = "https://evil.example/x"
            break
    rep = c.verify(t0, t1, ["2026-09-17"])
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("provenance host" in a for a in rep["anomalies"])


def test_bme_repeated_already_present_one_version(one_day):
    c, t0, t1 = one_day
    rep = c.verify(t0, t1, ["2026-09-17"])
    bme = rep["coverage"]["2026-09-17"][BME]
    assert bme["state"] == "CAPTURED"
    assert bme["distinct_capture_versions"] == 1
    assert bme["observations"] > 1    # manifest records every re-observation


def test_bme_content_mutation_two_versions(one_day):
    c, t0, t1 = one_day
    rt = datetime(2026, 9, 17, 20, 15, tzinfo=UTC)
    obj = c.capture(BME, bme_key("2026-09-17"), rt, data=b"mutated bytes")
    c.run(rt, mode="rolling", objs=[obj])   # later run observes the new hash
    rep = c.verify(t0, t1, ["2026-09-17"])
    bme = rep["coverage"]["2026-09-17"][BME]
    assert bme["state"] == "CAPTURED"
    assert bme["distinct_capture_versions"] == 2


def test_blb_irregular_cadence_does_not_autofail(tmp_path):
    """Token-cadence gaps are diagnostics only: with no acquisition fault
    the day stays CAPTURED and the window still passes."""
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill_window(c, t0, t1, ["2026-09-17"], blb=False)
    # sparse irregular publications: two tight clusters 4h35m apart
    for hh, mm in [(0, 5), (0, 15), (0, 25), (5, 0), (5, 10), (5, 20)]:
        tt = datetime(2026, 9, 17, hh, mm, tzinfo=UTC)
        c.mans[1]["results"]["objects"].append(c.capture(BLB, tok(tt, 2), tt))
    rep = c.verify(t0, t1, ["2026-09-17"])
    blb = rep["coverage"]["2026-09-17"][BLB]
    assert blb["state"] == "CAPTURED"
    assert blb["candidate_gaps_diagnostic_only"]       # reported, not fatal
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_blb_candidate_gap_plus_unrecovered_outage_fails(tmp_path):
    """A token gap alone never fails, but a candidate gap plus an
    unrecovered acquisition fault marks the day UNRECOVERED."""
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill_window(c, t0, t1, ["2026-09-17"], blb=False)
    for hh, mm in [(0, 5), (0, 15), (5, 0), (5, 10)]:
        tt = datetime(2026, 9, 17, hh, mm, tzinfo=UTC)
        c.mans[1]["results"]["objects"].append(c.capture(BLB, tok(tt, 2), tt))
    key = "BAPA-POST2-20260917-03:30:00.000-01.csv"   # inside the hole
    for m in c.mans:
        if m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
            break
    rep = c.verify(t0, t1, ["2026-09-17"])
    blb = rep["coverage"]["2026-09-17"][BLB]
    assert blb["state"] == "UNRECOVERED"
    assert rep["verdict"] == "INCOMPLETE"


def test_sep16_complete_backfill_counts(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 16, 9, 42, 46, tzinfo=UTC)
    t1 = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)
    fill_window(c, t0, t1, ["2026-09-16"])
    # next-morning rolling recovers pre-activation interval tokens
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    pre = [c.capture(BLB, tok(datetime(2026, 9, 16, hh, 5, tzinfo=UTC), 3),
                     rt) for hh in (0, 3, 6)]
    c.run(rt, mode="rolling", objs=pre,
          reach=[token(datetime(2026, 9, 16, 0, 5, tzinfo=UTC), 3),
                 token(rt, 1)])
    rep = c.verify(t0, t1, ["2026-09-16"], setup="2026-09-16")
    assert rep["setup_day"]["result"] == "SETUP_DAY_CAPTURE_COMPLETE"
    assert rep["setup_day"]["pre_activation"][
        "recovered_pre_activation_objects"] == 3


def test_sep16_incomplete_backfill_is_setup_day(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 16, 9, 42, 46, tzinfo=UTC)
    t1 = datetime(2026, 9, 16, 23, 30, tzinfo=UTC)
    fill_window(c, t0, t1, ["2026-09-16"])
    # no rolling rescan: pre-activation coverage cannot be demonstrated
    rep = c.verify(t0, t1, ["2026-09-16"], setup="2026-09-16")
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"
    assert rep["verdict"] == "INCOMPLETE"      # setup day was required here


def test_missing_rolling_run_flagged(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 18, 23, 30, tzinfo=UTC)
    fill_window(c, t0, t1, ["2026-09-17", "2026-09-18"])
    c.jrows = [r for r in c.jrows if r.get("mode") != "rolling"
               or r["observed_at"][:10] != "2026-09-18"]
    c.mans = [m for m in c.mans
              if m["results"]["context"]["mode"] != "rolling"
              or m["results"]["observed_at"][:10] != "2026-09-18"]
    rep = c.verify(t0, t1, ["2026-09-17", "2026-09-18"])
    assert any(g["kind"] == "rolling_missing"
               and g["state"] == "SCHEDULER_GAP_UNEXPLAINED"
               for g in rep["scheduler"]["gaps"])
    assert rep["verdict"] == "INCOMPLETE"


def test_clean_five_day_window(tmp_path):
    """Full-size scenario: Sep-16 setup start + 17,18,21,22 + rolling daily."""
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 16, 9, 42, 46, tzinfo=UTC)
    t1 = datetime(2026, 9, 22, 23, 30, tzinfo=UTC)
    days = ["2026-09-16", "2026-09-17", "2026-09-18",
            "2026-09-21", "2026-09-22"]
    fill_window(c, t0, t1, days)
    # the existing 06:15 rolling on 09-17 recovers Sep-16 pre-activation
    # tokens (same run_id as fill_window's rolling — enrich that manifest)
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    blb_roll = next(m for m in c.mans
                    if m["run_id"] == rid(rt)
                    and m["results"]["source_id"] == BLB)
    pre = [c.capture(BLB, tok(datetime(2026, 9, 16, hh, 5, tzinfo=UTC), 3),
                     rt) for hh in (0, 3, 6)]
    blb_roll["results"]["objects"].extend(pre)
    blb_roll["results"]["context"][
        "oldest_selected_publication_timestamp"] = \
        token(datetime(2026, 9, 15, 0, 5, tzinfo=UTC), 3)
    blb_roll["results"]["context"][
        "newest_selected_publication_timestamp"] = token(rt, 1)
    rep = c.verify(t0, t1, days, setup="2026-09-16")
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]
    assert rep["setup_day"]["result"] == "SETUP_DAY_CAPTURE_COMPLETE"
    for d in days:
        assert rep["coverage"][d][BME]["state"] == "CAPTURED"
        assert rep["coverage"][d][BLB]["state"] == "CAPTURED"
