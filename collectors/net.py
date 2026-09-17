"""Live HTTP transport for the operator-side collectors.

Acquisition runs ONLY in the operator environment, never in CI. All live
network access funnels through :func:`make_http_get`, which fails closed on
three layers:

1. **Scheme** — only ``http``/``https``. Any other scheme (``file:``,
   ``ftp:``, ``gopher:`` …) is refused before a request is made, so a
   malformed or hostile source page cannot turn discovery into a
   local-file read.
2. **Host allowlist** — the URL's hostname must be listed in the source
   descriptor's ``allowed_hosts``. A page link pointing anywhere else is
   never followed.
3. **Resolved address** — the hostname is resolved and EVERY returned
   address must be a globally routable IP. Loopback, RFC1918, link-local
   (e.g. cloud metadata at ``169.254.169.254``), multicast, reserved, and
   unspecified addresses are refused — the basic SSRF surface.

Redirects are re-validated on EVERY hop through the same rules, so an
allowed URL that redirects to an internal or foreign destination is refused
rather than followed.

Residual risk: DNS is checked at request time and ``urlopen`` resolves
again at connect time; a hostile resolver could theoretically rebind in
between. Fully pinning the resolved IP would require a custom connection
layer — disproportionate for this threat model. The host allowlist is the
primary control; the IP check is defense in depth.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.request
from collections.abc import Callable, Iterable
from urllib.parse import urlparse

USER_AGENT = "OpenMiFIRTape/0.0.1"
DEFAULT_TIMEOUT_S = 60
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_PORTS = frozenset({80, 443})


def is_http_url(url: str) -> bool:
    """True when ``url`` resolves to an absolute http/https URL."""
    scheme = urlparse(url).scheme.lower()
    return scheme in _ALLOWED_SCHEMES


def _resolve_ips(hostname: str, port: int) -> set[str]:
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"cannot resolve {hostname!r}: {exc}") from exc
    # Strip IPv6 zone identifiers ("fe80::1%eth0") before parsing.
    return {info[4][0].split("%", 1)[0] for info in infos}


def _validate_target(url: str, allowed_hosts: frozenset[str]) -> None:
    """Refuse any URL that is not a plain http(s) request to an explicitly
    allowed host resolving only to public addresses. Raises ValueError."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        scheme = parsed.scheme.lower() or "<none>"
        raise ValueError(f"refusing non-HTTP(S) URL scheme: {scheme!r}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError(f"refusing URL without a hostname: {url!r}")
    if host not in allowed_hosts:
        raise ValueError(f"refusing host not in allowed_hosts: {host!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"refusing URL with invalid port: {url!r}") from exc
    if port is not None and port not in _ALLOWED_PORTS:
        raise ValueError(f"refusing non-standard port {port} for {host!r}")
    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    addrs = _resolve_ips(host, effective_port)
    if not addrs:
        raise ValueError(f"cannot resolve {host!r}: no addresses")
    for raw in addrs:
        ip = ipaddress.ip_address(raw)
        if (not ip.is_global or ip.is_loopback or ip.is_private
                or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified):
            raise ValueError(
                f"refusing {host!r}: resolves to non-public address {ip}")


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validates every redirect target before it is followed."""

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_target(newurl, self._allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_http_get(allowed_hosts: Iterable[str]) -> Callable[[str], bytes]:
    """Build the fail-closed live fetcher for one source.

    ``allowed_hosts`` comes from the source descriptor; every URL — the
    initial request and each redirect hop — must match it AND resolve only
    to public IPs. An empty allowlist refuses everything.
    """
    hosts = frozenset(h.lower().rstrip(".") for h in allowed_hosts)
    opener = urllib.request.build_opener(_CheckedRedirectHandler(hosts))

    def get(url: str) -> bytes:
        _validate_target(url, hosts)
        req = urllib.request.Request(  # noqa: S310 - url passed _validate_target (scheme/host/port/public-IP) above
            url, headers={"User-Agent": USER_AGENT})
        with opener.open(req, timeout=DEFAULT_TIMEOUT_S) as resp:
            return resp.read()

    return get
