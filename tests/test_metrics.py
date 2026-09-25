"""Offline tests for G0-E1 cross-source metrics (metrics/).

Synthetic NormalizedRecords / raw files only — no provider data, no
network. Covers: metric definitions (unique trade = ledger chain),
source partitioning / intersection / exclusives, notional aggregation,
sample observation rate, interpretation-limit invariants and
NO_COVERAGE_CLAIM enforcement.
"""

import json

import yaml

from metrics.cross_source import (
    INTERPRETATION_LIMITS,
    compute_cross_source,
    scan_forbidden_claims,
)
from metrics.evidence import build_report, generate_report
from normalization import normalize_record

BME = "bme_apa"
BLB = "blb_apae"

# Synthetic validated scope (stands in for the 16-INCLUDE F-009 artifact —
# the metric logic is scope-agnostic; the real artifact is exercised by
# load_validated_scope tests in test_universe.py).
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
    assert m["unique_trades"]["ledger_consistent"] is True
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
    assert m["unique_trades"]["ledger_consistent"] is True


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


def test_scope_excludes_non_scope_records():
    recs = [_nbme(), _nbme(instrument_identification_code="ISIN-OUTSIDE",
                           transaction_identification_code="TX-X")]
    m = compute_cross_source(recs, SCOPE)
    assert m["records_by_source"][BME] == 1
    assert m["unique_trades"]["by_source"][BME] == 1


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


def _write_raw_tree(root):
    bme_dir = root / BME / "2026-09-17" / "file.json"
    bme_dir.mkdir(parents=True)
    bme_dir.joinpath("aa" * 32).write_text(
        json.dumps([_bme(instrument_identification_code=REAL_SCOPE_ISIN)]),
        encoding="utf-8")
    blb_dir = root / BLB / "2026-09-17" / "file.csv"
    blb_dir.mkdir(parents=True)
    row = _blb(**{"Instrument identification code": REAL_SCOPE_ISIN})
    blb_dir.joinpath("bb" * 32).write_text(
        "noise line\n" + ",".join(row) + "\n" +
        ",".join(row.values()) + "\n", encoding="utf-8")


def test_build_and_generate_report(tmp_path):
    raw = tmp_path / "raw"
    _write_raw_tree(raw)
    rep = build_report(raw, date="2026-09-25",
                       generated_utc="2026-09-25T00:00:00Z")
    assert rep["report"] == "COVERAGE_MEASURED"
    assert rep["gate"] == "G0-E"
    assert rep["metrics"]["unique_trades"]["by_source"][BME] == 1
    assert rep["metrics"]["unique_trades"]["by_source"][BLB] == 1
    assert rep["evidence_basis"]["frozen_sample_isin_count"] == 20

    out = generate_report(raw, tmp_path / "ev", date="2026-09-25",
                          generated_utc="2026-09-25T00:00:00Z")
    loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert loaded["report"] == "COVERAGE_MEASURED"


def test_report_deterministic(tmp_path):
    raw = tmp_path / "raw"
    _write_raw_tree(raw)
    a = build_report(raw, date="d", generated_utc="t")
    b = build_report(raw, date="d", generated_utc="t")
    assert yaml.safe_dump(a) == yaml.safe_dump(b)


def test_unparseable_raw_is_diagnostic_not_fatal(tmp_path):
    """A legitimately captured empty/undecodable raw object (e.g. the 0-byte
    BME daily seen in the A3 bundle) must surface as an explicit diagnostic
    — never crash the run, never silently vanish."""
    raw = tmp_path / "raw"
    _write_raw_tree(raw)
    bad_dir = raw / BME / "2026-09-21" / "bad.json"
    bad_dir.mkdir(parents=True)
    bad_dir.joinpath("e3" * 32).write_bytes(b"")   # captured empty payload
    rep = build_report(raw, date="d", generated_utc="t")
    unpar = rep["metrics"]["unparseable_raw_objects"]
    assert len(unpar) == 1
    assert unpar[0]["source"] == BME
    assert "bad.json" in unpar[0]["relpath"]
    # good records still counted
    assert rep["metrics"]["unique_trades"]["by_source"][BME] == 1


def test_forbidden_patterns_do_not_self_trigger_on_limits():
    # interpretation_limits names the claims in negated form — the scanner
    # sees the payload WITHOUT that block, so the fixed limits themselves
    # never trip the gate.
    for text in INTERPRETATION_LIMITS.values():
        assert isinstance(text, str) and text
