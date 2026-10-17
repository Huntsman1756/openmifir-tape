"""Offline content validation for frozen G2-B timed reference bundles (#48).

Records are private replay inputs, never evidence artifacts. Every declared
file/member is parsed before this loader returns; unavailable outcomes carry
no substitute payload from an earlier episode.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from defusedxml.common import DefusedXmlException

from universe.firds import FirdsParseError, iter_firds
from universe.fitrs import FitrsParseError, parse_fitrs
from universe.gleif import GleifParseError, parse_gleif

from .g2_authority import AuthorityError, checked_file, strict_pairs
from .g2_reference_timeline import prepare_bundle


def _xml_records(path: Path, family: str):
    with zipfile.ZipFile(path) as archive:
        members = sorted(n for n in archive.namelist() if n.lower().endswith(".xml"))
        if not members or len(members) != len(set(members)):
            raise AuthorityError("reference ZIP requires unique XML members")
        for member in members:
            count = 0
            with archive.open(member) as stream:
                records = parse_fitrs(stream) if family == "FITRS" else iter_firds(stream)
                for ordinal, record in enumerate(records):
                    count += 1
                    identifier = record.instrument_isin
                    if identifier is None or identifier == "":
                        raise AuthorityError(
                            f"reference record without instrument identifier: {member}#{ordinal}"
                        )
                    yield member, ordinal, identifier, record
            if not count:
                raise AuthorityError("reference XML member has no parseable records")


def _gleif_records(path: Path):
    doc = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=strict_pairs)
    data = doc.get("data") if isinstance(doc, dict) else None
    items = [data] if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise AuthorityError("GLEIF requires a nonempty data object/array")
    for ordinal, item in enumerate(items):
        if not isinstance(item, dict):
            raise AuthorityError("GLEIF data entries must be objects")
        # Parse independently: repeated LEIs must not disappear via last-wins.
        entities = parse_gleif(json.dumps({"data": item}).encode())
        for lei, entity in entities.items():
            yield None, ordinal, lei, entity


def open_reference_bundle(root: Path, manifest: dict, *, wanted_isins: set[str] | None = None) -> dict:
    """Authenticate, fully parse, and return local records plus canonical metadata.

    Optional ISIN filtering bounds retained memory, never parsing/validation.
    Every physical metadata copy is authenticated by prepare_bundle; payload
    copies with identical hashes have identical content semantics. Requested
    identifiers absent from an AVAILABLE observation's files are reported per
    family/observation in ``not_found`` (empty when nothing is missing).
    """
    prepared = prepare_bundle(root, manifest)
    indexes = {family: {} for family in prepared["bundle"]["observations"]}
    not_found = {family: {} for family in prepared["bundle"]["observations"]}
    for family, observations in prepared["bundle"]["observations"].items():
        for obs in observations:
            scope = obs["scope"]
            requested = None if scope["mode"] == "ALL_FILE_RECORDS" else set(scope["identifiers"])
            seen = set()
            for timed in obs["files"]:
                entry = timed["file"]
                path = checked_file(prepared["observation_roots"][obs["observation_id"]], entry)
                try:
                    rows = _gleif_records(path) if family == "GLEIF" else _xml_records(path, family)
                    for member, ordinal, identifier, record in rows:
                        if requested is not None and identifier not in requested:
                            continue
                        if requested is not None:
                            seen.add(identifier)
                        if family != "GLEIF" and wanted_isins is not None and identifier not in wanted_isins:
                            continue
                        handle = {"observation_id": obs["observation_id"], "file": entry,
                                  "member": member, "ordinal": ordinal,
                                  "report_id": getattr(record, "source_report_id", None)}
                        indexes[family].setdefault(identifier, []).append({"record": record, "handle": handle})
                except AuthorityError:
                    raise
                except (ValueError, TypeError, AttributeError, OSError, zipfile.BadZipFile, RuntimeError,
                        FitrsParseError, FirdsParseError, GleifParseError, DefusedXmlException) as exc:
                    raise AuthorityError(f"unparseable declared {family} file: {entry['name']}") from exc
            if requested is not None and obs["outcome"] == "AVAILABLE":
                missing = sorted(requested - seen)
                if missing:
                    not_found[family][obs["observation_id"]] = missing
    return {**prepared, "indexes": indexes, "not_found": not_found}
