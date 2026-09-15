# BME APA — Terms of Use snapshot

Target of G0-L1. Summary + URLs + fetched date, NOT verbatim provider text.
Reviewed: 2026-09-15 (snapshot). Sources inspected live 2026-09-15.

## Source
- Source name: BME APA (Bolsas y Mercados Españoles) — post-trade transparency data
- Source id: `bme_apa` (see `config/sources/bme_apa.yaml`)

## URLs fetched (evidence of inspection)
| URL | Fetched | Result |
| --- | --- | --- |
| `https://www.bolsasymercados.es/en/other-services/regulatory-services/post-trade-data.html` | 2026-09-15 | 200 — live page |
| `https://www.bolsasymercados.es/en/legal/legal-notice.html` | 2026-09-15 | 200 — live page |

Canonical data page (per the source descriptor):
`https://www.bolsasymercados.es/en/other-services/regulatory-services/post-trade-data.html`

## Key-terms summary
1. **Availability / format.** The post-trade transparency data is published as JSON
   files. The page states the files are freely accessible and free of charge,
   readable both by human users and mechanically, and downloadable via the site.
2. **Ownership + permitted use (data-service notice).** The user accepts that the
   data is owned by the Company (BME Group) and may only be downloaded, copied,
   and printed for personal use; the information is intended exclusively for
   internal use.
3. **Commercial / redistribution gate.** For any other use for commercial purposes
   and/or that involves redistribution of the information to third parties in a
   non-free manner, prior express authorisation of BME Market Data
   (`marketdata@grupobme.es`) is required.
4. **General Legal Disclaimer (website-wide).** Content may be downloaded, copied,
   or printed solely for personal use. Copy, duplication, redistribution,
   electronic reproduction, printing, marketing, or any other use of the content
   is not permitted, in whole or in part (even if the source is stated), unless
   there is prior written consent of the Company. Also: the Company can modify or
   withdraw content at any time; the information is not official; Spanish
   legislation governs, with jurisdiction of the civil and mercantile arbitration
   court of Madrid.

## Notes
- There is tension between the data-service notice (authorisation only required for
  *commercial* use and/or *non-free* redistribution) and the general Legal
  Disclaimer (no redistribution at all without prior written consent). No explicit
  licence is granted for free redistribution or for historical republication.
- The terms do not cite EU Regulation 600/2014. For MiFIR post-trade transparency
  obligations the project relies on the source's own statement that the data is
  free and machine-readable; that statement is an availability statement, not a
  redistribution licence.
- Term snapshot permits the project to store raw objects locally for internal
  research (download/copy/print + internal use). It does NOT itself authorise
  public redistribution or republication. See `artifact_classification.yaml`.

## Related
- `docs/legal/artifact_classification.yaml` — per-class legal status
- `evidence/g0-l1/G0-L1_summary_manifest.yaml` — metadata-only evidence
