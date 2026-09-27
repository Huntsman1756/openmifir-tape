"""G1-C3 — deterministic EVIDENCE_CAPSULE / ``explain`` (frozen G1 §4.3).

For any persisted ``publications`` row or ``trade_chains`` head — current
or historical ``as_of(T)`` — the tape emits a deterministic, metadata-only
capsule answering: why does this datum exist, from which observed evidence,
under which normalization mapping, at which lifecycle position, and what
was knowable exactly at T.

Contract (frozen, ``docs/gates/G1.md`` §4.3, tag ``g1-freeze-v0``):

- stable row id derived ONLY from immutable occurrence identity:
  ``sha256(canonical_json({source, source_object_id, capture_version,
  capture_record_ordinal}))`` — independent of filesystem root, Parquet
  row number/physical order, current lifecycle head, generation time,
  worktree name or machine;
- current chains explain the materialized G0-D ``chain[-1]`` with ordered
  members and supersession links taken from the persisted tape
  (self-consistency re-verified); historical chains are REBUILT through
  the verified G1-C2 path (``known_at <= T`` -> G0-D) — the materialized
  ``trade_chains`` head is never reused to explain the past, and no
  publication/lifecycle evidence with ``known_at > T`` may appear
  anywhere in the capsule (members, supersession links, markers);
- field provenance is the STORED ``rts2_mapping`` (schema-v2 replay
  substrate) — an absent mapping stays absent, never guessed;
- universe provenance cites the authenticated G1-B reference basis
  stage-aware: only the families whose observations the disposition
  actually consumed (``metrics.g1b.REASON_REFERENCE_STAGE``) plus the
  concrete observation handles from the byte-authenticated
  ``reference_links`` artifact, resolved against the pinned family set
  entry order;
- authority wiring is fail-closed end to end: the C2 parent report's
  bytes MUST hash to the pinned SHA-256 before its declared dataset
  digest is trusted, and the physical tape MUST recompute to that exact
  digest; the G1-B report, disposition rows and reference_links bytes are
  authenticated the same way;
- canonical serialization: UTF-8 JSON, sorted keys, compact separators,
  single trailing newline; no filesystem paths, no generation timestamp,
  no randomness — same tape + same request => byte-identical capsule.

This issue does NOT implement TAPE_MANIFEST / ``omt verify`` (G1-C4) or
G1-D divergence. The 2026 MiFIR transition (FITRS quantitative reporting
decommissioned; Art. 26-derived upstream calculations) is documented in
``docs/legal/mifir_2026_transition.md`` — capsules separate the three
axes explicitly: observed publication source (owned evidence), pinned
reference authority, and upstream regulatory calculation basis
(not owned).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from lifecycle.ledger import MARKER_UNCHAINABLE, build_ledger_indexed
from lifecycle.lifecycle import reconstruct_chains
from normalization.replay import ReplayError, record_from_evidence
from universe._paths import repo_file
from universe.model import Reason

from .evidence import _git_head
from .g1b import REASON_REFERENCE_STAGE
from .g1c1 import (
    CHAIN_COLUMNS,
    CHAIN_PARQUET,
    EXPECTED_G1B_BASIS_ID,
    EXPECTED_G1B_REPORT_SHA256,
    EXPECTED_G1B_ROWS_DIGEST,
    PUB_COLUMNS,
    TAPE_SCHEMA_VERSION,
    canonical_table_bytes,
    dataset_semantic_digest,
    pub_sort_key,
    verify_g1b_authority,
)
from .g1c2 import G1C2Error, _connect_publications, _parse_at

# Verified G1-C2 parent authority (issue #26 — the committed C2 evidence).
# The C3 capsule binds to its declared dataset digest; the report bytes
# MUST authenticate before anything in it is trusted — recomputed, never
# trusted by declaration.
EXPECTED_C2_REPORT_SHA256 = \
    "4bf02dec7c84a9e17d45fcacac3f77c6305fbd8adaf5263bd91086da5467c621"
EXPECTED_DATASET_SEMANTIC_DIGEST = \
    "ecb1cd7bca5a645783ae6053e57c8a0af0b98421c810111e61117678abbbcee8"
EXPECTED_G1B_LINKS_SHA256 = \
    "6fc07be6952ac48f7820af1286ead636222113c048ac8b0e624ee5167ecc51c1"

# Stable logical identifiers emitted as provenance — operator-supplied
# filesystem paths never enter committed evidence or capsule bytes.
C2_REPORT_LOGICAL_ID = "evidence/g1-c2/G1-C2_asof_reconstruction.yaml"
G1B_REPORT_LOGICAL_ID = \
    "evidence/g1-b1/G1-B1_observed_universe_disposition.yaml"
G1B_ROWS_LOGICAL_ID = "evidence/g1-b1/G1-B1_disposition_rows.jsonl"
G1B_LINKS_LOGICAL_ID = "evidence/g1-b1/G1-B1_reference_links.jsonl"

CAPSULE_SCHEMA_VERSION = 1
REPORT_NAME = "G1-C3_evidence_capsule.yaml"


class G1C3Error(Exception):
    """Fail-closed abort for G1-C3 (unknown/duplicate identity, authority
    mismatch, future-evidence leakage, unresolved lifecycle member)."""


# ---------------------------------------------------------------- canon --

def _canonical(obj: Any) -> bytes:
    """Canonical capsule bytes: sorted keys, compact separators, UTF-8,
    one trailing newline. This is the byte-identity contract."""
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def _sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def capsule_sha256(capsule: dict[str, Any]) -> str:
    """SHA-256 over the canonical capsule bytes EXCLUDING the digest
    field itself (self-reference is impossible)."""
    body = {k: v for k, v in capsule.items() if k != "capsule_sha256"}
    return _sha(_canonical(body))


def capsule_bytes(capsule: dict[str, Any]) -> bytes:
    """The exact canonical bytes emitted for a capsule."""
    return _canonical(capsule)


def publication_id(source: str, source_object_id: str,
                   capture_version: str,
                   capture_record_ordinal: int) -> str:
    """Stable publication-occurrence identity — immutable inputs only."""
    material = {
        "source": source,
        "source_object_id": source_object_id,
        "capture_version": capture_version,
        "capture_record_ordinal": capture_record_ordinal,
    }
    return _sha(_canonical(material))


def row_publication_id(row: dict[str, Any]) -> str:
    return publication_id(row["source"], row["source_object_id"],
                          row["capture_version"],
                          row["capture_record_ordinal"])


# ------------------------------------------------------------ authority --

def authenticate_c2_report(
    c2_report_path: Path,
    *,
    expected_sha256: str = EXPECTED_C2_REPORT_SHA256,
    expected_dataset_digest: str = EXPECTED_DATASET_SEMANTIC_DIGEST,
) -> dict[str, Any]:
    """Fail-closed authentication of the verified C2 parent report.

    Exists -> exact bytes -> sha256 == pinned -> only then YAML is parsed;
    the parsed report MUST declare tape_schema_version 2 and the pinned
    dataset digest. Returns ``{sha256, dataset_semantic_digest}``.
    """
    if not c2_report_path.is_file():
        raise G1C3Error(
            f"verified C2 report not found: {c2_report_path} — parent "
            "authority is mandatory")
    data = c2_report_path.read_bytes()
    sha = _sha(data)
    if sha != expected_sha256:
        raise G1C3Error(
            f"C2 report sha256 {sha!r} != expected {expected_sha256!r} — "
            "refusing unverified parent authority")
    doc = yaml.safe_load(data.decode("utf-8")) or {}
    if doc.get("tape_schema_version") != TAPE_SCHEMA_VERSION:
        raise G1C3Error(
            f"C2 report tape_schema_version "
            f"{doc.get('tape_schema_version')!r} != {TAPE_SCHEMA_VERSION}")
    declared = ((doc.get("tape_materialization_v2") or {})
                .get("dataset_semantic_digest"))
    if declared != expected_dataset_digest:
        raise G1C3Error(
            f"C2 report dataset digest {declared!r} != expected "
            f"{expected_dataset_digest!r} — dataset authority mismatch")
    return {"sha256": sha, "dataset_semantic_digest": declared}


def authenticate_g1b(
    g1b_rows_path: Path,
    g1b_report_path: Path,
    g1b_links_path: Path,
    *,
    expected_rows_digest: str = EXPECTED_G1B_ROWS_DIGEST,
    expected_report_sha256: str = EXPECTED_G1B_REPORT_SHA256,
    expected_basis_id: str = EXPECTED_G1B_BASIS_ID,
    expected_links_sha256: str = EXPECTED_G1B_LINKS_SHA256,
) -> dict[str, Any]:
    """Authenticate the full G1-B authority: report, rows AND
    reference_links bytes. Returns the verified basis, family set
    digests, per-family pinned entries and the parsed links index."""
    g1b = verify_g1b_authority(
        g1b_rows_path, g1b_report_path,
        expected_rows_digest=expected_rows_digest,
        expected_report_sha256=expected_report_sha256,
        expected_basis_id=expected_basis_id)

    doc = yaml.safe_load(g1b_report_path.read_bytes().decode("utf-8"))

    if not g1b_links_path.is_file():
        raise G1C3Error(
            f"G1-B reference_links not found: {g1b_links_path}")
    links_bytes = g1b_links_path.read_bytes()
    links_sha = _sha(links_bytes)
    declared_links = ((doc.get("reference_links") or {}).get("sha256"))
    if links_sha != expected_links_sha256 \
            or links_sha != declared_links:
        raise G1C3Error(
            f"G1-B reference_links sha256 {links_sha!r} != expected "
            f"{expected_links_sha256!r} (report declares "
            f"{declared_links!r}) — refusing unverified link authority")

    links: dict[str, dict[str, Any]] = {}
    for line in links_bytes.decode("utf-8").splitlines():
        row = json.loads(line)
        if not isinstance(row.get("i"), str):
            raise G1C3Error("reference_links row without ISIN key")
        if row["i"] in links:
            raise G1C3Error(
                f"duplicate reference_links row for {row['i']}")
        links[row["i"]] = row

    ref_sets = doc.get("reference_sets") or {}
    set_digests: dict[str, str] = {}
    entries: dict[str, list[dict[str, Any]]] = {}
    for fam, famset in ref_sets.items():
        sd = famset.get("set_digest")
        ents = famset.get("entries")
        if not isinstance(sd, str) or not isinstance(ents, list):
            raise G1C3Error(f"G1-B reference_sets.{fam} malformed")
        for e in ents:
            if set(e) != {"name", "sha256", "bytes"}:
                raise G1C3Error(
                    f"G1-B reference_sets.{fam} entry malformed: {e!r}")
        set_digests[fam] = sd
        entries[fam] = ents
    return {
        "report_sha256": g1b["report_sha256"],
        "rows_digest": g1b["rows_digest"],
        "links_sha256": links_sha,
        "reference_basis_id": g1b["reference_basis_id"],
        "set_digests": set_digests,
        "entries": entries,
        "links": links,
        "dispositions": g1b["dispositions"],
    }


def verify_tape(tape_dir: Path, *,
                expected_dataset_digest: str) -> dict[str, Any]:
    """Recompute the physical tape's canonical digests and require the
    dataset semantic digest == the authenticated expectation. The tape
    itself — not just the report that describes it — is authenticated."""
    con = _connect_publications(tape_dir)
    try:
        try:
            cur = con.execute("SELECT * FROM publications")
            pub_cols = [c[0] for c in cur.description]
            pub_rows = cur.fetchall()
        except Exception as exc:
            raise G1C3Error(
                f"cannot read publications parquet under {tape_dir}: "
                f"{exc}") from exc
    finally:
        con.close()
    missing = [c for c in PUB_COLUMNS if c not in pub_cols]
    if missing:
        raise G1C3Error(
            f"tape publications schema missing columns {missing} — "
            "schema v2 required")
    pubs = [dict(zip(pub_cols, r, strict=True)) for r in pub_rows]

    import duckdb
    con = duckdb.connect()
    try:
        p = str((tape_dir / CHAIN_PARQUET).resolve()).replace("\\", "/")
        try:
            cur = con.execute(
                f"SELECT * FROM '{p}'")  # noqa: S608 - path literal
            chain_cols = [c[0] for c in cur.description]
            chain_rows = cur.fetchall()
        except Exception as exc:
            raise G1C3Error(
                f"cannot read trade_chains parquet under {tape_dir}: "
                f"{exc}") from exc
    finally:
        con.close()
    chains = [dict(zip(chain_cols, r, strict=True)) for r in chain_rows]

    pub_digest = _sha(canonical_table_bytes(pubs, PUB_COLUMNS,
                                            pub_sort_key))
    chain_digest = _sha(canonical_table_bytes(
        chains, CHAIN_COLUMNS, lambda r: r["chain_id"]))
    dataset = dataset_semantic_digest(pub_digest, len(pubs),
                                      chain_digest, len(chains))
    if dataset != expected_dataset_digest:
        raise G1C3Error(
            f"tape dataset semantic digest {dataset!r} != expected "
            f"{expected_dataset_digest!r} — tape is not the verified "
            "schema-v2 materialization")
    return {"publications": pubs, "chains": chains,
            "publications_digest": pub_digest,
            "chains_digest": chain_digest,
            "dataset_semantic_digest": dataset}


# ---------------------------------------------------------- provenance --

_CORE_PROVENANCE_FIELDS = (
    "price", "quantity", "quantity_in_measurement_unit",
    "notional_amount", "price_currency", "notional_currency",
    "price_notation", "transaction_identification_code",
    "instrument_identification_code", "instrument_id_code_type",
    "venue_of_execution", "venue_of_publication",
    "trading_datetime", "publication_datetime",
    "notation_of_quantity_measurement_unit",
)


def _economic_provenance(row: dict[str, Any]) -> dict[str, Any]:
    """Source-field provenance from the STORED rts2_mapping only.

    Absent mapping stays absent — nothing is inferred from name
    similarity.
    """
    try:
        ev = json.loads(row["normalized_evidence_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise G1C3Error(
            "stored normalized_evidence_json is not parseable") from exc
    mapping = ev.get("rts2_mapping")
    if not isinstance(mapping, dict) \
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in mapping.items()):
        raise G1C3Error(
            "stored rts2_mapping is not a str->str mapping")
    return {
        "field_source_map": {f: mapping[f] for f in
                             _CORE_PROVENANCE_FIELDS if f in mapping},
        "unmapped_core_fields":
            [f for f in _CORE_PROVENANCE_FIELDS if f not in mapping],
        "normalized_evidence_sha256":
            _sha(row["normalized_evidence_json"]),
        "raw_field_count": ev.get("raw_field_count"),
    }


def _resolve_family_handles(groups: dict[str, Any], family: str,
                            entries: list[dict[str, Any]]) -> list:
    """Resolve '<file_idx>:<member_idx>' -> {o:[...], r:[...]?} link
    groups against the pinned family entry order. Any index outside the
    pinned set fails closed."""
    out = []
    for key in sorted(groups, key=lambda k: tuple(
            int(p) for p in k.split(":"))):
        parts = key.split(":")
        try:
            file_idx, member_idx = int(parts[0]), int(parts[1])
        except (ValueError, IndexError) as exc:
            raise G1C3Error(
                f"malformed {family} handle group {key!r}") from exc
        if not 0 <= file_idx < len(entries) or member_idx < 0:
            raise G1C3Error(
                f"{family} handle {key!r} points outside the pinned "
                f"family set ({len(entries)} entries)")
        entry = entries[file_idx]
        grp = groups[key]
        ordinals = grp.get("o")
        if not isinstance(ordinals, list) or not ordinals \
                or not all(isinstance(o, int) and not isinstance(o, bool)
                           and o >= 0 for o in ordinals):
            raise G1C3Error(
                f"{family} handle {key!r} has no record ordinals")
        resolved = {
            "file": entry["name"],
            "file_sha256": entry["sha256"],
            "file_bytes": entry["bytes"],
            "member_ordinal": member_idx,
            "record_ordinals": ordinals,
        }
        if "r" in grp:
            resolved["source_report_ids"] = grp["r"]
        out.append(resolved)
    return out


def _reference_authority(isin: str, reason: str,
                         g1b: dict[str, Any]) -> dict[str, Any]:
    """Stage-aware reference citation for one ISIN.

    Only the families the frozen resolver actually consumed for THIS
    reason are cited; concrete handles resolve against the authenticated
    pinned sets. Any link citing a family outside the stage — or a handle
    escaping the pinned set — fails closed.
    """
    try:
        stage = set(REASON_REFERENCE_STAGE[Reason(reason)])
    except KeyError as exc:
        raise G1C3Error(
            f"no reference stage for disposition reason {reason!r}") \
            from exc
    link = g1b["links"].get(isin)
    if link is None:
        raise G1C3Error(
            f"{isin}: no reference_links row — cannot bind concrete "
            "reference observations")
    cited = {f for f in ("fitrs", "firds", "gleif") if link.get(f)}
    outside = cited - stage
    if outside:
        raise G1C3Error(
            f"{isin}: reference_links cite {sorted(outside)} but the "
            f"disposition reason {reason!r} only consumed "
            f"{sorted(stage)} — stage-aware citation violated")

    set_digests = {f: g1b["set_digests"][f] for f in sorted(stage)}
    observations: dict[str, Any] = {}
    for fam in sorted(stage):
        grp = link.get(fam)
        if fam in ("fitrs", "firds") and grp:
            observations[fam] = _resolve_family_handles(
                grp, fam, g1b["entries"][fam])
        elif fam == "gleif" and grp:
            names = {e["name"] for e in g1b["entries"]["gleif"]}
            if grp not in names:
                raise G1C3Error(
                    f"{isin}: gleif link {grp!r} absent from the pinned "
                    "GLEIF family set")
            entry = next(e for e in g1b["entries"]["gleif"]
                         if e["name"] == grp)
            observations["gleif"] = {
                "entry": entry["name"],
                "sha256": entry["sha256"],
                "bytes": entry["bytes"],
            }
    return {
        "basis": "BUILD_SNAPSHOT",
        "reference_basis_id": g1b["reference_basis_id"],
        "families_cited": sorted(stage),
        "set_digests": set_digests,
        "observations": observations,
        "reference_links_sha256": g1b["links_sha256"],
        "regulatory": {
            "observed_publication_source":
                "provider APA publication (captured evidence owned by "
                "OpenMifir)",
            "reference_authority_kind": "pinned_reference_set",
            "regulatory_calculation_basis":
                "MiFIR RTS 2 transparency regime; post-2026 calculations "
                "derive from Art. 26 transaction reporting upstream — "
                "those payloads are not OpenMifir evidence",
            "upstream_reference_payloads_owned": False,
        },
    }


# -------------------------------------------------------------- capsule --

def _publication_section(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "publication_id": row_publication_id(row),
        "source": row["source"],
        "source_object_id": row["source_object_id"],
        "capture_version": row["capture_version"],
        "capture_record_ordinal": row["capture_record_ordinal"],
        "raw_sha256": row["raw_sha256"],
        "known_at": row["known_at"],
        "last_observed_at": row["last_observed_at"],
        "observation_count": row["observation_count"],
        "publication_utc": row["publication_utc"],
        "trading_utc": row["trading_utc"],
        "isin": row["isin"],
        "transaction_id": row["transaction_id"],
        "price": row["price"],
        "quantity": row["quantity"],
        "notional": row["notional"],
        "currency": row["currency"],
        "price_currency": row["price_currency"],
        "price_notation": row["price_notation"],
        "flags": sorted(row["flags"] or []),
        "source_report_id": row["source_report_id"],
    }


def _member(entry_id: str, state: str, supersedes: str | None,
            markers: list[str], row: dict[str, Any],
            publication_utc: str | None) -> dict[str, Any]:
    """One lifecycle member. ``entry_id``/``state``/``supersedes``/
    ``markers``/``publication_utc`` come from the authoritative source
    for the capsule's temporal context — materialized columns for
    ``current``, the reconstructed G0-D entries for ``as_of``; ``row``
    supplies only immutable occurrence fields (identity, known_at)."""
    return {
        "entry_id": entry_id,
        "publication_id": row_publication_id(row),
        "state": state,
        "supersedes_entry_id": supersedes,
        "known_at": row["known_at"],
        "publication_utc": publication_utc,
        "ledger_markers": sorted(markers or []),
    }


def _assemble_capsule(subject: dict[str, Any], mode: str,
                      as_of_utc: str | None,
                      head_row: dict[str, Any],
                      members: list[dict[str, Any]],
                      entry_id_scope: str,
                      chain_id: str, head_entry_id: str,
                      head_state: str, event_count: int,
                      g1b: dict[str, Any],
                      c2: dict[str, Any]) -> dict[str, Any]:
    capsule: dict[str, Any] = {
        "capsule_schema_version": CAPSULE_SCHEMA_VERSION,
        "subject": subject,
        "temporal": {"mode": mode, "as_of_utc": as_of_utc},
        "publication": _publication_section(head_row),
        "economic_provenance": _economic_provenance(head_row),
        "lifecycle": {
            "entry_id_scope": entry_id_scope,
            "chain_id": chain_id,
            "head_entry_id": head_entry_id,
            "head_state": head_state,
            "event_count": event_count,
            "members": members,
        },
        "universe": {
            "disposition": head_row["universe_disposition"],
            "reason": head_row["universe_disposition_reason"],
            "issuer_lei": head_row["issuer_lei"],
        },
        "reference_authority": _reference_authority(
            head_row["isin"],
            head_row["universe_disposition_reason"], g1b),
        "authority": {
            "c2_report": C2_REPORT_LOGICAL_ID,
            "c2_report_sha256": c2["sha256"],
            "tape_schema_version": TAPE_SCHEMA_VERSION,
            "tape_dataset_semantic_digest":
                c2["dataset_semantic_digest"],
        },
        "generator": {
            "module": "metrics.g1c3",
            "code_commit": _git_head(),
            "deterministic": True,
        },
    }
    capsule["capsule_sha256"] = capsule_sha256(capsule)
    return capsule


class _ExplainContext:
    """Authenticated context shared by explain calls in one process."""

    def __init__(self, tape_dir: Path, c2_report_path: Path,
                 g1b_rows_path: Path, g1b_report_path: Path,
                 g1b_links_path: Path, *,
                 expected_c2_report_sha256: str,
                 expected_dataset_digest: str,
                 expected_g1b_digest: str,
                 expected_g1b_report_sha256: str,
                 expected_g1b_basis_id: str,
                 expected_g1b_links_sha256: str):
        self.c2 = authenticate_c2_report(
            c2_report_path,
            expected_sha256=expected_c2_report_sha256,
            expected_dataset_digest=expected_dataset_digest)
        self.g1b = authenticate_g1b(
            g1b_rows_path, g1b_report_path, g1b_links_path,
            expected_rows_digest=expected_g1b_digest,
            expected_report_sha256=expected_g1b_report_sha256,
            expected_basis_id=expected_g1b_basis_id,
            expected_links_sha256=expected_g1b_links_sha256)
        self.tape = verify_tape(
            tape_dir, expected_dataset_digest=expected_dataset_digest)


_DEFAULTS = {
    "c2_report_path": repo_file(
        "evidence", "g1-c2", "G1-C2_asof_reconstruction.yaml"),
    "g1b_rows_path": repo_file(
        "evidence", "g1-b1", "G1-B1_disposition_rows.jsonl"),
    "g1b_report_path": repo_file(
        "evidence", "g1-b1", "G1-B1_observed_universe_disposition.yaml"),
    "g1b_links_path": repo_file(
        "evidence", "g1-b1", "G1-B1_reference_links.jsonl"),
}


def _ctx(tape_dir: Path, c2_report_path: Path | None,
         g1b_rows_path: Path | None, g1b_report_path: Path | None,
         g1b_links_path: Path | None, **expected) -> _ExplainContext:
    kw = {
        "expected_c2_report_sha256": EXPECTED_C2_REPORT_SHA256,
        "expected_dataset_digest": EXPECTED_DATASET_SEMANTIC_DIGEST,
        "expected_g1b_digest": EXPECTED_G1B_ROWS_DIGEST,
        "expected_g1b_report_sha256": EXPECTED_G1B_REPORT_SHA256,
        "expected_g1b_basis_id": EXPECTED_G1B_BASIS_ID,
        "expected_g1b_links_sha256": EXPECTED_G1B_LINKS_SHA256,
    }
    kw.update(expected)
    return _ExplainContext(
        tape_dir,
        c2_report_path or _DEFAULTS["c2_report_path"],
        g1b_rows_path or _DEFAULTS["g1b_rows_path"],
        g1b_report_path or _DEFAULTS["g1b_report_path"],
        g1b_links_path or _DEFAULTS["g1b_links_path"],
        **kw)


def explain_publication(
    tape_dir: Path | str,
    pub_id: str,
    at: Any = None,
    *,
    c2_report_path: Path | None = None,
    g1b_rows_path: Path | None = None,
    g1b_report_path: Path | None = None,
    g1b_links_path: Path | None = None,
    **expected: Any,
) -> dict[str, Any]:
    """EVIDENCE_CAPSULE for one persisted publication occurrence.

    ``pub_id`` is the stable occurrence identity
    (:func:`publication_id`). Unknown or duplicated ids fail closed. If
    ``at`` is given, the occurrence is resolved inside the verified
    ``known_at <= T`` reconstruction — future capture evidence can never
    enter the capsule, and lifecycle fields are the subset-local
    reconstructed values (``entry_id_scope = reconstructed_as_of``).
    """
    tape_dir = Path(tape_dir)
    ctx = _ctx(tape_dir, c2_report_path, g1b_rows_path, g1b_report_path,
               g1b_links_path, **expected)

    if at is not None:
        return _explain_publication_as_of(ctx, tape_dir, pub_id,
                                          _at(at))

    pubs = ctx.tape["publications"]
    matches = [r for r in pubs if row_publication_id(r) == pub_id]
    if not matches:
        raise G1C3Error(f"unknown publication_id {pub_id!r}")
    if len(matches) > 1:
        raise G1C3Error(
            f"publication_id {pub_id!r} is not unique in the tape")
    row = matches[0]

    chain_id = _chain_id_of(row)
    chain_row = next(
        (c for c in ctx.tape["chains"] if c["chain_id"] == chain_id),
        None)
    if chain_row is None:
        raise G1C3Error(
            f"publication's chain {chain_id!r} absent from materialized "
            "trade_chains — tape inconsistency")

    return _assemble_capsule(
        {"type": "publication", "publication_id": pub_id},
        "current", None, row,
        members=[_member(row["ledger_entry_id"], row["lifecycle_state"],
                         row["supersedes_entry_id"],
                         row["ledger_markers"], row,
                         row["publication_utc"])],
        entry_id_scope="materialized",
        chain_id=chain_id,
        head_entry_id=chain_row["head_entry_id"],
        head_state=chain_row["head_state"],
        event_count=chain_row["event_count"],
        g1b=ctx.g1b, c2=ctx.c2)


def _explain_publication_as_of(ctx: _ExplainContext, tape_dir: Path,
                               pub_id: str, t_canon: str
                               ) -> dict[str, Any]:
    _pubs, _indexed, row_by_entry, chains = _replay_at(tape_dir, t_canon)
    target = None
    for e_id, row in row_by_entry.items():
        if row_publication_id(row) == pub_id:
            if target is not None:
                raise G1C3Error(
                    f"publication_id {pub_id!r} maps to multiple "
                    "reconstructed entries")
            target = (e_id, row)
    if target is None:
        raise G1C3Error(
            f"publication {pub_id!r} not knowable at {t_canon} — "
            "refusing to cite evidence with known_at > T")
    entry_id, row = target
    entry = next(e for _p, e in _indexed if e.entry_id == entry_id)
    chain_id = next(cid for cid, chain in chains.items()
                    if any(e.entry_id == entry_id for e in chain))
    chain = chains[chain_id]
    head = chain[-1]
    return _assemble_capsule(
        {"type": "publication", "publication_id": pub_id},
        "as_of", t_canon, row,
        members=[_member(entry.entry_id, entry.state,
                         entry.supersedes_entry_id, entry.markers, row,
                         entry.publication_utc)],
        entry_id_scope="reconstructed_as_of",
        chain_id=chain_id,
        head_entry_id=head.entry_id,
        head_state=head.state,
        event_count=len(chain),
        g1b=ctx.g1b, c2=ctx.c2)


def explain_chain(
    tape_dir: Path | str,
    chain_id: str,
    at: Any = None,
    *,
    c2_report_path: Path | None = None,
    g1b_rows_path: Path | None = None,
    g1b_report_path: Path | None = None,
    g1b_links_path: Path | None = None,
    **expected: Any,
) -> dict[str, Any]:
    """EVIDENCE_CAPSULE for one chain head — current or ``as_of(T)``.

    Current: the materialized G0-D ``chain[-1]`` with ordered members and
    supersession links from the persisted tape (self-consistency
    re-verified). Historical: the chain is REBUILT through the verified
    G1-C2 path — replay of the ``known_at <= T`` subset; entry ids are
    subset-local (``entry_id_scope = reconstructed_as_of``) and no
    ``known_at > T`` evidence may appear. For ``UNCHAINABLE`` chains the
    id embeds the context-local entry id — use the id valid in the
    requested temporal context.
    """
    tape_dir = Path(tape_dir)
    ctx = _ctx(tape_dir, c2_report_path, g1b_rows_path, g1b_report_path,
               g1b_links_path, **expected)

    if at is None:
        return _explain_chain_current(ctx, chain_id)
    return _explain_chain_as_of(ctx, tape_dir, chain_id, _at(at))


def _at(at: Any) -> str:
    try:
        return _parse_at(at)
    except G1C2Error as exc:
        raise G1C3Error(str(exc)) from exc


def _explain_chain_current(ctx: _ExplainContext,
                           chain_id: str) -> dict[str, Any]:
    rows = [c for c in ctx.tape["chains"] if c["chain_id"] == chain_id]
    if not rows:
        raise G1C3Error(f"unknown chain_id {chain_id!r}")
    if len(rows) > 1:
        raise G1C3Error(f"duplicate chain_id {chain_id!r}")
    chain = rows[0]
    pubs = ctx.tape["publications"]

    if "|UNCHAINABLE|" in chain_id:
        members = [p for p in pubs
                   if p["ledger_entry_id"] == chain["head_entry_id"]]
        if len(members) != 1 or chain["event_count"] != 1 \
                or MARKER_UNCHAINABLE not in (members[0]
                                              ["ledger_markers"] or []):
            raise G1C3Error(
                f"unchainable chain {chain_id!r} is not a consistent "
                "singleton")
    else:
        # Members are exactly the occurrences under the G0-D grouping
        # key (source, raw transaction id) — reconstruct_chains grammar.
        members = [p for p in pubs
                   if p["source"] == chain["source"]
                   and p["transaction_id"] == chain["transaction_id"]]
    members.sort(key=lambda r: (r["publication_utc"] or "",
                                r["ledger_entry_id"]))
    if len(members) != chain["event_count"]:
        raise G1C3Error(
            f"chain {chain_id!r}: {len(members)} member occurrences != "
            f"event_count {chain['event_count']} — tape inconsistency")
    # Supersession self-consistency: G0-D links members sequentially.
    for i, m in enumerate(members):
        expected_sup = None if i == 0 else members[i - 1]["ledger_entry_id"]
        if m["supersedes_entry_id"] != expected_sup:
            raise G1C3Error(
                f"chain {chain_id!r}: member {m['ledger_entry_id']!r} "
                f"supersedes {m['supersedes_entry_id']!r} != expected "
                f"{expected_sup!r} — stored lifecycle inconsistent")
    if members[-1]["ledger_entry_id"] != chain["head_entry_id"]:
        raise G1C3Error(
            f"chain {chain_id!r}: ordered last member is not the "
            "materialized head")

    head_row = members[-1]
    return _assemble_capsule(
        {"type": "chain", "chain_id": chain_id},
        "current", None, head_row,
        members=[_member(m["ledger_entry_id"], m["lifecycle_state"],
                         m["supersedes_entry_id"], m["ledger_markers"],
                         m, m["publication_utc"]) for m in members],
        entry_id_scope="materialized",
        chain_id=chain_id,
        head_entry_id=chain["head_entry_id"],
        head_state=chain["head_state"],
        event_count=chain["event_count"],
        g1b=ctx.g1b, c2=ctx.c2)


# Columns consumed by a historical capsule: the G1-C2 replay substrate
# plus occurrence/economic presentation fields. The materialized
# lifecycle columns (ledger_entry_id, supersedes_entry_id,
# lifecycle_state, ledger_markers) are deliberately NOT selected — an
# as_of capsule cannot physically cite them.
_ASOF_PRESENTATION = tuple(c for c in PUB_COLUMNS if c not in {
    "ledger_entry_id", "supersedes_entry_id", "lifecycle_state",
    "ledger_markers"})


def _replay_at(tape_dir: Path, t_canon: str):
    """The verified G1-C2 reconstruction path over ``known_at <= T``.

    Same eligibility query as ``metrics.g1c2`` (NULL known_at aborts,
    canonical tape order) extended with the presentation columns needed
    by the capsule. Lifecycle authority is ALWAYS rebuilt via G0-D."""
    con = _connect_publications(tape_dir)
    try:
        null_known_at = con.execute(
            "SELECT count(*) FROM publications WHERE known_at IS NULL"
        ).fetchone()[0]
        if null_known_at:
            raise G1C3Error(
                f"{null_known_at} publications without known_at — "
                "refusing to reconstruct over unscopeable evidence")
        colspec = ", ".join(f'"{c}"' for c in _ASOF_PRESENTATION)
        rows = con.execute(
            f"SELECT {colspec} FROM publications "  # noqa: S608 - fixed cols
            "WHERE known_at <= ? "
            "ORDER BY source, source_object_id, capture_version, "
            "capture_record_ordinal",
            [t_canon]).fetchall()
    finally:
        con.close()
    pubs = [dict(zip(_ASOF_PRESENTATION, r, strict=True)) for r in rows]
    records: list[Any] = []
    for r in pubs:
        try:
            records.append(record_from_evidence(
                json.loads(r["normalized_evidence_json"])))
        except (json.JSONDecodeError, ReplayError) as exc:
            raise G1C3Error(f"replay substrate failure: {exc}") from exc
    indexed = build_ledger_indexed(records)
    row_by_entry: dict[str, dict[str, Any]] = {}
    for pos, entry in indexed:
        row_by_entry[entry.entry_id] = pubs[pos]
    if len(row_by_entry) != len(indexed):
        raise G1C3Error("reconstructed ledger has duplicate entry ids")
    chains = reconstruct_chains([e for _, e in indexed])
    return pubs, indexed, row_by_entry, chains


def _explain_chain_as_of(ctx: _ExplainContext, tape_dir: Path,
                         chain_id: str, t_canon: str) -> dict[str, Any]:
    _pubs, _indexed, row_by_entry, chains = _replay_at(tape_dir, t_canon)

    if chain_id not in chains:
        raise G1C3Error(
            f"chain {chain_id!r} not knowable at {t_canon} — absent "
            "from the T-limited reconstruction (unknown chain or "
            "entirely posterior evidence)")
    chain = chains[chain_id]

    members: list[dict[str, Any]] = []
    for e in chain:
        row = row_by_entry.get(e.entry_id)
        if row is None:
            raise G1C3Error(
                f"chain member {e.entry_id!r} does not resolve to a "
                "persisted publication occurrence — bijection broken")
        if row["known_at"] is None or row["known_at"] > t_canon:
            raise G1C3Error(
                f"chain member {e.entry_id!r} carries known_at "
                f"{row['known_at']!r} > {t_canon} — future evidence "
                "leakage refused")
        members.append(_member(e.entry_id, e.state,
                               e.supersedes_entry_id, e.markers, row,
                               e.publication_utc))
    head = chain[-1]
    head_row = row_by_entry[head.entry_id]
    return _assemble_capsule(
        {"type": "chain", "chain_id": chain_id},
        "as_of", t_canon, head_row,
        members=members,
        entry_id_scope="reconstructed_as_of",
        chain_id=chain_id,
        head_entry_id=head.entry_id,
        head_state=head.state,
        event_count=len(chain),
        g1b=ctx.g1b, c2=ctx.c2)


def _chain_id_of(row: dict[str, Any]) -> str:
    tid = (row["transaction_id"] or "").strip()
    if tid:
        return f"{row['source']}|{tid}"
    return f"{row['source']}|UNCHAINABLE|{row['ledger_entry_id']}"


# -------------------------------------------------------------- report --

def _capsule_digest_entry(capsule: dict[str, Any]) -> dict[str, Any]:
    return {
        "capsule_sha256": capsule["capsule_sha256"],
        "bytes_sha256": _sha(capsule_bytes(capsule)),
        "subject": capsule["subject"],
        "temporal": capsule["temporal"],
    }


def build_report(
    tape_dir: Path,
    *,
    c2_report_path: Path,
    g1b_rows_path: Path,
    g1b_report_path: Path,
    g1b_links_path: Path,
    asof_timestamps: list[str],
    representatives: dict[str, Any],
    date: str,
    generated_utc: str,
    expected_c2_report_sha256: str = EXPECTED_C2_REPORT_SHA256,
    expected_dataset_digest: str = EXPECTED_DATASET_SEMANTIC_DIGEST,
    expected_g1b_digest: str = EXPECTED_G1B_ROWS_DIGEST,
    expected_g1b_report_sha256: str = EXPECTED_G1B_REPORT_SHA256,
    expected_g1b_basis_id: str = EXPECTED_G1B_BASIS_ID,
    expected_g1b_links_sha256: str = EXPECTED_G1B_LINKS_SHA256,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """G1-C3 evidence report: representative capsules + determinism proof.

    ``representatives`` = {"publication_ids": [...], "chain_ids": [...],
    "asof_chain_ids": [...]} chosen deterministically by the caller. The
    report records capsule ids + sha256 only (metadata), plus a re-run
    byte-equality proof.
    """
    kw = {
        "expected_c2_report_sha256": expected_c2_report_sha256,
        "expected_dataset_digest": expected_dataset_digest,
        "expected_g1b_digest": expected_g1b_digest,
        "expected_g1b_report_sha256": expected_g1b_report_sha256,
        "expected_g1b_basis_id": expected_g1b_basis_id,
        "expected_g1b_links_sha256": expected_g1b_links_sha256,
    }
    paths = {
        "c2_report_path": c2_report_path,
        "g1b_rows_path": g1b_rows_path,
        "g1b_report_path": g1b_report_path,
        "g1b_links_path": g1b_links_path,
    }

    capsules: list[dict[str, Any]] = []
    for pid in representatives.get("publication_ids", []):
        capsules.append(explain_publication(tape_dir, pid, **paths, **kw))
    for cid in representatives.get("chain_ids", []):
        capsules.append(explain_chain(tape_dir, cid, **paths, **kw))
    for t in asof_timestamps:
        for cid in representatives.get("asof_chain_ids",
                                       representatives.get(
                                           "chain_ids", [])):
            capsules.append(
                explain_chain(tape_dir, cid, at=t, **paths, **kw))

    # Determinism proof: identical requests emit identical bytes.
    proof = explain_publication(
        tape_dir, representatives["publication_ids"][0], **paths, **kw)
    determinism = {
        "repeated_request_byte_identical":
            capsule_bytes(proof) == capsule_bytes(capsules[0]),
        "canonical_form":
            "JSON UTF-8, sort_keys, compact separators, trailing LF; "
            "no filesystem paths, no generation timestamp, no "
            "randomness in capsule identity",
    }

    report = {
        "gate": "G1",
        "issue_id": "G1-C3",
        "report": "G1-C3_EVIDENCE_CAPSULE",
        "date": date,
        "generated_utc": generated_utc,
        "frozen_authority": {
            "spec": "docs/gates/G1.md §4.3",
            "freeze_tag": "g1-freeze-v0",
            "freeze_commit": "2a5e4684b9a3f6066e6d2a53cbc37b920e2c9ffa",
        },
        "generator": {
            "module": "metrics.g1c3",
            "code_commit": _git_head(),
            "deterministic": True,
        },
        "capsule_schema_version": CAPSULE_SCHEMA_VERSION,
        "publication_identity":
            "sha256(canonical_json({source, source_object_id, "
            "capture_version, capture_record_ordinal})) — immutable "
            "occurrence identity only",
        "authority": {
            "c2_report": C2_REPORT_LOGICAL_ID,
            "c2_report_sha256": expected_c2_report_sha256,
            "tape_dataset_semantic_digest": expected_dataset_digest,
            "tape_schema_version": TAPE_SCHEMA_VERSION,
            "g1b_report": G1B_REPORT_LOGICAL_ID,
            "g1b_report_sha256": expected_g1b_report_sha256,
            "g1b_rows": G1B_ROWS_LOGICAL_ID,
            "g1b_rows_sha256": expected_g1b_digest,
            "g1b_reference_links": G1B_LINKS_LOGICAL_ID,
            "g1b_reference_links_sha256": expected_g1b_links_sha256,
            "reference_basis_id": expected_g1b_basis_id,
        },
        "representative_capsules":
            [_capsule_digest_entry(c) for c in capsules],
        "determinism": determinism,
        "no_future_evidence": True,
        "no_provider_payload": True,
        "hygiene": {
            "provider_payloads_in_repo": False,
            "capsule_corpus_committed": False,
            "note": "representative capsule digests only; the ~52k "
                    "corpus is regenerable deterministically",
        },
    }
    return report, capsules


def generate_report(
    tape_dir: Path,
    *,
    c2_report_path: Path,
    g1b_rows_path: Path,
    g1b_report_path: Path,
    g1b_links_path: Path,
    asof_timestamps: list[str],
    representatives: dict[str, Any],
    evidence_dir: Path,
    date: str,
    generated_utc: str,
    **expected: Any,
) -> Path:
    report, capsules = build_report(
        tape_dir, c2_report_path=c2_report_path,
        g1b_rows_path=g1b_rows_path, g1b_report_path=g1b_report_path,
        g1b_links_path=g1b_links_path,
        asof_timestamps=asof_timestamps,
        representatives=representatives, date=date,
        generated_utc=generated_utc, **expected)
    ev = evidence_dir / "g1-c3"
    ev.mkdir(parents=True, exist_ok=True)
    cap_dir = ev / "capsules"
    cap_dir.mkdir(exist_ok=True)
    capsule_files = []
    for i, c in enumerate(capsules):
        data = capsule_bytes(c)
        # Filenames are digest-keyed: chain ids contain '|' (illegal on
        # Windows) — the digest name is portable and collision-free.
        name = f"capsule_{i:02d}_{_sha(data)[:16]}.json"
        (cap_dir / name).write_bytes(data)
        capsule_files.append({"file": f"capsules/{name}",
                              "sha256": _sha(data)})
    report["representative_capsule_files"] = capsule_files
    path = ev / REPORT_NAME
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(report, fh, sort_keys=False)
    sha = _sha(path.read_bytes())
    (ev / f"{path.stem}.sha256.txt").write_text(
        f"{sha} *{path.name}\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------- CLI --

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tape-dir", type=Path,
                    default=Path("data/g1/tape"))
    ap.add_argument("--c2-report", type=Path,
                    default=_DEFAULTS["c2_report_path"])
    ap.add_argument("--g1b-rows", type=Path,
                    default=_DEFAULTS["g1b_rows_path"])
    ap.add_argument("--g1b-report", type=Path,
                    default=_DEFAULTS["g1b_report_path"])
    ap.add_argument("--g1b-links", type=Path,
                    default=_DEFAULTS["g1b_links_path"])
    ap.add_argument("--row", help="stable publication id to explain")
    ap.add_argument("--chain", help="chain id to explain")
    ap.add_argument("--at", default=None,
                    help="optional offset-aware T for historical explain")
    # Evidence generation (operator run).
    ap.add_argument("--out", type=Path, default=None,
                    help="generate the C3 evidence report under this "
                         "evidence dir")
    ap.add_argument("--date")
    ap.add_argument("--generated-utc")
    ap.add_argument("--asof-timestamps", default="[]")
    ap.add_argument("--representatives", default=None,
                    help="JSON {publication_ids, chain_ids, "
                         "asof_chain_ids?}")
    args = ap.parse_args(argv)

    kw = {"c2_report_path": args.c2_report,
          "g1b_rows_path": args.g1b_rows,
          "g1b_report_path": args.g1b_report,
          "g1b_links_path": args.g1b_links}

    if args.out is not None:
        for name in ("date", "generated_utc", "representatives"):
            if getattr(args, name) is None:
                ap.error(f"--{name.replace('_', '-')} is required for "
                         "evidence generation")
        path = generate_report(
            args.tape_dir, asof_timestamps=json.loads(args.asof_timestamps),
            representatives=json.loads(args.representatives),
            evidence_dir=args.out, date=args.date,
            generated_utc=args.generated_utc, **kw)
        print(path)
        return 0

    if args.row is None and args.chain is None:
        ap.error("one of --row/--chain is required (or --out for the "
                 "evidence report)")
    if args.row is not None and args.chain is not None:
        ap.error("--row and --chain are mutually exclusive")
    if args.row is not None:
        capsule = explain_publication(args.tape_dir, args.row,
                                      at=args.at, **kw)
    else:
        capsule = explain_chain(args.tape_dir, args.chain, at=args.at,
                                **kw)
    sys.stdout.buffer.write(capsule_bytes(capsule))
    return 0


if __name__ == "__main__":
    sys.exit(main())
