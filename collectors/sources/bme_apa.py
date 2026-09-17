"""BME APA post-trade JSON source adapter.

Per config/sources/bme_apa.yaml the entrypoint is RESOLVED. The deterministic
machine listing was discovered by inspecting the official page's own JS bundle:
the AEM taxonomy-filter component fetches ``${componentPath}.results.json``,
which returns ``{"results":[{"url":..., "publicationDate":<epoch-millis>},...]}``
of daily ``<YYYY-MM-DD>-bmea-posttrade.json`` files.

Discovery prefers the frozen ``entrypoint.listing_url``. If it is absent it
falls back to parsing the canonical page's static HTML / advertised component
path. In all cases the adapter FAILS CLOSED (G0-A.6): it never guesses a URL.

No trade normalization. No universe logic.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from ..net import is_http_url
from .base import DiscoveredObject, HttpGetter, SourceDiscoveryError, SourceFetchError

_DATA_FILE_RE = re.compile(r"\.(json|zip|csv|txt)(\?|$)", re.IGNORECASE)
_POSTTRADE_HINT_RE = re.compile(r"post[-_ ]?trade|transparenc|mifir", re.IGNORECASE)
_URL_RE = re.compile(r"""(?:href|src)=["']([^"']+)["']""")
_COMPONENT_PATH_RE = re.compile(r"""data-six-component-path=["']([^"']+)["']""")

SOURCE_ID = "bme_apa"


def _epoch_ms_to_iso(ms: Any) -> str:
    """Convert a Unix epoch milliseconds value to UTC ISO-8601.

    Fail closed on malformed input (never guess); raises SourceDiscoveryError.
    """
    try:
        ms_val = float(ms)
    except (TypeError, ValueError) as exc:
        raise SourceDiscoveryError(f"bme_apa: non-numeric publicationDate: {ms!r}") from exc
    try:
        return datetime.fromtimestamp(ms_val / 1000.0, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise SourceDiscoveryError(f"bme_apa: out-of-range publicationDate: {ms!r}") from exc


def _absolute(url: str, base: str) -> str:
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        from urllib.parse import urlparse

        parts = urlparse(base)
        return f"{parts.scheme}://{parts.netloc}{url}"
    from urllib.parse import urljoin

    return urljoin(base, url)


def _discover_from_listing(listing_url: str, http_get: HttpGetter) -> list[DiscoveredObject]:
    """Fetch the results.json listing and build DiscoveredObjects."""
    try:
        body = http_get(listing_url)
    except Exception as exc:
        raise SourceDiscoveryError(f"bme_apa: listing fetch failed: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise SourceDiscoveryError(f"bme_apa: listing is not valid JSON: {exc}") from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise SourceDiscoveryError("bme_apa: listing JSON has no 'results' array")

    objects: list[DiscoveredObject] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url or not is_http_url(url):
            # Non-http(s) entries (file:, javascript:, malformed) are unusable
            # fetch targets — skipped, never fetched (fail closed).
            continue
        key = url.split("/")[-1].split("?")[0]
        if not key or key in seen:
            continue
        seen.add(key)
        pub = item.get("publicationDate")
        pub_iso = _epoch_ms_to_iso(pub) if pub not in (None, "") else ""
        objects.append(
            DiscoveredObject(
                source_id=SOURCE_ID,
                object_key=key,
                url=url,
                publication_timestamp=pub_iso,
            )
        )
    if not objects:
        raise SourceDiscoveryError("bme_apa: listing returned no usable post-trade file URLs")
    # The listing is newest-first; return ascending by publication_timestamp so
    # the newest object is LAST (harness selects the tail).
    objects.sort(key=lambda o: o.publication_timestamp)
    return objects


def _discover_from_page(base_url: str, base_doc: str, http_get: HttpGetter) -> list[DiscoveredObject]:
    """Fallback: parse static HTML links and advertised AEM component paths."""
    html = http_get(base_url or base_doc).decode("utf-8", "replace")

    candidates: list[tuple[str, str]] = []
    for raw in _URL_RE.findall(html):
        url = _absolute(raw, base_url or base_doc)
        if not url or not is_http_url(url):
            continue
        is_file = bool(_DATA_FILE_RE.search(url))
        is_hint = bool(_POSTTRADE_HINT_RE.search(url))
        if is_file and (is_hint or url.endswith((".json", ".zip", ".csv"))):
            key = url.split("/")[-1].split("?")[0]
            candidates.append((url, key))

    for comp_path in _COMPONENT_PATH_RE.findall(html):
        for sel in (".results.json", ".model.json", ".json"):
            probe_url = comp_path + sel
            if not is_http_url(probe_url):
                continue
            try:
                body = http_get(probe_url).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001, S112 - optional probe, fail closed
                continue
            for raw in _URL_RE.findall(body):
                url = _absolute(raw, comp_path)
                if not is_http_url(url) or not _DATA_FILE_RE.search(url):
                    continue
                key = url.split("/")[-1].split("?")[0]
                if key:
                    candidates.append((url, key))
            for quoted in re.findall(r'"([^"]+\.(?:json|zip|csv|txt))"', body, re.IGNORECASE):
                url = _absolute(quoted, comp_path)
                if not is_http_url(url):
                    continue
                key = url.split("/")[-1].split("?")[0]
                if key:
                    candidates.append((url, key))

    seen: set[str] = set()
    objects: list[DiscoveredObject] = []
    for url, key in candidates:
        if key in seen:
            continue
        seen.add(key)
        objects.append(DiscoveredObject(source_id=SOURCE_ID, object_key=key, url=url, publication_timestamp=""))
    return objects


def discover(conf: dict[str, Any], http_get: HttpGetter) -> list[DiscoveredObject]:
    """Resolve BME post-trade JSON file URLs (listing URL first, then page fallback)."""
    entry = conf.get("entrypoint", {})
    listing_url = entry.get("listing_url") or ""
    base_url = entry.get("base_url") or ""
    base_doc = entry.get("base_doc") or ""

    if listing_url:
        return _discover_from_listing(listing_url, http_get)

    if not base_url and not base_doc:
        raise SourceDiscoveryError("bme_apa: no entrypoint.listing_url and no base_url/base_doc configured")

    objects = _discover_from_page(base_url, base_doc, http_get)
    if not objects:
        raise SourceDiscoveryError(
            "bme_apa: no machine feed URL resolved from the canonical page; "
            "file links are client-rendered (entrypoint.status=PARTIAL). "
            "Operator must set entrypoint.listing_url or a resolvable listing."
        )
    return objects


def fetch(conf: dict[str, Any], http_get: HttpGetter, obj: DiscoveredObject) -> bytes:
    """Fetch the exact bytes of one discovered BME post-trade object."""
    try:
        return http_get(obj.url)
    except Exception as exc:
        raise SourceFetchError(f"bme_apa: fetch failed for {obj.object_key}: {exc}") from exc
