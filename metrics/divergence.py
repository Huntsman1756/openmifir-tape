"""G1-D metadata ledger; frozen G1 sections 4.2/5, G0-D authoritative.

Membership is instrument-level observed-source membership at T. Economic
relations use exactly the G0-E head key, never trade identity or merging.
Classes compare candidate chains only; unpaired chains retain membership
without guessing a counterpart. Raw economic values never enter this ledger.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from lifecycle.ledger import build_ledger_indexed
from lifecycle.lifecycle import reconstruct_chains
from normalization.replay import ReplayError, record_from_evidence

from .cross_source import _econ_key
from .g1c1 import pub_sort_key
from .g1c2 import _parse_at

SOURCES = ("bme_apa", "blb_apae")


class DivergenceError(ValueError):
    """Invalid or unbound publication evidence; abort rather than guess."""


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass
class Observation:
    at: str
    publications: list[dict]
    records: list[Any]
    chains: dict
    publication_by_entry: dict


def reconstruct(publications: list[dict], at: str) -> Observation:
    """Filter knowledge time first, then replay INCLUDE through G0-D.

    Never use materialized heads, lifecycle fields or last_observed_at.
    Validate knowledge times even outside INCLUDE; invalid evidence cannot
    silently evade the temporal filter. References remain BUILD_SNAPSHOT.
    """
    t = _parse_at(at)
    pubs = []
    for row in sorted(publications, key=pub_sort_key):
        known = _parse_at(row.get("known_at"))
        if known <= t and row["universe_disposition"] == "INCLUDE":
            pubs.append(row)
    if len({pub_sort_key(r) for r in pubs}) != len(pubs):
        raise DivergenceError("duplicate publication capture identity/ordinal")
    records = []
    for row in pubs:
        try:
            rec = record_from_evidence(json.loads(row["normalized_evidence_json"]))
        except (KeyError, json.JSONDecodeError, ReplayError) as exc:
            raise DivergenceError("invalid normalized replay substrate") from exc
        if (rec.source != row["source"] or rec.source not in SOURCES
                or rec.instrument_identification_code != row["isin"]):
            raise DivergenceError("publication identity differs from replay substrate")
        records.append(rec)
    indexed = build_ledger_indexed(records)
    by_entry = {entry.entry_id: pubs[pos] for pos, entry in indexed}
    chains = reconstruct_chains([entry for _, entry in indexed])
    if len(by_entry) != len(pubs) or sum(map(len, chains.values())) != len(pubs):
        raise DivergenceError("non-bijective publication/ledger reconstruction")
    return Observation(t, pubs, records, chains, by_entry)


def build_divergence(observation: Observation) -> dict:
    """One metadata row per chain; relation flags independent of membership.

    Candidate grouping is conservative: an incomplete economic head key
    remains unmatched. Every candidate is retained, including ambiguous
    many-to-many groups; none implies an identified cross-source trade.
    """
    chains = observation.chains
    isin_sources = defaultdict(set)
    economic = defaultdict(list)
    for cid, chain in chains.items():
        head = chain[-1]
        isin_sources[head.instrument_identification_code].add(head.source)
        key = _econ_key(head)
        if all(value not in (None, "") for value in key):
            economic[key].append(cid)
    candidates = {}
    for key, cids in economic.items():
        if len({chains[cid][-1].source for cid in cids}) > 1:
            candidates[digest(key)] = sorted(cids)
    groups_by_chain = defaultdict(list)
    for group_id, cids in candidates.items():
        for cid in cids:
            groups_by_chain[cid].append(group_id)
    rows = []
    for cid, chain in sorted(chains.items()):
        head = chain[-1]
        source_set = isin_sources[head.instrument_identification_code]
        membership = ("OBSERVED_BOTH" if len(source_set) == 2 else
                      "OBSERVED_BME_ONLY" if head.source == SOURCES[0] else
                      "OBSERVED_BLOOMBERG_ONLY")
        comparisons = []
        for group_id in sorted(groups_by_chain[cid]):
            for peer_id in candidates[group_id]:
                peer = chains[peer_id]
                if peer[-1].source == head.source:
                    continue
                classes = []
                first = chain[0].publication_utc
                other_first = peer[0].publication_utc
                if first and other_first and first != other_first:
                    classes.append("PUBLICATION_LAG")
                if head.state != peer[-1].state:
                    classes.append("STATE_DIVERGENCE")
                for state, label in (("AMENDED", "AMENDMENT_DIVERGENCE"),
                                     ("CANCELLED", "CANCELLATION_DIVERGENCE")):
                    if any(e.state == state for e in chain) != any(e.state == state for e in peer):
                        classes.append(label)
                comparisons.append({"candidate_group_id": group_id,
                                    "peer_chain_id": peer_id,
                                    "classes": classes})
        members = []
        for entry in chain:
            pub = observation.publication_by_entry[entry.entry_id]
            members.append({
                "entry_id": entry.entry_id, "state": entry.state,
                "supersedes_entry_id": entry.supersedes_entry_id,
                "known_at": _parse_at(pub["known_at"]),
                "publication_utc": entry.publication_utc,
                "source_object_id": pub["source_object_id"],
                "capture_version": pub["capture_version"],
                "raw_sha256": pub["raw_sha256"],
            })
        rows.append({
            "chain_id": cid, "isin": head.instrument_identification_code,
            "source": head.source, "known_at": observation.at,
            "membership": membership,
            "relation_flags": ["CROSS_SOURCE_CANDIDATE"] if comparisons else [],
            "candidate_group_ids": sorted(groups_by_chain[cid]),
            "head_entry_id": head.entry_id, "head_state": head.state,
            "not_observed_in_capture": [
                f"NOT_OBSERVED_IN_{label}_CAPTURE"
                for s, label in zip(SOURCES, ("BME", "BLOOMBERG"), strict=True)
                if s not in source_set],
            "comparisons": comparisons, "members": members,
        })
    result = {"known_at": observation.at, "reference_basis": "BUILD_SNAPSHOT",
              "membership_basis": "observed INCLUDE instrument source set at known_at",
              "candidate_basis": "G0-E economic head key; candidates never merged",
              "rows": rows,
              "candidate_groups": [{"candidate_group_id": key, "chain_ids": cids}
                                   for key, cids in sorted(candidates.items())]}
    return {**result, "semantic_digest": digest(result)}
