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

The report/CLI and real-corpus evidence are the next work unit (G1-D2).
