"""Append-only deterministic ledger construction for G0-D / F-006.

Same normalized records in -> same ledger semantic digest out. No deletion, no
overwrite. Entry ids and the digest are deterministic (order-independent).

Chaining is keyed on the source transaction id only. A record WITHOUT a
transaction id is UNCHAINABLE: it forms a singleton chain, is never linked by
supersession, and no common identity is ever inferred from a missing id.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from .model import LedgerEntry, TradeState

MARKER_UNCHAINABLE = "UNCHAINABLE"
MARKER_UNPARSED_PUB_TS = "UNPARSED_PUBLICATION_TIMESTAMP"
MARKER_ENTRY_ID_COLLISION = "ENTRY_ID_COLLISION"
MARKER_STATE_UNRESOLVED = "STATE_UNRESOLVED"
MARKER_CONTRADICTORY_SIGNALS = "CONTRADICTORY_STATE_SIGNALS"
MARKER_PARTIALITY_UNDETERMINED = "PARTIALITY_UNDETERMINED"


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _state_and_event(rec: Any) -> tuple[str, str, list[str]]:
    """Resolve (state, event_type, markers) from normalized lifecycle signals.

    Deterministic and total. Fail closed: absent (None) or contradictory source
    signals never produce a guessed state — the observation is preserved with
    state UNRESOLVED and a diagnostic marker.
    """
    cancel, amend = rec.cancellation, rec.amendment
    defer, partial = rec.deferral, rec.partial_publication
    if cancel is True and amend is True:
        return TradeState.UNRESOLVED.value, "UNRESOLVED", [MARKER_CONTRADICTORY_SIGNALS]
    if cancel is True:
        return TradeState.CANCELLED.value, "CANCEL", []
    if amend is True:
        return TradeState.AMENDED.value, "AMEND", []
    if cancel is None or amend is None or defer is None:
        return TradeState.UNRESOLVED.value, "UNRESOLVED", [MARKER_STATE_UNRESOLVED]
    if defer:
        if partial is True:
            return TradeState.PARTIALLY_PUBLISHED.value, "PARTIAL_PUBLISH", []
        if partial is False:
            return TradeState.DEFERRED.value, "DEFER", []
        return TradeState.UNRESOLVED.value, "UNRESOLVED", [MARKER_PARTIALITY_UNDETERMINED]
    if partial is True:
        return TradeState.PARTIALLY_PUBLISHED.value, "PARTIAL_PUBLISH", []
    if partial is None:
        return TradeState.UNRESOLVED.value, "UNRESOLVED", [MARKER_PARTIALITY_UNDETERMINED]
    return TradeState.FULLY_PUBLISHED.value, "PUBLISH", []


def _identity_material(rec: Any, event_type: str) -> str:
    """Deterministic identity material for an entry.

    Includes the full normalized economic identity so that two distinct
    publications sharing (source, transaction id, publication ts) cannot
    silently collide; truly identical observations collide on purpose and are
    disambiguated by a deterministic occurrence index.
    """
    payload = {
        "source": rec.source,
        "source_report_id": rec.source_report_id,
        "tid": rec.transaction_identification_code,
        "instrument": rec.instrument_identification_code,
        "publication_utc": rec.publication_datetime.canonical_utc,
        "publication_source_value": rec.publication_datetime.source_value,
        "trading_utc": rec.trading_datetime.canonical_utc,
        "price": rec.price,
        "price_notation": rec.price_notation,
        "quantity": rec.quantity,
        "notional_amount": rec.notional_amount,
        "flags": sorted(rec.flags),
        "event_type": event_type,
    }
    return _sha(json.dumps(payload, sort_keys=True))


def _rec_sort_key(rec: Any) -> str:
    """Canonical ordering key over the whole normalized record content."""
    return json.dumps(rec.to_evidence(), sort_keys=True, default=str)


def build_ledger(normalized: list[Any]) -> list[LedgerEntry]:
    """Build a deterministic append-only ledger from normalized records.

    ``normalized`` items expose the attributes produced by G0-C1 (source,
    transaction_identification_code, instrument_identification_code, quantity,
    notional_amount, price, price_notation, publication_datetime,
    trading_datetime, flags) plus the lifecycle signals (cancellation,
    amendment, deferral, partial_publication, source_report_id).

    Entries are grouped by (source, transaction id), ordered by publication
    time, and linked by supersession. Records without a transaction id are
    UNCHAINABLE singletons: never grouped with each other, never superseded.
    """
    # Per-record resolution + deterministic identity material.
    items: list[tuple[Any, str, str, list[str], str]] = []
    for rec in normalized:
        state, event_type, markers = _state_and_event(rec)
        if rec.publication_datetime.parse_status != "PARSED":
            markers = markers + [MARKER_UNPARSED_PUB_TS]
        material = _identity_material(rec, event_type)
        items.append((rec, state, event_type, markers, material))

    # Deterministic occurrence index for identical identity material. Colliding
    # records are identical observations; the index disambiguates entry ids
    # without inferring any identity (fail closed, observable via marker).
    material_counts = Counter(m for *_rest, m in items)
    order = sorted(range(len(items)), key=lambda i: (items[i][4], _rec_sort_key(items[i][0])))
    occurrence = Counter()
    entry_ids: list[str] = []
    collided: list[bool] = []
    for i in order:
        m = items[i][4]
        occurrence[m] += 1
        if material_counts[m] > 1:
            entry_ids.append(f"{m}#{occurrence[m]}")
            collided.append(True)
        else:
            entry_ids.append(m)
            collided.append(False)
    # entry_ids/collided are indexed by position in `order`; map back to items.
    id_by_pos = dict(zip(order, entry_ids))
    col_by_pos = dict(zip(order, collided))

    # Group by (source, transaction id); missing/blank id -> singleton group.
    groups: dict[tuple, list[int]] = {}
    for i, (rec, *_rest) in enumerate(items):
        tid = (rec.transaction_identification_code or "").strip()
        if tid:
            groups.setdefault((rec.source, tid), []).append(i)
        else:
            groups[(rec.source, None, i)] = [i]

    entries: list[LedgerEntry] = []
    for key in groups.values():
        key.sort(key=lambda i: (
            items[i][0].publication_datetime.canonical_utc or "",
            id_by_pos[i],
        ))
        prev_id: str | None = None
        for i in key:
            rec, state, event_type, markers, _material = items[i]
            markers = list(markers)
            tid = (rec.transaction_identification_code or "").strip()
            if not tid:
                markers.append(MARKER_UNCHAINABLE)
            if col_by_pos[i]:
                markers.append(MARKER_ENTRY_ID_COLLISION)
            entry_id = id_by_pos[i]
            entries.append(
                LedgerEntry(
                    entry_id=entry_id,
                    source=rec.source,
                    source_report_id=rec.source_report_id,
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
                    supersedes_entry_id=prev_id,
                    flags=list(rec.flags),
                    markers=markers,
                    normalized={"raw_field_count": len(rec.raw_fields)},
                )
            )
            prev_id = entry_id
    return entries


def ledger_digest(entries: list[LedgerEntry]) -> str:
    """Deterministic, order-independent semantic digest over the ledger."""
    canonical = sorted(json.dumps(e.canonical(), sort_keys=True) for e in entries)
    return _sha("\n".join(canonical))


def ledger_stats(entries: list[LedgerEntry]) -> dict[str, int]:
    """Identity-health counters for evidence (metadata only)."""
    return {
        "entry_count": len(entries),
        "unchainable_record_count": sum(1 for e in entries if MARKER_UNCHAINABLE in e.markers),
        "ledger_entry_id_collision_count": sum(
            1 for e in entries if MARKER_ENTRY_ID_COLLISION in e.markers
        ),
        "supersession_link_count": sum(1 for e in entries if e.supersedes_entry_id),
        "unresolved_state_count": sum(1 for e in entries if e.state == TradeState.UNRESOLVED.value),
    }
