"""Lifecycle / ledger model for G0-D.

Append-only event log. Records are NEVER overwritten or deleted; they are
superseded via ``supersedes_source_report_id`` links. A trade is a sequence of
states reconstructed from successive publications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TradeState(str, Enum):
    EXECUTED = "EXECUTED"
    PARTIALLY_PUBLISHED = "PARTIALLY_PUBLISHED"
    DEFERRED = "DEFERRED"
    FULLY_PUBLISHED = "FULLY_PUBLISHED"
    AMENDED = "AMENDED"
    CANCELLED = "CANCELLED"


@dataclass
class LedgerEntry:
    """One immutable observation (publication event) in the append-only ledger."""

    entry_id: str                # deterministic
    source: str
    source_report_id: str        # source-side identity of this publication
    transaction_identification_code: str | None
    instrument_identification_code: str
    quantity: str | None
    notional_amount: str | None
    price: str | None
    price_notation: str | None
    publication_utc: str | None
    trading_utc: str | None
    state: str                   # TradeState value
    event_type: str              # PUBLISH | AMEND | CANCEL | DEFER | ...
    supersedes_source_report_id: str | None
    flags: list[str] = field(default_factory=list)
    normalized: dict[str, Any] = field(default_factory=dict)   # metadata ref, no payload

    def canonical(self) -> dict[str, Any]:
        """Order-independent canonical representation for the ledger digest."""
        return {
            "entry_id": self.entry_id,
            "source": self.source,
            "source_report_id": self.source_report_id,
            "transaction_identification_code": self.transaction_identification_code,
            "instrument_identification_code": self.instrument_identification_code,
            "quantity": self.quantity,
            "notional_amount": self.notional_amount,
            "price": self.price,
            "price_notation": self.price_notation,
            "publication_utc": self.publication_utc,
            "trading_utc": self.trading_utc,
            "state": self.state,
            "event_type": self.event_type,
            "supersedes_source_report_id": self.supersedes_source_report_id,
            "flags": sorted(self.flags),
        }
