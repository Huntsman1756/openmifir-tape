"""FIX Market Model Typology (MMT) post-trade flag sets.

MMT is an industry-standard flag taxonomy (maintained by the FIX Trading
Community), not a source-specific quirk. The two G0 sources publish MMT
mnemonics in their flag fields, so the sets live here, shared by the source
mappers.

Level 4.1 "Publication Mode / Post-Trade Deferral: Reason" — the publication
carries a non-immediate-publication (deferral) indication. Includes the
fixed-income deferral flags added by MMT for the RTS 2 deferral regime
(MLF1..VIF5, DEFF) and the classic RTS 2 deferral reasons (LRGS, ILQD, SIZE).

Level 4.2 "Post-Trade Deferral or Enrichment: Type" — only the non-full-detail
types are listed: limited details, volume omission, and aggregation types. The
full-details counterparts (FULF, FULA, FULV, FULJ) denote complete publications
and are deliberately NOT in the partial set.
"""

MMT_DEFERRAL_REASON = frozenset({
    "MLF1", "MIF2", "LLF3", "LIF4", "VLF5", "VIF5", "DEFF",
    "LRGS", "ILQD", "SIZE",
})

MMT_PARTIAL_TYPE = frozenset({
    "LMTF", "VOLO", "VOLW", "DATF", "FWAF", "IDAF", "COAF",
})

MMT_CANCEL_FLAG = "CANC"
MMT_AMEND_FLAG = "AMND"
