"""Registry of source adapters by source_id (config/sources/*.yaml source_id)."""

from __future__ import annotations

from .base import (
    DiscoveredObject,
    HttpGetter,
    SourceDiscoveryError,
    SourceFetchError,
)
from . import bme_apa, bloomberg_apae

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
    "bme_apa",
    "bloomberg_apae",
    "get_adapter",
    "DiscoveredObject",
    "HttpGetter",
    "SourceDiscoveryError",
    "SourceFetchError",
]
