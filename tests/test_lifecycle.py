"""Offline tests for G0-D1 lifecycle reconstruction + F-006 composition proof."""

from normalization import normalize_record
from lifecycle import (
    build_ledger,
    chain_state_sequence,
    economic_duplicate_candidates,
    ledger_digest,
    ledger_stats,
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
        "npft": "false", "post_trade_deferral": "", "flags": "",
        "instrument_id_code": "ISIN", "reception_date_time": "2026-09-14T10:00:05.000Z",
    }
    rec.update(overrides)
    return rec


def _blb(**overrides):
    row = {
        "Trading date and time": "2026-09-15T02:04:56.047000Z",
        "Instrument identification code type": "ISIN",
        "Instrument identification code": "AU3CB0335230",
        "Price": "98.9",
        "Venue of execution": "XOFF",
        "Price notation": "PERC",
        "Price Currency": "AUD",
        "Quantity": "",
        "Notional amount": "200000",
        "Notional currency": "AUD",
        "Type": "",
        "Publication Date and Time": "2026-09-15T02:05:05.529712Z",
        "Venue of publication": "BAPA",
        "Transaction Identification Code": "BLB-1",
        "Flags": "",
    }
    row.update(overrides)
    return row


def _norm(rec):
    return normalize_record("bme_apa", rec)


def test_ledger_is_append_only_with_supersession():
    recs = [
        _norm(_bme(transaction_identification_code="TX-1", publication_date_and_time="2026-09-14T10:00:05.000Z")),
        _norm(_bme(transaction_identification_code="TX-1", publication_date_and_time="2026-09-14T10:01:00.000Z",
                   flags="AMND", amnd="true", price="98.40")),
    ]
    entries = build_ledger(recs)
    assert len(entries) == 2
    e0, e1 = entries
    assert e0.state == "FULLY_PUBLISHED"
    assert e1.state == "AMENDED"
    assert e1.supersedes_entry_id == e0.entry_id
    assert e0.supersedes_entry_id is None


def test_cancellation_state():
    rec = _norm(_bme(transaction_identification_code="TX-2", publication_date_and_time="2026-09-14T11:00:00.000Z",
                     flags="CANC", canc="true"))
    entries = build_ledger([rec])
    assert entries[0].state == "CANCELLED"


def test_cancellation_from_boolean_field_without_flag():
    # Observed in the real corpus: canc=True with an empty flags field.
    rec = _norm(_bme(transaction_identification_code="TX-2b", canc="true", flags=""))
    entries = build_ledger([rec])
    assert entries[0].state == "CANCELLED"
    assert entries[0].event_type == "CANCEL"


def test_deferred_state_from_deferral_code():
    # post_trade_deferral carries the MMT 4.1 reason letter (A -> MLF1).
    rec = _norm(_bme(transaction_identification_code="TX-D",
                     post_trade_deferral="A", flags="MLF1"))
    entries = build_ledger([rec])
    assert entries[0].state == "DEFERRED"
    assert entries[0].event_type == "DEFER"


def test_partial_publication_via_volume_omission_under_deferral():
    # Observed pattern: DEFF publications omit the whole volume block.
    raw = _bme(transaction_identification_code="TX-P",
               post_trade_deferral="G", flags="DEFF", mifir_identifier="SFPS")
    del raw["quantity"], raw["notional_amount"], raw["quantity_in_measurement_unit"]
    rec = _norm(raw)
    entries = build_ledger([rec])
    assert entries[0].state == "PARTIALLY_PUBLISHED"
    assert entries[0].event_type == "PARTIAL_PUBLISH"


def test_partial_publication_via_missing_field_indicator():
    rec = _norm(_bme(transaction_identification_code="TX-P2",
                     post_trade_deferral="A", flags="MLF1", missing_quantity="true"))
    entries = build_ledger([rec])
    assert entries[0].state == "PARTIALLY_PUBLISHED"


def test_deferred_with_full_details_is_not_partial():
    rec = _norm(_bme(transaction_identification_code="TX-D2",
                     post_trade_deferral="C", flags="LLF3", quantity="22000"))
    entries = build_ledger([rec])
    assert entries[0].state == "DEFERRED"


def test_unresolved_when_signals_absent_fail_closed():
    # A row whose schema lacks every lifecycle signal cannot be given a state.
    row = _blb()
    del row["Flags"], row["Type"]
    rec = normalize_record("blb_apae", row)
    entries = build_ledger([rec])
    assert entries[0].state == "UNRESOLVED"
    assert "STATE_UNRESOLVED" in entries[0].markers


def test_contradictory_signals_fail_closed():
    rec = _norm(_bme(transaction_identification_code="TX-X", canc="true", amnd="true"))
    entries = build_ledger([rec])
    assert entries[0].state == "UNRESOLVED"
    assert "CONTRADICTORY_STATE_SIGNALS" in entries[0].markers


def test_missing_transaction_id_is_unchainable_singleton():
    recs = [
        _norm(_bme(transaction_identification_code=None, publication_date_and_time="2026-09-14T10:00:05.000Z")),
        _norm(_bme(transaction_identification_code=None, publication_date_and_time="2026-09-14T10:01:00.000Z",
                   price="98.40")),
    ]
    entries = build_ledger(recs)
    assert all("UNCHAINABLE" in e.markers for e in entries)
    assert all(e.supersedes_entry_id is None for e in entries)
    chains = reconstruct_chains(entries)
    assert len(chains) == 2  # two singletons; never one shared "" chain
    stats = ledger_stats(entries)
    assert stats["unchainable_record_count"] == 2


def test_unchainable_never_infers_identity_from_empty_id():
    recs = [
        _norm(_bme(transaction_identification_code="", publication_date_and_time="2026-09-14T10:00:05.000Z")),
        _norm(_bme(transaction_identification_code="  ", publication_date_and_time="2026-09-14T10:00:06.000Z",
                   price="99")),
        _norm(_bme(transaction_identification_code="TX-9", publication_date_and_time="2026-09-14T10:00:07.000Z")),
    ]
    chains = reconstruct_chains(build_ledger(recs))
    assert len(chains) == 3


def test_entry_id_collision_disambiguated_deterministically():
    rec = _norm(_bme(transaction_identification_code="TX-3", publication_date_and_time="2026-09-14T10:00:00.000Z"))
    entries = build_ledger([rec, rec])  # identical observation captured twice
    assert len(entries) == 2
    assert entries[0].entry_id != entries[1].entry_id
    assert all("ENTRY_ID_COLLISION" in e.markers for e in entries)
    assert ledger_stats(entries)["ledger_entry_id_collision_count"] == 2
    # deterministic: same multiset of ids regardless of input order
    entries2 = build_ledger([rec, rec])
    assert {e.entry_id for e in entries} == {e.entry_id for e in entries2}


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
    assert cands[0]["scope"] == "intra_source"
    assert cands[0]["sources"] == ["bme_apa"]
    # not removed: build_ledger keeps both
    assert len(build_ledger([a, b])) == 2


def test_economic_duplicate_cross_source_scope():
    bme_rec = _bme(transaction_identification_code="TX-A",
                   instrument_identification_code="ES0000000009",
                   trading_date_and_time="2026-09-15T02:04:56.047000Z",
                   price="98.9", quantity="")
    a = _norm(bme_rec)
    b = normalize_record("blb_apae", _blb(**{
        "Instrument identification code": "ES0000000009",
        "Quantity": "",
        "Transaction Identification Code": "BLB-9",
    }))
    cands = economic_duplicate_candidates([a, b])
    assert len(cands) == 1
    assert cands[0]["scope"] == "cross_source"
    assert cands[0]["sources"] == ["blb_apae", "bme_apa"]


def test_source_report_id_is_none_when_source_publishes_none():
    rec = _norm(_bme(transaction_identification_code="TX-10"))
    entries = build_ledger([rec])
    assert entries[0].source_report_id is None
    # entry_id is the deterministic ledger-local identity
    assert len(entries[0].entry_id) >= 64


def test_chain_state_sequence_reconstructs_executed():
    recs = [
        _norm(_bme(transaction_identification_code="TX-5", publication_date_and_time="2026-09-14T10:00:05.000Z")),
        _norm(_bme(transaction_identification_code="TX-5", publication_date_and_time="2026-09-14T10:01:00.000Z",
                   flags="AMND", amnd="true")),
    ]
    chains = reconstruct_chains(build_ledger(recs))
    (chain,) = chains.values()
    seq = chain_state_sequence(chain)
    assert seq == ["EXECUTED", "FULLY_PUBLISHED", "AMENDED"]


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
