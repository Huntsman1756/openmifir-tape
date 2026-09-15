"""``universe`` -- es_legal_issuer_v1 universe construction (G0-B1).

Frozen semantics (``docs/gates/G0.md`` §3 G0-B). This package builds the frozen
universe from FIRDS RTS 23 field 5 issuer LEI + GLEIF LegalJurisdiction.country
= ES, with a deterministic INCLUDE / EXCLUDE / QUARANTINE / CONFLICT
disposition table.

Offline by construction: acquisition adapters take an injected ``url -> bytes``
getter; unit tests never touch the network. No provider data is stored in the
repository tree.
"""

from __future__ import annotations

from .fixture_runner import (
    FixtureCase,
    FixtureRunResult,
    load_disposition_cases,
    run_fixture_cases,
)
from .firds import FirdsInstrument, fetch as firds_fetch, parse_firds
from .fitrs import FitrsRecord, parse_fitrs
from .gleif import GleifEntity, fetch as gleif_fetch, parse_gleif, resolve_legal_jurisdiction
from .model import Branch, Disposition, Reason, SecurityRecord
from .resolver import (
    build_disposition_table,
    branch_counts,
    resolve_disposition,
    snapshot_payload,
    universe_snapshot_sha256,
)

__all__ = [
    "Branch",
    "Disposition",
    "FirdsInstrument",
    "FitrsRecord",
    "FixtureCase",
    "FixtureRunResult",
    "GleifEntity",
    "Reason",
    "SecurityRecord",
    "branch_counts",
    "build_disposition_table",
    "firds_fetch",
    "gleif_fetch",
    "load_disposition_cases",
    "parse_firds",
    "parse_fitrs",
    "parse_gleif",
    "resolve_disposition",
    "resolve_legal_jurisdiction",
    "run_fixture_cases",
    "snapshot_payload",
    "universe_snapshot_sha256",
]
