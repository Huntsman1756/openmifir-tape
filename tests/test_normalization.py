"""Offline tests for G0-C1 normalization (lossless, exact timestamps, non-destructive)."""

from normalization import normalize_record
from normalization.model import NormalizedRecord
from normalization.quantity import notional_vs_quantity_price
from normalization.timestamp import timestamp


def _bme_record(**overrides):
    rec = {
        "instrument_identification_code": "ES0000000001",
        "venue_of_execution": "XOFF",
        "mifir_identifier": "CRPB",
        "price_currency": "EUR",
        "price_notation": "PERC",
        "notation_of_the_quantity_in_measurement_unit": "UNIT",
        "trading_date_and_time": "2026-09-14T10:00:00.123Z",
        "price": "98.50",
        "missing_price": "false",
        "quantity": "100000",
        "notional_amount": "98500",
        "notional_currency": "EUR",
        "transaction_to_be_cleared": "false",
        "transaction_identification_code": "TX1",
        "venue_of_publication": "BMEA",
        "quantity_in_measurement_unit": "100000",
        "publication_date_and_time": "2026-09-14T10:00:05.456Z",
        "third_country_trading_venue_of_execution": "",
        "benc": "false", "tpac": "false", "sdiv": "false", "entr": "false",
        "amnd": "false", "canc": "false", "xfph": "false", "lrgs": "false",
        "size": "", "ilqd": "false", "port": "false", "cont": "false",
        "npft": "false", "post_trade_deferral": "false", "flags": [],
        "instrument_id_code": "ISIN", "reception_date_time": "2026-09-14T10:00:05.456Z",
    }
    rec.update(overrides)
    return rec


def _bloomberg_row(**overrides):
    row = {
        "Trading date and time": "2026-09-15T02:04:56.047000Z",
        "Instrument identification code type": "ISIN",
        "Instrument identification code": "AU3CB0335230",
        "Effective date of the contract": "",
        "Maturity date of the contract": "",
        "Price": "98.9",
        "Price conditions": "",
        "Venue of execution": "XOFF",
        "Price notation": "PERC",
        "Price Currency": "AUD",
        "Notation of the quantity in measurement unit": "",
        "Quantity in measurement unit": "",
        "Quantity": "",
        "Notional amount": "200000",
        "Notional currency": "AUD",
        "Type": "",
        "Publication Date and Time": "2026-09-15T02:05:05.529712Z",
        "Venue of publication": "BAPA",
        "Transaction Identification Code": "64e25d91-6be7-4de3-a88f-e324f15a25ee",
        "Spread": "", "Upfront payment": "", "LEI of clearing house": "", "Flags": "",
    }
    row.update(overrides)
    return row


def test_bme_normalization_is_lossless_and_mapped():
    raw = _bme_record()
    n = normalize_record("bme_apa", raw)
    assert isinstance(n, NormalizedRecord)
    assert n.source == "bme_apa"
    assert n.instrument_identification_code == "ES0000000001"
    assert n.price_notation == "PERC"
    assert n.venue_of_publication == "BMEA"
    # LOSSLESS: every raw field preserved verbatim
    assert n.raw_fields == raw
    assert len(n.raw_fields) == len(raw)
    # exact timestamps parsed to canonical UTC
    assert n.trading_datetime.source_value == "2026-09-14T10:00:00.123Z"
    assert n.trading_datetime.parse_status == "PARSED"
    assert n.trading_datetime.canonical_utc == "2026-09-14T10:00:00.123000+00:00"
    assert n.publication_datetime.parse_status == "PARSED"
    # RTS2 mapping present
    assert n.rts2_mapping["price_notation"] == "price_notation"
    # quantity/notional sanity reported, not applied
    assert n.sanity[0].status == "PASS"  # 100000 * 98.50% = 98500


def test_bloomberg_normalization_is_lossless_and_mapped():
    row = _bloomberg_row()
    n = normalize_record("blb_apae", row)
    assert n.source == "blb_apae"
    assert n.instrument_identification_code == "AU3CB0335230"
    assert n.price_currency == "AUD"
    assert n.notional_amount == "200000"
    # LOSSLESS
    assert n.raw_fields == row
    # exact timestamps
    assert n.trading_datetime.canonical_utc == "2026-09-15T02:04:56.047000+00:00"
    assert n.publication_datetime.parse_status == "PARSED"
    # quantity missing -> sanity INSUFFICIENT (never guessed)
    assert n.sanity[0].status == "INSUFFICIENT"
    assert n.quantity is None or n.quantity == ""


def test_timestamp_unparseable_is_fail_closed():
    t = timestamp("not-a-date")
    assert t.parse_status == "UNPARSED"
    assert t.canonical_utc is None
    t2 = timestamp(None)
    assert t2.parse_status == "ABSENT"
    assert t2.canonical_utc is None


def test_timestamp_no_timezone_no_inference():
    t = timestamp("2026-09-14T10:00:00")
    assert t.parse_status == "UNPARSED_NO_TZ"
    assert t.canonical_utc is None


def test_bme_does_not_use_file_level_publication_date():
    # A file-level daily publicationDate (F-004) present in the raw record must
    # NOT be used as the record's publication timestamp.
    raw = _bme_record()
    raw["publicationDate"] = 1789336800000  # would be 2026-09-13T22:00:00Z
    n = normalize_record("bme_apa", raw)
    # The normalized publication timestamp comes from the record's own field.
    assert n.publication_datetime.source_value == "2026-09-14T10:00:05.456Z"
    assert n.publication_datetime.canonical_utc != "2026-09-13T22:00:00+00:00"
    assert "publicationDate" in n.raw_fields  # still lossless


def test_sanity_perc_mismatch_flags():
    s = notional_vs_quantity_price("100000", "50000", "98.50", "PERC")
    assert s.status == "FLAG"


def test_sanity_non_perc():
    s = notional_vs_quantity_price("100", "9850", "98.50", "XINT")
    assert s.status == "PASS"


def test_bme_mantissa_exponent_maps_to_decimal_preserving_raw():
    raw = _bme_record(quantity={"Mantissa": 2000000, "Exponent": 0},
                      price={"Mantissa": 9775, "Exponent": -2},
                      notional_amount={"Mantissa": 1955000, "Exponent": -2})
    n = normalize_record("bme_apa", raw)
    assert n.quantity == "2000000"
    assert n.price == "97.75"
    assert n.notional_amount == "19550.00"
    # raw preserved verbatim (lossless), not overwritten by the decimal mapping
    assert n.raw_fields["quantity"] == {"Mantissa": 2000000, "Exponent": 0}
    assert n.raw_fields["price"] == {"Mantissa": 9775, "Exponent": -2}


def test_non_aggressive_no_value_filling():
    raw = _bme_record(quantity="", notional_amount="", price="")
    n = normalize_record("bme_apa", raw)
    assert n.quantity == ""
    assert n.notional_amount == ""
    assert n.sanity[0].status == "INSUFFICIENT"
