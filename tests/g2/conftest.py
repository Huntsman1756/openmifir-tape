"""Synthetic authority only; no live/provider data."""

import pytest


@pytest.fixture
def window():
    return {
        "schema": "g2-window-profile-v1", "profile_version": "synthetic-v1",
        "sources": ["bme_apa"], "descriptor_sha256": {"bme_apa": "a" * 64},
        "start": "2026-10-01T00:00:00Z", "end": "2026-10-03T00:00:00Z",
        "frozen_at": "2026-09-30T00:00:00Z", "expected_trading_dates": ["2026-10-01"],
        "calendar_basis": "synthetic explicit dates", "schedule_authority": "docs/gates/G0.md#G0-A",
        "evidence_tail_policy": {
            "subject_period_end": "2026-10-02T00:00:00Z", "duration_seconds": 86400,
            "authority": "synthetic tail; no operational verdict",
        },
    }
