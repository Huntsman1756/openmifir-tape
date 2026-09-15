"""OpenMiFIR Tape: G0-D lifecycle module.

Append-only ledger, chain reconstruction, source-exact duplicate detection and
economic-duplicate candidate flagging. NO_HEURISTIC_DELETION is enforced: records
are never deleted or merged, only superseded via supersedes_entry_id.
"""

from .duplicates import economic_duplicate_candidates, source_exact_duplicates
from .ledger import build_ledger, ledger_digest, ledger_stats
from .lifecycle import (
    chain_state_sequence,
    composition_proof,
    reconstruct_chains,
    states_in_corpus,
)
from .model import LedgerEntry, TradeState

__all__ = [
    "LedgerEntry",
    "TradeState",
    "build_ledger",
    "ledger_digest",
    "ledger_stats",
    "reconstruct_chains",
    "chain_state_sequence",
    "states_in_corpus",
    "composition_proof",
    "source_exact_duplicates",
    "economic_duplicate_candidates",
]
