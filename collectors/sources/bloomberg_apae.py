"""Bloomberg APAE delayed trade reports source adapter.

Per config/sources/bloomberg_apae.yaml the entrypoint is RESOLVED: the
Bloomberg EU APA publishes delayed post-trade CSV reports publicly with no
authentication (verified 2026-09-14). The page lists files named
BAPA-POST2-<timestamp>.csv under PUBLIC DATA -> Post Trade, with a historical
page for backfill.

No trade normalization. No universe logic.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from ..sources.base import DiscoveredObject, HttpGetter, SourceDiscoveryError, SourceFetchError

SOURCE_ID = "blb_apae"

# A BAPA-POST2 report file is named with a timestamp token, e.g.
# BAPA-POST2-20260914-20:22:55.841-02.csv
_REPORT_TOKEN_RE = re.compile(r"(BAPA-POST2-[0-9:.\-]+\.csv)", re.IGNORECASE)
_URL_RE = re.compile(r"""(?:href|src)=["']([^"']+)["']""")


def _absolute(url: str, base: str) -> str:
    parts = urlparse(base)
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return f"{parts.scheme}://{parts.netloc}{url}"
    return urljoin(base, url)


def _parse_timestamp(token: str) -> str:
    """Return the raw publication trace token from a BAPA-POST2 file name.

    Timezone semantics (e.g. -02) are preserved verbatim; the exact UTC offset
    interpretation is resolved against the CSV schema during normalization
    (G0-C). Here we only carry the source's own token. Fail closed: if no token
    is present we return the file name.
    """
    m = re.search(r"BAPA-POST2-(?P<token>[0-9:.\-]+)\.csv", token, re.IGNORECASE)
    return m.group("token") if m else token


def discover(conf: dict[str, Any], http_get: HttpGetter) -> list[DiscoveredObject]:
    """Resolve the current Bloomberg APAE delayed report file(s)."""
    entry = conf.get("entrypoint", {})
    page = entry.get("public_data_page", entry.get("base_url")) or ""
    if not page:
        raise SourceDiscoveryError("blb_apae: no entrypoint.public_data_page configured")

    html = http_get(page).decode("utf-8", "replace")

    seen: dict[str, DiscoveredObject] = {}
    # 1) Links whose URL or text carries a BAPA-POST2 file.
    for raw in _URL_RE.findall(html) + ['' for _ in range(0)]:
        url = _absolute(raw, page)
        m = _REPORT_TOKEN_RE.search(url)
        if m:
            token = m.group(1)
            obj = DiscoveredObject(
                source_id=SOURCE_ID,
                object_key=token,
                url=url,
                publication_timestamp=_parse_timestamp(token),
            )
            seen[token] = obj
    # 2) download?key=BAPA-POST2-...csv entries from the button text.
    for m in re.finditer(r"download\?key=([^\"'&]+)", html):
        key = m.group(1)
        if not re.search(r"BAPA-POST2", key, re.IGNORECASE):
            continue
        url = urljoin(page, "download?key=" + key)
        seen[key] = DiscoveredObject(
            source_id=SOURCE_ID,
            object_key=key,
            url=url,
            publication_timestamp=_parse_timestamp(key),
        )

    if not seen:
        raise SourceDiscoveryError(
            "blb_apae: no BAPA-POST2 report file resolved on the public data page."
        )

    # Deterministic ordering, newest last; caller decides which to capture.
    objects = sorted(seen.values(), key=lambda o: o.publication_timestamp)
    return objects


def fetch(conf: dict[str, Any], http_get: HttpGetter, obj: DiscoveredObject) -> bytes:
    """Fetch the exact bytes of one Bloomberg APAE delayed report."""
    try:
        return http_get(obj.url)
    except Exception as exc:  # noqa: BLE001
        raise SourceFetchError(f"blb_apae: fetch failed for {obj.object_key}: {exc}") from exc
