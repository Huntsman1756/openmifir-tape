"""BME APA post-trade JSON source adapter.

Per config/sources/bme_apa.yaml the entrypoint status is PARTIAL: the BME
post-trade data is confirmed to be freely accessible JSON files, but the
per-file links are rendered client-side and are not present in static HTML.
This adapter therefore FAILS CLOSED (G0-A.6): it discovers machine-readable
file URLs from the canonical page when present, and if it cannot resolve a
machine feed URL it raises SourceDiscoveryError rather than guessing.

No trade normalization. No universe logic.
"""

from __future__ import annotations

import re
from typing import Any

from ..sources.base import DiscoveredObject, HttpGetter, SourceDiscoveryError, SourceFetchError

_DATA_FILE_RE = re.compile(r"\.(json|zip|csv|txt)(\?|$)", re.IGNORECASE)
_POSTTRADE_HINT_RE = re.compile(r"post[-_ ]?trade|transparenc|mifir", re.IGNORECASE)
_URL_RE = re.compile(r"""(?:href|src)=["']([^"']+)["']""")

SOURCE_ID = "bme_apa"


def _absolute(url: str, base: str) -> str:
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        # derive scheme+host from base
        from urllib.parse import urlparse

        parts = urlparse(base)
        return f"{parts.scheme}://{parts.netloc}{url}"
    from urllib.parse import urljoin

    return urljoin(base, url)


def discover(conf: dict[str, Any], http_get: HttpGetter) -> list[DiscoveredObject]:
    """Resolve BME post-trade JSON file URLs from the canonical page."""
    entry = conf.get("entrypoint", {})
    base_url = entry.get("base_url") or ""
    base_doc = entry.get("base_doc") or ""
    if not base_url and not base_doc:
        raise SourceDiscoveryError("bme_apa: no entrypoint.base_url and no entrypoint.base_doc configured")

    if base_url:
        # Operator-resolved machine feed; treat base_url as a listing.
        html = http_get(base_url).decode("utf-8", "replace")
    else:
        html = http_get(base_doc).decode("utf-8", "replace")

    candidates: list[tuple[str, str]] = []  # (url, key)
    for raw in _URL_RE.findall(html):
        url = _absolute(raw, base_url or base_doc)
        if not url:
            continue
        is_file = bool(_DATA_FILE_RE.search(url))
        is_hint = bool(_POSTTRADE_HINT_RE.search(url))
        if is_file and (is_hint or url.endswith((".json", ".zip", ".csv"))):
            key = url.split("/")[-1].split("?")[0]
            candidates.append((url, key))

    seen: set[str] = set()
    objects: list[DiscoveredObject] = []
    for url, key in candidates:
        if key in seen:
            continue
        seen.add(key)
        objects.append(
            DiscoveredObject(
                source_id=SOURCE_ID,
                object_key=key,
                url=url,
                publication_timestamp="",  # resolved from the object if present
            )
        )

    if not objects:
        raise SourceDiscoveryError(
            "bme_apa: no machine feed URL resolved from the canonical page; "
            "file links are client-rendered (entrypoint.status=PARTIAL). "
            "Operator must set entrypoint.base_url or a resolvable listing."
        )
    return objects


def fetch(conf: dict[str, Any], http_get: HttpGetter, obj: DiscoveredObject) -> bytes:
    """Fetch the exact bytes of one discovered BME post-trade object."""
    try:
        return http_get(obj.url)
    except Exception as exc:  # noqa: BLE001 - surface as fetch error, fail closed
        raise SourceFetchError(f"bme_apa: fetch failed for {obj.object_key}: {exc}") from exc
