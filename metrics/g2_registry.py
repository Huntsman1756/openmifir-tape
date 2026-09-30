"""Pure append-only admission planning for G2-A1; persistence belongs to C1.

Authority: docs/gates/G2.md#G2-A (#47). Caller authenticates bundle bytes
before invoking admission. Returned metadata contains no provider records.
"""

from __future__ import annotations

import json
from itertools import pairwise

from .g2_authority import AuthorityError, digest, fingerprint, mapping, text, timestamp, utc

G1_BUNDLE_SHA256 = "d9efcfd9f62758ca8d967f4a3e799542b5e64306e98a3575813a8382a37fb611"
G1_START = "2026-09-16T09:42:46Z"
G1_END = "2026-09-24T07:00:00Z"


def window_record(kind, profile_sha256, bundle_sha256, start, end, sources) -> dict:
    if kind not in {"G1_ORIGIN", "G2_PROSPECTIVE"}:
        raise AuthorityError("unsupported window kind")
    fingerprint(profile_sha256)
    fingerprint(bundle_sha256)
    if (not isinstance(sources, list) or not sources
            or any(not isinstance(s, str) or s not in {"bme_apa", "blb_apae"} for s in sources)
            or len(set(sources)) != len(sources) or not utc(start) < utc(end)):
        raise AuthorityError("invalid window interval/sources")
    if kind == "G1_ORIGIN" and (bundle_sha256 != G1_BUNDLE_SHA256
            or utc(start) != utc(G1_START) or utc(end) != utc(G1_END)
            or set(sources) != {"bme_apa", "blb_apae"}):
        raise AuthorityError("G1 origin must retain exact pinned authority")
    doc = {"kind": kind, "profile_sha256": profile_sha256, "bundle_sha256": bundle_sha256,
           "start": timestamp(utc(start)), "end": timestamp(utc(end)), "sources": sorted(sources)}
    return {"window_id": digest(doc), **doc}


def _attestation(value) -> dict:
    doc = mapping(value, {
        "source_id", "run_id", "source_object_id", "capture_version", "manifest_sha256", "observed_at",
    })
    for key in ("source_id", "run_id", "source_object_id"):
        text(doc[key], key)
    fingerprint(doc["capture_version"])
    fingerprint(doc["manifest_sha256"])
    return {**doc, "observed_at": timestamp(utc(doc["observed_at"]))}


def _aid(a) -> str:
    return digest([a[k] for k in ("source_id", "run_id", "source_object_id", "capture_version")])


def _cid(a) -> str:
    return digest([a[k] for k in ("source_id", "source_object_id", "capture_version")])


def _inside(a, w) -> bool:
    t = utc(a["observed_at"])
    return (a["source_id"] in w["sources"] and utc(w["start"]) <= t
            and (t <= utc(w["end"]) if w["kind"] == "G1_ORIGIN" else t < utc(w["end"])))


def _state(windows, attestations, memberships) -> dict:
    captures = {}
    for a in attestations.values():
        cid = _cid(a)
        c = captures.setdefault(cid, {
            **{k: a[k] for k in ("source_id", "source_object_id", "capture_version")},
            "known_at": a["observed_at"], "last_observed_at": a["observed_at"], "observation_count": 0,
        })
        c["known_at"] = min(c["known_at"], a["observed_at"])
        c["last_observed_at"] = max(c["last_observed_at"], a["observed_at"])
        c["observation_count"] += 1
    spans = []
    for previous, current in pairwise(windows):
        start, end = previous["end"], current["start"]
        relation = "GAP" if start < end else "ADJACENT" if start == end else "ORIGIN_OVERLAP"
        for sid in sorted(set(previous["sources"]) | set(current["sources"])):
            spans.append({"source_id": sid, "previous_window_id": previous["window_id"],
                          "next_window_id": current["window_id"], "start": min(start, end), "end": max(start, end),
                          "relation": relation, "status": "NOT_ASSESSED", "assessment_known_at": None})
    doc = {"schema": "g2-input-registry-v1", "windows": windows,
           "attestations": dict(sorted(attestations.items())), "captures": dict(sorted(captures.items())),
           "memberships": sorted(memberships, key=lambda m: (m["window_id"], m["attestation_id"])),
           "inter_window_spans": spans}
    return {**doc, "semantic_digest": digest(doc)}


def empty_registry() -> dict:
    return _state([], {}, [])


def admit(previous: dict, window: dict, observations: list[dict]) -> dict:
    """Plan admission without mutating the accepted state, including on failure."""
    state = json.loads(json.dumps(previous))
    if state != _state(state["windows"], state["attestations"], state["memberships"]):
        raise AuthorityError("accepted registry integrity failure")
    w = window_record(**{k: window[k] for k in ("kind", "profile_sha256", "bundle_sha256", "start", "end", "sources")})
    if window != w:
        raise AuthorityError("window identity mismatch")
    accepted = state["windows"]
    replay = w in accepted
    if not replay:
        if not accepted and w["kind"] != "G1_ORIGIN":
            raise AuthorityError("pinned G1 origin must be admitted first")
        if accepted and w["kind"] != "G2_PROSPECTIVE":
            raise AuthorityError("G1 origin cannot be replaced")
        if accepted and utc(w["start"]) < utc(accepted[0]["start"]):
            raise AuthorityError("backfill before the G1 origin is outside G2")
        if accepted and accepted[-1]["kind"] == "G2_PROSPECTIVE" and utc(w["start"]) < utc(accepted[-1]["end"]):
            raise AuthorityError("prospective windows cannot overlap or reorder")
    incoming = {_aid(a): a for a in map(_attestation, observations)}
    if len(incoming) != len(observations):
        raise AuthorityError("admission expects deduplicated attestation identities")
    runs = {(a["source_id"], a["run_id"]): (a["manifest_sha256"], a["observed_at"])
            for a in state["attestations"].values()}
    for aid, a in incoming.items():
        if not _inside(a, w):
            raise AuthorityError("attestation outside declared window/source")
        run_key = (a["source_id"], a["run_id"])
        authority = (a["manifest_sha256"], a["observed_at"])
        if run_key in runs and runs[run_key] != authority:
            raise AuthorityError("conflicting accepted attestation source/run authority")
        runs[run_key] = authority
        known = state["attestations"].get(aid)
        if known is not None:
            if known != a:
                raise AuthorityError("conflicting accepted attestation")
        else:
            capture = state["captures"].get(_cid(a))
            if replay or any(_inside(a, old) for old in accepted) or (
                    capture and utc(a["observed_at"]) < utc(capture["known_at"])):
                raise AuthorityError("new older attestation requires a versioned correction proposal")
            state["attestations"][aid] = a
    links = [{"window_id": w["window_id"], "attestation_id": aid} for aid in sorted(incoming)]
    if replay:
        existing = [m for m in state["memberships"] if m["window_id"] == w["window_id"]]
        if links != existing:
            raise AuthorityError("sealed window membership cannot change")
    else:
        state["windows"].append(w)
        state["memberships"].extend(links)
    return _state(state["windows"], state["attestations"], state["memberships"])
