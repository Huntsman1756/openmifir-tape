"""Offline tests for G0-D1 lifecycle reconstruction + F-006 composition proof."""

from normalization import normalize_record
from lifecycle import (
    build_ledger,
    economic_duplicate_candidates,
    ledger_digest,
    reconstruct_chains,
    source_exact_duplicates,
    composition_proof,
)


def _bme(**overrides):
    rec = {
        "instrument_identification_code": "ES0000000001",
        "venue_of_execution": "XOFF",
        "mifir_identifier": "CRPB",
        "price_currency": "EUR",
        "price_notation": "PERC",
        "notation_of_the_quantity_in_measurement_unit": "UNIT",
        "trading_date_and_time": "2026-09-14T10:00:00.000Z",
        "price": "98.50",
        "missing_price": "false",
        "quantity": "100000",
        "notional_amount": "98500",
        "notional_currency": "EUR",
        "transaction_identification_code": "TX-1",
        "venue_of_publication": "BMEA",
        "quantity_in_measurement_unit": "100000",
        "publication_date_and_time": "2026-09-14T10:00:05.000Z",
        "benc": "false", "tpac": "false", "sdiv": "false", "entr": "false",
        "amnd": "false", "canc": "false", "xfph": "false", "lrgs": "false",
        "size": "", "ilqd": "false", "port": "false", "cont": "false",
        "npft": "false", "post_trade_deferral": "false", "flags": [],
        "instrument_id_code": "ISIN", "reception_date_time": "2026-09-14T10:00:05.000Z",
    }
    rec.update(overrides)
    return rec


def _norm(rec):
    return normalize_record("bme_apa", rec)


def test_ledger_is_append_only_with_supersession():
    recs = [
        _norm(_bme(transaction_identification_code="TX-1", publication_date_and_time="2026-09-14T10:00:05.000Z")),
        _norm(_bme(transaction_identification_code="TX-1", publication_date_and_time="2026-09-14T10:01:00.000Z",
                   flags=["amnd"], amnd="true", price="98.40")),
    ]
    entries = build_ledger(recs)
    assert len(entries) == 2
    e0, e1 = entries
    assert e0.state == "FULLY_PUBLISHED"
    assert e1.state == "AMENDED"
    assert e1.supersedes_source_report_id == e0.entry_id
    assert e0.supersedes_source_report_id is None


def test_cancellation_state():
    rec = _norm(_bme(transaction_identification_code="TX-2", publication_date_and_time="2026-09-14T11:00:00.000Z",
                     flags=["canc"], canc="true"))
    entries = build_ledger([rec])
    assert entries[0].state == "CANCELLED"


def test_ledger_digest_deterministic_and_order_independent():
    recs = [_norm(_bme(transaction_identification_code=f"TX-{i}", publication_date_and_time=f"2026-09-14T10:00:0{i}.000Z"))
            for i in range(3)]
    d1 = ledger_digest(build_ledger(list(recs)))
    d2 = ledger_digest(build_ledger(list(reversed(recs))))  # different input order
    d3 = ledger_digest(build_ledger(list(recs)))
    assert d1 == d3
    assert d1 == d2  # order-independent


def test_no_heuristic_deletion_on_source_exact_duplicate():
    rec = _norm(_bme(transaction_identification_code="TX-3", publication_date_and_time="2026-09-14T10:00:00.000Z"))
    recs = [rec, rec]  # identical source-exact publication observed twice
    dups = source_exact_duplicates(recs)
    assert len(dups) == 2
    entries = build_ledger(recs)
    # both observations preserved (append-only), none deleted
    assert len(entries) == 2
    # economic candidates only flag, never delete
    cands = economic_duplicate_candidates(recs)
    assert cands  # grouped as candidate


def test_economic_duplicate_candidate_flagged_not_removed():
    a = _norm(_bme(transaction_identification_code="TX-A", instrument_identification_code="ES0000000009"))
    b = _norm(_bme(transaction_identification_code="TX-B", instrument_identification_code="ES0000000009"))
    cands = economic_duplicate_candidates([a, b])
    assert len(cands) == 1  # same instrument+quantity+price+trading time across trades
    # not removed: build_ledger keeps both
    assert len(build_ledger([a, b])) == 2


def test_composition_proof_deterministic_f006():
    raw = _bme(transaction_identification_code="TX-4", publication_date_and_time="2026-09-14T12:00:00.000Z")
    p1 = composition_proof([("bme_apa", raw)], normalize_record)
    p2 = composition_proof([("bme_apa", dict(raw))], normalize_record)
    assert p1["ledger_semantic_digest"] == p2["ledger_semantic_digest"]
    assert p1["entry_count"] == "1"


def test_chain_reconstruction_groups_by_trade():
    recs = [
        _norm(_bme(transaction_identification_code="TX-5", publication_date_and_time="2026-09-14T10:00:00.000Z")),
        _norm(_bme(transaction_identification_code="TX-6", publication_date_and_time="2026-09-14T10:01:00.000Z")),
    ]
    chains = reconstruct_chains(build_ledger(recs))
    assert len(chains) == 2
