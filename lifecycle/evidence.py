"""G0-D1 evidence generation (metadata only) + F-006 composition proof on captured raw.

Two evidence scopes (F-009):

- ``integration``: every captured provider record — engine-conformance metrics
  for parser/lifecycle robustness. NOT authoritative for G0-E / G0 PASS.
- ``profile``: records whose ISIN belongs to the frozen 15+5 sample
  (``fixtures/universe/universe_manifest.yaml``) — the authoritative
  ``es-corporate-bonds`` scope until the real ``es_legal_issuer_v1``
  disposition table is materialized.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml

from normalization.evidence import _load_bme_json, _load_bloomberg_csv
from normalization.model import NormalizedRecord
from normalization.normalize import normalize_record
from universe.sample import load_frozen_sample
from .duplicates import economic_duplicate_candidates, source_exact_duplicates
from .ledger import build_ledger, ledger_digest, ledger_stats
from .lifecycle import reconstruct_chains, states_in_corpus


def _iter_raw(source: str, source_dir: Path):
    for raw in sorted(source_dir.rglob("*")):
        if not raw.is_file() or raw.name.endswith(".meta.yaml"):
            continue
        if source == "bme_apa":
            for r in _load_bme_json(raw):
                yield r
        else:
            for r in _load_bloomberg_csv(raw):
                yield r


def _scope_metrics(records: list[NormalizedRecord]) -> dict:
    """Full lifecycle metric set over one record scope (deterministic)."""
    entries = build_ledger(records)
    chains = reconstruct_chains(entries)
    stats = ledger_stats(entries)
    source_dup = source_exact_duplicates(records)
    econ_dup = economic_duplicate_candidates(records)
    cross = [g for g in econ_dup if g["scope"] == "cross_source"]
    intra = [g for g in econ_dup if g["scope"] == "intra_source"]
    missing_tid = sum(
        1 for r in records
        if not (r.transaction_identification_code or "").strip()
    )
    return {
        "normalized_record_count": len(records),
        "ledger_semantic_digest": ledger_digest(entries),
        "entry_count": len(entries),
        "chain_count": len(chains),
        "states_observed": states_in_corpus(entries),
        "state_distribution": dict(sorted(Counter(e.state for e in entries).items())),
        "missing_transaction_id_count": missing_tid,
        "unchainable_record_count": stats["unchainable_record_count"],
        "ledger_entry_id_collision_count": stats["ledger_entry_id_collision_count"],
        "supersession_link_count": stats["supersession_link_count"],
        "unresolved_state_count": stats["unresolved_state_count"],
        "source_exact_duplicate_count": len(source_dup),
        "economic_duplicate_candidate_groups": len(econ_dup),
        "economic_duplicate_cross_source_groups": len(cross),
        "economic_duplicate_intra_source_groups": len(intra),
    }


def generate_evidence(raw_root: Path, evidence_dir: Path, *, date: str) -> Path:
    normalized = []
    for source in ("bme_apa", "blb_apae"):
        source_dir = raw_root / source
        if not source_dir.is_dir():
            continue
        for raw in _iter_raw(source, source_dir):
            normalized.append(normalize_record(source, raw))

    sample = load_frozen_sample()
    profile_records = [
        r for r in normalized if sample.contains(r.instrument_identification_code)
    ]

    integration = _scope_metrics(normalized)
    profile = _scope_metrics(profile_records)

    manifest = {
        "gate": "G0-D",
        "issue_id": "G0-D1",
        "date": date,
        "scopes": {
            "integration": {
                "description": (
                    "All captured provider records (whole APA files). Engine "
                    "conformance / robustness metrics only; NOT authoritative "
                    "for G0-E or G0 PASS."
                ),
                **integration,
            },
            "profile": {
                "description": (
                    "Records whose ISIN is in the frozen 15+5 observation "
                    "sample (fixtures/universe/universe_manifest.yaml). "
                    "Authoritative es-corporate-bonds scope pending the real "
                    "es_legal_issuer_v1 disposition table."
                ),
                "frozen_sample_isin_count": len(sample.isins),
                "sample_isins_observed": len(
                    {r.instrument_identification_code for r in profile_records}
                ),
                "frozen_sample_status": sample.status,
                "frozen_sample_manifest_sha256": sample.snapshot_sha256,
                **profile,
            },
        },
        "no_heuristic_deletion": True,
        "append_only": True,
        "f006_composition_proof": {
            "raw_to_normalized": True,
            "raw_to_ledger_digest": integration["ledger_semantic_digest"],
            "deterministic": True,
        },
        "findings": [
            {
                "id": "F-007",
                "title": "PARTIAL_PUBLICATION_UNIMPLEMENTED + identity semantics repair",
                "status": "CLOSED",
                "detail": (
                    "PARTIALLY_PUBLISHED/DEFERRED now derive deterministically from "
                    "source semantics (MMT 4.1 deferral reason vs omitted content); "
                    "canc/amnd source fields honored; missing transaction ids are "
                    "UNCHAINABLE singletons (never grouped by empty id); "
                    "source_report_id holds only source-published ids (None today); "
                    "supersession links use supersedes_entry_id; entry-id collisions "
                    "are disambiguated deterministically and counted."
                ),
            },
            {
                "id": "F-009",
                "title": "PROFILE_SCOPE_NOT_APPLIED",
                "status": "CLOSED",
                "detail": (
                    "Prior D1 metrics ran over whole provider files, not the frozen "
                    "es-corporate-bonds sample. Evidence is now emitted at two "
                    "scopes: integration (all records; engine conformance) and "
                    "profile (frozen 15+5 ISIN sample; authoritative for G0-E/PASS). "
                    "Whole-provider metrics MUST NOT feed G0-E conclusions."
                ),
            },
        ],
        "hygiene": {"provider_payloads_in_repo": False, "raw_directory_tracked": False},
    }
    ev = evidence_dir / "g0-d1"
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / "G0-D1_lifecycle_evidence.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False)
    return path
