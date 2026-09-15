# AGENTS.md — OpenMiFIR Tape

Permanent project rules. Every agent, human, or automation operating on this repository MUST read this file before acting.

## Project

OpenMiFIR Tape — reproducible, auditable post-trade transparency laboratory for MiFIR bond data. First profile: `es-corporate-bonds`. We are NOT building a CTP and NOT competing with fairCT. When the official EU bonds CTP goes live, it becomes a source and a benchmark.

## Authority hierarchy

```
frozen specification (docs/gates/G0.md)
        >
GitHub Issue
        >
agent inference
```

- Task instructions MUST reference repository authority.
- If an issue references `docs/gates/G0.md#G0-A`, the agent MUST read that section and its dependencies before acting.
- Issues MUST NOT restate or reinterpret the frozen gate specification. An issue defines scope, not semantics.
- In any conflict: frozen specification > issue > agent inference. Deviations are recorded in the PR description, never silent.

## Hard rules

1. **No provider data in this repository.** No raw provider payloads, no normalized historical trades. The public repo holds code, schemas, tests, manifests, and documentation only. Provider data lives in the local data directory (`data/`, gitignored).
2. **Evidence is metadata, never payload.** `evidence/` directories contain manifests, SHA-256 hashes, counts, timestamps, and logs — not provider file contents.
3. **Acquisition runs locally.** Collectors execute on the operator's environment (VPS), on schedule. CI runs deterministic, offline, fixture-based tests only. CI MUST NOT make network requests to data providers.
4. **No infrastructure ahead of the gate.** No orchestrator, no UI, no agent framework, no scheduler beyond what the current issue's gate requires. The G0 question is acquisition, not platform.
5. **Fail closed.** Missing issuer LEI, unresolvable LegalJurisdiction, or identity conflicts QUARANTINE or CONFLICT the record. No fuzzy matching. No automatic overrides. No heuristics that delete or merge records — the ledger is append-only with explicit supersession links.
6. **Raw is immutable.** Captured files are write-once. SHA-256 recorded at capture. Re-runs must not alter existing raw objects.
7. **Second ingest must produce zero unintended state changes.** Idempotence is a gate, not a nicety.
8. **No coverage claims.** We measure observed fragmentation across sources. We never claim market completeness we cannot demonstrate.

## Universe rule (summary)

`es_legal_issuer_v1`: instrument MiFIR ID = CRPB, issuer LEI from FIRDS field 5, GLEIF LegalJurisdiction.country = ES. No ISIN-prefix heuristics. Full disposition table, fixtures, and the 15+5 frozen sample live in `docs/gates/G0.md` and `fixtures/`.

## Lifecycle model

A trade is a sequence of states (EXECUTED → PARTIALLY_PUBLISHED / DEFERRED → FULLY_PUBLISHED → AMENDED → CANCELLED), reconstructed from successive publications. The event log records observations; records are never overwritten, only superseded via `supersedes_entry_id`.

## Task contract format

Every work item is a GitHub Issue with:

```
ID: G0-<letter><number>
Gate: G0-<letter>
Objective: one sentence
Authority: docs/gates/G0.md#G0-<letter>
Allowed scope: ...
Done when: gate criteria satisfied
Evidence: evidence/<issue-id>/
```

Nothing more. The canonical definitions (raw_sha256, second ingest, capture windows, universe, legal gates) are read from the specification, never re-drafted.

## Labels / state machine

`planned` → `ready-for-agent` → `running` → `ready-for-review` → `verified` → `closed`

An issue is only moved to `ready-for-agent` when its Authority section exists and is frozen. `verified` requires evidence artifacts present and CI green.

## Agent task vs operational task

Agents implement code, fixtures, and evidence tooling in bounded runs. Multi-day acquisition windows are operated by the scheduler, not by an agent. Issues that verify operational outcomes (e.g., CAPTURE_WINDOW_COMPLETE) are opened only after the scheduled runs have accrued the evidence.
