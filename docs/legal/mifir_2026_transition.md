# MiFIR 2026 transition — EVIDENCE_CAPSULE compatibility note

Scope: whether the G1-C3 `EVIDENCE_CAPSULE` schema (capsule_schema_version 1)
survives the 2026 MiFIR transition without conflating (a) evidence actually
observed by OpenMiFIR, (b) the reference authority used in universe
classification, and (c) the upstream regulatory calculation basis.

## Regulatory facts (primary sources)

- ESMA, *MiFIR reporting* page: from April 2026 the dedicated FIRDS
  transparency **equity and non-equity quantitative data reporting
  mechanism is decommissioned** — from 21 April 2026 no FITRS `DATEQU` and
  `DATNQU` files can be submitted; the last reporting day for quantitative
  transparency files was **31 March 2026**. The `DRAFT` prefix on the
  non-equity transparency reference-data file (`auth.033`/`DATNTR`) was
  removed effective 20 April 2026 (both forms accepted during transition).
- Commission Delegated Regulation (EU) **2026/392** (amending RTS under
  Delegated Regulation (EU) 2017/577): deletes the obligation for trading
  venues, APAs and CTPs to report the SI-quantitative data; ESMA assessed
  that **transaction data reported under Article 26 of MiFIR** can be used
  for the transparency / volume-cap calculations — the dedicated
  quantitative channel is replaced by Article 26-derived calculations
  upstream of ESMA.
- Regulation (EU) **2024/791** + Directive (EU) 2024/790 (MiFIR review,
  in force 28 March 2024): the quantitative SI test under Art. 4(1)(20)
  MiFID II is removed (qualitative, equity-only assessment remains); the
  DPE regime for OTC post-trade publication applies from 3 February 2025
  and ESMA discontinued the quarterly SI publication.

Consequence for this project: **FITRS outputs keep existing** (transparency
results / reference data publications continue), but the *upstream
production mechanism* of quantitative FITRS content changes: inbound
DATEQU/DATNQU reporting is replaced by Article 26-derived calculations that
ESMA performs on transaction reports OpenMiFIR does not possess.

## The three axes, kept separate in the capsule

Every capsule carries:

| Axis | Capsule location | Owned evidence? |
|---|---|---|
| Observed publication source | `publication.source`, `source_object_id`, `capture_version`, `capture_record_ordinal`, `raw_sha256` | YES — captured bytes pinned by hash |
| Reference authority | `reference_authority.basis` (`BUILD_SNAPSHOT`), `reference_basis_id`, `set_digests`, `observations` (resolved handles) | YES — pinned family sets + byte-authenticated links |
| Regulatory calculation basis | `reference_authority.regulatory.regulatory_calculation_basis` | NO — `upstream_reference_payloads_owned: false` is asserted literally |

The capsule never claims provenance over Art. 26 payloads, ESMA-internal
calculations, or any byte OpenMiFIR did not capture. `explain` resolves
reference observations only as metadata handles inside the pinned family
sets — never payload content.

## Why the schema survives the transition

1. **Publication identity is regime-agnostic.** `publication_id` =
   `sha256(canonical_json({source, source_object_id, capture_version,
   capture_record_ordinal}))`. `source` is an open string (capture-observed
   source id), not a closed enum — a post-transition source family needs no
   schema change.
2. **Reference families are open dict keys.** `set_digests` and
   `observations` are keyed by family name from the authenticated G1-B
   report; `REASON_REFERENCE_STAGE` is a stage map, not a type-level
   closed `Literal["FITRS","FIRDS","GLEIF"]`. A future gate pinning a
   different family set (e.g. a post-transition FITRS layout or a new
   reference snapshot) produces a new `reference_basis_id` — cited as-is.
3. **Stage-aware citation is semantic, not structural.** Which families are
   cited depends on the disposition reason, resolved at explain time
   against authenticated links — a reason/family change surfaces as a new
   stage entry, not a schema break.
4. **Versioned seams exist where semantics could change.**
   `capsule_schema_version`, `authority.tape_schema_version`, and the
   pinned `reference_basis_id` allow a future gate to evolve any axis
   without redefining capsule identity.
5. **Historical capsules are regime-agnostic.** `as_of(T)` semantics depend
   on `known_at`, not on publication schema or regulatory period; a chain
   spanning a transition boundary reconstructs identically
   (`test_regime_transition_identity_and_provenance`).

## Deliberate limits (conscious decisions, not debt)

- The capsule does not model the *production mechanism* of FITRS content;
  it cites the published artifact sets OpenMiFIR pinned. If a future gate
  needs to express "quantitative observation derived from Art. 26
  reporting", that belongs to the reference-authority description of that
  gate — bumping `capsule_schema_version` if structure changes.
- `regulatory_calculation_basis` is a descriptive string, not an
  enumerated regime id; the classification rule itself
  (`es_legal_issuer_v1`) is frozen G0/G1-B authority and unchanged here.
- No Article 26 ingestion, no post-transition FITRS capture: out of scope
  for G1-C3 (see issue #30 scope).

## Sources

- ESMA — *MiFIR reporting* (system updates note, April 2026):
  https://www.esma.europa.eu/data-reporting/mifir-reporting
- ESMA — *Start of DPE regime on 3 February and end of publication of
  Systematic Internalisers data* (January 2025).
- Commission Delegated Regulation (EU) 2026/392, OJ L — amending
  Delegated Regulation (EU) 2017/577 (volume cap / transparency
  calculations; Art. 26 transaction data).
- Regulation (EU) 2024/791 (MiFIR review), OJ L, 8 March 2024.
