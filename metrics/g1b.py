"""G1-B1 — OBSERVED_UNIVERSE_DISPOSITION (frozen docs/gates/G1.md §3).

Batch resolution of the verified G1-A OBSERVED_INSTRUMENT_SET through the
frozen ``es_legal_issuer_v1`` chain (docs/gates/G0.md §3, unchanged):

    FITRS auth.045:  MiFIR ID = BOND AND Bond Type = CRPB (SACL primary)
        -> FIRDS RTS 23 field 5: issuer LEI
        -> GLEIF: LegalJurisdiction.country = ES
        -> INCLUDE / EXCLUDE / QUARANTINE / CONFLICT

Branch decisions come EXCLUSIVELY from
``universe.resolver.resolve_disposition`` — this module assembles the
resolver's inputs (shared ``universe.assemble`` semantics, identical to the
frozen G0-B1/F-009 path) and records the outcomes; it implements no
precedence of its own and never steers a record toward a branch.

Input authority is verified cryptographically, not recomputed and not
trusted by declaration: the embedded G1-A ``semantic_digest`` is checked
against a recomputation over the artifact's instrument entries AND against
the verified expected value — any mutation fails closed.

Reference inputs are frozen BUILD_SNAPSHOT sets cited per family as
canonical ``{name, sha256, bytes}`` entries + ``set_digest``
(``universe.reference``, G1 §4.4 representation). The GLEIF family covers
EXACTLY the artifacts for the LEIs the resolver demonstrably reaches —
unrelated cache residue can never alter the reference basis.

GLEIF completeness rule (hard): ``LEI absent from local cache`` is NOT
"authoritatively unresolved". Required LEIs are derived by probing the
frozen resolver itself against an empty GLEIF index — an LEI is required
iff the probe reaches ``UNRESOLVABLE_LEGAL_JURISDICTION``. Every required
LEI needs an explicit acquisition outcome — RESOLVED /
NOT_FOUND(authoritative) / ACQUISITION_FAILED / NOT_ATTEMPTED. Any
FAILED/NOT_ATTEMPTED required LEI raises ``AcquisitionIncomplete`` — a
HOLD, never a fabricated QUARANTINE.

Traceability: besides the disposition rows, a ``reference_links`` artifact
binds every ISIN to the concrete reference observations that supported its
disposition — deterministic handles ``<file>:<member>:<ordinal>`` resolved
against each family's canonical entry order, source-published report ids
when present, and the exact pinned GLEIF artifact per required LEI. The
three family digests collapse to one ``reference_basis_id``; later G1-C
capsules cite (isin, reference_basis_id, handles).

Output: metadata-only report YAML + canonical ISIN-sorted rows + link
JSONL. No provider payload bytes, no filesystem paths, deterministic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from universe._paths import repo_file
from universe.assemble import (
    assemble_security_record,
    required_gleif_leis_by_isin,
)
from universe.firds import iter_firds
from universe.fitrs import parse_fitrs
from universe.gleif import GleifEntity
from universe.gleif_acquire import (
    NOTFOUND_SUFFIX,
    OUTCOME_ACQUISITION_FAILED,
    OUTCOME_NOT_ATTEMPTED,
    OUTCOME_NOT_FOUND,
    OUTCOMES,
    load_acquired_entities,
    notfound_path,
    payload_path,
)
from universe.model import Branch, Reason
from universe.reference import (
    canonical_reference_set,
    reference_entry,
    reference_set_from_files,
)
from universe.resolver import resolve_disposition

from .evidence import _git_head
from .g1a import digest_entries

# Verified G1-A1 authority (issue #20). The batch run consumes exactly this
# observed set — anything else fails closed before any resolution happens.
G1A_ARTIFACT = repo_file(
    "evidence", "g1-a1", "G1-A1_observed_instrument_set.yaml")
EXPECTED_G1A_SEMANTIC_DIGEST = \
    "f87b1b2fe5c35a506885036ad153494d93a67cedc7fe74062e893223c5139f71"

ROWS_NAME = "G1-B1_disposition_rows.jsonl"
LINKS_NAME = "G1-B1_reference_links.jsonl"
REPORT_NAME = "G1-B1_observed_universe_disposition.yaml"

# Row-schema legend (JSONL rows stay compact to remain well inside the
# evidence size budget; every key is documented here).
ROW_SCHEMA = {
    "isin": "observed instrument identifier (frozen G1-A input)",
    "fitrs": {
        "n": "FITRS records observed for the ISIN",
        "mifir": "resolved MiFIR ID (null = absent/undetermined)",
        "bond": "resolved bond type (null = absent/undetermined)",
        "sacl": "observed SACL criterion",
        "desc": "observed Desc label (emitted only when it diverges "
                "from the SACL-mapped label or SACL is absent)",
        "amb": "FITRS fields with contradictory observations",
    },
    "firds": {
        "n": "FIRDS records observed for the ISIN",
        "lei": "resolved issuer LEI (null = absent/contradictory)",
        "conflict": "FIRDS issuer-LEI contradiction flag",
    },
    "gleif": {
        "acq": "acquisition outcome for the resolved LEI "
               "(RESOLVED/NOT_FOUND; null = lookup not required)",
        "ok": "GLEIF entity resolved with a LegalJurisdiction country "
              "(null when the jurisdiction step was not reached)",
        "cc": "LegalJurisdiction.country (ISO 3166 alpha-2)",
    },
    "branch": "frozen disposition (INCLUDE/EXCLUDE/QUARANTINE/CONFLICT)",
    "reason": "frozen reason code",
    "notes": "diagnostics (absent = none)",
}

LINK_SCHEMA = {
    "i": "observed instrument identifier (row key)",
    "fitrs": {
        "<file_idx>:<member_idx>": {
            "o": "record ordinals within that member's parse stream "
                 "(ascending; full handle = '<file_idx>:<member_idx>:<o>')",
            "r": "parallel array of source-published report ids — emitted "
                 "only when every record in the group carries one",
        },
    },
    "firds": "same grouped grammar — '<file_idx>:<member_idx>' -> "
             "{o: [ordinals], r: [report_ids]?}",
    "gleif": "exact GLEIF family entry name consulted "
             "(<LEI>.json / <LEI>.notfound.json; null when no lookup)",
}

# Frozen-stage -> reference families whose observations supported the
# disposition. Derived from the resolver's own precedence; the links
# artifact only cites observations the decision actually consumed.
REASON_REFERENCE_STAGE: dict[Reason, tuple[str, ...]] = {
    Reason.MISSING_MIFIR_ID: ("fitrs",),
    Reason.NOT_BOND: ("fitrs",),
    Reason.MISSING_BOND_TYPE: ("fitrs",),
    Reason.NOT_CRPB: ("fitrs",),
    Reason.MISSING_ISSUER_LEI: ("fitrs", "firds"),
    Reason.INVALID_ISSUER_LEI: ("fitrs", "firds"),
    Reason.IDENTITY_CONFLICT: ("fitrs", "firds"),
    Reason.UNRESOLVABLE_LEGAL_JURISDICTION: ("fitrs", "firds", "gleif"),
    Reason.NON_ES_LEGAL_JURISDICTION: ("fitrs", "firds", "gleif"),
    Reason.IN_ES_CRPB_UNIVERSE: ("fitrs", "firds", "gleif"),
}


class G1BError(Exception):
    """Fail-closed abort for G1-B (input-authority mismatch, broken
    invariants, missing inputs)."""


@dataclass(frozen=True)
class RefObs:
    """One concrete reference observation plus its deterministic handle."""

    file: str       # reference file basename (zip entry of the family set)
    member: int     # index in the archive's sorted *.xml member list
    ordinal: int    # record ordinal within that member's parse stream
    report_id: str | None
    record: Any     # FitrsRecord | FirdsInstrument


def load_observed_isins(
    artifact_path: Path = G1A_ARTIFACT,
    *,
    expected_digest: str = EXPECTED_G1A_SEMANTIC_DIGEST,
) -> list[str]:
    """Load the verified G1-A observed set; fail closed on any deviation.

    The embedded ``semantic_digest`` is never trusted alone: the canonical
    G1-A digest is recomputed over the artifact's instrument entries and
    MUST equal the embedded value AND the expected authority. A mutated
    entry with a stale embedded digest fails closed.
    """
    if not artifact_path.is_file():
        raise G1BError(f"G1-A artifact not found: {artifact_path}")
    doc = yaml.safe_load(artifact_path.read_bytes().decode("utf-8"))
    observed = (doc or {}).get("observed_instrument_set") or {}
    instruments = observed.get("instruments") or []
    embedded = observed.get("semantic_digest")
    recomputed = digest_entries(instruments)
    if embedded != recomputed:
        raise G1BError(
            f"G1-A embedded semantic_digest {embedded!r} != recomputed "
            f"{recomputed!r} — artifact entries do not match their "
            "declared digest")
    if recomputed != expected_digest:
        raise G1BError(
            f"G1-A semantic digest {recomputed!r} != expected authority "
            f"{expected_digest!r} — refusing to resolve an unverified "
            "observed set")
    isins = [e.get("isin") for e in instruments if isinstance(e, dict)]
    if any(not isinstance(i, str) or not i.strip() for i in isins):
        raise G1BError("G1-A artifact contains an empty/non-string ISIN")
    declared = observed.get("instrument_count")
    if declared != len(instruments):
        raise G1BError(
            f"G1-A instrument_count {declared!r} != {len(instruments)} rows")
    if len(set(isins)) != len(isins):
        raise G1BError("G1-A artifact contains duplicate ISINs")
    return sorted(isins)


def scan_fitrs(
    zip_paths: list[Path],
    wanted: set[str],
) -> dict[str, list[RefObs]]:
    """Stream the pinned FITRS zips; keep handled records for observed
    ISINs only.

    ``parse_fitrs`` iterparses each member, so multi-hundred-MB XML is never
    materialized as a DOM; records outside the observed set are dropped
    immediately while still yielding a deterministic
    (file, member, ordinal) handle for every kept observation.
    """
    by_isin: dict[str, list[RefObs]] = {}
    for zp in sorted(zip_paths, key=lambda p: p.name):
        if not zp.is_file():
            raise G1BError(f"FITRS snapshot missing: {zp}")
        with zipfile.ZipFile(zp) as z:
            members = sorted(
                n for n in z.namelist() if n.lower().endswith(".xml"))
            for member_idx, name in enumerate(members):
                with z.open(name) as fh:
                    for ordinal, record in enumerate(parse_fitrs(fh)):
                        if record.instrument_isin in wanted:
                            by_isin.setdefault(
                                record.instrument_isin, []).append(RefObs(
                                    file=zp.name, member=member_idx,
                                    ordinal=ordinal,
                                    report_id=record.source_report_id,
                                    record=record))
    return by_isin


def scan_firds(
    zip_paths: list[Path],
    wanted: set[str],
) -> dict[str, list[RefObs]]:
    """Stream the pinned FIRDS zips; keep handled records for observed
    ISINs only (same handle grammar as :func:`scan_fitrs`)."""
    by_isin: dict[str, list[RefObs]] = {}
    for zp in sorted(zip_paths, key=lambda p: p.name):
        if not zp.is_file():
            raise G1BError(f"FIRDS snapshot missing: {zp}")
        with zipfile.ZipFile(zp) as z:
            members = sorted(
                n for n in z.namelist() if n.lower().endswith(".xml"))
            for member_idx, name in enumerate(members):
                with z.open(name) as fh:
                    for ordinal, instrument in enumerate(iter_firds(fh)):
                        if instrument.instrument_isin in wanted:
                            by_isin.setdefault(
                                instrument.instrument_isin, []).append(
                                    RefObs(
                                        file=zp.name, member=member_idx,
                                        ordinal=ordinal,
                                        report_id=instrument
                                        .source_report_id,
                                        record=instrument))
    return by_isin


def _records(obs: Iterable[RefObs]) -> list:
    return [o.record for o in obs]


def _desc_is_diagnostic(assembled) -> bool:
    """Emit ``desc`` only where it carries evidence the code path used:
    SACL absent/unknown, or a label that could not be cross-checked."""
    if assembled.desc is None:
        return False
    return assembled.sacl is None or assembled.bond_type is None


def _row(isin: str, assembled, disp, gleif_acquisition: str | None) -> dict:
    """One canonical disposition row (see ROW_SCHEMA)."""
    fitrs: dict[str, Any] = {
        "n": assembled.fitrs_count,
        "mifir": assembled.mifir_id,
        "bond": assembled.bond_type,
        "sacl": assembled.sacl,
    }
    if _desc_is_diagnostic(assembled):
        fitrs["desc"] = assembled.desc
    ambiguous = [f for f, flag in (
        ("mifir", assembled.mifir_ambiguous),
        ("bond", assembled.bond_type_ambiguous)) if flag]
    if ambiguous:
        fitrs["amb"] = ambiguous
    firds: dict[str, Any] = {
        "n": assembled.firds_count,
        "lei": assembled.issuer_lei,
    }
    if assembled.lei_conflict:
        firds["conflict"] = True
    # GLEIF fields are populated only when the jurisdiction step was
    # actually reached (the LEI was a required LEI); otherwise the lookup
    # never happened and every field is null.
    consulted = gleif_acquisition is not None
    gleif: dict[str, Any] = {
        "acq": gleif_acquisition,
        "ok": (bool(assembled.gleif and assembled.gleif.resolved)
               if consulted else None),
        "cc": (assembled.gleif.legal_jurisdiction_country
               if consulted and assembled.gleif else None),
    }
    row: dict[str, Any] = {
        "isin": isin,
        "fitrs": fitrs,
        "firds": firds,
        "gleif": gleif,
        "branch": disp.branch.value,
        "reason": disp.reason.value,
    }
    if assembled.notes:
        row["notes"] = list(assembled.notes)
    return row


def canonical_rows_bytes(rows: list[dict[str, Any]]) -> bytes:
    """Canonical serialization: ISIN-sorted, one compact JSON object per
    line — the semantic-digest material."""
    ordered = sorted(rows, key=lambda r: r["isin"])
    return ("\n".join(
        json.dumps(r, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False) for r in ordered) + "\n") \
        .encode("utf-8")


def canonical_links_bytes(links: list[dict[str, Any]]) -> bytes:
    """Canonical serialization of reference-link lines (same grammar)."""
    ordered = sorted(links, key=lambda r: r["i"])
    return ("\n".join(
        json.dumps(r, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False) for r in ordered) + "\n") \
        .encode("utf-8")


def reference_basis_id(set_digests: dict[str, str]) -> str:
    """One canonical id binding the FITRS/FIRDS/GLEIF family digests."""
    return hashlib.sha256(json.dumps(
        {"fitrs": set_digests["fitrs"], "firds": set_digests["firds"],
         "gleif": set_digests["gleif"]},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _group_handles(
    obs: Iterable[RefObs],
    file_index: dict[str, int],
) -> dict[str, Any]:
    """Group observation handles by '<file_idx>:<member_idx>'.

    ``o`` carries the ascending record ordinals; ``r`` is a parallel array
    of source-published report ids, emitted only when every record in the
    group carries one (a missing id never fabricates a citation — the
    ordinal handle always suffices).
    """
    groups: dict[str, dict[str, Any]] = {}
    for o in sorted(obs, key=lambda o: (o.file, o.member, o.ordinal)):
        key = f"{file_index[o.file]}:{o.member}"
        g = groups.setdefault(key, {"o": [], "r": []})
        g["o"].append(o.ordinal)
        g["r"].append(o.report_id)
    out: dict[str, Any] = {}
    for key in sorted(groups):
        g = groups[key]
        entry: dict[str, Any] = {"o": g["o"]}
        if all(r is not None for r in g["r"]):
            entry["r"] = g["r"]
        out[key] = entry
    return out


def build_reference_links(
    isins: list[str],
    fitrs_obs: dict[str, list[RefObs]],
    firds_obs: dict[str, list[RefObs]],
    required_by_isin: dict[str, str],
    gleif_census: dict[str, str],
    reason_by_isin: dict[str, str],
    *,
    fitrs_set: dict,
    firds_set: dict,
    gleif_set: dict,
) -> list[dict[str, Any]]:
    """ISIN-keyed concrete reference-observation bindings (LINK_SCHEMA).

    Stage-aware: only the families whose observations the disposition
    actually consumed are cited (REASON_REFERENCE_STAGE). The GLEIF link
    names the exact pinned artifact entry for THIS ISIN's required LEI —
    never an LEI merely present in the shared set. A gleif link for an
    ISIN that did not reach the jurisdiction step (or vice versa) is an
    internal inconsistency and fails closed.
    """
    file_index = {
        "fitrs": {e["name"]: i
                  for i, e in enumerate(fitrs_set["entries"])},
        "firds": {e["name"]: i
                  for i, e in enumerate(firds_set["entries"])},
    }
    gleif_names = {e["name"] for e in gleif_set["entries"]}
    links: list[dict[str, Any]] = []
    for isin in sorted(isins):
        try:
            stage = REASON_REFERENCE_STAGE[Reason(reason_by_isin[isin])]
        except KeyError as exc:
            raise G1BError(
                f"no reference stage for {isin} reason "
                f"{reason_by_isin.get(isin)!r}") from exc
        link: dict[str, Any] = {"i": isin}
        if "fitrs" in stage:
            freps = _group_handles(fitrs_obs.get(isin, []),
                                   file_index["fitrs"])
            if freps:
                link["fitrs"] = freps
        if "firds" in stage:
            rreps = _group_handles(firds_obs.get(isin, []),
                                   file_index["firds"])
            if rreps:
                link["firds"] = rreps
        gleif_ref = None
        lei = required_by_isin.get(isin)
        if lei is not None:
            if "gleif" not in stage:
                raise G1BError(
                    f"{isin}: resolver reached jurisdiction but stage "
                    f"for reason {reason_by_isin[isin]!r} excludes gleif")
            suffix = (NOTFOUND_SUFFIX
                      if gleif_census[lei] == OUTCOME_NOT_FOUND
                      else ".json")
            name = f"{lei}{suffix}"
            if name not in gleif_names:
                raise G1BError(
                    f"GLEIF link target {name!r} absent from the GLEIF "
                    "reference set")
            gleif_ref = name
        link["gleif"] = gleif_ref
        links.append(link)
    return links


def build_dispositions(
    isins: list[str],
    fitrs_obs: dict[str, list[RefObs]],
    firds_obs: dict[str, list[RefObs]],
    gleif_entities: dict[str, GleifEntity],
    gleif_census: dict[str, str],
    required_by_isin: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve every observed ISIN; return (rows, diagnostics).

    100% disposition invariant — enforced here before any evidence exists:
    exactly one row per distinct input ISIN, every row carrying one frozen
    branch + one frozen reason. Any violation raises ``G1BError``.
    """
    if len(set(isins)) != len(isins):
        raise G1BError("duplicate input ISINs rejected before resolution")
    rows: list[dict[str, Any]] = []
    diagnostics = {
        "fitrs_mifir_id_ambiguous_isins": 0,
        "fitrs_bond_type_ambiguous_isins": 0,
        "firds_issuer_lei_conflict_isins": 0,
        "isins_absent_from_fitrs": 0,
        "isins_absent_from_firds": 0,
        "gleif_resolved_isins": 0,
        "gleif_not_found_isins": 0,
    }
    for isin in sorted(isins):
        assembled = assemble_security_record(
            isin,
            _records(fitrs_obs.get(isin) or []),
            _records(firds_obs.get(isin) or []),
            gleif_entities,
        )
        disp = resolve_disposition(assembled.record)
        # GLEIF provenance only when THIS ISIN reached the jurisdiction
        # step (per-ISIN reachability from the resolver probe) — never
        # because its LEI happens to be required by another instrument.
        required_lei = required_by_isin.get(isin)
        gleif_acq = gleif_census.get(required_lei) \
            if required_lei is not None else None
        rows.append(_row(isin, assembled, disp, gleif_acq))
        if assembled.mifir_ambiguous:
            diagnostics["fitrs_mifir_id_ambiguous_isins"] += 1
        if assembled.bond_type_ambiguous:
            diagnostics["fitrs_bond_type_ambiguous_isins"] += 1
        if assembled.lei_conflict:
            diagnostics["firds_issuer_lei_conflict_isins"] += 1
        if not assembled.fitrs_count:
            diagnostics["isins_absent_from_fitrs"] += 1
        if not assembled.firds_count:
            diagnostics["isins_absent_from_firds"] += 1
        if gleif_acq is not None and assembled.gleif \
                and assembled.gleif.resolved:
            diagnostics["gleif_resolved_isins"] += 1
        if gleif_acq == OUTCOME_NOT_FOUND:
            diagnostics["gleif_not_found_isins"] += 1

    branches = Counter(r["branch"] for r in rows)
    reasons = Counter(r["reason"] for r in rows)
    unknown = set(branches) - {b.value for b in Branch}
    if unknown:
        raise G1BError(f"unknown disposition branches emitted: {unknown}")
    if len(rows) != len(isins) or {r["isin"] for r in rows} != set(isins):
        raise G1BError("100% disposition invariant violated")
    if sum(branches.values()) != len(isins):
        raise G1BError("disposition branch counts do not cover every ISIN")
    diagnostics["branch_counts"] = {
        b.value: branches.get(b.value, 0) for b in Branch}
    diagnostics["reason_counts"] = dict(sorted(reasons.items()))
    diagnostics["fitrs_observed_isins"] = \
        len(isins) - diagnostics["isins_absent_from_fitrs"]
    diagnostics["firds_observed_isins"] = \
        len(isins) - diagnostics["isins_absent_from_firds"]
    return rows, diagnostics


def _gleif_set_for_required(gleif_dir: Path, required: set[str]) -> dict:
    """Canonical GLEIF family over EXACTLY the required LEIs' terminal
    artifacts — unrelated cache residue can never enter the basis."""
    entries = []
    for lei in sorted(required):
        payload = payload_path(gleif_dir, lei)
        marker = notfound_path(gleif_dir, lei)
        if payload.is_file():
            entries.append(reference_entry(payload.name,
                                           payload.read_bytes()))
        elif marker.is_file():
            entries.append(reference_entry(marker.name,
                                           marker.read_bytes()))
        else:
            raise G1BError(
                f"required LEI {lei} has no pinned GLEIF artifact")
    return canonical_reference_set(entries)


def build_report(
    observed_path: Path,
    fitrs_paths: list[Path],
    firds_paths: list[Path],
    gleif_dir: Path,
    *,
    date: str,
    generated_utc: str,
    expected_g1a_digest: str = EXPECTED_G1A_SEMANTIC_DIGEST,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the OBSERVED_UNIVERSE_DISPOSITION report + canonical rows +
    reference links.

    Raises ``AcquisitionIncomplete`` (HOLD) when any required LEI lacks a
    terminal acquisition outcome; raises ``G1BError`` on input-authority or
    invariant violations.
    """
    isins = load_observed_isins(observed_path,
                                expected_digest=expected_g1a_digest)
    wanted = set(isins)

    fitrs_set = reference_set_from_files(fitrs_paths)
    firds_set = reference_set_from_files(firds_paths)
    fitrs_obs = scan_fitrs(fitrs_paths, wanted)
    firds_obs = scan_firds(firds_paths, wanted)

    # Required-LEI derivation by probing the frozen resolver — FIRDS alone
    # cannot decide reachability (an early NOT_BOND/NOT_CRPB/… exit means
    # the jurisdiction step is never needed). The map is per-ISIN so a row
    # can only cite GLEIF when its own chain reached that step.
    required_by_isin = required_gleif_leis_by_isin(
        isins,
        {k: _records(v) for k, v in fitrs_obs.items()},
        {k: _records(v) for k, v in firds_obs.items()})
    required = set(required_by_isin.values())
    gleif_entities, gleif_census = load_acquired_entities(gleif_dir,
                                                        required)
    gleif_set = _gleif_set_for_required(gleif_dir, required)

    rows, diagnostics = build_dispositions(
        isins, fitrs_obs, firds_obs, gleif_entities, gleif_census,
        required_by_isin)
    reason_by_isin = {r["isin"]: r["reason"] for r in rows}
    links = build_reference_links(
        isins, fitrs_obs, firds_obs, required_by_isin, gleif_census,
        reason_by_isin,
        fitrs_set=fitrs_set, firds_set=firds_set, gleif_set=gleif_set)

    rows_bytes = canonical_rows_bytes(rows)
    links_bytes = canonical_links_bytes(links)
    disposition_digest = hashlib.sha256(rows_bytes).hexdigest()
    links_digest = hashlib.sha256(links_bytes).hexdigest()
    basis_id = reference_basis_id({
        "fitrs": fitrs_set["set_digest"],
        "firds": firds_set["set_digest"],
        "gleif": gleif_set["set_digest"]})

    acquisition_counts = {o: sum(1 for v in gleif_census.values() if v == o)
                          for o in OUTCOMES}
    complete = not (acquisition_counts[OUTCOME_ACQUISITION_FAILED]
                    or acquisition_counts[OUTCOME_NOT_ATTEMPTED])
    if not complete:
        raise G1BError(  # defensive: load_acquired_entities already raised
            "GLEIF acquisition census is incomplete")

    report: dict[str, Any] = {
        "gate": "G1",
        "issue_id": "G1-B1",
        "report": "OBSERVED_UNIVERSE_DISPOSITION",
        "date": date,
        "generated_utc": generated_utc,
        "frozen_authority": {
            "spec": "docs/gates/G1.md#G1-B + docs/gates/G0.md §3 "
                    "(es_legal_issuer_v1)",
            "freeze_tag": "g1-freeze-v0",
            "freeze_commit": "2a5e4684b9a3f6066e6d2a53cbc37b920e2c9ffa",
        },
        "input_authority": {
            "artifact": "evidence/g1-a1/G1-A1_observed_instrument_set.yaml",
            "semantic_digest": expected_g1a_digest,
            "digest_verification":
                "embedded digest recomputed over instrument entries; "
                "recomputed == embedded == expected is required",
            "observed_identifier_count": len(isins),
        },
        "generator": {
            "module": "metrics.g1b",
            "code_commit": _git_head(),
            "deterministic": True,
        },
        "reference_basis": "BUILD_SNAPSHOT",
        "reference_basis_id": basis_id,
        "reference_sets": {
            "fitrs": fitrs_set,
            "firds": firds_set,
            "gleif": gleif_set,
        },
        "reference_binding": (
            "every row resolves against reference_basis_id (the canonical "
            "digest over the three family set_digests); per-ISIN concrete "
            "observation handles live in the reference_links artifact "
            "keyed by isin — the row's deterministic identity"),
        "reference_links": {
            "file": LINKS_NAME,
            "sha256": links_digest,
            "handle_grammar": LINK_SCHEMA,
        },
        "reference_acquisition": {
            "gleif": {
                "interface": "GLEIF API v1 /lei-records/{lei} "
                             "(api.gleif.org)",
                "required_leis": len(required),
                "outcome_counts": acquisition_counts,
                "complete": complete,
            },
        },
        "row_schema": ROW_SCHEMA,
        "semantics": (
            "Batch es_legal_issuer_v1 resolution over the verified G1-A "
            "OBSERVED_INSTRUMENT_SET. Every observed identifier received "
            "exactly one frozen disposition via universe.resolver."
            "resolve_disposition; QUARANTINE and CONFLICT are valid domain "
            "outcomes, not run failures. No ISIN-prefix heuristics, no "
            "fuzzy issuer matching, no manual overrides, no provider "
            "payload bytes."),
        "dispositions": {
            "identifier_count": len(rows),
            "rows_file": ROWS_NAME,
            "rows_sha256": disposition_digest,
            "disposition_semantic_digest": disposition_digest,
            **diagnostics,
        },
        "hygiene": {
            "provider_payloads_in_repo": False,
            "raw_directory_tracked": False,
        },
    }
    return report, rows, links


def generate_report(
    observed_path: Path,
    fitrs_paths: list[Path],
    firds_paths: list[Path],
    gleif_dir: Path,
    evidence_dir: Path,
    *,
    date: str,
    generated_utc: str,
) -> Path:
    report, rows, links = build_report(
        observed_path, fitrs_paths, firds_paths, gleif_dir,
        date=date, generated_utc=generated_utc)
    ev = evidence_dir / "g1-b1"
    ev.mkdir(parents=True, exist_ok=True)
    rows_path = ev / ROWS_NAME
    rows_path.write_bytes(canonical_rows_bytes(rows))
    links_path = ev / LINKS_NAME
    links_path.write_bytes(canonical_links_bytes(links))
    path = ev / REPORT_NAME
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(report, fh, sort_keys=False)
    for artifact in (path, rows_path, links_path):
        sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        sidecar = artifact.parent / f"{artifact.stem}.sha256.txt"
        sidecar.write_text(f"{sha} *{artifact.name}\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--observed-set", type=Path, default=G1A_ARTIFACT,
                    help="verified G1-A OBSERVED_INSTRUMENT_SET artifact")
    ap.add_argument("--fitrs", type=Path, nargs="+", required=True,
                    help="pinned FITRS snapshot zip(s)")
    ap.add_argument("--firds", type=Path, nargs="+", required=True,
                    help="pinned FIRDS snapshot zip(s)")
    ap.add_argument("--gleif-dir", type=Path, required=True,
                    help="pinned GLEIF acquisition directory "
                         "(<LEI>.json / <LEI>.notfound.json)")
    ap.add_argument("--out", type=Path, default=Path("evidence"))
    ap.add_argument("--date", required=True)
    ap.add_argument("--generated-utc", required=True)
    args = ap.parse_args(argv)
    path = generate_report(
        args.observed_set, args.fitrs, args.firds, args.gleif_dir,
        args.out, date=args.date, generated_utc=args.generated_utc)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
