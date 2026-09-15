"""Normalized record model for G0-C (G0-C1).

Guardrails (from the gate contract and review):
- RAW_FIELDS_LOSSLESS: every raw source field is preserved verbatim in
  ``raw_fields``; source quirks are NOT "corrected" away.
- Quantity/notional: sanity is reported, never silently filled or coerced.
- Timestamps: the exact source value is kept alongside a parsed canonical value;
  nothing is inferred (F-004 file-level publicationDate is never used here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SanityResult:
    check: str                 # e.g. "notional_vs_quantity_price"
    status: str                # PASS | FLAG | INSUFFICIENT | N/A
    detail: str


@dataclass
class TimestampValue:
    source_value: str          # exact value as published by the source
    canonical_utc: str | None  # parsed ISO-8601 UTC, or None if unparseable
    parse_status: str          # PARSED | UNPARSED | UNPARSED_NO_TZ | ABSENT


@dataclass
class NormalizedRecord:
    source: str
    instrument_identification_code: str
    instrument_id_code_type: str | None
    venue_of_execution: str | None
    venue_of_publication: str | None
    price_currency: str | None
    price_notation: str | None
    notation_of_quantity_measurement_unit: str | None
    price: str | None
    quantity: str | None
    quantity_in_measurement_unit: str | None
    notional_amount: str | None
    notional_currency: str | None
    transaction_identification_code: str | None
    trading_datetime: TimestampValue
    publication_datetime: TimestampValue
    flags: list[str] = field(default_factory=list)
    # Lifecycle signals (G0-D): tri-state. True/False = determined from source
    # semantics; None = the source schema cannot determine it (fail closed).
    deferral: bool | None = None            # non-immediate publication indicated
    partial_publication: bool | None = None # source published incomplete content
    cancellation: bool | None = None        # source marks this publication a cancel
    amendment: bool | None = None           # source marks this publication an amend
    source_report_id: str | None = None     # source-PUBLISHED publication/report id;
                                            # None when the source publishes none
    raw_fields: dict[str, Any] = field(default_factory=dict)     # LOSSLESS
    rts2_mapping: dict[str, str] = field(default_factory=dict)   # canonical -> source field
    sanity: list[SanityResult] = field(default_factory=list)

    def to_evidence(self) -> dict[str, Any]:
        """Metadata-only evidence (no raw payload)."""
        return {
            "source": self.source,
            "instrument_identification_code": self.instrument_identification_code,
            "instrument_id_code_type": self.instrument_id_code_type,
            "venue_of_execution": self.venue_of_execution,
            "venue_of_publication": self.venue_of_publication,
            "price_currency": self.price_currency,
            "price_notation": self.price_notation,
            "notation_of_quantity_measurement_unit": self.notation_of_quantity_measurement_unit,
            "price": self.price,
            "quantity": self.quantity,
            "quantity_in_measurement_unit": self.quantity_in_measurement_unit,
            "notional_amount": self.notional_amount,
            "notional_currency": self.notional_currency,
            "transaction_identification_code": self.transaction_identification_code,
            "trading_datetime": {
                "source_value": self.trading_datetime.source_value,
                "canonical_utc": self.trading_datetime.canonical_utc,
                "parse_status": self.trading_datetime.parse_status,
            },
            "publication_datetime": {
                "source_value": self.publication_datetime.source_value,
                "canonical_utc": self.publication_datetime.canonical_utc,
                "parse_status": self.publication_datetime.parse_status,
            },
            "flags": self.flags,
            "deferral": self.deferral,
            "partial_publication": self.partial_publication,
            "cancellation": self.cancellation,
            "amendment": self.amendment,
            "source_report_id": self.source_report_id,
            "raw_field_count": len(self.raw_fields),             # count, not content
            "rts2_mapping": self.rts2_mapping,
            "sanity": [s.__dict__ for s in self.sanity],
        }
