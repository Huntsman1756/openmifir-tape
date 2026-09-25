"""OpenMiFIR Tape: G0-E cross-source metrics module.

G0-E1 scope. Computes descriptive cross-source metrics over the VALIDATED
INCLUDE subset of the frozen es-corporate-bonds sample
(``universe.sample_validation.load_validated_scope``) from captured raw
objects. Metrics are DESCRIPTIVE observations of an intact capture only —
``interpretation_limits`` and ``NO_COVERAGE_CLAIM`` are enforced invariants,
not documentation.
"""

from .cross_source import (
    FORBIDDEN_CLAIM_PATTERNS,
    INTERPRETATION_LIMITS,
    compute_cross_source,
    scan_forbidden_claims,
)
from .selection import SelectionError, select_eligible

__all__ = [
    "FORBIDDEN_CLAIM_PATTERNS",
    "INTERPRETATION_LIMITS",
    "SelectionError",
    "compute_cross_source",
    "scan_forbidden_claims",
    "select_eligible",
]
