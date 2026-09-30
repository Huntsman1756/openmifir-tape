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

The verified capture baseline follows frozen [`docs/gates/G0.md`](docs/gates/G0.md).
The audit gate is [`G1`](docs/gates/G1.md): the Explainable Audit Tape over
the sealed observed corpus, with the same regulatory and lifecycle semantics.

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

G1 adds a local Parquet tape queried through embedded DuckDB, point-in-time
reconstruction, metadata evidence capsules, two-layer offline verification,
and [source-divergence/observed-fragmentation reports](docs/g1-divergence.md).
`known_at` is the first eligible observation, distinct from trading and
publication time. `as_of(T)` rebuilds the ledger; it never filters final heads.
Reference classification remains the declared frozen `BUILD_SNAPSHOT`.

G1's corpus is one sealed September 2026 window, not a continuously updated
market dataset. The tape contains all observed regulatory dispositions;
fragmentation metrics use only INCLUDE instruments. UI, API, historical
backfill and market-total estimation are outside this gate.

See the [project status and continuation decisions](docs/project-status.md) and
the [formal G1 evaluation](evidence/g1-d3/G1-D3_gate_evaluation.yaml).

## Hard rules

These are enforced, not aspirational (see [`AGENTS.md`](AGENTS.md)):

- **No provider data in this repository.** Raw payloads live only in the
  gitignored `data/` directory. `tools/check_hygiene.py` scans tracked files
  for payload shapes and known credential patterns in CI (best-effort
  detection, not proof of absence).
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
configured in [`config/sources/`](config/sources), never hard-coded. The
descriptors are a runtime resource: they ship inside the wheel (as the
`config.sources` package), so an installed `omt-g0a1`/`omt-g0a3`
loads them without a checkout and regardless of the working directory.
`--config-dir` overrides the packaged descriptors explicitly; a missing or
invalid descriptor set fails closed with `ConfigError`.

### Audit the local tape (offline)

```bash
python -m metrics.g1c2 --tape-dir data/g1/tape --at 2026-09-23T00:00:00Z
omt-explain --tape-dir data/g1/tape --authority-dir . --chain '<chain-id>'
omt-verify data/g1/tape --authority-dir .
omt-fragmentation --tape-dir data/g1/tape --authority-dir . \
    --at 2026-09-24T07:00:00Z --out evidence/g1-d2
```

`--authority-dir` points to a root containing the curated `evidence/` layout.
For an installed wheel outside a checkout, provide that root and the local
tape explicitly. Missing or changed pinned metadata fails closed; it never
falls back to another authority root. The wheel contains code and source
descriptors, not evidence or provider data.

Self-verification checks the tape and declared metadata; it emits full
provenance `NOT_RUN`. To authenticate original input bytes as well, supply
`--bundle`, `--fitrs`, `--firds` and `--gleif-dir` to `omt-verify`.
Two equivalent materializations are required separately for reproducibility.

### Scheduling

Reference systemd units live in [`ops/systemd/`](ops/systemd); the operator
runbook is [`ops/A3-runbook.md`](ops/A3-runbook.md). A3 serializes concurrent
runs with an on-disk lock (`fcntl` on POSIX, `msvcrt` on Windows) and records
`SKIPPED_LOCKED` events in `data/a3/journal.jsonl`.
Operational A3 manifests default to `data/a3/evidence/g0-a3/runs/`;
versioned `evidence/` holds curated gate artifacts only.

## Development

```bash
pip install -e ".[dev]"

python -m pytest              # offline test suite
ruff check .                  # lint (rule set pinned in pyproject.toml)
python tools/check_hygiene.py # tracked-file guard: payloads, evidence shape, secret patterns
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the task-contract workflow.

## Repository layout

```
collectors/     acquisition: sources/, storage, harness, A1/A2/A3 runners
normalization/  lossless source->normalized mapping, timestamps, quantities
universe/       es_legal_issuer_v1 resolution, FITRS/FIRDS/GLEIF parsers
lifecycle/      append-only event ledger, duplicates, evidence
metrics/        authenticated tape, as-of/explain/verify, observed fragmentation
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
`defusedxml`; the live transport (`collectors/net.py`) restricts fetches to
per-source `allowed_hosts` over `http`/`https`, refuses non-public resolved
addresses, and re-validates every redirect hop.

## License

MIT — see [`LICENSE`](LICENSE). The license covers the repository's code,
fixtures, and documentation **only**. Third-party data and source materials
(BME, Bloomberg, ESMA, GLEIF, …) remain subject to their respective terms of
use — see [`NOTICE`](NOTICE) and [`docs/legal/`](docs/legal/).
