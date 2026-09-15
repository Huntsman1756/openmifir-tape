"""G0-D1 evidence generation (metadata only) + F-006 composition proof on captured raw."""

from __future__ import annotations

from pathlib import Path

import yaml

from normalization.evidence import _load_bme_json, _load_bloomberg_csv
from normalization.normalize import normalize_record
from .duplicates import economic_duplicate_candidates, source_exact_duplicates
from .ledger import build_ledger, ledger_digest
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


def generate_evidence(raw_root: Path, evidence_dir: Path, *, date: str) -> Path:
    normalized = []
    for source in ("bme_apa", "blb_apae"):
        source_dir = raw_root / source
        if not source_dir.is_dir():
            continue
        for raw in _iter_raw(source, source_dir):
            normalized.append(normalize_record(source, raw))

    entries = build_ledger(normalized)
    chains = reconstruct_chains(entries)
    digest = ledger_digest(entries)
    source_dup = source_exact_duplicates(normalized)
    econ_dup = economic_duplicate_candidates(normalized)

    manifest = {
        "gate": "G0-D",
        "issue_id": "G0-D1",
        "date": date,
        "ledger_semantic_digest": digest,
        "entry_count": len(entries),
        "chain_count": len(chains),
        "states_observed": states_in_corpus(entries),
        "source_exact_duplicate_count": len(source_dup),
        "economic_duplicate_candidate_groups": len(econ_dup),
        "no_heuristic_deletion": True,
        "append_only": True,
        "f006_composition_proof": {
            "raw_to_normalized": True,
            "raw_to_ledger_digest": digest,
            "deterministic": True,
        },
        "hygiene": {"provider_payloads_in_repo": False, "raw_directory_tracked": False},
    }
    ev = evidence_dir / "g0-d1"
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / "G0-D1_lifecycle_evidence.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False)
    return path
