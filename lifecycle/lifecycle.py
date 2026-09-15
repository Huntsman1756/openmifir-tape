"""G0-D lifecycle reconstruction and F-006 composition proof helpers."""

from __future__ import annotations

from typing import Any

from .ledger import MARKER_UNCHAINABLE, build_ledger, ledger_digest
from .model import LedgerEntry, TradeState


def reconstruct_chains(entries: list[LedgerEntry]) -> dict[str, list[LedgerEntry]]:
    """Group ledger entries into per-trade chains by transaction id.

    UNCHAINABLE entries (missing transaction id) always form singleton chains:
    no common identity is ever inferred from an absent id.
    """
    chains: dict[str, list[LedgerEntry]] = {}
    for e in entries:
        if MARKER_UNCHAINABLE in e.markers:
            key = f"{e.source}|UNCHAINABLE|{e.entry_id}"
        else:
            key = f"{e.source}|{e.transaction_identification_code}"
        chains.setdefault(key, []).append(e)
    for chain in chains.values():
        chain.sort(key=lambda e: (e.publication_utc or "", e.entry_id))
    return chains


def chain_state_sequence(chain: list[LedgerEntry]) -> list[str]:
    """Reconstructed state sequence for a chain.

    Publications are the only observations; the trade's EXECUTED state precedes
    the first observed publication whenever the source published a trading
    timestamp. EXECUTED is therefore a reconstructed (not observed) state.
    """
    seq = [e.state for e in chain]
    if chain and chain[0].trading_utc:
        return [TradeState.EXECUTED.value, *seq]
    return seq


def states_in_corpus(entries: list[LedgerEntry]) -> list[str]:
    """Distinct trade states observed in the corpus (order-preserving)."""
    seen: list[str] = []
    for e in entries:
        if e.state not in seen:
            seen.append(e.state)
    return seen


def composition_proof(
    raw_inputs: list[tuple[str, dict[str, Any]]],
    normalize_fn,
) -> dict[str, str]:
    """F-006: prove raw -> normalized -> ledger reproducible.

    Given the same raw inputs, produces a deterministic ledger semantic digest.
    Returns the digest and a note; a second call with identical inputs must yield
    the identical digest (asserted by the caller/test).
    """
    normalized = [normalize_fn(source_id, raw) for source_id, raw in raw_inputs]
    entries = build_ledger(normalized)
    return {"ledger_semantic_digest": ledger_digest(entries), "entry_count": str(len(entries))}
