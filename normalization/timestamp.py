"""Exact timestamp handling for G0-C.

Guardrail: use the source value exactly; parse it to a canonical UTC value. If it
cannot be parsed, record parse_status=UNPARSED and leave canonical_utc=None (fail
closed). Never infer a timestamp. In particular, the BME file-level
publicationDate (a daily catalogue boundary, F-004) is NEVER used here; only the
record's own trading/publication timestamp field is considered.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .model import TimestampValue


def _parse(value: str | None) -> tuple[str | None, str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "ABSENT"
    try:
        # Accept ISO-8601 with 'Z' or offset; normalize to UTC.
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            # Source omitted timezone: record as unparseable-to-UTC (no inference).
            return None, "UNPARSED_NO_TZ"
        return dt.astimezone(UTC).isoformat(), "PARSED"
    except (ValueError, TypeError):
        return None, "UNPARSED"


def timestamp(source_value: str | None) -> TimestampValue:
    canonical, status = _parse(source_value)
    return TimestampValue(
        source_value=source_value or "",
        canonical_utc=canonical,
        parse_status=status,
    )
