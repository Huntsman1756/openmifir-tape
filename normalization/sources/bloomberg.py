"""Bloomberg APAE CSV row -> normalized record (G0-C).

Lossless: every raw CSV field is preserved in ``raw_fields``. The leading
disclaimer header lines are NOT data; the mapper receives a single parsed data
row keyed by the column header (line 12) of the source file.
"""

from __future__ import annotations

from typing import Any

from ..mmt import MMT_AMEND_FLAG, MMT_CANCEL_FLAG, MMT_DEFERRAL_REASON, MMT_PARTIAL_TYPE
from ..model import NormalizedRecord
from ..quantity import notional_vs_quantity_price
from ..timestamp import timestamp

SOURCE = "blb_apae"

_FIELD_MAP = {
    "instrument_identification_code": "Instrument identification code",
    "instrument_id_code_type": "Instrument identification code type",
    "venue_of_execution": "Venue of execution",
    "venue_of_publication": "Venue of publication",
    "price_currency": "Price Currency",
    "price_notation": "Price notation",
    "notation_of_quantity_measurement_unit": "Notation of the quantity in measurement unit",
    "price": "Price",
    "quantity": "Quantity",
    "quantity_in_measurement_unit": "Quantity in measurement unit",
    "notional_amount": "Notional amount",
    "notional_currency": "Notional currency",
    "transaction_identification_code": "Transaction Identification Code",
    "trading_datetime": "Trading date and time",
    "publication_datetime": "Publication Date and Time",
}


def _flags(row: dict[str, Any]) -> list[str]:
    val = row.get("Flags")
    if isinstance(val, list):
        return [str(f) for f in val]
    if isinstance(val, str) and val:
        return [f.strip() for f in val.split(",") if f.strip()]
    return []


def _lifecycle_signals(row: dict[str, Any], flags: list[str]) -> tuple[bool | None, ...]:
    """Deferral/partial/cancel/amend signals from source semantics.

    The APAE CSV schema exposes a ``Flags`` column (MMT mnemonics) and a
    ``Type`` column. A present-but-empty Flags column is a determined "no
    indication"; a missing column means the source schema cannot determine the
    signal -> None (fail closed). Partial publication under deferral = the
    publication omits the volume (Quantity cell empty) or carries an MMT 4.2
    non-full-detail type flag.
    """
    flagset = set(flags)
    has_flags_col = "Flags" in row
    type_val = str(row.get("Type") or "").strip().upper()
    has_type_col = "Type" in row

    cancellation = True if (MMT_CANCEL_FLAG in flagset or type_val in {"CANC", "CANCEL"}) else (
        False if (has_flags_col or has_type_col) else None
    )
    amendment = True if (MMT_AMEND_FLAG in flagset or type_val in {"AMND", "AMEND"}) else (
        False if (has_flags_col or has_type_col) else None
    )
    deferral = True if flagset & MMT_DEFERRAL_REASON else (
        False if has_flags_col else None
    )
    if flagset & MMT_PARTIAL_TYPE:
        partial: bool | None = True
    elif deferral is True:
        partial = not str(row.get("Quantity") or "").strip()
    elif deferral is None:
        partial = None
    else:
        partial = False
    return deferral, partial, cancellation, amendment


def normalize(row: dict[str, Any]) -> NormalizedRecord:
    raw = dict(row)
    td = timestamp(raw.get("Trading date and time"))
    pd = timestamp(raw.get("Publication Date and Time"))
    flags = _flags(raw)
    deferral, partial, cancellation, amendment = _lifecycle_signals(raw, flags)
    sanity = [
        notional_vs_quantity_price(
            raw.get("Quantity"), raw.get("Notional amount"),
            raw.get("Price"), raw.get("Price notation"),
        )
    ]
    return NormalizedRecord(
        source=SOURCE,
        instrument_identification_code=str(raw.get("Instrument identification code") or ""),
        instrument_id_code_type=raw.get("Instrument identification code type"),
        venue_of_execution=raw.get("Venue of execution"),
        venue_of_publication=raw.get("Venue of publication"),
        price_currency=raw.get("Price Currency"),
        price_notation=raw.get("Price notation"),
        notation_of_quantity_measurement_unit=raw.get("Notation of the quantity in measurement unit"),
        price=raw.get("Price"),
        quantity=raw.get("Quantity"),
        quantity_in_measurement_unit=raw.get("Quantity in measurement unit"),
        notional_amount=raw.get("Notional amount"),
        notional_currency=raw.get("Notional currency"),
        transaction_identification_code=raw.get("Transaction Identification Code"),
        trading_datetime=td,
        publication_datetime=pd,
        flags=flags,
        deferral=deferral,
        partial_publication=partial,
        cancellation=cancellation,
        amendment=amendment,
        source_report_id=None,   # APAE CSV publishes no per-publication report id
        raw_fields=raw,
        rts2_mapping=dict(_FIELD_MAP),
        sanity=sanity,
    )
