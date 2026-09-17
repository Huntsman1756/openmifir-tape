"""Regression tests for fail-closed hardening: URL scheme allowlist,
max_objects=0 selection, write-once metadata, and cross-platform locking.
All offline — no network is touched.
"""

from pathlib import Path

import pytest

from collectors import sources
from collectors.harness import run_source
from collectors.net import _CheckedRedirectHandler, is_http_url, make_http_get
from collectors.sources import bloomberg_apae, bme_apa
from collectors.sources.base import DiscoveredObject, SourceDiscoveryError
from collectors.storage import RawStore, WriteOnceConflict


def _fake_getter(mapping: dict[str, bytes]):
    def get(url: str) -> bytes:
        if url not in mapping:
            raise RuntimeError(f"unexpected network call to {url}")
        return mapping[url]

    return get


# --- URL scheme allowlist -------------------------------------------------

def test_is_http_url_accepts_only_http_schemes():
    assert is_http_url("https://example.test/x")
    assert is_http_url("http://example.test/x")
    assert not is_http_url("file:///etc/passwd")
    assert not is_http_url("FILE:///C:/Windows/win.ini")
    assert not is_http_url("javascript:alert(1)")
    assert not is_http_url("ftp://example.test/x")
    assert not is_http_url("gopher://example.test/")
    assert not is_http_url("/relative/path.json")
    assert not is_http_url("")


def test_transport_refuses_non_http_without_network():
    get = make_http_get(["example.test"])
    with pytest.raises(ValueError, match="non-HTTP"):
        get("file:///etc/passwd")


# --- SSRF hardening: host allowlist + public-IP-only + redirect re-validation --

def _public_addrinfo(*_a, **_k):
    return [(2, 1, 6, "", ("93.184.216.34", 443))]  # example.com range, public


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"payload"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x",                # loopback
    "http://169.254.169.254/latest",     # link-local cloud metadata
    "http://10.0.0.5/x",                 # RFC1918
    "http://[::1]/x",                    # IPv6 loopback
    "http://evil.test/x",                # host simply not allowlisted
])
def test_transport_refuses_forbidden_targets(url):
    # allowlist contains the literal hosts so refusal at the IP layer is
    # exercised for IP literals, and at the host layer for evil.test
    get = make_http_get(["127.0.0.1", "169.254.169.254", "10.0.0.5", "::1"])
    with pytest.raises(ValueError, match="refusing"):
        get(url)


def test_transport_refuses_host_resolving_to_private_ip(monkeypatch):
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("192.168.1.10", 443))])
    get = make_http_get(["allowed.test"])
    with pytest.raises(ValueError, match="non-public"):
        get("https://allowed.test/x")


def test_transport_refuses_when_dns_fails(monkeypatch):
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("nxdomain")))
    get = make_http_get(["allowed.test"])
    with pytest.raises(ValueError, match="cannot resolve"):
        get("https://allowed.test/x")


def test_transport_allows_public_allowed_url(monkeypatch):
    import socket
    import urllib.request
    monkeypatch.setattr(socket, "getaddrinfo", _public_addrinfo)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda self, req, timeout=None: _FakeResponse())
    get = make_http_get(["www.bloombergapa.com"])
    assert get("https://www.bloombergapa.com/x.csv") == b"payload"


def test_redirect_to_forbidden_destination_refused(monkeypatch):
    import socket
    import urllib.request
    monkeypatch.setattr(socket, "getaddrinfo", _public_addrinfo)
    handler = _CheckedRedirectHandler(frozenset({"www.bolsasymercados.es"}))
    req = urllib.request.Request("https://www.bolsasymercados.es/a.json")
    # allowed host redirecting to link-local metadata -> refused
    with pytest.raises(ValueError, match="refusing"):
        handler.redirect_request(
            req, None, 302, "", {}, "http://169.254.169.254/latest/meta-data")
    # allowed host redirecting to a foreign host -> refused
    with pytest.raises(ValueError, match="refusing"):
        handler.redirect_request(
            req, None, 302, "", {}, "https://attacker.example/x")


def test_redirect_within_allowed_host_followed(monkeypatch):
    import socket
    import urllib.request
    monkeypatch.setattr(socket, "getaddrinfo", _public_addrinfo)
    handler = _CheckedRedirectHandler(frozenset({"www.bolsasymercados.es"}))
    req = urllib.request.Request("https://www.bolsasymercados.es/a.json")
    new = handler.redirect_request(
        req, None, 302, "", {}, "https://www.bolsasymercados.es/b.json")
    assert new is not None and "b.json" in new.full_url


def test_bme_discover_skips_non_http_links():
    doc = "https://www.bolsasymercados.es/en/page.html"
    page = b"""
    <html><body>
    <a href="file:///etc/daily_MiFID_20260914.json">trap</a>
    <a href="javascript:void(0)">noop</a>
    </body></html>
    """
    conf = {"source_id": "bme_apa", "entrypoint": {"base_doc": doc}}
    with pytest.raises(SourceDiscoveryError):
        bme_apa.discover(conf, _fake_getter({doc: page}))


def test_bme_discover_skips_non_http_component_probe():
    doc = "https://www.bolsasymercados.es/en/page.html"
    page = b'<html><div data-six-component-path="file:///etc/comp"></div></html>'
    conf = {"source_id": "bme_apa", "entrypoint": {"base_doc": doc}}
    # The file:// probe must be refused, leaving zero objects -> fail closed.
    with pytest.raises(SourceDiscoveryError):
        bme_apa.discover(conf, _fake_getter({doc: page}))


def test_bloomberg_discover_skips_non_http_download_links():
    page = b"""
    <html><body>
    <a href="file:///etc/BAPA-POST2-20260914-20:22:55.841-02.csv">trap</a>
    </body></html>
    """
    conf = {
        "source_id": "blb_apae",
        "entrypoint": {"public_data_page": "https://www.bloombergapa.com/"},
    }
    with pytest.raises(SourceDiscoveryError):
        bloomberg_apae.discover(conf, _fake_getter(
            {"https://www.bloombergapa.com/": page}))


# --- max_objects=0 selection ----------------------------------------------

FAKE_ID = "fake_max_obj"


class _ManyObjAdapter:
    SOURCE_ID = FAKE_ID

    @classmethod
    def discover(cls, conf, http_get):
        return [
            DiscoveredObject(source_id=FAKE_ID, object_key=f"o{i}.json",
                             url=f"https://example.test/o{i}.json",
                             publication_timestamp=f"2026-09-14T10:0{i}:00Z")
            for i in range(3)
        ]

    @classmethod
    def fetch(cls, conf, http_get, obj):
        return b"{}"


sources.ADAPTERS[FAKE_ID] = _ManyObjAdapter


def test_max_objects_zero_selects_nothing(tmp_path: Path):
    fetched: list[str] = []

    def counting_get(url: str) -> bytes:
        fetched.append(url)
        return b"{}"

    result = run_source(FAKE_ID, {"source_id": FAKE_ID}, RawStore(tmp_path),
                        counting_get, now="2026-09-14T12:00:00+00:00",
                        max_objects=0)
    assert result.succeeded
    assert result.object_count == 0
    assert fetched == []  # -0 bug would have fetched every discovered object


def test_max_objects_none_selects_all(tmp_path: Path):
    result = run_source(FAKE_ID, {"source_id": FAKE_ID}, RawStore(tmp_path),
                        lambda u: b"{}", now="2026-09-14T12:00:00+00:00",
                        max_objects=None)
    assert result.object_count == 3


# --- write-once metadata ----------------------------------------------------

def _store_once(store: RawStore):
    return store.store(
        source_id="s", collection_date="2026-09-14", object_key="o.json",
        data=b"payload", publication_timestamp="2026-09-14T10:00:00Z",
        provenance_url="https://example.test/o.json",
        observation_utc="2026-09-14T12:00:00+00:00",
    )


def test_store_metadata_is_write_once(tmp_path: Path):
    import yaml

    store = RawStore(tmp_path)
    rec = _store_once(store)
    meta = Path(rec.meta_path).read_bytes()

    # A racing first writer's meta must never be overwritten: plant a
    # sentinel meta describing the SAME capture identity, then store again —
    # the sentinel survives and the loser reuses it without failing.
    sentinel_meta = yaml.safe_load(meta)
    sentinel_meta["planted_by_first_writer"] = True
    sentinel = yaml.safe_dump(sentinel_meta).encode()
    Path(rec.meta_path).write_bytes(sentinel)
    rec2 = _store_once(store)
    assert rec2.status == "ALREADY_PRESENT"
    assert Path(rec.meta_path).read_bytes() == sentinel
    assert meta != sentinel  # sanity: the original write did happen


def test_store_metadata_conflict_fails_closed(tmp_path: Path):
    """A meta file that does NOT describe the capture identity is corruption,
    not a legitimate race — refuse rather than silently reuse it."""
    import yaml

    store = RawStore(tmp_path)
    rec = _store_once(store)
    corrupted = yaml.safe_load(Path(rec.meta_path).read_bytes())
    corrupted["raw_sha256"] = "0" * 64
    Path(rec.meta_path).write_text(yaml.safe_dump(corrupted), encoding="utf-8")
    with pytest.raises(WriteOnceConflict):
        _store_once(store)


# --- cross-platform run lock --------------------------------------------------

def test_run_lock_mutually_excludes_second_holder(tmp_path: Path):
    """A held A3 run lock must deny a second holder on POSIX (fcntl) AND
    Windows (msvcrt) — previously this silently degraded to a no-op on
    Windows, allowing concurrent scheduled runs."""
    from collectors import a3

    if a3.fcntl is None and a3.msvcrt is None:
        pytest.skip("no locking primitive on this platform")

    lock = tmp_path / "runner.lock"
    with a3._run_lock(lock) as (acquired, _waited):
        assert acquired
        with open(lock, "a+b") as fd2, pytest.raises(OSError):
            a3._try_lock(fd2)


def test_run_lock_times_out_when_held(tmp_path: Path):
    from collectors import a3

    if a3.fcntl is None and a3.msvcrt is None:
        pytest.skip("no locking primitive on this platform")

    lock = tmp_path / "runner.lock"
    with a3._run_lock(lock) as (acquired, _w):
        assert acquired
        # Zero-timeout second attempt must report not-acquired, not deadlock.
        with a3._run_lock(lock, timeout_s=0.0, sleep=lambda s: None) as (acq2, _w2):
            assert not acq2
