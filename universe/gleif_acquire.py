"""GLEIF reference acquisition for the G1-B batch run (operator-local only).

Frozen problem this solves: ``LEI absent from local cache`` is NOT the same
as ``authoritatively unresolved in GLEIF``. The batch resolver may only run
once every LEI that actually requires a jurisdiction lookup has an explicit
acquisition outcome:

- ``RESOLVED``            — a pinned official payload exists for the LEI
                            (the payload itself may still carry no
                            LegalJurisdiction — that is a domain outcome,
                            not an acquisition failure);
- ``NOT_FOUND``           — the official GLEIF interface authoritatively
                            answered HTTP 404 for that LEI;
- ``ACQUISITION_FAILED``  — retrieval was attempted and failed (network
                            error, non-200/404 status, unparseable or
                            LEI-mismatched payload);
- ``NOT_ATTEMPTED``       — no pinned artifact and no recorded attempt.

``ACQUISITION_FAILED`` / ``NOT_ATTEMPTED`` on a REQUIRED LEI invalidates the
G1-B run (HOLD); it MUST NOT be folded into ``UNRESOLVABLE_LEGAL_JURISDICTION``.

Artifacts are write-once under a caller-supplied, gitignored directory:

- ``<LEI>.json``          — verbatim official payload bytes;
- ``<LEI>.notfound.json`` — deterministic marker ``{"lei":…,"result":"NOT_FOUND"}``
                            so the NOT_FOUND outcome is hash-pinned like data;
- ``acquisition-manifest.yaml`` — per-LEI outcomes + hashes + timestamps
                            (operator-local provenance; never committed).

Offline by construction: ``acquire`` takes an injected ``http_get``. The CLI
builds one via ``collectors.net.make_http_get`` restricted to the official
GLEIF host; tests inject fakes. CI never reaches the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .gleif import GleifEntity, parse_gleif

GLEIF_API_HOST = "api.gleif.org"
GLEIF_RECORD_URL = "https://api.gleif.org/api/v1/lei-records/{lei}"

OUTCOME_RESOLVED = "RESOLVED"
OUTCOME_NOT_FOUND = "NOT_FOUND"
OUTCOME_ACQUISITION_FAILED = "ACQUISITION_FAILED"
OUTCOME_NOT_ATTEMPTED = "NOT_ATTEMPTED"
OUTCOMES = (OUTCOME_RESOLVED, OUTCOME_NOT_FOUND,
            OUTCOME_ACQUISITION_FAILED, OUTCOME_NOT_ATTEMPTED)

MANIFEST_NAME = "acquisition-manifest.yaml"
NOTFOUND_SUFFIX = ".notfound.json"
PAYLOAD_SUFFIX = ".json"


class AcquisitionIncomplete(Exception):
    """Raised when required LEIs lack a terminal acquisition outcome.

    Carries the per-LEI census so the operator sees exactly which LEIs are
    ``ACQUISITION_FAILED`` / ``NOT_ATTEMPTED`` — a HOLD, never a fabricated
    domain disposition.
    """

    def __init__(self, census: dict[str, str]) -> None:
        self.census = census
        bad = {k: v for k, v in census.items()
               if v in (OUTCOME_ACQUISITION_FAILED, OUTCOME_NOT_ATTEMPTED)}
        super().__init__(
            f"GLEIF reference acquisition incomplete: {len(bad)} required "
            f"LEI(s) without a terminal outcome "
            f"({', '.join(f'{k}={v}' for k, v in sorted(bad.items()))})")


def _norm_lei(lei: str) -> str:
    return lei.strip().upper()


def payload_path(gleif_dir: Path, lei: str) -> Path:
    return gleif_dir / f"{_norm_lei(lei)}{PAYLOAD_SUFFIX}"


def notfound_path(gleif_dir: Path, lei: str) -> Path:
    return gleif_dir / f"{_norm_lei(lei)}{NOTFOUND_SUFFIX}"


def _notfound_bytes(lei: str) -> bytes:
    return (json.dumps({"lei": _norm_lei(lei), "result": "NOT_FOUND"},
                       sort_keys=True, separators=(",", ":")) + "\n") \
        .encode("utf-8")


def acquire_leis(
    required_leis: list[str] | set[str],
    gleif_dir: Path,
    http_get,
    *,
    retrieved_at: str | None = None,
    delay_s: float = 0.0,
) -> dict[str, Any]:
    """Acquire every required LEI once; write-once, resumable, deterministic
    in structure (timestamps live in the local manifest only).

    Existing ``<LEI>.json`` / ``<LEI>.notfound.json`` artifacts are never
    re-fetched and never overwritten — a second run is a no-op on the
    cache and converges the manifest to the same outcome set.
    """
    gleif_dir.mkdir(parents=True, exist_ok=True)
    stamp = retrieved_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    outcomes: dict[str, dict[str, Any]] = {}
    for i, raw_lei in enumerate(sorted(_norm_lei(lei) for lei in required_leis)):
        lei = _norm_lei(raw_lei)
        payload = payload_path(gleif_dir, lei)
        marker = notfound_path(gleif_dir, lei)
        entry: dict[str, Any] = {"lei": lei, "retrieved_at": stamp}
        if payload.is_file():
            data = payload.read_bytes()
            entry.update(outcome=OUTCOME_RESOLVED, http_status=200,
                         sha256=hashlib.sha256(data).hexdigest(),
                         bytes=len(data), cached=True)
        elif marker.is_file():
            entry.update(outcome=OUTCOME_NOT_FOUND, http_status=404,
                         sha256=hashlib.sha256(marker.read_bytes())
                         .hexdigest(),
                         bytes=marker.stat().st_size, cached=True)
        else:
            if i and delay_s:
                time.sleep(delay_s)
            try:
                data = http_get(GLEIF_RECORD_URL.format(lei=lei))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    marker.write_bytes(_notfound_bytes(lei))
                    entry.update(outcome=OUTCOME_NOT_FOUND, http_status=404,
                                 sha256=hashlib.sha256(marker.read_bytes())
                                 .hexdigest(),
                                 bytes=marker.stat().st_size, cached=False)
                else:
                    entry.update(outcome=OUTCOME_ACQUISITION_FAILED,
                                 http_status=exc.code, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - recorded as ACQUISITION_FAILED per contract
                entry.update(outcome=OUTCOME_ACQUISITION_FAILED,
                             error=f"{type(exc).__name__}: {exc}")
            else:
                try:
                    entities = parse_gleif(data)
                except Exception as exc:  # noqa: BLE001 - recorded as ACQUISITION_FAILED per contract
                    entities = None
                    entry.update(outcome=OUTCOME_ACQUISITION_FAILED,
                                 error=f"unparseable payload "
                                       f"({type(exc).__name__}: {exc})")
                if entities is not None and lei not in entities:
                    entities = None
                    entry.update(outcome=OUTCOME_ACQUISITION_FAILED,
                                 error="payload carries no record for the "
                                       "requested LEI")
                if entities is not None:
                    with open(payload, "xb") as fh:  # write-once
                        fh.write(data)
                    entry.update(outcome=OUTCOME_RESOLVED, http_status=200,
                                 sha256=hashlib.sha256(data).hexdigest(),
                                 bytes=len(data), cached=False)
        outcomes[lei] = entry

    manifest = {
        "artifact": "gleif_lei_acquisition",
        "interface": "GLEIF API v1 /lei-records/{lei} (api.gleif.org)",
        "generated_utc": stamp,
        "lei_count": len(outcomes),
        "outcome_counts": {o: sum(1 for e in outcomes.values()
                                  if e["outcome"] == o) for o in OUTCOMES},
        "leis": outcomes,
    }
    (gleif_dir / MANIFEST_NAME).write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return manifest


def acquired_outcome(gleif_dir: Path, lei: str) -> str:
    """Outcome for one LEI from pinned artifacts alone (offline)."""
    lei = _norm_lei(lei)
    if payload_path(gleif_dir, lei).is_file():
        return OUTCOME_RESOLVED
    if notfound_path(gleif_dir, lei).is_file():
        return OUTCOME_NOT_FOUND
    return OUTCOME_NOT_ATTEMPTED


def load_acquired_entities(
    gleif_dir: Path,
    required_leis: set[str],
) -> tuple[dict[str, GleifEntity], dict[str, str]]:
    """Resolve required LEIs against the pinned acquisition artifacts.

    Returns ``(entities, census)`` where ``census[LEI]`` is that LEI's
    acquisition outcome. A ``RESOLVED`` payload whose bytes no longer parse
    or which carries a different LEI is treated as ``ACQUISITION_FAILED``
    (corrupt reference data is an acquisition defect, not a domain result).
    Raises :class:`AcquisitionIncomplete` when any required LEI is
    ``ACQUISITION_FAILED`` or ``NOT_ATTEMPTED``.
    """
    entities: dict[str, GleifEntity] = {}
    census: dict[str, str] = {}
    for raw in required_leis:
        lei = _norm_lei(raw)
        outcome = acquired_outcome(gleif_dir, lei)
        if outcome == OUTCOME_RESOLVED:
            try:
                parsed = parse_gleif(payload_path(gleif_dir, lei)
                                     .read_bytes())
            except Exception:  # noqa: BLE001 - corrupt pinned payload -> acquisition defect
                parsed = {}
            entity = parsed.get(lei)
            if entity is None:
                outcome = OUTCOME_ACQUISITION_FAILED
            else:
                entities[lei] = entity
        elif outcome == OUTCOME_NOT_FOUND:
            # Authoritative absence: the resolver sees an unresolved LEI.
            entities[lei] = GleifEntity(lei=lei,
                                        legal_jurisdiction_country=None,
                                        resolved=False)
        census[lei] = outcome
    incomplete = {k: v for k, v in census.items()
                  if v in (OUTCOME_ACQUISITION_FAILED, OUTCOME_NOT_ATTEMPTED)}
    if incomplete:
        raise AcquisitionIncomplete(census)
    return entities, census


def main(argv: list[str] | None = None) -> int:
    """Operator-local acquisition: one LEI per line on --leis-file.

    ``--live`` performs real HTTPS retrieval against api.gleif.org through
    the fail-closed transport; without it the command only audits the
    existing pinned artifacts (never touches the network).
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--leis-file", type=Path, required=True)
    ap.add_argument("--gleif-dir", type=Path, required=True)
    ap.add_argument("--live", action="store_true",
                    help="fetch missing LEIs from api.gleif.org")
    ap.add_argument("--delay-s", type=float, default=0.35)
    args = ap.parse_args(argv)

    leis = [line.strip() for line in
            args.leis_file.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if args.live:
        from collectors.net import make_http_get
        http_get = make_http_get([GLEIF_API_HOST])
    else:
        def http_get(url: str) -> bytes:
            raise RuntimeError(f"--live not set; refusing network: {url}")
    manifest = acquire_leis(leis, args.gleif_dir, http_get,
                            delay_s=args.delay_s)
    counts = manifest["outcome_counts"]
    print(f"LEIs={manifest['lei_count']} "
          + " ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
