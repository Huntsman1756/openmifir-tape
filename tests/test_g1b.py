"""Offline tests for G1-B1 OBSERVED_UNIVERSE_DISPOSITION (metrics/g1b.py).

Synthetic fixtures only — no provider data, no network. The GLEIF layer is
exercised through pinned local artifacts (<LEI>.json / <LEI>.notfound.json)
and injected getters; the acquisition completeness contract is tested for
all four outcomes.
"""

import hashlib
import json
import urllib.error
import zipfile

import pytest
import yaml

from metrics.g1a import digest_entries
from metrics.g1b import (
    G1BError,
    build_dispositions,
    build_report,
    canonical_links_bytes,
    canonical_rows_bytes,
    load_observed_isins,
    scan_firds,
    scan_fitrs,
)
from universe.gleif_acquire import (
    AcquisitionIncomplete,
    acquire_leis,
    load_acquired_entities,
)
from universe.reference import (
    canonical_reference_set,
    reference_entry,
    reference_set_from_dir,
    reference_set_from_files,
)

LEI_ES = "00000000000000000ES1"[:20].ljust(20, "0")  # 20 chars
LEI_FR = "00000000000000000FR1"[:20].ljust(20, "0")
LEI_BAD = "SHORT"


def _gleif_payload(lei: str, jurisdiction: str | None = "ES") -> bytes:
    entity = {} if jurisdiction is None else {"legalJurisdiction": jurisdiction}
    return json.dumps({"data": {"id": lei, "attributes": {
        "lei": lei, "entity": entity}}}).encode()


def _fitrs_xml(recs) -> bytes:
    """recs: [(isin, mifir_id, sacl|None, desc|None)]"""
    items = []
    for i, (isin, mifir, sacl, desc) in enumerate(recs):
        deriv = ""
        if desc is not None:
            deriv += f"<Desc>{desc}</Desc>"
        if sacl is not None:
            deriv += ("<SgmttnCrit><CritNm>SACL</CritNm>"
                      f"<CritVal>{sacl}</CritVal></SgmttnCrit>")
        deriv_xml = f"<DerivSubClss>{deriv}</DerivSubClss>" if deriv else ""
        mifir_xml = (f"<FinInstrmClssfctn>{mifir}</FinInstrmClssfctn>"
                     if mifir is not None else "")
        items.append(
            f"<NonEqtyTrnsprncyData><TechRcrdId>{i}</TechRcrdId><Id>"
            f"<ISINAndSubClss><ISIN>{isin}</ISIN>{deriv_xml}{mifir_xml}"
            "</ISINAndSubClss></Id></NonEqtyTrnsprncyData>")
    return ("<FinInstrmRptgNonEqtyTradgActvtyRslt>" + "".join(items)
            + "</FinInstrmRptgNonEqtyTradgActvtyRslt>").encode()


def _firds_xml(recs) -> bytes:
    """recs: [(isin, lei|None)]"""
    items = []
    for isin, lei in recs:
        issr = f"<Issr>{lei or ''}</Issr>"
        items.append(
            "<FinInstrm><NewRcrd><FinInstrmGnlAttrbts>"
            f"<Id>{isin}</Id><ClssfctnTp>DBFUGB</ClssfctnTp>"
            f"</FinInstrmGnlAttrbts>{issr}</NewRcrd></FinInstrm>")
    return ("<Envelope>" + "".join(items) + "</Envelope>").encode()


def _zip(path, members: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _write_observed_artifact(path, isins) -> str:
    """Minimal G1-A-shaped artifact; returns its semantic digest."""
    entries = [{
        "isin": i, "sources": ["bme_apa"],
        "first_observed_at": "2026-09-17T00:00:00.000000Z",
        "last_observed_at": "2026-09-17T00:00:00.000000Z",
        "eligible_capture_identities": 1,
    } for i in sorted(isins)]
    digest = digest_entries(entries)
    doc = {"observed_instrument_set": {
        "instrument_count": len(entries),
        "instruments": entries,
        "semantic_digest": digest}}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return digest


def _gleif_dir(root, payloads: dict[str, bytes], notfound=()):
    d = root / "gleif"
    d.mkdir(parents=True, exist_ok=True)
    for lei, data in payloads.items():
        (d / f"{lei}.json").write_bytes(data)
    for lei in notfound:
        (d / f"{lei}.notfound.json").write_bytes(
            json.dumps({"lei": lei, "result": "NOT_FOUND"}).encode() + b"\n")
    return d


def _build(tmp_path, isin, fitrs_recs, firds_recs, gleif_payloads=None,
           gleif_notfound=()):
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, [isin])
    fitrs_zip = _zip(tmp_path / "fitrs.zip",
                     {"fitrs.xml": _fitrs_xml(fitrs_recs)})
    firds_zip = _zip(tmp_path / "firds.zip",
                     {"firds.xml": _firds_xml(firds_recs)})
    gleif = _gleif_dir(tmp_path, gleif_payloads or {},
                       notfound=gleif_notfound)
    return build_report(observed, [fitrs_zip], [firds_zip], gleif,
                        expected_g1a_digest=digest,
                        date="d", generated_utc="t")[:2]


def _row_of(report_rows, isin):
    return next(r for r in report_rows if r["isin"] == isin)


# ---------- disposition chain ----------

def test_include_es_crpb(tmp_path):
    report, rows = _build(tmp_path, "XS0000000001",
                          [("XS0000000001", "BOND", "BOND5", "Corporate bond")],
                          [("XS0000000001", LEI_ES)],
                          {LEI_ES: _gleif_payload(LEI_ES, "ES")})
    r = _row_of(rows, "XS0000000001")
    assert r["branch"] == "INCLUDE"
    assert r["reason"] == "IN_ES_CRPB_UNIVERSE"
    assert r["gleif"]["cc"] == "ES" and r["gleif"]["ok"] is True
    assert report["dispositions"]["branch_counts"]["INCLUDE"] == 1


def test_exclude_non_bond(tmp_path):
    _, rows = _build(tmp_path, "XS0000000002",
                     [("XS0000000002", "DERV", "IRD05", "Swaptions")],
                     [])
    r = _row_of(rows, "XS0000000002")
    assert (r["branch"], r["reason"]) == ("EXCLUDE", "NOT_BOND")


def test_exclude_non_crpb(tmp_path):
    _, rows = _build(tmp_path, "XS0000000003",
                     [("XS0000000003", "BOND", "BOND1", "Sovereign bond")],
                     [])
    r = _row_of(rows, "XS0000000003")
    assert (r["branch"], r["reason"]) == ("EXCLUDE", "NOT_CRPB")


def test_quarantine_missing_mifir_id(tmp_path):
    """ISIN present in FIRDS but absent from FITRS -> MiFIR ID undetermined."""
    _, rows = _build(tmp_path, "XS0000000004",
                     [("XS-OTHER", "BOND", "BOND5", "Corporate bond")],
                     [("XS0000000004", LEI_ES)],
                     {LEI_ES: _gleif_payload(LEI_ES, "ES")})
    r = _row_of(rows, "XS0000000004")
    assert (r["branch"], r["reason"]) == ("QUARANTINE", "MISSING_MIFIR_ID")
    assert r["fitrs"]["n"] == 0


def test_quarantine_missing_bond_type(tmp_path):
    """BOND without a SACL criterion -> bond type undetermined."""
    _, rows = _build(tmp_path, "XS0000000005",
                     [("XS0000000005", "BOND", None, None)], [])
    r = _row_of(rows, "XS0000000005")
    assert (r["branch"], r["reason"]) == ("QUARANTINE", "MISSING_BOND_TYPE")


def test_quarantine_missing_issuer_lei(tmp_path):
    _, rows = _build(tmp_path, "XS0000000006",
                     [("XS0000000006", "BOND", "BOND5", "Corporate bond")],
                     [("XS0000000006", None)])
    r = _row_of(rows, "XS0000000006")
    assert (r["branch"], r["reason"]) == ("QUARANTINE", "MISSING_ISSUER_LEI")


def test_quarantine_invalid_lei(tmp_path):
    _, rows = _build(tmp_path, "XS0000000007",
                     [("XS0000000007", "BOND", "BOND5", "Corporate bond")],
                     [("XS0000000007", LEI_BAD)])
    r = _row_of(rows, "XS0000000007")
    assert (r["branch"], r["reason"]) == ("QUARANTINE", "INVALID_ISSUER_LEI")


def test_conflict_firds_leis_frozen_precedence(tmp_path):
    """Contradictory FIRDS LEIs: the frozen precedence is preserved — no
    representative LEI is injected, so the resolver hits
    MISSING_ISSUER_LEI (step 5) before the conflict branch (step 7).
    This is the pre-PR G0/F-009 behavior, kept verbatim."""
    _report, rows = _build(tmp_path, "XS0000000008",
                           [("XS0000000008", "BOND", "BOND5",
                             "Corporate bond")],
                           [("XS0000000008", LEI_ES),
                            ("XS0000000008", LEI_FR)])
    r = _row_of(rows, "XS0000000008")
    assert (r["branch"], r["reason"]) == ("QUARANTINE", "MISSING_ISSUER_LEI")
    assert r["firds"]["conflict"] is True and r["firds"]["lei"] is None
    assert "IDENTITY_CONFLICT" in r["notes"][0]
    # Pre-PR equivalence: the record handed to the resolver is exactly
    # issuer_lei=None + identity_conflict=True.
    from universe.assemble import assemble_security_record
    from universe.firds import FirdsInstrument
    from universe.fitrs import FitrsRecord
    from universe.resolver import resolve_disposition
    a = assemble_security_record(
        "XS0000000008",
        [FitrsRecord("XS0000000008", "BOND", "CRPB", "Corporate bond",
                     "BOND5")],
        [FirdsInstrument("XS0000000008", LEI_ES),
         FirdsInstrument("XS0000000008", LEI_FR)], {})
    assert a.record.issuer_lei is None
    assert a.record.identity_conflict is True
    d = resolve_disposition(a.record)
    assert (d.branch.value, d.reason.value) == \
        ("QUARANTINE", "MISSING_ISSUER_LEI")


def test_quarantine_gleif_not_found(tmp_path):
    """Authoritative NOT_FOUND is a domain result: unresolved jurisdiction."""
    _, rows = _build(tmp_path, "XS0000000009",
                     [("XS0000000009", "BOND", "BOND5", "Corporate bond")],
                     [("XS0000000009", LEI_ES)],
                     gleif_notfound=[LEI_ES])
    r = _row_of(rows, "XS0000000009")
    assert (r["branch"], r["reason"]) == (
        "QUARANTINE", "UNRESOLVABLE_LEGAL_JURISDICTION")
    assert r["gleif"]["acq"] == "NOT_FOUND"


def test_conflict_non_es(tmp_path):
    _, rows = _build(tmp_path, "XS0000000010",
                     [("XS0000000010", "BOND", "BOND5", "Corporate bond")],
                     [("XS0000000010", LEI_FR)],
                     {LEI_FR: _gleif_payload(LEI_FR, "FR")})
    r = _row_of(rows, "XS0000000010")
    assert (r["branch"], r["reason"]) == (
        "CONFLICT", "NON_ES_LEGAL_JURISDICTION")
    assert r["gleif"]["cc"] == "FR"


def test_contradictory_fitrs_fails_closed(tmp_path):
    """Two FITRS records disagreeing on bond type -> undetermined ->
    QUARANTINE (never guessed)."""
    _, rows = _build(tmp_path, "XS0000000011",
                     [("XS0000000011", "BOND", "BOND5", "Corporate bond"),
                      ("XS0000000011", "BOND", "BOND4", "Covered Bond")],
                     [("XS0000000011", LEI_ES)],
                     {LEI_ES: _gleif_payload(LEI_ES, "ES")})
    r = _row_of(rows, "XS0000000011")
    assert (r["branch"], r["reason"]) == ("QUARANTINE", "MISSING_BOND_TYPE")
    assert r["fitrs"]["amb"] == ["bond"]
    assert "disagree on bond type" in r["notes"][0]


def test_every_isin_exactly_one_row(tmp_path):
    isins = [f"XS{i:010d}" for i in range(5)]
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, isins)
    fitrs_zip = _zip(tmp_path / "f.zip", {"f.xml": _fitrs_xml(
        [(i, "BOND", "BOND5", "Corporate bond") for i in isins])})
    firds_zip = _zip(tmp_path / "r.zip", {"r.xml": _firds_xml(
        [(i, LEI_ES) for i in isins])})
    gleif = _gleif_dir(tmp_path, {LEI_ES: _gleif_payload(LEI_ES, "ES")})
    _, rows, _links = build_report(
        observed, [fitrs_zip], [firds_zip], gleif,
        expected_g1a_digest=digest, date="d", generated_utc="t")
    assert len(rows) == 5
    assert sorted(r["isin"] for r in rows) == sorted(isins)


def test_duplicate_input_isin_rejected(tmp_path):
    with pytest.raises(G1BError, match="duplicate input ISINs"):
        build_dispositions(["XS1", "XS1"], {}, {}, {}, {}, {})


def test_input_authority_digest_enforced(tmp_path):
    observed = tmp_path / "g1a.yaml"
    _write_observed_artifact(observed, ["XS0000000001"])
    with pytest.raises(G1BError, match="expected authority"):
        load_observed_isins(observed,
                            expected_digest="0" * 64)


def test_artifact_count_mismatch_fails_closed(tmp_path):
    observed = tmp_path / "g1a.yaml"
    entries = [{"isin": "XS1", "sources": ["bme_apa"],
                "first_observed_at": None, "last_observed_at": None,
                "eligible_capture_identities": 1}]
    observed.write_text(yaml.safe_dump({"observed_instrument_set": {
        "instrument_count": 99, "instruments": entries,
        "semantic_digest": digest_entries(entries)}}))
    digest = digest_entries(entries)
    with pytest.raises(G1BError, match="instrument_count"):
        load_observed_isins(observed, expected_digest=digest)


def test_mutated_isin_with_stale_embedded_digest_fails(tmp_path):
    """Blocker-3 regression: mutate one ISIN but keep the embedded
    semantic_digest — the recomputation must catch it."""
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(
        observed, ["XS0000000001", "XS0000000002"])
    doc = yaml.safe_load(observed.read_text())
    doc["observed_instrument_set"]["instruments"][0]["isin"] = "XX9999999999"
    observed.write_text(yaml.safe_dump(doc))
    with pytest.raises(G1BError, match="recomputed"):
        load_observed_isins(observed, expected_digest=digest)


def test_changed_embedded_digest_same_rows_fails(tmp_path):
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, ["XS0000000001"])
    doc = yaml.safe_load(observed.read_text())
    doc["observed_instrument_set"]["semantic_digest"] = "f" * 64
    observed.write_text(yaml.safe_dump(doc))
    with pytest.raises(G1BError, match="recomputed"):
        load_observed_isins(observed, expected_digest=digest)


def test_valid_embedded_and_recomputed_pass(tmp_path):
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, ["XS0000000001"])
    assert load_observed_isins(observed, expected_digest=digest) == \
        ["XS0000000001"]


# ---------- reference-set canonicalization ----------

def test_reference_set_digest_path_and_order_independent(tmp_path):
    a = tmp_path / "loc_a"
    b = tmp_path / "loc_b"
    for d in (a, b):
        d.mkdir()
        (d / "one.xml").write_bytes(b"<a/>")
        (d / "two.xml").write_bytes(b"<b/>")
    sa = reference_set_from_files([a / "one.xml", a / "two.xml"])
    sb = reference_set_from_files(list(reversed(
        [b / "one.xml", b / "two.xml"])))
    assert sa["set_digest"] == sb["set_digest"]
    assert [e["name"] for e in sa["entries"]] == ["one.xml", "two.xml"]


def test_reference_entry_rejects_absolute_name():
    from universe.reference import ReferenceSetError
    with pytest.raises(ReferenceSetError):
        canonical_reference_set([{
            "name": "C:/abs/path/x.xml", "sha256": "0" * 64, "bytes": 1}])
    with pytest.raises(ReferenceSetError):
        canonical_reference_set([reference_entry("x", b"1"),
                                 reference_entry("x", b"1")])


# ---------- GLEIF acquisition completeness ----------

def test_gleif_acquisition_missing_cache_is_not_unresolved(tmp_path):
    """A required LEI with no pinned artifact must HOLD the run — it can
    never masquerade as UNRESOLVABLE_LEGAL_JURISDICTION."""
    gleif = _gleif_dir(tmp_path, {})  # empty: nothing acquired
    with pytest.raises(AcquisitionIncomplete) as exc:
        load_acquired_entities(gleif, {LEI_ES})
    assert exc.value.census[LEI_ES] == "NOT_ATTEMPTED"


def test_gleif_acquisition_outcomes(tmp_path):
    gleif = _gleif_dir(tmp_path, {LEI_ES: _gleif_payload(LEI_ES, "ES")},
                       notfound=[LEI_FR])
    entities, census = load_acquired_entities(gleif, {LEI_ES, LEI_FR})
    assert census == {LEI_ES: "RESOLVED", LEI_FR: "NOT_FOUND"}
    assert entities[LEI_ES].legal_jurisdiction_country == "ES"
    assert entities[LEI_FR].resolved is False


def test_gleif_acquire_writes_once_and_marks_404(tmp_path):
    gleif = tmp_path / "gleif"
    calls = []

    def fake_get(url):
        calls.append(url)
        if url.endswith(LEI_FR):
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        return _gleif_payload(LEI_ES, "ES")

    m1 = acquire_leis([LEI_ES, LEI_FR], gleif, fake_get,
                      retrieved_at="2026-09-26T00:00:00Z")
    assert m1["outcome_counts"]["RESOLVED"] == 1
    assert m1["outcome_counts"]["NOT_FOUND"] == 1
    payload = gleif / f"{LEI_ES}.json"
    assert payload.is_file()
    # Second run: cached, no new fetches.
    m2 = acquire_leis([LEI_ES, LEI_FR], gleif, fake_get,
                      retrieved_at="2026-09-26T00:00:00Z")
    assert len(calls) == 2
    assert all(e["cached"] for e in m2["leis"].values())


def test_gleif_acquire_failed_then_complete(tmp_path):
    gleif = tmp_path / "gleif"

    def failing(url):
        raise ConnectionError("offline")

    m = acquire_leis([LEI_ES], gleif, failing)
    assert m["outcome_counts"]["ACQUISITION_FAILED"] == 1
    with pytest.raises(AcquisitionIncomplete):
        load_acquired_entities(gleif, {LEI_ES})
    acquire_leis([LEI_ES], gleif, lambda u: _gleif_payload(LEI_ES, "ES"))
    _, census = load_acquired_entities(gleif, {LEI_ES})
    assert census[LEI_ES] == "RESOLVED"


def test_corrupt_pinned_payload_is_acquisition_failure(tmp_path):
    gleif = _gleif_dir(tmp_path, {LEI_ES: b"not-json"})
    with pytest.raises(AcquisitionIncomplete):
        load_acquired_entities(gleif, {LEI_ES})


def test_corrupt_notfound_marker_is_acquisition_failure(tmp_path):
    """Optional hardening: a NOT_FOUND marker whose contents do not attest
    the expected LEI/result is an acquisition defect, not a domain result."""
    gleif = _gleif_dir(tmp_path, {})
    (gleif / f"{LEI_ES}.notfound.json").write_bytes(
        json.dumps({"lei": LEI_FR, "result": "NOT_FOUND"}).encode())
    with pytest.raises(AcquisitionIncomplete) as exc:
        load_acquired_entities(gleif, {LEI_ES})
    assert exc.value.census[LEI_ES] == "ACQUISITION_FAILED"


def test_gleif_set_digest_stable(tmp_path):
    gleif = _gleif_dir(tmp_path, {LEI_ES: _gleif_payload(LEI_ES, "ES")},
                       notfound=[LEI_FR])
    s1 = reference_set_from_dir(gleif)
    s2 = reference_set_from_dir(gleif)
    assert s1["set_digest"] == s2["set_digest"]
    names = [e["name"] for e in s1["entries"]]
    assert names == sorted(names)


# ---------- determinism / hygiene of emitted artifact ----------

def test_deterministic_rows_and_digest(tmp_path):
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(
        observed, ["XS0000000002", "XS0000000001"])
    fitrs_zip = _zip(tmp_path / "f.zip", {"f.xml": _fitrs_xml([
        ("XS0000000001", "BOND", "BOND5", "Corporate bond"),
        ("XS0000000002", "BOND", "BOND5", "Corporate bond")])})
    firds_zip = _zip(tmp_path / "r.zip", {"r.xml": _firds_xml([
        ("XS0000000001", LEI_ES), ("XS0000000002", LEI_FR)])})
    gleif = _gleif_dir(tmp_path, {
        LEI_ES: _gleif_payload(LEI_ES, "ES"),
        LEI_FR: _gleif_payload(LEI_FR, "FR")})
    r1, rows1, l1 = build_report(observed, [fitrs_zip], [firds_zip], gleif,
                                 expected_g1a_digest=digest,
                                 date="d", generated_utc="t")
    r2, rows2, l2 = build_report(observed, [fitrs_zip], [firds_zip], gleif,
                                 expected_g1a_digest=digest,
                                 date="d", generated_utc="t")
    assert canonical_links_bytes(l1) == canonical_links_bytes(l2)
    assert canonical_rows_bytes(rows1) == canonical_rows_bytes(rows2)
    assert r1["dispositions"]["disposition_semantic_digest"] == \
        r2["dispositions"]["disposition_semantic_digest"]
    lines = canonical_rows_bytes(rows1).decode().splitlines()
    assert [json.loads(line)["isin"] for line in lines] == \
        ["XS0000000001", "XS0000000002"]


def test_no_workstation_paths_in_report(tmp_path):
    report, rows = _build(tmp_path, "XS0000000001",
                          [("XS0000000001", "BOND", "BOND5", "Corporate bond")],
                          [("XS0000000001", LEI_ES)],
                          {LEI_ES: _gleif_payload(LEI_ES, "ES")})
    text = yaml.safe_dump(report) + canonical_rows_bytes(rows).decode()
    assert str(tmp_path) not in text
    for family in ("fitrs", "firds", "gleif"):
        for entry in report["reference_sets"][family]["entries"]:
            assert "/" not in entry["name"] and "\\" not in entry["name"]
    assert [e["name"] for e in
            report["reference_sets"]["fitrs"]["entries"]] == ["fitrs.zip"]


def test_scan_helpers_filter_to_observed(tmp_path):
    fitrs_zip = _zip(tmp_path / "f.zip", {"f.xml": _fitrs_xml([
        ("XS1", "BOND", "BOND5", "Corporate bond"),
        ("XS-OTHER", "DERV", "IRD05", "Swaptions")])})
    firds_zip = _zip(tmp_path / "r.zip", {"r.xml": _firds_xml([
        ("XS1", LEI_ES), ("XS-OTHER", LEI_FR)])})
    fmap = scan_fitrs([fitrs_zip], {"XS1"})
    rmap = scan_firds([firds_zip], {"XS1"})
    assert list(fmap) == ["XS1"] and list(rmap) == ["XS1"]
    assert fmap["XS1"][0].record.instrument_mifir_id == "BOND"


# ---------- Blocker 2: resolver-probe required-LEI derivation ----------

def _fitrs_map(recs):
    from universe.fitrs import parse_fitrs
    out = {}
    for spec in recs:
        for rec in parse_fitrs(_fitrs_xml([spec])):
            out.setdefault(rec.instrument_isin, []).append(rec)
    return out


def _firds_map(recs):
    from universe.firds import parse_firds
    out = {}
    for spec in recs:
        for rec in parse_firds(_firds_xml([spec])):
            out.setdefault(rec.instrument_isin, []).append(rec)
    return out


def _required(isin, fitrs, firds):
    from universe.assemble import required_gleif_leis
    return required_gleif_leis([isin], _fitrs_map(fitrs),
                             _firds_map(firds))


def test_required_lei_not_bond_not_required():
    """A: non-BOND terminates before the jurisdiction step."""
    assert _required("XS1", [("XS1", "DERV", "IRD05", "Swaptions")],
                     [("XS1", LEI_ES)]) == set()


def test_required_lei_not_crpb_not_required():
    """B: non-CRPB terminates before the jurisdiction step."""
    assert _required("XS1", [("XS1", "BOND", "BOND1", "Sovereign bond")],
                     [("XS1", LEI_ES)]) == set()


def test_required_lei_missing_mifir_not_required():
    """C: no FITRS record -> MISSING_MIFIR_ID, no GLEIF needed."""
    assert _required("XS1", [], [("XS1", LEI_ES)]) == set()


def test_required_lei_missing_bond_type_not_required():
    """D: BOND without SACL -> MISSING_BOND_TYPE, no GLEIF needed."""
    assert _required("XS1", [("XS1", "BOND", None, None)],
                     [("XS1", LEI_ES)]) == set()


def test_required_lei_crpb_valid_lei_required():
    """E: BOND+CRPB + single valid LEI + no GLEIF observation -> required."""
    assert _required("XS1", [("XS1", "BOND", "BOND5", "Corporate bond")],
                     [("XS1", LEI_ES)]) == {LEI_ES}


def test_required_lei_ambiguous_not_required():
    """F: contradictory FIRDS LEIs -> decided before jurisdiction."""
    assert _required("XS1", [("XS1", "BOND", "BOND5", "Corporate bond")],
                     [("XS1", LEI_ES), ("XS1", LEI_FR)]) == set()


def test_required_lei_invalid_not_required():
    """G: non-ISO-17442 LEI -> INVALID_ISSUER_LEI, no lookup."""
    assert _required("XS1", [("XS1", "BOND", "BOND5", "Corporate bond")],
                     [("XS1", LEI_BAD)]) == set()


# ---------- Blocker 2b: GLEIF set covers exactly the required universe ---

def test_gleif_set_ignores_unrelated_cache_residue(tmp_path):
    """An extra <LEI>.json in the directory must not alter the set, the
    rows, the links, or the report bytes."""
    isin = "XS0000000001"
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, [isin])
    fitrs_zip = _zip(tmp_path / "f.zip", {"f.xml": _fitrs_xml(
        [(isin, "BOND", "BOND5", "Corporate bond")])})
    firds_zip = _zip(tmp_path / "r.zip", {"r.xml": _firds_xml(
        [(isin, LEI_ES)])})

    clean = _gleif_dir(tmp_path / "a", {LEI_ES: _gleif_payload(LEI_ES, "ES")})
    dirty = _gleif_dir(tmp_path / "b", {
        LEI_ES: _gleif_payload(LEI_ES, "ES"),
        LEI_FR: _gleif_payload(LEI_FR, "FR"),   # residue: not required
    })
    rep_a, rows_a, links_a = build_report(
        observed, [fitrs_zip], [firds_zip], clean,
        expected_g1a_digest=digest, date="d", generated_utc="t")
    rep_b, rows_b, links_b = build_report(
        observed, [fitrs_zip], [firds_zip], dirty,
        expected_g1a_digest=digest, date="d", generated_utc="t")
    assert rep_a["reference_sets"]["gleif"] == \
        rep_b["reference_sets"]["gleif"]
    assert [e["name"] for e in
            rep_a["reference_sets"]["gleif"]["entries"]] == \
        [f"{LEI_ES}.json"]
    assert rep_a["reference_basis_id"] == rep_b["reference_basis_id"]
    assert canonical_rows_bytes(rows_a) == canonical_rows_bytes(rows_b)
    assert canonical_links_bytes(links_a) == canonical_links_bytes(links_b)
    assert yaml.safe_dump(rep_a) == yaml.safe_dump(rep_b)


# ---------- Blocker 4: concrete reference-observation handles ----------

def test_reference_links_multi_record_handles(tmp_path):
    """Multiple FITRS/FIRDS records for one ISIN produce deterministic,
    ordered handles — and every handle is emitted."""
    isin = "XS0000000001"
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, [isin])
    # Two FITRS members across two zips; two FIRDS records in one member.
    f1 = _zip(tmp_path / "a.zip", {"a.xml": _fitrs_xml([
        (isin, "BOND", "BOND5", "Corporate bond")])})
    f2 = _zip(tmp_path / "b.zip", {"b1.xml": _fitrs_xml([
        (isin, "BOND", "BOND5", "Corporate bond"),
        ("OTHER", "DERV", "IRD05", "Swaptions")])})
    r1 = _zip(tmp_path / "r.zip", {"r.xml": _firds_xml(
        [(isin, LEI_ES), ("OTHER", LEI_FR), (isin, LEI_ES)])})
    gleif = _gleif_dir(tmp_path, {LEI_ES: _gleif_payload(LEI_ES, "ES")})
    report, _rows, links = build_report(
        observed, [f1, f2], [r1], gleif,
        expected_g1a_digest=digest, date="d", generated_utc="t")
    link = links[0]
    assert link["i"] == isin
    # fitrs: file index by canonical sorted entries (a.zip=0, b.zip=1),
    # member index, record ordinals grouped under '<file>:<member>'.
    assert link["fitrs"] == {
        "0:0": {"o": [0], "r": ["0"]},
        "1:0": {"o": [0], "r": ["0"]},
    }
    # firds: same file/member, ordinals 0 and 2 (OTHER in between); no
    # source-published report id -> no 'r' array.
    assert link["firds"] == {"0:0": {"o": [0, 2]}}
    assert link["gleif"] == f"{LEI_ES}.json"
    assert report["reference_links"]["sha256"] == \
        hashlib.sha256(canonical_links_bytes(links)).hexdigest()
    # deterministic across runs
    _, _, links2 = build_report(
        observed, [f1, f2], [r1], gleif,
        expected_g1a_digest=digest, date="d", generated_utc="t")
    assert canonical_links_bytes(links) == canonical_links_bytes(links2)


def test_reference_links_cross_root_determinism(tmp_path):
    """Identical inputs under different roots -> identical link bytes and
    report (no filesystem paths anywhere)."""
    isin = "XS0000000001"
    outs = []
    for loc in ("loc_a", "loc_b"):
        root = tmp_path / loc
        root.mkdir()
        observed = root / "g1a.yaml"
        digest = _write_observed_artifact(observed, [isin])
        fz = _zip(root / "f.zip", {"f.xml": _fitrs_xml(
            [(isin, "BOND", "BOND5", "Corporate bond")])})
        rz = _zip(root / "r.zip", {"r.xml": _firds_xml([(isin, LEI_ES)])})
        gleif = _gleif_dir(root, {LEI_ES: _gleif_payload(LEI_ES, "ES")})
        rep, rows, links = build_report(
            observed, [fz], [rz], gleif, expected_g1a_digest=digest,
            date="d", generated_utc="t")
        outs.append((rep, rows, links))
    assert yaml.safe_dump(outs[0][0]) == yaml.safe_dump(outs[1][0])
    assert canonical_rows_bytes(outs[0][1]) == \
        canonical_rows_bytes(outs[1][1])
    assert canonical_links_bytes(outs[0][2]) == \
        canonical_links_bytes(outs[1][2])
    text = yaml.safe_dump(outs[0][0]) \
        + canonical_rows_bytes(outs[0][1]).decode() \
        + canonical_links_bytes(outs[0][2]).decode()
    assert str(tmp_path) not in text and "loc_a" not in text \
        and "loc_b" not in text


def test_reference_links_no_report_id_fallback(tmp_path):
    """Absent source_report_id -> handle still emitted, rid omitted."""
    isin = "XS0000000001"
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, [isin])
    # FIRDS record without FinInstrmRptgRprtSts/RptgRef -> rid absent.
    fz = _zip(tmp_path / "f.zip", {"f.xml": _fitrs_xml(
        [(isin, "BOND", "BOND5", "Corporate bond")])})
    rz = _zip(tmp_path / "r.zip", {"r.xml": _firds_xml([(isin, LEI_ES)])})
    gleif = _gleif_dir(tmp_path, {LEI_ES: _gleif_payload(LEI_ES, "ES")})
    _, _, links = build_report(observed, [fz], [rz], gleif,
                               expected_g1a_digest=digest,
                               date="d", generated_utc="t")
    # deterministic ordinal handle always present; 'r' only when every
    # record in the group carries a source-published report id
    assert links[0]["fitrs"]["0:0"]["o"] == [0]
    assert links[0]["firds"]["0:0"]["o"] == [0]
    assert "r" not in links[0]["firds"]["0:0"]


# ---------- Review fix: per-ISIN GLEIF reachability + stage-aware links --

def test_gleif_never_cited_for_early_exit_sharing_lei(tmp_path):
    """Two ISINs share one issuer LEI. A reaches the jurisdiction step
    (BOND+CRPB); B exits at NOT_CRPB. B must carry NO gleif fields and no
    gleif reference link — the shared LEI must not leak provenance."""
    isin_a, isin_b = "XS00000000AA", "XS00000000BB"
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, [isin_a, isin_b])
    fz = _zip(tmp_path / "f.zip", {"f.xml": _fitrs_xml([
        (isin_a, "BOND", "BOND5", "Corporate bond"),
        (isin_b, "BOND", "BOND1", "Sovereign bond")])})
    rz = _zip(tmp_path / "r.zip", {"r.xml": _firds_xml(
        [(isin_a, LEI_ES), (isin_b, LEI_ES)])})
    gleif = _gleif_dir(tmp_path, {LEI_ES: _gleif_payload(LEI_ES, "ES")})
    report, rows, links = build_report(
        observed, [fz], [rz], gleif, expected_g1a_digest=digest,
        date="d", generated_utc="t")
    ra = _row_of(rows, isin_a)
    rb = _row_of(rows, isin_b)
    assert ra["branch"] == "INCLUDE"
    assert ra["gleif"] == {"acq": "RESOLVED", "ok": True, "cc": "ES"}
    assert rb["branch"] == "EXCLUDE" and rb["reason"] == "NOT_CRPB"
    assert rb["gleif"] == {"acq": None, "ok": None, "cc": None}
    la = next(ln for ln in links if ln["i"] == isin_a)
    lb = next(ln for ln in links if ln["i"] == isin_b)
    assert la["gleif"] == f"{LEI_ES}.json"
    assert lb["gleif"] is None
    # stage-aware: the early exit cites only the FITRS observations it used
    assert "firds" not in lb and "fitrs" in lb
    assert "firds" in la and "gleif" in la
    # gleif_resolved_isins counts only ISINs that actually consulted GLEIF
    assert report["dispositions"]["gleif_resolved_isins"] == 1
    # the shared LEI is still required exactly once
    assert report["reference_acquisition"]["gleif"]["required_leis"] == 1


def test_stage_aware_links_missing_lei_includes_firds(tmp_path):
    """MISSING_ISSUER_LEI consumed FIRDS evidence (empty/absent Issr) ->
    links cite fitrs + firds, gleif null."""
    isin = "XS00000000CC"
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, [isin])
    fz = _zip(tmp_path / "f.zip", {"f.xml": _fitrs_xml(
        [(isin, "BOND", "BOND5", "Corporate bond")])})
    rz = _zip(tmp_path / "r.zip", {"r.xml": _firds_xml([(isin, None)])})
    gleif = _gleif_dir(tmp_path, {})
    _, rows, links = build_report(observed, [fz], [rz], gleif,
                                  expected_g1a_digest=digest,
                                  date="d", generated_utc="t")
    assert _row_of(rows, isin)["reason"] == "MISSING_ISSUER_LEI"
    link = links[0]
    assert "fitrs" in link and "firds" in link
    assert link["gleif"] is None


def test_stage_aware_links_missing_mifir_fitrs_only(tmp_path):
    """MISSING_MIFIR_ID (ISIN absent from FITRS): the decision consumed the
    FITRS search only — no FIRDS rows are cited even though they exist."""
    isin = "XS00000000DD"
    observed = tmp_path / "g1a.yaml"
    digest = _write_observed_artifact(observed, [isin])
    fz = _zip(tmp_path / "f.zip", {"f.xml": _fitrs_xml(
        [("OTHER", "BOND", "BOND5", "Corporate bond")])})
    rz = _zip(tmp_path / "r.zip", {"r.xml": _firds_xml([(isin, LEI_ES)])})
    gleif = _gleif_dir(tmp_path, {})
    _, rows, links = build_report(observed, [fz], [rz], gleif,
                                  expected_g1a_digest=digest,
                                  date="d", generated_utc="t")
    assert _row_of(rows, isin)["reason"] == "MISSING_MIFIR_ID"
    link = links[0]
    assert "firds" not in link and link["gleif"] is None
