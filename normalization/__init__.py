"""OpenMiFIR Tape: G0-C normalization module.

G0-C1 scope. Transforms raw source records into a lossless normalized view with
exact timestamps, price-notation awareness and non-destructive quantity/notional
sanity results. It does NOT build a ledger, query layer, or UI.
"""

from .model import NormalizedRecord, SanityResult, TimestampValue
from .normalize import normalize_record

__all__ = ["NormalizedRecord", "SanityResult", "TimestampValue", "normalize_record"]
