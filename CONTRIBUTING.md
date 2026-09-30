# Contributing to OpenMiFIR Tape

Thanks for your interest. This project is specification-driven: before
changing behavior, read [`AGENTS.md`](AGENTS.md) (permanent project rules) and
the issue's relevant frozen authority: [`G0`](docs/gates/G0.md) for capture
baseline work, [`G1`](docs/gates/G1.md) for the verified audit tape,
and [`G2`](docs/gates/G2.md) for successor temporal/history work.

## Authority hierarchy

```
docs/gates/G<gate>.md  (frozen specification for the active work item)
        >
GitHub Issue / task contract
        >
implementation detail
```

Issues define *scope*, not *semantics*. If a change would alter behavior
frozen by the applicable gate, that is a spec discussion first — not a PR. Deviations must
be recorded explicitly in the PR description; silent deviation is not allowed.

## Ground rules (enforced in CI)

- **No provider data.** Never commit files under `data/` or anything that
  contains raw provider payloads. `tools/check_hygiene.py` fails the build.
- **Evidence is metadata only.** `evidence/` holds manifests, hashes, counts,
  timestamps, and sanitized logs — never payload bytes.
- **CI is offline.** Tests must not perform network access. Use synthetic
  fixtures and injected HTTP getters (`HttpGetter`) like the existing tests.
- **Fail closed.** No fuzzy matching, no heuristic merging or deletion, no
  automatic overrides. Ambiguity resolves to `QUARANTINE`/`CONFLICT`.
- **Raw is immutable.** Never overwrite a captured object or its metadata.

## Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    POSIX: source .venv/bin/activate
pip install -e ".[dev]"
```

## Validate before submitting

```bash
ruff check .                  # lint — rule set is pinned in pyproject.toml
python -m pytest              # offline suite — all tests must pass
python tools/check_hygiene.py # repository hygiene guard
```

## Testing conventions

- Tests live in `tests/` and must be deterministic and offline.
- Inject HTTP via the `HttpGetter` callable; never call real endpoints.
- Test failure modes and fail-closed behavior, not just happy paths.
- Do not add tests purely for coverage metrics.
- CI uses a unique directory under `runner.temp` for synthetic test files,
  and reports the ten slowest cases. This avoids expensive Windows process-temp
  filesystem operations without changing test assertions.
- Installed-wheel audit smoke runs with `python -I`: runtime imports must
  come from the wheel, synthetic tests supply fixtures only, and explicit
  missing/mutated metadata authority must fail closed.

## Pull requests

- Keep PRs scoped to a single gate/task contract where possible.
- Describe *why*, and record any deviation from the frozen spec explicitly.
- CI runs lint, the hygiene guard, and the offline test suite on Linux and
  Windows across supported Python versions — all must be green.
