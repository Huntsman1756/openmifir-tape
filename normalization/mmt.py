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

# BME `post_trade_deferral` field code table — BME Gate Codification Tables
# (v10.00.00), Level 4.1 "Publication Mode / Post-Trade Deferral: Reason",
# efficient-mode encoding. ''/' ' = immediate publication (no deferral);
# 6/7/8 = LRGS/ILQD/SIZE; A-F = MLF1/MIF2/LLF3/LIF4/VLF5/VIF5; G = DEFF.
#
# The same field historically carried Level 4.2 type mnemonics, so they are
# classified too: LMTF/DATF/VOLO/VOLW/FWAF/IDAF/COAF are non-full-detail
# (partial) publication types; FULF/FULA/FULV/FULJ are full-detail deferred
# publications. Any other non-empty value is UNKNOWN and must fail closed
# (deferral undetermined -> UNRESOLVED), never bool(non_empty).
BME_DEFERRAL_CODE_CLASS = {
    "": "NONE",
    " ": "NONE",
    "6": "REASON",
    "7": "REASON",
    "8": "REASON",
    "A": "REASON",
    "B": "REASON",
    "C": "REASON",
    "D": "REASON",
    "E": "REASON",
    "F": "REASON",
    "G": "REASON",
    "LRGS": "REASON",
    "ILQD": "REASON",
    "SIZE": "REASON",
    "MLF1": "REASON",
    "MIF2": "REASON",
    "LLF3": "REASON",
    "LIF4": "REASON",
    "VLF5": "REASON",
    "VIF5": "REASON",
    "DEFF": "REASON",
    "LMTF": "PARTIAL",
    "DATF": "PARTIAL",
    "VOLO": "PARTIAL",
    "VOLW": "PARTIAL",
    "FWAF": "PARTIAL",
    "IDAF": "PARTIAL",
    "COAF": "PARTIAL",
    "FULF": "FULL",
    "FULA": "FULL",
    "FULV": "FULL",
    "FULJ": "FULL",
}

# BME dedicated boolean indicators for the classic RTS 2 deferral reasons.
BME_DEFERRAL_BOOL_FIELDS = ("lrgs", "ilqd", "size")
