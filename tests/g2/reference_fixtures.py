"""Synthetic timed reference inputs, with deterministic ZIP bytes."""

import hashlib
import io
import json
import zipfile

LEI = "00000000000000000001"
ISIN = "SYNTHETIC-ISIN"
FITRS = (f"<Envelope><NonEqtyTrnsprncyData><Id><ISINAndSubClss><ISIN>{ISIN}</ISIN>"
         "<FinInstrmClssfctn>BOND</FinInstrmClssfctn><DerivSubClss><SgmttnCrit>"
         "<CritNm>SACL</CritNm><CritVal>BOND5</CritVal></SgmttnCrit></DerivSubClss>"
         "</ISINAndSubClss></Id></NonEqtyTrnsprncyData></Envelope>").encode()
FIRDS = (f"<Envelope><FinInstrm><NewRcrd><FinInstrmGnlAttrbts><Id>{ISIN}</Id>"
         f"</FinInstrmGnlAttrbts><Issr>{LEI}</Issr></NewRcrd></FinInstrm></Envelope>").encode()


def gleif(country="ES"):
    return json.dumps({"data": {"id": LEI, "attributes": {"entity": {"legalJurisdiction": country}}}}).encode()


def zipped(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in members:
            archive.writestr(zipfile.ZipInfo(name, (2026, 9, 1, 0, 0, 0)), content)
    return stream.getvalue()


def write_bundle(root, *, episode="synthetic-episode", known="2026-10-01T12:00:00Z",
                 overrides=None, failed=None, scopes=None):
    payloads = {"FITRS": zipped([("synthetic.xml", FITRS)]),
                "FIRDS": zipped([("synthetic.xml", FIRDS)]), "GLEIF": gleif()}
    payloads.update(overrides or {})
    declarations = {}
    for family, content in payloads.items():
        folder = root / family
        folder.mkdir(parents=True, exist_ok=True)

        def store(name, blob, destination=folder):
            (destination / name).write_bytes(blob)
            return {"name": name, "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}

        identifiers = (scopes or {}).get(family, [LEI if family == "GLEIF" else ISIN])
        scope = {"mode": "IDENTIFIERS" if identifiers else "ALL_FILE_RECORDS",
                 "identifier_kind": "LEI" if family == "GLEIF" else "ISIN",
                 "identifiers": identifiers}
        receipt = {"schema": "g2-acquisition-outcome-v1", "family": family,
                   "acquisition_id": episode + family, "scope": scope,
                   "source_provenance": "https://synthetic.invalid/reference",
                   "outcome": "ACQUISITION_FAILED" if family == failed else "AVAILABLE",
                   "reason": "synthetic failure" if family == failed else None}
        entry = store("synthetic.json" if family == "GLEIF" else "synthetic.zip", content)
        doc = {"schema": "g2-reference-observation-v1", **{k: receipt[k] for k in (
            "family", "acquisition_id", "scope", "source_provenance", "outcome")}, "source_revision": None,
            "files": [] if family == failed else [{"file": entry, "observed_at": known}],
            "receipt": {"file": store("outcome.json", json.dumps(receipt).encode()), "observed_at": known}}
        metadata = store("observation.json", json.dumps(doc).encode())
        declarations[family] = [{**metadata, "name": family + "/" + metadata["name"]}]
    return {"schema": "g2-reference-bundle-v1", "episode_id": episode, "observations": declarations}
