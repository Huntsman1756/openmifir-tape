"""BME APA post-trade JSON -> normalized record (G0-C).

Lossless: every raw field is preserved in ``raw_fields``. Source quirks are not
"corrected". Quantity/notional and timestamps follow the shared guardrails.
"""

from __future__ import annotations

from typing import Any

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


def _flags(raw: dict[str, Any]) -> list[str]:
    val = raw.get("flags")
    if isinstance(val, list):
        return [str(f) for f in val]
    if isinstance(val, str) and val:
        return [f.strip() for f in val.split(",") if f.strip()]
    return []


def normalize(rec: dict[str, Any]) -> NormalizedRecord:
    raw = dict(rec)
    td = timestamp(raw.get("trading_date_and_time"))
    pd = timestamp(raw.get("publication_date_and_time"))
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
        price=raw.get("price"),
        quantity=raw.get("quantity"),
        quantity_in_measurement_unit=raw.get("quantity_in_measurement_unit"),
        notional_amount=raw.get("notional_amount"),
        notional_currency=raw.get("notional_currency"),
        transaction_identification_code=raw.get("transaction_identification_code"),
        trading_datetime=td,
        publication_datetime=pd,
        flags=_flags(raw),
        deferral=bool(raw.get("post_trade_deferral")) if raw.get("post_trade_deferral") is not None else None,
        raw_fields=raw,
        rts2_mapping=dict(_FIELD_MAP),
        sanity=sanity,
    )
