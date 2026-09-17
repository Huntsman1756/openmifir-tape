"""Source mapper registry for G0-C normalization."""

from __future__ import annotations

from collections.abc import Callable

from . import bloomberg, bme

MAPPERS: dict[str, Callable] = {
    bme.SOURCE: bme.normalize,
    bloomberg.SOURCE: bloomberg.normalize,
}


def get_mapper(source_id: str) -> Callable:
    try:
        return MAPPERS[source_id]
    except KeyError as exc:
        raise KeyError(f"no normalization mapper for source_id={source_id!r}") from exc
