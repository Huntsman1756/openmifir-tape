"""G1-C1 — deterministic audit-tape materialization core (frozen G1 §4.1/§4.2).

Materializes the first physical tape:

    data/g1/tape/publications.parquet
    data/g1/tape/trade_chains.parquet

over EXACTLY the verified eligible A3 corpus (``metrics.selection``
select_eligible — the hardened #17 selector, not a parallel one), G0-C
normalization, the authoritative G0-D ledger/chains, and the verified
G1-B disposition join by exact ISIN.

Authority wiring — nothing is reimplemented here:

- temporal contract (§4.2): ``known_at = CaptureVersionRef.first_observed_at``
  exactly; ``last_observed_at``/``observation_count`` ride as diagnostics;
  publication/trading times stay distinct — never inferred from each other;
- lifecycle: ``lifecycle.ledger.build_ledger_indexed`` (the position->
  LedgerEntry map is EXPLICIT — identical observations get their exact
  deterministic occurrence ids; no post-hoc economic-field matching) and
  ``lifecycle.lifecycle.reconstruct_chains``;
- universe: the verified G1-B disposition rows (digest-verified) join by
  exact ISIN — ``resolve_disposition`` is NOT rerun.

Publication unit: one normalized record occurrence inside one exact
eligible capture identity. No heuristic dedup — the same normalized
content observed under two distinct capture identities yields two rows,
and the ledger's own occurrence indexing decides their entry ids.

Semantic identity is INDEPENDENT of Parquet physical bytes: canonical
JSONL per table (fixed column order, canonical row order, JSON null for
absent values, sorted list fields) -> sha256. ``dataset_semantic_digest``
binds schema version + both table digests/counts.

This issue does NOT implement as_of(T), EVIDENCE_CAPSULE, TAPE_MANIFEST or
omt verify (G1-C2/C3/C4).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from lifecycle.ledger import build_ledger_indexed
from lifecycle.lifecycle import reconstruct_chains

from .evidence import _git_head, _load_version, _parse_ts
from .g1a import digest_entries
from .g1b import EXPECTED_G1A_SEMANTIC_DIGEST
from .selection import CaptureVersionRef, select_eligible

# Schema v2 (G1-C2): adds ``normalized_evidence_json`` — the canonical
# JSON of ``NormalizedRecord.to_evidence()`` per publication occurrence —
# so the persisted tape alone is sufficient for exact G0-D replay
# (as_of(T)) without provider raw bytes. All v1 columns and semantics are
# preserved; C1 v1 digests remain the historical authority.
TAPE_SCHEMA_VERSION = 2

# Verified upstream authority (issues #20/#22) — recomputed, never
# trusted by declaration.
EXPECTED_G1B_ROWS_DIGEST = \
    "1f50657ca9063e351fe05e9b653cc691431214dfd38dccc3260cf6a89d6c81d8"
EXPECTED_G1B_REPORT_SHA256 = \
    "3d75da51a3b30b4f0e1139afc8891c3983bc28bdbb134cd49d8690c348633c8f"
EXPECTED_G1B_BASIS_ID = \
    "9374a21cea087416378c7685d89501b3d49fa7af2728375422dce9fb6287beb8"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

PUB_COLUMNS = [
    "source", "source_object_id", "capture_version", "raw_sha256",
    "capture_record_ordinal",
    "known_at", "last_observed_at", "observation_count",
    "publication_utc", "trading_utc",
    "isin", "transaction_id", "price", "quantity", "notional",
    "currency", "price_currency", "price_notation", "flags",
    "source_report_id",
    "lifecycle_state", "ledger_entry_id", "supersedes_entry_id",
    "ledger_markers",
    "universe_disposition", "universe_disposition_reason", "issuer_lei",
    "normalized_evidence_json",
]

CHAIN_COLUMNS = [
    "source", "chain_id", "isin", "transaction_id",
    "head_entry_id", "head_state", "event_count",
    "first_publication_utc", "latest_publication_utc",
    "first_known_at", "head_known_at",
]

# DuckDB physical types for the canonical row values.
PUB_DUCK_TYPES = {
    "observation_count": "BIGINT",
    "capture_record_ordinal": "BIGINT",
    "flags": "VARCHAR[]",
    "ledger_markers": "VARCHAR[]",
}
CHAIN_DUCK_TYPES = {"event_count": "BIGINT"}

REPORT_NAME = "G1-C1_tape_materialization.yaml"
PUB_PARQUET = "publications.parquet"
CHAIN_PARQUET = "trade_chains.parquet"


class G1C1Error(Exception):
    """Fail-closed abort for G1-C1 (authority mismatch, broken bijection,
    missing inputs)."""


def _canon_ts(dt: datetime | None) -> str | None:
    """Canonical UTC timestamp (microseconds + Z), None preserved."""
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def load_g1b_dispositions(
    rows_path: Path,
    *,
    expected_digest: str = EXPECTED_G1B_ROWS_DIGEST,
) -> dict[str, dict[str, str | None]]:
    """Load the verified G1-B disposition rows — sha256-verified.

    Returns ``{isin: {"branch": ..., "reason": ..., "lei": ...}}``.
    Fail closed on digest mismatch, duplicate ISIN, or malformed
    branch/reason authority.
    """
    from universe.model import Branch, Reason

    if not rows_path.is_file():
        raise G1C1Error(f"G1-B disposition rows not found: {rows_path}")
    data = rows_path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_digest:
        raise G1C1Error(
            f"G1-B disposition rows digest {actual!r} != expected "
            f"{expected_digest!r} — refusing unverified universe authority")
    by_isin: dict[str, dict[str, str | None]] = {}
    for line in data.decode("utf-8").splitlines():
        row = json.loads(line)
        isin = row["isin"]
        branch, reason = row["branch"], row["reason"]
        if branch not in {b.value for b in Branch} \
                or reason not in {r.value for r in Reason}:
            raise G1C1Error(
                f"{isin}: malformed disposition authority "
                f"{branch!r}/{reason!r}")
        if isin in by_isin:
            raise G1C1Error(f"duplicate G1-B disposition row for {isin}")
        firds = row.get("firds") or {}
        by_isin[isin] = {"branch": branch, "reason": reason,
                         "lei": firds.get("lei")}
    return by_isin


def verify_g1b_authority(
    rows_path: Path,
    report_path: Path,
    *,
    expected_rows_digest: str = EXPECTED_G1B_ROWS_DIGEST,
    expected_report_sha256: str = EXPECTED_G1B_REPORT_SHA256,
    expected_basis_id: str = EXPECTED_G1B_BASIS_ID,
) -> dict[str, Any]:
    """Fully verify G1-B authority: disposition rows AND reference basis.

    - report file MUST exist and its bytes MUST hash to the verified
      ``expected_report_sha256`` — metadata from an unauthenticated
      report is never trusted;
    - the authenticated report's ``dispositions.rows_sha256`` MUST equal
      the independently computed sha256 of the supplied rows file AND
      ``expected_rows_digest``;
    - ``reference_basis_id`` is RECOMPUTED from the three family
      ``set_digest`` values via ``metrics.g1b.reference_basis_id`` and
      must equal the embedded value AND ``expected_basis_id``.

    Returns ``{"rows_digest", "report_sha256", "reference_basis_id"}``.
    Any deviation raises ``G1C1Error`` — no fallbacks.
    """
    from .g1b import reference_basis_id as _g1b_basis_id

    dispositions = load_g1b_dispositions(
        rows_path, expected_digest=expected_rows_digest)

    if not report_path.is_file():
        raise G1C1Error(
            f"G1-B report not found: {report_path} — reference authority "
            "is mandatory, no fallback")
    report_bytes = report_path.read_bytes()
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    if report_sha != expected_report_sha256:
        raise G1C1Error(
            f"G1-B report sha256 {report_sha!r} != expected "
            f"{expected_report_sha256!r} — refusing unverified report")
    doc = yaml.safe_load(report_bytes.decode("utf-8")) or {}

    declared_rows_sha = ((doc.get("dispositions") or {})
                         .get("rows_sha256"))
    actual_rows_sha = hashlib.sha256(rows_path.read_bytes()).hexdigest()
    if declared_rows_sha != actual_rows_sha \
            or actual_rows_sha != expected_rows_digest:
        raise G1C1Error(
            "G1-B report does not bind the supplied disposition rows: "
            f"declared={declared_rows_sha!r} actual={actual_rows_sha!r} "
            f"expected={expected_rows_digest!r}")

    families = doc.get("reference_sets") or {}
    set_digests: dict[str, str] = {}
    for fam in ("fitrs", "firds", "gleif"):
        sd = (families.get(fam) or {}).get("set_digest")
        if not isinstance(sd, str) or not _HEX64_RE.match(sd):
            raise G1C1Error(
                f"G1-B report has malformed/missing {fam} set_digest: "
                f"{sd!r}")
        set_digests[fam] = sd
    computed = _g1b_basis_id(set_digests)
    embedded = doc.get("reference_basis_id")
    if computed != embedded or computed != expected_basis_id:
        raise G1C1Error(
            "G1-B reference_basis_id verification failed: "
            f"computed={computed!r} embedded={embedded!r} "
            f"expected={expected_basis_id!r}")
    return {"dispositions": dispositions,
            "rows_digest": actual_rows_sha,
            "report_sha256": report_sha,
            "reference_basis_id": computed}


def verify_g1a_authority(
    artifact_path: Path,
    publication_isins: set[str] | None = None,
    *,
    expected_digest: str = EXPECTED_G1A_SEMANTIC_DIGEST,
) -> set[str]:
    """Recompute the canonical G1-A digest over instrument entries and
    require ``recomputed == embedded == expected``. When
    ``publication_isins`` is given, the materialized ISIN surface MUST
    equal the observed-instrument set exactly — any drift fails closed."""
    if not artifact_path.is_file():
        raise G1C1Error(f"G1-A artifact not found: {artifact_path}")
    doc = yaml.safe_load(artifact_path.read_bytes().decode("utf-8"))
    observed = (doc or {}).get("observed_instrument_set") or {}
    instruments = observed.get("instruments") or []
    embedded = observed.get("semantic_digest")
    recomputed = digest_entries(instruments)
    if embedded != recomputed or recomputed != expected_digest:
        raise G1C1Error(
            "G1-A semantic digest verification failed: "
            f"embedded={embedded!r} recomputed={recomputed!r} "
            f"expected={expected_digest!r}")
    observed_isins = {e["isin"] for e in instruments}
    if publication_isins is not None and \
            publication_isins != observed_isins:
        raise G1C1Error(
            "materialized publication ISIN surface != verified G1-A "
            "observed instrument set "
            f"(only_in_tape={sorted(publication_isins - observed_isins)[:5]}"
            f" only_in_g1a={sorted(observed_isins - publication_isins)[:5]})")
    return observed_isins


def _pub_row(
    version: CaptureVersionRef,
    ordinal: int,
    rec: Any,
    entry,
    disposition: dict[str, str | None],
) -> dict[str, Any]:
    """One canonical publication row (fixed PUB_COLUMNS order)."""
    isin = (rec.instrument_identification_code or "").strip()
    if not isin:
        raise G1C1Error(
            "parseable publication without instrument identifier "
            f"({version.source_id}/{version.source_object_id}/"
            f"{version.capture_version}#{ordinal}) — fail closed")
    if isin not in disposition:
        raise G1C1Error(
            f"{isin}: no verified G1-B disposition row — fail closed")
    disp = disposition[isin]
    return {
        "source": version.source_id,
        "source_object_id": version.source_object_id,
        "capture_version": version.capture_version,
        "raw_sha256": version.capture_version,  # capture_version IS sha256
        "capture_record_ordinal": ordinal,
        "known_at": _canon_ts(version.first_observed_at),
        "last_observed_at": _canon_ts(version.last_observed_at),
        "observation_count": version.observation_count,
        "publication_utc": rec.publication_datetime.canonical_utc,
        "trading_utc": rec.trading_datetime.canonical_utc,
        "isin": isin,
        "transaction_id": rec.transaction_identification_code,
        "price": rec.price,
        "quantity": rec.quantity,
        "notional": rec.notional_amount,
        # Frozen: currency = normalized notional_currency; never price.
        "currency": rec.notional_currency,
        "price_currency": rec.price_currency,
        "price_notation": rec.price_notation,
        "flags": sorted(rec.flags),
        "source_report_id": rec.source_report_id,
        "lifecycle_state": entry.state,
        "ledger_entry_id": entry.entry_id,
        "supersedes_entry_id": entry.supersedes_entry_id,
        "ledger_markers": sorted(entry.markers),
        "universe_disposition": disp["branch"],
        "universe_disposition_reason": disp["reason"],
        "issuer_lei": disp["lei"],
        # Replay substrate (schema v2): canonical metadata-only
        # to_evidence() — exactly what G0-D consumes; no raw fields.
        "normalized_evidence_json": json.dumps(
            rec.to_evidence(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False),
    }


def build_publications(
    selection: dict[str, Any],
    dispositions: dict[str, dict[str, str | None]],
) -> tuple[list[dict[str, Any]], list[tuple[int, Any]],
           list[dict[str, str]]]:
    """Materialize publication rows + the authoritative indexed ledger.

    Returns ``(pub_rows, indexed_ledger, unparseable_diagnostics)`` where
    ``indexed_ledger[i] = (input_position, LedgerEntry)`` and input
    position i corresponds to the i-th publication row's source record —
    the mapping is EXPLICIT, never reconstructed by economic fields.
    """
    normalized: list[Any] = []
    span: list[tuple[CaptureVersionRef, int]] = []   # (version, ordinal)
    unparseable: list[dict[str, str]] = []
    for v in selection["versions"]:
        recs, err = _load_version(v)
        if err is not None:
            unparseable.append({
                "source_id": v.source_id,
                "source_object_id": v.source_object_id,
                "capture_version": v.capture_version,
                "reason": err,
            })
            continue
        for ordinal, rec in enumerate(recs):
            normalized.append(rec)
            span.append((v, ordinal))

    indexed = build_ledger_indexed(normalized)
    if len(indexed) != len(normalized):
        raise G1C1Error("indexed ledger cardinality != normalized input")
    entry_by_pos = dict(indexed)
    if len(entry_by_pos) != len(indexed):
        raise G1C1Error("non-bijective input_position -> LedgerEntry map")
    entry_ids = [e.entry_id for _, e in indexed]
    if len(set(entry_ids)) != len(entry_ids):
        raise G1C1Error("duplicate ledger_entry_id materialized")

    rows: list[dict[str, Any]] = []
    for pos, (v, ordinal) in enumerate(span):
        rows.append(_pub_row(v, ordinal, normalized[pos],
                             entry_by_pos[pos], dispositions))
    return rows, indexed, unparseable


def build_chains(
    indexed: list[tuple[int, Any]],
    pub_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Materialize current/FULL-materialization chain heads via
    ``reconstruct_chains`` — NOT an as-of(T) view (G1-C2)."""
    entries = [e for _, e in indexed]
    chains = reconstruct_chains(entries)
    known_at_by_entry = {r["ledger_entry_id"]: r["known_at"]
                         for r in pub_rows}
    if len(known_at_by_entry) != len(entries):
        raise G1C1Error("non-bijective ledger_entry -> publication map")

    rows: list[dict[str, Any]] = []
    chained = 0
    for chain_id, chain in chains.items():
        chained += len(chain)
        head = chain[-1]                      # authoritative head, exactly
        known_ats = [known_at_by_entry[e.entry_id] for e in chain]
        rows.append({
            "source": head.source,
            "chain_id": chain_id,
            "isin": head.instrument_identification_code,
            "transaction_id": head.transaction_identification_code,
            "head_entry_id": head.entry_id,
            "head_state": head.state,
            "event_count": len(chain),
            "first_publication_utc": chain[0].publication_utc,
            "latest_publication_utc": head.publication_utc,
            "first_known_at": min(
                (k for k in known_ats if k is not None), default=None),
            # head_known_at = knowledge time of the EXACT chain[-1]
            # entry — never max(known_at) (a rescan is not the head).
            "head_known_at": known_at_by_entry[head.entry_id],
        })
    if chained != len(entries):
        raise G1C1Error("reconstruct_chains did not cover every entry")
    return rows


def canonical_table_bytes(rows: list[dict[str, Any]],
                          columns: list[str],
                          sort_key) -> bytes:
    """Canonical JSONL: fixed column set, canonical row order, JSON null
    for absent values. The semantic-digest material."""
    ordered = sorted(rows, key=sort_key)
    out = []
    for r in ordered:
        out.append(json.dumps({c: r[c] for c in columns},
                              sort_keys=False, separators=(",", ":"),
                              ensure_ascii=False))
    return ("\n".join(out) + "\n").encode("utf-8") if out else b""


def pub_sort_key(r: dict[str, Any]) -> tuple:
    return (r["source"], r["source_object_id"], r["capture_version"],
            r["capture_record_ordinal"])


def semantic_digest(rows: list[dict[str, Any]], columns: list[str],
                    sort_key) -> str:
    return hashlib.sha256(
        canonical_table_bytes(rows, columns, sort_key)).hexdigest()


def dataset_semantic_digest(pub_digest: str, pub_rows: int,
                            chain_digest: str, chain_rows: int) -> str:
    material = {
        "tape_schema_version": TAPE_SCHEMA_VERSION,
        "publications": {"rows": pub_rows, "semantic_digest": pub_digest},
        "trade_chains": {"rows": chain_rows,
                         "semantic_digest": chain_digest},
    }
    return hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")).hexdigest()


def write_parquet(rows: list[dict[str, Any]], columns: list[str],
                  duck_types: dict[str, str], path: Path,
                  sort_key) -> None:
    """Write canonical rows to Parquet through embedded DuckDB.

    Physical bytes are non-authoritative (writer metadata varies); the
    semantic identity is the canonical JSONL digest.
    """
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        colspec = ", ".join(
            f'"{c}" {duck_types.get(c, "VARCHAR")}' for c in columns)
        # DDL is built from fixed schema literals (never user input);
        # COPY/VIEW paths are operator-local files, not query input.
        con.execute(f"CREATE TABLE t ({colspec})")
        placeholders = ", ".join("?" for _ in columns)
        ordered = sorted(rows, key=sort_key)
        if ordered:
            con.executemany(
                f"INSERT INTO t VALUES ({placeholders})",  # noqa: S608 - parameterized
                [[r[c] for c in columns] for r in ordered])
        con.execute(
            f"COPY t TO '{str(path).replace(chr(92), '/')}' "
            "(FORMAT PARQUET)")
    finally:
        con.close()


def connect_tape(tape_dir: Path):
    """Open an embedded DuckDB session exposing ``publications`` and
    ``trade_chains`` views over the materialized Parquet files."""
    import duckdb

    con = duckdb.connect()
    for name, fname in (("publications", PUB_PARQUET),
                        ("trade_chains", CHAIN_PARQUET)):
        p = str((tape_dir / fname).resolve()).replace("\\", "/")
        con.execute(
            f"CREATE VIEW {name} AS "  # noqa: S608 - fixed view name
            f"SELECT * FROM '{p}'")    # + operator-local path literal
    return con


def duckdb_smoke(tape_dir: Path) -> dict[str, Any]:
    """Deterministic smoke queries — row counts and a join sanity check."""
    con = connect_tape(tape_dir)
    try:
        pubs = con.execute("SELECT count(*) FROM publications").fetchone()[0]
        chains = con.execute(
            "SELECT count(*) FROM trade_chains").fetchone()[0]
        joined = con.execute(
            "SELECT count(*) FROM publications p JOIN trade_chains c "
            "ON p.ledger_entry_id = c.head_entry_id").fetchone()[0]
        return {"publications_rows": pubs, "trade_chains_rows": chains,
                "pubs_on_head_join_rows": joined}
    finally:
        con.close()


def build_report(
    raw_root: Path,
    runs_dir: Path,
    g1a_artifact: Path,
    g1b_rows: Path,
    g1b_report: Path,
    *,
    window: dict[str, str],
    tape_dir: Path,
    date: str,
    generated_utc: str,
    expected_g1a_digest: str = EXPECTED_G1A_SEMANTIC_DIGEST,
    expected_g1b_digest: str = EXPECTED_G1B_ROWS_DIGEST,
    expected_g1b_report_sha256: str = EXPECTED_G1B_REPORT_SHA256,
    expected_g1b_basis_id: str = EXPECTED_G1B_BASIS_ID,
) -> tuple[dict[str, Any], bytes, bytes]:
    """Materialize the tape and build the C1 metadata report.

    Returns ``(report, publications_jsonl, chains_jsonl)`` — canonical
    table bytes for determinism comparison. Writes the Parquet tables to
    ``tape_dir``.
    """
    start = _parse_ts(window["operational_start"])
    tail = _parse_ts(window["evidence_tail_end"])
    g1b = verify_g1b_authority(
        g1b_rows, g1b_report,
        expected_rows_digest=expected_g1b_digest,
        expected_report_sha256=expected_g1b_report_sha256,
        expected_basis_id=expected_g1b_basis_id)
    dispositions = g1b["dispositions"]

    selection = select_eligible(runs_dir, raw_root, start=start,
                                tail_end=tail)
    pub_rows, indexed, unparseable = build_publications(
        selection, dispositions)
    pub_isins = {r["isin"] for r in pub_rows}
    observed_isins = verify_g1a_authority(g1a_artifact, pub_isins,
                                          expected_digest=expected_g1a_digest)
    chain_rows = build_chains(indexed, pub_rows)

    pub_bytes = canonical_table_bytes(pub_rows, PUB_COLUMNS, pub_sort_key)
    chain_bytes = canonical_table_bytes(
        chain_rows, CHAIN_COLUMNS, lambda r: r["chain_id"])
    pub_digest = hashlib.sha256(pub_bytes).hexdigest()
    chain_digest = hashlib.sha256(chain_bytes).hexdigest()
    dataset = dataset_semantic_digest(
        pub_digest, len(pub_rows), chain_digest, len(chain_rows))

    write_parquet(pub_rows, PUB_COLUMNS, PUB_DUCK_TYPES,
                  tape_dir / PUB_PARQUET, pub_sort_key)
    write_parquet(chain_rows, CHAIN_COLUMNS, CHAIN_DUCK_TYPES,
                  tape_dir / CHAIN_PARQUET, lambda r: r["chain_id"])
    smoke = duckdb_smoke(tape_dir)

    states = {}
    for r in pub_rows:
        states[r["lifecycle_state"]] = states.get(
            r["lifecycle_state"], 0) + 1
    superseded = sum(1 for r in pub_rows if r["supersedes_entry_id"])
    unchainable = sum(1 for r in pub_rows
                      if "UNCHAINABLE" in r["ledger_markers"])
    collisions = sum(1 for r in pub_rows
                     if "ENTRY_ID_COLLISION" in r["ledger_markers"])

    report: dict[str, Any] = {
        "gate": "G1",
        "issue_id": "G1-C1",
        "report": "G1-C1_TAPE_MATERIALIZATION",
        "date": date,
        "generated_utc": generated_utc,
        "frozen_authority": {
            "spec": "docs/gates/G1.md §4.1/§4.2",
            "freeze_tag": "g1-freeze-v0",
            "freeze_commit": "2a5e4684b9a3f6066e6d2a53cbc37b920e2c9ffa",
        },
        "generator": {
            "module": "metrics.g1c1",
            "code_commit": _git_head(),
            "deterministic": True,
        },
        "tape_schema_version": TAPE_SCHEMA_VERSION,
        "input_authority": {
            "selector": "metrics.selection.select_eligible (#17-hardened)",
            "input_layout": "sealed_bundle/{runs,raw}",
            "capture_window_utc": window,
            "g1a": {
                "artifact":
                    "evidence/g1-a1/G1-A1_observed_instrument_set.yaml",
                "semantic_digest": expected_g1a_digest,
                "verification":
                    "canonical digest recomputed over instrument entries; "
                    "publication ISIN surface == observed set enforced",
                "observed_identifier_count": len(observed_isins),
            },
            "g1b": {
                "rows": "evidence/g1-b1/G1-B1_disposition_rows.jsonl",
                "rows_sha256": g1b["rows_digest"],
                "report":
                    "evidence/g1-b1/G1-B1_observed_universe_disposition.yaml",
                "report_sha256": g1b["report_sha256"],
                "reference_basis_id": g1b["reference_basis_id"],
                "verification":
                    "report bytes sha256-verified; report.dispositions."
                    "rows_sha256 bound to the independently hashed rows "
                    "file; reference_basis_id recomputed from the three "
                    "family set_digests (metrics.g1b.reference_basis_id) "
                    "and required to equal embedded == expected",
            },
        },
        "tables": {
            "publications": {
                "file": f"data/g1/tape/{PUB_PARQUET}",
                "columns": PUB_COLUMNS,
                "row_count": len(pub_rows),
                "semantic_digest": pub_digest,
            },
            "trade_chains": {
                "file": f"data/g1/tape/{CHAIN_PARQUET}",
                "columns": CHAIN_COLUMNS,
                "row_count": len(chain_rows),
                "semantic_digest": chain_digest,
            },
        },
        "dataset_semantic_digest": dataset,
        "lifecycle_reconciliation": {
            "ledger_entries": len(indexed),
            "chains": len(chain_rows),
            "supersession_links": superseded,
            "unchainable_singletons": unchainable,
            "entry_id_collisions": collisions,
            "head_state_counts": _head_state_counts(chain_rows),
            "publication_state_counts": dict(sorted(states.items())),
        },
        "diagnostics": {
            "input_census": selection["census"],
            "unparseable_capture_versions": unparseable,
            "publications_parseable_without_isin": 0,
            "duckdb_smoke": smoke,
        },
        "hygiene": {
            "provider_payloads_in_repo": False,
            "parquet_files_committed": False,
            "semantic_identity_basis": "canonical JSONL digests; Parquet "
                                       "physical bytes are "
                                       "non-authoritative",
        },
    }
    return report, pub_bytes, chain_bytes


def _head_state_counts(chain_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in chain_rows:
        counts[r["head_state"]] = counts.get(r["head_state"], 0) + 1
    return dict(sorted(counts.items()))


def generate_report(
    raw_root: Path,
    runs_dir: Path,
    g1a_artifact: Path,
    g1b_rows: Path,
    g1b_report: Path,
    *,
    window: dict[str, str],
    tape_dir: Path,
    evidence_dir: Path,
    date: str,
    generated_utc: str,
    expected_g1a_digest: str = EXPECTED_G1A_SEMANTIC_DIGEST,
    expected_g1b_digest: str = EXPECTED_G1B_ROWS_DIGEST,
    expected_g1b_report_sha256: str = EXPECTED_G1B_REPORT_SHA256,
    expected_g1b_basis_id: str = EXPECTED_G1B_BASIS_ID,
) -> Path:
    report, _pub, _chain = build_report(
        raw_root, runs_dir, g1a_artifact, g1b_rows, g1b_report,
        window=window, tape_dir=tape_dir,
        date=date, generated_utc=generated_utc,
        expected_g1a_digest=expected_g1a_digest,
        expected_g1b_digest=expected_g1b_digest,
        expected_g1b_report_sha256=expected_g1b_report_sha256,
        expected_g1b_basis_id=expected_g1b_basis_id)
    ev = evidence_dir / "g1-c1"
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / REPORT_NAME
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(report, fh, sort_keys=False)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    (ev / f"{path.stem}.sha256.txt").write_text(
        f"{sha} *{path.name}\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-root", type=Path, required=True)
    ap.add_argument("--runs-dir", type=Path, required=True)
    ap.add_argument("--g1a-artifact", type=Path, required=True)
    ap.add_argument("--g1b-rows", type=Path, required=True)
    ap.add_argument("--g1b-report", type=Path, required=True,
                    help="verified G1-B disposition report — mandatory, "
                         "bytes sha256-verified and basis recomputed")
    ap.add_argument("--tape-dir", type=Path,
                    default=Path("data/g1/tape"))
    ap.add_argument("--out", type=Path, default=Path("evidence"))
    ap.add_argument("--date", required=True)
    ap.add_argument("--generated-utc", required=True)
    ap.add_argument("--window", required=True,
                    help="JSON with operational_start + evidence_tail_end")
    args = ap.parse_args(argv)
    path = generate_report(
        args.raw_root, args.runs_dir, args.g1a_artifact, args.g1b_rows,
        args.g1b_report, window=json.loads(args.window),
        tape_dir=args.tape_dir, evidence_dir=args.out,
        date=args.date, generated_utc=args.generated_utc)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
