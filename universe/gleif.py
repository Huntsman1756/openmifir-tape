"""Thin GLEIF acquisition adapter: resolve ``LegalJurisdiction.country``.

Offline by construction: HTTP is injected (``url -> bytes``). Unit tests pass a
getter that raises on any call, so no test can reach the network.

Caching is write-once and content-addressed under a caller-supplied ``cache_dir``
(the operator's gitignored local data directory). NO provider bytes ever enter
the repository tree.

GLEIF LEI-CDF/API record shape handled: ``data[].attributes.entity.
legalJurisdiction`` (a ISO 3166-1 alpha-2 code). ``legalJurisdiction`` may be a
bare code or an object carrying a ``country`` code. Unresolvable LEIs are
returned with ``resolved=False`` (fail closed; never guessed).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

HttpGet = Callable[[str], bytes]


class GleifParseError(Exception):
    """Raised when GLEIF bytes cannot be parsed per the documented subset."""


@dataclass(frozen=True)
class GleifEntity:
    lei: str
    legal_jurisdiction_country: str | None
    resolved: bool


def _norm_lei(value: str | None) -> str:
    return (value or "").strip().upper()


def _stringify(code) -> str | None:
    if code is None:
        return None
    if isinstance(code, str):
        text = code.strip()
        return text or None
    if isinstance(code, dict):
        return _stringify(code.get("country"))
    return None


def parse_gleif(payload: bytes) -> dict[str, GleifEntity]:
    """Return ``{LEI: GleifEntity}`` for a GLEIF LEI-CDF/API payload."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GleifParseError(f"GLEIF payload not UTF-8: {exc}") from exc

    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GleifParseError(f"GLEIF payload not JSON: {exc}") from exc

    entities: dict[str, GleifEntity] = {}

    def ingest_leif_identifier(raw: dict):
        lei = _norm_lei(raw.get("lei") or raw.get("id"))
        if not lei:
            return
        attributes = raw.get("attributes") or {}
        entity = attributes.get("entity") or {}
        jurisdiction = entity.get("legalJurisdiction", attributes.get("legalJurisdiction"))
        country = _stringify(jurisdiction)
        entities[lei] = GleifEntity(
            lei=lei,
            legal_jurisdiction_country=country,
            resolved=bool(country),
        )

    data = doc.get("data") if isinstance(doc, dict) else None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                ingest_leif_identifier(item)
        if entities:
            return entities

    if isinstance(data, dict):
        ingest_leif_identifier(data)
        if entities:
            return entities

    raise GleifParseError("GLEIF payload has no resolvable LEI data array")


def fetch(url: str, http_get: HttpGet) -> bytes:
    return http_get(url)


def cache_raw(raw_bytes: bytes, cache_dir: Path, *, source_id: str) -> tuple[Path, str]:
    digest = hashlib.sha256(raw_bytes).hexdigest()
    target_dir = Path(cache_dir) / source_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / digest
    if not target.exists():
        with open(target, "xb") as fh:
            fh.write(raw_bytes)
    return target, digest


def resolve_legal_jurisdiction(lei: str, entities: dict[str, GleifEntity]) -> GleifEntity:
    """Resolve an LEI against a parsed GLEIF index; fail closed when absent."""
    normalized = _norm_lei(lei)
    found = entities.get(normalized)
    if found is not None:
        return found
    return GleifEntity(lei=normalized, legal_jurisdiction_country=None, resolved=False)
