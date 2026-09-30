# G1 source-divergence ledger

Authority: frozen `docs/gates/G1.md` sections 4.2/5; G0-D/E semantics.
The ledger describes the observed INCLUDE set at one stated `known_at`.

Membership describes instrument observations in the two captures. It does
not identify a trade: an ISIN observed in both sources gives OBSERVED_BOTH
even when its chains have no economic candidate relation.

Candidate groups use the G0-E head economic key (ISIN, quantity, price,
trading time). Incomplete keys remain unmatched. All source chains and
many-to-many candidates remain separate. Candidate relations compare first
publication times, reconstructed head states, and observed amendment/cancel
events. Unpaired chains retain membership; no counterpart is invented.

Reconstruction first filters `known_at <= T`, then replays normalized
records through G0-D. Materialized heads cannot supply historical authority.
Reference classification is explicitly BUILD_SNAPSHOT. Economic keys are
hashed in emitted metadata; no prices, quantities or raw records are emitted.

## Authenticated full-window report

```bash
omt-fragmentation --tape-dir data/g1/tape --at 2026-09-24T07:00:00Z \
    --out evidence/g1-d2
```

This emits observed aggregates, a metadata JSONL ledger and SHA256SUMS.
The tape, G1-B disposition/reference authority, C4 manifest and C1 input
census are authenticated before use. It does not repeat full byte-level
input provenance verification: use `omt-verify` with the original bundle
and reference files for that layer.

The census covers the full sealed window, so the report requires T at or
after its evidence tail. Historical reconstructions use the divergence
core; the full-window census must not become historical observation evidence.
Installed packages can supply `--authority-dir <root>` pointing to the
curated `evidence/` layout, independently of the working directory.
