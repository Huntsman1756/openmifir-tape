"""Tests for metrics.g1c4 — G1-C4 TAPE_MANIFEST + offline verify +
reproducibility gate (docs/gates/G1.md §4.4/§4.5).

Adversarial inventory (mandate §15):

1.  pristine tape -> self verify PASS
2.  mutate publications logical content -> FAIL
3.  mutate chains logical content -> FAIL
4.  mutate TAPE_MANIFEST -> FAIL
5.  mutate declared dataset digest -> FAIL
6.  mutate one FITRS byte -> full provenance FAIL
7.  mutate one FIRDS byte -> FAIL
8.  mutate one GLEIF byte -> FAIL
9.  replace input bundle (mutate a raw/manifest byte) -> FAIL
10. remove one reference artifact -> FAIL
11. add unexpected artifact (exact-set family) -> FAIL
12. same sealed inputs, roots A/B -> same semantic digests
13. same requests on roots A/B -> byte-identical capsules
14. parquet bytes differ but semantic digest same -> PASS
15. self-verify without --bundle -> must NOT claim full provenance
16. path/timestamp differences do not alter semantic identity
17. wrong generator/schema authority -> FAIL
18. tampered capsule fixture -> FAIL
19. missing manifest -> FAIL; malformed -> FAIL; wrong schema -> FAIL
20. partial provenance args (some but not all dirs) -> FAIL (CLI)
21. mutated eligible manifest bytes inside bundle -> FAIL
22. extra unlisted file inside sealed bundle -> FAIL
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml
from test_g1c1 import EXPECTED_LEI
from test_g1c3 import (
    _links_bytes,
    _rows_bytes,
)
from test_metrics import BME, WINDOW, _bme, _manifest, _store

from metrics.g1b import reference_basis_id
from metrics.g1c1 import PUB_COLUMNS, PUB_DUCK_TYPES, pub_sort_key, write_parquet
from metrics.g1c3 import capsule_bytes, explain_publication, row_publication_id
from metrics.g1c4 import (
    MANIFEST_NAME,
    G1C4Error,
    build_tape_manifest,
    replay_materialization,
    reproducibility_check,
    verify_full,
    verify_self,
    write_manifest,
)
from metrics.g1c4 import (
    main as c4_main,
)
from universe.reference import reference_entry, reference_set_from_files

PIN = "ab" * 20          # deterministic fake code_commit for tests

T_ASOF = "2026-09-17T14:30:00Z"


# ------------------------------------------------------------- fixtures --

def _write_sums(bundle: Path) -> Path:
    """Seal the synthetic bundle: GNU-style SHA256SUMS over every file."""
    lines = []
    for p in sorted(bundle.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(bundle)).replace("\\", "/")
            lines.append(
                f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {rel}")
    sums = bundle / "SHA256SUMS.txt"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sums


def _ref_files(tmp_path):
    """Tiny synthetic reference snapshots (FITRS zip, FIRDS zip, GLEIF
    per-LEI artifact)."""
    fitrs = tmp_path / "fitrs"
    firds = tmp_path / "firds"
    gleif = tmp_path / "gleif"
    for d in (fitrs, firds, gleif):
        d.mkdir(parents=True)
    (fitrs / "fulncr_a.zip").write_bytes(b"FITRS-SNAPSHOT-A")
    (firds / "fulins_b.zip").write_bytes(b"FIRDS-SNAPSHOT-B")
    (gleif / f"{EXPECTED_LEI}.json").write_bytes(b'{"lei":"x"}')
    return fitrs, firds, gleif


def _c4_env(tmp_path, populate, isin_specs, *, mutate_manifest=None,
            subjects=("pub", "chain", "asof")):
    """Synthetic sealed input + references + tape + TAPE_MANIFEST.

    Layout:
        bundle/{runs,raw} + SHA256SUMS.txt   (sealed input)
        fitrs/ firds/ gleif/                 (reference snapshots)
        tape/{publications,trade_chains}.parquet + TAPE_MANIFEST.yaml
    Returns (tape, kw) where kw carries g1c3 explain authority bindings.
    """
    bundle = tmp_path / "bundle"
    raw = bundle / "raw"
    runs = bundle / "runs"
    runs.mkdir(parents=True)
    populate(raw, runs)
    _write_sums(bundle)

    fitrs_dir, firds_dir, gleif_dir = _ref_files(tmp_path / "refs")
    fitrs_set = reference_set_from_files(
        sorted(fitrs_dir.iterdir()))
    firds_set = reference_set_from_files(
        sorted(firds_dir.iterdir()))
    gleif_set = {"entries": [
        reference_entry(f.name, f.read_bytes())
        for f in sorted(gleif_dir.iterdir())]}
    from universe.reference import canonical_reference_set
    gleif_set = canonical_reference_set(gleif_set["entries"])
    set_digests = {"fitrs": fitrs_set["set_digest"],
                   "firds": firds_set["set_digest"],
                   "gleif": gleif_set["set_digest"]}
    basis = reference_basis_id(set_digests)

    tape, pub_rows, chain_rows = _tape_from_selection(
        raw, runs, isin_specs, tmp_path / "tape")

    # synthetic g1b authority bound to the REAL computed digests
    rows_b = _rows_bytes(isin_specs)
    links_b = _links_bytes(isin_specs)
    g1b_rows = tmp_path / "g1b_rows.jsonl"
    g1b_links = tmp_path / "g1b_links.jsonl"
    g1b_report = tmp_path / "g1b.yaml"
    g1b_rows.write_bytes(rows_b)
    g1b_links.write_bytes(links_b)
    doc = {
        "dispositions": {
            "rows_sha256": hashlib.sha256(rows_b).hexdigest()},
        "reference_sets": {"fitrs": fitrs_set, "firds": firds_set,
                           "gleif": gleif_set},
        "reference_basis_id": basis,
        "reference_links": {"file": g1b_links.name,
                            "sha256": hashlib.sha256(
                                links_b).hexdigest()},
    }
    g1b_report.write_bytes(yaml.safe_dump(doc).encode())

    # synthetic G1-A observed-set artifact bound to the real digest
    from metrics.g1a import digest_entries
    instruments = [{"isin": i} for i in sorted(isin_specs)]
    g1a = tmp_path / "g1a.yaml"
    g1a.write_bytes(yaml.safe_dump({
        "observed_instrument_set": {
            "instruments": instruments,
            "instrument_count": len(instruments),
            "semantic_digest": digest_entries(instruments)}}).encode())

    from metrics.g1c1 import CHAIN_COLUMNS, dataset_semantic_digest, semantic_digest
    pub_d = semantic_digest(pub_rows, PUB_COLUMNS, pub_sort_key)
    chain_d = semantic_digest(chain_rows, CHAIN_COLUMNS,
                              lambda r: r["chain_id"])
    ds = dataset_semantic_digest(pub_d, len(pub_rows), chain_d,
                                 len(chain_rows))
    c2 = tmp_path / "c2.yaml"
    c2.write_bytes(yaml.safe_dump(
        {"tape_schema_version": 2,
         "tape_materialization_v2": {"dataset_semantic_digest": ds}}
    ).encode())

    explain_kw = {
        "c2_report_path": c2,
        "g1b_rows_path": g1b_rows,
        "g1b_report_path": g1b_report,
        "g1b_links_path": g1b_links,
        "expected_c2_report_sha256": hashlib.sha256(
            c2.read_bytes()).hexdigest(),
        "expected_dataset_digest": ds,
        "expected_g1b_digest": hashlib.sha256(rows_b).hexdigest(),
        "expected_g1b_report_sha256": hashlib.sha256(
            g1b_report.read_bytes()).hexdigest(),
        "expected_g1b_basis_id": basis,
        "expected_g1b_links_sha256": hashlib.sha256(
            links_b).hexdigest(),
    }

    chain_id = chain_rows[0]["chain_id"]
    pub_id = row_publication_id(pub_rows[0])
    subs = []
    if "pub" in subjects:
        subs.append({"type": "publication", "publication_id": pub_id})
    if "chain" in subjects:
        subs.append({"type": "chain", "chain_id": chain_id})
    if "asof" in subjects:
        subs.append({"type": "chain", "chain_id": chain_id,
                     "at": T_ASOF})

    # Pin generator commits INSIDE the artifacts: capsule fixtures and
    # the manifest's declared code_commit are all computed under PIN, so
    # the declared fixture digests attest commit PIN.
    from unittest import mock
    with mock.patch("metrics.g1c3._git_head", lambda: PIN), \
            mock.patch("metrics.g1c4._git_head", lambda: PIN):
        manifest = build_tape_manifest(
            tape, runs_dir=runs, raw_root=raw, bundle_dir=bundle,
            fitrs_paths=sorted(fitrs_dir.iterdir()),
            firds_paths=sorted(firds_dir.iterdir()),
            gleif_dir=gleif_dir, g1b_report_path=g1b_report,
            window=WINDOW, capsule_subjects=subs, code_commit=PIN,
            expected_g1b_report_sha256=explain_kw[
                "expected_g1b_report_sha256"],
            expected_g1b_basis_id=basis, explain_kw=explain_kw)
    if mutate_manifest:
        manifest = mutate_manifest(manifest)
    write_manifest(tape, manifest)

    return {
        "tape": tape, "bundle": bundle, "runs": runs, "raw": raw,
        "fitrs_dir": fitrs_dir, "firds_dir": firds_dir,
        "gleif_dir": gleif_dir, "pub_rows": pub_rows,
        "chain_rows": chain_rows, "chain_id": chain_id,
        "pub_id": pub_id, "explain_kw": explain_kw,
        "manifest": manifest, "g1a": g1a,
        "g1b_rows": g1b_rows, "g1b_report": g1b_report,
        "g1b_links": g1b_links,
        "basis": basis, "dataset_digest": ds,
        "c1_expected": {
            "expected_g1a_digest": digest_entries(instruments),
            "expected_g1b_digest": hashlib.sha256(rows_b).hexdigest(),
            "expected_g1b_report_sha256": explain_kw[
                "expected_g1b_report_sha256"],
            "expected_g1b_basis_id": basis},
    }


def _tape_from_selection(raw, runs, isin_specs, tape_dir):
    """Materialize a tape from the same raw+runs the C1 build consumes."""
    from datetime import datetime

    from metrics.g1c1 import CHAIN_COLUMNS, CHAIN_DUCK_TYPES, build_chains, build_publications
    from metrics.selection import select_eligible
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
    write_parquet(pub_rows, PUB_COLUMNS, PUB_DUCK_TYPES,
                  tape_dir / "publications.parquet", pub_sort_key)
    write_parquet(chain_rows, CHAIN_COLUMNS, CHAIN_DUCK_TYPES,
                  tape_dir / "trade_chains.parquet",
                  lambda r: r["chain_id"])
    return tape_dir, pub_rows, chain_rows


def _populate_one(raw, runs):
    o = _store(raw, BME, "2026-09-17", "d.json", [_bme()])
    _manifest(runs, "20260917T100000Z", BME,
              "2026-09-17T10:00:00Z", [o])


def _basic_env(tmp_path, **kw):
    return _c4_env(tmp_path, _populate_one, {"ISIN-A": {}}, **kw)


# -------------------------------------------------------- self-verify --

def test_pristine_self_verify(tmp_path):
    env = _basic_env(tmp_path)
    res = verify_self(env["tape"], running_commit=PIN,
                      explain_kw=env["explain_kw"])
    assert res["self_verification"] == "PASS"
    assert res["full_provenance"].startswith("NOT_RUN")
    assert all(c["result"] == "PASS" for c in res["checks"])


def test_missing_manifest_fails(tmp_path):
    env = _basic_env(tmp_path)
    (env["tape"] / MANIFEST_NAME).unlink()
    with pytest.raises(G1C4Error, match="missing"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_malformed_manifest_fails(tmp_path):
    env = _basic_env(tmp_path)
    (env["tape"] / MANIFEST_NAME).write_bytes(b": not yaml [")
    with pytest.raises(G1C4Error, match=r"malformed|mapping"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_wrong_manifest_schema_fails(tmp_path):
    env = _basic_env(tmp_path, mutate_manifest=lambda m: {
        **m, "tape_manifest_schema_version": 99})
    with pytest.raises(G1C4Error, match="schema version"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_mutated_publications_table_fails(tmp_path):
    env = _basic_env(tmp_path)
    rows = [dict(r, price="0.0") if i == 0 else r
            for i, r in enumerate(env["pub_rows"])]
    write_parquet(rows, PUB_COLUMNS, PUB_DUCK_TYPES,
                  env["tape"] / "publications.parquet", pub_sort_key)
    with pytest.raises(G1C4Error, match=r"semantic_digest|mismatch"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_mutated_chains_table_fails(tmp_path):
    env = _basic_env(tmp_path)
    from metrics.g1c1 import CHAIN_COLUMNS, CHAIN_DUCK_TYPES
    rows = [dict(r, head_state="CANCELLED") if i == 0 else r
            for i, r in enumerate(env["chain_rows"])]
    write_parquet(rows, CHAIN_COLUMNS, CHAIN_DUCK_TYPES,
                  env["tape"] / "trade_chains.parquet",
                  lambda r: r["chain_id"])
    with pytest.raises(G1C4Error, match="mismatch"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_mutated_declared_dataset_digest_fails(tmp_path):
    env = _basic_env(tmp_path, mutate_manifest=lambda m: {
        **m, "outputs": {**m["outputs"],
                         "dataset_semantic_digest": "0" * 64}})
    with pytest.raises(G1C4Error, match="dataset_semantic_digest"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_mutated_declared_row_count_fails(tmp_path):
    env = _basic_env(tmp_path, mutate_manifest=lambda m: {
        **m, "outputs": {**m["outputs"],
                         "publications_rows":
                             m["outputs"]["publications_rows"] + 1}})
    with pytest.raises(G1C4Error, match="publications_rows"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_malformed_generator_commit_fails(tmp_path):
    """generator.code_commit is a provenance record — it must be a
    well-formed 40-hex commit; malformed authority fails closed."""
    env = _basic_env(tmp_path, mutate_manifest=lambda m: {
        **m, "generator": {**m["generator"], "code_commit": "tampered"}})
    with pytest.raises(G1C4Error, match=r"generator.*commit"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])
    env2 = _basic_env(tmp_path / "other",
                      mutate_manifest=lambda m: {
                          **m, "generator": {
                              **m["generator"],
                              "tape_schema_version": 1}})
    with pytest.raises(G1C4Error, match=r"schema_version"):
        verify_self(env2["tape"], running_commit=PIN,
                    explain_kw=env2["explain_kw"])


def test_verify_at_different_commit_still_verifies_fixtures(tmp_path):
    """Design fix: the verifier does NOT require HEAD ==
    declared code_commit — but the DECLARED fixture digests are still
    verified via commit normalization. An intact fixture passes with
    code_commit_match=false."""
    env = _basic_env(tmp_path)
    res = verify_self(env["tape"], running_commit="cd" * 20,
                      explain_kw=env["explain_kw"])
    assert res["self_verification"] == "PASS"
    gen = next(c for c in res["checks"]
               if c["check"] == "generator_authority")
    assert gen["code_commit_match"] is False
    fx = next(c for c in res["checks"]
              if c["check"] == "capsule_fixtures")
    assert "normalized" in fx["mode"]


def test_tampered_fixture_at_other_commit_fails(tmp_path):
    """Mandated adversarial: running_commit B != declared generator A,
    declared capsule digest = syntactically valid but wrong -> MUST
    FAIL. The declared digest is always verified."""
    def flip(m):
        fx = [dict(f) for f in m["capsule_fixtures"]]
        fx[0]["capsule_sha256"] = "a" * 64
        return {**m, "capsule_fixtures": fx}
    env = _basic_env(tmp_path, mutate_manifest=flip)
    with pytest.raises(G1C4Error, match=r"declared capsule_sha256"):
        verify_self(env["tape"], running_commit="cd" * 20,
                    explain_kw=env["explain_kw"])


def test_references_metadata_tampering_fails(tmp_path):
    """Adversarial: mutating ONLY references metadata (no table content)
    MUST fail self-verification — internal consistency of the declared
    reference authority is part of layer A."""
    for mutate, pattern in (
            (lambda m: {**m, "references": {
                **m["references"],
                "fitrs": {**m["references"]["fitrs"],
                          "set_digest": "0" * 64}}},
             r"fitrs.*set_digest"),
            (lambda m: {**m, "references": {
                **m["references"], "basis_id": "0" * 64}},
             r"basis_id"),
            (lambda m: {**m, "references": {
                **m["references"],
                "gleif": {**m["references"]["gleif"],
                          "entries":
                              m["references"]["gleif"]["entries"][1:]}},
             }, r"gleif.*set_digest"),
    ):
        env = _basic_env(tmp_path / str(abs(hash(pattern))),
                         mutate_manifest=mutate)
        with pytest.raises(G1C4Error, match=pattern):
            verify_self(env["tape"], running_commit=PIN,
                        explain_kw=env["explain_kw"])


def _rewrite_parquet_with(tape, table, columns, duck_types, rows,
                          sort_key):
    write_parquet(rows, columns, duck_types, tape / table, sort_key)


def test_extra_column_fails_physical_schema(tmp_path):
    """A Parquet carrying an unexpected extra column is a tamper —
    logical content may be untouched but the declared schema set is
    exact."""
    env = _basic_env(tmp_path)
    cols = [*PUB_COLUMNS, "rogue_col"]
    rows = [dict(r, rogue_col="x") for r in env["pub_rows"]]
    _rewrite_parquet_with(env["tape"], "publications.parquet", cols,
                          {**PUB_DUCK_TYPES}, rows, pub_sort_key)
    with pytest.raises(G1C4Error, match=r"physical schema.*extra"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_missing_column_fails_physical_schema(tmp_path):
    env = _basic_env(tmp_path)
    cols = [c for c in PUB_COLUMNS if c != "currency"]
    rows = [{k: v for k, v in r.items() if k != "currency"}
            for r in env["pub_rows"]]
    _rewrite_parquet_with(env["tape"], "publications.parquet", cols,
                          PUB_DUCK_TYPES, rows, pub_sort_key)
    with pytest.raises(G1C4Error, match=r"physical schema"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_column_type_drift_fails_physical_schema(tmp_path):
    env = _basic_env(tmp_path)
    # redeclare observation_count as VARCHAR -> type drift
    dt = {**PUB_DUCK_TYPES, "observation_count": "VARCHAR"}
    _rewrite_parquet_with(env["tape"], "publications.parquet",
                          PUB_COLUMNS, dt, env["pub_rows"], pub_sort_key)
    with pytest.raises(G1C4Error, match=r"physical schema"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_tampered_capsule_fixture_fails(tmp_path):
    def flip(m):
        fx = [dict(f) for f in m["capsule_fixtures"]]
        fx[0]["capsule_sha256"] = "0" * 64
        return {**m, "capsule_fixtures": fx}
    env = _basic_env(tmp_path, mutate_manifest=flip)
    with pytest.raises(G1C4Error, match="capsule fixture"):
        verify_self(env["tape"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_self_verify_never_claims_provenance(tmp_path):
    """Adversarial #15: a bare self-verify MUST NOT claim the input
    bytes were authenticated."""
    env = _basic_env(tmp_path)
    res = verify_self(env["tape"], running_commit=PIN,
                      explain_kw=env["explain_kw"])
    assert res["full_provenance"] != "PASS"
    assert "NOT_RUN" in res["full_provenance"]


def test_cross_root_capsule_and_digests_identical(tmp_path):
    """Adversarial #12/#13/#16: the same sealed input materialized in a
    second root produces identical semantic digests and byte-identical
    capsules — paths never enter identity."""
    env_a = _basic_env(tmp_path / "a")
    env_b = _basic_env(tmp_path / "b")
    from metrics.g1c4 import tape_outputs
    assert tape_outputs(env_a["tape"]) == tape_outputs(env_b["tape"])
    for subj in ({"type": "publication",
                  "publication_id": env_a["pub_id"]},
                 {"type": "chain", "chain_id": env_a["chain_id"]},
                 {"type": "chain", "chain_id": env_a["chain_id"],
                  "at": T_ASOF}):
        cap_a = _explain(env_a, subj)
        cap_b = _explain(env_b, subj)
        assert capsule_bytes(cap_a) == capsule_bytes(cap_b)


def _explain(env, subj):
    kw = env["explain_kw"]
    if subj["type"] == "publication":
        return explain_publication(env["tape"], subj["publication_id"],
                                   **kw)
    from metrics.g1c3 import explain_chain
    return explain_chain(env["tape"], subj["chain_id"],
                         at=subj.get("at"), **kw)


def test_parquet_bytes_may_differ_semantics_same(tmp_path):
    """Adversarial #14: the tape's semantic identity does not depend on
    Parquet physical bytes. Rewriting the same logical rows — bytes may
    or may not differ (writer metadata) — MUST keep self-verify PASS."""
    env = _basic_env(tmp_path)
    write_parquet(env["pub_rows"], PUB_COLUMNS, PUB_DUCK_TYPES,
                  env["tape"] / "publications.parquet", pub_sort_key)
    res = verify_self(env["tape"], running_commit=PIN,
                      explain_kw=env["explain_kw"])
    assert res["self_verification"] == "PASS"


# ------------------------------------------------------ full provenance --

def _full(env):
    return verify_full(
        env["tape"], bundle_dir=env["bundle"],
        fitrs_paths=sorted(env["fitrs_dir"].iterdir()),
        firds_paths=sorted(env["firds_dir"].iterdir()),
        gleif_dir=env["gleif_dir"], running_commit=PIN,
        explain_kw=env["explain_kw"])


def test_full_provenance_pristine(tmp_path):
    env = _basic_env(tmp_path)
    res = _full(env)
    assert res["self_verification"] == "PASS"
    assert res["full_provenance"] == "PASS"
    names = {c["check"] for c in res["checks"]}
    assert {"a3_bundle_bytes", "eligible_manifest_set",
            "fitrs_reference_set", "firds_reference_set",
            "gleif_reference_set"} <= names


def test_mutated_fitrs_byte_fails(tmp_path):
    env = _basic_env(tmp_path)
    f = env["fitrs_dir"] / "fulncr_a.zip"
    f.write_bytes(f.read_bytes()[:-1] + b"X")
    with pytest.raises(G1C4Error, match=r"fitrs"):
        _full(env)


def test_mutated_firds_byte_fails(tmp_path):
    env = _basic_env(tmp_path)
    f = env["firds_dir"] / "fulins_b.zip"
    f.write_bytes(f.read_bytes()[:-1] + b"X")
    with pytest.raises(G1C4Error, match=r"firds"):
        _full(env)


def test_mutated_gleif_byte_fails(tmp_path):
    env = _basic_env(tmp_path)
    f = env["gleif_dir"] / f"{EXPECTED_LEI}.json"
    f.write_bytes(f.read_bytes()[:-1] + b"X")
    with pytest.raises(G1C4Error, match=r"GLEIF|gleif"):
        _full(env)


def test_mutated_bundle_raw_byte_fails(tmp_path):
    env = _basic_env(tmp_path)
    victim = next(env["raw"].rglob("*"))
    victim = next(p for p in env["raw"].rglob("*") if p.is_file())
    victim.write_bytes(victim.read_bytes() + b"\x00")
    with pytest.raises(G1C4Error, match=r"bundle file"):
        _full(env)


def test_mutated_eligible_manifest_fails(tmp_path):
    env = _basic_env(tmp_path)
    m = next(env["runs"].glob("*__manifest.yaml"))
    m.write_bytes(m.read_bytes() + b"# tampered\n")
    with pytest.raises(G1C4Error, match=r"bundle file|eligible"):
        _full(env)


def test_extra_unlisted_bundle_file_fails(tmp_path):
    env = _basic_env(tmp_path)
    (env["bundle"] / "runs" / "unexpected.bin").write_bytes(b"extra")
    with pytest.raises(G1C4Error, match=r"unlisted|not exact"):
        _full(env)


def test_missing_reference_artifact_fails(tmp_path):
    env = _basic_env(tmp_path)
    missing = env["fitrs_dir"] / "fulncr_a.zip"
    files = [p for p in sorted(env["fitrs_dir"].iterdir())
             if p != missing]
    with pytest.raises(G1C4Error, match=r"fitrs"):
        verify_full(env["tape"], bundle_dir=env["bundle"],
                    fitrs_paths=files,
                    firds_paths=sorted(env["firds_dir"].iterdir()),
                    gleif_dir=env["gleif_dir"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_unexpected_reference_artifact_fails(tmp_path):
    env = _basic_env(tmp_path)
    rogue = env["firds_dir"] / "unexpected.zip"
    rogue.write_bytes(b"rogue")
    with pytest.raises(G1C4Error, match=r"firds"):
        _full(env)


def test_missing_gleif_pinned_entry_fails(tmp_path):
    env = _basic_env(tmp_path)
    (env["gleif_dir"] / f"{EXPECTED_LEI}.json").unlink()
    with pytest.raises(G1C4Error, match=r"GLEIF"):
        _full(env)


def test_wrong_bundle_dir_fails(tmp_path):
    env = _basic_env(tmp_path)
    other = tmp_path / "other_bundle"
    shutil.copytree(env["bundle"], other)
    sums = other / "SHA256SUMS.txt"
    sums.write_text("0" * 64 + "  collection-meta.yaml\n")
    with pytest.raises(G1C4Error, match=r"bundle|sha256"):
        verify_full(env["tape"], bundle_dir=other,
                    fitrs_paths=sorted(env["fitrs_dir"].iterdir()),
                    firds_paths=sorted(env["firds_dir"].iterdir()),
                    gleif_dir=env["gleif_dir"], running_commit=PIN,
                    explain_kw=env["explain_kw"])


def test_partial_provenance_cli_fails(tmp_path):
    env = _basic_env(tmp_path)
    with pytest.raises(G1C4Error, match="ALL"):
        c4_main([str(env["tape"]), "--bundle", str(env["bundle"])])


def test_cli_self_verify_fail_closed(tmp_path):
    """CLI entry point surfaces fail-closed errors — malformed declared
    generator commit rejects before any explain work."""
    env = _basic_env(tmp_path, mutate_manifest=lambda m: {
        **m, "generator": {**m["generator"], "code_commit": "x"}})
    with pytest.raises(G1C4Error, match=r"generator"):
        c4_main([str(env["tape"])])


# ------------------------------------------------------ reproducibility --

def test_reproducibility_two_roots(tmp_path):
    """Adversarial #12/#13 via the C4 gate: two materializations of the
    same synthetic corpus produce identical digests and capsules."""
    def env_at(root):
        return _c4_env(root, _populate_one, {"ISIN-A": {}},
                       subjects=("pub", "chain"))

    env_a = env_at(tmp_path / "A")
    env_b = env_at(tmp_path / "B")
    assert env_a["manifest"]["outputs"] == env_b["manifest"]["outputs"]
    assert (env_a["manifest"]["capsule_fixtures"]
            == env_b["manifest"]["capsule_fixtures"])


def _rebuild_for(env):
    """Injectable universe-rebuild for synthetic replays: writes the
    env's synthetic authority artifacts into the replay work dir and
    returns digests — the same contract `_rebuild_universe` fulfils
    with real g1a/g1b builders."""
    def _rb(bundle_dir, fitrs_paths, firds_paths, gleif_dir, window,
            work_dir, *, date, generated_utc, expected_g1a_digest):
        work_dir.mkdir(parents=True, exist_ok=True)
        a = work_dir / "g1a.yaml"
        b_rows = work_dir / "g1b_rows.jsonl"
        b_links = work_dir / "g1b_links.jsonl"
        b_rep = work_dir / "g1b_report.yaml"
        a.write_bytes(env["g1a"].read_bytes())
        b_rows.write_bytes(env["g1b_rows"].read_bytes())
        b_links.write_bytes(env["g1b_links"].read_bytes())
        b_rep.write_bytes(env["g1b_report"].read_bytes())
        return {
            "g1a_path": a,
            "g1a_semantic_digest":
                env["c1_expected"]["expected_g1a_digest"],
            "g1b_rows_path": b_rows,
            "g1b_links_path": b_links,
            "g1b_report_path": b_rep,
            "g1b_rows_digest":
                env["c1_expected"]["expected_g1b_digest"],
            "g1b_links_sha256":
                env["explain_kw"]["expected_g1b_links_sha256"],
            "g1b_report_sha256":
                env["explain_kw"]["expected_g1b_report_sha256"],
            "g1b_basis_id": env["basis"],
        }
    return _rb


def _anchors(env):
    return {
        "g1a_semantic_digest": env["c1_expected"]["expected_g1a_digest"],
        "g1b_rows_digest": env["c1_expected"]["expected_g1b_digest"],
        "g1b_links_sha256":
            env["explain_kw"]["expected_g1b_links_sha256"],
        "g1b_basis_id": env["basis"],
    }


def test_reproducibility_gate_synthetic(tmp_path):
    """§4.5 end-to-end: two independent replays — each rebuilding the
    universe chain from the same sealed corpus — must produce identical
    digests, identical manifests and byte-identical capsules."""
    env = _basic_env(tmp_path / "src")
    subjects = [{"type": "publication",
                 "publication_id": env["pub_id"]},
                {"type": "chain", "chain_id": env["chain_id"]}]
    res = reproducibility_check(
        env["bundle"],
        fitrs_paths=sorted(env["fitrs_dir"].iterdir()),
        firds_paths=sorted(env["firds_dir"].iterdir()),
        gleif_dir=env["gleif_dir"], window=WINDOW,
        replay_roots=[tmp_path / "mA", tmp_path / "mB"],
        capsule_subjects=subjects, date="d", generated_utc="t",
        anchors=_anchors(env), rebuild_universe=_rebuild_for(env),
        c2_authority={
            "report_path": env["explain_kw"]["c2_report_path"],
            "expected_sha256":
                env["explain_kw"]["expected_c2_report_sha256"]},
        code_commit=PIN)
    assert res["tape_reproducible"] is True
    assert res["dataset_semantic_digest_equal"] is True
    assert res["capsule_bytes_byte_identical"] is True
    # every build emits its manifest (§4.4) — and they are identical
    for root in (tmp_path / "mA", tmp_path / "mB"):
        assert (root / "tape" / MANIFEST_NAME).is_file()


def test_replay_anchor_divergence_fails(tmp_path):
    """A replay whose rebuilt upstream digests diverge from the
    canonical anchors is NOT a reproduction — fail before the tape."""
    env = _basic_env(tmp_path / "src")
    anchors = _anchors(env)
    anchors["g1a_semantic_digest"] = "0" * 64
    with pytest.raises(G1C4Error, match=r"upstream replay diverged"):
        replay_materialization(
            env["bundle"],
            fitrs_paths=sorted(env["fitrs_dir"].iterdir()),
            firds_paths=sorted(env["firds_dir"].iterdir()),
            gleif_dir=env["gleif_dir"], window=WINDOW,
            tape_dir=tmp_path / "t", work_dir=tmp_path / "w",
            date="d", generated_utc="t", anchors=anchors,
            rebuild_universe=_rebuild_for(env))


def test_reproducibility_diverged_digest_fails(tmp_path):
    """A materialization producing a different dataset digest fails the
    gate rather than being reported as reproducible."""
    env = _basic_env(tmp_path / "src")
    with pytest.raises(Exception):  # noqa: B017 - fail-closed, any error
        reproducibility_check(
            tmp_path / "empty_bundle",
            fitrs_paths=sorted(env["fitrs_dir"].iterdir()),
            firds_paths=sorted(env["firds_dir"].iterdir()),
            gleif_dir=env["gleif_dir"], window=WINDOW,
            replay_roots=[tmp_path / "x1", tmp_path / "x2"],
            date="d", generated_utc="t",
            anchors=_anchors(env), rebuild_universe=_rebuild_for(env))
