"""G0-C1 evidence generation (metadata only).

Reads the locally-captured raw objects (gitignored data/raw), runs the
normalization pipeline, and emits METADATA-ONLY evidence: record counts, field
preservation, sanity-status and timestamp-parse distributions. NEVER writes
provider payload content to evidence.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml

from universe.sample import load_frozen_sample

from .model import NormalizedRecord
from .normalize import normalize_record


def _load_bme_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _load_bloomberg_csv(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    rows: list[dict] = []
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("Trading date and time"):
            header_idx = i
            break
    if header_idx is None:
        return rows
    reader = csv.reader(lines[header_idx:])
    header = next(reader)
    for row in reader:
        if not row or all(c == "" for c in row):
            continue
        rows.append(dict(zip(header, row)))
    return rows


def _norm_stats(records: list[NormalizedRecord]) -> dict:
    parse_dist: dict[str, int] = {}
    sanity_dist: dict[str, int] = {}
    field_counts: list[int] = []
    flags: int = 0
    for rec in records:
        parse_dist[rec.trading_datetime.parse_status] = parse_dist.get(rec.trading_datetime.parse_status, 0) + 1
        parse_dist[rec.publication_datetime.parse_status] = parse_dist.get(rec.publication_datetime.parse_status, 0) + 1
        for s in rec.sanity:
            sanity_dist[s.status] = sanity_dist.get(s.status, 0) + 1
        field_counts.append(len(rec.raw_fields))
        if rec.flags:
            flags += 1
    return {
        "record_count": len(records),
        "timestamp_parse_distribution": dict(sorted(parse_dist.items())),
        "sanity_distribution": dict(sorted(sanity_dist.items())),
        "min_raw_field_count": min(field_counts) if field_counts else 0,
        "max_raw_field_count": max(field_counts) if field_counts else 0,
        "records_with_flags": flags,
    }


def generate_evidence(raw_root: Path, evidence_dir: Path, *, date: str) -> Path:
    results: dict[str, dict] = {}
    total_records = 0
    profile_records = 0
    sample = load_frozen_sample()
    for source in ("bme_apa", "blb_apae"):
        source_dir = raw_root / source
        if not source_dir.is_dir():
            continue
        records: list[NormalizedRecord] = []
        for raw in sorted(source_dir.rglob("*")):
            if not raw.is_file() or raw.name.endswith(".meta.yaml"):
                continue
            if source == "bme_apa":
                for r in _load_bme_json(raw):
                    records.append(normalize_record(source, r))
            else:
                for r in _load_bloomberg_csv(raw):
                    records.append(normalize_record(source, r))
        results[source] = _norm_stats(records)
        total_records += results[source]["record_count"]
        profile_records += sum(
            1 for r in records if sample.contains(r.instrument_identification_code)
        )

    manifest = {
        "gate": "G0-C",
        "issue_id": "G0-C1",
        "date": date,
        "raw_lossless": True,
        "scope": "integration",
        "scope_note": (
            "Whole-provider-file metrics (F-009): engine conformance only, NOT "
            "the authoritative es-corporate-bonds profile scope."
        ),
        "total_normalized_records": total_records,
        "profile_scope_normalized_records": profile_records,
        "profile_scope_basis": "frozen 15+5 sample, fixtures/universe/universe_manifest.yaml",
        "by_source": results,
        "note": "Metadata only. Raw provider content is NOT included; only counts, "
                "parse/sanity distributions and field-preservation metrics.",
        "hygiene": {"provider_payloads_in_repo": False, "raw_directory_tracked": False},
    }
    ev = evidence_dir / "g0-c1"
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / "G0-C1_normalization_evidence.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False)
    return path
