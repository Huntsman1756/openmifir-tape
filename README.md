# OpenMiFIR Tape

Reproducible, auditable post-trade transparency laboratory for MiFIR bond
data. First profile: `es-corporate-bonds` — Spanish corporate bonds as
published by two independent post-trade transparency sources:

| source_id  | display        | publication format |
|------------|----------------|--------------------|
| `bme_apa`  | BME APA        | JSON               |
| `blb_apae` | Bloomberg APAE | CSV                |

OpenMiFIR Tape is **not** a consolidated tape provider and does not compete
with fairCT. When the official EU bonds CTP goes live, it becomes a source
and a benchmark.

The project measures **observed** fragmentation across sources. It never
claims market completeness it cannot demonstrate.

## What it does

The pipeline is organized around the frozen G0 gate specification
([`docs/gates/G0.md`](docs/gates/G0.md)):

- **Acquisition (G0-A)** — discover, fetch, and persist the *exact* source
  bytes, write-once, with SHA-256 recorded at capture.
- **Idempotence (G0-A2)** — a second ingest produces zero unintended state
  changes; verified by replaying captured bytes locally.
- **Scheduled capture (G0-A3)** — external-trigger poll and rolling-rescan
  runner with a serialized on-disk lock and an append-only journal.
- **Universe (G0-B)** — deterministic, fail-closed resolution of
  `es_legal_issuer_v1` (MiFIR ID `BOND` + Bond Type `CRPB` + issuer LEI with
  GLEIF `LegalJurisdiction.country = ES`). No ISIN-prefix heuristics, no
  fuzzy matching — ambiguity resolves to `QUARANTINE` or `CONFLICT`.
- **Normalization (G0-C)** — lossless mapping of source records into a
  normalized model; raw values are preserved and sanity checks only flag,
  never mutate.
- **Lifecycle (G0-D)** — append-only event ledger reconstructing trade state
  chains with explicit `supersedes_entry_id` links.
- **Legal classification (G0-L)** — terms-of-use snapshot classification per
  source (`docs/legal/`).

## Hard rules

These are enforced, not aspirational (see [`AGENTS.md`](AGENTS.md)):

- **No provider data in this repository.** Raw payloads live only in the
  gitignored `data/` directory. `tools/check_hygiene.py` verifies this in CI.
- **Evidence is metadata, never payload.** `evidence/` holds manifests,
  SHA-256 hashes, counts, timestamps, and sanitized logs only.
- **CI is offline.** Tests use synthetic fixtures and injected HTTP getters;
  CI never contacts BME or Bloomberg.
- **Raw is immutable.** Captured files are write-once; a hash collision on
  an existing identity raises `WriteOnceConflict` rather than overwriting.

## Installation

Requires Python 3.11+.

```bash
pip install -e .          # runtime only
pip install -e ".[dev]"   # + pytest and ruff
```

## Usage

All acquisition commands run in the **operator environment** (they perform
live network requests). They write raw payloads under `data/` and metadata
under `evidence/`.

```bash
# G0-A1 — one-shot acquisition smoke (latest object per source by default)
omt-g0a1 --source all --max-objects 1

# G0-A2 — idempotence: replay captured bytes, verify zero state change
omt-g0a2

# G0-A3 — scheduled capture runner (designed for cron/systemd timers)
omt-g0a3 --mode poll
omt-g0a3 --mode rolling

# F-009 — validate the frozen universe sample against local FITRS/FIRDS/GLEIF
omt-sample-validation evidence/g0-b1/F-009_frozen_sample_validation.yaml \
    --fitrs <fitrs-file> --firds <firds-file> --gleif-cache <dir> \
    --manifest fixtures/universe/universe_manifest.yaml
```

Every command accepts `--help`. Source endpoints and retrieval intervals are
configured in [`config/sources/`](config/sources), never hard-coded.

### Scheduling

Reference systemd units live in [`ops/systemd/`](ops/systemd); the operator
runbook is [`ops/A3-runbook.md`](ops/A3-runbook.md). A3 serializes concurrent
runs with an on-disk lock (`fcntl` on POSIX, `msvcrt` on Windows) and records
`SKIPPED_LOCKED` events in `data/a3/journal.jsonl`.

## Development

```bash
pip install -e ".[dev]"

python -m pytest              # offline test suite
ruff check .                  # lint (rule set pinned in pyproject.toml)
python tools/check_hygiene.py # no payloads / no secrets / evidence is metadata
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the task-contract workflow.

## Repository layout

```
collectors/     acquisition: sources/, storage, harness, A1/A2/A3 runners
normalization/  lossless source->normalized mapping, timestamps, quantities
universe/       es_legal_issuer_v1 resolution, FITRS/FIRDS/GLEIF parsers
lifecycle/      append-only event ledger, duplicates, evidence
fixtures/       synthetic fixtures (no provider payloads)
config/sources/ per-source endpoints and retrieval intervals
evidence/       metadata-only gate evidence (manifests, hashes, logs)
ops/            operator runbook and systemd units
tools/          repository hygiene guard
docs/gates/     frozen gate specifications (authoritative)
docs/legal/     per-source terms-of-use classification
tests/          offline, fixture-based test suite
```

## Security

See [`SECURITY.md`](SECURITY.md). All provider XML is parsed with
`defusedxml`; all fetched URLs are restricted to `http`/`https`.

## License

MIT — see [`LICENSE`](LICENSE).
