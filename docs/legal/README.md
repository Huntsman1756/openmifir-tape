# docs/legal/README.md
# Legal / terms-of-use snapshots and artifact classification for G0-L1.

Authority: `docs/gates/G0.md#G0-L`. Snapshot date: 2026-09-15.

## Contents
| File | Purpose |
| --- | --- |
| `bme_apa_terms.md` | BME APA terms-of-use snapshot (source, URLs, fetched date, key-terms summary). |
| `bloomberg_apae_terms.md` | Bloomberg APAE terms-of-use snapshot (source, URLs, fetched date, key-terms summary). |
| `artifact_classification.yaml` | Classification of every output artifact class under LOCAL_ARCHIVAL / FREE_DELAYED_REDISTRIBUTION / HISTORICAL_ARCHIVE_REPUBLICATION (PASS \| FAIL \| UNRESOLVED) with one-line bases. |

## What these docs hold
Summaries, URLs, fetched dates, and statuses — never provider payloads and never
verbatim ToS dumps. Raw provider payloads live only in the local `data/`
directory (gitignored).

## Gradient of confidence
- BME APA term text was fully retrievable (data page + website Legal Disclaimer).
- Bloomberg APAE page-level disclaimer was retrievable, but the linked Terms of Use
  (`https://www.bloomberg.com/tos/`) is bot-blocked (HTTP 403) as of 2026-09-15, so
  the exact ToS content is UNRESOLVED.

## Classification (summary)
| Source | LOCAL_ARCHIVAL | FREE_DELAYED_REDISTRIBUTION | HISTORICAL_ARCHIVE_REPUBLICATION |
| --- | --- | --- | --- |
| bme_apa | PASS | UNRESOLVED | FAIL |
| blb_apae | UNRESOLVED | UNRESOLVED | UNRESOLVED |

See `artifact_classification.yaml` for full bases. Metadata-only evidence is in
`evidence/g0-l1/`.
