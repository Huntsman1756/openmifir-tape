"""G1-C4 — TAPE_MANIFEST + offline verify + reproducibility gate.

Frozen contract: ``docs/gates/G1.md`` §4.4/§4.5 (g1-freeze-v0) +
issue #32. Every tape build emits one small metadata-only
``TAPE_MANIFEST.yaml`` (co-located in the tape directory) declaring:

- input authority: the sealed A3 bundle identity (sha256 of its
  SHA256SUMS.txt checksum manifest) and the canonical
  ``eligible_manifest_digest`` — a {name, sha256, bytes} set digest over
  the window-eligible run manifests, computed with the same
  ``canonical_reference_set`` machinery as reference families;
- references: the pinned FITRS/FIRDS/GLEIF multi-file sets
  (``{name, sha256, bytes}`` entries + ``set_digest``), bound fail-closed
  to the authenticated G1-B declaration;
- generator authority: code_commit + tape_schema_version;
- outputs: row counts, disposition counts, per-table semantic digests
  and the dataset semantic digest (the same canonical functions as
  G1-C1);
- capsule_fixtures: declared capsule_sha256 for representative
  explain requests (publication / current chain / as_of chain).

Two verify layers are kept strictly separate (§4.4):

1. ``omt verify <tape>`` — tape self-verification. Recomputes schema,
   row counts, per-table semantic digests, dataset digest, disposition
   counts, capsule fixtures, and manifest internal consistency against
   the TAPE_MANIFEST declarations. Proves the local tape is the claimed
   materialization *if* the declared inputs are trusted. It NEVER claims
   full provenance — ``full_provenance`` is emitted as NOT_RUN.

2. ``omt verify <tape> --bundle <dir> --fitrs <f...> --firds <f...>
   --gleif-dir <d>`` — full provenance verification. Hashes the sealed
   bundle's original input bytes via SHA256SUMS (exact-set: every listed
   file verified, every present file listed), recomputes the eligible
   manifest set digest through the hardened selector, and recomputes the
   reference family set digests from the the supplied pinned file lists (exact set). Any
   mutated, missing or unexpected byte fails closed. No provider payload
   is parsed, emitted, or exposed — hashing bytes only.

§4.5 reproducibility: ``replay_materialization`` rebuilds a tape from a
sealed bundle + reference snapshots and ``reproducibility_check``
compares two independent materializations: per-table and dataset
semantic digests MUST be identical and representative capsules
byte-identical. Parquet physical byte-equality is NOT required (writer
metadata may vary).

Capsule fixtures embed the generator code_commit (C3 capsule design):
their capsule_sha256 is only reproducible under the manifest's declared
generator commit. ``verify`` therefore fails closed when the running
commit differs from ``generator.code_commit`` — generator authority
mismatch. Verify at the manifest's declared commit.

Fail-closed on: missing/malformed/wrong-schema manifest, row-count or
digest mismatch, dataset digest mismatch, capsule fixture mismatch,
generator/schema authority mismatch, mutated input/reference bytes,
missing or unexpected pinned artifact, any structural inconsistency.

Metadata-only: no provider payload bytes are ever parsed or emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from universe._paths import repo_file
from universe.reference import (
    canonical_reference_set,
    reference_entry,
    reference_set_from_files,
)

from .evidence import _git_head
from .g1b import reference_basis_id
from .g1c1 import (
    CHAIN_COLUMNS,
    CHAIN_PARQUET,
    EXPECTED_G1B_BASIS_ID,
    EXPECTED_G1B_REPORT_SHA256,
    PUB_COLUMNS,
    PUB_PARQUET,
    TAPE_SCHEMA_VERSION,
    canonical_table_bytes,
    connect_tape,
    dataset_semantic_digest,
    pub_sort_key,
)
from .g1c3 import (
    G1B_LINKS_LOGICAL_ID,
    G1B_REPORT_LOGICAL_ID,
    G1B_ROWS_LOGICAL_ID,
    capsule_bytes,
    capsule_sha256,
    explain_chain,
    explain_publication,
)
from .selection import select_eligible

MANIFEST_NAME = "TAPE_MANIFEST.yaml"
MANIFEST_SCHEMA_VERSION = 1
BUNDLE_SUMS_FILE = "SHA256SUMS.txt"

G1A_LOGICAL_ID = "evidence/g1-a1/G1-A1_observed_instrument_set.yaml"
C2_REPORT_LOGICAL_ID = "evidence/g1-c2/G1-C2_asof_reconstruction.yaml"


class G1C4Error(Exception):
    """Fail-closed abort for G1-C4 (manifest/digest/authority/verification
    failure)."""


def _sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _parse_ts(text: str) -> Any:
    from datetime import datetime
    dt = datetime.fromisoformat(str(text).strip())
    if dt.tzinfo is None:
        raise G1C4Error(f"window bound {text!r} is naive — UTC required")
    return dt


# ------------------------------------------------------------ tape read --

def _read_table(tape_dir: Path, fname: str, view: str,
                columns: list[str]) -> list[dict[str, Any]]:
    """Read a tape table back as canonical dict rows (declared column
    order). Types round-trip: BIGINT -> int, VARCHAR[] -> list."""
    con = connect_tape(tape_dir)
    try:
        # SELECT built from fixed schema-literal column/view names —
        # never user input; operator-local paths only inside connect_tape.
        colspec = ", ".join(f'"{c}"' for c in columns)
        rows = con.execute(
            f"SELECT {colspec} FROM {view}"  # noqa: S608
        ).fetchall()
        return [dict(zip(columns, r, strict=True)) for r in rows]
    finally:
        con.close()


def _table_digest(rows: list[dict[str, Any]], columns: list[str],
                  sort_key) -> str:
    return _sha(canonical_table_bytes(rows, columns, sort_key))


def tape_outputs(tape_dir: Path) -> dict[str, Any]:
    """Recompute all output-level declarations from the physical tape."""
    tape_dir = Path(tape_dir)
    pub = tape_dir / PUB_PARQUET
    chn = tape_dir / CHAIN_PARQUET
    for p in (pub, chn):
        if not p.is_file():
            raise G1C4Error(f"tape table missing: {p.name}")
    pubs = _read_table(tape_dir, PUB_PARQUET, "publications", PUB_COLUMNS)
    chains = _read_table(tape_dir, CHAIN_PARQUET, "trade_chains",
                         CHAIN_COLUMNS)
    pub_digest = _table_digest(pubs, PUB_COLUMNS, pub_sort_key)
    chain_digest = _table_digest(chains, CHAIN_COLUMNS,
                                 lambda r: r["chain_id"])
    dataset = dataset_semantic_digest(pub_digest, len(pubs),
                                      chain_digest, len(chains))
    counts = Counter(r["universe_disposition_reason"] for r in pubs)
    return {
        "publications_rows": len(pubs),
        "trade_chains_rows": len(chains),
        "disposition_counts": dict(sorted(counts.items())),
        "publications_semantic_digest": pub_digest,
        "chains_semantic_digest": chain_digest,
        "dataset_semantic_digest": dataset,
    }


def _disposition_counts_ok(manifest_counts: dict,
                           actual_counts: dict) -> None:
    if manifest_counts != actual_counts:
        raise G1C4Error(
            f"disposition_counts mismatch: declared {manifest_counts!r} "
            f"!= recomputed {actual_counts!r}")


# --------------------------------------------------------- input layer --

def eligible_manifest_set(runs_dir: Path, raw_root: Path,
                          window: dict[str, str]) -> dict[str, Any]:
    """Canonical {name, sha256, bytes} set over the window-eligible A3
    run manifests, resolved through the hardened selector — the declared
    input corpus, independent of how the files are physically laid out.
    """
    sel = select_eligible(
        Path(runs_dir), Path(raw_root),
        start=_parse_ts(window["operational_start"]),
        tail_end=_parse_ts(window["evidence_tail_end"]))
    names = sel["eligible_manifests"]
    missing = [n for n in names if not (Path(runs_dir) / n).is_file()]
    if missing:
        raise G1C4Error(
            f"eligible manifest files missing from runs dir: {missing}")
    return canonical_reference_set(
        reference_entry(n, (Path(runs_dir) / n).read_bytes())
        for n in names)


def bundle_identity(bundle_dir: Path) -> str:
    """sha256 of the sealed bundle's checksum manifest — the declared
    ``a3_bundle_sha256``. The sums file is the bundle's own canonical
    content index; hashing it commits to every listed byte."""
    sums = Path(bundle_dir) / BUNDLE_SUMS_FILE
    if not sums.is_file():
        raise G1C4Error(f"sealed bundle lacks {BUNDLE_SUMS_FILE}: "
                        f"{bundle_dir}")
    return _sha(sums.read_bytes())


def _parse_sums(sums_path: Path) -> dict[str, str]:
    """Parse GNU-style ``<sha256>  <relpath>`` checksum lines."""
    out: dict[str, str] = {}
    for i, line in enumerate(
            sums_path.read_bytes().decode("utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise G1C4Error(
                f"{sums_path.name}:{i + 1}: malformed checksum line")
        rel = parts[1].lstrip("*").strip()
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise G1C4Error(
                f"{sums_path.name}:{i + 1}: unsafe path {rel!r}")
        if rel in out:
            raise G1C4Error(
                f"{sums_path.name}:{i + 1}: duplicate entry {rel!r}")
        out[rel] = parts[0].lower()
    if not out:
        raise G1C4Error(f"{sums_path.name}: empty checksum manifest")
    return out


def verify_bundle(bundle_dir: Path, expected_sums_sha256: str) -> dict:
    """Full-provenance check over the sealed A3 bundle.

    1. sha256(SHA256SUMS.txt) == declared a3_bundle_sha256 — authenticates
       the checksum manifest itself.
    2. Every listed file exists and its bytes hash to the listed sha256.
    3. Exact set: every file under the bundle (except the sums file)
       is listed — unexpected extra bytes fail closed.
    """
    bundle_dir = Path(bundle_dir)
    sums = bundle_dir / BUNDLE_SUMS_FILE
    if not sums.is_file():
        raise G1C4Error(f"bundle {BUNDLE_SUMS_FILE} missing: {bundle_dir}")
    actual = _sha(sums.read_bytes())
    if actual != expected_sums_sha256:
        raise G1C4Error(
            f"bundle {BUNDLE_SUMS_FILE} sha256 {actual!r} != declared "
            f"{expected_sums_sha256!r} — bundle identity mismatch")
    declared = _parse_sums(sums)
    for rel, want in declared.items():
        f = bundle_dir / rel
        if not f.is_file():
            raise G1C4Error(f"bundle file missing: {rel}")
        got = _sha(f.read_bytes())
        if got != want:
            raise G1C4Error(
                f"bundle file {rel} sha256 {got!r} != listed {want!r} — "
                "input bytes mutated")
    present = {str(p.relative_to(bundle_dir)).replace("\\", "/")
               for p in bundle_dir.rglob("*") if p.is_file()}
    present.discard(BUNDLE_SUMS_FILE)
    extra = sorted(present - set(declared))
    if extra:
        raise G1C4Error(
            f"bundle contains {len(extra)} unlisted file(s) (e.g. "
            f"{extra[0]!r}) — sealed set is not exact")
    return {"files_verified": len(declared),
            "a3_bundle_sha256": actual}


# ------------------------------------------------------ reference layer --

def _verify_reference_files(paths: list[Path], declared: dict,
                            family: str) -> dict:
    """Exact-set verification over the supplied pinned files: the
    canonical set MUST equal the declared {entries, set_digest} — a
    missing, mutated or extra artifact diverges the digest."""
    actual = reference_set_from_files(paths)
    if actual["set_digest"] != declared["set_digest"]:
        missing = sorted({e["name"] for e in declared["entries"]}
                         - {e["name"] for e in actual["entries"]})
        extra = sorted({e["name"] for e in actual["entries"]}
                       - {e["name"] for e in declared["entries"]})
        raise G1C4Error(
            f"{family} set_digest mismatch (missing={missing}, "
            f"extra={extra}) — reference snapshot is not the pinned set")
    for want, got in zip(declared["entries"], actual["entries"],
                         strict=True):
        if want != got:
            raise G1C4Error(
                f"{family} entry {want['name']!r} diverged "
                "(sha256/bytes) — mutated reference artifact")
    return {"set_digest": actual["set_digest"],
            "entries_verified": len(actual["entries"])}


def _verify_gleif_subset(directory: Path, declared: dict) -> dict:
    """GLEIF pinned set is the required-LEI subset of the snapshot dir
    (unrelated residue can never enter the basis — same semantics as
    ``_gleif_set_for_required``). Every declared entry MUST resolve to a
    file with matching sha256+bytes; the declared set_digest must equal
    the canonical digest over the declared entries."""
    directory = Path(directory)
    recomputed = []
    for want in declared["entries"]:
        f = directory / want["name"]
        if not f.is_file():
            raise G1C4Error(
                f"declared GLEIF entry missing from snapshot dir: "
                f"{want['name']!r}")
        got = reference_entry(f.name, f.read_bytes())
        if got != want:
            raise G1C4Error(
                f"GLEIF entry {want['name']!r} sha256/bytes diverged — "
                "mutated reference artifact")
        recomputed.append(got)
    if canonical_reference_set(recomputed)["set_digest"] \
            != declared["set_digest"]:
        raise G1C4Error(
            "gleif set_digest mismatch — declared entries do not "
            "recompute to the pinned set")
    return {"set_digest": declared["set_digest"],
            "entries_verified": len(declared["entries"])}


def _gleif_declared_set(g1b_report_path: Path, gleif_dir: Path,
                        expected_g1b_report_sha256: str) -> dict:
    """The GLEIF pinned set comes from the authenticated G1-B report —
    it is required-LEI-driven. Report bytes are sha256-verified before
    the declared set is trusted; every declared entry is then bound to
    an existing file with matching sha256+bytes in the supplied
    snapshot dir."""
    report_path = Path(g1b_report_path)
    if not report_path.is_file():
        raise G1C4Error(f"G1-B report not found: {report_path}")
    data = report_path.read_bytes()
    if _sha(data) != expected_g1b_report_sha256:
        raise G1C4Error(
            "G1-B report sha256 mismatch — refusing to trust the "
            "declared GLEIF pinned set from an unauthenticated report")
    doc = yaml.safe_load(data.decode("utf-8"))
    declared = (doc.get("reference_sets") or {}).get("gleif") or {}
    if not declared.get("entries") or not declared.get("set_digest"):
        raise G1C4Error(
            "G1-B report declares no GLEIF pinned set — cannot emit "
            "manifest without the reference authority")
    directory = Path(gleif_dir)
    for want in declared["entries"]:
        f = directory / want["name"]
        if not f.is_file():
            raise G1C4Error(
                f"declared GLEIF entry missing from snapshot dir: "
                f"{want['name']!r}")
        got = reference_entry(f.name, f.read_bytes())
        if got != want:
            raise G1C4Error(
                f"GLEIF entry {want['name']!r} diverges from the "
                "authenticated G1-B declaration")
    return declared


# ------------------------------------------------------------- manifest --

def build_tape_manifest(
    tape_dir: Path,
    *,
    runs_dir: Path,
    raw_root: Path,
    bundle_dir: Path,
    fitrs_paths: list[Path],
    firds_paths: list[Path],
    gleif_dir: Path,
    g1b_report_path: Path,
    window: dict[str, str],
    capsule_subjects: list[dict[str, Any]] | None = None,
    code_commit: str | None = None,
    expected_g1b_report_sha256: str,
    expected_g1b_basis_id: str,
    explain_kw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit the TAPE_MANIFEST for a materialized tape.

    Every declaration is recomputed fail-closed: tape digests come from
    the physical tables, the eligible manifest set through the hardened
    selector, reference set digests from the supplied snapshot bytes and
    the GLEIF pinned set from the authenticated G1-B report. Reference
    set_digests MUST equal the G1-B declared basis — a tape claiming a
    different reference authority is a contradiction, not a choice.
    ``capsule_subjects`` declares representative explain requests whose
    capsule_sha256 is recorded for fixture verification.
    """
    tape_dir = Path(tape_dir)
    outputs = tape_outputs(tape_dir)
    fitrs_set = reference_set_from_files(fitrs_paths)
    firds_set = reference_set_from_files(firds_paths)
    gleif_set = _gleif_declared_set(
        Path(g1b_report_path), Path(gleif_dir),
        expected_g1b_report_sha256)
    basis = reference_basis_id({
        "fitrs": fitrs_set["set_digest"],
        "firds": firds_set["set_digest"],
        "gleif": gleif_set["set_digest"]})
    if basis != expected_g1b_basis_id:
        raise G1C4Error(
            f"recomputed reference_basis_id {basis!r} != authenticated "
            f"G1-B basis {expected_g1b_basis_id!r} — reference authority "
            "mismatch")

    eligible = eligible_manifest_set(Path(runs_dir), Path(raw_root),
                                     window)
    bundle_sha = bundle_identity(Path(bundle_dir))

    fixtures: list[dict[str, Any]] = []
    for subj in capsule_subjects or []:
        kw = explain_kw or {}
        if subj["type"] == "publication":
            cap = explain_publication(tape_dir, subj["publication_id"],
                                      **kw)
            subject = {"type": "publication",
                       "publication_id": subj["publication_id"]}
        else:
            cap = explain_chain(tape_dir, subj["chain_id"],
                                at=subj.get("at"), **kw)
            subject = {"type": "chain", "chain_id": subj["chain_id"],
                       "at": subj.get("at")}
        fixtures.append({"subject": subject,
                         "capsule_sha256": capsule_sha256(cap)})

    return {
        "tape_manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "input": {
            "a3_bundle_sha256": bundle_sha,
            "a3_bundle_sums_file": BUNDLE_SUMS_FILE,
            "eligible_manifest_digest": eligible["set_digest"],
            "eligible_manifests": len(eligible["entries"]),
            "capture_window_utc": {
                "operational_start": window["operational_start"],
                "evidence_tail_end": window["evidence_tail_end"],
            },
        },
        "references": {
            "fitrs": fitrs_set,
            "firds": firds_set,
            "gleif": gleif_set,
        },
        "generator": {
            "module": "metrics.g1c4 (materialization: metrics.g1c1)",
            "code_commit": code_commit or _git_head(),
            "tape_schema_version": TAPE_SCHEMA_VERSION,
        },
        "outputs": outputs,
        "capsule_fixtures": fixtures,
    }


def write_manifest(tape_dir: Path, manifest: dict[str, Any]) -> Path:
    path = Path(tape_dir) / MANIFEST_NAME
    path.write_bytes(yaml.safe_dump(
        manifest, sort_keys=False, allow_unicode=True).encode("utf-8"))
    return path


def load_manifest(tape_dir: Path) -> dict[str, Any]:
    path = Path(tape_dir) / MANIFEST_NAME
    if not path.is_file():
        raise G1C4Error(f"{MANIFEST_NAME} missing in {tape_dir} — "
                        "the tape cannot be verified without its "
                        "declared manifest")
    try:
        doc = yaml.safe_load(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise G1C4Error(
            f"{MANIFEST_NAME} unreadable/malformed: {exc}") from exc
    if not isinstance(doc, dict):
        raise G1C4Error(f"{MANIFEST_NAME} is not a mapping")
    if doc.get("tape_manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise G1C4Error(
            f"{MANIFEST_NAME} schema version "
            f"{doc.get('tape_manifest_schema_version')!r} != "
            f"{MANIFEST_SCHEMA_VERSION}")
    for section in ("input", "references", "generator", "outputs",
                    "capsule_fixtures"):
        if not isinstance(doc.get(section), (dict, list)):
            raise G1C4Error(
                f"{MANIFEST_NAME}: missing/invalid section {section!r}")
    return doc


# -------------------------------------------------------------- verify --

def _explain_subject(tape_dir: Path, subject: dict[str, Any],
                     explain_kw: dict[str, Any]) -> dict[str, Any]:
    if subject.get("type") == "publication":
        return explain_publication(
            tape_dir, subject["publication_id"], **explain_kw)
    if subject.get("type") == "chain":
        return explain_chain(tape_dir, subject["chain_id"],
                             at=subject.get("at"), **explain_kw)
    raise G1C4Error(f"capsule_fixture subject type unsupported: "
                    f"{subject!r}")


def verify_self(
    tape_dir: Path,
    *,
    code_commit: str | None = None,
    explain_kw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tape self-verification (§4.4 layer A).

    Recomputes every manifest declaration from the physical tape and the
    explain API; verifies internal consistency. Does NOT prove input
    bytes — that is the --bundle layer. ``full_provenance`` is always
    NOT_RUN here by construction.
    """
    tape_dir = Path(tape_dir)
    manifest = load_manifest(tape_dir)
    checks: list[dict[str, Any]] = []

    def ok(name: str) -> None:
        checks.append({"check": name, "result": "PASS"})

    gen = manifest["generator"]
    head = code_commit if code_commit is not None else _git_head()
    if gen.get("code_commit") != head:
        raise G1C4Error(
            f"generator.code_commit {gen.get('code_commit')!r} != running "
            f"commit {head!r} — generator authority mismatch; verify at "
            "the manifest's declared commit")
    if gen.get("tape_schema_version") != TAPE_SCHEMA_VERSION:
        raise G1C4Error(
            f"generator.tape_schema_version "
            f"{gen.get('tape_schema_version')!r} != {TAPE_SCHEMA_VERSION}")
    ok("generator_authority")

    actual = tape_outputs(tape_dir)
    declared = manifest["outputs"]
    for key in ("publications_rows", "trade_chains_rows",
                "publications_semantic_digest", "chains_semantic_digest",
                "dataset_semantic_digest"):
        if declared.get(key) != actual[key]:
            raise G1C4Error(
                f"outputs.{key} declared {declared.get(key)!r} != "
                f"recomputed {actual[key]!r} — tape content mismatch")
    ok("table_row_counts")
    ok("publications_semantic_digest")
    ok("chains_semantic_digest")
    ok("dataset_semantic_digest")
    _disposition_counts_ok(declared.get("disposition_counts") or {},
                           actual["disposition_counts"])
    ok("disposition_counts")

    # Capsule fixtures: recompute each declared subject — capsule_sha256
    # must match byte-for-byte (determinism is part of the tape claim).
    for fx in manifest["capsule_fixtures"]:
        cap = _explain_subject(tape_dir, fx["subject"],
                               explain_kw or {})
        got = capsule_sha256(cap)
        if got != fx["capsule_sha256"]:
            raise G1C4Error(
                f"capsule fixture {fx['subject']!r}: capsule_sha256 "
                f"{got!r} != declared {fx['capsule_sha256']!r} — "
                "capsule evidence tampered or non-deterministic")
        if got != cap["capsule_sha256"]:
            raise G1C4Error("capsule internal digest inconsistency")
    ok("capsule_fixtures")

    return {
        "tape": str(tape_dir),
        "tape_manifest_sha256": _sha(
            (tape_dir / MANIFEST_NAME).read_bytes()),
        "self_verification": "PASS",
        "full_provenance": "NOT_RUN — supply --bundle and reference "
                           "snapshot dirs to authenticate input bytes",
        "checks": checks,
    }


def verify_full(
    tape_dir: Path,
    *,
    bundle_dir: Path,
    fitrs_paths: list[Path],
    firds_paths: list[Path],
    gleif_dir: Path,
    code_commit: str | None = None,
    explain_kw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full provenance verification (§4.4 layer B): self-verification
    plus byte-level authentication of every declared input."""
    result = verify_self(tape_dir, code_commit=code_commit,
                         explain_kw=explain_kw)
    manifest = load_manifest(tape_dir)
    declared = manifest["input"]
    refs = manifest["references"]

    bundle = verify_bundle(
        Path(bundle_dir), declared["a3_bundle_sha256"])
    result["checks"].append(
        {"check": "a3_bundle_bytes", "result": "PASS",
         "files_verified": bundle["files_verified"]})

    eligible = eligible_manifest_set(
        Path(bundle_dir) / "runs", Path(bundle_dir) / "raw",
        declared["capture_window_utc"])
    if eligible["set_digest"] != declared["eligible_manifest_digest"]:
        raise G1C4Error(
            "eligible_manifest_digest mismatch — the bundle's eligible "
            "manifest set does not recompute to the declared input "
            "corpus")
    result["checks"].append(
        {"check": "eligible_manifest_set", "result": "PASS",
         "entries": len(eligible["entries"])})

    for family, paths in (("fitrs", fitrs_paths), ("firds", firds_paths)):
        r = _verify_reference_files(paths, refs[family], family)
        result["checks"].append(
            {"check": f"{family}_reference_set", "result": "PASS",
             **r})
    g = _verify_gleif_subset(Path(gleif_dir), refs["gleif"])
    result["checks"].append(
        {"check": "gleif_reference_set", "result": "PASS",
         "semantics": "pinned required-LEI subset (unrelated dir residue "
                      "can never enter the basis)", **g})

    result["full_provenance"] = "PASS"
    return result


# ----------------------------------------------------- reproducibility --

def replay_materialization(
    runs_dir: Path,
    raw_root: Path,
    *,
    g1a_artifact: Path,
    g1b_rows: Path,
    g1b_report: Path,
    window: dict[str, str],
    tape_dir: Path,
    date: str,
    generated_utc: str,
    **c1_expected: Any,
) -> dict[str, Any]:
    """Independent §4.5 materialization replay: full G1-C1 build into a
    fresh root. Returns the computed output digests (metadata only)."""
    from .g1c1 import build_report as _c1_build  # local: heavy import
    report, _pub_bytes, _chain_bytes = _c1_build(
        Path(raw_root), Path(runs_dir), Path(g1a_artifact),
        Path(g1b_rows), Path(g1b_report), window=window,
        tape_dir=Path(tape_dir), date=date, generated_utc=generated_utc,
        **c1_expected)
    return {
        "tape_dir": str(tape_dir),
        "publications_rows":
            report["tables"]["publications"]["row_count"],
        "trade_chains_rows":
            report["tables"]["trade_chains"]["row_count"],
        "publications_semantic_digest":
            report["tables"]["publications"]["semantic_digest"],
        "chains_semantic_digest":
            report["tables"]["trade_chains"]["semantic_digest"],
        "dataset_semantic_digest": report["dataset_semantic_digest"],
    }


def reproducibility_check(
    runs_dir: Path,
    raw_root: Path,
    *,
    g1a_artifact: Path,
    g1b_rows: Path,
    g1b_report: Path,
    window: dict[str, str],
    tape_roots: list[Path],
    capsule_subjects: list[dict[str, Any]],
    date: str,
    generated_utc: str,
    explain_kw: dict[str, Any] | None = None,
    c1_expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§4.5 gate: N independent materializations from identical sealed
    inputs MUST produce identical semantic digests and byte-identical
    representative capsules. Parquet byte-identity is NOT required."""
    if len(tape_roots) < 2:
        raise G1C4Error(
            "reproducibility requires at least two independent roots")
    runs: list[dict[str, Any]] = []
    for root in tape_roots:
        runs.append(replay_materialization(
            runs_dir, raw_root, g1a_artifact=g1a_artifact,
            g1b_rows=g1b_rows, g1b_report=g1b_report, window=window,
            tape_dir=root, date=date, generated_utc=generated_utc,
            **(c1_expected or {})))
    ref = runs[0]
    for r in runs[1:]:
        for key in ("publications_rows", "trade_chains_rows",
                    "publications_semantic_digest",
                    "chains_semantic_digest", "dataset_semantic_digest"):
            if r[key] != ref[key]:
                raise G1C4Error(
                    f"materialization {r['tape_dir']} diverged on "
                    f"{key}: {r[key]!r} != {ref[key]!r}")

    kw = explain_kw or {}
    capsule_sets: list[list[bytes]] = []
    for root in tape_roots:
        capsule_sets.append([
            capsule_bytes(_explain_subject(root, s, kw))
            for s in capsule_subjects])
    for i, got in enumerate(capsule_sets[1:], start=1):
        if got != capsule_sets[0]:
            raise G1C4Error(
                f"capsule bytes for materialization {i} diverge from "
                "root 0 — byte-identical capsule requirement failed")

    return {
        "materializations": runs,
        "publications_semantic_digest_equal": True,
        "chains_semantic_digest_equal": True,
        "dataset_semantic_digest_equal": True,
        "capsule_bytes_byte_identical": True,
        "parquet_byte_equality_required": False,
        "tape_reproducible": True,
    }


# ----------------------------------------------------------------- CLI --

def _default_authority() -> dict[str, Path]:
    return {
        "g1a_artifact": repo_file(G1A_LOGICAL_ID),
        "g1b_rows": repo_file(G1B_ROWS_LOGICAL_ID),
        "g1b_report": repo_file(G1B_REPORT_LOGICAL_ID),
        "g1b_links": repo_file(G1B_LINKS_LOGICAL_ID),
    }


def _emit_result(result: dict[str, Any]) -> None:
    sys.stdout.write(yaml.safe_dump(result, sort_keys=False,
                                    allow_unicode=True))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="omt verify",
        description="G1-C4 — tape self-verification + full provenance + "
                    "reproducibility (metadata-only, offline, "
                    "fail-closed)")
    ap.add_argument("tape_dir", type=Path, nargs="?",
                    help="tape directory containing TAPE_MANIFEST.yaml")
    ap.add_argument("--bundle", type=Path, default=None,
                    help="sealed A3 bundle dir (full provenance layer)")
    ap.add_argument("--gleif-dir", type=Path, default=None)
    ap.add_argument("--emit-manifest", action="store_true",
                    help="emit TAPE_MANIFEST.yaml into tape_dir instead "
                         "of verifying")
    ap.add_argument("--runs-dir", type=Path, default=None)
    ap.add_argument("--raw-root", type=Path, default=None)
    ap.add_argument("--fitrs", type=Path, nargs="+", default=None,
                    help="explicit pinned FITRS snapshot files")
    ap.add_argument("--firds", type=Path, nargs="+", default=None,
                    help="explicit pinned FIRDS snapshot files")
    ap.add_argument("--window", default=None,
                    help="JSON {operational_start, evidence_tail_end}")
    ap.add_argument("--subjects", default=None,
                    help="JSON capsule_fixtures subjects list")
    args = ap.parse_args(argv)

    if args.tape_dir is None:
        ap.error("tape_dir is required")

    if args.emit_manifest:
        for name in ("runs_dir", "raw_root", "bundle", "fitrs",
                     "firds", "gleif_dir", "window"):
            if getattr(args, name) is None:
                ap.error(f"--{name.replace('_', '-')} is required for "
                         "--emit-manifest")
        auth = _default_authority()
        manifest = build_tape_manifest(
            args.tape_dir, runs_dir=args.runs_dir, raw_root=args.raw_root,
            bundle_dir=args.bundle, fitrs_paths=args.fitrs,
            firds_paths=args.firds, gleif_dir=args.gleif_dir,
            g1b_report_path=auth["g1b_report"],
            window=json.loads(args.window),
            capsule_subjects=json.loads(args.subjects or "[]"),
            expected_g1b_report_sha256=EXPECTED_G1B_REPORT_SHA256,
            expected_g1b_basis_id=EXPECTED_G1B_BASIS_ID)
        path = write_manifest(args.tape_dir, manifest)
        print(path)
        return 0

    full = all(x is not None for x in
               (args.bundle, args.fitrs, args.firds, args.gleif_dir))
    partial = any(x is not None for x in
                  (args.bundle, args.fitrs, args.firds,
                   args.gleif_dir))
    if partial and not full:
        raise G1C4Error(
            "full provenance requires ALL of --bundle --fitrs --firds "
            "--gleif-dir — partial input authentication would be a "
            "claim we cannot support")
    result = (verify_full(args.tape_dir, bundle_dir=args.bundle,
                          fitrs_paths=args.fitrs,
                          firds_paths=args.firds,
                          gleif_dir=args.gleif_dir)
              if full else verify_self(args.tape_dir))
    _emit_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
