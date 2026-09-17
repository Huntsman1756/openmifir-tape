"""Source adapter contract shared by the G0 collectors.

Per docs/gates/G0.md §2 G0-A.1 each adapter has one responsibility per stage:
discover/fetch -> capture -> derive metadata -> write immutable -> record.
Adapters NEVER normalize trades; source-specific quirks stay inside the
adapter. Factual endpoints come from config/sources/*.yaml; adapters never
invent or hardcode a URL.

HTTP is injected (a callable url -> bytes) so adapters are fully testable
offline with synthetic fixtures and never make network calls in unit tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveredObject:
    """A source object the adapter will fetch. Identity + provenance only."""

    source_id: str
    object_key: str        # stable identity by which the object is stored
    url: str
    publication_timestamp: str


HttpGetter = Callable[[str], bytes]


class SourceDiscoveryError(Exception):
    """Raised when the adapter cannot resolve a source object per its rules.

    The adapter FAILS CLOSED (G0-A.6): it does not guess a URL or coerce an
    identifier. The message must be enough to diagnose without provider payload.
    """


class SourceFetchError(Exception):
    """Raised when a discovered object cannot be fetched (network/HTTP)."""
