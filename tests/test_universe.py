"""Deterministic, OFFLINE tests for es_legal_issuer_v1 universe construction (G0-B1).

No network: adapters take an injected ``url -> bytes`` getter, and one test
monkeypatches ``socket.socket`` to fail if any network access is attempted.

Frozen semantics (F-008 corrected): instrument MiFIR ID = BOND (RTS 2 field 3)
AND Bond Type = CRPB (RTS 2 field 9, from ESMA FITRS auth.045 — never guessed
from the FIRDS CFI code), issuer LEI from FIRDS field 5, GLEIF
LegalJurisdiction.country = ES, no ISIN-prefix heuristics.

Coverage: every frozen branch (INCLUDE / EXCLUDE / QUARANTINE / CONFLICT),
MISSING_IDENTITY_FAIL_CLOSED negative controls (including undetermined bond
type), NO ISIN-prefix heuristics, snapshot determinism + content sensitivity,
the synthetic fixture runner, and the FITRS auth.045 parser.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from universe import (
    Branch,
    FixtureRunResult,
    Reason,
    SecurityRecord,
    branch_counts,
    build_disposition_table,
    firds_fetch,
    gleif_fetch,
    parse_firds,
    parse_fitrs,
    parse_gleif,
    resolve_disposition,
    resolve_legal_jurisdiction,
    run_fixture_cases,
    snapshot_payload,
    universe_snapshot_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIVERSE_FIXTURES = REPO_ROOT / "fixtures" / "universe"
DISPOSITION_CASES = UNIVERSE_FIXTURES / "disposition_cases.yaml"
FIRDS_SAMPLE = UNIVERSE_FIXTURES / "firds_sample.xml"
FITRS_SAMPLE = UNIVERSE_FIXTURES / "fitrs_sample.xml"
GLEIF_SAMPLE = UNIVERSE_FIXTURES / "gleif_sample.json"


def _rec(**kwargs) -> SecurityRecord:
    """A profile-conforming BOND + CRPB record unless overridden."""
    kwargs.setdefault("instrument_mifir_id", "BOND")
    kwargs.setdefault("instrument_bond_type", "CRPB")
    return SecurityRecord(**kwargs)


def _exploding_getter(url: str) -> bytes:
    raise AssertionError(f"network access attempted: {url}")


# --- Branch resolution -------------------------------------------------------


def test_include_valid_es_crpb():
    d = resolve_disposition(
        _rec(issuer_lei="00000000000000000001", gleif_legal_jurisdiction_country="ES")
    )
    assert d.branch is Branch.INCLUDE
    assert d.reason is Reason.IN_ES_CRPB_UNIVERSE


def test_exclude_non_bond():
    for wrong_class in ("SHRS", "OPTN", "FUND", "DERV", "SFPS", "ETFS"):
        d = resolve_disposition(
            _rec(
                instrument_mifir_id=wrong_class,
                instrument_bond_type=None,
                issuer_lei="00000000000000000001",
                gleif_legal_jurisdiction_country="ES",
            )
        )
        assert d.branch is Branch.EXCLUDE
        assert d.reason is Reason.NOT_BOND


def test_exclude_non_crpb_bond():
    # A BOND of a known non-corporate type is EXCLUDE / NOT_CRPB.
    for bond_type in ("EUSB", "OEPB", "CVTB", "CVDB", "OTHR"):
        d = resolve_disposition(
            _rec(
                instrument_bond_type=bond_type,
                issuer_lei="00000000000000000001",
                gleif_legal_jurisdiction_country="ES",
            )
        )
        assert d.branch is Branch.EXCLUDE
        assert d.reason is Reason.NOT_CRPB


def test_quarantine_missing_bond_type():
    # BOND but RTS 2 field 9 undetermined -> fail closed, never INCLUDE/EXCLUDE.
    for missing in (None, "", "   ", "UNKNOWN"):
        d = resolve_disposition(
            _rec(
                instrument_bond_type=missing,
                issuer_lei="00000000000000000001",
                gleif_legal_jurisdiction_country="ES",
            )
        )
        if missing == "UNKNOWN":
            # An explicitly wrong type is proven non-corporate -> EXCLUDE.
            assert d.branch is Branch.EXCLUDE
            assert d.reason is Reason.NOT_CRPB
        else:
            assert d.branch is Branch.QUARANTINE
            assert d.reason is Reason.MISSING_BOND_TYPE


def test_exclude_precedence_over_missing_identity():
    # A non-BOND record with a missing LEI is still EXCLUDE (scope decided first).
    d = resolve_disposition(
        _rec(
            instrument_mifir_id="FUND",
            instrument_bond_type=None,
            issuer_lei=None,
            gleif_legal_jurisdiction_country=None,
        )
    )
    assert d.branch is Branch.EXCLUDE
    assert d.reason is Reason.NOT_BOND


def test_quarantine_missing_issuer_lei():
    for missing in (None, "", "   "):
        d = resolve_disposition(
            _rec(issuer_lei=missing, gleif_legal_jurisdiction_country="ES")
        )
        assert d.branch is Branch.QUARANTINE
        assert d.reason is Reason.MISSING_ISSUER_LEI


def test_quarantine_malformed_issuer_lei():
    for bad in ("12345", "0000000000000000000", "0000000000000000000!", "ABCDEFGHIJKLMNOPQRSTU"):
        d = resolve_disposition(
            _rec(issuer_lei=bad, gleif_legal_jurisdiction_country="ES")
        )
        assert d.branch is Branch.QUARANTINE
        assert d.reason is Reason.INVALID_ISSUER_LEI


def test_quarantine_unresolvable_legal_jurisdiction():
    # GLEIF has no record.
    d1 = resolve_disposition(
        _rec(
            issuer_lei="00000000000000000006",
            gleif_resolved=False,
            gleif_legal_jurisdiction_country=None,
        )
    )
    assert d1.branch is Branch.QUARANTINE
    assert d1.reason is Reason.UNRESOLVABLE_LEGAL_JURISDICTION

    # GLEIF resolved but no jurisdiction country.
    d2 = resolve_disposition(
        _rec(
            issuer_lei="00000000000000000007",
            gleif_resolved=True,
            gleif_legal_jurisdiction_country="   ",
        )
    )
    assert d2.branch is Branch.QUARANTINE
    assert d2.reason is Reason.UNRESOLVABLE_LEGAL_JURISDICTION


def test_conflict_non_es_jurisdiction():
    d = resolve_disposition(
        _rec(issuer_lei="00000000000000000008", gleif_legal_jurisdiction_country="FR")
    )
    assert d.branch is Branch.CONFLICT
    assert d.reason is Reason.NON_ES_LEGAL_JURISDICTION


def test_conflict_contradictory_jurisdictions():
    d = resolve_disposition(
        _rec(
            issuer_lei="00000000000000000009",
            gleif_legal_jurisdiction_country="ES",
            conflicting_jurisdictions=("ES", "FR"),
        )
    )
    assert d.branch is Branch.CONFLICT
    assert d.reason is Reason.IDENTITY_CONFLICT


def test_conflict_explicit_flag():
    d = resolve_disposition(
        _rec(
            issuer_lei="00000000000000000010",
            gleif_legal_jurisdiction_country="US",
            identity_conflict=True,
        )
    )
    assert d.branch is Branch.CONFLICT
    assert d.reason is Reason.IDENTITY_CONFLICT


def test_no_isin_prefix_heuristics():
    # XS-prefixed ISIN + ES jurisdiction -> INCLUDE (prefix must not block).
    d_include = resolve_disposition(
        _rec(
            instrument_isin="XS0000000001",
            issuer_lei="00000000000000000011",
            gleif_legal_jurisdiction_country="ES",
        )
    )
    assert d_include.branch is Branch.INCLUDE

    # ES-prefixed ISIN + non-ES jurisdiction -> CONFLICT (prefix must not force).
    d_conflict = resolve_disposition(
        _rec(
            instrument_isin="ES0000000001",
            issuer_lei="00000000000000000012",
            gleif_legal_jurisdiction_country="FR",
        )
    )
    assert d_conflict.branch is Branch.CONFLICT


# --- Snapshot determinism / content sensitivity ------------------------------


def _mixed_table() -> list[SecurityRecord]:
    return [
        _rec(issuer_lei="00000000000000000001", gleif_legal_jurisdiction_country="ES"),
        _rec(issuer_lei=None, gleif_legal_jurisdiction_country="ES"),
        _rec(instrument_mifir_id="SHRS", instrument_bond_type=None, issuer_lei="00000000000000000002"),
    ]


def test_snapshot_sha256_deterministic():
    a = build_disposition_table(_mixed_table())
    b = build_disposition_table(_mixed_table())
    assert universe_snapshot_sha256(a) == universe_snapshot_sha256(b)
    assert len(universe_snapshot_sha256(a)) == 64


def test_snapshot_sha256_order_independent():
    records = _mixed_table()
    forward = build_disposition_table(records)
    reverse = build_disposition_table(list(reversed(records)))
    assert universe_snapshot_sha256(forward) == universe_snapshot_sha256(reverse)


def test_snapshot_sha256_content_sensitive():
    base = build_disposition_table(_mixed_table())
    changed_records = list(_mixed_table())
    changed_records[0] = _rec(
        issuer_lei="00000000000000000001",
        gleif_legal_jurisdiction_country="FR",
    )
    changed = build_disposition_table(changed_records)
    assert universe_snapshot_sha256(base) != universe_snapshot_sha256(changed)
    assert snapshot_payload(base) != snapshot_payload(changed)


def test_snapshot_sha256_covers_every_branch():
    records = [
        _rec(issuer_lei="00000000000000000001", gleif_legal_jurisdiction_country="ES"),
        _rec(instrument_mifir_id="SHRS", instrument_bond_type=None, issuer_lei="00000000000000000002"),
        _rec(issuer_lei=None),
        _rec(issuer_lei="00000000000000000003", gleif_legal_jurisdiction_country="FR"),
    ]
    counts = branch_counts(build_disposition_table(records))
    assert counts == {"INCLUDE": 1, "EXCLUDE": 1, "QUARANTINE": 1, "CONFLICT": 1}


# --- Synthetic fixture runner ------------------------------------------------


def test_fixture_runner_all_cases_resolve_to_expected_branch():
    result = run_fixture_cases(DISPOSITION_CASES)
    assert isinstance(result, FixtureRunResult)
    assert result.case_count >= 12
    assert result.all_passed, [r.to_manifest() for r in result.results if not r.passed]
    assert len(result.snapshot_sha256) == 64


def test_fixture_runner_exercises_every_branch():
    result = run_fixture_cases(DISPOSITION_CASES)
    assert set(result.branch_counts) == {"INCLUDE", "EXCLUDE", "QUARANTINE", "CONFLICT"}
    assert all(count >= 1 for count in result.branch_counts.values())


def test_fixture_runner_demonstrates_missing_identity_fail_closed():
    result = run_fixture_cases(DISPOSITION_CASES)
    assert result.fail_closed_demonstrated is True
    identity_cases = [
        r
        for r in result.results
        if r.reason
        in {
            "MISSING_BOND_TYPE",
            "MISSING_ISSUER_LEI",
            "INVALID_ISSUER_LEI",
            "UNRESOLVABLE_LEGAL_JURISDICTION",
        }
    ]
    assert identity_cases
    assert all(r.branch == "QUARANTINE" for r in identity_cases)


# --- FIRDS / FITRS / GLEIF adapters (offline) --------------------------------


def test_firds_parse_extracts_field5_issuer_lei():
    instruments = parse_firds(FIRDS_SAMPLE.read_bytes())
    assert len(instruments) == 3
    by_isin = {i.instrument_isin: i for i in instruments}
    assert by_isin["XS0000000101"].issuer_lei == "00000000000000000101"
    assert by_isin["XS0000000101"].cfi_code == "DBFUGB"
    # Missing issuer LEI is preserved as empty (resolver QUARANTINEs; never guessed).
    assert by_isin["XS0000000102"].issuer_lei == ""


def test_fitrs_parse_extracts_bond_classification():
    records = parse_fitrs(FITRS_SAMPLE.read_bytes())
    assert len(records) == 5
    by_isin = {r.instrument_isin: r for r in records}

    corporate = by_isin["XS0000000201"]
    assert corporate.instrument_mifir_id == "BOND"
    assert corporate.bond_type == "CRPB"
    assert corporate.sub_asset_class == "BOND5"
    assert corporate.source_report_id == "1001"

    sovereign = by_isin["XS0000000202"]
    assert sovereign.instrument_mifir_id == "BOND"
    assert sovereign.bond_type == "EUSB"

    derivative = by_isin["XS0000000203"]
    assert derivative.instrument_mifir_id == "DERV"
    assert derivative.bond_type is None  # "Swaptions" is not a bond label

    unknown = by_isin["XS0000000204"]
    assert unknown.instrument_mifir_id == "BOND"
    assert unknown.bond_type is None  # unmapped label -> fail closed

    no_subclass = by_isin["XS0000000205"]
    assert no_subclass.instrument_mifir_id == "BOND"
    assert no_subclass.bond_type is None


def test_gleif_parse_resolves_legal_jurisdiction_country():
    entities = parse_gleif(GLEIF_SAMPLE.read_bytes())
    assert entities["00000000000000000101"].legal_jurisdiction_country == "ES"
    assert entities["00000000000000000103"].legal_jurisdiction_country == "FR"
    assert entities["00000000000000000999"].legal_jurisdiction_country == "US"
    unresolved = resolve_legal_jurisdiction("00000000000000000000", entities)
    assert unresolved.resolved is False
    assert unresolved.legal_jurisdiction_country is None


def test_adapters_use_injected_getter_not_network():
    calls: list[str] = []

    def recording_getter(url: str) -> bytes:
        calls.append(url)
        return FIRDS_SAMPLE.read_bytes()

    data = firds_fetch("https://fixture.invalid/firds.xml", recording_getter)
    assert calls == ["https://fixture.invalid/firds.xml"]
    assert parse_firds(data)

    with pytest.raises(AssertionError):
        firds_fetch("https://fixture.invalid/firds.xml", _exploding_getter)
    with pytest.raises(AssertionError):
        gleif_fetch("https://fixture.invalid/gleif.json", _exploding_getter)


def test_no_network_during_offline_resolution_and_parsing(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("network access attempted during offline test")

    monkeypatch.setattr(socket, "socket", _boom)

    result = run_fixture_cases(DISPOSITION_CASES)
    assert result.all_passed
    assert parse_firds(FIRDS_SAMPLE.read_bytes())
    assert parse_fitrs(FITRS_SAMPLE.read_bytes())
    assert parse_gleif(GLEIF_SAMPLE.read_bytes())

    records = [
        _rec(issuer_lei="00000000000000000020", gleif_legal_jurisdiction_country="ES")
    ]
    assert universe_snapshot_sha256(build_disposition_table(records))


# --- Frozen sample consumer (F-009) ------------------------------------------


def test_frozen_sample_loader_and_membership():
    from universe.sample import load_frozen_sample

    sample = load_frozen_sample()
    assert sample.profile == "es-corporate-bonds"
    assert len(sample.isins) == 20
    assert sample.contains("XS2984223102") is True   # frozen active ISIN
    assert sample.contains("XS3071337847") is True   # frozen quiet ISIN
    assert sample.contains("ES9999999999") is False  # not in sample
    assert sample.contains(None) is False
    assert sample.contains("") is False
    # Digest is deterministic and matches the manifest's frozen snapshot
    # (active-then-quiet order, LF-joined — the documented convention).
    assert sample.isin_digest() == sample.snapshot_sha256
