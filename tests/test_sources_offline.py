"""Offline, synthetic-fixture tests for the source adapters (NO network)."""

import pytest

from collectors.sources import bme_apa, bloomberg_apae
from collectors.sources.base import SourceDiscoveryError
from collectors.sources.bloomberg_apae import _parse_timestamp


def _fake_getter(mapping: dict[str, bytes]):
    def get(url: str) -> bytes:
        if url not in mapping:
            raise RuntimeError(f"unexpected network call to {url}")
        return mapping[url]

    return get


# --- Bloomberg APAE ---

BLB_PAGE = b"""
<html><body>
<h1>Public Data</h1>
<a href="https://www.bloombergapa.com/download?key=BAPA-POST2-20260914-20:22:55.841-02.csv">
  BAPA-POST2-20260914-20:22:55.841-02.csv</a>
</body></html>
"""

BLB_CONF = {
    "source_id": "blb_apae",
    "entrypoint": {"public_data_page": "https://www.bloombergapa.com/"},
}


def test_bloomberg_parse_timestamp_token():
    assert _parse_timestamp("BAPA-POST2-20260914-20:22:55.841-02.csv") == "20260914-20:22:55.841-02"
    assert _parse_timestamp("weird") == "weird"


def test_bloomberg_discover_and_fetch_offline():
    key_url = "https://www.bloombergapa.com/download?key=BAPA-POST2-20260914-20:22:55.841-02.csv"
    getter = _fake_getter({"https://www.bloombergapa.com/": BLB_PAGE, key_url: b"csv-bytes"})
    objects = bloomberg_apae.discover(BLB_CONF, getter)
    assert objects
    assert objects[0].object_key == "BAPA-POST2-20260914-20:22:55.841-02.csv"
    assert objects[0].publication_timestamp == "20260914-20:22:55.841-02"
    data = bloomberg_apae.fetch(BLB_CONF, getter, objects[0])
    assert data == b"csv-bytes"


def test_bloomberg_discover_no_file_raises():
    getter = _fake_getter({"https://www.bloombergapa.com/": b"<html>no files</html>"})
    with pytest.raises(SourceDiscoveryError):
        bloomberg_apae.discover(BLB_CONF, getter)


# --- BME APA ---

BME_DOC = "https://www.bolsasymercados.es/en/other-services/regulatory-services/post-trade-data.html"
BME_PAGE_WITH_FILE = b"""
<html><body>
<a href="/other-services/post-trade/daily_MiFID_20260914.json">JSON</a>
</body></html>
"""
BME_CONF = {"source_id": "bme_apa", "entrypoint": {"base_doc": BME_DOC}}


def test_bme_discover_finds_data_file():
    getter = _fake_getter({BME_DOC: BME_PAGE_WITH_FILE})
    objects = bme_apa.discover(BME_CONF, getter)
    assert objects
    assert objects[0].object_key == "daily_MiFID_20260914.json"
    assert objects[0].url.startswith("https://www.bolsasymercados.es/")


def test_bme_discover_no_file_raises():
    getter = _fake_getter({BME_DOC: b"<html>client-rendered, no links</html>"})
    with pytest.raises(SourceDiscoveryError):
        bme_apa.discover(BME_CONF, getter)


def test_bme_discover_no_entrypoint_raises():
    with pytest.raises(SourceDiscoveryError):
        bme_apa.discover({"source_id": "bme_apa", "entrypoint": {"base_doc": ""}}, lambda u: b"")


def test_bme_discover_probes_component_path():
    comp = "https://www.bolsasymercados.es/en/other-services/regulatory-services/post-trade-data/_jcr_content/root/containers/container/grid/container0/assetstaxonomyfilter"
    page = f'<html><div data-six-component-path="{comp}"></div></html>'.encode()
    model = b'{"items":[{"url":"/content/dam/bme/posttrade/2026-09-14_equity.json"}]}'
    getter = _fake_getter({
        BME_DOC: page,
        comp + ".model.json": model,
    })
    objects = bme_apa.discover(BME_CONF, getter)
    assert objects
    assert objects[0].object_key == "2026-09-14_equity.json"
