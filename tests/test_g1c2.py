"""Offline tests for G1-C2 as_of(T) point-in-time reconstruction.

Synthetic store/manifest fixtures only — no provider data, no network.
Covers the frozen rule

    as_of(T) = publications WHERE known_at <= T
               -> build_ledger -> reconstruct_chains -> chain[-1]

including the adversarial timelines required by issue #26: future
evidence must not leak, historical entry ids are REBUILT from the
T-limited subset (never the full-tape ids), ``head_known_at`` is the
known_at of chain[-1] (not max known_at), the replay substrate works
from the tape alone, and invalid T fails closed.
"""

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from test_g1c1 import (
    _dispositions,
    _g1a_artifact,
    _g1b_report,
    _g1b_rows,
)
from test_metrics import BME, WINDOW, _bme, _fixture, _manifest, _store

from metrics.g1c1 import (
    CHAIN_COLUMNS,
    CHAIN_DUCK_TYPES,
    PUB_COLUMNS,
    PUB_DUCK_TYPES,
    build_chains,
    build_publications,
    pub_sort_key,
    semantic_digest,
    write_parquet,
)
from metrics.g1c2 import (
    ASOF_COLUMNS,
    C1_REPORT_LOGICAL_ID,
    EXPECTED_C1_REPORT_SHA256,
    REFERENCE_BASIS,
    G1C2Error,
    as_of,
    asof_semantic_digest,
    build_report,
    chains_projection_digest,
)
from metrics.selection import select_eligible
from normalization import normalize_record
from normalization.replay import ReplayError, record_from_evidence

T1 = "2026-09-17T10:00:00Z"
T2 = "2026-09-17T12:00:00Z"
T3 = "2026-09-17T14:00:00Z"
T4 = "2026-09-17T16:00:00Z"


def _tape_for(tmp_path, populate, isins):
    """Populate a synthetic store, materialize tape Parquet files, and
    return (tape_dir, pub_rows, chain_rows)."""
    raw, runs = _fixture(tmp_path)
    populate(raw, runs)
    sel = select_eligible(
        runs, raw,
        start=datetime.fromisoformat(WINDOW["operational_start"]),
        tail_end=datetime.fromisoformat(WINDOW["evidence_tail_end"]))
    pub_rows, indexed, unparseable = build_publications(
        sel, _dispositions(*isins))
    assert not unparseable
    chain_rows = build_chains(indexed, pub_rows)
    tape = tmp_path / "tape"
    write_parquet(pub_rows, PUB_COLUMNS, PUB_DUCK_TYPES,
                  tape / "publications.parquet", pub_sort_key)
    write_parquet(chain_rows, CHAIN_COLUMNS, CHAIN_DUCK_TYPES,
                  tape / "trade_chains.parquet",
                  lambda r: r["chain_id"])
    return tape, pub_rows, chain_rows


def _three_stage(tmp_path, cancel_pub="2026-09-17T12:00:05.000Z"):
    """orig known T1, amendment known T2, cancellation known T3."""
    def populate(raw, runs):
        o1 = _store(raw, BME, "2026-09-17", "d1.json", [_bme()])
        o2 = _store(raw, BME, "2026-09-17", "d2.json", [_bme(
            publication_date_and_time="2026-09-17T11:00:05.000Z",
            amnd="true", flags="AMND", price="98.40")])
        o3 = _store(raw, BME, "2026-09-17", "d3.json", [_bme(
            publication_date_and_time=cancel_pub,
            canc="true", flags="CANC")])
        _manifest(runs, "20260917T100000Z", BME, T1, [o1])
        _manifest(runs, "20260917T120000Z", BME, T2, [o2])
        _manifest(runs, "20260917T140000Z", BME, T3, [o3])
    return _tape_for(tmp_path, populate, ["ISIN-A"])


# ---------- 1. original / amendment / cancellation timeline ----------

def test_original_amend_cancel_heads_at_t1_t2_t3(tmp_path):
    tape, _pubs, _chains = _three_stage(tmp_path)
    r1 = as_of(tape, T1)
    assert r1["row_count"] == 1
    assert r1["rows"][0]["head_state"] == "FULLY_PUBLISHED"
    assert r1["rows"][0]["event_count"] == 1
    r2 = as_of(tape, T2)
    assert r2["rows"][0]["head_state"] == "AMENDED"
    assert r2["rows"][0]["event_count"] == 2
    r3 = as_of(tape, T3)
    assert r3["rows"][0]["head_state"] == "CANCELLED"
    assert r3["rows"][0]["event_count"] == 3
    # three distinct authoritative heads — ids rebuilt per subset
    ids = {r1["rows"][0]["head_entry_id"],
           r2["rows"][0]["head_entry_id"],
           r3["rows"][0]["head_entry_id"]}
    assert len(ids) == 3


# ---------- 2. future evidence never leaks ----------

def test_future_cancellation_never_leaks_before_its_known_at(tmp_path):
    """The cancellation's publication_utc sits BETWEEN the original and
    the amendment — adversarially earlier than the moment it leaks —
    yet only ``known_at`` gates visibility."""
    tape, _p, _c = _three_stage(
        tmp_path, cancel_pub="2026-09-17T10:30:05.000Z")
    r = as_of(tape, T2)
    assert r["row_count"] == 1
    row = r["rows"][0]
    assert row["head_state"] == "AMENDED"
    assert row["event_count"] == 2      # the T3-known cancel is absent


def test_future_evidence_does_not_alter_historical_result(tmp_path):
    """as_of(T2) over the full tape == as_of(T2) over a tape where the
    T3 cancellation was never captured: identical rows AND digest."""
    def full(raw, runs):
        o1 = _store(raw, BME, "2026-09-17", "d1.json", [_bme()])
        o2 = _store(raw, BME, "2026-09-17", "d2.json", [_bme(
            publication_date_and_time="2026-09-17T11:00:05.000Z",
            amnd="true", flags="AMND", price="98.40")])
        o3 = _store(raw, BME, "2026-09-17", "d3.json", [_bme(
            publication_date_and_time="2026-09-17T12:00:05.000Z",
            canc="true", flags="CANC")])
        _manifest(runs, "20260917T100000Z", BME, T1, [o1])
        _manifest(runs, "20260917T120000Z", BME, T2, [o2])
        _manifest(runs, "20260917T140000Z", BME, T3, [o3])

    def partial(raw, runs):
        o1 = _store(raw, BME, "2026-09-17", "d1.json", [_bme()])
        o2 = _store(raw, BME, "2026-09-17", "d2.json", [_bme(
            publication_date_and_time="2026-09-17T11:00:05.000Z",
            amnd="true", flags="AMND", price="98.40")])
        _manifest(runs, "20260917T100000Z", BME, T1, [o1])
        _manifest(runs, "20260917T120000Z", BME, T2, [o2])

    (tmp_path / "full").mkdir()
    tape_full, _p, _c = _tape_for(tmp_path / "full", full, ["ISIN-A"])
    (tmp_path / "part").mkdir()
    tape_part, _p2, _c2 = _tape_for(tmp_path / "part", partial,
                                    ["ISIN-A"])
    a = as_of(tape_full, T2)
    b = as_of(tape_part, T2)
    assert a["rows"] == b["rows"]
    assert a["semantic_digest"] == b["semantic_digest"]


# ---------- 3. head follows G0-D ordering, not max known_at ----------

def test_head_follows_publication_order_not_max_known_at(tmp_path):
    """Amendment captured first (T1), original captured later (T2):
    the head is still the amendment (later publication_utc) and
    head_known_at is the amendment's T1 — never max(known_at)."""
    def populate(raw, runs):
        amend = _store(raw, BME, "2026-09-17", "a.json", [_bme(
            amnd="true", flags="AMND", price="98.40",
            publication_date_and_time="2026-09-17T11:00:05.000Z")])
        orig = _store(raw, BME, "2026-09-17", "o.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, T1, [amend])
        _manifest(runs, "20260917T120000Z", BME, T2, [orig])
    tape, _p, _c = _tape_for(tmp_path, populate, ["ISIN-A"])

    r1 = as_of(tape, T1)
    assert r1["row_count"] == 1
    assert r1["rows"][0]["head_state"] == "AMENDED"
    assert r1["rows"][0]["event_count"] == 1
    assert r1["rows"][0]["head_known_at"].startswith(T1[:19])

    r2 = as_of(tape, T2)
    row = r2["rows"][0]
    assert row["event_count"] == 2
    assert row["head_state"] == "AMENDED"
    assert row["head_known_at"].startswith(T1[:19])     # NOT T2
    assert row["first_known_at"].startswith(T1[:19])
    assert row["head_known_at"] != row["as_of_utc"]


# ---------- 4. duplicate-occurrence ids are REBUILT ----------

def test_duplicate_occurrence_ids_rebuilt_from_subset(tmp_path):
    """Two identical records captured at T1 and T3 collide in the full
    tape as ``<hash>#1``/``<hash>#2``. At T2 only one exists, so the
    historical ledger assigns the bare ``<hash>`` — proof that full-tape
    occurrence suffixes are never reused as historical authority."""
    def populate(raw, runs):
        o1 = _store(raw, BME, "2026-09-17", "d1.json", [_bme()])
        o2 = _store(raw, BME, "2026-09-18", "d2.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, T1, [o1])
        _manifest(runs, "20260917T140000Z", BME, T3, [o2])
    tape, pub_rows, _c = _tape_for(tmp_path, populate, ["ISIN-A"])

    full_ids = sorted(r["ledger_entry_id"] for r in pub_rows)
    assert full_ids[0].endswith("#1") and full_ids[1].endswith("#2")
    assert full_ids[0][:-2] == full_ids[1][:-2]
    base = full_ids[0][:-2]

    r = as_of(tape, T2)
    assert r["row_count"] == 1
    head = r["rows"][0]
    assert head["head_entry_id"] == base          # rebuilt, no suffix
    assert "#" not in head["head_entry_id"]
    assert head["head_entry_id"] not in full_ids  # not a reused id
    assert head["event_count"] == 1

    r3 = as_of(tape, T3)
    assert r3["rows"][0]["event_count"] == 2
    assert r3["rows"][0]["head_entry_id"] == base + "#2"


# ---------- 5. repeated attestation does not advance known_at ----------

def test_repeated_attestation_keeps_first_known_at(tmp_path):
    def populate(raw, runs):
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, T1, [o])
        _manifest(runs, "20260917T160000Z", BME, T4, [o])  # rescan
    tape, pub_rows, _c = _tape_for(tmp_path, populate, ["ISIN-A"])
    assert len(pub_rows) == 1
    r = as_of(tape, T1)
    assert r["row_count"] == 1
    assert r["rows"][0]["first_known_at"].startswith(T1[:19])
    # before the first attestation the chain does not exist
    assert as_of(tape, "2026-09-17T09:59:59Z")["row_count"] == 0


# ---------- 6. chain entirely after T is absent ----------

def test_chain_entirely_after_T_absent(tmp_path):
    def populate(raw, runs):
        o1 = _store(raw, BME, "2026-09-17", "d1.json", [_bme()])
        o2 = _store(raw, BME, "2026-09-17", "d2.json",
                    [_bme(transaction_identification_code="TX-LATE",
                          instrument_identification_code="ISIN-B")])
        _manifest(runs, "20260917T100000Z", BME, T1, [o1])
        _manifest(runs, "20260917T140000Z", BME, T3, [o2])
    tape, _p, _c = _tape_for(tmp_path, populate, ["ISIN-A", "ISIN-B"])
    r = as_of(tape, T2)
    assert r["row_count"] == 1
    assert r["rows"][0]["isin"] == "ISIN-A"
    assert as_of(tape, T3)["row_count"] == 2


# ---------- 7. `known_at <= T` boundary is inclusive ----------

def test_t_equal_to_known_at_included(tmp_path):
    def populate(raw, runs):
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, T1, [o])
    tape, _p, _c = _tape_for(tmp_path, populate, ["ISIN-A"])
    assert as_of(tape, T1)["row_count"] == 1
    # equivalent offset form hits the same boundary
    assert as_of(tape, "2026-09-17T10:00:00+00:00")["row_count"] == 1
    assert as_of(tape, "2026-09-17T09:59:59.999999Z")["row_count"] == 0


# ---------- 8. missing/naive/unparseable T fails closed ----------

@pytest.mark.parametrize("bad", [
    None, "", "   ", "not-a-timestamp", "2026-13-99T99:99:99Z",
    "2026-09-17T10:00:00",               # naive ISO string
    "2026-09-17",                        # naive date
    datetime(2026, 9, 17, 10, 0, 0),     # naive datetime
    12345,                               # wrong type
])
def test_invalid_T_rejected_fail_closed(tmp_path, bad):
    tape, _p, _c = _tape_for(
        tmp_path,
        lambda raw, runs: None,
        [])
    with pytest.raises(G1C2Error):
        as_of(tape, bad)


# ---------- 9. BUILD_SNAPSHOT reference semantics ----------

def test_reference_basis_build_snapshot(tmp_path):
    tape, _p, _c = _three_stage(tmp_path)
    r = as_of(tape, T2)
    assert r["reference_basis"] == REFERENCE_BASIS == "BUILD_SNAPSHOT"
    row = r["rows"][0]
    assert row["reference_basis"] == "BUILD_SNAPSHOT"
    assert row["universe_disposition"] == "INCLUDE"
    assert row["universe_disposition_reason"] == "IN_ES_CRPB_UNIVERSE"
    assert row["issuer_lei"]


# ---------- 10. tape alone suffices — raw removed ----------

def test_replay_from_tape_alone_without_raw(tmp_path):
    """Once materialized, as_of answers from publications.parquet only:
    removing the raw store and run manifests changes nothing."""
    def populate(raw, runs):
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, T1, [o])
    raw = tmp_path / "raw"
    tape, _p, _c = _tape_for(tmp_path, populate, ["ISIN-A"])
    before = as_of(tape, T1)
    shutil.rmtree(raw)
    shutil.rmtree(tmp_path / "runs")
    after = as_of(tape, T1)
    assert after["rows"] == before["rows"]
    assert after["semantic_digest"] == before["semantic_digest"]


def test_asof_never_consults_trade_chains(tmp_path):
    """The materialized chain table is not an input: deleting
    trade_chains.parquet changes nothing for as_of."""
    tape, _p, _c = _three_stage(tmp_path)
    before = as_of(tape, T2)
    (tape / "trade_chains.parquet").unlink()
    after = as_of(tape, T2)
    assert after["rows"] == before["rows"]
    assert after["semantic_digest"] == before["semantic_digest"]


# ---------- replay fidelity / determinism ----------

def test_asof_at_max_known_at_reproduces_materialized_chains(tmp_path):
    """as_of(max known_at) selects every publication; the rebuilt chain
    projection MUST equal the materialized trade_chains exactly —
    ids, supersession, heads included."""
    tape, _p, chain_rows = _three_stage(tmp_path)
    r = as_of(tape, "9999-01-01T00:00:00Z")
    assert chains_projection_digest(r["rows"]) == semantic_digest(
        chain_rows, CHAIN_COLUMNS, lambda c: c["chain_id"])
    # rebuilt head ids at max == materialized head ids
    assert {row["head_entry_id"] for row in r["rows"]} == \
        {c["head_entry_id"] for c in chain_rows}


def test_asof_deterministic_and_T_bound(tmp_path):
    tape, _p, _c = _three_stage(tmp_path)
    a = as_of(tape, T2)
    b = as_of(tape, T2)
    assert a["rows"] == b["rows"]
    assert a["semantic_digest"] == b["semantic_digest"]
    # T is part of the semantic identity
    c = as_of(tape, T3)
    assert c["semantic_digest"] != a["semantic_digest"]
    # explicit digest recompute matches
    assert a["semantic_digest"] == asof_semantic_digest(
        a["rows"], a["as_of_utc"])


def test_asof_row_schema_and_canonical_order(tmp_path):
    tape, _p, _c = _three_stage(tmp_path)
    r = as_of(tape, T3)
    for row in r["rows"]:
        assert set(row) == set(ASOF_COLUMNS)
        assert row["as_of_utc"] == r["as_of_utc"]


def test_known_at_null_fails_closed(tmp_path):
    """A publication without known_at is unscopeable: refuse rather than
    silently include/exclude it."""
    def populate(raw, runs):
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, T1, [o])
    tape, pub_rows, _chains = _tape_for(
        tmp_path, populate, ["ISIN-A"])
    broken = [dict(r, known_at=None) for r in pub_rows]
    write_parquet(broken, PUB_COLUMNS, PUB_DUCK_TYPES,
                  tape / "publications.parquet", pub_sort_key)
    with pytest.raises(G1C2Error, match="known_at"):
        as_of(tape, T1)


def test_replay_substrate_roundtrip_on_tape(tmp_path):
    """Every stored normalized_evidence_json parses and replays into a
    record whose to_evidence() is byte-identical under canonical JSON."""
    def populate(raw, runs):
        o = _store(raw, BME, "2026-09-17", "d.json", [
            _bme(),
            _bme(amnd="true", flags="AMND", price="98.40",
                 publication_date_and_time="2026-09-17T11:00:05.000Z")])
        _manifest(runs, "20260917T100000Z", BME, T1, [o])
    _tape, pub_rows, _c = _tape_for(tmp_path, populate, ["ISIN-A"])
    for r in pub_rows:
        ev = json.loads(r["normalized_evidence_json"])
        rec = record_from_evidence(ev)
        assert json.dumps(rec.to_evidence(), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False) \
            == r["normalized_evidence_json"]


# ---------- replay adapter fail-closed ----------

def _base_evidence():
    return normalize_record(BME, _bme()).to_evidence()


def test_record_from_evidence_roundtrip():
    ev = _base_evidence()
    rec = record_from_evidence(ev)
    assert rec.to_evidence() == ev
    # G0-D-consumed attributes are present
    assert rec.publication_datetime.canonical_utc
    assert len(rec.raw_fields) == ev["raw_field_count"]
    assert rec.cancellation is False and rec.amendment is False


@pytest.mark.parametrize("mutate", [
    lambda e: e.pop("price"),
    lambda e: e.update(extra_key="x"),
    lambda e: e.update(cancellation=1),          # not a tri-state
    lambda e: e.update(raw_field_count=-1),
    lambda e: e.update(raw_field_count=True),
    lambda e: e.update(flags="AMND"),            # not a list
    lambda e: e.update(publication_datetime={
        "source_value": "x"}),                   # missing ts keys
    lambda e: e.update(sanity=[{"check": "c"}]), # incomplete sanity
    lambda e: e.update(rts2_mapping={"a": 1}),
])
def test_record_from_evidence_rejects_tampering(mutate):
    ev = _base_evidence()
    mutate(ev)
    with pytest.raises(ReplayError):
        record_from_evidence(ev)


def test_record_from_evidence_rejects_lost_precision():
    from decimal import Decimal
    ev = _base_evidence()
    ev["price"] = Decimal("98.5")   # not JSON-native — cannot roundtrip
    with pytest.raises(ReplayError):
        record_from_evidence(ev)


def test_record_from_evidence_preserves_source_typed_values():
    """BME raw passthrough can carry non-str values (e.g. Mantissa/
    Exponent dicts) — replay preserves them verbatim."""
    ev = _base_evidence()
    ev["quantity_in_measurement_unit"] = {"Mantissa": 3000000,
                                        "Exponent": 0}
    rec = record_from_evidence(ev)
    assert rec.quantity_in_measurement_unit == \
        {"Mantissa": 3000000, "Exponent": 0}
    assert rec.to_evidence() == ev


# ---------- C1 parent-authority authentication ----------

def _c1_report(path, *, schema_version=1):
    """Write a minimal synthetic C1-style report; return its sha256."""
    doc = {
        "issue_id": "G1-C1",
        "report": "G1-C1_TAPE_MATERIALIZATION",
        "tape_schema_version": schema_version,
        "dataset_semantic_digest": "d" * 64,
        "tables": {
            "publications": {"row_count": 2, "semantic_digest": "p" * 64},
            "trade_chains": {"row_count": 1, "semantic_digest": "c" * 64},
        },
    }
    data = yaml.safe_dump(doc).encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _c2_build(tmp_path, c1_path, **over):
    """Run metrics.g1c2.build_report over a minimal synthetic input set."""
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, T1, [o])
    g1a = tmp_path / "g1a.yaml"
    g1a_d = _g1a_artifact(g1a, ["ISIN-A"])
    g1b = tmp_path / "rows.jsonl"
    g1b_d = _g1b_rows(g1b, ["ISIN-A"])
    g1b_rep = tmp_path / "g1b.yaml"
    g1b_sha, basis = _g1b_report(g1b_rep, g1b_d)
    kw = {
        "window": WINDOW, "tape_dir": tmp_path / "tape",
        "c1_report_path": c1_path, "asof_timestamps": [], "date": "d",
        "generated_utc": "t", "expected_g1a_digest": g1a_d,
        "expected_g1b_digest": g1b_d,
        "expected_g1b_report_sha256": g1b_sha,
        "expected_g1b_basis_id": basis,
    }
    kw.update(over)
    return build_report(raw, runs, g1a, g1b, g1b_rep, **kw)


def test_c1_report_missing_fails_closed(tmp_path):
    with pytest.raises(G1C2Error):
        _c2_build(tmp_path, tmp_path / "absent.yaml",
                  expected_c1_report_sha256="0" * 64)


def test_c1_report_authenticates_synthetic_parent(tmp_path):
    c1 = tmp_path / "c1.yaml"
    sha = _c1_report(c1)
    report = _c2_build(tmp_path, c1, expected_c1_report_sha256=sha)
    pa = report["parent_authority"]
    assert pa["c1_report_sha256"] == sha
    # provenance is a stable logical id, never the operator path
    assert pa["c1_report"] == C1_REPORT_LOGICAL_ID
    assert str(tmp_path) not in pa["c1_report"]


def test_c1_report_mutated_bytes_fail_despite_schema_v1(tmp_path):
    c1 = tmp_path / "c1.yaml"
    sha = _c1_report(c1)
    with c1.open("ab") as fh:  # keep schema_version: 1, change the bytes
        fh.write(b"# tampered\n")
    with pytest.raises(G1C2Error, match="sha256"):
        _c2_build(tmp_path, c1, expected_c1_report_sha256=sha)


def test_c1_report_wrong_expected_sha_fails(tmp_path):
    c1 = tmp_path / "c1.yaml"
    _c1_report(c1)
    with pytest.raises(G1C2Error, match="sha256"):
        _c2_build(tmp_path, c1, expected_c1_report_sha256="0" * 64)


def test_c1_report_schema_checked_only_after_sha(tmp_path):
    c1 = tmp_path / "c1.yaml"
    sha = _c1_report(c1, schema_version=7)
    with pytest.raises(G1C2Error, match="tape_schema_version"):
        _c2_build(tmp_path, c1, expected_c1_report_sha256=sha)


def test_expected_c1_sha256_is_the_verified_report():
    """Production constant binds to the committed C1 evidence bytes."""
    assert EXPECTED_C1_REPORT_SHA256 == (
        "35b4dbea5b829d9e91882e9e03f9d7cc8beb24467ed385d5e6b3c93f7d2d99c8")
    p = Path("evidence/g1-c1/G1-C1_tape_materialization.yaml")
    if not p.is_file():
        pytest.skip("real C1 report not checked out")
    assert hashlib.sha256(p.read_bytes()).hexdigest() == \
        EXPECTED_C1_REPORT_SHA256
