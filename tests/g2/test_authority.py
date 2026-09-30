import hashlib

import pytest

from metrics.g2_authority import AuthorityError, checked_file, digest, file_entry, profile, sealed_bundle, utc


def entry(name, content):
    return {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def test_profile_copy_and_canonical_digest(window):
    validated = profile(window)
    window["sources"].clear()
    assert validated["sources"] == ["bme_apa"]
    assert digest(validated) == digest(dict(reversed(list(validated.items()))))


@pytest.mark.parametrize(("field", "value"), [
    ("frozen_at", "2026-10-01T00:00:00Z"),
    ("start", "2026-10-01T00:00:00"),
    ("end", "2026-10-03T01:00:00+01:00"),
    ("sources", ["bme_apa", "bme_apa"]),
    ("expected_trading_dates", ["2026-10-04"]),
    ("expected_trading_dates", ["20261001"]),
    ("schedule_authority", "guessed retry policy"),
])
def test_profile_rejects_invalid_authority(window, field, value):
    window[field] = value
    with pytest.raises(AuthorityError):
        profile(window)


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/escape", "x\\y", "a//b", "./a", "a/../b"])
def test_relative_names_fail_closed(name):
    with pytest.raises(AuthorityError):
        file_entry(entry(name, b"metadata"))


def test_seal_authenticates_every_copy_and_profile(tmp_path, window):
    (tmp_path / "one.yaml").write_bytes(b"synthetic metadata")
    (tmp_path / "two.yaml").write_bytes(b"synthetic metadata")
    (tmp_path / "journal.jsonl").write_bytes(b"{}\n")
    bundle = {
        "schema": "g2-window-bundle-v1", "profile_sha256": digest(window),
        "manifests": [entry(n, b"synthetic metadata") for n in ("one.yaml", "two.yaml")],
        "journal": entry("journal.jsonl", b"{}\n"), "acquisition_revision": "a" * 40,
        "deployment_revision": "b" * 40, "sealed_at": window["end"],
    }
    assert sealed_bundle(tmp_path, bundle, window) == bundle
    (tmp_path / "two.yaml").write_bytes(b"altered duplicate")
    with pytest.raises(AuthorityError, match="integrity"):
        sealed_bundle(tmp_path, bundle, window)
    bundle["profile_sha256"] = "c" * 64
    with pytest.raises(AuthorityError, match="mismatch"):
        sealed_bundle(tmp_path, bundle, window)


def test_missing_metadata_is_not_skipped(tmp_path):
    with pytest.raises(AuthorityError, match="missing"):
        checked_file(tmp_path, entry("missing", b""))


def test_timing_and_tail_are_independent(window):
    window["evidence_tail_policy"]["duration_seconds"] = True
    with pytest.raises(AuthorityError, match="tail"):
        profile(window)
    assert utc("2026-10-01T00:00:00Z").isoformat().endswith("+00:00")
