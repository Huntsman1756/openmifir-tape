"""Executable consumer of the frozen 15+5 observation sample (F-009).

``fixtures/universe/universe_manifest.yaml`` freezes the ``es-corporate-bonds``
observation seed (15 active + 5 quiet ISINs). Until the real
``es_legal_issuer_v1`` disposition table is materialized from live FIRDS +
FITRS + GLEIF data, this sample is the ONLY authoritative profile scope:
integration-scope metrics (whole provider files) are engine-conformance
evidence; profile-scope metrics (records whose ISIN is in this frozen set)
are the evidence that counts for G0-E / G0 PASS.

The frozen sample itself is validated against live FITRS + FIRDS + GLEIF
reference data by ``universe/sample_validation.py`` (F-009), whose
metadata-only artifact lives at
``evidence/g0-b1/F-009_frozen_sample_validation.yaml``. ISINs that resolve to
EXCLUDE/QUARANTINE/CONFLICT there stay in the sample (preregistration — never
re-drawn); their documented dispositions qualify what "profile scope" means.

Fail closed: a malformed or missing manifest raises; nothing is guessed. The
frozen shape is enforced at load time — bucket counts (15 active + 5 quiet),
ISIN uniqueness (20 distinct), the frozen status token, and the declared
``sample_snapshot_sha256`` against the recomputed ISIN digest. Any mismatch
raises ``SampleManifestError``; the loader never returns a mutated sample.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ._paths import repo_file

DEFAULT_SAMPLE_MANIFEST = repo_file("fixtures", "universe", "universe_manifest.yaml")


class SampleManifestError(Exception):
    """Raised when the frozen sample manifest is missing, malformed, or
    fails the frozen-shape verification (counts, status, digest)."""


# The frozen 15+5 observation sample shape (FROZEN_SAMPLE_2026_09_14). These
# constants ARE the freeze: re-freezing the sample is a spec change and must
# update them here via a reviewed PR, not silently in the manifest.
FROZEN_SAMPLE_STATUS = "FROZEN_SAMPLE_2026_09_14"
FROZEN_ACTIVE_COUNT = 15
FROZEN_QUIET_COUNT = 5
FROZEN_TOTAL_COUNT = 20


@dataclass(frozen=True)
class FrozenSample:
    universe_id: str
    profile: str
    status: str
    isins: frozenset[str]
    isin_order: tuple[str, ...]
    snapshot_sha256: str
    buckets: dict[str, str] = field(default_factory=dict)

    def contains(self, instrument_identification_code: str | None) -> bool:
        return (instrument_identification_code or "") in self.isins

    def bucket_of(self, isin: str) -> str:
        return self.buckets.get(isin, "")

    def isin_digest(self) -> str:
        """SHA-256 over the manifest's ISIN list (active then quiet, LF-joined)."""
        return hashlib.sha256("\n".join(self.isin_order).encode("utf-8")).hexdigest()


def load_frozen_sample(path: Path = DEFAULT_SAMPLE_MANIFEST) -> FrozenSample:
    """Load the frozen sample ISIN set from the universe manifest."""
    if not path.exists():
        raise SampleManifestError(f"universe manifest not found: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    selected = (((doc.get("corpus") or {}).get("selected_isins")) or {})
    status = str(selected.get("status") or "")
    if status != FROZEN_SAMPLE_STATUS:
        raise SampleManifestError(
            f"selected_isins.status {status!r} != frozen {FROZEN_SAMPLE_STATUS!r}"
        )
    isins: list[str] = []
    buckets: dict[str, str] = {}
    bucket_counts: dict[str, int] = {}
    for bucket in ("active", "quiet"):
        entries = selected.get(bucket) or []
        if not isinstance(entries, list):
            raise SampleManifestError(f"selected_isins.{bucket} must be a list")
        bucket_counts[bucket] = len(entries)
        for entry in entries:
            isin = (entry or {}).get("isin") if isinstance(entry, dict) else None
            if not isinstance(isin, str) or not isin.strip():
                raise SampleManifestError(f"selected_isins.{bucket} entry missing 'isin'")
            isins.append(isin.strip())
            buckets[isin.strip()] = bucket
    if bucket_counts["active"] != FROZEN_ACTIVE_COUNT:
        raise SampleManifestError(
            f"active count {bucket_counts['active']} != frozen {FROZEN_ACTIVE_COUNT}"
        )
    if bucket_counts["quiet"] != FROZEN_QUIET_COUNT:
        raise SampleManifestError(
            f"quiet count {bucket_counts['quiet']} != frozen {FROZEN_QUIET_COUNT}"
        )
    if len(set(isins)) != FROZEN_TOTAL_COUNT or len(isins) != FROZEN_TOTAL_COUNT:
        raise SampleManifestError(
            f"expected {FROZEN_TOTAL_COUNT} distinct ISINs, got {len(isins)} entries "
            f"({len(set(isins))} distinct)"
        )
    declared = str(doc.get("sample_snapshot_sha256") or "")
    computed = hashlib.sha256("\n".join(isins).encode("utf-8")).hexdigest()
    if declared != computed:
        raise SampleManifestError(
            f"sample_snapshot_sha256 mismatch: declared {declared!r} != computed {computed!r}"
        )
    return FrozenSample(
        universe_id=str(doc.get("universe_id") or ""),
        profile=str(doc.get("profile") or ""),
        status=str(selected.get("status") or ""),
        isins=frozenset(isins),
        isin_order=tuple(isins),
        snapshot_sha256=str(doc.get("sample_snapshot_sha256") or ""),
        buckets=buckets,
    )
