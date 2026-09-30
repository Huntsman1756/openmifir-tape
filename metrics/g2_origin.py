"""Import only the exact verified G1 origin; never reinterpret its interval.

Authority: docs/gates/G2.md#G2-A and frozen G1-A (#47).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .g2_authority import AuthorityError, checked_file, digest, file_entry, utc
from .g2_registry import G1_BUNDLE_SHA256, G1_END, G1_START, admit, empty_registry, window_record
from .g2_selection import load_metadata, select_attestations
from .selection import select_eligible

G1_BUNDLE_ID = "g0-a3-bundle-20260924T181127Z"


def _sealed_entries(root: Path) -> list[dict]:
    """Authenticate the original byte manifest and every declared file."""
    try:
        content = (root / "SHA256SUMS.txt").read_bytes()
        if hashlib.sha256(content).hexdigest() != G1_BUNDLE_SHA256:
            raise AuthorityError("G1 sealed bundle fingerprint mismatch")
        entries, names = [], set()
        for line in content.decode("utf-8").splitlines():
            sha, name = line.split("  ", 1)
            # The pinned byte manifest has no byte-count column; compute size
            # locally, but authenticate content against its original hash.
            entry = file_entry({"name": name, "sha256": sha, "bytes": 0})
            if name in names:
                raise AuthorityError("duplicate G1 bundle name")
            names.add(name)
            path = (root / name).resolve()
            if not path.is_relative_to(root.resolve()):
                raise AuthorityError("G1 bundle path escape")
            entry["bytes"] = path.stat().st_size
            checked_file(root, entry)
            entries.append(entry)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, AuthorityError):
            raise
        raise AuthorityError("unreadable/malformed G1 sealed bundle") from exc
    return entries


def import_origin(root: Path, previous: dict | None = None) -> dict:
    """Return an authenticated admission plan; no files or G1 artifacts change."""
    entries = _sealed_entries(root)
    meta = load_metadata(root / "collection-meta.yaml")
    if meta.get("bundle_id") != G1_BUNDLE_ID:
        raise AuthorityError("G1 origin bundle identity mismatch")
    declared_runs = {e["name"] for e in entries if e["name"].startswith("runs/")
                     and e["name"].endswith("__manifest.yaml")}
    physical_runs = {"runs/" + p.name for p in (root / "runs").glob("*__manifest.yaml")}
    if declared_runs != physical_runs:
        raise AuthorityError("G1 run inventory differs from pinned byte authority")
    original = select_eligible(root / "runs", root / "raw", start=utc(G1_START), tail_end=utc(G1_END))
    by_name = {e["name"]: e for e in entries}
    manifest_entries = []
    for name in original["eligible_manifests"]:
        entry = by_name.get("runs/" + name)
        if entry is None:
            raise AuthorityError("eligible G1 manifest absent from sealed byte authority")
        manifest_entries.append(entry)
    selected = select_attestations(root, manifest_entries, root / "raw",
                                   start=utc(G1_START), end=utc(G1_END), sources=["bme_apa", "blb_apae"],
                                   inclusive_end=True)

    def capture_basis(versions):
        return [(v.source_id, v.source_object_id, v.capture_version, v.first_observed_at,
                 v.last_observed_at, v.observation_count) for v in versions]

    if capture_basis(original["versions"]) != capture_basis(selected["versions"]):
        raise AuthorityError("G1-origin observation basis changed")
    binding = {"authority": "docs/gates/G1.md#G1-A", "bundle_id": G1_BUNDLE_ID,
               "bundle_manifest_sha256": G1_BUNDLE_SHA256,
               "selection_rule": "[operational_start, evidence_tail_end]",
               "reference_classification": "LEGACY_BUILD_SNAPSHOT"}
    window = window_record("G1_ORIGIN", digest(binding), G1_BUNDLE_SHA256, G1_START, G1_END,
                           ["bme_apa", "blb_apae"])
    selected["census"]["manifests_total"] = original["census"]["manifests_total"]
    selected["census"]["manifests_excluded"] = (original["census"]["manifests_total"]
                                                  - original["census"]["manifests_eligible"])
    return {"origin_binding": binding, "window": window, "selection": selected,
            "original_selection_census": original["census"],
            "registry": admit(previous if previous is not None else empty_registry(), window, selected["attestations"])}
