"""G1-C2 — point-in-time ``as_of(T)`` reconstruction over the durable tape.

Frozen rule (docs/gates/G1.md §4.2, tag g1-freeze-v0)::

    as_of(T) = publications WHERE known_at <= T
               -> build_ledger -> reconstruct_chains -> chain[-1]
               (G0-D ordering, entry_id tie-break included)

Hard prohibitions honored by construction:

- the materialized ``trade_chains`` table is NEVER consulted — heads are
  rebuilt through G0-D, not filtered;
- full-tape ``ledger_entry_id`` / ``supersedes_entry_id`` /
  ``lifecycle_state`` are NEVER reused as historical authority — the
  historical ledger (and therefore its ids, supersession links and
  occurrence suffixes) is rebuilt from the T-limited publication subset;
- ``publication_utc`` / ``trading_utc`` / ``last_observed_at`` are never
  a substitute for ``known_at``;
- no publication occurrence with ``known_at > T`` participates (no
  future evidence);
- universe disposition is the frozen build-snapshot classification,
  labelled ``reference_basis = BUILD_SNAPSHOT`` — not a claim that the
  reference data was known at T.

Replay substrate (tape_schema_version = 2): each publication row carries
``normalized_evidence_json``, the canonical JSON of
``NormalizedRecord.to_evidence()`` at materialization time.
``normalization.replay.record_from_evidence`` rebuilds the exact record
attributes consumed by G0-D; no parallel lifecycle/state logic exists
here, and provider raw bytes are never reread.

Determinism: the eligible subset is replayed in canonical tape order
``(source, source_object_id, capture_version, capture_record_ordinal)``
— exactly the materialization input order — so identical observations
receive deterministic occurrence ids from the T-limited subset. Same
tape + same T -> identical rows/digest; T is part of the semantic
identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from lifecycle.ledger import build_ledger_indexed
from lifecycle.lifecycle import reconstruct_chains
from normalization.replay import ReplayError, record_from_evidence

from .evidence import _git_head
from .g1b import EXPECTED_G1A_SEMANTIC_DIGEST
from .g1c1 import (
    CHAIN_COLUMNS,
    EXPECTED_G1B_BASIS_ID,
    EXPECTED_G1B_REPORT_SHA256,
    EXPECTED_G1B_ROWS_DIGEST,
    PUB_PARQUET,
    TAPE_SCHEMA_VERSION,
    _canon_ts,
    canonical_table_bytes,
)
from .g1c1 import (
    build_report as build_tape_v2,
)

REFERENCE_BASIS = "BUILD_SNAPSHOT"
REPORT_NAME = "G1-C2_asof_reconstruction.yaml"

ASOF_COLUMNS = [
    "source", "chain_id", "isin", "transaction_id",
    "head_entry_id", "head_state", "event_count",
    "first_publication_utc", "latest_publication_utc",
    "first_known_at", "head_known_at",
    "as_of_utc", "reference_basis",
    "universe_disposition", "universe_disposition_reason", "issuer_lei",
]

# Columns consumed by reconstruction. Deliberately excludes every
# materialized lifecycle field (ledger_entry_id, supersedes_entry_id,
# lifecycle_state, ledger_markers): they are full-tape authority and must
# never steer a historical rebuild.
_REPLAY_SELECT = (
    "source", "source_object_id", "capture_version",
    "capture_record_ordinal", "known_at", "isin", "transaction_id",
    "universe_disposition", "universe_disposition_reason", "issuer_lei",
    "normalized_evidence_json",
)


class G1C2Error(Exception):
    """Fail-closed abort for G1-C2 (invalid T, corrupt replay substrate,
    non-bijective reconstruction, missing evidence)."""


def _parse_at(at: Any) -> str:
    """Fail-closed T parsing: offset-aware timestamp -> canonical UTC str.

    Missing, unparseable, or naive timestamps are rejected — an
    unscopeable T can never silently widen/narrow the evidence window.
    """
    if isinstance(at, datetime):
        dt = at
    elif isinstance(at, str) and at.strip():
        try:
            dt = datetime.fromisoformat(at.strip())
        except ValueError as exc:
            raise G1C2Error(f"unparseable as-of timestamp: {at!r}") from exc
    else:
        raise G1C2Error(f"missing as-of timestamp: {at!r}")
    if dt.tzinfo is None:
        raise G1C2Error(f"as-of timestamp must be timezone-aware: {at!r}")
    return _canon_ts(dt.astimezone(UTC))


def _sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _connect_publications(tape_dir: Path):
    """Open embedded DuckDB exposing ONLY the ``publications`` view.

    ``trade_chains`` is never created — as_of must be physically unable
    to read materialized lifecycle authority.
    """
    import duckdb

    con = duckdb.connect()
    p = str((tape_dir / PUB_PARQUET).resolve()).replace("\\", "/")
    con.execute(
        "CREATE VIEW publications AS "  # noqa: S608 - fixed view name
        f"SELECT * FROM '{p}'")         # + operator-local path literal
    return con


def _eligible_publications(con, t_canon: str) -> list[dict[str, Any]]:
    """``publications WHERE known_at <= T`` in canonical tape order.

    The filter criterion is exactly ``known_at <= T`` — boundary
    inclusive. A NULL ``known_at`` anywhere aborts: knowledge time is
    mandatory, unscopeable publications fail closed instead of leaking.
    """
    null_known_at = con.execute(
        "SELECT count(*) FROM publications WHERE known_at IS NULL"
    ).fetchone()[0]
    if null_known_at:
        raise G1C2Error(
            f"{null_known_at} publications without known_at — refusing "
            "to reconstruct over unscopeable evidence")
    colspec = ", ".join(f'"{c}"' for c in _REPLAY_SELECT)
    rows = con.execute(
        f"SELECT {colspec} FROM publications "  # noqa: S608 - fixed cols
        "WHERE known_at <= ? "
        "ORDER BY source, source_object_id, capture_version, "
        "capture_record_ordinal",
        [t_canon]).fetchall()
    return [dict(zip(_REPLAY_SELECT, r, strict=True)) for r in rows]


def as_of(tape_dir: Path | str, at: Any) -> dict[str, Any]:
    """Reconstruct G0-D chain heads exactly as knowable at T.

    Reads ``publications.parquet`` (through embedded DuckDB) under
    ``tape_dir``, filters by ``known_at <= T``, replays each row's
    ``normalized_evidence_json`` through ``record_from_evidence``, and
    rebuilds the ledger/chains with the authoritative G0-D machinery.
    Returns ``{"as_of_utc", "reference_basis", "row_count", "rows",
    "semantic_digest", "max_known_at_used"}``.
    """
    t_canon = _parse_at(at)
    con = _connect_publications(Path(tape_dir))
    try:
        pubs = _eligible_publications(con, t_canon)
    finally:
        con.close()

    records: list[Any] = []
    for r in pubs:
        try:
            records.append(
                record_from_evidence(
                    json.loads(r["normalized_evidence_json"])))
        except (json.JSONDecodeError, ReplayError) as exc:
            raise G1C2Error(
                "replay substrate failure at "
                f"{r['source']}/{r['source_object_id']}/"
                f"{r['capture_version']}#{r['capture_record_ordinal']}: "
                f"{exc}") from exc

    indexed = build_ledger_indexed(records)
    row_by_entry: dict[str, dict[str, Any]] = {}
    for pos, entry in indexed:
        row_by_entry[entry.entry_id] = pubs[pos]
    if len(row_by_entry) != len(indexed):
        raise G1C2Error("reconstructed ledger has duplicate entry ids")
    chains = reconstruct_chains([e for _, e in indexed])

    rows: list[dict[str, Any]] = []
    chained = 0
    for chain_id, chain in chains.items():
        chained += len(chain)
        head = chain[-1]                        # authoritative head, exactly
        head_pub = row_by_entry[head.entry_id]
        known_ats = [row_by_entry[e.entry_id]["known_at"] for e in chain]
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
            "first_known_at": min(known_ats),
            "head_known_at": head_pub["known_at"],
            "as_of_utc": t_canon,
            "reference_basis": REFERENCE_BASIS,
            "universe_disposition": head_pub["universe_disposition"],
            "universe_disposition_reason":
                head_pub["universe_disposition_reason"],
            "issuer_lei": head_pub["issuer_lei"],
        })
    if chained != len(indexed):
        raise G1C2Error("reconstruct_chains did not cover every entry")

    return {
        "as_of_utc": t_canon,
        "reference_basis": REFERENCE_BASIS,
        "row_count": len(rows),
        "rows": rows,
        "semantic_digest": asof_semantic_digest(rows, t_canon),
        "max_known_at_used": max(
            (r["known_at"] for r in pubs), default=None),
    }


def asof_semantic_digest(rows: list[dict[str, Any]], as_of_utc: str) -> str:
    """Deterministic digest of an as_of(T) result; T binds the identity."""
    body = canonical_table_bytes(
        rows, ASOF_COLUMNS, lambda r: r["chain_id"])
    material = {
        "as_of_utc": as_of_utc,
        "reference_basis": REFERENCE_BASIS,
        "row_count": len(rows),
        "rows_sha256": _sha(body),
    }
    return _sha(json.dumps(material, sort_keys=True,
                           separators=(",", ":")))


def chains_projection_digest(rows: list[dict[str, Any]]) -> str:
    """Digest of as_of rows projected onto the trade_chains column set —
    comparable to the materialized ``trade_chains`` semantic digest."""
    proj = [{c: r[c] for c in CHAIN_COLUMNS} for r in rows]
    return _sha(canonical_table_bytes(
        proj, CHAIN_COLUMNS, lambda r: r["chain_id"]))


def build_report(
    raw_root: Path,
    runs_dir: Path,
    g1a_artifact: Path,
    g1b_rows: Path,
    g1b_report: Path,
    *,
    window: dict[str, str],
    tape_dir: Path,
    c1_report_path: Path,
    asof_timestamps: list[str],
    date: str,
    generated_utc: str,
    expected_g1a_digest: str = EXPECTED_G1A_SEMANTIC_DIGEST,
    expected_g1b_digest: str = EXPECTED_G1B_ROWS_DIGEST,
    expected_g1b_report_sha256: str = EXPECTED_G1B_REPORT_SHA256,
    expected_g1b_basis_id: str = EXPECTED_G1B_BASIS_ID,
) -> dict[str, Any]:
    """Materialize the schema-v2 tape and run as_of(T) evidence.

    Materialization delegates entirely to ``metrics.g1c1.build_report``
    (same verified upstream authority, same §4.1/§4.2 semantics — schema
    v2 adds only the replay substrate column). The verified C1 v1 report
    is loaded read-only as the historical parent authority.
    """
    v2, _pub_b, _chain_b = build_tape_v2(
        raw_root, runs_dir, g1a_artifact, g1b_rows, g1b_report,
        window=window, tape_dir=tape_dir, date=date,
        generated_utc=generated_utc,
        expected_g1a_digest=expected_g1a_digest,
        expected_g1b_digest=expected_g1b_digest,
        expected_g1b_report_sha256=expected_g1b_report_sha256,
        expected_g1b_basis_id=expected_g1b_basis_id)

    if not c1_report_path.is_file():
        raise G1C2Error(
            f"verified C1 report not found: {c1_report_path} — parent "
            "authority is mandatory for v1/v2 reconciliation")
    c1 = yaml.safe_load(c1_report_path.read_bytes().decode("utf-8")) or {}
    v1_tables = c1.get("tables") or {}
    v1_pubs = v1_tables.get("publications") or {}
    v1_chains = v1_tables.get("trade_chains") or {}
    c1_sha = _sha(c1_report_path.read_bytes())
    if c1.get("tape_schema_version") != 1:
        raise G1C2Error(
            f"C1 report tape_schema_version "
            f"{c1.get('tape_schema_version')!r} != 1 — refusing "
            "unverified parent authority")

    # Replay-fidelity probe: as_of(max known_at) selects the whole tape;
    # its rebuilt chain set must equal the materialized trade_chains
    # projection exactly — ids included.
    con = _connect_publications(tape_dir)
    try:
        max_known_at = con.execute(
            "SELECT max(known_at) FROM publications").fetchone()[0]
    finally:
        con.close()

    timestamps = [_parse_at(t) for t in asof_timestamps]
    if max_known_at not in timestamps:
        timestamps.append(max_known_at)
    timestamps = sorted(set(timestamps))

    runs: list[dict[str, Any]] = []
    fidelity: dict[str, Any] = {}
    for t in timestamps:
        res = as_of(tape_dir, t)
        runs.append({
            "as_of_utc": res["as_of_utc"],
            "row_count": res["row_count"],
            "semantic_digest": res["semantic_digest"],
            "max_known_at_used": res["max_known_at_used"],
            "no_future_evidence":
                res["max_known_at_used"] is None
                or res["max_known_at_used"] <= res["as_of_utc"],
        })
        if res["as_of_utc"] == max_known_at:
            proj = chains_projection_digest(res["rows"])
            fidelity = {
                "as_of_utc": res["as_of_utc"],
                "chain_projection_digest": proj,
                "materialized_chains_digest":
                    v2["tables"]["trade_chains"]["semantic_digest"],
                "equals_materialized_chains":
                    proj == v2["tables"]["trade_chains"]
                    ["semantic_digest"],
                "equals_v1_chains":
                    proj == v1_chains.get("semantic_digest"),
            }
            if not fidelity["equals_materialized_chains"]:
                raise G1C2Error(
                    "as_of(max known_at) chain projection diverges from "
                    "the materialized trade_chains — replay substrate is "
                    "not faithful to G0-D")

    return {
        "gate": "G1",
        "issue_id": "G1-C2",
        "report": "G1-C2_ASOF_RECONSTRUCTION",
        "date": date,
        "generated_utc": generated_utc,
        "frozen_authority": {
            "spec": "docs/gates/G1.md §4.2",
            "freeze_tag": "g1-freeze-v0",
            "freeze_commit": "2a5e4684b9a3f6066e6d2a53cbc37b920e2c9ffa",
        },
        "generator": {
            "module": "metrics.g1c2",
            "code_commit": _git_head(),
            "deterministic": True,
        },
        "tape_schema_version": TAPE_SCHEMA_VERSION,
        "asof_rule": (
            "as_of(T) = publications WHERE known_at <= T -> "
            "build_ledger -> reconstruct_chains -> chain[-1]; G0-D "
            "ordering and entry_id tie-break; materialized trade_chains "
            "and full-tape lifecycle fields are never consulted."),
        "replay_substrate": {
            "field": "normalized_evidence_json",
            "contract": (
                "canonical JSON (sorted keys, compact separators) of "
                "NormalizedRecord.to_evidence() per publication "
                "occurrence; metadata only — no raw provider fields"),
            "adapter": "normalization.replay.record_from_evidence",
            "input_order": (
                "source, source_object_id, capture_version, "
                "capture_record_ordinal — identical to the "
                "materialization input order, so occurrence ids are "
                "rebuilt deterministically from the T-limited subset"),
        },
        "parent_authority": {
            "c1_report": str(c1_report_path.as_posix()),
            "c1_report_sha256": c1_sha,
            "v1": {
                "tape_schema_version": c1.get("tape_schema_version"),
                "publications": {
                    "row_count": v1_pubs.get("row_count"),
                    "semantic_digest": v1_pubs.get("semantic_digest"),
                },
                "trade_chains": {
                    "row_count": v1_chains.get("row_count"),
                    "semantic_digest": v1_chains.get("semantic_digest"),
                },
                "dataset_semantic_digest":
                    c1.get("dataset_semantic_digest"),
            },
        },
        "tape_materialization_v2": {
            "publications": v2["tables"]["publications"],
            "trade_chains": v2["tables"]["trade_chains"],
            "dataset_semantic_digest": v2["dataset_semantic_digest"],
            "lifecycle_reconciliation": v2["lifecycle_reconciliation"],
            "diagnostics": v2["diagnostics"],
        },
        "v1_v2_reconciliation": {
            "publications": {
                "v1_rows": v1_pubs.get("row_count"),
                "v2_rows": v2["tables"]["publications"]["row_count"],
                "v1_digest": v1_pubs.get("semantic_digest"),
                "v2_digest":
                    v2["tables"]["publications"]["semantic_digest"],
            },
            "trade_chains": {
                "v1_rows": v1_chains.get("row_count"),
                "v2_rows": v2["tables"]["trade_chains"]["row_count"],
                "v1_digest": v1_chains.get("semantic_digest"),
                "v2_digest": v2["tables"]["trade_chains"]
                ["semantic_digest"],
            },
            "dataset_semantic_digest": {
                "v1": c1.get("dataset_semantic_digest"),
                "v2": v2["dataset_semantic_digest"],
            },
            "explanation": (
                "Row counts are unchanged: no market-data change. The "
                "publications digest changes only because schema v2 adds "
                "the normalized_evidence_json replay-substrate column; "
                "trade_chains is semantically identical (same columns, "
                "same G0-D result), so its digest is unchanged. The "
                "dataset digest changes because it binds "
                "tape_schema_version 2 plus the new publications "
                "digest."),
        },
        "replay_fidelity": fidelity,
        "asof_runs": runs,
        "reference_semantics": {
            "reference_basis": REFERENCE_BASIS,
            "note": (
                "universe_disposition / reason / issuer_lei are the "
                "frozen G1-B build-snapshot classification (verified "
                "G1-B authority), explicitly NOT a claim that the "
                "reference data was known at T; no bitemporal "
                "reference history."),
        },
        "hygiene": {
            "provider_payloads_in_repo": False,
            "parquet_files_committed": False,
            "provider_raw_reread_for_asof": False,
        },
    }


def generate_report(
    raw_root: Path,
    runs_dir: Path,
    g1a_artifact: Path,
    g1b_rows: Path,
    g1b_report: Path,
    *,
    window: dict[str, str],
    tape_dir: Path,
    c1_report_path: Path,
    asof_timestamps: list[str],
    evidence_dir: Path,
    date: str,
    generated_utc: str,
    expected_g1a_digest: str = EXPECTED_G1A_SEMANTIC_DIGEST,
    expected_g1b_digest: str = EXPECTED_G1B_ROWS_DIGEST,
    expected_g1b_report_sha256: str = EXPECTED_G1B_REPORT_SHA256,
    expected_g1b_basis_id: str = EXPECTED_G1B_BASIS_ID,
) -> Path:
    report = build_report(
        raw_root, runs_dir, g1a_artifact, g1b_rows, g1b_report,
        window=window, tape_dir=tape_dir, c1_report_path=c1_report_path,
        asof_timestamps=asof_timestamps, date=date,
        generated_utc=generated_utc,
        expected_g1a_digest=expected_g1a_digest,
        expected_g1b_digest=expected_g1b_digest,
        expected_g1b_report_sha256=expected_g1b_report_sha256,
        expected_g1b_basis_id=expected_g1b_basis_id)
    ev = evidence_dir / "g1-c2"
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / REPORT_NAME
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(report, fh, sort_keys=False)
    sha = _sha(path.read_bytes())
    (ev / f"{path.stem}.sha256.txt").write_text(
        f"{sha} *{path.name}\n", encoding="utf-8")
    return path


def _asof_cli(tape_dir: Path, at: str) -> int:
    res = as_of(tape_dir, at)
    out = {k: v for k, v in res.items() if k != "rows"}
    out["rows_preview"] = res["rows"][:10]
    print(yaml.safe_dump(out, sort_keys=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--at", default=None,
                    help="run a single as_of(T) query over --tape-dir "
                         "and print the result (no evidence written)")
    ap.add_argument("--raw-root", type=Path)
    ap.add_argument("--runs-dir", type=Path)
    ap.add_argument("--g1a-artifact", type=Path)
    ap.add_argument("--g1b-rows", type=Path)
    ap.add_argument("--g1b-report", type=Path)
    ap.add_argument("--c1-report", type=Path,
                    default=Path("evidence/g1-c1/"
                                 "G1-C1_tape_materialization.yaml"),
                    help="verified C1 v1 report — parent authority")
    ap.add_argument("--tape-dir", type=Path,
                    default=Path("data/g1/tape"))
    ap.add_argument("--out", type=Path, default=Path("evidence"))
    ap.add_argument("--date")
    ap.add_argument("--generated-utc")
    ap.add_argument("--window",
                    help="JSON with operational_start + evidence_tail_end")
    ap.add_argument("--asof-timestamps", default="[]",
                    help="JSON list of offset-aware timestamps to run")
    args = ap.parse_args(argv)

    if args.at is not None:
        return _asof_cli(args.tape_dir, args.at)

    for name in ("raw_root", "runs_dir", "g1a_artifact", "g1b_rows",
                 "g1b_report", "date", "generated_utc", "window"):
        if getattr(args, name) is None:
            ap.error(f"--{name.replace('_', '-')} is required "
                     "for evidence generation")
    path = generate_report(
        args.raw_root, args.runs_dir, args.g1a_artifact, args.g1b_rows,
        args.g1b_report, window=json.loads(args.window),
        tape_dir=args.tape_dir, c1_report_path=args.c1_report,
        asof_timestamps=json.loads(args.asof_timestamps),
        evidence_dir=args.out, date=args.date,
        generated_utc=args.generated_utc)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
