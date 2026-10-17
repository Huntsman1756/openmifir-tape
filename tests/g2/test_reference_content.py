import json

import pytest
from reference_fixtures import FIRDS, FITRS, ISIN, LEI, gleif, write_bundle, zipped

from metrics.g2_authority import AuthorityError
from metrics.g2_reference_content import open_reference_bundle


def test_parse_all_families_and_preserve_concrete_handles(tmp_path):
    manifest = write_bundle(tmp_path)
    loaded = open_reference_bundle(tmp_path, manifest)
    for family, identifier in (("FITRS", ISIN), ("FIRDS", ISIN), ("GLEIF", LEI)):
        row = loaded["indexes"][family][identifier][0]
        assert row["handle"]["ordinal"] == 0
        assert row["handle"]["observation_id"]
        assert row["handle"]["member"] == (None if family == "GLEIF" else "synthetic.xml")


@pytest.mark.parametrize("family,content", [
    ("FITRS", b"not a zip"), ("FIRDS", zipped([("empty.xml", b"<Envelope/>")])),
    ("FITRS", zipped([("a.xml", FITRS), ("b.xml", b"broken XML")])),
    ("FIRDS", zipped([("a.xml", FIRDS), ("b.xml", b"broken XML")])),
    ("GLEIF", b'{"data": []}'), ("GLEIF", b'{"data": null,"data": {}}'),
    ("GLEIF", json.dumps({"data": [json.loads(gleif())["data"], None]}).encode()),
])
def test_all_declared_content_validated_even_when_nothing_requested(tmp_path, family, content):
    manifest = write_bundle(tmp_path, overrides={family: content})
    with pytest.raises(AuthorityError):
        open_reference_bundle(tmp_path, manifest, wanted_isins=set())


def test_scope_and_retention_filter_do_not_change_bundle_identity(tmp_path):
    manifest = write_bundle(tmp_path, scopes={"FITRS": ["OTHER-ISIN"]})
    loaded = open_reference_bundle(tmp_path, manifest)
    assert loaded["indexes"]["FITRS"] == {}
    filtered = open_reference_bundle(tmp_path, manifest, wanted_isins=set())
    assert filtered["bundle"] == loaded["bundle"]
    assert filtered["indexes"]["FIRDS"] == {}


def test_explicit_failure_does_not_parse_undeclared_old_payload(tmp_path):
    manifest = write_bundle(tmp_path, failed="FITRS", overrides={"FITRS": b"old corrupt cache"})
    assert open_reference_bundle(tmp_path, manifest)["indexes"]["FITRS"] == {}


def test_duplicate_lei_facts_keep_both_handles(tmp_path):
    items = [json.loads(gleif(country))["data"] for country in ("ES", "FR")]
    manifest = write_bundle(tmp_path, overrides={"GLEIF": json.dumps({"data": items}).encode()})
    rows = open_reference_bundle(tmp_path, manifest)["indexes"]["GLEIF"][LEI]
    assert [r["record"].legal_jurisdiction_country for r in rows] == ["ES", "FR"]
    assert [r["handle"]["ordinal"] for r in rows] == [0, 1]


@pytest.mark.parametrize("family,content", [
    ("FITRS", FITRS.replace(b"<ISIN>SYNTHETIC-ISIN</ISIN>", b"<ISIN></ISIN>")),
    ("FIRDS", FIRDS.replace(f"<Id>{ISIN}</Id>".encode(), b"<Id></Id>")),
])
def test_record_without_instrument_identifier_fails_closed(tmp_path, family, content):
    manifest = write_bundle(tmp_path, overrides={family: zipped([("synthetic.xml", content)])})
    with pytest.raises(AuthorityError, match="reference record without instrument identifier"):
        open_reference_bundle(tmp_path, manifest)


def test_requested_identifier_missing_from_file_is_reported_not_found(tmp_path):
    manifest = write_bundle(tmp_path, scopes={"FITRS": ["MISSING-ISIN", ISIN]})
    loaded = open_reference_bundle(tmp_path, manifest)
    observation_id = loaded["bundle"]["observations"]["FITRS"][0]["observation_id"]
    assert loaded["not_found"]["FITRS"] == {observation_id: ["MISSING-ISIN"]}
    assert ISIN in loaded["indexes"]["FITRS"]
    assert "MISSING-ISIN" not in loaded["indexes"]["FITRS"]


def test_whole_file_scope_reports_empty_not_found(tmp_path):
    manifest = write_bundle(tmp_path, scopes={"FITRS": []})
    loaded = open_reference_bundle(tmp_path, manifest)
    assert set(loaded["not_found"]) == {"FITRS", "FIRDS", "GLEIF"}
    assert loaded["not_found"]["FITRS"] == {}
    assert ISIN in loaded["indexes"]["FITRS"]
