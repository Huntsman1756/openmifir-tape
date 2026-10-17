# G3 Issue Series — Task Contracts (DRAFT, pre-freeze)

Task contracts for the G3 execution of OpenMiFIR Tape, profile
`es-corporate-bonds`. Semantics live in [`docs/gates/G3.md`](docs/gates/G3.md);
these contracts define scope only.

**Status: DRAFT.** G3 is not frozen. Per [`AGENTS.md`](AGENTS.md), an issue is
only moved to `ready-for-agent` when its Authority section exists and is frozen,
so **none of these contracts may be created yet**. This file is prepared so the
series can be issued verbatim once the `g3-freeze-v0` tag exists. Nothing here
acquires data, consumes the official CTP, or changes any existing gate.

---

## G3-A1 — Pinned ISO 20022 CTP schema authority (agent task)

```
ID: G3-A1
Gate: G3-A
No tape dependency; may run first.

Objective:
Pin a timed, immutable schema snapshot of the ISO 20022 consolidated-tape
message definitions and external code sets the bonds profile needs, with
input/output declared separately.

Authority:
docs/gates/G3.md#G3-A

Allowed scope:
- schema snapshot acquisition (operator environment only)
- snapshot validation, attestation and set-digest tooling
- census of pinned definitions and referenced code sets

Done when:
- the snapshot binds identifiers, versions, provenance, observation UTC and
  {name, sha256, bytes} entries with set digests
- input (contributor->CTP) and output (CTP->consumer) definitions are declared
  separately; a hybrid snapshot is rejected
- missing/ambiguous/divergent artifacts fail closed; draft artifacts are
  labelled DRAFT_SCHEMA_BASIS
- evidence/g3-a1/ holds metadata only (no schema payloads unless G3-L permits)

Evidence:
evidence/g3-a1/
```

## G3-L1 — Legal classification of schema and CTP artifacts (agent task)

```
ID: G3-L1
Gate: G3-L
No tape dependency; may run in parallel with G3-A1.

Objective:
Snapshot the ISO 20022 message-definition terms and the CTP access/reuse
conditions, and classify SCHEMA_ARTIFACT_VENDORING, CTP_OUTPUT_REDISTRIBUTION
and DERIVED_MESSAGE_PUBLICATION (PASS | FAIL | UNRESOLVED).

Authority:
docs/gates/G3.md#G3-L

Allowed scope:
- docs/legal/ snapshots with URLs and dates
- G3-L classification table

Done when:
- every G3-L class carries an explicit status; UNRESOLVED is acceptable
- where vendoring is not PASS, the repository holds only digests and the
  public location, and schema files stay under data/
- existing G0-L classifications are unchanged

Evidence:
evidence/g3-l1/
```

## G3-B1 — Deterministic structural projection (agent task)

```
ID: G3-B1
Gate: G3-B
Depends on: G3-A1, and a verified G2-C1 immutable build.

Objective:
Project an admitted immutable build into ISO 20022 message instances as a pure
function of build ID, schema snapshot, direction, target definition and
code-mapping registry version.

Authority:
docs/gates/G3.md#G3-B

Allowed scope:
- metrics/ projection module + versioned code-mapping registry
- projection identity and idempotence logic

Done when:
- the projection re-derives no universe, lifecycle or temporal semantics
- every value crossing a code boundary has an explicit mapping or an explicit
  G3_UNMAPPED_<field> reason; unmapped instances are PROJECTION_INCOMPLETE
- re-projecting the same authority under the same snapshot is a no-op
  (G3-PROJECTION-IDEMPOTENT)
- distinct observations never collapse by economic similarity

Evidence:
evidence/g3-b1/
```

## G3-B2 — Structural validation and census (agent task)

```
ID: G3-B2
Gate: G3-B
Depends on: G3-B1.

Objective:
Validate every emitted instance at the schema level and emit a complete census
of emitted/valid/invalid/incomplete instances with reasons.

Authority:
docs/gates/G3.md#G3-B

Allowed scope:
- schema-level validation (syntax, logical model, declared code sets)
- census and diagnostics

Done when:
- no instance is silently dropped, repaired or coerced; parse failures remain
  visible census entries
- every schema-valid instance carries STRUCTURAL_COMPATIBILITY_ONLY
- unknown-code and missing-mandatory counterexamples fail closed

Evidence:
evidence/g3-b2/
```

## G3-C1 — Round-trip and loss accounting (agent task)

```
ID: G3-C1
Gate: G3-C
Depends on: G3-B1.

Objective:
Parse each emitted instance back through the pinned snapshot and classify every
field as CARRIED, NOT_MODELLED or LOSSY with stated reasons.

Authority:
docs/gates/G3.md#G3-C

Allowed scope:
- round-trip comparison and loss classification
- metadata-only comparison capsules

Done when:
- every field is classified exactly once; no lossless claim without proof
- evidence emits counts, per-class tallies and digests only (economic values
  hashed, never emitted)
- two clean replays produce identical verdicts and byte-identical capsules

Evidence:
evidence/g3-c1/
```

## G3-D1 — Official bonds CTP benchmark-readiness contract (agent task)

```
ID: G3-D1
Gate: G3-D
Depends on: G3-A1 and recorded G3-L1 statuses.

Objective:
Declare the interface by which the future official EU bonds tape would be
admitted as a benchmark source, without consuming it.

Authority:
docs/gates/G3.md#G3-D

Allowed scope:
- declared interface contract (identity, timing, pinning, comparison, access
  conditions)

Done when:
- CTP status is NOT_CONSUMED and it appears nowhere as evidence
- no live acquisition, no assumed endpoint, no CTP content claim
- mandatory limits are carried verbatim (NO_COVERAGE_CLAIM,
  NO_MARKET_SHARE_INFERENCE, NO_ABSENCE_OF_TRADING_INFERENCE)

Evidence:
evidence/g3-d1/
```

## G3-E1 — Evidence and PASS evaluation (evaluation)

```
ID: G3-E1
Gate: G3-E
Depends on: G3-A1, G3-B1, G3-B2, G3-C1, G3-D1, G3-L1.

Objective:
Evaluate each G3 PASS criterion explicitly, cite committed metadata and show
PASS or REVIEW_REQUIRED.

Authority:
docs/gates/G3.md#G3-E

Allowed scope:
- gate evaluation report

Done when:
- all ten criteria are evaluated with cited evidence
- no schema-valid projection is presented as authorisation, conformance or
  market coverage

Evidence:
evidence/g3-e1/
```

---

## Ordering

```
G3-A1 ─────────────┐
G3-L1 (parallel) ──┤
                   ├─→ G3-B1 ─→ G3-B2 ─┐
G2-C1 (verified) ──┘        └─→ G3-C1 ─┤
                                        ├─→ G3-E1 (PASS evaluation)
                   G3-D1 (after A1/L1) ─┘
```

G3 PASS requires all criteria in `docs/gates/G3.md` §7 — no partial credit, no
gate substitution. Issuing these contracts requires the `g3-freeze-v0` tag.
