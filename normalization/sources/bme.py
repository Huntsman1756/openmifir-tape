"""BME APA post-trade JSON -> normalized record (G0-C).

Lossless: every raw field is preserved in ``raw_fields``. Source quirks are not
"corrected". BME encodes decimals as ``{"Mantissa":..., "Exponent":...}``; the
canonical normalized quantity/price/notional fields map these to a decimal string
(``Mantissa * 10**Exponent``), while the raw dict is preserved verbatim in
``raw_fields``. Quantity/notional and timestamps follow the shared guardrails.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..mmt import MMT_AMEND_FLAG, MMT_CANCEL_FLAG, MMT_DEFERRAL_REASON, MMT_PARTIAL_TYPE
from ..model import NormalizedRecord
from ..quantity import notional_vs_quantity_price
from ..timestamp import timestamp

SOURCE = "bme_apa"

_FIELD_MAP = {
    "instrument_identification_code": "instrument_identification_code",
    "instrument_id_code_type": "instrument_id_code",
    "venue_of_execution": "venue_of_execution",
    "venue_of_publication": "venue_of_publication",
    "price_currency": "price_currency",
    "price_notation": "price_notation",
    "notation_of_quantity_measurement_unit": "notation_of_the_quantity_in_measurement_unit",
    "price": "price",
    "quantity": "quantity",
    "quantity_in_measurement_unit": "quantity_in_measurement_unit",
    "notional_amount": "notional_amount",
    "notional_currency": "notional_currency",
    "transaction_identification_code": "transaction_identification_code",
    "trading_datetime": "trading_date_and_time",
    "publication_datetime": "publication_date_and_time",
}


def _decimal_str(val: Any) -> Any:
    """Map a BME Mantissa/Exponent dict to a decimal string; otherwise pass through."""
    if isinstance(val, dict) and "Mantissa" in val:
        try:
            mant = Decimal(str(val["Mantissa"]))
            exp = Decimal(str(val.get("Exponent", 0)))
            return str(mant * (Decimal(10) ** int(exp)))
        except (InvalidOperation, ValueError):
            return val  # fail closed: preserve the raw value
    return val


def _flags(raw: dict[str, Any]) -> list[str]:
    val = raw.get("flags")
    if isinstance(val, list):
        return [str(f) for f in val]
    if isinstance(val, str) and val:
        return [f.strip() for f in val.split(",") if f.strip()]
    return []


def _bool(val: Any) -> bool | None:
    """Parse string booleans ('true'/'false') without coercing arbitrary text."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        v = val.strip().lower()
        if v == "true":
            return True
        if v == "false":
            return False
        return None
    return None


def _deferral_signal(raw: dict[str, Any], flags: list[str]) -> bool | None:
    """Deferral signal from source semantics.

    ``post_trade_deferral`` carries the MMT 4.1 reason code letter (observed in
    the captured corpus: 'A'->MLF1, 'C'->LLF3, 'G'->DEFF); any non-empty code
    means a non-immediate publication deferral was applied. An MMT 4.1 reason
    mnemonic in ``flags`` also indicates deferral. Field absent AND no flag ->
    None (undetermined, fail closed).
    """
    if set(flags) & MMT_DEFERRAL_REASON:
        return True
    if "post_trade_deferral" not in raw:
        return None
    value = raw.get("post_trade_deferral")
    parsed = _bool(value)
    if parsed is not None:
        return parsed
    return bool(str(value).strip())


def _partial_signal(raw: dict[str, Any], flags: list[str], deferral: bool | None) -> bool | None:
    """Partial-publication signal from source semantics.

    A publication is partial when the source publishes the trade but withholds
    content: an explicit ``missing_price``/``missing_quantity`` indicator, an
    MMT 4.2 non-full-detail type flag, or — under deferral — the volume block
    omitted from the publication entirely (observed: DEFF publications carry no
    quantity/notional keys at all, while non-deferred records of the same
    instrument type do carry them). Undetermined when deferral itself is
    undetermined.
    """
    if set(flags) & MMT_PARTIAL_TYPE:
        return True
    if _bool(raw.get("missing_price")) or _bool(raw.get("missing_quantity")):
        return True
    if deferral is None:
        return None
    if deferral and raw.get("quantity") is None:
        return True
    return False


def normalize(rec: dict[str, Any]) -> NormalizedRecord:
    raw = dict(rec)
    td = timestamp(raw.get("trading_date_and_time"))
    pd = timestamp(raw.get("publication_date_and_time"))
    flags = _flags(raw)
    flagset = set(flags)
    deferral = _deferral_signal(raw, flags)
    sanity = [
        notional_vs_quantity_price(
            raw.get("quantity"), raw.get("notional_amount"),
            raw.get("price"), raw.get("price_notation"),
        )
    ]
    return NormalizedRecord(
        source=SOURCE,
        instrument_identification_code=str(raw.get("instrument_identification_code") or ""),
        instrument_id_code_type=raw.get("instrument_id_code"),
        venue_of_execution=raw.get("venue_of_execution"),
        venue_of_publication=raw.get("venue_of_publication"),
        price_currency=raw.get("price_currency"),
        price_notation=raw.get("price_notation"),
        notation_of_quantity_measurement_unit=raw.get("notation_of_the_quantity_in_measurement_unit"),
        price=_decimal_str(raw.get("price")),
        quantity=_decimal_str(raw.get("quantity")),
        quantity_in_measurement_unit=raw.get("quantity_in_measurement_unit"),
        notional_amount=_decimal_str(raw.get("notional_amount")),
        notional_currency=raw.get("notional_currency"),
        transaction_identification_code=raw.get("transaction_identification_code"),
        trading_datetime=td,
        publication_datetime=pd,
        flags=flags,
        deferral=deferral,
        partial_publication=_partial_signal(raw, flags, deferral),
        cancellation=True if MMT_CANCEL_FLAG in flagset else _bool(raw.get("canc")),
        amendment=True if MMT_AMEND_FLAG in flagset else _bool(raw.get("amnd")),
        source_report_id=None,   # BME publishes no per-publication report id
        raw_fields=raw,
        rts2_mapping=dict(_FIELD_MAP),
        sanity=sanity,
    )
