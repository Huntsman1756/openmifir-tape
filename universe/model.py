"""Universe decision model for ``es_legal_issuer_v1`` (G0-B1).

Frozen semantics (``docs/gates/G0.md`` §3 G0-B), read not re-drafted:

- instrument MiFIR ID = BOND (RTS 2 Annex IV Table 2 field 3);
- Bond Type = CRPB — "Corporate Bond" (RTS 2 field 9), from ESMA FITRS
  non-equity transparency data (never derived from the FIRDS CFI code);
- issuer LEI from FIRDS RTS 23 field 5;
- GLEIF ``LegalJurisdiction.country`` = ES;
- NO ISIN-prefix heuristics.

Branches: INCLUDE / EXCLUDE / QUARANTINE / CONFLICT. Missing issuer LEI,
unresolvable LegalJurisdiction, or identity conflict QUARANTINE or CONFLICT.
No fuzzy matching. No automatic overrides. No heuristic deletion or merge.

This module is pure data + deterministic resolution inputs. It never fetches
anything and never holds provider payloads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

UNIVERSE_ID = "es_legal_issuer_v1"
PROFILE = "es-corporate-bonds"
INSTRUMENT_MIFIR_ID = "BOND"
BOND_TYPE = "CRPB"
LEGAL_JURISDICTION_FILTER = "ES"
ISSUER_LEI_SOURCE = "FIRDS_RTS23_FIELD5"
BOND_TYPE_SOURCE = "ESMA_FITRS_auth045_ISINAndSubClss"
LEGAL_JURISDICTION_SOURCE = "GLEIF_LegalJurisdiction.country"

# ISO 17442 LEI: exactly 20 alphanumeric characters. Exact-shape check only;
# this is NOT fuzzy matching and does NOT attempt checksum repair.
LEI_PATTERN = re.compile(r"^[A-Z0-9]{20}$")


class Branch(StrEnum):
    """Disposition branch (frozen)."""

    INCLUDE = "INCLUDE"
    EXCLUDE = "EXCLUDE"
    QUARANTINE = "QUARANTINE"
    CONFLICT = "CONFLICT"


class Reason(StrEnum):
    """Deterministic, auditable reason code attached to every disposition."""

    IN_ES_CRPB_UNIVERSE = "IN_ES_CRPB_UNIVERSE"
    MISSING_MIFIR_ID = "MISSING_MIFIR_ID"
    NOT_BOND = "NOT_BOND"
    MISSING_BOND_TYPE = "MISSING_BOND_TYPE"
    NOT_CRPB = "NOT_CRPB"
    MISSING_ISSUER_LEI = "MISSING_ISSUER_LEI"
    INVALID_ISSUER_LEI = "INVALID_ISSUER_LEI"
    UNRESOLVABLE_LEGAL_JURISDICTION = "UNRESOLVABLE_LEGAL_JURISDICTION"
    NON_ES_LEGAL_JURISDICTION = "NON_ES_LEGAL_JURISDICTION"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"


@dataclass(frozen=True)
class SecurityRecord:
    """One candidate security + issuer identity observation.

    ``instrument_isin`` is carried for audit only and is NEVER used to decide a
    branch (NO ISIN-prefix heuristics, G0-B).

    ``instrument_mifir_id`` is the RTS 2 field 3 MiFIR identifier (``BOND`` for
    the profile); ``instrument_bond_type`` is the RTS 2 field 9 bond type
    (``CRPB`` for the profile), sourced from ESMA FITRS transparency reference
    data — ``None`` when the source cannot determine it.

    ``gleif_resolved`` is False when GLEIF has no record for the LEI.
    ``conflicting_jurisdictions`` carries two or more distinct jurisdiction
    codes observed for the same issuer identity (contradictory records).
    ``identity_conflict`` is an explicit upstream contradiction flag.
    """

    instrument_mifir_id: str | None
    instrument_bond_type: str | None = None
    issuer_lei: str | None = None
    gleif_legal_jurisdiction_country: str | None = None
    gleif_resolved: bool = True
    conflicting_jurisdictions: tuple[str, ...] = ()
    identity_conflict: bool = False
    instrument_isin: str | None = None
    source_report_id: str | None = None


@dataclass(frozen=True)
class Disposition:
    """Result of resolving one ``SecurityRecord`` to a frozen branch."""

    instrument_mifir_id: str
    instrument_bond_type: str | None
    issuer_lei: str | None
    legal_jurisdiction_country: str | None
    branch: Branch
    reason: Reason

    def to_canonical(self) -> dict[str, str | None]:
        """Deterministic, hash-stable row (no timestamps, no ordering noise)."""
        return {
            "instrument_mifir_id": self.instrument_mifir_id,
            "instrument_bond_type": self.instrument_bond_type,
            "issuer_lei": self.issuer_lei,
            "legal_jurisdiction_country": self.legal_jurisdiction_country,
            "branch": self.branch.value,
            "reason": self.reason.value,
        }
