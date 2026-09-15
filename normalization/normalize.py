"""G0-C raw -> normalized orchestration.

Lossless and non-destructive: the raw record is always preserved; normalization
produces a canonical view plus sanity results and exact timestamps. Nothing is
inferred, coerced, or "corrected".
"""

from __future__ import annotations

from typing import Any

from .model import NormalizedRecord
from .sources import get_mapper


def normalize_record(source_id: str, raw_record: dict[str, Any]) -> NormalizedRecord:
    mapper = get_mapper(source_id)
    return mapper(raw_record)
