"""Synthetic adversarial corpus for tools/verify_a3_window.py (v5).

Every fixture is generated: raw payloads are arbitrary bytes written through
the frozen efd2268 store_capture (so the raw/meta layout matches the deployed
collector), source descriptors are the exact deployed bytes (sealed by
fingerprint), and journal/manifests reproduce the deployed efd2268 schema.
No provider data and no accrued A3 evidence is ever read.

Gate-level tests use the preregistered six-candidate set
{16,17,18,21,22,23} with deterministic Day-1 fallback. Single-day and
axis-level tests run with component=True, a mode that can never emit
COMPLETE_SUPPORTED.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from tools.a3_efd2268_compat import (
    UNVERIFIED_TS_MARGIN,
    parse_publication_timestamp,
    sanitize_filename,
    store_capture,
)
from tools.verify_a3_window import PROFILE_SHA256, verify

SOURCES = ("bme_apa", "blb_apae")
HOSTS = {"bme_apa": "www.bolsasymercados.es",
         "blb_apae": "www.bloombergapa.com"}
BME = "bme_apa"
BLB = "blb_apae"

CANDIDATES = ["2026-09-16", "2026-09-17", "2026-09-18",
              "2026-09-21", "2026-09-22", "2026-09-23"]
SETUP = "2026-09-16"
W0 = datetime(2026, 9, 16, 9, 42, 46, tzinfo=UTC)   # declared op start
W1 = datetime(2026, 9, 23, 23, 59, 59, tzinfo=UTC)  # subject-period end
TAIL = datetime(2026, 9, 24, 7, 0, tzinfo=UTC)      # preregistered tail end
TAIL_FILL = datetime(2026, 9, 24, 6, 42, 46, tzinfo=UTC)  # last tail poll

# Exact deployed source descriptors (efd2268), fingerprinted by the
# sealed adjudication profile.
EFD2268_DESC_DIR = (Path(__file__).parent / "fixtures"
                    / "efd2268_descriptors")


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
        self.jrows: list[dict] = []
        self.mans: list[dict] = []
        for d in (self.raw, self.runs, self.cfg):
            d.mkdir(parents=True, exist_ok=True)
        # The deployed efd2268 descriptors verbatim: the verifier seals
        # them by exact-byte SHA-256, so the corpus must reproduce the
        # real bytes (checked into tests/fixtures/efd2268_descriptors/).
        for name in ("bme_apa.yaml", "bloomberg_apae.yaml"):
            (self.cfg / name).write_bytes(
                (EFD2268_DESC_DIR / name).read_bytes())

    # -- raw objects -----------------------------------------------------
    def capture(self, source, key, t, data=None, pub=None):
        data = data if data is not None else b"synthetic:" + key.encode()
        if pub is None:
            # deployed adapters store the BARE token (no BAPA-POST2- prefix)
            pub = (key[len("BAPA-POST2-"):].removesuffix(".csv")
                   if key.startswith("BAPA-POST2-")
                   else key.split("-bmea-")[0])
        # efd2268 store semantics via the frozen compat layer — the corpus
        # never depends on the current production RawStore.
        return store_capture(
            self.raw,
            source_id=source,
            collection_date=t.date().isoformat(),
            object_key=key,
            data=data,
            publication_timestamp=pub,
            provenance_url=f"https://{HOSTS[source]}/{key}",
            observation_utc=iso(t))

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
            wh = 168.0 if mode == "rolling" else 24.0
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
                        "mode": mode, "window_hours": wh,
                        "window_cutoff_utc": iso(
                            (observed_at or t) - timedelta(hours=wh)),
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
                "mode": mode, "window_hours": wh, "status": st,
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

    def drop_raw(self, source: str, cv: str,
                 collection_date: str | None = None) -> None:
        """Remove a captured object dir (simulates discover never fetched
        it). Restricted to one collection_date — the same capture_version
        legitimately lives under other dates."""
        import shutil
        pattern = (f"{source}/{collection_date}/*/{cv}" if collection_date
                   else f"{source}/*/*/{cv}")
        for f in self.raw.glob(pattern):
            shutil.rmtree(f.parent)

    def verify(self, start=W0, end=W1, candidates=CANDIDATES,
               setup=SETUP, explained=None, tail=TAIL, component=False):
        self.flush()
        ep = None
        if explained is not None:
            ep = self.root / "explained.yaml"
            ep.write_text(yaml.safe_dump(explained), encoding="utf-8")
        return verify(self.journal, self.runs, self.raw, self.cfg,
                      iso(start), iso(end), list(candidates),
                      setup, ep, tail_end=iso(tail) if tail else None,
                      component=component)


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
            # a rolling rescan re-observes every candidate daily file in
            # its 7-day lookback (ALREADY_PRESENT after first capture)
            robjs = [c.capture(BME, bme_key(cd), rt)
                     for cd in trading_days
                     if day - timedelta(days=7) <= date.fromisoformat(cd)
                     <= day]
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
    # unverified-token margin: guaranteed reach = [oldest+6h, newest-6h]
    # must cover [Sep-16 00:00Z, activation 09:42:46Z], so oldest must be
    # <= Sep-15 18:00Z and newest >= Sep-16 15:42:46Z.
    reach = [token(datetime(2026, 9, 15, 17, 55, tzinfo=UTC), 1),
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
    """Full six-candidate window incl. the preregistered recovery tail:
    polls through Sep-24 06:42:46Z, tail ends 07:00Z. All days covered,
    Sep-16 setup proven."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, tail=None)
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    # a post-tail run observes the failed key — must NOT repair it
    post = TAIL + timedelta(hours=1)
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, component=True,
                   tail=None)
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
                   tail=None,
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
                   tail=None,
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    assert rep["rolling"][BLB]["state"] == "ROLLING_INCIDENT_UNRESOLVED"
    assert rep["verdict"] != "COMPLETE_SUPPORTED"


def test_weekend_rolling_gap_is_diagnostic_only(tmp_path):
    """Missing rolling on a non-counted calendar day must NOT fail the gate
    (weekend continuity is not a frozen criterion)."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    # remove all rolling activity on Sunday 2026-09-20 (including the raw
    # objects that invocation alone created)
    removed = [m for m in c.mans if (
        m["results"]["context"]["mode"] == "rolling"
        and m["results"]["observed_at"][:10] == "2026-09-20")]
    for m in removed:
        for o in m["results"]["objects"]:
            if o["status"] == "CREATED":
                c.drop_raw(o["source_id"], o["capture_version"],
                           m["results"]["observed_at"][:10])
    c.jrows = [r for r in c.jrows if not (
        r.get("mode") == "rolling" and r["observed_at"][:10] == "2026-09-20")]
    c.mans = [m for m in c.mans if m not in removed]
    rep = c.verify()
    for sid in SOURCES:
        assert "2026-09-20" not in \
            rep["rolling"][sid]["days_observed_diagnostic"]
        assert rep["rolling"][sid]["state"] == "ROLLING_EFFECTIVE"
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_bme_discover_failure_recovered_by_daily_file(tmp_path):
    """Source-semantic recovery: a BME discover failure on day D is
    RECOVERED when a later in-window run observes D's daily file, even
    though the generic reach rule cannot cross the failure instant."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    # raw written during the window but never recorded in any manifest
    rec = store_capture(c.raw, source_id=BME, collection_date="2026-09-21",
                        object_key="orphan-file", data=b"orphan",
                        publication_timestamp="2026-09-21",
                        provenance_url=f"https://{HOSTS[BME]}/x",
                        observation_utc=iso(
                            datetime(2026, 9, 21, 12, 0, tzinfo=UTC)))
    assert rec["status"] == "CREATED"
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("never recorded" in a for a in rep["anomalies"])


def test_pre_window_raw_orphan_is_diagnostic(tmp_path):
    """A raw object dated before the window that no A3 manifest references
    is legitimate pre-existing evidence, not A3 corruption."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    store_capture(c.raw, source_id=BME, collection_date="2026-09-15",
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    c.jrows[0]["observed_at"] = "2026-09-16T09:42:46"   # naive: no tz
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("naive" in a or "unparseable" in a for a in rep["anomalies"])


def test_naive_manifest_timestamp_is_anomaly(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    c.mans[0]["results"]["observed_at"] = "2026-09-16T09:42:46"
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"


def test_duplicate_run_source_different_mode(tmp_path):
    """Two rows sharing (run_id, source_id) with different modes cannot be
    represented by distinct manifest files — must flag."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    dup = dict(c.jrows[0])
    dup["mode"] = "rolling"
    c.jrows.append(dup)
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("duplicate" in a for a in rep["anomalies"])


def test_manifest_filename_mismatch(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    c.flush()
    p = next(c.runs.glob("*__manifest.yaml"))
    m = yaml.safe_load(p.read_text(encoding="utf-8"))
    m["run_id"] = "99999999T999999Z"      # content != filename
    p.write_text(yaml.safe_dump(m), encoding="utf-8")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None, iso(TAIL))
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("filename" in a for a in rep["anomalies"])


def test_invalid_utf8_journal(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    c.flush()
    with c.journal.open("ab") as f:
        f.write(b"\xff\xfe\x00invalid\n")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None, iso(TAIL))
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("UTF-8" in a for a in rep["anomalies"])


def test_invalid_utf8_manifest(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    c.flush()
    p = next(c.runs.glob("*__manifest.yaml"))
    p.write_bytes(b"\xff\xfe\x00invalid")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None, iso(TAIL))
    assert rep["verdict"] == "REVIEW_REQUIRED"


def test_meta_field_mismatch_detected(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    fill(c, W0, TAIL_FILL, CANDIDATES)
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
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, tail=None,
                       component=True)
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


def test_sep16_sparse_tokens_insufficient(sep16):
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    pre = [sep16.capture(BLB, tok(datetime(2026, 9, 16, hh, 5, tzinfo=UTC), 3),
                         rt) for hh in (0, 3, 6)]
    sep16.run(rt, mode="rolling", objs=pre,
              reach=[token(datetime(2026, 9, 16, 1, 0, tzinfo=UTC), 1),
                     token(rt, 1)])
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, tail=None,
                       component=True)
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


def test_sep16_oldest_after_activation_insufficient(sep16):
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    sep16.run(rt, mode="rolling",
              reach=[token(datetime(2026, 9, 16, 10, 0, tzinfo=UTC), 1),
                     token(rt, 1)])
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, tail=None,
                       component=True)
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


def test_sep16_historical_reach_covering_interval(sep16):
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    pre = [sep16.capture(BLB, tok(datetime(2026, 9, 16, hh, 5, tzinfo=UTC), 3),
                         rt) for hh in (0, 3, 6)]
    sep16.run(rt, mode="rolling", objs=pre,
              reach=[token(datetime(2026, 9, 15, 17, 55, tzinfo=UTC), 1),
                     token(datetime(2026, 9, 17, 0, 5, tzinfo=UTC), 1)])
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, tail=None,
                       component=True)
    assert rep["setup_day"]["result"] == "SETUP_DAY_CAPTURE_COMPLETE"


def test_sep16_failed_historical_scan_not_demonstrated(sep16):
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    sep16.run(rt, mode="rolling", errs={BLB: ["discover: HTTP 503"]})
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, tail=None,
                       component=True)
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
                c.drop_raw(o["source_id"], o["capture_version"],
                           m["results"]["observed_at"][:10])
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
                 CANDIDATES, SETUP, None, iso(TAIL))
    assert rep["verdict"] == "REVIEW_REQUIRED"


def test_raw_without_meta(window):
    c = window
    c.flush()
    d = c.raw / BME / "2026-09-21" / "orphan"
    d.mkdir(parents=True)
    (d / ("0" * 64)).write_bytes(b"x")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None, iso(TAIL))
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
                 CANDIDATES, SETUP, None, iso(TAIL))
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("meta without raw" in a for a in rep["anomalies"])


def test_hash_mismatch(window):
    c = window
    c.flush()
    rawf = next(p for p in c.raw.rglob("*")
                if p.is_file() and not p.name.endswith(".meta.yaml"))
    rawf.write_bytes(b"tampered")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None, iso(TAIL))
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
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, component=True,
                   tail=None)
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
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, component=True,
                   tail=None)
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


# ============================================================= v4: tail end

def test_tail_observation_covers_final_counted_day(tmp_path):
    """A day-23 token published just before close and first observable by
    the first post-midnight poll inside the preregistered tail IS eligible
    evidence for its counted day."""
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 23, tzinfo=UTC)
    fill(c, t0, TAIL_FILL, ["2026-09-23"])
    late = datetime(2026, 9, 24, 0, 12, 46, tzinfo=UTC)
    key = tok(datetime(2026, 9, 23, 23, 55, tzinfo=UTC), 9)
    for m in c.mans:
        if m["run_id"] == rid(late) and m["results"]["source_id"] == BLB:
            m["results"]["objects"].append(c.capture(BLB, key, late))
    rep = c.verify(t0, W1, ["2026-09-23"], setup=None, component=True)
    assert rep["coverage"]["2026-09-23"][BLB]["state"] == "CAPTURED"
    bme = rep["coverage"]["2026-09-23"][BME]
    assert bme["state"] == "CAPTURED"
    # the tail rolling re-observed the day file after subject close
    assert bme["last_observed"] > iso(W1)


def test_tail_cannot_create_sixth_day(tmp_path):
    """Next-day objects observed inside the tail never create coverage for
    a day outside the counted set."""
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 23, tzinfo=UTC)
    fill(c, t0, TAIL_FILL, ["2026-09-23"])
    late = datetime(2026, 9, 24, 0, 12, 46, tzinfo=UTC)
    key = tok(datetime(2026, 9, 24, 0, 30, tzinfo=UTC), 9)
    for m in c.mans:
        if m["run_id"] == rid(late) and m["results"]["source_id"] == BLB:
            m["results"]["objects"].append(c.capture(BLB, key, late))
    rep = c.verify(t0, W1, ["2026-09-23"], setup=None, component=True)
    assert "2026-09-24" not in rep["coverage"]
    assert rep["counted_days"] == ["2026-09-23"]


def test_fault_recovered_inside_tail(window):
    """An in-window fetch fault whose exact (source,key) is re-observed by
    a tail run is RECOVERED — the tail exists precisely for this."""
    c = window
    key = tok(datetime(2026, 9, 22, 9, 55, tzinfo=UTC), 5)
    ft = datetime(2026, 9, 22, 10, 12, 46, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    for r in c.jrows:
        if r["run_id"] == rid(ft) and r.get("source_id") == BLB:
            r["status"] = "PARTIAL"
            r["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    t = datetime(2026, 9, 24, 6, 42, 46, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(t) and m["results"]["source_id"] == BLB:
            m["results"]["objects"].append(c.capture(BLB, key, t))
    rep = c.verify()
    fault = [x for x in rep["recovery"] if x.get("key") == key]
    assert fault and fault[0]["state"] == "RECOVERED"
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_fault_recovery_after_tail_ignored(window):
    """Metamorphic pair of the above: the SAME observation moved one hour
    past evidence_tail_end cannot repair the fault."""
    c = window
    key = tok(datetime(2026, 9, 22, 9, 55, tzinfo=UTC), 5)
    ft = datetime(2026, 9, 22, 10, 12, 46, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    for r in c.jrows:
        if r["run_id"] == rid(ft) and r.get("source_id") == BLB:
            r["status"] = "PARTIAL"
            r["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    post = TAIL + timedelta(hours=1)
    obj = c.capture(BLB, key, post)
    c.run(post, objs=[obj], sources=[BLB])
    rep = c.verify()
    fault = [x for x in rep["recovery"] if x.get("key") == key]
    assert fault and fault[0]["state"] == "UNRECOVERED"
    assert rep["verdict"] == "INCOMPLETE"


def test_fallback_sep23_completed_by_tail_rolling(window):
    """Fallback counted = 17..23: even if the Sep-23 daily file were only
    observable by the tail rolling, the day still adjudicates CAPTURED."""
    c = window
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(rt) and m["results"]["source_id"] == BLB:
            m["results"]["context"]["oldest_selected_publication_timestamp"] = \
                token(datetime(2026, 9, 17, 0, 0, tzinfo=UTC), 1)  # too late
    # strip every subject-period observation of the Sep-23 daily file;
    # only the Sep-24 06:15 tail rolling keeps it
    for m in c.mans:
        res = m["results"]
        if res["source_id"] != BME:
            continue
        if res["observed_at"] >= iso(W1):
            continue
        keep = []
        for o in res["objects"]:
            if o["source_object_id"] == bme_key("2026-09-23"):
                if o["status"] == "CREATED":
                    c.drop_raw(BME, o["capture_version"],
                               res["observed_at"][:10])
            else:
                keep.append(o)
        res["objects"] = keep
        res["object_count"] = len(keep)
    rep = c.verify()
    assert rep["counted_days"] == CANDIDATES[1:]
    bme = rep["coverage"]["2026-09-23"][BME]
    assert bme["state"] == "CAPTURED"
    assert bme["first_observed"] > iso(W1)
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


# ====================================================== v4: first-write meta

def test_pre_start_first_write_meta_accepted(tmp_path):
    """The real Sep-16 scenario: pre-operational smoke captures the daily
    file at 05:00Z; A3 re-observes it from 09:42:46Z onward. meta stays
    05:00Z — earlier than the first eligible observation — ACCEPTED."""
    c = Corpus(tmp_path)
    smoke = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)
    c.capture(BME, bme_key("2026-09-16"), smoke)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    rep = c.verify()
    assert rep["anomalies"] == []
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_meta_later_than_first_known_observation_is_anomaly(window):
    """meta.observation_utc claiming first write AFTER a manifest-recorded
    observation of that stored version is a structural anomaly."""
    c = window
    objdir = c.raw / BME / "2026-09-16" / sanitize_filename(
        bme_key("2026-09-16"))
    mf = next(objdir.glob("*.meta.yaml"))
    meta = yaml.safe_load(mf.read_text(encoding="utf-8"))
    meta["observation_utc"] = "2026-09-16T12:00:00+00:00"  # > 09:42:46 obs
    mf.write_text(yaml.safe_dump(meta), encoding="utf-8")
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("later than first known observation" in a
               for a in rep["anomalies"])


# ============================================== v4: discovery at-risk spans

def _fail_discover(c, source, ft):
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == source:
            for o in m["results"]["objects"]:
                if o["status"] == "CREATED":
                    c.drop_raw(source, o["capture_version"],
                               ft.date().isoformat())
            m["results"]["objects"] = []
            m["results"]["object_count"] = 0
            m["results"]["status"] = "FAILED"
            m["results"]["errors"] = ["discover: HTTP 503"]
            m["results"]["context"]["oldest_selected_publication_timestamp"] \
                = None
            m["results"]["context"]["newest_selected_publication_timestamp"] \
                = None
    for r in c.jrows:
        if r["run_id"] == rid(ft) and r.get("source_id") == source:
            r["status"] = "FAILED"
            r["errors"] = ["discover: HTTP 503"]


def test_blb_discover_interval_needs_full_cover(tmp_path):
    """Failure 10:12:46 on the 21st; previous clean discovery 09:42:46.
    A rescan reach [09:55,10:30] cannot conservatively cover the at-risk
    interval (09:42:46,10:12:46] under the 6h unverified-token margin."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    _fail_discover(c, BLB, ft)
    rt = datetime(2026, 9, 21, 11, 0, tzinfo=UTC)
    c.run(rt, mode="rolling", objs=[
        c.capture(BLB, tok(datetime(2026, 9, 21, 9, 55, tzinfo=UTC), 1), rt),
        c.capture(BLB, tok(datetime(2026, 9, 21, 10, 30, tzinfo=UTC), 1), rt)])
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BLB and r["run_id"] == rid(ft)]
    assert disc and disc[0]["state"] == "INDETERMINATE"
    assert disc[0]["gate_relevant"] is True
    assert disc[0]["at_risk"]["start"] == "2026-09-21T09:42:46+00:00"
    assert rep["verdict"] == "INCOMPLETE"


def test_blb_discover_interval_full_cover_recovers(tmp_path):
    """Same failure; a rescan whose guaranteed reach [oldest+6h, newest-6h]
    covers the whole at-risk interval proves recovery."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    _fail_discover(c, BLB, ft)
    rt = datetime(2026, 9, 21, 12, 30, tzinfo=UTC)
    # guaranteed reach [09:30, 10:30] covers the at-risk (09:42:46,10:12:46]
    c.run(rt, mode="rolling", objs=[
        c.capture(BLB, tok(datetime(2026, 9, 21, 3, 30, tzinfo=UTC), 1), rt),
        c.capture(BLB, tok(datetime(2026, 9, 21, 16, 30, tzinfo=UTC), 1), rt)])
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BLB and r["run_id"] == rid(ft)]
    assert disc and disc[0]["state"] == "RECOVERED"
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_blb_discover_interval_narrowing_metamorphic(tmp_path):
    """Narrowing the demonstrated reach can only worsen (or keep) the
    recovery state, never improve it."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    _fail_discover(c, BLB, ft)
    rt = datetime(2026, 9, 21, 12, 30, tzinfo=UTC)
    # guaranteed reach [09:30, 10:45] covers the at-risk (09:42:46,10:12:46]
    c.run(rt, mode="rolling", objs=[
        c.capture(BLB, tok(datetime(2026, 9, 21, 3, 30, tzinfo=UTC), 1), rt),
        c.capture(BLB, tok(datetime(2026, 9, 21, 16, 45, tzinfo=UTC), 1), rt)])
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BLB and r["run_id"] == rid(ft)]
    assert disc[0]["state"] == "RECOVERED"
    # narrowing the oldest edge inside the margin: guaranteed start
    # becomes 09:50 > 09:42:46 -> no longer covers -> INDETERMINATE
    for m in c.mans:
        if m["run_id"] == rid(rt) and m["results"]["source_id"] == BLB:
            m["results"]["context"][
                "oldest_selected_publication_timestamp"] = token(
                    datetime(2026, 9, 21, 3, 50, tzinfo=UTC), 1)
    rep2 = c.verify()
    disc2 = [r for r in rep2["recovery"] if r["kind"] == "discover"
             and r["source"] == BLB and r["run_id"] == rid(ft)]
    assert disc2[0]["state"] == "INDETERMINATE"


def test_blb_discover_at_risk_crosses_day_boundary(tmp_path):
    """A failure at 00:12:46 on the 22nd puts publications after the
    23:42:46 discovery on the 21st at risk — the interval crosses the
    trading-date boundary and stays gate-relevant."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    ft = datetime(2026, 9, 22, 0, 12, 46, tzinfo=UTC)
    _fail_discover(c, BLB, ft)
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BLB and r["run_id"] == rid(ft)]
    assert disc[0]["at_risk"]["start"] == "2026-09-21T23:42:46+00:00"
    assert disc[0]["gate_relevant"] is True
    assert disc[0]["state"] == "INDETERMINATE"


def test_blb_discover_first_run_uses_conservative_bound(tmp_path):
    """No previous successful discovery: the at-risk interval falls back
    to [op_start, t_fail] (the run's own cutoff predates the window)."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    # no setup proof -> fallback counted = 17..23 -> Sep-16 not counted
    _fail_discover(c, BLB, W0)
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BLB and r["run_id"] == rid(W0)]
    assert disc[0]["at_risk"]["start"] == "2026-09-16T09:42:46+00:00"
    # the interval touches only Sep-16, which is not a counted day
    assert disc[0]["gate_relevant"] is False


# ===================================================== v4: BAPA uncertainty

def test_sep16_oldest_inside_margin_insufficient(sep16):
    """oldest=Sep-15 19:00 is inside the 6h uncertainty margin of day0 —
    guaranteed reach starts Sep-16 01:00, short of 00:00Z: not a proof."""
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    sep16.run(rt, mode="rolling",
              reach=[token(datetime(2026, 9, 15, 19, 0, tzinfo=UTC), 1),
                     token(rt, 1)])
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, tail=None,
                       component=True)
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


def test_sep16_newest_inside_margin_insufficient(sep16):
    """newest=Sep-16 12:00 leaves guaranteed reach ending at 06:00Z < the
    09:42:46Z activation — the pre-activation interval is not covered."""
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    sep16.run(rt, mode="rolling",
              reach=[token(datetime(2026, 9, 15, 17, 0, tzinfo=UTC), 1),
                     token(datetime(2026, 9, 16, 12, 0, tzinfo=UTC), 1)])
    rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, tail=None,
                       component=True)
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


def test_bapa_token_shift_inside_margin_is_metamorphic(sep16):
    """Shifting an unverified token inside the uncertainty margin must not
    create a false exact-time proof — both shifts stay non-proofs."""
    rt = datetime(2026, 9, 17, 6, 15, tzinfo=UTC)
    for hh in (20, 22):  # Sep-15 20:00 / 22:00 — both inside day0's margin
        sep16.mans = [m for m in sep16.mans if m["run_id"] != rid(rt)]
        sep16.jrows = [r for r in sep16.jrows if r["run_id"] != rid(rt)]
        sep16.run(rt, mode="rolling",
                  reach=[token(datetime(2026, 9, 15, hh, 0, tzinfo=UTC), 1),
                         token(rt, 1)])
        rep = sep16.verify(W0, SEP16_END, [SETUP], setup=SETUP, tail=None,
                           component=True)
        assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"


# ===================================================== v4: rolling contract

def test_failed_rolling_then_recovered_is_operating(tmp_path):
    """One failed rolling invocation followed by demonstrated recovery of
    its at-risk interval must not worsen the verdict vs an uninterrupted
    effective state (only historical diagnostics differ)."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    rt = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(rt) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "FAILED"
            m["results"]["errors"] = ["discover: HTTP 503"]
    for r in c.jrows:
        if r["run_id"] == rid(rt) and r.get("source_id") == BLB:
            r["status"] = "FAILED"
            r["errors"] = ["discover: HTTP 503"]
    # demonstrated recovery: guaranteed reach covers (06:12:46, 06:15:00]
    rr = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)
    c.run(rr, mode="rolling", objs=[
        c.capture(BLB, tok(datetime(2026, 9, 21, 0, 0, tzinfo=UTC), 1), rr),
        c.capture(BLB, tok(datetime(2026, 9, 21, 12, 30, tzinfo=UTC), 1), rr)])
    rep = c.verify()
    assert rep["rolling"][BLB]["state"] == "ROLLING_INCIDENT_RECOVERED"
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_bme_rolling_discover_incident_recovered(tmp_path):
    """A BME discover failure in a rolling (7d lookback) is RECOVERED when
    every at-risk candidate daily file is later observed."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    rt = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
    _fail_discover(c, BME, rt)
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BME and r["run_id"] == rid(rt)]
    assert disc[0]["state"] == "RECOVERED"
    assert rep["rolling"][BME]["state"] == "ROLLING_INCIDENT_RECOVERED"
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_rolling_never_operating_fails(tmp_path):
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES, rolling=False)
    rep = c.verify()
    for sid in SOURCES:
        assert rep["rolling"][sid]["state"] == "ROLLING_NOT_OPERATING"
    assert rep["verdict"] == "INCOMPLETE"


# ===================================================== v4: evidence sealing

def test_explained_gap_bad_hash_is_review(tmp_path):
    c = Corpus(tmp_path)
    t0 = datetime(2026, 9, 17, tzinfo=UTC)
    t1 = datetime(2026, 9, 17, 23, 30, tzinfo=UTC)
    fill(c, t0, datetime(2026, 9, 17, 10, 0, tzinfo=UTC), ["2026-09-17"],
         rolling=False)
    fill(c, datetime(2026, 9, 17, 12, 0, tzinfo=UTC), t1, ["2026-09-17"],
         rolling=False)
    c.run(datetime(2026, 9, 17, 6, 15, tzinfo=UTC), mode="rolling")
    ref, _sha = evidence_ref_file(c.root)
    rep = c.verify(t0, t1, ["2026-09-17"], setup=None, component=True,
                   tail=None,
                   explained=[{
                       "start": "2026-09-17T10:00:00Z",
                       "end": "2026-09-17T12:00:00Z",
                       "reason": "reboot",
                       "evidence_type": "journalctl",
                       "evidence_ref": ref,
                       "evidence_sha256": "0" * 64,   # wrong hash
                   }])
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("hash mismatch" in a for a in rep["anomalies"])


def test_explained_gap_evidence_recorded(tmp_path):
    """A verified explanation seals {type, ref, sha256} into the report."""
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
                   tail=None,
                   explained=[{
                       "start": "2026-09-17T10:00:00Z",
                       "end": "2026-09-17T12:00:00Z",
                       "reason": "reboot per journalctl --list-boots",
                       "evidence_type": "journalctl",
                       "evidence_ref": ref,
                       "evidence_sha256": sha,
                   }])
    g = rep["scheduler"]["poll"]["gaps"][0]
    assert g["state"] == "SCHEDULER_GAP_EXPLAINED"
    assert g["evidence"] == {"type": "journalctl", "ref": ref,
                            "sha256": sha}


# ======================================== v4: frozen efd2268 compatibility

def test_efd2268_parse_publication_timestamp_parity():
    dt, verified = parse_publication_timestamp(
        "20260917-03:30:00.000-01")
    assert dt == datetime(2026, 9, 17, 3, 30, tzinfo=UTC)
    assert verified is False
    _dt, verified = parse_publication_timestamp("2026-09-17T03:30:00+00:00")
    assert verified is True
    dt, verified = parse_publication_timestamp("2026-09-17T03:30:00")
    assert dt == datetime(2026, 9, 17, 3, 30, tzinfo=UTC)
    assert verified is True           # deployed assumes UTC for naive ISO
    assert parse_publication_timestamp("garbage") == (None, False)
    assert parse_publication_timestamp(None) == (None, False)


def test_efd2268_sanitize_parity():
    assert sanitize_filename("BAPA-POST2-20260917-03:30:00.000-01.csv") == \
        "BAPA-POST2-20260917-03_30_00.000-01.csv"
    assert sanitize_filename("2026-09-16-bmea-posttrade.json") == \
        "2026-09-16-bmea-posttrade.json"
    assert sanitize_filename("  a  ") == "a"
    assert sanitize_filename("...") == "unnamed"
    assert sanitize_filename("a/b\\c:d") == "a_b_c_d"
    assert sanitize_filename("") == "unnamed"


def test_verifier_and_corpus_free_of_collectors_dependency():
    """The adjudicator must not silently inherit mutable main-line
    semantics: nothing it exposes may come from collectors.*, and the
    frozen margin constant is the deployed 6h."""
    import tools.a3_efd2268_compat as compat
    import tools.verify_a3_window as v
    for mod in (v, compat):
        assert not any(
            "collectors" in (getattr(m, "__module__", "") or "")
            for m in vars(mod).values())
    assert compat.UNVERIFIED_TS_MARGIN == UNVERIFIED_TS_MARGIN


# ==================================================== v5: sealed profile

def test_gate_setup_day_mandatory(window):
    """Omitting setup_day cannot emit the gate verdict: the deterministic
    six-candidate fallback is a preregistered decision, not optional."""
    rep = window.verify(setup=None)
    assert rep["verdict"] != "COMPLETE_SUPPORTED"
    assert any("setup_day" in a for a in rep["anomalies"])
    assert rep["profile"]["parameters_match"] is False


def test_gate_tail_mandatory(window):
    rep = window.verify(tail=None)
    assert rep["verdict"] != "COMPLETE_SUPPORTED"
    assert any("evidence_tail_end" in a for a in rep["anomalies"])


def test_gate_five_direct_days_cannot_bypass_setup(window):
    """Five direct candidate days without the setup-day mechanism is a
    different adjudication than the preregistered one — rejected."""
    rep = window.verify(candidates=CANDIDATES[1:], setup=None)
    assert rep["verdict"] != "COMPLETE_SUPPORTED"


def test_gate_modified_window_bounds_rejected(window):
    rep = window.verify(start=W0 + timedelta(hours=1))
    assert rep["verdict"] != "COMPLETE_SUPPORTED"
    assert any("profile" in a for a in rep["anomalies"])


def test_gate_modified_candidates_rejected(window):
    rep = window.verify(candidates=["2026-09-16", "2026-09-17",
                                    "2026-09-18", "2026-09-21",
                                    "2026-09-22", "2026-09-24"])
    assert rep["verdict"] != "COMPLETE_SUPPORTED"
    assert any("candidate_days" in a for a in rep["anomalies"])


def test_profile_identity_and_fingerprint_in_report(window):
    rep = window.verify()
    assert rep["profile"]["id"] == "g0-a3-es-corporate-bonds-2026-09"
    assert rep["profile"]["sha256"] == PROFILE_SHA256
    assert rep["profile"]["mode"] == "gate"
    assert rep["profile"]["parameters_match"] is True
    fp = rep["descriptor_fingerprints"]
    assert fp["bme_apa.yaml"]["match"] is True
    assert fp["bloomberg_apae.yaml"]["match"] is True
    assert fp["bme_apa.yaml"]["expected_sha256"] == \
        fp["bme_apa.yaml"]["observed_sha256"]


def test_descriptor_fingerprint_mismatch_is_review(window):
    """A modified acquisition endpoint changes which provenance hosts are
    acceptable — the exact-byte efd2268 fingerprint must catch it."""
    c = window
    p = c.cfg / "bme_apa.yaml"
    p.write_bytes(p.read_bytes().replace(
        b"www.bolsasymercados.es", b"www.bolsasymercados.evil", 1))
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("fingerprint" in a for a in rep["anomalies"])
    assert rep["descriptor_fingerprints"]["bme_apa.yaml"]["match"] is False


def test_extra_descriptor_is_review(window):
    (window.cfg / "extra.yaml").write_text(
        yaml.safe_dump({"source_id": "extra_src", "entrypoint": {}}),
        encoding="utf-8")
    rep = window.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("unexpected source descriptors" in a
               for a in rep["anomalies"])


# ============================================ v5: setup-day non-circularity

def test_sep23_fault_does_not_block_setup_proof(window):
    """A Bloomberg discover failure on Sep-23 — a candidate that enters
    counted days ONLY via fallback — must NOT make Sep-16 fail its setup
    proof. (v4 circular dependency: fault on day N could force fallback
    onto day N and make itself gate-relevant -> false FAIL.)"""
    c = window
    ft = datetime(2026, 9, 23, 10, 12, 46, tzinfo=UTC)
    _fail_discover(c, BLB, ft)
    rep = c.verify()
    assert rep["setup_day"]["result"] == "SETUP_DAY_CAPTURE_COMPLETE"
    assert rep["counted_days"] == CANDIDATES[:5]
    assert rep["fallback_applied"] is False
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BLB and r["run_id"] == rid(ft)]
    assert disc[0]["gate_relevant"] is False
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_setup_proof_blocked_by_setup_day_fault(window):
    """Inverse: an unrecovered fetch fault on a Sep-16 Bloomberg object
    still blocks the setup proof (affected_day == setup day)."""
    c = window
    key = tok(datetime(2026, 9, 16, 10, 0, tzinfo=UTC), 7)
    ft = datetime(2026, 9, 16, 11, 12, 46, tzinfo=UTC)
    for m in c.mans:
        if m["run_id"] == rid(ft) and m["results"]["source_id"] == BLB:
            m["results"]["status"] = "PARTIAL"
            m["results"]["errors"] = [
                f"fetch {key}: exhausted 3 attempt(s)"]
    for r in c.jrows:
        if r["run_id"] == rid(ft) and r.get("source_id") == BLB:
            r["status"] = "PARTIAL"
            r["errors"] = [f"fetch {key}: exhausted 3 attempt(s)"]
    rep = c.verify()
    assert rep["setup_day"]["result"] == "NOT_COUNTED_SETUP_DAY"
    assert rep["fallback_applied"] is True
    # with Sep-16 not counted, its faults are not gate-relevant either
    fault = [r for r in rep["recovery"] if r.get("key") == key]
    assert fault[0]["gate_relevant"] is False


# ===================================== v5: first-write observational meta

def test_meta_observational_fields_are_first_write(tmp_path):
    """First-write meta keeps provenance URL A / pub A; a later
    ALREADY_PRESENT observation carries legitimate observational
    metadata B for identical bytes. Identity still reconciles; the
    observational fields are compared only against the first-write
    record, never blindly against every manifest."""
    c = Corpus(tmp_path)
    smoke = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)
    key = bme_key("2026-09-16")
    rec_a = store_capture(
        c.raw, source_id=BME, collection_date="2026-09-16",
        object_key=key, data=b"synthetic:" + key.encode(),
        publication_timestamp="2026-09-16",
        provenance_url="https://www.bolsasymercados.es/dam/path-a.json",
        observation_utc=iso(smoke))
    # the smoke manifest records the actual first write (metadata A)
    c.run(smoke, objs=[rec_a], sources=[BME])
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    # a later ALREADY_PRESENT observation reports different (legitimate)
    # observational metadata for the same bytes — same host, other path
    for m in c.mans:
        res = m["results"]
        if res["source_id"] != BME:
            continue
        for o in res["objects"]:
            if o["source_object_id"] == key \
                    and o["status"] == "ALREADY_PRESENT":
                o["provenance_url"] = ("https://www.bolsasymercados.es"
                                       "/en/other-services/b.json")
                o["publication_timestamp"] = "2026-09-16T00:00:01+00:00"
                break
        else:
            continue
        break
    rep = c.verify()
    assert rep["anomalies"] == []
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]


def test_meta_first_write_record_mismatch_flagged(tmp_path):
    """When the bundle DOES contain the first-write manifest, meta's
    observational fields must reconcile with that record."""
    c = Corpus(tmp_path)
    smoke = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)
    key = bme_key("2026-09-16")
    rec_a = store_capture(
        c.raw, source_id=BME, collection_date="2026-09-16",
        object_key=key, data=b"synthetic:" + key.encode(),
        publication_timestamp="2026-09-16",
        provenance_url="https://www.bolsasymercados.es/dam/path-a.json",
        observation_utc=iso(smoke))
    # the first-write manifest claims a different provenance than meta
    rec_a = dict(rec_a, provenance_url=(
        "https://www.bolsasymercados.es/dam/path-b.json"))
    c.run(smoke, objs=[rec_a], sources=[BME])
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("first-write" in a for a in rep["anomalies"])


# ================================================ v5: structural scoping

def test_pre_window_duplicate_journal_row_is_diagnostic(window):
    """A clearly timestamped pre-window duplicate is diagnostic — it
    cannot contaminate the active window."""
    c = window
    pre = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)
    row = {"run_id": rid(pre), "observed_at": iso(pre), "source_id": BME,
           "mode": "poll", "status": "SUCCEEDED", "window_hours": 24}
    c.jrows.extend([dict(row), dict(row)])   # duplicate pair, pre-window
    rep = c.verify()
    assert rep["verdict"] == "COMPLETE_SUPPORTED", rep["verdict_reasons"]
    assert any("duplicate" in d
               for d in rep["diagnostics"]["out_of_window_structural"])


def test_in_window_duplicate_still_review(window):
    """Control: the same duplicate inside the eligible window stays a
    gate-relevant anomaly."""
    c = window
    dup = dict(c.jrows[0])
    dup["mode"] = "rolling"
    c.jrows.append(dup)
    rep = c.verify()
    assert rep["verdict"] == "REVIEW_REQUIRED"
    assert any("duplicate" in a for a in rep["anomalies"])


def test_malformed_unscopable_journal_line_is_review(window):
    """A record with NO parseable time cannot be scoped safely — it stays
    an anomaly even though it might be pre-window."""
    c = window
    c.flush()
    with c.journal.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    rep = verify(c.journal, c.runs, c.raw, c.cfg, iso(W0), iso(W1),
                 CANDIDATES, SETUP, None, iso(TAIL))
    assert rep["verdict"] == "REVIEW_REQUIRED"


# ========================================== v5: BME temporal recovery

def _strip_observations(c, source, key, after):
    """Remove manifest observations of `key` strictly after `after`,
    dropping raw copies those observations alone created."""
    for m in c.mans:
        res = m["results"]
        if res["source_id"] != source:
            continue
        t = datetime.fromisoformat(res["observed_at"])
        if t <= after:
            continue
        for o in res["objects"]:
            if o["source_object_id"] == key and o["status"] == "CREATED":
                c.drop_raw(source, o["capture_version"],
                           res["observed_at"][:10])
        res["objects"] = [o for o in res["objects"]
                          if o["source_object_id"] != key]
        res["object_count"] = len(res["objects"])


def test_bme_discover_pre_failure_only_is_not_recovery(tmp_path):
    """The at-risk daily file exists ONLY in pre-failure observations:
    that proves 'no known loss' of that snapshot, not post-failure
    coverage — classified explicitly, never RECOVERED."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    _fail_discover(c, BME, ft)
    _strip_observations(c, BME, bme_key("2026-09-21"), ft)
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BME and r["run_id"] == rid(ft)]
    assert disc[0]["state"] == \
        "AT_RISK_OBJECT_ALREADY_CAPTURED_BEFORE_FAILURE"
    assert disc[0]["days_pre_failure_only"] == ["2026-09-21"]
    assert rep["verdict"] != "COMPLETE_SUPPORTED"


def test_bme_discover_recovered_by_later_new_version(tmp_path):
    """A post-failure observation with a NEW capture_version (the file
    mutated) demonstrates recovery just as ALREADY_PRESENT does."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    _fail_discover(c, BME, ft)
    nt = datetime(2026, 9, 21, 11, 12, 46, tzinfo=UTC)
    obj = c.capture(BME, bme_key("2026-09-21"), nt, data=b"v2-bytes")
    for m in c.mans:
        if m["run_id"] == rid(nt) and m["results"]["source_id"] == BME:
            m["results"]["objects"].append(obj)
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BME and r["run_id"] == rid(ft)]
    assert disc[0]["state"] == "RECOVERED"


def test_bme_discover_missing_daily_file_unrecovered(tmp_path):
    """The at-risk counted daily file is never observed at all:
    UNRECOVERED."""
    c = Corpus(tmp_path)
    fill(c, W0, TAIL_FILL, CANDIDATES)
    make_setup_proof(c)
    ft = datetime(2026, 9, 21, 10, 12, 46, tzinfo=UTC)
    _fail_discover(c, BME, ft)
    _strip_observations(c, BME, bme_key("2026-09-21"),
                        datetime(2026, 9, 20, tzinfo=UTC))
    rep = c.verify()
    disc = [r for r in rep["recovery"] if r["kind"] == "discover"
            and r["source"] == BME and r["run_id"] == rid(ft)]
    assert disc[0]["state"] == "UNRECOVERED"
    assert rep["verdict"] != "COMPLETE_SUPPORTED"
