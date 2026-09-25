"""Cross-source descriptive metrics for G0-E1 (es-corporate-bonds).

Metric definitions are fixed here — they are part of the gate contract, not
tunable knobs:

- **Scope** — the VALIDATED INCLUDE subset of the frozen 15+5 sample
  (``universe.sample_validation.load_validated_scope``), 16 ISINs. The 4
  EXCLUDE stay frozen for audit and are never profile scope.
- **Unique trade** — a G0-D ledger chain: records grouped by
  (source, transaction_identification_code); records WITHOUT a transaction id
  are UNCHAINABLE singletons — no identity is ever inferred for them (fail
  closed, NO_HEURISTIC_DELETION). Rescans of the same publication collapse
  into the same chain.
- **Chain head** — the latest observation of a chain by publication time;
  the trade's latest published state.
- **Cross-source trade identity** — the economic key
  (isin, quantity, price, trading_utc) of chain heads. A key shared by heads
  of >1 source is a cross-source CANDIDATE match: flagged, never merged or
  deleted (same semantics as ``lifecycle.duplicates``).
- **Notional** — sums over chain heads per currency, split between
  terminal-CANCELLED heads and the rest. A cancelled trade is reported, not
  silently included as volume nor silently dropped.
- **Sample observation rate** — |observed validated ISINs| / 16 over the
  capture evidence. A low rate is a VALID result (preregistration, issue
  #14): it never triggers re-scoping.

NO_COVERAGE_CLAIM is enforced: the report carries an explicit
``interpretation_limits`` section and ``scan_forbidden_claims`` rejects
completeness / market-share / absence-of-trading language in the emitted
metrics. Metrics are observations over an intact capture, nothing more.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from lifecycle.ledger import build_ledger
from lifecycle.lifecycle import reconstruct_chains
from normalization.model import NormalizedRecord

# Forbidden claims scanned over the metrics payload of the emitted report
# (the interpretation_limits block itself is excluded — it necessarily names
# these in negated form). Matched case-insensitively.
FORBIDDEN_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"market\s+share",
        r"market\s+coverage",
        r"share\s+of\s+(?:the\s+)?market",
        r"(?:complete|comprehensive|full)\s+(?:market|capture|coverage)",
        r"market\s+completeness",
        r"cover(?:s|ed|ing)?\s+(?:the\s+)?(?:whole|entire|full)\s+market",
        r"did\s+not\s+trade",
        r"(?:were|was|are|is)\s+not\s+traded",
        r"no\s+trading\s+(?:took\s+place|occurred|activity)",
        r"absence\s+of\s+trading",
        r"(?:\d+(?:\.\d+)?\s*%|percent|pct)\s+of\s+(?:the\s+)?market",
        r"represent(?:s|ative|ing)?\s+(?:of\s+)?(?:the\s+)?market",
    )
)

# Mandatory interpretation limits (issue #14 Done-when). Emitted verbatim in
# every report; a report without them is not a G0-E artifact.
INTERPRETATION_LIMITS: dict[str, str] = {
    "no_completeness_inference": (
        "Metrics describe observations inside one intact capture only; "
        "nothing here estimates or bounds market completeness."
    ),
    "no_market_share_inference": (
        "Source-exclusive sets and intersections are relations between two "
        "observed sets of publications; they are not market share and do "
        "not support share-like reasoning."
    ),
    "no_absence_of_trading_inference": (
        "An unobserved sample ISIN means 'not observed in an intact "
        "capture'; it never means the instrument did not trade."
    ),
    "no_low_n_generalization": (
        "Low-count source-exclusive observations describe this sample "
        "only and must not be generalized."
    ),
}


def scan_forbidden_claims(text: str) -> list[str]:
    """Return every forbidden-claim pattern found in ``text`` (empty = clean)."""
    return [p.pattern for p in FORBIDDEN_CLAIM_PATTERNS if p.search(text)]


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _econ_key(rec: NormalizedRecord) -> tuple:
    """Cross-source trade identity: same definition as G0-D economic
    duplicate candidates (instrument + quantity + price + trading time)."""
    return (
        rec.instrument_identification_code,
        rec.quantity,
        rec.price,
        rec.trading_datetime.canonical_utc,
    )


def _group_trades(records: list[NormalizedRecord]) -> list[list[NormalizedRecord]]:
    """Group records into per-trade chains mirroring the G0-D ledger rule:
    keyed (source, transaction id); missing/blank ids are UNCHAINABLE
    singletons — never grouped, never merged. Each chain is ordered by
    publication time; the head (latest observation) is chain[-1].
    """
    groups: dict[tuple, list[NormalizedRecord]] = {}
    for i, rec in enumerate(records):
        tid = (rec.transaction_identification_code or "").strip()
        key = (rec.source, tid) if tid else (rec.source, None, i)
        groups.setdefault(key, []).append(rec)
    chains = list(groups.values())
    for chain in chains:
        chain.sort(key=lambda r: (
            r.publication_datetime.canonical_utc or "",
            r.publication_datetime.source_value or "",
        ))
    return chains


def _head_currency(
    head: NormalizedRecord, index: dict[tuple, set[str]]
) -> str | None:
    """Currency of a chain head, resolved by exact-field join against the
    records that produced the chain (rescans of identical publications share
    the join key; a multi-currency collision is surfaced as ambiguity)."""
    key = (
        head.source,
        (head.transaction_identification_code or "").strip(),
        head.instrument_identification_code,
        head.publication_datetime.canonical_utc,
        head.trading_datetime.canonical_utc,
        head.price,
        head.quantity,
        head.notional_amount,
    )
    ccy = index.get(key, set())
    if len(ccy) == 1:
        return next(iter(ccy))
    return None  # absent or ambiguous — reported, never guessed


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_cross_source(
    records: Iterable[NormalizedRecord],
    validated_scope: frozenset[str],
    *,
    sources: tuple[str, ...] = ("bme_apa", "blb_apae"),
) -> dict[str, Any]:
    """Compute the G0-E1 metric set over ``records`` restricted to
    ``validated_scope``. Deterministic: identical input -> identical output.
    All counts/sums are metadata; no provider payload is emitted.
    """
    scoped = [r for r in records
              if (r.instrument_identification_code or "") in validated_scope]
    scoped.sort(key=lambda r: (
        r.source, r.instrument_identification_code,
        r.publication_datetime.canonical_utc or "",
        r.publication_datetime.source_value or "",
    ))

    # -- record-level context -------------------------------------------------
    records_by_source = dict.fromkeys(sources, 0)
    pub_range: dict[str, dict[str, str | None]] = {}
    for r in scoped:
        records_by_source[r.source] = records_by_source.get(r.source, 0) + 1
        ts = r.publication_datetime.canonical_utc
        slot = pub_range.setdefault(r.source, {"min": None, "max": None})
        if ts is None:
            continue
        slot["min"] = ts if slot["min"] is None or ts < slot["min"] else slot["min"]
        slot["max"] = ts if slot["max"] is None or ts > slot["max"] else slot["max"]

    # -- chains over the G0-D ledger (authoritative lifecycle semantics) ------
    entries = build_ledger(scoped)
    ledger_chains = reconstruct_chains(entries)
    head_state_by_source: dict[str, Counter] = defaultdict(Counter)
    ledger_chain_counts = dict.fromkeys(sources, 0)
    for key, chain in ledger_chains.items():
        source = key.split("|", 1)[0]
        head = chain[-1]
        ledger_chain_counts[source] = ledger_chain_counts.get(source, 0) + 1
        head_state_by_source[source][head.state] += 1

    # -- trade grouping over records (carries notional_currency) --------------
    rec_chains = _group_trades(scoped)
    heads = [c[-1] for c in rec_chains]
    heads_by_source: dict[str, list[NormalizedRecord]] = defaultdict(list)
    for h in heads:
        heads_by_source[h.source].append(h)

    unchainable_by_source = dict.fromkeys(sources, 0)
    for c in rec_chains:
        tid = (c[-1].transaction_identification_code or "").strip()
        if not tid:
            unchainable_by_source[c[-1].source] += 1

    # currency join index: exact-field identity of each record -> currencies
    ccy_index: dict[tuple, set[str]] = defaultdict(set)
    for r in scoped:
        ccy_index[(
            r.source,
            (r.transaction_identification_code or "").strip(),
            r.instrument_identification_code,
            r.publication_datetime.canonical_utc,
            r.trading_datetime.canonical_utc,
            r.price,
            r.quantity,
            r.notional_amount,
        )].add(r.notional_currency or "")

    # -- ISIN-level sets --------------------------------------------------------
    observed_isins_by_source: dict[str, set[str]] = {
        s: set() for s in sources}
    for h in heads:
        observed_isins_by_source[h.source].add(
            h.instrument_identification_code)
    observed_union = set().union(*observed_isins_by_source.values())
    isin_intersection = (
        set.intersection(*observed_isins_by_source.values())
        if observed_isins_by_source else set()
    )
    unobserved = set(validated_scope) - observed_union

    # -- trade-level cross-source partition over chain heads --------------------
    econ_groups: dict[tuple, list[NormalizedRecord]] = defaultdict(list)
    for h in heads:
        econ_groups[_econ_key(h)].append(h)

    cross_groups: list[dict[str, Any]] = []
    exclusive_trades: dict[str, int] = dict.fromkeys(sources, 0)
    matched_trades: dict[str, int] = dict.fromkeys(sources, 0)
    for key, members in econ_groups.items():
        member_sources = sorted({m.source for m in members})
        if len(member_sources) > 1:
            per_source = Counter(m.source for m in members)
            for s in member_sources:
                matched_trades[s] += per_source[s]
            cross_groups.append({
                "isin": key[0],
                "sources": member_sources,
                "heads_per_source": dict(sorted(per_source.items())),
                # audit handle without emitting trade economics
                "key_sha256": _sha(repr(key)),
            })
        else:
            exclusive_trades[member_sources[0]] += len(members)
    cross_groups.sort(key=lambda g: (g["isin"], g["key_sha256"]))

    # -- notional over chain heads ------------------------------------------------
    notional: dict[str, Any] = {}
    for s in sources:
        sums: dict[str, Decimal] = defaultdict(Decimal)
        sums_excl_cancelled: dict[str, Decimal] = defaultdict(Decimal)
        cancelled_heads = 0
        unparseable = 0
        no_currency = 0
        for h in heads_by_source.get(s, []):
            n = _dec(h.notional_amount)
            if n is None:
                unparseable += 1
                continue
            ccy = _head_currency(h, ccy_index)
            if ccy is None:
                no_currency += 1
                ccy = ""
            sums[ccy] += n
            # Terminal CANCELLED head == head publication the source marks
            # as a cancellation (and not simultaneously an amendment —
            # that combination is UNRESOLVED in G0-D, never CANCELLED).
            if h.cancellation is True and h.amendment is not True:
                cancelled_heads += 1
            else:
                sums_excl_cancelled[ccy] += n
        notional[s] = {
            "chains_with_parseable_notional": sum(
                1 for h in heads_by_source.get(s, [])
                if _dec(h.notional_amount) is not None),
            "unparseable_notional_heads": unparseable,
            "heads_with_ambiguous_or_missing_currency": no_currency,
            "sum_by_currency": {
                c: str(v) for c, v in sorted(sums.items())},
            "cancelled_heads": cancelled_heads,
            "sum_by_currency_excluding_cancelled_heads": {
                c: str(v) for c, v in sorted(sums_excl_cancelled.items())},
        }

    observed = len(observed_union)
    scope_n = len(validated_scope)
    return {
        "scope": {
            "validated_profile_isins": scope_n,
            "validated_isins": sorted(validated_scope),
        },
        "records_by_source": records_by_source,
        "observed_publication_utc_range_by_source": pub_range,
        "unique_trades": {
            "by_source": {s: len(heads_by_source.get(s, []))
                          for s in sources},
            "definition": (
                "G0-D ledger chain: (source, transaction_id); records "
                "without a transaction id are UNCHAINABLE singletons. "
                "Rescans of the same publication collapse into one chain."),
            "unchainable_singletons_by_source": unchainable_by_source,
            "ledger_chain_count_by_source": ledger_chain_counts,
            "ledger_consistent": all(
                len(heads_by_source.get(s, []))
                == ledger_chain_counts.get(s, 0) for s in sources),
            "head_state_distribution_by_source": {
                s: dict(sorted(head_state_by_source.get(s, {}).items()))
                for s in sources},
        },
        "unique_isins": {
            "by_source": {s: sorted(observed_isins_by_source.get(s, set()))
                          for s in sources},
            "intersection": sorted(isin_intersection),
            "source_exclusive": {
                s: sorted(observed_isins_by_source.get(s, set())
                          - set().union(*(
                              observed_isins_by_source.get(t, set())
                              for t in sources if t != s)))
                for s in sources},
            "unobserved_in_capture": sorted(unobserved),
        },
        "trades": {
            "intersection_candidates": {
                "definition": (
                    "chain heads sharing the economic key (isin, quantity, "
                    "price, trading_utc) across >1 source — candidate "
                    "matches, never merged"),
                "group_count": len(cross_groups),
                "groups": cross_groups,
                "heads_matched_by_source": matched_trades,
            },
            "source_exclusive": {
                "definition": (
                    "chain heads whose economic key appears in no other "
                    "source — observed-set difference, not market "
                    "exclusivity"),
                "by_source": exclusive_trades,
            },
        },
        "notional_by_source": notional,
        "sample_observation_rate": {
            "observed_isins": observed,
            "validated_isins": scope_n,
            "rate": str(Decimal(observed) / Decimal(scope_n))
            if scope_n else "0",
            "semantics": (
                "fraction of the validated 16-ISIN scope with >=1 observed "
                "trade in this capture; 'unobserved' means not observed in "
                "an intact capture"),
        },
        "interpretation_limits": INTERPRETATION_LIMITS,
        "no_coverage_claim": True,
    }
