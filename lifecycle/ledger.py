"""Append-only deterministic ledger construction for G0-D / F-006.

Same normalized records in -> same ledger semantic digest out. No deletion, no
overwrite. Entry ids and the digest are deterministic (order-independent).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .model import LedgerEntry, TradeState


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _state_and_event(flags: list[str], deferral: bool | None) -> tuple[str, str]:
    fl = set(flags)
    if "canc" in fl or "cancel" in fl or "CANC" in fl:
        return TradeState.CANCELLED.value, "CANCEL"
    if "amnd" in fl or "amend" in fl or "AMND" in fl:
        return TradeState.AMENDED.value, "AMEND"
    if deferral:
        return TradeState.DEFERRED.value, "DEFER"
    return TradeState.FULLY_PUBLISHED.value, "PUBLISH"


def build_ledger(normalized: list[Any]) -> list[LedgerEntry]:
    """Build a deterministic append-only ledger from normalized records.

    ``normalized`` items expose the attributes produced by G0-C1 (source,
    transaction_identification_code, instrument_identification_code, quantity,
    notional_amount, price, price_notation, publication_datetime, trading_datetime,
    flags, deferral). Entries are grouped by source + transaction id, ordered by
    publication time, and linked by supersession.
    """
    # Group by (source, txn) for chain reconstruction.
    groups: dict[tuple[str, str], list[Any]] = {}
    for rec in normalized:
        key = (rec.source, rec.transaction_identification_code or "")
        groups.setdefault(key, []).append(rec)

    entries: list[LedgerEntry] = []
    for (_src, _txn), recs in groups.items():
        recs.sort(key=lambda r: (r.publication_datetime.canonical_utc or "", r.transaction_identification_code or ""))
        prev_id: str | None = None
        for rec in recs:
            flags = list(rec.flags)
            state, event_type = _state_and_event(flags, rec.deferral)
            entry_id = _sha(f"{rec.source}|{rec.transaction_identification_code}|{rec.publication_datetime.canonical_utc}|{event_type}")
            entries.append(
                LedgerEntry(
                    entry_id=entry_id,
                    source=rec.source,
                    source_report_id=entry_id,  # deterministic local report id
                    transaction_identification_code=rec.transaction_identification_code,
                    instrument_identification_code=rec.instrument_identification_code,
                    quantity=rec.quantity,
                    notional_amount=rec.notional_amount,
                    price=rec.price,
                    price_notation=rec.price_notation,
                    publication_utc=rec.publication_datetime.canonical_utc,
                    trading_utc=rec.trading_datetime.canonical_utc,
                    state=state,
                    event_type=event_type,
                    supersedes_source_report_id=prev_id,
                    flags=flags,
                    normalized={"raw_field_count": len(rec.raw_fields)},
                )
            )
            prev_id = entry_id
    return entries


def ledger_digest(entries: list[LedgerEntry]) -> str:
    """Deterministic, order-independent semantic digest over the ledger."""
    canonical = sorted(json.dumps(e.canonical(), sort_keys=True) for e in entries)
    return _sha("\n".join(canonical))
