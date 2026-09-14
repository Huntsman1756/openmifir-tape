"""Command-line entry point for G0-A1 acquisition (operator/live runs).

This is the ACQUISITION tool. It performs network requests to data providers
and therefore runs in the operator environment, NOT in CI. CI executes only the
deterministic, offline fixture-based tests in tests/.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import load_all_sources
from .harness import run_source, write_evidence
from .sources.base import HttpGetter
from .storage import RawStore


def default_http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "OpenMiFIRTape/0.0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omt-g0a1", description="G0-A1 acquisition")
    parser.add_argument("--source", default="all", choices=["all", "bme_apa", "blb_apae"])
    parser.add_argument("--config-dir", type=Path, default=Path("config/sources"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--max-objects", type=int, default=1)
    args = parser.parse_args(argv)

    configs = load_all_sources(args.config_dir)
    wanted = list(configs) if args.source == "all" else [args.source]

    run_id = _run_id()
    http_get: HttpGetter = default_http_get
    store = RawStore(args.raw_dir)
    summary: list[str] = []

    for source_id in wanted:
        if source_id not in configs:
            print(f"ERROR: no config for source_id={source_id}", file=sys.stderr)
            return 2
        conf = configs[source_id]
        result = run_source(source_id, conf, store, http_get, max_objects=args.max_objects)
        manifest_path = write_evidence(args.evidence_dir, source_id, result, run_id)
        summary.append(f"{source_id}: status={result.status} objects={result.object_count}")
        if result.errors:
            summary.append(f"  errors: {result.errors}")
        summary.append(f"  evidence: {manifest_path}")

    for line in summary:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
