"""Synthetic adversarial corpus for tools/verify_a3_window.py (v3).

Every fixture is generated: raw payloads are arbitrary bytes written through
the real RawStore (so the raw/meta layout matches the deployed collector),
and journal/manifests reproduce the deployed efd2268 schema. No provider
data and no accrued A3 evidence is ever read.

Gate-level tests use the preregistered six-candidate set
{16,17,18,21,22,23} with deterministic Day-1 fallback. Single-day and
axis-level tests run with component=True, a mode that can never emit
COMPLETE_SUPPORTED.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from collectors.storage import RawStore, sanitize_filename
from tools.verify_a3_window import verify

SOURCES = ("bme_apa", "blb_apae")
HOSTS = {"bme_apa": "www.bolsasymercados.es",
         "blb_apae": "www.bloombergapa.com"}
BME = "bme_apa"
BLB = "blb_apae"

CANDIDATES = ["2026-09-16", "2026-09-17", "2026-09-18",
              "2026-09-21", "2026-09-22", "2026-09-23"]
SETUP = "2026-09-16"
W0 = datetime(2026, 9, 16, 9, 42, 46, tzinfo=UTC)   # declared op start
W1 = datetime(2026, 9, 23, 23, 59, 59, tzinfo=UTC)  # window close


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


def evidence_ref_file(root: Path, name="journalctl-excerpt.log",
                      data=b"synthetic systemd excerpt"):
    """Write a synthetic operator-evidence artifact; return (name, sha256)."""
    import hashlib
    (root / name).write_bytes(data)
    return name, hashlib.sha256(data).hexdigest()


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
            # efd2268-era descriptor: no allowed_hosts; acquisition hosts
            # come only from entrypoint fields the deployed adapters read.
            (self.cfg / f"{sid}.yaml").write_text(yaml.safe_dump(
                {"source_id": sid,
                 "source_url": f"https://{HOSTS[sid]}/post-trade-data.html",
                 "info_page": "https://www.bloombergapa.net/info"
                 if sid == BLB else f"https://{HOSTS[sid]}/about",
                 "entrypoint": {
                     "listing_url" if sid == BME else "public_data_page":
                     f"https://{HOSTS[sid]}/listing.json",
                     "base_url": f"https://{HOSTS[sid]}/"}}))

    # -- raw objects -----------------------------------------------------
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

    # -- runs -------------------------------------------------------------
    def run(self, t, mode="poll", objs=(), errs=None, status=None,
            sources=SOURCES, reach=None, observed_at=None):
        errs = errs or {}
        obs = iso(observed_at or t)
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
                    "observed_at": obs, "object_count": len(objects),
                    "created_count": sum(o["status"] == "CREATED"
                                         for o in objects),
                    "already_present_count": sum(
                        o["status"] == "ALREADY_PRESENT" for o in objects),
                    "objects": objects, "errors": e, "log": [],
                    "context": {
                        "mode": mode, "window_hours": 24.0,
                        "window_cutoff_utc": iso(
                            (observed_at or t) - timedelta(hours=24)),
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
                "run_id": rid(t), "observed_at": obs, "source_id": sid,
                "mode": mode, "window_hours": 24.0, "status": st,
                "discovered": len(objects), "selected": len(objects),
                "created": man["results"]["created_count"],
                "already_present":
                    man["results"]["already_present_count"],
                "errors": e,
            })

    def skipped(self, t, mode="poll"):
        self.jrows.append({
            "run_id": rid(t), "observed_at": iso(t), "mode": mode,
            "status": "SKIPPED_LOCKED",
            "reason": "run_lock_busy timeout", "lock_wait_seconds": 120,
        })

    # -- output -----------------------------------------------------------
    def flush(self):
        self.journal.write_text(
            "\n".join(json.dumps(r) for r in self.jrows) + "\n",
            encoding="utf-8")
        for m in self.mans:
            name = f"{m['run_id']}__{m['results']['source_id']}__manifest.yaml"
            (self.runs / name).write_text(yaml.safe_dump(m), encoding="utf-8")

    def drop_raw(self, source: str, cv: str) -> None:
        """Remove a captured object dir (simulates discover never fetched it)."""
        import shutil
        for f in self.raw.glob(f"{source}/*/*/{cv}"):
            shutil.rmtree(f.parent)

    def verify(self, start=W0, end=W1, candidates=CANDIDATES,
               setup=SETUP, explained=None, component=False):
        self.flush()
        ep = None
        if explained is not None:
            ep = self.root / "explained.yaml"
            ep.write_text(yaml.safe_dump(explained), encoding="utf-8")
        return verify(self.journal, self.runs, self.raw, self.cfg,
                      iso(start), iso(end), list(candidates),
                      setup, ep, component=component)


# --------------------------------------------------------- corpus builder

def fill(c: Corpus, t0: datetime, t1: datetime, trading_days,
         step=timedelta(minutes=30), blb=True, rolling=True):
    """Nominal 30-min polls over [t0,t1]; 06:15 rolling per day in span."""
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
        if t0 <= rt <= t1:
            robjs = []
            if day.isoformat() in trading_days:
                robjs.append(c.capture(BME, bme_key(day.isoformat()), rt))
            c.run(rt, mode="rolling", objs=robjs)
        day += timedelta(days=1)


def make_setup_proof(c: Corpus, t0=W0):
    """Give Sep-16 a demonstrable pre-activation recovery: the 2026-09-17
    06:15 historical rolling whose selected reach spans [00:00, activation].
    """
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    man = next((m for m in c.mans
                if m["run_id"] == rid(rt)
                and m["results"]["source_id"] == BLB), None)
    pre = [c.capture(BLB, tok(datetime(2026, 9, 16, hh, 5, tzinfo=UTC), 3), rt)
           for hh in (0, 3, 6, 9)]
    reach = [token(datetime(2026, 9, 15, 23, 55, tzinfo=UTC), 1),
             token(datetime(2026, 9, 17, 0, 5, tzinfo=UTC), 1)]
    if man is None:
        c.run(rt, mode="rolling", objs=pre, reach=reach)
    else:
        man["results"]["objects"].extend(pre)
        man["results"]["context"][
            "oldest_selected_publication_timestamp"] = reach[0]
        man["results"]["context"][
            "newest_selected_publication_timestamp"] = reach[1]


@pytest.fixture
def window(tmp_path):
    """Full six-candidate window, all days covered, Sep-16 setup proven."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    return c


# ================================================================ gate level

def test_gate_complete_supported_clean(window):
    rep = window.verify()
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]
    assert rep["counted_days"] == ["2026-09-16", "2026-09-17", "2026-09-18",
                                 "2026-09-21", "2026-09-22"]
    assert rep["fallback_applied"] is False
    assert rep["setup_day"]["result"] == "SETUP_DAY_CAPTURE_COMPLETE"


def test_gate_fallback_when_setup_undemonstrated(window):
    """Sep-16 without a historical-reach proof falls back to 17..23 and the
    window can still be COMPLETE."""
    c = window
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(rt) and m["results"]["source_id"] == BLB:
            m["results"]["context"]["oldest_selected_publication_timestamp"] = \
                token(datetime(2026, 9, 17, 0, 0, tzinfo=UTC), 1)  # too late
    rep = c.verify()
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"
    assert rep["counted_days"] == CANDIDATES[1:]
    assert rep["fallback_applied"] is True
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_gate_rejects_single_day(tmp_path):
    """One clean trading day must NEVER emit COMPLETE_SUPPORTED (frozen G0-A.8
    requires five). Regression: v2 returned COMPLETE_SUPPORTED here."""
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill(c, t0, t1, ["2026-09-17"])
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None)
    assert rep["verdict"] != "COMPLETE_SUPPORTED"


def test_gate_rejects_four_candidates(window):
    rep = window.verify(candidates=CANDIDATES[1:5], setup=None)
    assert rep["verdict"] != "COMPLETE_SUPPORTED"
    assert any("counted" in r for r in rep["verdict_reasons"])


def test_gate_rejects_duplicate_candidate(window):
    rep = window.verify(
        candidates=[*CANDIDATES[:4], CANDIDATES[3], *CANDIDATES[5:]],
        setup=SETUP)
    assert rep["verdict"] != "COMPLETE_SUPPORTED"


def test_pre_window_manifest_cannot_create_coverage(tmp_path):
    """Metamorphic: a pre-start manifest observing the day file must NOT
    make a missing day pass."""
    c = Corpus(tmp_path)
    # a pre-start smoke run captures the 09-18 file before the window opens
    smoke = datetime(2026, 9, 15, 4, 44, 54, tzinfo=UTC)
    obj = c.capture(BME, bme_key("2026-09-18"), smoke)
    c.run(smoke, objs=[obj])
    # in-window coverage deliberately lacks the 09-18 file
    fill(c, W0, W1, [d for d in CANDIDATES if d != "2026-09-18"])
    make_setup_proof(c)
    rep = c.verify()
    assert rep["coverage"]["2026-09-18"][BME]["state"] != "CAPTURED"
    assert rep["verdict"] != "COMPLETE_SUPPORTED"


def test_post_window_manifest_cannot_recover_fault(tmp_path):
    """Metamorphic: a post-close observation must not flip UNRECOVERED."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    key = tok(datetime(2026, 9, 22, 3, 30, tzinfo=UTC), 7)
    hit = None
    for m in c.mans:
        if m["results"]["source_id"] == BLB \
                and m["results"]["observed_at"][:10] == "2026-09-22" \
                and m["results"]["context"]["mode"] == "poll":
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
            hit = m["run_id"]
            break
    for r in c.jrows:
        if r["run_id"] == hit and r.get("source_id") == BLB:
            r["status"] = "PARTIAL"
            r["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    # a post-close run observes the failed key — must NOT repair it
    post = W1 + timedelta(hours=2)
    obj = c.capture(BLB, key, post)
    c.run(post, mode="rolling", objs=[obj], sources=[BLB])
    rep = c.verify()
    fault = [r for r in rep["recovery"] if r.get("key") == key]
    assert fault and fault[0]["state"] == "UNRECOVERED"
    assert rep["verdict"] == "INCOMPLETE"


def test_pre_window_smoke_does_not_create_review(tmp_path):
    """Known pre-start smoke manifests must not cause journal<->manifest
    anomalies for the operational window."""
    c = Corpus(tmp_path)
    smoke = datetime(2026, 9, 15, 5, 21, 27, tzinfo=UTC)
    c.run(smoke, objs=[c.capture(BME, bme_key("2026-09-15"), smoke)])
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    rep = c.verify()
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]
    assert rep["anomalies"] == []


def test_rolling_cannot_hide_poll_gap(tmp_path):
    """poll 00:00 -> rolling 00:45 -> poll 01:30 is a 90-min POLL gap;
    the rolling attempt must not split it. Regression for v2."""
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill(c, t0, datetime(2026, 9, 17, 0, 0, tzinfo=UTC), ["2026-09-17"],
         rolling=False)
    c.run(datetime(2026, 9, 17, 0, 45, tzinfo=UTC), mode="rolling")
    fill(c, datetime(2026, 9, 17, 1, 30, tzinfo=UTC), t1, ["2026-09-17"],
         rolling=False)
    c.run(datetime(2026, 9, 17, 6, 15, tzinfo=UTC), mode="rolling")
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, component=True)
    gaps = list(rep["scheduler"]["poll"]["gaps"])
    assert any(g["state"] == "SCHEDULER_GAP_UNEXPLAINED"
               and g["minutes"] == 90.0 for g in gaps)


def test_explained_gap_without_evidence_is_review(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill(c, t0, datetime(2026, 9, 17, 10, 0, tzinfo=UTC), ["2026-09-17"],
         rolling=False)
    fill(c, datetime(2026, 9, 17, 12, 0, tzinfo=UTC), t1, ["2026-09-17"],
         rolling=False)
    c.run(datetime(2026, 9, 17, 6, 15, tzinfo=UTC), mode="rolling")
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, component=True,
                   explained=[{"start": "2026-09-17T10:00:00Z",
                               "end": "2026-09-17T12:00:00Z",
                               "reason": "reboot (operator says so)"}])
    assert any(g["state"] == "SCHEDULER_GAP_EXPLAINED_UNVERIFIED"
               for g in rep["scheduler"]["poll"]["gaps"])
    assert rep["verdict"] == "REVIEW_REQUIRED"


def test_explained_gap_with_evidence(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill(c, t0, datetime(2026, 9, 17, 10, 0, tzinfo=UTC), ["2026-09-17"],
         rolling=False)
    fill(c, datetime(2026, 9, 17, 12, 0, tzinfo=UTC), t1, ["2026-09-17"],
         rolling=False)
    c.run(datetime(2026, 9, 17, 6, 15, tzinfo=UTC), mode="rolling")
    ref, sha = evidence_ref_file(c.root)
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, component=True,
                   explained=[{
                       "start": "2026-09-17T10:00:00Z",
                       "end": "2026-09-17T12:00:00Z",
                       "reason": "reboot per journalctl --list-boots",
                       "evidence_type": "journalctl",
                       "evidence_ref": ref,
                       "evidence_sha256": sha,
                   }])
    assert rep["scheduler"]["poll"]["gaps"][0]["state"] == \
        "SCHEDULER_GAP_EXPLAINED"
    assert rep["verdict"] == "COMPONENT_MODE"


def test_rolling_failed_is_not_operating(tmp_path):
    """A FAILED rolling does not satisfy the rescan requirement on a
    counted trading day."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    rt = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(rt) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "FAILED"
            m["results"]["errors"] = ["discover: HTTP 503"]
            m["results"]["objects"] = []
            m["results"]["context"]["oldest_selected_publication_timestamp"] \
                = None
            m["results"]["context"]["newest_selected_publication_timestamp"] \
                = None
    for r in c.jrows:
        if r["run_id"] == rid(rt) and r.get("source_id") == BLB:
            r["status"] = "FAILED"
            r["errors"] = ["discover: HTTP 503"]
    rep = c.verify()
    assert rep["rolling"]["2026-09-21"][BLB] == "INEFFECTIVE"
    assert rep["verdict"] != "COMPLETE_SUPPORTED"


def test_weekend_rolling_gap_is_diagnostic_only(tmp_path):
    """Missing rolling on a non-counted calendar day must NOT fail the gate
    (weekend continuity is not a frozen criterion)."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    # remove all rolling activity on Sunday 2026-09-20
    c.jrows = [r for r in c.jrows if not (
        r.get("mode") == "rolling" and r["observed_at"][:10] == "2026-09-20")]
    c.mans = [m for m in c.mans if not (
        m["results"]["context"]["mode"] == "rolling"
        and m["results"]["observed_at"][:10] == "2026-09-20")]
    rep = c.verify()
    assert rep["rolling"]["2026-09-20"]["gate_relevant"] is False
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_bme_discover_failure_recovered_by_daily_file(tmp_path):
    """Source-semantic recovery: a BME discover failure on day D is
    RECOVERED when a later in-window run observes D's daily file, even
    though the generic reach rule cannot cross the failure instant."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    # the failed run only re-observed ALREADY_PRESENT objects — its raw was
    # written by earlier runs and stays referenced by their manifests
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BME:
            m["results"]["status"] = "FAILED"
            m["results"]["errors"] = ["discover: HTTP 503"]
            m["results"]["objects"] = []
    for r in c.jrows:
        if r["run_id"] == rid(ft) and r.get("source_id") == BME:
            r["status"] = "FAILED"
            r["errors"] = ["discover: HTTP 503"]
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BME]
    assert disc and disc[0]["state"] == "RECOVERED"


def test_cross_source_same_key_cannot_recover(tmp_path):
    """An object with the same key observed under the OTHER source must not
    recover this source's fetch fault."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    shared = "shared-key.dat"
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [
                f"fetch {shared}: exhausted 3 attempt(s)"]
    for r in c.jrows:
        if r["run_id"] == rid(ft) and r.get("source_id") == BLB:
            r["status"] = "PARTIAL"
            r["errors"] = [f"fetch {shared}: exhausted 3 attempt(s)"]
    later = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    # same key observed under the OTHER source must not recover the fault
    for m in c.mans:
        res = m["results"]
        t = datetime.fromisoformat(res["observed_at"])
        if res["source_id"] == BME and t >= later:
            res["objects"].append(c.capture(BME, shared, t))
            break
    rep = c.verify()
    fault = [r for r in rep["recovery"] if r.get("key") == shared]
    assert fault and fault[0]["state"] == "UNRECOVERED"


def test_same_object_same_hash_two_collection_dates(tmp_path):
    """Same object key + same hash legitimately stored under two
    collection_dates (observed on different days) must not flag."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    # a run on the next day re-stores the same content under its own date
    key = bme_key("2026-09-17")
    d = c.raw / BME / "2026-09-17" / sanitize_filename(key)
    data = next(p for p in d.iterdir()
                if not p.name.endswith(".meta.yaml")).read_bytes()
    t2 = datetime(2026, 9, 18, 6, 15, tzinfo=UTC)
    obj = c.capture(BME, key, t2, data=data)
    for m in c.mans:
        if m["run_id"] == rid(t2) and m["results"]["source_id"] == BME:
            m["results"]["objects"].append(obj)
    rep = c.verify()
    assert rep["anomalies"] == []
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_in_window_raw_orphan_flagged(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    # raw written during the window but never recorded in any manifest
    rec = c.store.store(source_id=BME, collection_date="2026-09-21",
                        object_key="orphan-file", data=b"orphan",
                        publication_timestamp="2026-09-21",
                        provenance_url=f"https://{HOSTS[BME]}/x",
                        observation_utc=iso(
                            datetime(2026, 9, 21, 12, 0, tzinfo=UTC)))
    assert rec.status == "CREATED"
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("never recorded" in a for a in rep["anomalies"])


def test_pre_window_raw_orphan_is_diagnostic(tmp_path):
    """A raw object dated before the window that no A3 manifest references
    is legitimate pre-existing evidence, not A3 corruption."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    c.store.store(source_id=BME, collection_date="2026-09-15",
                  object_key="a1-legacy", data=b"old",
                  publication_timestamp="2026-09-15",
                  provenance_url=f"https://{HOSTS[BME]}/x",
                  observation_utc=iso(
                      datetime(2026, 9, 15, 4, 0, tzinfo=UTC)))
    rep = c.verify()
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]
    assert rep["diagnostics"]["out_of_window_raw_objects"] >= 1


def test_naive_journal_timestamp_is_anomaly(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    c.jrows[0]["observed_at"] = "2026-09-16T09:42:46"   # naive: no tz
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("naive" in a or "unparseable" in a for a in rep["anomalies"])


def test_naive_manifest_timestamp_is_anomaly(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    c.mans[0]["results"]["observed_at"] = "2026-09-16T09:42:46"
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"


def test_duplicate_run_source_different_mode(tmp_path):
    """Two rows sharing (run_id, source_id) with different modes cannot be
    represented by distinct manifest files — must flag."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    dup = dict(c.jrows[0])
    dup["mode"] = "rolling"
    c.jrows.append(dup)
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("duplicate" in a for a in rep["anomalies"])


def test_manifest_filename_mismatch(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    c.flush()
    p = next(c.runs.glob("*__manifest.yaml"))
    m = yaml.safe_load(p.read_text(encoding="utf-8"))
    m["run_id"] = "99999999T999999Z"      # content != filename
    p.write_text(yaml.safe_dump(m), encoding="utf-8")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None)
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("filename" in a for a in rep["anomalies"])


def test_invalid_utf8_journal(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    c.flush()
    with c.journal.open("ab") as f:
        f.write(b"\xff\xfe\x00invalid\n")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None)
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("UTF-8" in a for a in rep["anomalies"])


def test_invalid_utf8_manifest(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    c.flush()
    p = next(c.runs.glob("*__manifest.yaml"))
    p.write_bytes(b"\xff\xfe\x00invalid")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None)
    assert rep["verdict"] == "REVIEW_REQUIRED"


def test_meta_field_mismatch_detected(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    mp = next(c.raw.rglob("*.meta.yaml"))
    meta = yaml.safe_load(mp.read_text())
    meta["source_object_id"] = "tampered-key"
    mp.write_text(yaml.safe_dump(meta))
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("meta" in a for a in rep["anomalies"])


def test_object_observation_mismatch_detected(tmp_path):
    """Deployed semantics: every object's observation_utc equals the run's
    observed_at. Divergence is internal inconsistency."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    m = c.mans[0]
    m["results"]["objects"][0]["observation_utc"] = iso(
        W0 + timedelta(hours=1))
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("observation_utc" in a for a in rep["anomalies"])


def test_provenance_cross_source_rejected(tmp_path):
    """A BME object provenanced to the Bloomberg host must fail."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    for m in c.mans:
        hit = [o for o in m["results"]["objects"]
               if o["source_id"] == BME]
        if hit:
            hit[0]["provenance_url"] = f"https://{HOSTS[BLB]}/x"
            break
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("provenance" in a for a in rep["anomalies"])


def test_provenance_info_page_rejected(tmp_path):
    """bloombergapa.net appears in the descriptor (info_page) but is not an
    acquisition endpoint; payload provenance to it must fail."""
    c = Corpus(tmp_path)
    fill(c, W0, W1, CANDIDATES)
    make_setup_proof(c)
    for m in c.mans:
        hit = [o for o in m["results"]["objects"]
               if o["source_id"] == BLB]
        if hit:
            hit[0]["provenance_url"] = "https://www.bloombergapa.net/x"
            break
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("provenance" in a for a in rep["anomalies"])


# --------------------------------------------------------- setup-day proofs

@pytest.fixture
def sep16(tmp_path):
    """Sep-16 polls only; the adjudication window extends to cover the
    2026-09-17 06:15 rolling that any setup-day proof relies on."""
    c = Corpus(tmp_path)
    fill(c, W0, datetime(2026, 9, 16, 23, 30, tzinfo=UTC), [SETUP])
    return c


SEP16_END = datetime(2026, 9, 17, 7, 0, tzinfo=UTC)


def test_sep16_single_pre_token_insufficient(sep16):
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    pre = [sep16.capture(BLB, tok(datetime(2026, 9, 16, 0, 5, tzinfo=UTC), 3),
                         rt)]
    sep16.run(rt, mode="rolling", objs=pre,
              reach=[token(rt, 1), token(rt, 1)])
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, component=True)
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


def test_sep16_sparse_tokens_insufficient(sep16):
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    pre = [sep16.capture(BLB, tok(datetime(2026, 9, 16, hh, 5, tzinfo=UTC), 3),
                         rt) for hh in (0, 3, 6)]
    sep16.run(rt, mode="rolling", objs=pre,
              reach=[token(datetime(2026, 9, 16, 1, 0, tzinfo=UTC), 1),
                     token(rt, 1)])
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, component=True)
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


def test_sep16_oldest_after_activation_insufficient(sep16):
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    sep16.run(rt, mode="rolling",
              reach=[token(datetime(2026, 9, 16, 10, 0, tzinfo=UTC), 1),
                     token(rt, 1)])
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, component=True)
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


def test_sep16_historical_reach_covering_interval(sep16):
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    pre = [sep16.capture(BLB, tok(datetime(2026, 9, 16, hh, 5, tzinfo=UTC), 3),
                         rt) for hh in (0, 3, 6)]
    sep16.run(rt, mode="rolling", objs=pre,
              reach=[token(datetime(2026, 9, 15, 23, 55, tzinfo=UTC), 1),
                     token(datetime(2026, 9, 17, 0, 5, tzinfo=UTC), 1)])
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, component=True)
    assert rep["setup_day"]["result"] == "SETUP_DAY_CAPTURE_COMPLETE"


def test_sep16_failed_historical_scan_not_demonstrated(sep16):
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    sep16.run(rt, mode="rolling", errs={BLB: ["discover: HTTP 503"]})
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, component=True)
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


# ----------------------------------------------------- remaining v2 checks

def test_unexplained_poll_gap_fails(window):
    """A 2h invocation hole (journal+manifests+their captures all absent)
    is an unexplained poll gap -> INCOMPLETE."""
    c = window
    hole_start = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    hole_end = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    removed = [m for m in c.mans if (
        m["results"]["context"]["mode"] == "poll"
        and hole_start < datetime.fromisoformat(
            m["results"]["observed_at"]) < hole_end)]
    for m in removed:
        # objects first created inside the hole never existed
        for o in m["results"]["objects"]:
            if o["status"] == "CREATED":
                c.drop_raw(o["source_id"], o["capture_version"])
    c.jrows = [r for r in c.jrows if not (
        r.get("mode") == "poll"
        and hole_start < datetime.fromisoformat(r["observed_at"]) < hole_end)]
    c.mans = [m for m in c.mans if m not in removed]
    rep = c.verify()
    assert rep["verdict"] == "INCOMPLETE"
    assert rep["scheduler"]["poll"]["unexplained"]


def test_skipped_locked_poll_is_attempt(window):
    c = window
    t = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    c.skipped(t, mode="poll")
    rep = c.verify()
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_failed_run_is_attempt_not_gap(window):
    c = window
    key = tok(datetime(2026, 9, 21, 9, 55, tzinfo=UTC), 5)
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    for r in c.jrows:
        if r["run_id"] == rid(ft) and r["source_id"] == BLB:
            r["status"] = "PARTIAL"
            r["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    rep = c.verify()
    assert rep["scheduler"]["poll"]["unexplained"] == []
    fault = [x for x in rep["recovery"] if x.get("key") == key]
    assert fault and fault[0]["state"] == "UNRECOVERED"
    assert rep["verdict"] == "INCOMPLETE"


def test_partial_recovered_by_exact_key(window):
    c = window
    key = tok(datetime(2026, 9, 21, 9, 55, tzinfo=UTC), 5)
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    for r in c.jrows:
        if r["run_id"] == rid(ft) and r.get("source_id") == BLB:
            r["status"] = "PARTIAL"
            r["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    rt = datetime(2026, 9, 21, 13, 15, tzinfo=UTC)
    obj = c.capture(BLB, key, rt)
    c.run(rt, mode="rolling", objs=[obj],
          reach=[token(datetime(2026, 9, 21, 0, 5, tzinfo=UTC), 1),
                 token(rt, 1)])
    rep = c.verify()
    fault = [x for x in rep["recovery"] if x.get("key") == key]
    assert fault and fault[0]["state"] == "RECOVERED"
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_manifest_without_journal(window):
    c = window
    drop = rid(W0)
    c.jrows = [r for r in c.jrows if r["run_id"] != drop]
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("manifest without journal" in a for a in rep["anomalies"])


def test_journal_without_manifest(window):
    c = window
    drop = rid(W0)
    c.mans = [m for m in c.mans if m["run_id"] != drop]
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("journal row without manifest" in a for a in rep["anomalies"])


def test_duplicate_journal_row(window):
    c = window
    c.jrows.append(dict(c.jrows[0]))
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"


def test_malformed_journal_line(window):
    c = window
    c.flush()
    with c.journal.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None)
    assert rep["verdict"] == "REVIEW_REQUIRED"


def test_raw_without_meta(window):
    c = window
    c.flush()
    d = c.raw / BME / "2026-09-21" / "orphan"
    d.mkdir(parents=True)
    (d / ("0" * 64)).write_bytes(b"x")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None)
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("raw object without meta" in a for a in rep["anomalies"])


def test_meta_without_raw(window):
    c = window
    c.flush()
    d = c.raw / BME / "2026-09-21" / "orphan"
    d.mkdir(parents=True)
    (d / ("0" * 64 + ".meta.yaml")).write_text(yaml.safe_dump(
        {"raw_sha256": "0" * 64,
         "observation_utc": "2026-09-21T12:00:00+00:00"}))
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None)
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("meta without raw" in a for a in rep["anomalies"])


def test_hash_mismatch(window):
    c = window
    c.flush()
    rawf = next(p for p in c.raw.rglob("*")
                if p.is_file() and not p.name.endswith(".meta.yaml"))
    rawf.write_bytes(b"tampered")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None)
    assert rep["verdict"] == "REVIEW_REQUIRED"


def test_bme_single_version_repeated_observations(window):
    rep = window.verify()
    bme = rep["coverage"]["2026-09-17"][BME]
    assert bme["state"] == "CAPTURED"
    assert bme["distinct_capture_versions"] == 1
    assert bme["observations"] > 1


def test_bme_mutation_two_versions(window):
    c = window
    rt = datetime(2026, 9, 17, 20, 15, tzinfo=UTC)
    obj = c.capture(BME, bme_key("2026-09-17"), rt, data=b"mutated")
    c.run(rt, mode="rolling", objs=[obj])
    rep = c.verify()
    bme = rep["coverage"]["2026-09-17"][BME]
    assert bme["state"] == "CAPTURED"
    assert bme["distinct_capture_versions"] == 2


def test_blb_irregular_cadence_not_fatal(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill(c, t0, t1, ["2026-09-17"], blb=False)
    mt = datetime.fromisoformat(c.mans[1]["results"]["observed_at"])
    for hh, mm in [(0, 5), (0, 15), (0, 25), (5, 0), (5, 10), (5, 20)]:
        tt = datetime(2026, 9, 17, hh, mm, tzinfo=UTC)
        c.mans[1]["results"]["objects"].append(c.capture(BLB, tok(tt, 2), mt))
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, component=True)
    blb = rep["coverage"]["2026-09-17"][BLB]
    assert blb["state"] == "CAPTURED"
    assert blb["candidate_gaps_diagnostic_only"]


def test_blb_gap_plus_unrecovered_outage(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill(c, t0, t1, ["2026-09-17"], blb=False)
    mt = datetime.fromisoformat(c.mans[1]["results"]["observed_at"])
    for hh, mm in [(0, 5), (0, 15), (5, 0), (5, 10)]:
        tt = datetime(2026, 9, 17, hh, mm, tzinfo=UTC)
        c.mans[1]["results"]["objects"].append(c.capture(BLB, tok(tt, 2), mt))
    key = "BAPA-POST2-20260917-03:30:00.000-01.csv"
    hit = None
    for m in c.mans:
        if m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
            hit = m["run_id"]
            break
    for r in c.jrows:
        if r["run_id"] == hit and r.get("source_id") == BLB:
            r["status"] = "PARTIAL"
            r["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, component=True)
    assert rep["coverage"]["2026-09-17"][BLB]["state"] == "UNRECOVERED"


def test_journal_order_invariance(window):
    """Journal line order must not affect adjudication."""
    c = window
    c.jrows = c.jrows[::-1]
    c.mans = c.mans[::-1]
    rep = c.verify()
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_component_mode_never_complete(window):
    """Even a fully-clean window evaluated in component mode cannot emit
    the gate verdict."""
    rep = window.verify(component=True)
    assert rep["verdict"] == "COMPONENT_MODE"
