"""Offline tests for G1-B1 OBSERVED_UNIVERSE_DISPOSITION (metrics/g1b.py).

Synthetic fixtures only — no provider data, no network. The GLEIF layer is
exercised through pinned local artifacts (<LEI>.json / <LEI>.notfound.json)
and injected getters; the acquisition completeness contract is tested for
all four outcomes.
"""

import json
import urllib.error
import zipfile

import pytest
import yaml

from metrics.g1a import _digest_entries
from metrics.g1b import (
    G1BError,
    build_dispositions,
    build_report,
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
    digest = _digest_entries(entries)
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
                        date="d", generated_utc="t")


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


def test_conflict_firds_leis(tmp_path):
    _, rows = _build(tmp_path, "XS0000000008",
                     [("XS0000000008", "BOND", "BOND5", "Corporate bond")],
                     [("XS0000000008", LEI_ES),
                      ("XS0000000008", LEI_FR)])
    r = _row_of(rows, "XS0000000008")
    assert (r["branch"], r["reason"]) == ("CONFLICT", "IDENTITY_CONFLICT")
    assert r["firds"]["conflict"] is True


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
    _, rows = build_report(observed, [fitrs_zip], [firds_zip], gleif,
                           expected_g1a_digest=digest,
                           date="d", generated_utc="t")
    assert len(rows) == 5
    assert sorted(r["isin"] for r in rows) == sorted(isins)


def test_duplicate_input_isin_rejected(tmp_path):
    with pytest.raises(G1BError, match="duplicate input ISINs"):
        build_dispositions(["XS1", "XS1"], {}, {}, {}, {})


def test_input_authority_digest_enforced(tmp_path):
    observed = tmp_path / "g1a.yaml"
    _write_observed_artifact(observed, ["XS0000000001"])
    with pytest.raises(G1BError, match="semantic digest mismatch"):
        load_observed_isins(observed,
                            expected_digest="0" * 64)


def test_artifact_count_mismatch_fails_closed(tmp_path):
    observed = tmp_path / "g1a.yaml"
    entries = [{"isin": "XS1", "sources": ["bme_apa"],
                "first_observed_at": None, "last_observed_at": None,
                "eligible_capture_identities": 1}]
    observed.write_text(yaml.safe_dump({"observed_instrument_set": {
        "instrument_count": 99, "instruments": entries,
        "semantic_digest": _digest_entries(entries)}}))
    digest = _digest_entries(entries)
    with pytest.raises(G1BError, match="instrument_count"):
        load_observed_isins(observed, expected_digest=digest)


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
    r1, rows1 = build_report(observed, [fitrs_zip], [firds_zip], gleif,
                             expected_g1a_digest=digest,
                             date="d", generated_utc="t")
    r2, rows2 = build_report(observed, [fitrs_zip], [firds_zip], gleif,
                             expected_g1a_digest=digest,
                             date="d", generated_utc="t")
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
    assert fmap["XS1"][0].instrument_mifir_id == "BOND"
