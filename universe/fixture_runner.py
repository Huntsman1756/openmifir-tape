"""Fixture runner: load synthetic disposition fixtures and verify every branch.

Loads ``fixtures/universe/disposition_cases.yaml`` (SYNTHETIC fixtures -- not
provider data), resolves each case with the frozen semantics, asserts each
resolves to its expected branch + reason, and produces a metadata-only
disposition table, SHA-256 snapshot, and correctness evidence.

Offline: reads only the fixtures file and in-memory records. No network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import SecurityRecord
from .resolver import (
    branch_counts,
    build_disposition_table,
    snapshot_payload,
    universe_snapshot_sha256,
)

DEFAULT_UNIVERSE_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "universe" / "disposition_cases.yaml"
)


class FixtureParseError(Exception):
    """Raised when a disposition fixture is malformed (fail closed)."""


@dataclass(frozen=True)
class FixtureCase:
    case_id: str
    expected_branch: str
    expected_reason: str
    record: SecurityRecord
    note: str = ""


@dataclass
class CaseResult:
    case_id: str
    branch: str
    reason: str
    expected_branch: str
    expected_reason: str
    passed: bool
    instrument_mifir_id: str | None
    instrument_bond_type: str | None
    issuer_lei: str | None
    legal_jurisdiction_country: str | None
    note: str = ""

    def to_manifest(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "expected_branch": self.expected_branch,
            "expected_reason": self.expected_reason,
            "resolved_branch": self.branch,
            "resolved_reason": self.reason,
            "passed": self.passed,
            "input": {
                "instrument_mifir_id": self.instrument_mifir_id,
                "instrument_bond_type": self.instrument_bond_type,
                "issuer_lei": self.issuer_lei,
                "legal_jurisdiction_country": self.legal_jurisdiction_country,
            },
            "note": self.note,
        }


@dataclass
class FixtureRunResult:
    results: list[CaseResult]
    snapshot_sha256: str
    branch_counts: dict[str, int]
    fail_closed_demonstrated: bool
    samples: dict[str, Any] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def case_count(self) -> int:
        return len(self.results)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "universe_id": "es_legal_issuer_v1",
            "profile": "es-corporate-bonds",
            "instrument_mifir_id": "BOND",
            "instrument_bond_type": "CRPB",
            "bond_type_source": "ESMA_FITRS_auth045_ISINAndSubClss",
            "legal_jurisdiction_filter": "ES",
            "synthetic_fixtures_only": True,
            "case_count": self.case_count,
            "all_passed": self.all_passed,
            "branch_counts": self.branch_counts,
            "universe_snapshot_sha256": self.snapshot_sha256,
            "fail_closed_demonstrated": self.fail_closed_demonstrated,
            "cases": [r.to_manifest() for r in self.results],
            "samples": self.samples,
        }


def _load_record_fields(raw: dict[str, Any]) -> SecurityRecord:
    record_raw = raw.get("record") or {}
    if not isinstance(record_raw, dict):
        raise FixtureParseError("case 'record' must be a mapping")

    conflicting = record_raw.get("conflicting_jurisdictions") or []
    if isinstance(conflicting, str):
        conflicting = [conflicting]
    conflicting = tuple(conflicting)

    gleif_resolved = record_raw.get("gleif_resolved", True)
    if not isinstance(gleif_resolved, bool):
        raise FixtureParseError("case 'gleif_resolved' must be a boolean")

    identity_conflict = record_raw.get("identity_conflict", False)
    if not isinstance(identity_conflict, bool):
        raise FixtureParseError("case 'identity_conflict' must be a boolean")

    return SecurityRecord(
        instrument_mifir_id=record_raw.get("instrument_mifir_id"),
        instrument_bond_type=record_raw.get("instrument_bond_type"),
        issuer_lei=record_raw.get("issuer_lei"),
        gleif_legal_jurisdiction_country=record_raw.get("gleif_legal_jurisdiction_country"),
        gleif_resolved=gleif_resolved,
        conflicting_jurisdictions=conflicting,
        identity_conflict=identity_conflict,
        instrument_isin=record_raw.get("instrument_isin"),
        source_report_id=record_raw.get("source_report_id"),
    )


def load_disposition_cases(path: Path = DEFAULT_UNIVERSE_FIXTURE) -> list[FixtureCase]:
    if not path.exists():
        raise FixtureParseError(f"disposition fixtures not found: {path}")
    with open(path, encoding="utf-8", newline="\n") as fh:
        doc = yaml.safe_load(fh) or {}
    cases_raw = doc.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise FixtureParseError("fixtures must define a non-empty 'cases' list")

    cases: list[FixtureCase] = []
    for index, raw in enumerate(cases_raw, start=1):
        if not isinstance(raw, dict):
            raise FixtureParseError(f"case #{index} must be a mapping")
        case_id = raw.get("id")
        branch = raw.get("expected_branch")
        reason = raw.get("expected_reason")
        if not isinstance(case_id, str) or not case_id:
            raise FixtureParseError(f"case #{index} missing 'id'")
        if not isinstance(branch, str) or not branch:
            raise FixtureParseError(f"case #{index} missing 'expected_branch'")
        if not isinstance(reason, str) or not reason:
            raise FixtureParseError(f"case #{index} missing 'expected_reason'")
        cases.append(
            FixtureCase(
                case_id=case_id,
                expected_branch=branch.upper(),
                expected_reason=reason.upper(),
                record=_load_record_fields(raw),
                note=str(raw.get("note", "")),
            )
        )
    return cases


def run_fixture_cases(path: Path = DEFAULT_UNIVERSE_FIXTURE) -> FixtureRunResult:
    cases = load_disposition_cases(path)
    records = [c.record for c in cases]
    dispositions = build_disposition_table(records)

    results: list[CaseResult] = []
    for case, disp in zip(cases, dispositions, strict=True):
        results.append(
            CaseResult(
                case_id=case.case_id,
                branch=disp.branch.value,
                reason=disp.reason.value,
                expected_branch=case.expected_branch,
                expected_reason=case.expected_reason,
                passed=disp.branch.value == case.expected_branch
                and disp.reason.value == case.expected_reason,
                instrument_mifir_id=disp.instrument_mifir_id,
                instrument_bond_type=disp.instrument_bond_type,
                issuer_lei=disp.issuer_lei,
                legal_jurisdiction_country=disp.legal_jurisdiction_country,
                note=case.note,
            )
        )

    snapshot_sha256 = universe_snapshot_sha256(dispositions)
    counts = branch_counts(dispositions)

    # MISSING_IDENTITY_FAIL_CLOSED: any case where a missing/unusable identity
    # (LEI, jurisdiction) did NOT resolve to INCLUDE.
    identity_branches = {r.branch for r in results if r.reason in {
        "MISSING_MIFIR_ID",
        "MISSING_ISSUER_LEI",
        "INVALID_ISSUER_LEI",
        "UNRESOLVABLE_LEGAL_JURISDICTION",
    }}
    fail_closed_demonstrated = identity_branches == {"QUARANTINE"}

    samples = {
        "snapshot_payload_sha256_of": "universe_snapshot_sha256 over the frozen disposition table",
    }

    return FixtureRunResult(
        results=results,
        snapshot_sha256=snapshot_sha256,
        branch_counts=counts,
        fail_closed_demonstrated=fail_closed_demonstrated,
        samples=samples,
    )


def build_snapshot_payload(path: Path = DEFAULT_UNIVERSE_FIXTURE) -> bytes:
    """Canonical snapshot payload bytes over the fixture disposition table."""
    cases = load_disposition_cases(path)
    dispositions = build_disposition_table([c.record for c in cases])
    return snapshot_payload(dispositions)
