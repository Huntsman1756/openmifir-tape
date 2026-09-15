# Bloomberg APAE — Terms of Use snapshot

Target of G0-L1. Summary + URLs + fetched date, NOT verbatim provider text.
Reviewed: 2026-09-15 (snapshot). Sources inspected live 2026-09-15.

## Source
- Source name: Bloomberg APAE — Bloomberg Approved Publication Arrangement (EU
  APA) delayed trade reports
- Source id: `blb_apae` (see `config/sources/bloomberg_apae.yaml`)

## URLs fetched (evidence of inspection)
| URL | Fetched | Result |
| --- | --- | --- |
| `https://www.bloombergapa.com/` | 2026-09-15 | 200 — live page |
| `https://www.bloombergapa.com/contactus` | 2026-09-15 | 200 — live page |
| `https://www.bloombergapa.com/historyfiles` | 2026-09-15 | 200 — live page |
| `https://www.bloomberg.com/tos/` (linked ToS) | 2026-09-15 | **403 — bot-blocked; exact ToS content NOT retrieved** |

Canonical data page: `https://www.bloombergapa.com/`

## Key-terms summary
1. **Nature of the site.** Publishes delayed trade reports produced by Bloomberg
   APA. The PUBLIC DATA page lists per-interval CSV files retrievable without
   authentication.
2. **Disclaimer (page-level).** Transparency Data made available on the page are
   subject to a Terms of Use linked from the site (the "ToS"). If any provision of
   the ToS is held inconsistent with the obligations applicable under EU
   Regulation 600/2014, that provision shall not apply, but the remainder of the
   ToS remains valid and enforceable to the fullest extent permitted by law; the
   invalid provision is reformed only to the minimum extent necessary.
3. **Linked ToS location.** The page footer links the site's general Terms
   (`http://www.bloomberg.com/tos/`). The exact text could not be retrieved by the
   automated fetch (HTTP 403) as of 2026-09-15 → the **exact ToS content is
   UNRESOLVED**.
4. **No other source-specific licence granted on the site.** No additional
   grant/restriction for local archival, free delayed redistribution, or
   historical republication is exposed on the pages themselves.

## Notes
- EU Regulation 600/2014 = MiFIR. The disclaimer means MiFIR obligations (which
  require post-trade transparency data to be made public) take precedence over any
  inconsistent ToS provision. That carve-out is a defensive clause; it does not by
  itself grant a redistribution licence and cannot be applied to unknown ToS text.
- Because the linked ToS body is inaccessible, the project records the terms
  snapshot at the page level and marks the ToS content (and hence
  redistribution-class rights) UNRESOLVED. See `artifact_classification.yaml`.

## Related
- `docs/legal/artifact_classification.yaml` — per-class legal status
- `evidence/g0-l1/G0-L1_summary_manifest.yaml` — metadata-only evidence
