"""G1-C2 replay substrate — rebuild a G0-D-consumable record from the tape.

The schema-v2 publications table stores ``normalized_evidence_json``: the
canonical JSON of ``NormalizedRecord.to_evidence()`` at materialization
time (metadata only — no raw provider fields). ``record_from_evidence``
rebuilds a :class:`NormalizedRecord` exposing exactly the attributes the
G0-D ledger machinery consumes, so ``build_ledger``/``reconstruct_chains``
replay byte-identical lifecycle semantics from the tape alone.

Fail closed: any structural deviation (missing/extra evidence keys,
malformed timestamps, wrong tri-state values, wrong field count, or a
reconstructed ``to_evidence()`` that does not reproduce the stored
evidence exactly) raises :class:`ReplayError`. Never guessed, never
coerced.
"""

from __future__ import annotations

import json
from typing import Any

from .model import NormalizedRecord, SanityResult, TimestampValue

# The complete key set produced by NormalizedRecord.to_evidence(). Any
# divergence is a schema violation — fail closed.
_EVIDENCE_KEYS = frozenset({
    "source",
    "instrument_identification_code",
    "instrument_id_code_type",
    "venue_of_execution",
    "venue_of_publication",
    "price_currency",
    "price_notation",
    "notation_of_quantity_measurement_unit",
    "price",
    "quantity",
    "quantity_in_measurement_unit",
    "notional_amount",
    "notional_currency",
    "transaction_identification_code",
    "trading_datetime",
    "publication_datetime",
    "flags",
    "deferral",
    "partial_publication",
    "cancellation",
    "amendment",
    "source_report_id",
    "raw_field_count",
    "rts2_mapping",
    "sanity",
})

_TS_KEYS = frozenset({"source_value", "canonical_utc", "parse_status"})
_SANITY_KEYS = frozenset({"check", "status", "detail"})



class ReplayError(Exception):
    """Fail-closed abort: stored normalized evidence cannot be replayed
    exactly (malformed, incomplete, or not self-consistent)."""


class _RawFieldsUnavailable:
    """Length-only stand-in for provider raw fields withheld from the tape.

    The tape stores ``raw_field_count`` metadata, never raw payload
    content. G0-D consumes only ``len(rec.raw_fields)``; this object
    supports exactly that and nothing else.
    """

    def __init__(self, count: int):
        self.count = count

    def __len__(self) -> int:
        return self.count

    def __repr__(self) -> str:
        return f"<raw_fields withheld: {self.count} fields>"


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _ts(d: Any, field: str) -> TimestampValue:
    if not isinstance(d, dict) or set(d) != _TS_KEYS:
        raise ReplayError(f"{field}: malformed timestamp evidence {d!r}")
    if not isinstance(d["source_value"], str) \
            or (d["canonical_utc"] is not None
                and not isinstance(d["canonical_utc"], str)) \
            or not isinstance(d["parse_status"], str):
        raise ReplayError(f"{field}: malformed timestamp value types {d!r}")
    return TimestampValue(d["source_value"], d["canonical_utc"],
                          d["parse_status"])


def record_from_evidence(evidence: dict[str, Any]) -> NormalizedRecord:
    """Rebuild a NormalizedRecord from its stored ``to_evidence()`` dict.

    The returned record feeds ``lifecycle.ledger.build_ledger`` directly:
    every attribute G0-D reads is restored from the evidence, and the
    reconstructed ``to_evidence()`` MUST reproduce the stored evidence
    byte-identically under canonical JSON — otherwise the ledger's
    ``_rec_sort_key`` (and hence entry ordering/occurrence ids) could
    diverge, which is refused.
    """
    if not isinstance(evidence, dict):
        raise ReplayError(f"normalized evidence is not a mapping: "
                          f"{type(evidence).__name__}")
    if set(evidence) != _EVIDENCE_KEYS:
        raise ReplayError(
            "normalized evidence key set diverges from "
            "NormalizedRecord.to_evidence(): "
            f"missing={sorted(_EVIDENCE_KEYS - set(evidence))} "
            f"extra={sorted(set(evidence) - _EVIDENCE_KEYS)}")

    for name in ("deferral", "partial_publication",
                 "cancellation", "amendment"):
        if evidence[name] is not None \
                and not isinstance(evidence[name], bool):
            raise ReplayError(f"{name}: not a tri-state: "
                              f"{evidence[name]!r}")
    if not isinstance(evidence["raw_field_count"], int) \
            or isinstance(evidence["raw_field_count"], bool) \
            or evidence["raw_field_count"] < 0:
        raise ReplayError("raw_field_count must be a non-negative int")
    if not isinstance(evidence["flags"], list) \
            or not all(isinstance(f, str) for f in evidence["flags"]):
        raise ReplayError("flags must be a list of strings")
    if not isinstance(evidence["rts2_mapping"], dict) \
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in evidence["rts2_mapping"].items()):
        raise ReplayError("rts2_mapping must be a str->str mapping")
    if not isinstance(evidence["sanity"], list) \
            or not all(isinstance(s, dict) and set(s) == _SANITY_KEYS
                       and all(isinstance(v, str) for v in s.values())
                       for s in evidence["sanity"]):
        raise ReplayError("sanity must be a list of "
                          "{check,status,detail} string mappings")

    rec = NormalizedRecord(
        source=evidence["source"],
        instrument_identification_code=
        evidence["instrument_identification_code"],
        instrument_id_code_type=evidence["instrument_id_code_type"],
        venue_of_execution=evidence["venue_of_execution"],
        venue_of_publication=evidence["venue_of_publication"],
        price_currency=evidence["price_currency"],
        price_notation=evidence["price_notation"],
        notation_of_quantity_measurement_unit=
        evidence["notation_of_quantity_measurement_unit"],
        price=evidence["price"],
        quantity=evidence["quantity"],
        quantity_in_measurement_unit=
        evidence["quantity_in_measurement_unit"],
        notional_amount=evidence["notional_amount"],
        notional_currency=evidence["notional_currency"],
        transaction_identification_code=
        evidence["transaction_identification_code"],
        trading_datetime=_ts(evidence["trading_datetime"],
                             "trading_datetime"),
        publication_datetime=_ts(evidence["publication_datetime"],
                                 "publication_datetime"),
        flags=list(evidence["flags"]),
        deferral=evidence["deferral"],
        partial_publication=evidence["partial_publication"],
        cancellation=evidence["cancellation"],
        amendment=evidence["amendment"],
        source_report_id=evidence["source_report_id"],
        raw_fields=_RawFieldsUnavailable(evidence["raw_field_count"]),
        rts2_mapping=dict(evidence["rts2_mapping"]),
        sanity=[SanityResult(s["check"], s["status"], s["detail"])
                for s in evidence["sanity"]],
    )

    # Exact-replay guard: the reconstructed evidence MUST equal the stored
    # evidence under canonical JSON (catches non-JSON-native values,
    # lost precision, or adapter drift before they can alter G0-D keys).
    try:
        replayed = _canonical(rec.to_evidence())
        stored = _canonical(evidence)
    except TypeError as exc:
        raise ReplayError(
            f"normalized evidence is not JSON-canonical: {exc}") from exc
    if replayed != stored:
        raise ReplayError(
            "reconstructed to_evidence() does not reproduce the stored "
            "normalized evidence — refusing inexact replay")
    return rec
