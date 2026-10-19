# G3-L1 — ISO 20022 schema and consolidated-tape access terms snapshot

Snapshot date: 2026-10-17. Authority: `docs/gates/G3.md#G3-L` (freeze tag
`g3-freeze-v0`).

Scope: the legal status of (a) the ISO 20022 consolidated-tape message
definitions and (b) the consolidated tape's input/output data and access
conditions, insofar as they constrain what this repository may store and
publish. This file paraphrases the sources with citations and dates. It
contains **no verbatim terms text, no provider data and no schema bytes**, and
it grants no rights. The machine-readable statuses live in
`docs/legal/g3_artifact_classification.yaml`; the receipt lives in
`evidence/g3-l1/`.

## Why this exists

G3 pins an ISO 20022 schema snapshot and projects observed trades onto CTP
messages. Both actions touch third-party artifacts: ISO 20022's published
schemas, and the consolidated tape's data product. The gate requires the
artifact classes to carry an explicit `PASS | FAIL | UNRESOLVED` status before
anything is vendored or published. `UNRESOLVED` is an acceptable recorded
outcome; it is not a licence and it does not resolve G0-L's open cells.

## Artifact identity

- ESMA's consolidated-tape submission is listed on the ISO 20022 submissions
  register as submission **266** ("Consolidated Tape Provider Data Reporting"),
  status **"Registered and published"**, status date **12 June 2026**. ESMA
  states the message documentation is published on the ISO 20022 site and that
  these messages serve **both** the bonds tape and the shares/ETFs tape.
  - ISO 20022 submissions register (RA Id 266):
    https://www.iso20022.org/development/status-iso-20022-submissions
  - ESMA, Consolidated Tape Providers:
    https://www.esma.europa.eu/esmas-activities/markets-and-infrastructure/consolidated-tape-providers
    (accessed 2026-10-17)
- The specific message-set and submission download pages cited by ESMA
  (`iso20022.org/message-set/1253`, `iso20022.org/submission-status/2838`)
  returned **HTTP 403** to automated retrieval on 2026-10-17, so the exact
  message-set title, version and "last updated" stamp could not be read
  directly. This is recorded as an unresolved item, not assumed.

## ISO 20022 artifact terms (paraphrased)

- The ISO 20022 **Intellectual Property Rights** page states that contributing
  organisations retain their IPR, warrant they hold sufficient rights for the
  Registration Authority to publish, and grant third parties a non-exclusive,
  **royalty-free licence to use** the published information so it can be used
  widely and uniformly.
  - https://www.iso20022.org/intellectual-property-rights (accessed 2026-10-17)
- The ISO 20022 **Terms of use** state the site material is intended to be used
  and **reproduced freely** under the ISO 20022 IPR Policy, subject to a
  condition on replication: any replication must state that it is **not** the
  official site and that the **sole source of up-to-date materials** is
  `https://www.iso20022.org/`. The material is provided "as is", without
  warranty.
  - https://www.iso20022.org/terms-use (accessed 2026-10-17)
- Governance sits with the **Registration Management Group** (RMG), the highest
  ISO 20022 registration body reporting to ISO/TC 68/SC 9. The RMG-approved
  Registration Procedures (v23, March 2026) contain an "Intellectual Property
  Rights" section.
  - https://www.iso20022.org/registration-management-group (accessed 2026-10-17)
  - https://www.iso20022.org/sites/default/files/media/file/ISO20022RegistrationProcedures.pdf
    (document dated March 2026)
- **Distinction that must not be collapsed:** the *Repository artifacts*
  (message definitions, Message Definition Reports, schemas) are the freely
  reproducible material, whereas the **ISO 20022 standard parts** (ISO 20022-1
  … -8) are separately sold, copyrighted ISO publications. "The standards are
  free" is not a fact.
- **Conflict on record:** ISO's site-wide copyright notice states that
  reproduction requires written permission and restricts use of ISO content for
  machine-learning / artificial-intelligence purposes (e.g. model training),
  while the ISO 20022 property says its material may be reproduced freely. It is
  **not established** whether the site-wide restriction is intended to reach the
  ISO 20022 Repository artifacts.
  - https://www.iso.org/copyright.html

Reading: on the ISO 20022-specific terms, committing schema files with an
attribution statement and no implied warranty *appears* permitted. The
site-wide ISO copyright, the absence of an express redistribution clause (the
grant is to "use"), and the unverified artifact pages are, however, real and
unclosed. The status is therefore **UNRESOLVED**.

## EU consolidated tape terms (paraphrased)

The act referred to in this repository as `mifir-rts-2025-3102` is **Commission
Delegated Regulation (EU) 2025/1155** (Commission reference C/2025/3102, adopted
12 June 2025, published in OJ L 2025/1155 on **3 November 2025**). The canonical
citation is the OJ number, not the Commission file name. Its title attaches "for
shares and ETFs" to the revenue-redistribution chapter; the act nonetheless
governs the **bonds** tape as well (input data, output data, latency, and the
post-trade core market data table for bonds in Annex II).

- Input data: contributors must transmit input data in a format that **adheres
  to the ISO 20022 methodology**; adherence to a single physical syntax is not
  required where well-established market practice exists, provided a mapping to
  the ISO 20022 conceptual/logical model exists.
- Output data: the CTP must disseminate output data through a **graphical user
  interface** and simultaneously in at least **two formats, CSV and a format
  adhering to the ISO 20022 methodology**; it must publish and give advance
  notice of its access/format instructions.
- Latency: input-data transmission for bonds is bounded in the sub-second range.
- Source: https://eur-lex.europa.eu/eli/reg_del/2025/1155/oj/eng (OJ 3.11.2025)

Access and reuse:

- Access to the consolidated tape must be **free of charge** to retail
  investors, academics, civil society organisations and competent authorities;
  the CTP otherwise collects fees from users. (MiFIR Art. 27h.)
- Dissemination must be on **non-discriminatory** terms, as close to real time
  as technically possible, and machine-readable; CTP data must be made available
  on a **reasonable commercial basis with fair and unbiased contractual terms**
  (MiFIR Arts. 13(1), 27h). The accompanying access/RCB act — **Commission
  Delegated Regulation (EU) 2025/1156** (C/2025/3103, OJ 3.11.2025) — specifies
  identical fee schedules and terms for all clients, factual client categories
  for fee differentials, per-client fees to avoid multiple billing, no
  bundling, and notice/withdrawal rights on unilateral changes.
  - https://eur-lex.europa.eu/eli/reg_del/2025/1156/oj/eng
- **Redistribution is not addressed by a statutory prohibition or grant.**
  Redistribution is instead contemplated as a commercial licensing activity:
  the access act recognises "data redistributors" as a client category and
  provides per-client fee rules for data obtained through multiple providers.
  Republishing CTP output would therefore normally require a market-data
  agreement and applicable fees, unless the user is within a statutory
  free-of-charge category. The definitive terms sit in the CTP's **market data
  policy**, which for the bonds tape is not yet published.

## Bonds CTP status

- **Selection ≠ authorisation.** ESMA **selected** the bonds-tape applicant
  (Ediphy / fairCT) on 3 July 2025 and invited it to apply for authorisation;
  ESMA's CTP page still describes the authorisation process as ongoing. Only the
  shares/ETFs tape (EuroCTP) has been authorised. No bonds-tape authorisation
  decision has been published. Any assumption that the bonds tape is live or
  authorised is unsupported.
  - https://www.esma.europa.eu/press-news/esma-news/esma-selects-ediphy-fairct-become-first-consolidated-tape-provider-bonds
    (03/07/2025)

## Consequences for this repository

1. `SCHEMA_ARTIFACT_VENDORING` is **UNRESOLVED**, so — per the gate's fail-closed
   rule — the repository holds only **digests, provenance and the public
   location**, and schema files remain under `data/` (gitignored). No schema
   bytes are committed.
2. `CTP_OUTPUT_REDISTRIBUTION` is **UNRESOLVED**: there is no statutory right to
   republish, the bonds tape's market data policy does not yet exist, and the
   free-of-charge categories apply to access, not to republication. CTP output
   stays `NOT_CONSUMED`.
3. `DERIVED_MESSAGE_PUBLICATION` is **FAIL** as-is: a published projected
   instance would carry provider-derived economic values, which G0-L already
   classifies as prohibited for the observed BME source. This holds regardless
   of the schema question, and it is why the projection never emits economic
   values.

## Deliberate limits

- This is a snapshot dated 2026-10-17. It records statuses and bases, and may
  change; it is not legal advice and conveys no rights.
- ISO 20022's site-wide AI/ML restriction is noted but **not resolved** here; it
  is a limitation to track, not a determination.
- No G0-L status is changed: `docs/legal/artifact_classification.yaml` remains
  as frozen for G0-L.

## Sources

| Source | URL | Date | Authority |
| --- | --- | --- | --- |
| ISO 20022 IPR policy | https://www.iso20022.org/intellectual-property-rights | accessed 2026-10-17 | ISO 20022 RA/RMG |
| ISO 20022 terms of use | https://www.iso20022.org/terms-use | accessed 2026-10-17 | ISO 20022 RA |
| ISO 20022 submissions register (RA Id 266) | https://www.iso20022.org/development/status-iso-20022-submissions | status 12 Jun 2026 | ISO 20022 RA |
| ISO 20022 registration procedures | https://www.iso20022.org/sites/default/files/media/file/ISO20022RegistrationProcedures.pdf | March 2026 | ISO 20022 RMG |
| ISO site-wide copyright | https://www.iso.org/copyright.html | accessed 2026-10-17 | ISO |
| Del. Reg. (EU) 2025/1155 (input/output, bonds) | https://eur-lex.europa.eu/eli/reg_del/2025/1155/oj/eng | OJ 3 Nov 2025 | EU (Commission) |
| Del. Reg. (EU) 2025/1156 (access / reasonable commercial basis) | https://eur-lex.europa.eu/eli/reg_del/2025/1156/oj/eng | OJ 3 Nov 2025 | EU (Commission) |
| ESMA CTP page | https://www.esma.europa.eu/esmas-activities/markets-and-infrastructure/consolidated-tape-providers | accessed 2026-10-17 | ESMA |
| ESMA bonds-CTP selection | https://www.esma.europa.eu/press-news/esma-news/esma-selects-ediphy-fairct-become-first-consolidated-tape-provider-bonds | 3 Jul 2025 | ESMA |
