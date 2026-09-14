# G0 Issue Series — Task Contracts

Frozen task contracts for the G0 execution of OpenMiFIR Tape, profile `es-corporate-bonds`. Each issue is created in GitHub verbatim. Semantics live in `docs/gates/G0.md`; these contracts define scope only.

---

## G0-A1 — Acquisition smoke (agent task)

```
ID: G0-A1
Gate: G0-A

Objective:
Implement collectors for BME APA post-trade JSON and Bloomberg APAE
delayed trade reports, and execute the first acquisition run for the
frozen 15+5 ISIN corpus.

Authority:
docs/gates/G0.md#G0-A
fixtures/universe/ (universe snapshot, selection seed, selected_isins)

Allowed scope:
- collectors/ (BME APA, Bloomberg APAE)
- capture scheduler wiring (polling <= 1h, overlap >= 2h)
- raw storage layout (immutable, write-once)
- SHA-256 manifest generation
- evidence tooling
No normalization. No universe logic. No query layer.

Done when:
- 2/2 sources acquired automatically in the first run
- raw objects stored immutably with raw_sha256 recorded
- observation timestamps recorded
- evidence/g0-a1/ contains manifest + run log + counts (no payloads)

Evidence:
evidence/g0-a1/
```

## G0-A2 — Idempotence (agent task)

```
ID: G0-A2
Gate: G0-A
Depends on: G0-A1

Objective:
Execute a second ingest of already-captured objects and prove zero
unintended state changes.

Authority:
docs/gates/G0.md#G0-A (SECOND_RUN_IDEMPOTENT)

Allowed scope:
- ingest idempotence logic
- state-change diff report

Done when:
- second run produces zero unintended state changes
- diff report committed to evidence/g0-a2/

Evidence:
evidence/g0-a2/
```

## G0-A3 — Capture window complete (operational verification)

```
ID: G0-A3
Gate: G0-A
Depends on: G0-A1, G0-A2
Opens only after 5 trading days of scheduled runs.

Objective:
Verify CAPTURE_WINDOW_COMPLETE: 5 trading days captured with daily
rescan (previous 24h, keyed by publication_timestamp) and 7-day rolling
rescan operating.

Authority:
docs/gates/G0.md#G0-A (CAPTURE_WINDOW_COMPLETE)

Allowed scope:
- window completeness report

Done when:
- 5 trading days present with no unexplained gaps
- catch-up/retry log for any outage

Evidence:
evidence/g0-a3/
```

## G0-B1 — Universe construction (agent task)

```
ID: G0-B1
Gate: G0-B

Objective:
Implement es_legal_issuer_v1: FIRDS download, issuer LEI extraction
(RTS 23 field 5), GLEIF LegalJurisdiction resolution, and the frozen
disposition table with fixture-based verification of every branch
(INCLUDE / EXCLUDE / QUARANTINE / CONFLICT), including negative controls.

Authority:
docs/gates/G0.md#G0-B
fixtures/universe/ (disposition fixtures)

Allowed scope:
- universe/ module
- FIRDS + GLEIF acquisition and caching
- fixture runner

Done when:
- all disposition fixtures resolve to their expected branch
- universe_snapshot_sha256 recorded
- MISSING_IDENTITY_FAIL_CLOSED demonstrated by fixtures

Evidence:
evidence/g0-b1/
```

## G0-C1 — Normalization (agent task)

```
ID: G0-C1
Gate: G0-C
Depends on: G0-A1, G0-B1

Objective:
Implement raw -> normalized transformation with lossless field
preservation, RTS 2 field mapping, price notation, quantity/notional
sanity checks, and exact timestamp handling.

Authority:
docs/gates/G0.md#G0-C

Allowed scope:
- normalization/ module
- normalization tests on captured fixtures

Done when:
- all G0-C gates produce PASS/FAIL with artifacts
- raw -> normalized -> ledger reproducible

Evidence:
evidence/g0-c1/
```

## G0-D1 — Lifecycle reconstruction (agent task)

```
ID: G0-D1
Gate: G0-D
Depends on: G0-C1

Objective:
Implement deferral state, partial publication, amendment, cancellation
and supersession chain reconstruction; source-exact duplicate detection
and economic duplicate candidate flagging. No deletion ever.

Authority:
docs/gates/G0.md#G0-D

Allowed scope:
- lifecycle/ module
- chain reconstruction tests

Done when:
- chains observed in corpus are reconstructed correctly
- NO_HEURISTIC_DELETION enforced by test

Evidence:
evidence/g0-d1/
```

## G0-E1 — Cross-source market evidence (agent task)

```
ID: G0-E1
Gate: G0-E
Depends on: G0-D1, G0-A3

Objective:
Produce cross-source metrics (unique ISINs, trades, intersections,
source-exclusive sets, notional/trades by source) without coverage
claims.

Authority:
docs/gates/G0.md#G0-E

Allowed scope:
- metrics/ module + report generation

Done when:
- COVERAGE_MEASURED report exists
- NO_COVERAGE_CLAIM holds (no completeness language anywhere)

Evidence:
evidence/g0-e1/
```

## G0-L1 — Legal / ToU classification (agent task)

```
ID: G0-L1
Gate: G0-L
Can run in parallel with G0-A1.

Objective:
Snapshot source terms of use for BME APA and Bloomberg APAE, and
classify the legal status of every output artifact class under
LOCAL_ARCHIVAL, FREE_DELAYED_REDISTRIBUTION, and
HISTORICAL_ARCHIVE_REPUBLICATION (PASS | FAIL | UNRESOLVED).

Authority:
docs/gates/G0.md#G0-L

Allowed scope:
- docs/legal/ (snapshots with URLs and dates)
- artifact classification table

Done when:
- SOURCE_TERMS_SNAPSHOTTED for 2/2 sources
- every artifact class carries an explicit legal status
- UNRESOLVED is an acceptable recorded outcome

Evidence:
evidence/g0-l1/
```

---

## Ordering

```
G0-A1 ─→ G0-A2 ─→ G0-A3 ─────┐
G0-B1 ─→ G0-C1 ─→ G0-D1 ─→ G0-E1 ─→ G0 PASS evaluation
G0-L1 (parallel) ─────────────┘
```

G0 PASS requires all criteria in `docs/gates/G0.md` §9 — no partial credit, no gate substitution.
