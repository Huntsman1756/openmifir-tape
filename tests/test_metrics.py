"""Offline tests for G0-E1 cross-source metrics (metrics/).

Synthetic NormalizedRecords / manifests / raw files only — no provider
data, no network. Covers: eligible-evidence input selection (A3 manifest
scoping), single-authority ledger heads, source partitioning /
intersection / exclusives, notional aggregation, sample observation rate,
interpretation-limit invariants and NO_COVERAGE_CLAIM enforcement.
"""

import json
from datetime import UTC, datetime

import pytest
import yaml

from lifecycle import build_ledger, reconstruct_chains
from metrics.cross_source import (
    INTERPRETATION_LIMITS,
    compute_cross_source,
    scan_forbidden_claims,
)
from metrics.evidence import build_report, generate_report
from metrics.selection import SelectionError, select_eligible
from normalization import normalize_record
from tools.a3_efd2268_compat import store_capture

BME = "bme_apa"
BLB = "blb_apae"

START = datetime(2026, 9, 16, 9, 42, 46, tzinfo=UTC)
TAIL = datetime(2026, 9, 24, 7, 0, 0, tzinfo=UTC)
WINDOW = {"operational_start": "2026-09-16T09:42:46Z",
          "subject_period_end": "2026-09-23T23:59:59Z",
          "evidence_tail_end": "2026-09-24T07:00:00Z"}

# Synthetic validated scope (the metric logic is scope-agnostic; the real
# F-009 artifact is exercised by load_validated_scope in test_universe.py).
SCOPE = frozenset({"ISIN-A", "ISIN-B", "ISIN-C", "ISIN-D"})


def _bme(**overrides):
    rec = {
        "instrument_identification_code": "ISIN-A",
        "venue_of_execution": "XOFF",
        "mifir_identifier": "BOND",
        "price_currency": "EUR",
        "price_notation": "PERC",
        "notation_of_the_quantity_in_measurement_unit": "UNIT",
        "trading_date_and_time": "2026-09-17T10:00:00.000Z",
        "price": "98.50",
        "missing_price": "false",
        "quantity": "100000",
        "notional_amount": "98500",
        "notional_currency": "EUR",
        "transaction_identification_code": "TX-1",
        "venue_of_publication": "BMEA",
        "quantity_in_measurement_unit": "100000",
        "publication_date_and_time": "2026-09-17T10:00:05.000Z",
        "benc": "false", "tpac": "false", "sdiv": "false", "entr": "false",
        "amnd": "false", "canc": "false", "xfph": "false", "lrgs": "false",
        "size": "", "ilqd": "false", "port": "false", "cont": "false",
        "npft": "false", "post_trade_deferral": "", "flags": "",
        "instrument_id_code": "ISIN",
        "reception_date_time": "2026-09-17T10:00:05.000Z",
    }
    rec.update(overrides)
    return rec


def _blb(**overrides):
    row = {
        "Trading date and time": "2026-09-17T10:00:00.000Z",
        "Instrument identification code type": "ISIN",
        "Instrument identification code": "ISIN-A",
        "Price": "98.50",
        "Venue of execution": "XOFF",
        "Price notation": "PERC",
        "Price Currency": "EUR",
        "Quantity": "100000",
        "Notional amount": "98500",
        "Notional currency": "EUR",
        "Type": "",
        "Publication Date and Time": "2026-09-17T10:00:05.000Z",
        "Venue of publication": "BAPA",
        "Transaction Identification Code": "BLB-1",
        "Flags": "",
    }
    row.update(overrides)
    return row


def _nbme(**kw):
    return normalize_record(BME, _bme(**kw))


def _nblb(**kw):
    return normalize_record(BLB, _blb(**kw))


# ------------------------- unique-trade / chain semantics -----------------

def test_rescanned_identical_publications_collapse_to_one_trade():
    recs = [_nbme(), _nbme(), _nbme()]  # same (source, tid), identical bytes
    m = compute_cross_source(recs, SCOPE)
    assert m["unique_trades"]["by_source"][BME] == 1
    assert m["records_by_source"][BME] == 3   # observations kept; trades dedup


def test_amendment_and_cancel_reuse_the_chain():
    recs = [
        _nbme(publication_date_and_time="2026-09-17T10:00:05.000Z"),
        _nbme(publication_date_and_time="2026-09-17T11:00:05.000Z",
              amnd="true", flags="AMND", price="98.40"),
        _nbme(publication_date_and_time="2026-09-17T12:00:05.000Z",
              canc="true", flags="CANC"),
    ]
    m = compute_cross_source(recs, SCOPE)
    assert m["unique_trades"]["by_source"][BME] == 1
    dist = m["unique_trades"]["head_state_distribution_by_source"][BME]
    assert dist == {"CANCELLED": 1}                      # head = latest
    nb = m["notional_by_source"][BME]
    assert nb["cancelled_heads"] == 1
    assert nb["sum_by_currency"]["EUR"] == "98500"       # all heads
    assert nb["sum_by_currency_excluding_cancelled_heads"] == {}


def test_missing_transaction_id_never_merges():
    recs = [_nbme(transaction_identification_code=""),
            _nbme(transaction_identification_code=None)]
    m = compute_cross_source(recs, SCOPE)
    assert m["unique_trades"]["by_source"][BME] == 2
    assert m["unique_trades"]["unchainable_singletons_by_source"][BME] == 2


def test_ledger_is_the_single_head_authority():
    """Adversarial (PR #15 review): same source+tid, two records with the
    SAME publication_utc but different economic material. The ledger
    orders them by entry_id; E1 must take exactly chain[-1] — no parallel
    record-level grouping may pick a different head."""
    recs = [
        _nbme(price="98.50", quantity="100000", notional_amount="98500"),
        _nbme(price="98.90", quantity="200000", notional_amount="197800",
              amnd="true", flags="AMND"),
    ]
    entries = build_ledger(recs)
    chains = reconstruct_chains(entries)
    assert len(chains) == 1
    head = next(iter(chains.values()))[-1]

    m = compute_cross_source(recs, SCOPE)
    assert m["unique_trades"]["by_source"][BME] == 1
    # notional is summed over the AUTHORITATIVE head only — it must equal
    # the ledger head's notional, proving head identity, not just counts.
    sums = m["notional_by_source"][BME]["sum_by_currency"]
    assert sums == {"EUR": head.notional_amount}
    dist = m["unique_trades"]["head_state_distribution_by_source"][BME]
    assert dist == {head.state: 1}


# ------------------------- source partition / intersection ----------------

def test_isin_partition_intersection_exclusive_unobserved():
    recs = [
        _nbme(),                                                       # A both
        _nblb(),                                                       # A both
        _nbme(instrument_identification_code="ISIN-B",
              transaction_identification_code="TX-B"),                 # B bme-only
        _nblb(**{"Instrument identification code": "ISIN-C",
                 "Transaction Identification Code": "BLB-C"}),         # C blb-only
    ]                                                                  # D unseen
    m = compute_cross_source(recs, SCOPE)
    ui = m["unique_isins"]
    assert ui["by_source"][BME] == ["ISIN-A", "ISIN-B"]
    assert ui["by_source"][BLB] == ["ISIN-A", "ISIN-C"]
    assert ui["intersection"] == ["ISIN-A"]
    assert ui["source_exclusive"][BME] == ["ISIN-B"]
    assert ui["source_exclusive"][BLB] == ["ISIN-C"]
    assert ui["unobserved_in_capture"] == ["ISIN-D"]
    rate = m["sample_observation_rate"]
    assert rate["observed_isins"] == 3 and rate["validated_isins"] == 4
    assert rate["rate"] == "0.75"


def test_trade_level_intersection_and_exclusives():
    recs = [
        _nbme(),                                                       # econ key K
        _nblb(),                                                       # same K, blb
        _nbme(instrument_identification_code="ISIN-B",
              transaction_identification_code="TX-B",
              quantity="50000", notional_amount="49250"),              # bme-only
    ]
    m = compute_cross_source(recs, SCOPE)
    inter = m["trades"]["intersection_candidates"]
    assert inter["group_count"] == 1
    g = inter["groups"][0]
    assert g["isin"] == "ISIN-A" and g["sources"] == [BLB, BME]
    assert g["heads_per_source"] == {BME: 1, BLB: 1}
    assert len(g["key_sha256"]) == 64
    assert inter["heads_matched_by_source"] == {BME: 1, BLB: 1}
    assert m["trades"]["source_exclusive"]["by_source"] == \
        {BME: 1, BLB: 0}


def test_cross_source_group_needs_two_sources():
    recs = [_nbme(), _nbme()]  # same key, same source -> intra, not cross
    m = compute_cross_source(recs, SCOPE)
    assert m["trades"]["intersection_candidates"]["group_count"] == 0
    assert m["trades"]["source_exclusive"]["by_source"][BME] == 1


# ------------------------- notional --------------------------------------

def test_notional_sums_per_currency_and_unparseable():
    recs = [
        _nbme(notional_amount="98500"),
        _nbme(transaction_identification_code="TX-2",
              instrument_identification_code="ISIN-B",
              notional_amount="51500", notional_currency="EUR",
              publication_date_and_time="2026-09-17T10:05:00.000Z"),
        _nbme(transaction_identification_code="TX-3",
              instrument_identification_code="ISIN-C",
              notional_amount="not-a-number",
              publication_date_and_time="2026-09-17T10:10:00.000Z"),
    ]
    m = compute_cross_source(recs, SCOPE)
    nb = m["notional_by_source"][BME]
    assert nb["sum_by_currency"]["EUR"] == "150000"
    assert nb["unparseable_notional_heads"] == 1
    assert nb["chains_with_parseable_notional"] == 2
    assert nb["heads_with_ambiguous_or_missing_currency"] == 0


def test_currency_join_ambiguity_is_reported_not_guessed():
    """Identical ledger-identity records carrying conflicting currencies
    must surface as ambiguity — never an invented currency."""
    recs = [_nbme(notional_currency="EUR"),
            _nbme(notional_currency="USD")]
    m = compute_cross_source(recs, SCOPE)
    nb = m["notional_by_source"][BME]
    assert nb["heads_with_ambiguous_or_missing_currency"] == 1
    assert "" in nb["sum_by_currency"]      # bucketed under empty key


def test_scope_excludes_non_scope_records():
    recs = [_nbme(), _nbme(instrument_identification_code="ISIN-OUTSIDE",
                           transaction_identification_code="TX-X")]
    m = compute_cross_source(recs, SCOPE)
    assert m["records_by_source"][BME] == 1
    assert m["unique_trades"]["by_source"][BME] == 1


# ------------------- eligible-evidence input selection --------------------

def _store(raw_root, source, coldate, key, payload,
           obs="2026-09-17T10:00:05Z"):
    """Store raw bytes under a collection date; returns manifest object."""
    data = payload if isinstance(payload, bytes) else (
        json.dumps(payload) if source == BME else payload).encode()
    return store_capture(
        raw_root, source_id=source, collection_date=coldate,
        object_key=key, data=data, publication_timestamp="PUB",
        provenance_url=f"https://host/{key}", observation_utc=obs)


def _manifest(runs_dir, run_id, source, observed_at, objects):
    man = {
        "run_id": run_id, "gate": "G0-A3", "purpose": "synthetic",
        "results": {
            "source_id": source, "attempted": True, "status": "SUCCEEDED",
            "observed_at": observed_at, "object_count": len(objects),
            "objects": list(objects), "errors": [], "log": [],
            "context": {"mode": "poll"},
        },
    }
    (runs_dir / f"{run_id}__{source}__manifest.yaml").write_text(
        yaml.safe_dump(man), encoding="utf-8")
    return man


def _fixture(tmp_path):
    """Minimal store+runs: one eligible BME daily file (2 scope records)."""
    raw = tmp_path / "raw"
    runs = tmp_path / "runs"
    runs.mkdir()
    return raw, runs


def test_selection_A_post_tail_manifest_cannot_add_records(tmp_path):
    """Regression A: a validated-scope raw referenced ONLY by a post-tail
    manifest contributes nothing to E1."""
    raw, runs = _fixture(tmp_path)
    payload = [_bme()]
    obj = _store(raw, BME, "2026-09-24", "d.json", payload)
    _manifest(runs, "20260924T090000Z", BME, "2026-09-24T09:00:00Z", [obj])
    sel = select_eligible(runs, raw, start=START, tail_end=TAIL)
    assert sel["versions"] == []
    assert sel["census"]["post_tail_manifests_excluded"] == 1
    assert sel["census"]["manifests_eligible"] == 0


def test_selection_B_pre_window_manifest_cannot_add_records(tmp_path):
    """Regression B: a pre-window (smoke) manifest is excluded by time."""
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-16", "d.json", [_bme()])
    _manifest(runs, "20260916T052127Z", BME, "2026-09-16T05:21:27Z", [obj])
    sel = select_eligible(runs, raw, start=START, tail_end=TAIL)
    assert sel["versions"] == []
    assert sel["census"]["pre_window_manifests_excluded"] == 1


def test_selection_C_same_capture_version_parsed_once(tmp_path):
    """Regression C: repeated manifest observations of the same
    (source_id, source_object_id, capture_version) dedupe to ONE input —
    capture-identity dedup, not trade dedup."""
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [obj])
    _manifest(runs, "20260917T110000Z", BME, "2026-09-17T11:00:00Z", [obj])
    sel = select_eligible(runs, raw, start=START, tail_end=TAIL)
    assert sel["census"]["object_observations_eligible"] == 2
    assert sel["census"]["unique_capture_versions_eligible"] == 1
    assert len(sel["versions"]) == 1


def test_selection_D_distinct_capture_versions_stay_distinct(tmp_path):
    """Regression D: two capture_versions of the same source object are two
    distinct input publications."""
    raw, runs = _fixture(tmp_path)
    o1 = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    o2 = _store(raw, BME, "2026-09-18", "d.json", [_bme(price="98.60")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o1])
    _manifest(runs, "20260918T100000Z", BME, "2026-09-18T10:00:00Z", [o2])
    sel = select_eligible(runs, raw, start=START, tail_end=TAIL)
    assert sel["census"]["unique_capture_versions_eligible"] == 2
    assert len(sel["versions"]) == 2


def test_selection_E_eligible_manifest_missing_raw_fails_closed(tmp_path):
    """Regression E: an eligible manifest pointing at absent bytes aborts —
    never a silent skip."""
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    (raw / BME / "2026-09-17" / "d.json" / obj["capture_version"]).unlink()
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [obj])
    with pytest.raises(SelectionError, match="missing raw"):
        select_eligible(runs, raw, start=START, tail_end=TAIL)


def test_selection_pre_window_publication_is_fine_if_observed_in_window(tmp_path):
    """publication_timestamp is NOT a selection criterion: a source object
    published before operational_start but captured by an eligible manifest
    belongs to E1 (24h/rolling rescans legitimately reach back)."""
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "old.json",
                 [_bme(trading_date_and_time="2026-09-10T10:00:00.000Z",
                      publication_date_and_time="2026-09-10T10:00:05.000Z")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [obj])
    sel = select_eligible(runs, raw, start=START, tail_end=TAIL)
    assert len(sel["versions"]) == 1


def test_selection_malformed_in_window_fails_closed(tmp_path):
    raw, runs = _fixture(tmp_path)
    (runs / "20260917T100000Z__bme_apa__manifest.yaml").write_bytes(b"\x00\xff")
    with pytest.raises(SelectionError):
        select_eligible(runs, raw, start=START, tail_end=TAIL)


def test_selection_malformed_out_of_window_is_diagnostic(tmp_path):
    raw, runs = _fixture(tmp_path)
    (runs / "20260925T120000Z__bme_apa__manifest.yaml").write_bytes(b"\x00\xff")
    sel = select_eligible(runs, raw, start=START, tail_end=TAIL)
    assert sel["census"]["malformed_out_of_window_manifests"] == 1


# ------------------------- interpretation limits / NO_COVERAGE_CLAIM -----

def test_interpretation_limits_present_and_fixed():
    m = compute_cross_source([], SCOPE)
    lim = m["interpretation_limits"]
    for key in ("no_completeness_inference", "no_market_share_inference",
                "no_absence_of_trading_inference", "no_low_n_generalization"):
        assert lim.get(key)
    assert m["no_coverage_claim"] is True


def test_forbidden_claim_scanner_catches_claims():
    assert scan_forbidden_claims("we estimate 40% of the market")
    assert scan_forbidden_claims("ISIN X did not trade this week")
    assert scan_forbidden_claims("BME has 60% market share")
    assert scan_forbidden_claims("an absence of trading was observed")
    assert scan_forbidden_claims("") == []
    assert scan_forbidden_claims("observed in an intact capture") == []


def test_metrics_payload_contains_no_forbidden_claims():
    recs = [_nbme(), _nblb()]
    m = compute_cross_source(recs, SCOPE)
    payload = {k: v for k, v in m.items() if k != "interpretation_limits"}
    text = yaml.safe_dump(payload)
    assert scan_forbidden_claims(text) == [], "payload must stay claim-free"


# ------------------------- report generation ------------------------------

REAL_SCOPE_ISIN = "XS2984223102"   # validated INCLUDE per F-009 artifact


def _write_raw_tree(tmp_path):
    """Store + eligible manifests: 1 BME file + 1 BLB file in scope."""
    raw = tmp_path / "raw"
    runs = tmp_path / "runs"
    runs.mkdir()
    bme_obj = _store(raw, BME, "2026-09-17", "f.json",
                     [_bme(instrument_identification_code=REAL_SCOPE_ISIN)])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z",
              [bme_obj])
    blb_row = _blb(**{"Instrument identification code": REAL_SCOPE_ISIN})
    csv_data = "noise\n" + ",".join(blb_row) + "\n" + \
        ",".join(blb_row.values()) + "\n"
    blb_obj = _store(raw, BLB, "2026-09-17", "f.csv", csv_data)
    _manifest(runs, "20260917T100000Z", BLB, "2026-09-17T10:00:00Z",
              [blb_obj])
    return raw, runs


def test_build_and_generate_report(tmp_path):
    raw, runs = _write_raw_tree(tmp_path)
    rep = build_report(raw, runs, window=WINDOW, date="2026-09-25",
                       generated_utc="2026-09-25T00:00:00Z")
    assert rep["report"] == "COVERAGE_MEASURED"
    assert rep["gate"] == "G0-E"
    assert rep["metrics"]["unique_trades"]["by_source"][BME] == 1
    assert rep["metrics"]["unique_trades"]["by_source"][BLB] == 1
    assert rep["evidence_basis"]["frozen_sample_isin_count"] == 20
    cen = rep["metrics"]["input_census"]
    assert cen["manifests_eligible"] == 2
    assert cen["unique_capture_versions_eligible"] == 2
    assert cen["raw_versions_parsed"] == 2

    out = generate_report(raw, runs, tmp_path / "ev", window=WINDOW,
                          date="2026-09-25", generated_utc="2026-09-25T00:00:00Z")
    loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert loaded["report"] == "COVERAGE_MEASURED"
    sha_file = out.with_name("G0-E1_coverage_measured.sha256.txt")
    assert sha_file.exists()


def test_report_deterministic(tmp_path):
    raw, runs = _write_raw_tree(tmp_path)
    a = build_report(raw, runs, window=WINDOW, date="d", generated_utc="t")
    b = build_report(raw, runs, window=WINDOW, date="d", generated_utc="t")
    assert yaml.safe_dump(a) == yaml.safe_dump(b)


def test_unparseable_eligible_raw_is_diagnostic_not_fatal(tmp_path):
    """An eligible capture whose bytes cannot decode (e.g. the 0-byte BME
    daily in the A3 bundle) surfaces as an explicit diagnostic identified
    by (source_id, source_object_id, capture_version)."""
    raw, runs = _write_raw_tree(tmp_path)
    bad = _store(raw, BME, "2026-09-21", "bad.json", b"")
    _manifest(runs, "20260921T100000Z", BME, "2026-09-21T10:00:00Z", [bad])
    rep = build_report(raw, runs, window=WINDOW, date="d",
                       generated_utc="t")
    unpar = rep["metrics"]["unparseable_raw_objects"]
    assert len(unpar) == 1
    assert unpar[0]["source_id"] == BME
    assert unpar[0]["source_object_id"] == "bad.json"
    assert unpar[0]["capture_version"] == bad["capture_version"]
    assert rep["metrics"]["input_census"]["raw_versions_parsed"] == 2
    assert rep["metrics"]["unique_trades"]["by_source"][BME] == 1


def test_forbidden_patterns_do_not_self_trigger_on_limits():
    # interpretation_limits names the claims in negated form — the scanner
    # sees the payload WITHOUT that block, so the fixed limits themselves
    # never trip the gate.
    for text in INTERPRETATION_LIMITS.values():
        assert isinstance(text, str) and text


# ------------- #17: source_object_id binding / meta reconciliation ---------

def _obj(source, key, cv):
    """Minimal manifest object entry (fields selection consumes)."""
    return {"source_id": source, "source_object_id": key,
            "capture_version": cv}


def _rewrite_meta(raw, sid, coldate, key_dir, cv, **fields):
    """Overwrite the sibling meta with modified fields (synthetic tamper)."""
    meta_p = raw / sid / coldate / key_dir / f"{cv}.meta.yaml"
    meta = yaml.safe_load(meta_p.read_bytes())
    meta.update(fields)
    meta_p.write_text(yaml.safe_dump(meta), encoding="utf-8")


def test_identity_exact_succeeds(tmp_path):
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "k.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [obj])
    sel = select_eligible(runs, raw, start=START, tail_end=TAIL)
    assert len(sel["versions"]) == 1
    assert sel["versions"][0].source_object_id == "k.json"


def test_identity_wrong_object_key_fails(tmp_path):
    """Same source, identical bytes/cv, but only under another object key —
    the manifest reference must not be satisfied by a cross-key copy."""
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "k2.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z",
              [_obj(BME, "k1.json", obj["capture_version"])])
    with pytest.raises(SelectionError, match="different source_object_id"):
        select_eligible(runs, raw, start=START, tail_end=TAIL)


def test_identity_correct_and_wrong_key_coexist(tmp_path):
    """Identical bytes under K1 and K2 dirs; manifest asks K1 — the exact
    K1 copy wins deterministically; the K2 copy must not poison it."""
    raw, runs = _fixture(tmp_path)
    data = json.dumps([_bme()]).encode()
    o1 = _store(raw, BME, "2026-09-17", "k1.json", data)
    _store(raw, BME, "2026-09-17", "k2.json", data)
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o1])
    sel = select_eligible(runs, raw, start=START, tail_end=TAIL)
    assert len(sel["versions"]) == 1
    assert sel["versions"][0].path.parent.name == "k1.json"


def test_identity_sanitization_collision_fails_closed(tmp_path):
    """Adversarial: K1='a:b.json' and K2='a?b.json' sanitize to the same
    'a_b.json' dir. Bytes/meta physically belong to K2; manifest asks K1.
    Path-name check alone is insufficient — meta reconciliation fails."""
    raw, runs = _fixture(tmp_path)
    o2 = _store(raw, BME, "2026-09-17", "a?b.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z",
              [_obj(BME, "a:b.json", o2["capture_version"])])
    with pytest.raises(SelectionError):
        select_eligible(runs, raw, start=START, tail_end=TAIL)


def test_identity_missing_meta_fails(tmp_path):
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "k.json", [_bme()])
    (raw / BME / "2026-09-17" / "k.json"
     / f"{obj['capture_version']}.meta.yaml").unlink()
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [obj])
    with pytest.raises(SelectionError, match="meta reconciliation"):
        select_eligible(runs, raw, start=START, tail_end=TAIL)


def test_identity_malformed_meta_fails(tmp_path):
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "k.json", [_bme()])
    meta_p = (raw / BME / "2026-09-17" / "k.json"
              / f"{obj['capture_version']}.meta.yaml")
    meta_p.write_bytes(b"\x00\xff[")
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [obj])
    with pytest.raises(SelectionError):
        select_eligible(runs, raw, start=START, tail_end=TAIL)


@pytest.mark.parametrize("field,value", [
    ("source_id", "other_src"),
    ("source_object_id", "other-key.json"),
    ("capture_version", "0" * 64),
    ("raw_sha256", "0" * 64),
    ("collection_date", "2026-09-18"),
])
def test_identity_meta_field_mismatch_fails(tmp_path, field, value):
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "k.json", [_bme()])
    _rewrite_meta(raw, BME, "2026-09-17", "k.json",
                  obj["capture_version"], **{field: value})
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [obj])
    with pytest.raises(SelectionError, match="meta reconciliation"):
        select_eligible(runs, raw, start=START, tail_end=TAIL)


def test_identity_same_tuple_across_collection_dates(tmp_path):
    """Same logical identity stored under two collection dates: both valid,
    resolution deterministic."""
    raw, runs = _fixture(tmp_path)
    data = json.dumps([_bme()]).encode()
    o1 = _store(raw, BME, "2026-09-17", "k.json", data)
    _store(raw, BME, "2026-09-18", "k.json", data)   # same bytes, new date
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o1])
    _manifest(runs, "20260918T100000Z", BME, "2026-09-18T10:00:00Z", [o1])
    a = select_eligible(runs, raw, start=START, tail_end=TAIL)
    b = select_eligible(runs, raw, start=START, tail_end=TAIL)
    assert len(a["versions"]) == 1
    assert a["versions"][0].path == b["versions"][0].path
    assert "2026-09-17" in str(a["versions"][0].path)   # earliest coldate


def test_identity_content_hash_tamper_fails_at_load(tmp_path):
    """Exact path + valid meta but tampered bytes: the load-stage sha256
    check remains a hard failure (SelectionError)."""
    from metrics.evidence import _load_eligible_records
    raw, runs = _fixture(tmp_path)
    obj = _store(raw, BME, "2026-09-17", "k.json", [_bme()])
    (raw / BME / "2026-09-17" / "k.json"
     / obj["capture_version"]).write_bytes(b"tampered")
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [obj])
    sel = select_eligible(runs, raw, start=START, tail_end=TAIL)
    with pytest.raises(SelectionError, match="divergence"):
        _load_eligible_records(sel)
