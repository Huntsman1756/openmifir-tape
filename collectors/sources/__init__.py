"""Registry of source adapters by source_id (config/sources/*.yaml source_id)."""

from __future__ import annotations

from . import bloomberg_apae, bme_apa
from .base import (
    DiscoveredObject,
    HttpGetter,
    SourceDiscoveryError,
    SourceFetchError,
)

ADAPTERS = {
    bme_apa.SOURCE_ID: bme_apa,
    bloomberg_apae.SOURCE_ID: bloomberg_apae,
}


def get_adapter(source_id: str):
    try:
        return ADAPTERS[source_id]
    except KeyError as exc:
        raise SourceDiscoveryError(f"no adapter registered for source_id={source_id!r}") from exc


__all__ = [
    "ADAPTERS",
    "DiscoveredObject",
    "HttpGetter",
    "SourceDiscoveryError",
    "SourceFetchError",
    "bloomberg_apae",
    "bme_apa",
    "get_adapter",
]
