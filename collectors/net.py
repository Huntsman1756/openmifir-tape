"""Live HTTP transport for the operator-side collectors.

Acquisition runs ONLY in the operator environment, never in CI. All live
network access funnels through :func:`default_http_get`, which fails closed:
only ``http``/``https`` URLs are fetched. Any other scheme (``file:``,
``ftp:``, ``gopher:`` …) is refused before a request is made, so a malformed
or hostile source page cannot turn discovery into a local-file read.
"""

from __future__ import annotations

import urllib.request
from urllib.parse import urlparse

USER_AGENT = "OpenMiFIRTape/0.0.1"
DEFAULT_TIMEOUT_S = 60
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def is_http_url(url: str) -> bool:
    """True when ``url`` resolves to an absolute http/https URL."""
    scheme = urlparse(url).scheme.lower()
    return scheme in _ALLOWED_SCHEMES


def default_http_get(url: str) -> bytes:
    """Fetch the exact response bytes for one http/https URL.

    Fails closed: a non-http(s) URL raises ``ValueError`` instead of being
    handed to ``urlopen`` (which would otherwise honour ``file://`` etc.).
    """
    if not is_http_url(url):
        scheme = urlparse(url).scheme.lower() or "<none>"
        raise ValueError(f"refusing non-HTTP(S) URL scheme: {scheme!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_S) as resp:
        return resp.read()
