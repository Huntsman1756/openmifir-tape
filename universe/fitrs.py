"""ESMA FITRS non-equity transparency data (auth.045) -> bond classification.

Primary source for the frozen universe's instrument classification: MiFIR ID
(RTS 2 Annex IV Table 2 field 3) and Bond Type (field 9). Verified against a
live ``DLTNCR`` (``auth.045.001.03``) payload during F-008:

- ``NonEqtyTrnsprncyData/Id/ISINAndSubClss/ISIN`` carries the instrument ISIN;
- ``ISINAndSubClss/FinInstrmClssfctn`` carries the MiFIR ID (``BOND`` for
  bonds; ``DERV``/``SDRV`` etc. otherwise);
- ``ISINAndSubClss/DerivSubClss/Desc`` carries the bond type as a published
  label (``Corporate bond``, ``Convertible bond``, ``Covered Bond``,
  ``Sovereign bond``, ``Public bond``, ``Other bonds``);
- ``SgmttnCrit`` ``SACL`` carries the sub-asset class code (``BOND1``..``BOND6``
  in the same order as the labels above — ICMA RTS 2 response).

Bond type labels are mapped to RTS 2 field 9 codes via an explicit frozen
table; an unmapped label yields ``bond_type=None`` (fail closed — the resolver
QUARANTINEs it as ``MISSING_BOND_TYPE``). Nothing is derived from the FIRDS
CFI code.

Offline by construction: the parser consumes in-memory bytes or a file object;
no network access. Uses ``iterparse`` so multi-GB full files stream.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import IO, Iterable

# RTS 2 Annex IV Table 2 field 9 codes for the six FITRS-published bond labels.
FITRS_BOND_TYPE: dict[str, str] = {
    "Corporate bond": "CRPB",
    "Convertible bond": "CVTB",
    "Covered Bond": "CVDB",
    "Sovereign bond": "EUSB",
    "Public bond": "OEPB",
    "Other bonds": "OTHR",
}


class FitrsParseError(Exception):
    """Raised when FITRS bytes cannot be parsed per the documented subset."""


@dataclass(frozen=True)
class FitrsRecord:
    """One non-equity transparency record's instrument classification."""

    instrument_isin: str
    instrument_mifir_id: str
    bond_type: str | None
    bond_type_label: str | None
    sub_asset_class: str | None
    source_report_id: str | None = None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


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


def _record_from(element: ET.Element) -> FitrsRecord | None:
    """Build a record from one ``NonEqtyTrnsprncyData`` element."""
    id_block = _direct_child(element, "Id")
    sub = _direct_child(id_block, "ISINAndSubClss") if id_block is not None else None
    if sub is None:
        return None
    isin = _direct_child_text(sub, "ISIN") or ""
    mifir_id = _direct_child_text(sub, "FinInstrmClssfctn") or ""
    deriv = _direct_child(sub, "DerivSubClss")
    label = _direct_child_text(deriv, "Desc") if deriv is not None else None
    sacl = None
    if deriv is not None:
        for crit in deriv:
            if _local_name(crit.tag) != "SgmttnCrit":
                continue
            if _direct_child_text(crit, "CritNm") == "SACL":
                sacl = _direct_child_text(crit, "CritVal")
                break
    bond_type = FITRS_BOND_TYPE.get(label.strip()) if label else None
    return FitrsRecord(
        instrument_isin=isin,
        instrument_mifir_id=mifir_id,
        bond_type=bond_type,
        bond_type_label=label,
        sub_asset_class=sacl,
        source_report_id=_direct_child_text(element, "TechRcrdId"),
    )


def parse_fitrs(source: bytes | IO[bytes]) -> list[FitrsRecord]:
    """Parse a FITRS non-equity transparency payload (auth.045).

    Fail closed: records without an ``ISINAndSubClss`` block are skipped, an
    unmapped bond label yields ``bond_type=None`` (never guessed), and a
    payload with zero parseable records raises ``FitrsParseError``.
    """
    records: list[FitrsRecord] = []
    try:
        if isinstance(source, bytes):
            import io

            source = io.BytesIO(source)
        for _event, element in ET.iterparse(source):
            if _local_name(element.tag) != "NonEqtyTrnsprncyData":
                continue
            record = _record_from(element)
            element.clear()
            if record is not None:
                records.append(record)
    except ET.ParseError as exc:
        raise FitrsParseError(f"FITRS XML parse failed: {exc}") from exc
    if not records:
        raise FitrsParseError("no <NonEqtyTrnsprncyData> records found in FITRS payload")
    return records


def iter_corporate_bond_isins(records: Iterable[FitrsRecord]) -> set[str]:
    """ISINs classified BOND + CRPB by FITRS transparency data."""
    return {
        r.instrument_isin
        for r in records
        if r.instrument_mifir_id == "BOND" and r.bond_type == "CRPB" and r.instrument_isin
    }
