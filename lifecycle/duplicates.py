"""Source-exact duplicate detection + economic duplicate candidate flagging.

NO_HEURISTIC_DELETION: duplicates are recorded/flagged, NEVER deleted or merged.
Source-exact duplicate = identical source-side publication identity (source +
transaction_identification_code + publication timestamp).
Economic duplicate candidate = same instrument + quantity + price + trading
time across sources (candidate only, for G0-E; not removed).
"""

from __future__ import annotations

from typing import Any


def source_exact_key(rec: Any) -> tuple:
    """Identity for source-exact duplicate detection."""
    return (
        rec.source,
        rec.transaction_identification_code,
        rec.instrument_identification_code,
        rec.publication_datetime.canonical_utc,
        rec.publication_datetime.source_value,
    )


def source_exact_duplicates(normalized: list[Any]) -> list[Any]:
    """Return records whose source-exact identity is seen more than once."""
    seen: dict[tuple, int] = {}
    for rec in normalized:
        key = source_exact_key(rec)
        seen[key] = seen.get(key, 0) + 1
    return [rec for rec in normalized if seen[source_exact_key(rec)] > 1]


def economic_duplicate_candidates(normalized: list[Any]) -> list[tuple[Any, list[Any]]]:
    """Flag economic-duplicate candidate groups (never merged/deleted).

    Candidate key = instrument + quantity + price + trading time. Records are NOT
    removed; they are only grouped and flagged for cross-source analysis (G0-E).
    """
    groups: dict[tuple, list[Any]] = {}
    for rec in normalized:
        key = (
            rec.instrument_identification_code,
            rec.quantity,
            rec.price,
            rec.trading_datetime.canonical_utc,
        )
        groups.setdefault(key, []).append(rec)
    return [(k, v) for k, v in groups.items() if len(v) > 1]
