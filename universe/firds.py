"""Thin FIRDS acquisition adapter: extract RTS 23 field 5 (issuer LEI).

Offline by construction: HTTP is injected (``url -> bytes``). Unit tests pass a
getter that raises on any call, so no test can reach the network.

Caching is write-once and content-addressed. It writes ONLY under an explicit
``cache_dir`` supplied by the caller (the operator's gitignored local data
directory, ``data/``); this module NEVER writes provider bytes into the
repository tree. Cache metadata is a SHA-256 identity, never a payload dump.

The XML subset parsed here mirrors the ESMA FIRDS instrument record: issuer LEI
lives in the ``<Issr>`` element (RTS 23 field 5), the ISIN in
``FinInstrmGnlAttrbts/<Id>`` and the instrument classification (CFI, ISO 10962)
in ``<ClssfctnTp>``.

Verified against a live ``auth.036.001.03`` DLTINS payload during F-005:
``FinInstrm`` wraps a record-kind element (``NewRcrd``/``ModfdRcrd``/
``TermntdRcrd``) that carries ``FinInstrmGnlAttrbts`` and ``Issr`` as direct
children; there is NO ``FinInstrmTp`` element in that schema. The FULINS
full file (``auth.017.001.02``, verified live 2026-09-12 during F-009)
carries the same two fields directly under ``RefData`` record elements —
both record-level tags are matched, and the two schemas never nest inside
each other. Per F-008, the MiFIR ID / Bond Type classification comes from
ESMA FITRS non-equity transparency data (``universe/fitrs.py``, auth.045),
NEVER guessed from CFI.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from defusedxml import ElementTree as ET

FIELD_ISSUER_LEI = 5  # RTS 23 field number for issuer LEI

# Record-level elements across the two FIRDS schemas: ``FinInstrm`` wraps the
# record kind in auth.036 deltas (DLTINS); ``RefData`` is the record element in
# auth.017 full files (FULINS). The two never nest within each other.
_RECORD_TAGS = frozenset({"FinInstrm", "RefData"})

HttpGet = Callable[[str], bytes]


class FirdsParseError(Exception):
    """Raised when FIRDS bytes cannot be parsed per the documented subset."""


@dataclass(frozen=True)
class FirdsInstrument:
    instrument_isin: str
    issuer_lei: str
    cfi_code: str | None = None
    source_report_id: str | None = None


@dataclass(frozen=True)
class CacheRecord:
    raw_sha256: str
    path: str
    size_bytes: int
    created: bool


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == name and child.text:
            return child.text.strip()
    return None


def _direct_child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _direct_child_text(element: ET.Element, name: str) -> str | None:
    child = _direct_child(element, name)
    if child is not None and child.text:
        return child.text.strip()
    return None


def _instrument_from(element: ET.Element) -> FirdsInstrument:
    # The record wrapper (NewRcrd/ModfdRcrd/TermntdRcrd in auth.036, or a
    # direct FinInstrmGnlAttrbts in the synthetic fixture) carries
    # FinInstrmGnlAttrbts; Issr (RTS 23 field 5) is a direct child of the
    # record element — never descend into nested instrument structures.
    wrapper = next((c for c in element if isinstance(c.tag, str)), None)
    if wrapper is None:
        gnl = _direct_child(element, "FinInstrmGnlAttrbts")
        issuer_lei = _direct_child_text(element, "Issr")
    elif _local_name(wrapper.tag) == "FinInstrmGnlAttrbts":
        gnl = wrapper
        issuer_lei = _direct_child_text(element, "Issr")
    else:
        gnl = _direct_child(wrapper, "FinInstrmGnlAttrbts")
        issuer_lei = _direct_child_text(wrapper, "Issr")
    isin = _direct_child_text(gnl, "Id") if gnl is not None else ""
    cfi = _direct_child_text(gnl, "ClssfctnTp") if gnl is not None else None
    report_id = _child_text(element, "FinInstrmRptgRprtSts") or _child_text(element, "RptgRef")
    return FirdsInstrument(
        instrument_isin=isin or "",
        issuer_lei=issuer_lei or "",
        cfi_code=cfi,
        source_report_id=report_id,
    )


def parse_firds(xml_bytes: bytes) -> list[FirdsInstrument]:
    """Parse a FIRDS XML subset into instruments with field 5 issuer LEI.

    Fail closed: a record without an issuer LEI is still emitted with an empty
    LEI string so the resolver can QUARANTINE it; nothing is guessed.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:  # XMLSyntaxError is a subclass
        raise FirdsParseError(f"FIRDS XML parse failed: {exc}") from exc

    instruments = [
        _instrument_from(element)
        for element in root.iter()
        if _local_name(element.tag) in _RECORD_TAGS
    ]
    if not instruments:
        raise FirdsParseError("no <FinInstrm>/<RefData> records found in FIRDS payload")
    return instruments


def iter_firds(source: IO[bytes]) -> Iterator[FirdsInstrument]:
    """Streaming variant for multi-hundred-MB FULINS payloads (iterparse).

    Same fail-closed semantics as :func:`parse_firds`; consumes a file object
    so full reference-data files never materialize as one DOM tree.
    """
    try:
        for _event, element in ET.iterparse(source):
            if _local_name(element.tag) not in _RECORD_TAGS:
                continue
            yield _instrument_from(element)
            element.clear()
    except ET.ParseError as exc:
        raise FirdsParseError(f"FIRDS XML parse failed: {exc}") from exc


def fetch(url: str, http_get: HttpGet) -> bytes:
    """Thin injected fetch. No live network access in this module or tests."""
    return http_get(url)


def cache_raw(
    raw_bytes: bytes,
    cache_dir: Path,
    *,
    source_id: str,
    provenance_url: str,
) -> CacheRecord:
    """Write-once, content-addressed cache under a caller-supplied directory."""
    digest = hashlib.sha256(raw_bytes).hexdigest()
    target_dir = Path(cache_dir) / source_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / digest
    created = False
    if not target.exists():
        try:
            with open(target, "xb") as fh:
                fh.write(raw_bytes)
            created = True
        except FileExistsError:
            created = False
    return CacheRecord(
        raw_sha256=digest,
        path=str(target),
        size_bytes=len(raw_bytes),
        created=created,
    )
