"""Offline tests for G1-C1 tape materialization (metrics/g1c1.py).

Synthetic store/manifest fixtures only — no provider data, no network.
Covers the publication-unit contract, temporal fields, the explicit
occurrence -> LedgerEntry mapping, chain-head semantics, the verified
G1-A/G1-B authority joins, canonical digests, Parquet/DuckDB access.
"""

import hashlib
import json
from datetime import datetime

import pytest
import yaml
from test_metrics import (
    BME,
    WINDOW,
    _bme,
    _fixture,
    _manifest,
    _store,
)

from lifecycle.ledger import build_ledger, build_ledger_indexed, ledger_digest
from metrics.g1a import digest_entries
from metrics.g1c1 import (
    PUB_COLUMNS,
    G1C1Error,
    build_publications,
    build_report,
    canonical_table_bytes,
    connect_tape,
    generate_report,
    load_g1b_dispositions,
    pub_sort_key,
    verify_g1a_authority,
)
from metrics.selection import select_eligible
from normalization import normalize_record

EXPECTED_LEI = "254900QBKK4WBSO3GE51"

PUB_TS = "2026-09-17T10:00:05.000Z"


def _g1a_artifact(path, isins):
    entries = [{
        "isin": i,
        "sources": [BME],
        "first_observed_at": "2026-09-17T10:00:00.000000Z",
        "last_observed_at": "2026-09-17T10:00:00.000000Z",
        "eligible_capture_identities": 1,
    } for i in sorted(isins)]
    doc = {"observed_instrument_set": {
        "instrument_count": len(entries), "instruments": entries,
        "semantic_digest": digest_entries(entries)}}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return digest_entries(entries)


def _g1b_rows(path, isins, **by_isin):
    """Write canonical G1-B-style rows; returns their sha256."""
    lines = []
    for i in sorted(isins):
        ov = by_isin.get(i, {})
        lines.append(json.dumps({
            "isin": i,
            "branch": ov.get("branch", "INCLUDE"),
            "reason": ov.get("reason", "IN_ES_CRPB_UNIVERSE"),
            "firds": {"lei": ov.get("lei", EXPECTED_LEI)},
            "gleif": {"acq": "RESOLVED", "ok": True, "cc": "ES"}},
            sort_keys=True, separators=(",", ":")))
    data = ("\n".join(lines) + "\n").encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


SD_FITRS = "aa" * 32
SD_FIRDS = "bb" * 32
SD_GLEIF = "cc" * 32


def _g1b_report(path, rows_digest,
                fitrs_sd=SD_FITRS, firds_sd=SD_FIRDS, gleif_sd=SD_GLEIF,
                embedded_basis=None):
    """Write a minimal internally-valid G1-B report; returns
    (report_sha256, basis_id)."""
    from metrics.g1b import reference_basis_id
    basis = reference_basis_id({"fitrs": fitrs_sd, "firds": firds_sd,
                                "gleif": gleif_sd})
    doc = {
        "dispositions": {"rows_sha256": rows_digest},
        "reference_sets": {
            "fitrs": {"set_digest": fitrs_sd},
            "firds": {"set_digest": firds_sd},
            "gleif": {"set_digest": gleif_sd},
        },
        "reference_basis_id": embedded_basis if embedded_basis is not None
        else basis,
    }
    path.write_bytes(yaml.safe_dump(doc).encode("utf-8"))
    return hashlib.sha256(path.read_bytes()).hexdigest(), basis


def _materialize(tmp_path):
    """(raw, runs) fixture root -> build_publications + helpers."""
    raw, runs = _fixture(tmp_path)
    return raw, runs


def _pubs(raw, runs, dispositions):
    sel = select_eligible(
        runs, raw,
        start=datetime.fromisoformat(WINDOW["operational_start"]),
        tail_end=datetime.fromisoformat(WINDOW["evidence_tail_end"]))
    return build_publications(sel, dispositions)


def _dispositions(*isns):
    return {i: {"branch": "INCLUDE", "reason": "IN_ES_CRPB_UNIVERSE",
                "lei": EXPECTED_LEI} for i in isns}


def _rows_of(rows, isin="ISIN-A"):
    return [r for r in rows if r["isin"] == isin]


# ---------- publication unit / temporal contract ----------

def test_one_capture_one_record_one_publication_one_chain(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    rows, indexed, unparseable = _pubs(raw, runs, _dispositions("ISIN-A"))
    assert len(rows) == 1 and len(indexed) == 1 and not unparseable
    r = rows[0]
    assert r["isin"] == "ISIN-A"
    assert r["raw_sha256"] == r["capture_version"] == o["capture_version"]
    assert r["capture_record_ordinal"] == 0
    assert r["known_at"].startswith("2026-09-17T10:00:00")
    assert r["publication_utc"] == "2026-09-17T10:00:05+00:00"
    assert r["trading_utc"] == "2026-09-17T10:00:00+00:00"
    assert r["observation_count"] == 1
    assert r["universe_disposition"] == "INCLUDE"
    assert r["issuer_lei"] == EXPECTED_LEI


def test_multiple_records_one_capture_share_known_at(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json",
               [_bme(),
                _bme(transaction_identification_code="TX-2",
                     publication_date_and_time="2026-09-17T10:01:00.000Z"),
                _bme(instrument_identification_code="ISIN-B",
                     transaction_identification_code="TX-B")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    rows, _, _ = _pubs(raw, runs, _dispositions("ISIN-A", "ISIN-B"))
    assert len(rows) == 3
    assert {r["known_at"] for r in rows} == {
        rows[0]["known_at"]}  # identical capture known_at
    assert [r["capture_record_ordinal"] for r in
            sorted(rows, key=lambda r: r["capture_record_ordinal"])] == \
        [0, 1, 2]


def test_repeated_attestations_keep_earliest_known_at(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    for hh in ("12", "10", "14"):          # deliberately unsorted
        _manifest(runs, f"20260917T{hh}0000Z", BME,
                  f"2026-09-17T{hh}:00:00Z", [o])
    rows, _, _ = _pubs(raw, runs, _dispositions("ISIN-A"))
    assert len(rows) == 1
    r = rows[0]
    assert r["known_at"].startswith("2026-09-17T10:00:00")
    assert r["last_observed_at"].startswith("2026-09-17T14:00:00")
    assert r["observation_count"] == 3


def test_same_record_two_capture_identities_survive_twice(tmp_path):
    """Identical normalized content under two distinct eligible capture
    identities yields two publication rows — never deduplicated."""
    raw, runs = _fixture(tmp_path)
    o1 = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    o2 = _store(raw, BME, "2026-09-18", "d2.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o1])
    _manifest(runs, "20260918T100000Z", BME, "2026-09-18T10:00:00Z", [o2])
    rows, indexed, _ = _pubs(raw, runs, _dispositions("ISIN-A"))
    assert len(rows) == 2
    assert rows[0]["source_object_id"] != rows[1]["source_object_id"]
    assert len(indexed) == 2  # ledger kept both occurrences


def test_identical_records_get_exact_occurrence_mapping(tmp_path):
    """Two identical records under distinct capture identities: each pub
    occurrence binds exactly one distinct deterministic ledger entry."""
    raw, runs = _fixture(tmp_path)
    o1 = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    o2 = _store(raw, BME, "2026-09-18", "d2.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o1])
    _manifest(runs, "20260918T100000Z", BME, "2026-09-18T10:00:00Z", [o2])
    rows, indexed, _ = _pubs(raw, runs, _dispositions("ISIN-A"))
    ids = [r["ledger_entry_id"] for r in
           sorted(rows, key=lambda r: r["source_object_id"])]
    assert ids[0] != ids[1]
    assert ids[0].endswith("#1") and ids[1].endswith("#2")
    # earlier capture identity -> occurrence #1 (positions, not values)
    assert indexed[0][0] == 0 and indexed[1][0] == 1
    assert {r["ledger_entry_id"] for r in rows} == \
        {e.entry_id for _, e in indexed}
    assert all("ENTRY_ID_COLLISION" in e.markers for _, e in indexed)


# ---------- lifecycle authority ----------

def test_original_amendment_cancel_chain(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [
        _bme(),
        _bme(publication_date_and_time="2026-09-17T11:00:05.000Z",
             amnd="true", flags="AMND", price="98.40"),
        _bme(publication_date_and_time="2026-09-17T12:00:05.000Z",
             canc="true", flags="CANC")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    rows, _indexed, _ = _pubs(raw, runs, _dispositions("ISIN-A"))
    assert len(rows) == 3
    by_state = {r["lifecycle_state"]: r for r in rows}
    assert set(by_state) == {"FULLY_PUBLISHED", "AMENDED", "CANCELLED"}
    assert by_state["AMENDED"]["supersedes_entry_id"] == \
        by_state["FULLY_PUBLISHED"]["ledger_entry_id"]
    assert by_state["CANCELLED"]["supersedes_entry_id"] == \
        by_state["AMENDED"]["ledger_entry_id"]


def test_head_known_at_is_chain_tail_not_max(tmp_path):
    """head_known_at = known_at of the EXACT chain[-1] — an earlier-known
    amendment supersedes a later-known original."""
    raw, runs = _fixture(tmp_path)
    # amendment captured early, original captured late
    amend = _store(raw, BME, "2026-09-17", "a.json", [_bme(
        amnd="true", flags="AMND", price="98.40",
        publication_date_and_time="2026-09-17T11:00:05.000Z")])
    orig = _store(raw, BME, "2026-09-17", "o.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z",
              [amend])
    _manifest(runs, "20260917T200000Z", BME, "2026-09-17T20:00:00Z",
              [orig])
    rows, indexed, _ = _pubs(raw, runs, _dispositions("ISIN-A"))
    from metrics.g1c1 import build_chains
    chains = build_chains(indexed, rows)
    assert len(chains) == 1
    c = chains[0]
    assert c["event_count"] == 2
    assert c["head_state"] == "AMENDED"
    assert c["first_known_at"].startswith("2026-09-17T10:00:00")
    assert c["head_known_at"].startswith("2026-09-17T10:00:00")
    # head_known_at is NOT max(known_at) — the original's capture was
    # known later (20:00) yet the amendment is the authoritative head
    known_ats = {r["known_at"] for r in rows}
    assert max(known_ats).startswith("2026-09-17T20:00:00")
    assert c["head_known_at"] != max(known_ats)


def test_missing_tid_unchainable_singleton(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json",
               [_bme(transaction_identification_code="")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    rows, indexed, _ = _pubs(raw, runs, _dispositions("ISIN-A"))
    assert len(rows) == 1
    assert "UNCHAINABLE" in rows[0]["ledger_markers"]
    assert rows[0]["supersedes_entry_id"] is None
    from metrics.g1c1 import build_chains
    chains = build_chains(indexed, rows)
    assert len(chains) == 1 and chains[0]["event_count"] == 1
    assert "UNCHAINABLE" in chains[0]["chain_id"]


# ---------- G1-A / G1-B authority ----------

def test_g1b_join_by_exact_isin(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    disps = _dispositions("ISIN-A")
    disps["ISIN-A"] = {"branch": "EXCLUDE", "reason": "NOT_CRPB",
                       "lei": None}
    rows, _, _ = _pubs(raw, runs, disps)
    assert rows[0]["universe_disposition"] == "EXCLUDE"
    assert rows[0]["universe_disposition_reason"] == "NOT_CRPB"
    assert rows[0]["issuer_lei"] is None


def test_missing_disposition_fails_closed(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    with pytest.raises(G1C1Error, match="no verified G1-B"):
        _pubs(raw, runs, _dispositions("ISIN-OTHER"))


def test_duplicate_disposition_row_fails_closed(tmp_path):
    p = tmp_path / "rows.jsonl"
    row = json.dumps({"isin": "ISIN-A", "branch": "INCLUDE",
                      "reason": "IN_ES_CRPB_UNIVERSE", "firds": {"lei": None}})
    data = (row + "\n" + row + "\n").encode()
    p.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    with pytest.raises(G1C1Error, match="duplicate"):
        load_g1b_dispositions(p, expected_digest=digest)


def test_g1b_rows_digest_tamper_fails(tmp_path):
    p = tmp_path / "rows.jsonl"
    p.write_bytes(b'{"isin":"ISIN-A","branch":"INCLUDE",'
                  b'"reason":"IN_ES_CRPB_UNIVERSE","firds":{}}\n')
    with pytest.raises(G1C1Error, match="digest"):
        load_g1b_dispositions(p, expected_digest="0" * 64)


# ---------- Blocker: mandatory + independently-verified G1-B authority --

def _g1b_pair(tmp_path, isins=("ISIN-A",)):
    """Valid synthetic (rows, report) pair; returns
    (rows_path, report_path, expecteds dict)."""
    rows = tmp_path / "rows.jsonl"
    rows_sha = _g1b_rows(rows, list(isins))
    rep = tmp_path / "g1b.yaml"
    rep_sha, basis = _g1b_report(rep, rows_sha)
    return rows, rep, {
        "expected_rows_digest": rows_sha,
        "expected_report_sha256": rep_sha,
        "expected_basis_id": basis,
    }


def test_g1b_authority_valid_pair_passes(tmp_path):
    from metrics.g1c1 import verify_g1b_authority
    rows, rep, exp = _g1b_pair(tmp_path)
    out = verify_g1b_authority(rows, rep, **exp)
    assert out["reference_basis_id"] == exp["expected_basis_id"]
    assert out["report_sha256"] == exp["expected_report_sha256"]
    assert len(out["dispositions"]) == 1


def test_g1b_missing_report_fails(tmp_path):
    from metrics.g1c1 import verify_g1b_authority
    rows = tmp_path / "rows.jsonl"
    rows_sha = _g1b_rows(rows, ["ISIN-A"])
    with pytest.raises(G1C1Error, match="report not found"):
        verify_g1b_authority(
            rows, tmp_path / "absent.yaml",
            expected_rows_digest=rows_sha,
            expected_report_sha256="0" * 64,
            expected_basis_id="0" * 64)


def test_g1b_mutated_report_bytes_fail(tmp_path):
    from metrics.g1c1 import verify_g1b_authority
    rows, rep, exp = _g1b_pair(tmp_path)
    rep.write_bytes(rep.read_bytes() + b"\n# tampered\n")
    with pytest.raises(G1C1Error, match="report sha256"):
        verify_g1b_authority(rows, rep, **exp)


def test_g1b_embedded_basis_altered_fails(tmp_path):
    from metrics.g1c1 import verify_g1b_authority
    rows = tmp_path / "rows.jsonl"
    rows_sha = _g1b_rows(rows, ["ISIN-A"])
    rep = tmp_path / "g1b.yaml"
    rep_sha, basis = _g1b_report(rep, rows_sha,
                                 embedded_basis="ff" * 32)
    # computed basis != embedded -> fail even though bytes-hash is right
    with pytest.raises(G1C1Error, match="reference_basis_id"):
        verify_g1b_authority(rows, rep,
                             expected_rows_digest=rows_sha,
                             expected_report_sha256=rep_sha,
                             expected_basis_id=basis)


def test_g1b_family_set_digest_altered_fails_recompute(tmp_path):
    from metrics.g1c1 import verify_g1b_authority
    rows = tmp_path / "rows.jsonl"
    rows_sha = _g1b_rows(rows, ["ISIN-A"])
    rep = tmp_path / "g1b.yaml"
    # embedded basis kept at the OLD value while gleif set_digest changes
    rep_sha, basis = _g1b_report(
        rep, rows_sha, gleif_sd="dd" * 32,
        embedded_basis="aa" * 32)
    with pytest.raises(G1C1Error, match="reference_basis_id"):
        verify_g1b_authority(rows, rep,
                             expected_rows_digest=rows_sha,
                             expected_report_sha256=rep_sha,
                             expected_basis_id=basis)


def test_g1b_rows_sha_mismatch_with_report_fails(tmp_path):
    """Report binds different rows than supplied -> fail."""
    from metrics.g1c1 import verify_g1b_authority
    rows = tmp_path / "rows.jsonl"
    rows_sha = _g1b_rows(rows, ["ISIN-A"])
    rep = tmp_path / "g1b.yaml"
    rep_sha, basis = _g1b_report(rep, "0" * 64)  # declared != actual
    with pytest.raises(G1C1Error, match="does not bind"):
        verify_g1b_authority(rows, rep,
                             expected_rows_digest=rows_sha,
                             expected_report_sha256=rep_sha,
                             expected_basis_id=basis)


def test_g1a_digest_tamper_fails(tmp_path):
    art = tmp_path / "g1a.yaml"
    _g1a_artifact(art, ["ISIN-A"])
    doc = yaml.safe_load(art.read_text())
    doc["observed_instrument_set"]["instruments"][0]["isin"] = "XX"
    art.write_text(yaml.safe_dump(doc))
    with pytest.raises(G1C1Error, match="digest"):
        verify_g1a_authority(art, {"ISIN-A"})


def test_g1a_isin_surface_mismatch_fails(tmp_path):
    art = tmp_path / "g1a.yaml"
    digest = _g1a_artifact(art, ["ISIN-A", "ISIN-B"])
    with pytest.raises(G1C1Error, match="surface"):
        verify_g1a_authority(art, {"ISIN-A"}, expected_digest=digest)


# ---------- schema / canonicalization ----------

def test_currency_is_notional_currency_never_price(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json",
               [_bme(price_currency="USD", notional_currency="EUR")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    rows, _, _ = _pubs(raw, runs, _dispositions("ISIN-A"))
    assert rows[0]["currency"] == "EUR"
    assert rows[0]["price_currency"] == "USD"


def test_canonical_digest_input_order_independent(tmp_path):
    """Two equivalent fixtures in different roots and input orders ->
    identical canonical bytes."""
    outs = []
    for loc, order in (("a", ["10", "11"]), ("b", ["11", "10"])):
        (tmp_path / loc).mkdir()
        raw, runs = _fixture(tmp_path / loc)
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
        for hh in order:
            _manifest(runs, f"20260917T{hh}0000Z", BME,
                      f"2026-09-17T{hh}:00:00Z", [o])
        rows, _, _ = _pubs(raw, runs, _dispositions("ISIN-A"))
        outs.append(canonical_table_bytes(rows, PUB_COLUMNS, pub_sort_key))
    assert outs[0] == outs[1]


def test_ledger_indexed_mapping_bijective(tmp_path):
    """input_position -> LedgerEntry is explicit and bijective; identical
    records keep deterministic occurrence ids."""
    recs = [normalize_record(BME, _bme()),
            normalize_record(BME, _bme()),
            normalize_record(BME, _bme(transaction_identification_code="TX-2"))]
    indexed = build_ledger_indexed(recs)
    assert sorted(pos for pos, _ in indexed) == [0, 1, 2]
    assert [e for _, e in indexed] == build_ledger(recs)
    assert ledger_digest([e for _, e in indexed]) == \
        ledger_digest(build_ledger(recs))


# ---------- Parquet / DuckDB ----------

def _full_materialization(tmp_path):
    """End-to-end: store + manifest + authority files -> report + tape."""
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [
        _bme(),
        _bme(publication_date_and_time="2026-09-17T11:00:05.000Z",
             amnd="true", flags="AMND")])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    g1a = tmp_path / "g1a.yaml"
    g1a_digest = _g1a_artifact(g1a, ["ISIN-A"])
    g1b = tmp_path / "rows.jsonl"
    g1b_digest = _g1b_rows(g1b, ["ISIN-A"])
    g1b_rep = tmp_path / "g1b.yaml"
    g1b_rep_sha, basis = _g1b_report(g1b_rep, g1b_digest)
    tape = tmp_path / "tape"
    report, pub_b, chain_b = build_report(
        raw, runs, g1a, g1b, g1b_rep, window=WINDOW,
        tape_dir=tape, date="d", generated_utc="t",
        expected_g1a_digest=g1a_digest,
        expected_g1b_digest=g1b_digest,
        expected_g1b_report_sha256=g1b_rep_sha,
        expected_g1b_basis_id=basis)
    return report, pub_b, chain_b, tape


def test_full_materialization_report_and_digests(tmp_path):
    report, pub_b, chain_b, _tape = _full_materialization(tmp_path)
    pubs = report["tables"]["publications"]
    chains = report["tables"]["trade_chains"]
    assert pubs["row_count"] == 2 and chains["row_count"] == 1
    assert pubs["semantic_digest"] == hashlib.sha256(pub_b).hexdigest()
    assert chains["semantic_digest"] == hashlib.sha256(chain_b).hexdigest()
    ds = report["dataset_semantic_digest"]
    assert len(ds) == 64
    assert report["tape_schema_version"] == 1
    assert report["lifecycle_reconciliation"]["supersession_links"] == 1
    assert report["lifecycle_reconciliation"]["head_state_counts"] == \
        {"AMENDED": 1}


def test_parquet_row_counts_and_duckdb_queries(tmp_path):
    report, _pub_b, _chain_b, tape = _full_materialization(tmp_path)
    con = connect_tape(tape)
    pubs = con.execute("SELECT count(*) FROM publications").fetchone()[0]
    chains = con.execute(
        "SELECT count(*) FROM trade_chains").fetchone()[0]
    assert pubs == report["tables"]["publications"]["row_count"]
    assert chains == report["tables"]["trade_chains"]["row_count"]
    # filter by ISIN
    n = con.execute(
        "SELECT count(*) FROM publications WHERE isin='ISIN-A'"
    ).fetchone()[0]
    assert n == 2
    # chain lookup + join pubs -> head
    head = con.execute(
        "SELECT head_entry_id, head_state, first_known_at, head_known_at "
        "FROM trade_chains").fetchone()
    assert head[1] == "AMENDED"
    joined = con.execute(
        "SELECT p.lifecycle_state FROM publications p JOIN trade_chains c "
        "ON p.ledger_entry_id = c.head_entry_id").fetchone()
    assert joined[0] == "AMENDED"
    # currency column = notional currency
    assert con.execute(
        "SELECT currency, price_currency FROM publications"
    ).fetchall() == [("EUR", "EUR"), ("EUR", "EUR")]
    con.close()


def test_real_g1b_artifact_digest_verified():
    """The committed G1-B rows file verifies against the frozen expected
    digest — the real upstream authority is intact."""
    from universe._paths import repo_file
    rows = repo_file("evidence", "g1-b1",
                     "G1-B1_disposition_rows.jsonl")
    if not rows.is_file():
        pytest.skip("G1-B evidence not checked out")
    disps = load_g1b_dispositions(rows)  # default expected digest
    assert len(disps) == 8077


def test_evidence_emission_and_no_paths(tmp_path):
    raw, runs = _fixture(tmp_path)
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME, "2026-09-17T10:00:00Z", [o])
    g1a = tmp_path / "g1a.yaml"
    g1a_d = _g1a_artifact(g1a, ["ISIN-A"])
    g1b = tmp_path / "rows.jsonl"
    g1b_d = _g1b_rows(g1b, ["ISIN-A"])
    g1b_rep = tmp_path / "g1b.yaml"
    rep_sha, basis = _g1b_report(g1b_rep, g1b_d)
    path = generate_report(
        raw, runs, g1a, g1b, g1b_rep, window=WINDOW,
        tape_dir=tmp_path / "tape", evidence_dir=tmp_path / "ev",
        date="d", generated_utc="t",
        expected_g1a_digest=g1a_d, expected_g1b_digest=g1b_d,
        expected_g1b_report_sha256=rep_sha,
        expected_g1b_basis_id=basis)
    text = path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "publications.parquet" in text
    assert (tmp_path / "tape" / "publications.parquet").is_file()
    assert (tmp_path / "tape" / "trade_chains.parquet").is_file()
    rep = yaml.safe_load(text)
    assert rep["hygiene"]["parquet_files_committed"] is False
