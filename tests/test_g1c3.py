"""Tests for metrics.g1c3 — G1-C3 deterministic EVIDENCE_CAPSULE / explain.

Adversarial inventory (issue #30 + mandate):

1.  same publication explained twice -> byte-identical capsule
2.  same current chain explained twice -> byte-identical capsule
3.  original/amend/cancel at T1/T2/T3
4.  future cancellation never leaks into T2 (early publication_utc)
5.  duplicate occurrences stay distinguishable by stable occurrence id
6.  stage-aware reference citation (NOT_CRPB -> FITRS; INCLUDE -> 3 families)
7.  mutated reference_links bytes -> fail closed
8.  mutated C2 parent report / dataset authority -> fail closed
9.  provenance comes only from stored rts2_mapping
10. capsule carries no provider raw payload/fields
11. cross-root materialization -> byte-identical capsule
12. raw store removed -> explain still works
13. historical correction/republication preserves earlier knowledge
14. regime/artifact-family transition does not break identity or
    provenance separation
15. unknown / duplicate publication id -> fail closed
16. unknown chain id -> fail closed
17. naive / unparseable T -> fail closed
18. tape dataset/schema mismatch -> fail closed
19. lifecycle member not resolvable to a persisted occurrence -> fail closed
20. historical capsule contains no future evidence
21. pinned fixture capsule sha256 (cross-platform determinism anchor —
    the same assertion runs on Linux and Windows self-hosted CI)
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shutil
from datetime import datetime

import pytest
import yaml
from test_g1c1 import EXPECTED_LEI
from test_metrics import BME, WINDOW, _bme, _manifest, _nbme, _store

from metrics.g1b import reference_basis_id
from metrics.g1c1 import (
    CHAIN_COLUMNS,
    CHAIN_DUCK_TYPES,
    PUB_COLUMNS,
    PUB_DUCK_TYPES,
    build_chains,
    build_publications,
    dataset_semantic_digest,
    pub_sort_key,
    semantic_digest,
    write_parquet,
)
from metrics.g1c3 import (
    G1C3Error,
    build_report,
    capsule_bytes,
    capsule_sha256,
    explain_chain,
    explain_publication,
    generate_report,
    row_publication_id,
)
from metrics.selection import select_eligible

T1 = "2026-09-17T10:00:00Z"
T2 = "2026-09-17T12:00:00Z"
T3 = "2026-09-17T14:00:00Z"

FITRS_SD = "aa" * 32
FIRDS_SD = "bb" * 32
GLEIF_SD = "cc" * 32

# reason -> reference families actually consumed (metrics.g1b stage map)
_STAGE = {
    "IN_ES_CRPB_UNIVERSE": ("fitrs", "firds", "gleif"),
    "NOT_CRPB": ("fitrs",),
    "MISSING_ISSUER_LEI": ("fitrs", "firds"),
}


def _rows_bytes(isin_specs):
    """Canonical synthetic disposition rows {isin: {branch, reason, lei}}."""
    lines = []
    for isin in sorted(isin_specs):
        ov = isin_specs[isin]
        lines.append(json.dumps({
            "isin": isin,
            "branch": ov.get("branch", "INCLUDE"),
            "reason": ov.get("reason", "IN_ES_CRPB_UNIVERSE"),
            "firds": {"lei": ov.get("lei", EXPECTED_LEI)},
            "gleif": {"acq": "RESOLVED", "ok": True, "cc": "ES"}},
            sort_keys=True, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode()


def _links_bytes(isin_specs, mutate=None):
    """Synthetic reference_links jsonl — stage-aware handles per reason."""
    lines = []
    for isin in sorted(isin_specs):
        reason = isin_specs[isin].get("reason", "IN_ES_CRPB_UNIVERSE")
        stage = _STAGE[reason]
        link = {"i": isin}
        if "fitrs" in stage:
            link["fitrs"] = {"0:0": {"o": [7, 8], "r": ["R-7", "R-8"]}}
        if "firds" in stage:
            link["firds"] = {"0:1": {"o": [3]}}
        link["gleif"] = (f"{isin_specs[isin].get('lei', EXPECTED_LEI)}.json"
                         if "gleif" in stage else None)
        if mutate and mutate.get(isin):
            link = mutate[isin](link)
        lines.append(json.dumps(link, sort_keys=True, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode()


def _tape_with_dispositions(tmp_path, populate, isin_specs):
    """Like test_g1c2._tape_for but joins the per-ISIN dispositions the
    test actually asserts (not the all-INCLUDE convenience map)."""
    raw = tmp_path / "raw"
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    populate(raw, runs)
    sel = select_eligible(
        runs, raw,
        start=datetime.fromisoformat(WINDOW["operational_start"]),
        tail_end=datetime.fromisoformat(WINDOW["evidence_tail_end"]))
    dispositions = {
        i: {"branch": s.get("branch", "INCLUDE"),
            "reason": s.get("reason", "IN_ES_CRPB_UNIVERSE"),
            "lei": s.get("lei", EXPECTED_LEI)}
        for i, s in isin_specs.items()}
    pub_rows, indexed, unparseable = build_publications(sel, dispositions)
    assert not unparseable
    chain_rows = build_chains(indexed, pub_rows)
    tape = tmp_path / "tape"
    write_parquet(pub_rows, PUB_COLUMNS, PUB_DUCK_TYPES,
                  tape / "publications.parquet", pub_sort_key)
    write_parquet(chain_rows, CHAIN_COLUMNS, CHAIN_DUCK_TYPES,
                  tape / "trade_chains.parquet",
                  lambda r: r["chain_id"])
    return tape, pub_rows, chain_rows


def _env(tmp_path, populate, isin_specs, *, links_mutate=None):
    """Build a synthetic tape + authenticated authority bundle.

    Returns (tape_dir, pub_rows, chain_rows, kw) where kw carries the
    authority paths and every expected_* digest bound to the synthetic
    artifacts (production constants stay untouched).
    """
    tape, pub_rows, chain_rows = _tape_with_dispositions(
        tmp_path, populate, isin_specs)

    rows_b = _rows_bytes(isin_specs)
    links_b = _links_bytes(isin_specs, mutate=links_mutate)
    rows_sha = hashlib.sha256(rows_b).hexdigest()
    links_sha = hashlib.sha256(links_b).hexdigest()

    g1b_rows = tmp_path / "g1b_rows.jsonl"
    g1b_links = tmp_path / "g1b_links.jsonl"
    g1b_rows.write_bytes(rows_b)
    g1b_links.write_bytes(links_b)

    lei = EXPECTED_LEI
    gleif_entries = [{"name": f"{lei}.json", "sha256": "ee" * 32,
                      "bytes": 300}]
    for spec in isin_specs.values():
        lei_i = spec.get("lei", EXPECTED_LEI)
        if f"{lei_i}.json" not in {e["name"] for e in gleif_entries}:
            gleif_entries.append({"name": f"{lei_i}.json",
                                  "sha256": "ff" * 32, "bytes": 301})
    gleif_entries.sort(key=lambda e: e["name"])
    set_digests = {"fitrs": FITRS_SD, "firds": FIRDS_SD, "gleif": GLEIF_SD}
    basis = reference_basis_id(set_digests)
    g1b_doc = {
        "dispositions": {"rows_sha256": rows_sha},
        "reference_sets": {
            "fitrs": {"set_digest": FITRS_SD, "entries": [
                {"name": "fulncr_a.zip", "sha256": "aa" * 32,
                 "bytes": 100}]},
            "firds": {"set_digest": FIRDS_SD, "entries": [
                {"name": "fulins_b.zip", "sha256": "bb" * 32,
                 "bytes": 200}]},
            "gleif": {"set_digest": GLEIF_SD, "entries": gleif_entries},
        },
        "reference_basis_id": basis,
        "reference_links": {"file": "g1b_links.jsonl", "sha256": links_sha},
    }
    g1b_report = tmp_path / "g1b.yaml"
    g1b_report.write_bytes(yaml.safe_dump(g1b_doc).encode())
    g1b_sha = hashlib.sha256(g1b_report.read_bytes()).hexdigest()

    pub_d = semantic_digest(pub_rows, PUB_COLUMNS, pub_sort_key)
    chain_d = semantic_digest(chain_rows, CHAIN_COLUMNS,
                              lambda r: r["chain_id"])
    ds = dataset_semantic_digest(pub_d, len(pub_rows), chain_d,
                                 len(chain_rows))
    c2_doc = {"tape_schema_version": 2,
              "tape_materialization_v2": {"dataset_semantic_digest": ds}}
    c2_path = tmp_path / "c2.yaml"
    c2_path.write_bytes(yaml.safe_dump(c2_doc).encode())
    c2_sha = hashlib.sha256(c2_path.read_bytes()).hexdigest()

    kw = {
        "c2_report_path": c2_path,
        "g1b_rows_path": g1b_rows,
        "g1b_report_path": g1b_report,
        "g1b_links_path": g1b_links,
        "expected_c2_report_sha256": c2_sha,
        "expected_dataset_digest": ds,
        "expected_g1b_digest": rows_sha,
        "expected_g1b_report_sha256": g1b_sha,
        "expected_g1b_basis_id": basis,
        "expected_g1b_links_sha256": links_sha,
    }
    return tape, pub_rows, chain_rows, kw


def _one_pub(tmp_path, **bme_kw):
    def populate(raw, runs):
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme(**bme_kw)])
        _manifest(runs, "20260917T100000Z", BME, T1, [o])
    return _env(tmp_path, populate, {"ISIN-A": {}})


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
    return _env(tmp_path, populate, {"ISIN-A": {}})


def _chain_id(tape):
    import duckdb
    con = duckdb.connect()
    p = str((tape / "trade_chains.parquet").resolve()).replace("\\", "/")
    try:
        return con.execute(
            f"SELECT chain_id FROM '{p}'").fetchone()[0]  # noqa: S608
    finally:
        con.close()


def _rebind_c2(kw, tmp_path, pubs, chains, tag):
    """Re-pin the synthetic C2 authority after a tape rewrite."""
    ds = dataset_semantic_digest(
        semantic_digest(pubs, PUB_COLUMNS, pub_sort_key), len(pubs),
        semantic_digest(chains, CHAIN_COLUMNS,
                        lambda r: r["chain_id"]), len(chains))
    c2_path = tmp_path / f"c2_{tag}.yaml"
    c2_path.write_bytes(yaml.safe_dump({
        "tape_schema_version": 2,
        "tape_materialization_v2": {"dataset_semantic_digest": ds}
    }).encode())
    return dict(kw, c2_report_path=c2_path,
                expected_c2_report_sha256=hashlib.sha256(
                    c2_path.read_bytes()).hexdigest(),
                expected_dataset_digest=ds)


# ---------------- 1/2. repeated-request byte determinism ----------------

def test_same_publication_twice_byte_identical(tmp_path):
    tape, pubs, _c, kw = _one_pub(tmp_path)
    pid = row_publication_id(pubs[0])
    a = explain_publication(tape, pid, **kw)
    b = explain_publication(tape, pid, **kw)
    assert capsule_bytes(a) == capsule_bytes(b)
    assert a["capsule_sha256"] == capsule_sha256(a)


def test_same_current_chain_twice_byte_identical(tmp_path):
    tape, _p, _c, kw = _three_stage(tmp_path)
    cid = _chain_id(tape)
    a = explain_chain(tape, cid, **kw)
    b = explain_chain(tape, cid, **kw)
    assert capsule_bytes(a) == capsule_bytes(b)


def test_same_asof_request_twice_byte_identical(tmp_path):
    tape, _p, _c, kw = _three_stage(tmp_path)
    cid = _chain_id(tape)
    a = explain_chain(tape, cid, at=T2, **kw)
    b = explain_chain(tape, cid, at=T2, **kw)
    assert capsule_bytes(a) == capsule_bytes(b)


# ---------------- 3. original / amend / cancel timeline ----------------

def test_orig_amend_cancel_capsules_at_t1_t2_t3(tmp_path):
    tape, _pubs, _c, kw = _three_stage(tmp_path)
    cid = _chain_id(tape)

    cur = explain_chain(tape, cid, **kw)
    assert cur["lifecycle"]["event_count"] == 3
    assert cur["lifecycle"]["head_state"] == "CANCELLED"
    assert cur["temporal"]["mode"] == "current"

    c1 = explain_chain(tape, cid, at=T1, **kw)
    assert c1["lifecycle"]["event_count"] == 1
    assert c1["lifecycle"]["head_state"] == "FULLY_PUBLISHED"
    assert c1["temporal"] == {"mode": "as_of", "as_of_utc":
                            "2026-09-17T10:00:00.000000Z"}
    assert c1["lifecycle"]["entry_id_scope"] == "reconstructed_as_of"

    c2 = explain_chain(tape, cid, at=T2, **kw)
    assert c2["lifecycle"]["event_count"] == 2
    assert c2["lifecycle"]["head_state"] == "AMENDED"

    c3 = explain_chain(tape, cid, at=T3, **kw)
    assert c3["lifecycle"]["event_count"] == 3
    assert c3["lifecycle"]["head_state"] == "CANCELLED"

    # supersession links order members; current capsule mirrors the
    # materialized ledger; historical capsules are rebuilt per-subset
    members = cur["lifecycle"]["members"]
    assert members[0]["supersedes_entry_id"] is None
    for prev, m in itertools.pairwise(members):
        assert m["supersedes_entry_id"] == prev["entry_id"]


# ---------------- 4. future cancellation never leaks ----------------

def test_future_cancel_never_leaks_despite_early_pub_utc(tmp_path):
    """The cancellation's publication_utc sits between orig and amend —
    adversarially early — yet only known_at gates capsule content."""
    tape, pubs, _c, kw = _three_stage(
        tmp_path, cancel_pub="2026-09-17T10:30:05.000Z")
    cid = _chain_id(tape)
    cancel_pid = row_publication_id(
        next(r for r in pubs if r["lifecycle_state"] == "CANCELLED"))

    cap = explain_chain(tape, cid, at=T2, **kw)
    assert cap["lifecycle"]["event_count"] == 2
    assert cap["lifecycle"]["head_state"] == "AMENDED"
    blob = capsule_bytes(cap)
    assert cancel_pid.encode() not in blob
    member_ids = {m["publication_id"] for m in cap["lifecycle"]["members"]}
    assert cancel_pid not in member_ids
    assert all(m["known_at"] <= "2026-09-17T12:00:00.000000Z"
               for m in cap["lifecycle"]["members"])

    # and the occurrence itself refuses historical explanation
    with pytest.raises(G1C3Error, match="not knowable"):
        explain_publication(tape, cancel_pid, at=T2, **kw)


# ---------------- 5. duplicate occurrences stay distinguishable --------

def test_duplicate_occurrences_distinct_publication_ids(tmp_path):
    """Identical record bytes captured twice -> same ledger identity
    material, distinct publication occurrence ids."""
    def populate(raw, runs):
        o1 = _store(raw, BME, "2026-09-17", "d1.json", [_bme()])
        o2 = _store(raw, BME, "2026-09-18", "d2.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, T1, [o1])
        _manifest(runs, "20260918T100000Z", BME, T3, [o2])
    tape, pubs, _c, kw = _env(tmp_path, populate, {"ISIN-A": {}})
    assert len(pubs) == 2
    ids = [row_publication_id(r) for r in pubs]
    assert len(set(ids)) == 2
    for pid, row in zip(ids, pubs, strict=True):
        cap = explain_publication(tape, pid, **kw)
        assert cap["publication"]["capture_version"] \
            == row["capture_version"]
        assert cap["publication"]["source_object_id"] \
            == row["source_object_id"]


# ---------------- 6. stage-aware reference citation --------------------

def test_stage_aware_reference_citation(tmp_path):
    """NOT_CRPB cites FITRS only; INCLUDE cites FITRS+FIRDS+GLEIF — never
    a family whose observations did not support the disposition."""
    def populate(raw, runs):
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
        o2 = _store(raw, BME, "2026-09-17", "e.json",
                    [_bme(instrument_identification_code="ISIN-B",
                          transaction_identification_code="TX-B")])
        _manifest(runs, "20260917T100000Z", BME, T1, [o, o2])
    tape, pubs, _c, kw = _env(
        tmp_path, populate,
        {"ISIN-A": {}, "ISIN-B": {"branch": "EXCLUDE",
                                  "reason": "NOT_CRPB", "lei": None}})

    caps = {r["isin"]: explain_publication(
        tape, row_publication_id(r), **kw) for r in pubs}
    inc = caps["ISIN-A"]["reference_authority"]
    exc = caps["ISIN-B"]["reference_authority"]
    assert inc["families_cited"] == ["firds", "fitrs", "gleif"]
    assert set(inc["set_digests"]) == {"fitrs", "firds", "gleif"}
    assert "gleif" in inc["observations"]
    assert exc["families_cited"] == ["fitrs"]
    assert set(exc["set_digests"]) == {"fitrs"}
    assert set(exc["observations"]) <= {"fitrs"}
    # concrete handle resolution against the pinned family entry order
    h = inc["observations"]["fitrs"][0]
    assert h["file"] == "fulncr_a.zip" and h["record_ordinals"] == [7, 8]


# ---------------- 7/8. mutated authorities fail closed -----------------

def test_mutated_reference_links_fail_closed(tmp_path):
    tape, pubs, _c, kw = _one_pub(tmp_path)
    kw["g1b_links_path"].write_bytes(
        kw["g1b_links_path"].read_bytes() + b" ")
    pid = row_publication_id(pubs[0])
    with pytest.raises(G1C3Error, match="reference_links"):
        explain_publication(tape, pid, **kw)


def test_mutated_c2_report_fails_closed(tmp_path):
    tape, pubs, _c, kw = _one_pub(tmp_path)
    kw["c2_report_path"].write_bytes(
        kw["c2_report_path"].read_bytes() + b"# tampered\n")
    pid = row_publication_id(pubs[0])
    with pytest.raises(G1C3Error, match="C2 report sha256"):
        explain_publication(tape, pid, **kw)


def test_wrong_expected_dataset_digest_fails_closed(tmp_path):
    tape, pubs, _c, kw = _one_pub(tmp_path)
    kw["expected_dataset_digest"] = "0" * 64
    pid = row_publication_id(pubs[0])
    with pytest.raises(G1C3Error, match="dataset digest"):
        explain_publication(tape, pid, **kw)


def test_wrong_expected_c2_sha_fails_closed(tmp_path):
    tape, pubs, _c, kw = _one_pub(tmp_path)
    kw["expected_c2_report_sha256"] = "0" * 64
    with pytest.raises(G1C3Error, match="C2 report sha256"):
        explain_publication(tape, row_publication_id(pubs[0]), **kw)


def test_missing_c2_report_fails_closed(tmp_path):
    tape, pubs, _c, kw = _one_pub(tmp_path)
    kw["c2_report_path"] = tmp_path / "absent.yaml"
    with pytest.raises(G1C3Error, match="not found"):
        explain_publication(tape, row_publication_id(pubs[0]), **kw)


def test_link_handle_outside_pinned_family_set_fails(tmp_path):
    def bad(link):
        link["fitrs"] = {"5:0": {"o": [1]}}   # only 1 fitrs entry (idx 0)
        return link
    tape, pubs, _c, kw = _env(
        tmp_path,
        lambda raw, runs: (_manifest(
            runs, "20260917T100000Z", BME, T1,
            [_store(raw, BME, "2026-09-17", "d.json", [_bme()])])),
        {"ISIN-A": {}}, links_mutate={"ISIN-A": bad})
    # mutate links AFTER rows/report were authenticated — links sha must
    # be re-pinned to the mutated bytes to reach the resolver
    links_b = kw["g1b_links_path"].read_bytes()
    doc = yaml.safe_load(kw["g1b_report_path"].read_bytes())
    doc["reference_links"]["sha256"] = hashlib.sha256(links_b).hexdigest()
    kw["g1b_report_path"].write_bytes(yaml.safe_dump(doc).encode())
    kw["expected_g1b_report_sha256"] = hashlib.sha256(
        kw["g1b_report_path"].read_bytes()).hexdigest()
    kw["expected_g1b_links_sha256"] = hashlib.sha256(links_b).hexdigest()
    with pytest.raises(G1C3Error, match="outside the pinned family"):
        explain_publication(tape, row_publication_id(pubs[0]), **kw)


# ---------------- 9. provenance only from stored rts2_mapping ----------

def test_provenance_only_from_stored_mapping(tmp_path):
    tape, pubs, _c, kw = _one_pub(tmp_path)
    pid = row_publication_id(pubs[0])
    cap = explain_publication(tape, pid, **kw)
    prov = cap["economic_provenance"]["field_source_map"]
    assert prov["price"] == "price"                       # stored mapping
    ev = json.loads(pubs[0]["normalized_evidence_json"])
    assert prov == {f: ev["rts2_mapping"][f]
                    for f in prov if f in ev["rts2_mapping"]}

    # strip a mapping from the stored evidence: it stays absent,
    # never guessed
    ev["rts2_mapping"] = {k: v for k, v in ev["rts2_mapping"].items()
                          if k != "price"}
    row = dict(pubs[0])
    row["normalized_evidence_json"] = json.dumps(
        ev, sort_keys=True, separators=(",", ":"))
    tape2 = tmp_path / "tape2"
    pubs2 = [row]
    write_parquet(pubs2, PUB_COLUMNS, PUB_DUCK_TYPES,
                  tape2 / "publications.parquet", pub_sort_key)
    shutil.copy(tape / "trade_chains.parquet", tape2 / "trade_chains.parquet")
    kw2 = _rebind_c2(kw, tmp_path, pubs2,
                     _read_chains(tape2), "nomap")
    cap2 = explain_publication(tape2, pid, **kw2)
    prov2 = cap2["economic_provenance"]
    assert "price" not in prov2["field_source_map"]
    assert "price" in prov2["unmapped_core_fields"]


def _read_chains(tape):
    import duckdb
    con = duckdb.connect()
    p = str((tape / "trade_chains.parquet").resolve()).replace("\\", "/")
    try:
        cols = [c[0] for c in con.execute(
            f"DESCRIBE SELECT * FROM '{p}'").fetchall()]  # noqa: S608
        return [dict(zip(cols, r, strict=True)) for r in
                con.execute(
                    f"SELECT * FROM '{p}'").fetchall()]  # noqa: S608
    finally:
        con.close()


# ---------------- 10. no provider raw payload --------------------------

def test_capsule_carries_no_provider_payload(tmp_path):
    tape, pubs, _c, kw = _one_pub(
        tmp_path, provider_internal_field="PAYLOAD_XYZ_NEVER_EMITTED",
        quantity_in_measurement_unit={"Mantissa": 3000000, "Exponent": 0})
    pid = row_publication_id(pubs[0])
    blob = capsule_bytes(explain_publication(tape, pid, **kw))
    assert b"PAYLOAD_XYZ_NEVER_EMITTED" not in blob
    assert b"provider_internal_field" not in blob
    assert b"Mantissa" not in blob
    assert b"raw_fields" not in blob


# ---------------- 11. cross-root byte determinism ----------------------

def test_cross_root_materialization_byte_identical(tmp_path):
    def populate(raw, runs):
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, T1, [o])
    spec = {"ISIN-A": {}}
    tape_a, pubs_a, _c, kw_a = _env(tmp_path / "root_a", populate, spec)
    tape_b, pubs_b, _c2, kw_b = _env(tmp_path / "root_b", populate, spec)
    pid_a = row_publication_id(pubs_a[0])
    pid_b = row_publication_id(pubs_b[0])
    assert pid_a == pid_b                     # identity is root-free
    a = capsule_bytes(explain_publication(tape_a, pid_a, **kw_a))
    b = capsule_bytes(explain_publication(tape_b, pid_b, **kw_b))
    assert a == b
    assert str(tmp_path).encode() not in a


# ---------------- 12. raw store removed --------------------------------

def test_explain_works_without_raw_store(tmp_path):
    def populate(raw, runs):
        o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
        _manifest(runs, "20260917T100000Z", BME, T1, [o])
    tape, pubs, _c, kw = _env(tmp_path, populate, {"ISIN-A": {}})
    shutil.rmtree(tmp_path / "raw")
    cap = explain_publication(tape, row_publication_id(pubs[0]), **kw)
    assert cap["capsule_sha256"]


# ---------------- 13. historical correction / republication ------------

def test_historical_correction_preserves_earlier_knowledge(tmp_path):
    """Same source object republished with corrected bytes: version A
    known at T1 stays explainable at T1; version B (known T2) never
    leaks into the T1 view and both remain distinct occurrences."""
    def populate(raw, runs):
        a = _store(raw, BME, "2026-09-17", "d.json", [_bme()],
                   obs=T1)
        b = _store(raw, BME, "2026-09-18", "d.json",
                   [_bme(price="98.10")], obs=T2)
        _manifest(runs, "20260917T100000Z", BME, T1, [a])
        _manifest(runs, "20260917T120000Z", BME, T2, [b])
    tape, pubs, _c, kw = _env(tmp_path, populate, {"ISIN-A": {}})
    assert len(pubs) == 2
    by_cv = {r["capture_version"]: r for r in pubs}
    pid_a, pid_b = (row_publication_id(by_cv[cv]) for cv in
                    sorted(by_cv, key=lambda c: by_cv[c]["known_at"]))
    assert pid_a != pid_b                    # object + version, not object

    cap_a_t1 = explain_publication(tape, pid_a, at=T1, **kw)
    assert b"98.10" not in capsule_bytes(cap_a_t1)
    with pytest.raises(G1C3Error, match="not knowable"):
        explain_publication(tape, pid_b, at=T1, **kw)

    cap_b_t2 = explain_publication(tape, pid_b, at=T2, **kw)
    assert cap_b_t2["publication"]["capture_version"] != \
        cap_a_t1["publication"]["capture_version"]
    # earlier knowledge is not overwritten: A stays explainable at T2 too
    explain_publication(tape, pid_a, at=T2, **kw)


# ---------------- 14. regime / source-family transition ----------------

def test_regime_transition_identity_and_provenance(tmp_path):
    """A chain whose members carry provenance from two artifact-schema
    generations still explains correctly: occurrence identity does not
    depend on a closed source/family enum, per-member provenance stays
    separated, and no upstream Art.26 evidence is fabricated."""
    rec_orig = _nbme()
    ev_amend = _nbme(
        amnd="true", flags="AMND", price="98.40",
        publication_date_and_time="2026-09-18T10:00:05.000Z"
    ).to_evidence()
    # post-transition capture: same logical fields, renamed source schema
    ev_amend["rts2_mapping"] = {
        **ev_amend["rts2_mapping"], "price": "PostTradePx_2026"}
    from normalization.replay import record_from_evidence
    rec_amend = record_from_evidence(ev_amend)

    from lifecycle.ledger import build_ledger_indexed
    indexed = build_ledger_indexed([rec_orig, rec_amend])
    entry_by_pos = dict(indexed)

    def row(ev, oid, cv, known):
        rec = record_from_evidence(ev)
        entry = entry_by_pos[0 if oid == "d1.json" else 1]
        return {
            "source": BME, "source_object_id": oid,
            "capture_version": cv, "raw_sha256": cv,
            "capture_record_ordinal": 0,
            "known_at": known, "last_observed_at": known,
            "observation_count": 1,
            "publication_utc": rec.publication_datetime.canonical_utc,
            "trading_utc": rec.trading_datetime.canonical_utc,
            "isin": "ISIN-A",
            "transaction_id": rec.transaction_identification_code,
            "price": rec.price, "quantity": rec.quantity,
            "notional": rec.notional_amount,
            "currency": rec.notional_currency,
            "price_currency": rec.price_currency,
            "price_notation": rec.price_notation,
            "flags": sorted(rec.flags),
            "source_report_id": rec.source_report_id,
            "lifecycle_state": entry.state,
            "ledger_entry_id": entry.entry_id,
            "supersedes_entry_id": entry.supersedes_entry_id,
            "ledger_markers": sorted(entry.markers),
            "universe_disposition": "INCLUDE",
            "universe_disposition_reason": "IN_ES_CRPB_UNIVERSE",
            "issuer_lei": EXPECTED_LEI,
            "normalized_evidence_json": json.dumps(
                ev, sort_keys=True, separators=(",", ":")),
        }

    pubs = [row(rec_orig.to_evidence(), "d1.json", "a" * 64,
                "2026-09-17T10:00:00.000000Z"),
            row(ev_amend, "d2.json", "b" * 64,
                "2026-09-18T10:00:00.000000Z")]
    chains = build_chains(indexed, pubs)
    tape = tmp_path / "tape"
    write_parquet(pubs, PUB_COLUMNS, PUB_DUCK_TYPES,
                  tape / "publications.parquet", pub_sort_key)
    write_parquet(chains, CHAIN_COLUMNS, CHAIN_DUCK_TYPES,
                  tape / "trade_chains.parquet", lambda r: r["chain_id"])

    spec = {"ISIN-A": {}}
    rows_b = _rows_bytes(spec)
    links_b = _links_bytes(spec)
    rows_sha = hashlib.sha256(rows_b).hexdigest()
    links_sha = hashlib.sha256(links_b).hexdigest()
    (tmp_path / "g1b_rows.jsonl").write_bytes(rows_b)
    (tmp_path / "g1b_links.jsonl").write_bytes(links_b)
    basis = reference_basis_id(
        {"fitrs": FITRS_SD, "firds": FIRDS_SD, "gleif": GLEIF_SD})
    g1b_doc = {
        "dispositions": {"rows_sha256": rows_sha},
        "reference_sets": {
            "fitrs": {"set_digest": FITRS_SD, "entries": [
                {"name": "fulncr_a.zip", "sha256": "aa" * 32,
                 "bytes": 100}]},
            "firds": {"set_digest": FIRDS_SD, "entries": [
                {"name": "fulins_b.zip", "sha256": "bb" * 32,
                 "bytes": 200}]},
            "gleif": {"set_digest": GLEIF_SD, "entries": [
                {"name": f"{EXPECTED_LEI}.json", "sha256": "ee" * 32,
                 "bytes": 300}]},
        },
        "reference_basis_id": basis,
        "reference_links": {"file": "g1b_links.jsonl",
                            "sha256": links_sha},
    }
    g1b_path = tmp_path / "g1b.yaml"
    g1b_path.write_bytes(yaml.safe_dump(g1b_doc).encode())
    ds = dataset_semantic_digest(
        semantic_digest(pubs, PUB_COLUMNS, pub_sort_key), len(pubs),
        semantic_digest(chains, CHAIN_COLUMNS, lambda r: r["chain_id"]),
        len(chains))
    c2_path = tmp_path / "c2.yaml"
    c2_path.write_bytes(yaml.safe_dump({
        "tape_schema_version": 2,
        "tape_materialization_v2": {"dataset_semantic_digest": ds}
    }).encode())
    kw = {
        "c2_report_path": c2_path,
        "g1b_rows_path": tmp_path / "g1b_rows.jsonl",
        "g1b_report_path": g1b_path,
        "g1b_links_path": tmp_path / "g1b_links.jsonl",
        "expected_c2_report_sha256": hashlib.sha256(
            c2_path.read_bytes()).hexdigest(),
        "expected_dataset_digest": ds,
        "expected_g1b_digest": rows_sha,
        "expected_g1b_report_sha256": hashlib.sha256(
            g1b_path.read_bytes()).hexdigest(),
        "expected_g1b_basis_id": basis,
        "expected_g1b_links_sha256": links_sha,
    }

    pid_orig = row_publication_id(pubs[0])
    pid_new = row_publication_id(pubs[1])
    assert pid_orig != pid_new
    cap = explain_publication(tape, pid_new, **kw)
    # provenance per member is separate: head cites the post-transition
    # source field name verbatim from its stored mapping
    assert cap["economic_provenance"]["field_source_map"]["price"] \
        == "PostTradePx_2026"
    cap_orig = explain_publication(tape, pid_orig, **kw)
    assert cap_orig["economic_provenance"]["field_source_map"]["price"] \
        == "price"
    # the capsule asserts no ownership of upstream regulatory payloads
    reg = cap["reference_authority"]["regulatory"]
    assert reg["upstream_reference_payloads_owned"] is False
    assert b"ARTICLE_26" not in capsule_bytes(cap).upper()
    # reference authority is the pinned set, independent of the
    # publication's schema generation
    assert cap["reference_authority"]["basis"] == "BUILD_SNAPSHOT"


# ---------------- 15-18. fail-closed identity / temporal / tape --------

def test_unknown_publication_id_fails_closed(tmp_path):
    tape, _p, _c, kw = _one_pub(tmp_path)
    with pytest.raises(G1C3Error, match="unknown publication_id"):
        explain_publication(tape, "0" * 64, **kw)


def test_duplicate_publication_identity_fails_closed(tmp_path):
    _tape, pubs, chains, kw = _one_pub(tmp_path)
    dup = [*pubs, dict(pubs[0])]            # same occurrence identity
    tape2 = tmp_path / "tape2"
    write_parquet(dup, PUB_COLUMNS, PUB_DUCK_TYPES,
                  tape2 / "publications.parquet", pub_sort_key)
    write_parquet(chains, CHAIN_COLUMNS, CHAIN_DUCK_TYPES,
                  tape2 / "trade_chains.parquet",
                  lambda r: r["chain_id"])
    kw2 = _rebind_c2(kw, tmp_path, dup, chains, "dup")
    with pytest.raises(G1C3Error, match="not unique"):
        explain_publication(tape2, row_publication_id(pubs[0]), **kw2)


def test_unknown_chain_id_fails_closed(tmp_path):
    tape, _p, _c, kw = _one_pub(tmp_path)
    with pytest.raises(G1C3Error, match="unknown chain_id"):
        explain_chain(tape, f"{BME}|NOPE", **kw)
    with pytest.raises(G1C3Error, match="not knowable"):
        explain_chain(tape, f"{BME}|NOPE", at=T1, **kw)


def test_naive_and_bad_timestamps_fail_closed(tmp_path):
    tape, _p, _c, kw = _three_stage(tmp_path)
    cid = _chain_id(tape)
    with pytest.raises(G1C3Error):
        explain_chain(tape, cid, at="2026-09-17T12:00:00", **kw)   # naive
    with pytest.raises(G1C3Error):
        explain_chain(tape, cid, at="not-a-date", **kw)
    with pytest.raises(G1C3Error):
        explain_chain(tape, cid, at=datetime(2026, 9, 17, 12, 0, 0),
                      **kw)                                     # naive dt
    with pytest.raises(G1C3Error):
        explain_chain(tape, cid, at="", **kw)


def test_tape_digest_mismatch_fails_closed(tmp_path):
    tape, pubs, _c, kw = _one_pub(tmp_path)
    tape2 = tmp_path / "tape2"
    pubs2 = [dict(pubs[0], price="0.01")]    # tamper economic field
    write_parquet(pubs2, PUB_COLUMNS, PUB_DUCK_TYPES,
                  tape2 / "publications.parquet", pub_sort_key)
    shutil.copy(tape / "trade_chains.parquet", tape2 / "trade_chains.parquet")
    with pytest.raises(G1C3Error, match="dataset semantic digest"):
        explain_publication(tape2, row_publication_id(pubs2[0]), **kw)


def test_missing_schema_v2_column_fails_closed(tmp_path):
    _tape, pubs, chains, kw = _one_pub(tmp_path)
    tape2 = tmp_path / "tape2"
    stripped = [{k: v for k, v in r.items()
                 if k != "normalized_evidence_json"} for r in pubs]
    cols = [c for c in PUB_COLUMNS if c != "normalized_evidence_json"]
    write_parquet(stripped, cols, PUB_DUCK_TYPES,
                  tape2 / "publications.parquet", pub_sort_key)
    write_parquet(chains, CHAIN_COLUMNS, CHAIN_DUCK_TYPES,
                  tape2 / "trade_chains.parquet",
                  lambda r: r["chain_id"])
    with pytest.raises(G1C3Error):
        explain_publication(tape2, row_publication_id(pubs[0]), **kw)


# ---------------- 19/20. bijection + future-evidence guards ------------

def test_chain_member_resolution_is_bijective(tmp_path):
    """Every capsule member resolves to exactly one persisted occurrence
    identity — publication_id uniqueness is asserted across members."""
    tape, _p, _c, kw = _three_stage(tmp_path)
    cid = _chain_id(tape)
    cap = explain_chain(tape, cid, at=T3, **kw)
    pids = [m["publication_id"] for m in cap["lifecycle"]["members"]]
    assert len(set(pids)) == 3
    assert all(m["entry_id"] for m in cap["lifecycle"]["members"])


def test_no_future_evidence_anywhere_in_asof_capsule(tmp_path):
    tape, pubs, _c, kw = _three_stage(tmp_path)
    cid = _chain_id(tape)
    cap = explain_chain(tape, cid, at=T2, **kw)
    blob = capsule_bytes(cap).decode()
    t3_row = next(r for r in pubs
                  if r["known_at"] > "2026-09-17T12:00:00.000000Z")
    # the T3 occurrence's identity and capture can never be cited at T2
    assert row_publication_id(t3_row) not in blob
    assert t3_row["capture_version"] not in blob
    assert t3_row["ledger_entry_id"] not in blob
    for m in cap["lifecycle"]["members"]:
        assert m["known_at"] <= "2026-09-17T12:00:00.000000Z"


# ---------------- 21. pinned fixture digest (cross-platform anchor) -----

# Pinned digest over the canonical fixture capsule (generator commit
# pinned to "0"*40 via monkeypatch). The same assertion runs on every
# self-hosted CI leg — a Windows/Linux divergence fails the suite.
PINNED_FIXTURE_CAPSULE_SHA256 = \
    "0a257ce52b665c11c1c5fdf01815aaede3cfbe72967a1f10661e947bd770489a"
PINNED_FIXTURE_PUBLICATION_ID = \
    "d7ea5e039a3602b270379b68a2ae7fe8e02c28320384039a7de8d9f654d24456"


def test_fixture_capsule_digest_is_platform_stable(tmp_path, monkeypatch):
    """Cross-platform byte determinism: this pinned digest must reproduce
    identically on every OS/Python runner (self-hosted CI asserts it)."""
    monkeypatch.setattr("metrics.g1c3._git_head", lambda: "0" * 40)
    tape, pubs, _c, kw = _one_pub(tmp_path)
    pid = row_publication_id(pubs[0])
    assert pid == PINNED_FIXTURE_PUBLICATION_ID
    cap = explain_publication(tape, pid, **kw)
    assert cap["capsule_sha256"] == PINNED_FIXTURE_CAPSULE_SHA256
    body = capsule_bytes(cap)
    assert body.endswith(b"\n")
    assert json.loads(body) == cap


# ---------------- report generation ------------------------------------

def test_report_generation_metadata_only(tmp_path):
    tape, pubs, _c, kw = _three_stage(tmp_path)
    cid = _chain_id(tape)
    rep = {
        "publication_ids": [row_publication_id(pubs[0])],
        "chain_ids": [cid],
        "asof_chain_ids": [cid],
    }
    report, capsules = build_report(
        tape, asof_timestamps=[T1, T2, T3], representatives=rep,
        date="d", generated_utc="t", **kw)
    assert report["issue_id"] == "G1-C3"
    assert report["determinism"]["repeated_request_byte_identical"]
    assert len(report["representative_capsules"]) == len(capsules)

    path = generate_report(
        tape, asof_timestamps=[T1, T2, T3], representatives=rep,
        evidence_dir=tmp_path / "ev", date="d", generated_utc="t",
        **kw)
    assert path.is_file()
    cap_files = list((tmp_path / "ev" / "g1-c3" / "capsules").glob("*.json"))
    assert cap_files
    for f in cap_files:
        data = f.read_bytes()
        cap = json.loads(data)
        assert cap["capsule_sha256"] == capsule_sha256(cap)


def test_cli_row_and_chain(tmp_path, capsysbinary, monkeypatch):
    """`python -m metrics.g1c3 --row/--chain` emits exact capsule bytes.

    The CLI resolves authority paths via flags; expected_* production
    constants are monkeypatched to the synthetic digests so the CLI path
    exercises the same authentication flow the library tests use."""
    tape, pubs, _c, kw = _three_stage(tmp_path)
    import metrics.g1c3 as g1c3
    monkeypatch.setattr(
        g1c3, "EXPECTED_C2_REPORT_SHA256", kw["expected_c2_report_sha256"])
    monkeypatch.setattr(
        g1c3, "EXPECTED_DATASET_SEMANTIC_DIGEST",
        kw["expected_dataset_digest"])
    monkeypatch.setattr(
        g1c3, "EXPECTED_G1B_ROWS_DIGEST", kw["expected_g1b_digest"])
    monkeypatch.setattr(
        g1c3, "EXPECTED_G1B_REPORT_SHA256",
        kw["expected_g1b_report_sha256"])
    monkeypatch.setattr(
        g1c3, "EXPECTED_G1B_BASIS_ID", kw["expected_g1b_basis_id"])
    monkeypatch.setattr(
        g1c3, "EXPECTED_G1B_LINKS_SHA256", kw["expected_g1b_links_sha256"])

    pid = row_publication_id(pubs[0])
    argv = ["--tape-dir", str(tape),
            "--c2-report", str(kw["c2_report_path"]),
            "--g1b-rows", str(kw["g1b_rows_path"]),
            "--g1b-report", str(kw["g1b_report_path"]),
            "--g1b-links", str(kw["g1b_links_path"])]
    capsysbinary.readouterr()
    assert g1c3.main([*argv, "--row", pid]) == 0
    out = capsysbinary.readouterr().out
    lib = explain_publication(tape, pid, **kw)
    assert out == capsule_bytes(lib)

    cid = _chain_id(tape)
    assert g1c3.main([*argv, "--chain", cid]) == 0
    out2 = capsysbinary.readouterr().out
    assert out2 == capsule_bytes(explain_chain(tape, cid, **kw))

    assert g1c3.main([*argv, "--chain", cid, "--at", T2]) == 0
    out3 = capsysbinary.readouterr().out
    assert out3 == capsule_bytes(explain_chain(tape, cid, at=T2, **kw))
