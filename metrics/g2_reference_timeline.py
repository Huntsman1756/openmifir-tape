"""Canonical reference bundle metadata and chronological admission planning.

Frozen G2-B (#48). Content validation is required by the integrated B1
loader before publication; these helpers perform no acquisition/activation.
"""

from __future__ import annotations

import json
from pathlib import Path

from universe.reference import canonical_reference_set

from .g2_authority import AuthorityError, checked_file, digest, mapping, text, utc
from .g2_reference_observation import FAMILIES, observation
from .g2_selection import load_metadata


def _basis(episode: str, observations: dict) -> dict:
    sets, canonical = {}, {}
    for family in sorted(FAMILIES):
        unique, acquisitions, entries, identifiers = {}, set(), {}, set()
        whole_scope = False
        for obs in observations[family]:
            oid = obs["observation_id"]
            if obs["family"] != family or digest({k: v for k, v in obs.items() if k != "observation_id"}) != oid:
                raise AuthorityError("reference observation identity/family mismatch")
            if oid in unique:
                continue  # Physical copies were independently authenticated by prepare_bundle.
            if obs["acquisition_id"] in acquisitions:
                raise AuthorityError("conflicting acquisition identity within family")
            acquisitions.add(obs["acquisition_id"])
            requested = obs["scope"]
            if requested["mode"] == "ALL_FILE_RECORDS":
                if unique:
                    raise AuthorityError("ambiguous overlapping reference scopes")
                whole_scope = True
            elif whole_scope or identifiers.intersection(requested["identifiers"]):
                raise AuthorityError("ambiguous overlapping reference scopes")
            identifiers.update(requested["identifiers"])
            for timed in obs["files"]:
                entry = timed["file"]
                old = entries.setdefault(entry["name"], entry)
                if old != entry:
                    raise AuthorityError("conflicting canonical reference filename")
            unique[oid] = obs
        if not unique:
            raise AuthorityError("every family requires an explicit observation/outcome")
        canonical[family] = [v for _, v in sorted(unique.items())]
        sets[family] = canonical_reference_set(entries.values())
    body = {"schema": "g2-reference-basis-v1", "episode_id": text(episode, "episode_id"),
            "observations": canonical, "family_sets": sets,
            "reference_known_at": max(o["observed_at"] for family in canonical.values() for o in family),
            "reference_basis": "OBSERVED_REFERENCE_SNAPSHOT"}
    return {**body, "bundle_id": digest(body)}


def prepare_bundle(root: Path, value: dict) -> dict:
    """Authenticate all metadata/copies; packaging paths never set knowledge time."""
    doc = mapping(value, {"schema", "episode_id", "observations"})
    if doc["schema"] != "g2-reference-bundle-v1":
        raise AuthorityError("unsupported reference bundle schema")
    families = mapping(doc["observations"], set(FAMILIES))
    observations, roots, packaging, names = {}, {}, [], set()
    for family in sorted(FAMILIES):
        entries = families[family]
        if not isinstance(entries, list) or not entries:
            raise AuthorityError("all three families require explicit evidence")
        observations[family] = []
        for entry in entries:
            path = checked_file(root, entry)
            if entry["name"] in names:
                raise AuthorityError("reference metadata names must be unique")
            names.add(entry["name"])
            obs = observation(path.parent, load_metadata(path))
            observations[family].append(obs)
            roots.setdefault(obs["observation_id"], path.parent)
            packaging.append(dict(entry))
    return {"bundle": _basis(doc["episode_id"], observations), "observation_roots": roots,
            "packaging": sorted(packaging, key=lambda e: e["name"])}


def _timeline(bundles: list) -> dict:
    body = {"schema": "g2-reference-timeline-v1", "bundles": bundles}
    return {**body, "semantic_digest": digest(body)}


def empty_timeline() -> dict:
    return _timeline([])


def _validate_timeline(state: dict) -> None:
    if state != _timeline(state["bundles"]):
        raise AuthorityError("reference timeline integrity failure")
    episodes, last = set(), None
    for bundle in state["bundles"]:
        if bundle != _basis(bundle["episode_id"], bundle["observations"]):
            raise AuthorityError("accepted reference bundle identity failure")
        known = utc(bundle["reference_known_at"])
        if bundle["episode_id"] in episodes or (last is not None and known <= last):
            raise AuthorityError("accepted reference timeline chronology is ambiguous")
        episodes.add(bundle["episode_id"])
        last = known


def plan_reference_admission(previous: dict, bundle: dict, sealed_windows: list[dict]) -> dict:
    """Append only; replays preserve identity and failures leave caller state intact."""
    state = json.loads(json.dumps(previous))
    _validate_timeline(state)
    if bundle != _basis(bundle["episode_id"], bundle["observations"]):
        raise AuthorityError("reference bundle identity failure")
    for old in state["bundles"]:
        if old["bundle_id"] == bundle["bundle_id"]:
            if old != bundle:
                raise AuthorityError("conflicting accepted reference bundle")
            return state
        if old["episode_id"] == bundle["episode_id"]:
            raise AuthorityError("accepted acquisition episode cannot be rewritten/reobserved")
    known = utc(bundle["reference_known_at"])
    if state["bundles"]:
        last = utc(state["bundles"][-1]["reference_known_at"])
        if known == last:
            raise AuthorityError("distinct bundles at the same knowledge time are ambiguous")
        if known < last:
            raise AuthorityError("older reference bundle requires a versioned correction proposal")
    for window in sealed_windows:
        end = utc(window["end"])
        within = known <= end if window["kind"] == "G1_ORIGIN" else known < end
        if utc(window["start"]) <= known and within:
            raise AuthorityError("reference observation within sealed interval requires a correction proposal")
    state["bundles"].append(bundle)
    return _timeline(state["bundles"])


def select_reference(timeline: dict, cutoff: str) -> dict | None:
    """Select one complete bundle; never mix/carry forward individual families."""
    _validate_timeline(timeline)
    t = utc(cutoff)
    eligible = [b for b in timeline["bundles"] if utc(b["reference_known_at"]) <= t]
    return json.loads(json.dumps(eligible[-1])) if eligible else None
