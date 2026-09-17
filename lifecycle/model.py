"""Lifecycle / ledger model for G0-D.

Append-only event log. Records are NEVER overwritten or deleted; they are
superseded via ``supersedes_entry_id`` links. A trade is a sequence of
states reconstructed from successive publications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TradeState(StrEnum):
    EXECUTED = "EXECUTED"
    PARTIALLY_PUBLISHED = "PARTIALLY_PUBLISHED"
    DEFERRED = "DEFERRED"
    FULLY_PUBLISHED = "FULLY_PUBLISHED"
    AMENDED = "AMENDED"
    CANCELLED = "CANCELLED"
    # Fail-closed meta-state (not a lifecycle state): the source signals were
    # absent or contradictory, so no lifecycle state could be determined.
    UNRESOLVED = "UNRESOLVED"


@dataclass
class LedgerEntry:
    """One immutable observation (publication event) in the append-only ledger."""

    entry_id: str                # deterministic ledger-local identity (sha256)
    source: str
    source_report_id: str | None  # source-PUBLISHED publication/report id;
                                # None when the source publishes none
    transaction_identification_code: str | None
    instrument_identification_code: str
    quantity: str | None
    notional_amount: str | None
    price: str | None
    price_notation: str | None
    publication_utc: str | None
    trading_utc: str | None
    state: str                   # TradeState value
    event_type: str              # PUBLISH | PARTIAL_PUBLISH | DEFER | AMEND | CANCEL | UNRESOLVED
    supersedes_entry_id: str | None  # entry_id of the observation this supersedes
    flags: list[str] = field(default_factory=list)    # source-published flags (verbatim)
    markers: list[str] = field(default_factory=list)  # ledger-internal diagnostics
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
            "supersedes_entry_id": self.supersedes_entry_id,
            "flags": sorted(self.flags),
            "markers": sorted(self.markers),
        }
