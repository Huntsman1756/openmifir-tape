"""Executable consumer of the frozen 15+5 observation sample (F-009).

``fixtures/universe/universe_manifest.yaml`` freezes the ``es-corporate-bonds``
observation seed (15 active + 5 quiet ISINs). Until the real
``es_legal_issuer_v1`` disposition table is materialized from live FIRDS +
FITRS + GLEIF data, this sample is the ONLY authoritative profile scope:
integration-scope metrics (whole provider files) are engine-conformance
evidence; profile-scope metrics (records whose ISIN is in this frozen set)
are the evidence that counts for G0-E / G0 PASS.

Fail closed: a malformed or missing manifest raises; nothing is guessed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_SAMPLE_MANIFEST = (
    Path(__file__).resolve().parents[1] / "fixtures" / "universe" / "universe_manifest.yaml"
)


class SampleManifestError(Exception):
    """Raised when the frozen sample manifest is missing or malformed."""


@dataclass(frozen=True)
class FrozenSample:
    universe_id: str
    profile: str
    status: str
    isins: frozenset[str]
    isin_order: tuple[str, ...]
    snapshot_sha256: str

    def contains(self, instrument_identification_code: str | None) -> bool:
        return (instrument_identification_code or "") in self.isins

    def isin_digest(self) -> str:
        """SHA-256 over the manifest's ISIN list (active then quiet, LF-joined)."""
        return hashlib.sha256("\n".join(self.isin_order).encode("utf-8")).hexdigest()


def load_frozen_sample(path: Path = DEFAULT_SAMPLE_MANIFEST) -> FrozenSample:
    """Load the frozen sample ISIN set from the universe manifest."""
    if not path.exists():
        raise SampleManifestError(f"universe manifest not found: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    selected = (((doc.get("corpus") or {}).get("selected_isins")) or {})
    isins: list[str] = []
    for bucket in ("active", "quiet"):
        entries = selected.get(bucket) or []
        if not isinstance(entries, list):
            raise SampleManifestError(f"selected_isins.{bucket} must be a list")
        for entry in entries:
            isin = (entry or {}).get("isin") if isinstance(entry, dict) else None
            if not isinstance(isin, str) or not isin.strip():
                raise SampleManifestError(f"selected_isins.{bucket} entry missing 'isin'")
            isins.append(isin.strip())
    if not isins:
        raise SampleManifestError("frozen sample is empty")
    return FrozenSample(
        universe_id=str(doc.get("universe_id") or ""),
        profile=str(doc.get("profile") or ""),
        status=str(selected.get("status") or ""),
        isins=frozenset(isins),
        isin_order=tuple(isins),
        snapshot_sha256=str(doc.get("sample_snapshot_sha256") or ""),
    )
