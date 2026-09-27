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
their declared capsule_sha256 is only byte-reproducible when verify runs
at the manifest's declared commit. ``generator.code_commit`` is a
provenance record, not a runtime constraint — verify never requires a
particular checkout; when commits differ, fixtures are verified at
determinism + internal-consistency level and the report records
``code_commit_match: false``.

Fail-closed on: missing/malformed/wrong-schema manifest, physical
schema drift (extra/missing/typed columns), row-count or digest
mismatch, dataset digest mismatch, reference-set/basis inconsistency,
capsule non-determinism or fixture mismatch at the declared commit,
malformed generator authority, mutated input/reference bytes, missing
or unexpected pinned artifact, any structural inconsistency.

Metadata-only: no provider payload bytes are ever parsed or emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import duckdb
import yaml

from universe._paths import repo_file
from universe.reference import (
    canonical_reference_set,
    reference_entry,
    reference_set_from_files,
)

from .evidence import _git_head
from .g1b import (
    G1BError,
    canonical_links_bytes,
    canonical_rows_bytes,
    reference_basis_id,
)
from .g1c1 import (
    CHAIN_COLUMNS,
    CHAIN_DUCK_TYPES,
    CHAIN_PARQUET,
    EXPECTED_G1A_SEMANTIC_DIGEST,
    EXPECTED_G1B_BASIS_ID,
    EXPECTED_G1B_REPORT_SHA256,
    EXPECTED_G1B_ROWS_DIGEST,
    PUB_COLUMNS,
    PUB_DUCK_TYPES,
    PUB_PARQUET,
    TAPE_SCHEMA_VERSION,
    canonical_table_bytes,
    connect_tape,
    dataset_semantic_digest,
    pub_sort_key,
)
from .g1c3 import (
    EXPECTED_C2_REPORT_SHA256,
    EXPECTED_G1B_LINKS_SHA256,
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
            "basis_id": basis,
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


def _table_schema_ok(tape_dir: Path, parquet: str, columns: list[str],
                     duck_types: dict[str, str]) -> None:
    """Physical schema check: the Parquet file's column NAMES, ORDER and
    TYPES must equal the declared schema exactly — an unexpected extra
    column is as much a tamper as a missing one."""
    path = tape_dir / parquet
    con = duckdb.connect()
    try:
        lit = str(path).replace("'", "''")
        got = con.execute(
            f"DESCRIBE SELECT * FROM '{lit}'"  # noqa: S608
        ).fetchall()
    finally:
        con.close()
    actual = [(r[0], r[1]) for r in got]
    expected = [(c, duck_types.get(c, "VARCHAR")) for c in columns]
    if actual != expected:
        an = {n for n, _ in actual}
        en = {n for n, _ in expected}
        raise G1C4Error(
            f"{parquet} physical schema mismatch — "
            f"missing={sorted(en - an)} extra={sorted(an - en)} "
            f"actual={actual}")


_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def verify_self(
    tape_dir: Path,
    *,
    running_commit: str | None = None,
    explain_kw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tape self-verification (§4.4 layer A).

    Recomputes every manifest declaration from the physical tape and the
    explain API; verifies internal consistency. Does NOT prove input
    bytes — that is the --bundle layer. ``full_provenance`` is always
    NOT_RUN here by construction.

    Generator-commit semantics: ``generator.code_commit`` is a PROVENANCE
    record (which commit generated this tape), not a runtime requirement.
    Verification does not require checking out the declared commit — but
    capsule-fixture digests embed code_commit, so strict fixture-digest
    equality only holds when the declared commit equals the running one;
    otherwise fixtures are verified at determinism level (explain twice
    -> byte-identical) plus capsule internal digest consistency, and the
    result records ``generator.code_commit_match``.
    """
    tape_dir = Path(tape_dir)
    manifest = load_manifest(tape_dir)
    checks: list[dict[str, Any]] = []

    gen = manifest["generator"]
    declared_commit = gen.get("code_commit")
    if not (isinstance(declared_commit, str)
            and _HEX40.match(declared_commit)):
        raise G1C4Error(
            f"generator.code_commit {declared_commit!r} is not a "
            "well-formed commit record — generator authority malformed")
    if gen.get("tape_schema_version") != TAPE_SCHEMA_VERSION:
        raise G1C4Error(
            f"generator.tape_schema_version "
            f"{gen.get('tape_schema_version')!r} != {TAPE_SCHEMA_VERSION}")
    running = running_commit if running_commit is not None \
        else _git_head()
    commit_match = declared_commit == running
    checks.append({"check": "generator_authority", "result": "PASS",
                   "declared_commit": declared_commit,
                   "running_commit": running,
                   "code_commit_match": commit_match})

    for parquet, cols, dt in (
            (PUB_PARQUET, PUB_COLUMNS, PUB_DUCK_TYPES),
            (CHAIN_PARQUET, CHAIN_COLUMNS, CHAIN_DUCK_TYPES)):
        _table_schema_ok(tape_dir, parquet, cols, dt)
    checks.append({"check": "table_physical_schema", "result": "PASS"})

    # References internal consistency: the declared pinned-entry lists
    # must recompute to their declared set_digests and the declared
    # basis — mutating references metadata alone MUST fail here.
    refs = manifest["references"]
    set_digests: dict[str, str] = {}
    for fam in ("fitrs", "firds", "gleif"):
        declared_set = refs.get(fam) or {}
        entries = declared_set.get("entries") or []
        try:
            recomputed = canonical_reference_set(entries)
        except G1BError as exc:
            raise G1C4Error(
                f"references.{fam} malformed: {exc}") from exc
        if recomputed["set_digest"] != declared_set.get("set_digest"):
            raise G1C4Error(
                f"references.{fam}.set_digest "
                f"{declared_set.get('set_digest')!r} does not recompute "
                "from declared entries — reference metadata tampered")
        set_digests[fam] = recomputed["set_digest"]
    if reference_basis_id(set_digests) != refs.get("basis_id"):
        raise G1C4Error(
            "references.basis_id does not recompute from the declared "
            "family set digests — reference authority inconsistent")
    checks.append({"check": "references_internal_consistency",
                   "result": "PASS"})

    actual = tape_outputs(tape_dir)
    declared = manifest["outputs"]
    for key in ("publications_rows", "trade_chains_rows",
                "publications_semantic_digest", "chains_semantic_digest",
                "dataset_semantic_digest"):
        if declared.get(key) != actual[key]:
            raise G1C4Error(
                f"outputs.{key} declared {declared.get(key)!r} != "
                f"recomputed {actual[key]!r} — tape content mismatch")
    checks.extend({"check": n, "result": "PASS"} for n in (
        "table_row_counts", "publications_semantic_digest",
        "chains_semantic_digest", "dataset_semantic_digest"))
    _disposition_counts_ok(declared.get("disposition_counts") or {},
                           actual["disposition_counts"])
    checks.append({"check": "disposition_counts", "result": "PASS"})

    for fx in manifest["capsule_fixtures"]:
        declared_sha = fx.get("capsule_sha256")
        if not (isinstance(declared_sha, str)
                and _HEX64.match(declared_sha)):
            raise G1C4Error(
                f"capsule fixture {fx.get('subject')!r}: declared "
                "capsule_sha256 malformed")
        kw = explain_kw or {}
        cap_a = _explain_subject(tape_dir, fx["subject"], kw)
        cap_b = _explain_subject(tape_dir, fx["subject"], kw)
        if capsule_bytes(cap_a) != capsule_bytes(cap_b):
            raise G1C4Error(
                f"capsule fixture {fx['subject']!r}: explain not "
                "deterministic — repeated call produced different bytes")
        if cap_a["capsule_sha256"] != capsule_sha256(cap_a):
            raise G1C4Error("capsule internal digest inconsistency")
        if commit_match and cap_a["capsule_sha256"] != declared_sha:
            raise G1C4Error(
                f"capsule fixture {fx['subject']!r}: capsule_sha256 "
                f"{cap_a['capsule_sha256']!r} != declared "
                f"{declared_sha!r} — capsule evidence tampered")
    checks.append({
        "check": "capsule_fixtures", "result": "PASS",
        "mode": "strict_declared_digest"
        if commit_match
        else "determinism_plus_internal_consistency"
        " (declared digests attest the declared generator commit)",
        "fixtures": len(manifest["capsule_fixtures"])})

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
    running_commit: str | None = None,
    explain_kw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full provenance verification (§4.4 layer B): self-verification
    plus byte-level authentication of every declared input."""
    result = verify_self(tape_dir, running_commit=running_commit,
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

def _rebuild_universe(
    bundle_dir: Path,
    fitrs_paths: list[Path],
    firds_paths: list[Path],
    gleif_dir: Path,
    window: dict[str, str],
    work_dir: Path,
    *,
    date: str,
    generated_utc: str,
    expected_g1a_digest: str,
) -> dict[str, Any]:
    """End-to-end universe rebuild for a §4.5 replay: G1-A observed set
    from the sealed bundle, then G1-B dispositions + reference links from
    that rebuilt G1-A and the pinned FITRS/FIRDS/GLEIF snapshots. Writes
    the rebuilt artifacts under ``work_dir`` and returns their paths and
    digests."""
    from . import g1a as _g1a
    from . import g1b as _g1b

    bundle_dir = Path(bundle_dir)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    a_report = _g1a.build_report(
        bundle_dir / "raw", bundle_dir / "runs", window=window,
        bundle_dir=bundle_dir, date=date, generated_utc=generated_utc)
    a_path = work_dir / "g1a_observed_instrument_set.yaml"
    a_path.write_bytes(yaml.safe_dump(
        {"observed_instrument_set":
         a_report["observed_instrument_set"]}).encode("utf-8"))
    g1a_digest = a_report["observed_instrument_set"]["semantic_digest"]

    b_report, b_rows, b_links = _g1b.build_report(
        a_path, fitrs_paths, firds_paths, gleif_dir,
        date=date, generated_utc=generated_utc,
        expected_g1a_digest=expected_g1a_digest)
    b_rows_path = work_dir / "g1b_rows.jsonl"
    b_links_path = work_dir / "g1b_reference_links.jsonl"
    b_report_path = work_dir / "g1b_report.yaml"
    b_rows_path.write_bytes(canonical_rows_bytes(b_rows))
    b_links_path.write_bytes(canonical_links_bytes(b_links))
    b_report_path.write_bytes(yaml.safe_dump(
        b_report, sort_keys=False).encode("utf-8"))

    return {
        "g1a_path": a_path,
        "g1a_semantic_digest": g1a_digest,
        "g1b_rows_path": b_rows_path,
        "g1b_links_path": b_links_path,
        "g1b_report_path": b_report_path,
        "g1b_rows_digest": _sha(b_rows_path.read_bytes()),
        "g1b_links_sha256": _sha(b_links_path.read_bytes()),
        "g1b_report_sha256": _sha(b_report_path.read_bytes()),
        "g1b_basis_id": b_report["reference_basis_id"],
    }


DEFAULT_ANCHORS: dict[str, str] = {
    "g1a_semantic_digest": EXPECTED_G1A_SEMANTIC_DIGEST,
    "g1b_rows_digest": EXPECTED_G1B_ROWS_DIGEST,
    "g1b_links_sha256": EXPECTED_G1B_LINKS_SHA256,
    "g1b_basis_id": EXPECTED_G1B_BASIS_ID,
}


def replay_materialization(
    bundle_dir: Path,
    *,
    fitrs_paths: list[Path],
    firds_paths: list[Path],
    gleif_dir: Path,
    window: dict[str, str],
    tape_dir: Path,
    work_dir: Path,
    date: str,
    generated_utc: str,
    anchors: dict[str, str] | None = None,
    capsule_subjects: list[dict[str, Any]] | None = None,
    code_commit: str | None = None,
    rebuild_universe: Any | None = None,
    c2_authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Independent §4.5 replay — the FULL pipeline, indivisible:

    sealed bundle -> G1-A rebuild -> G1-B rebuild (pinned snapshots)
    -> C1 materialization -> TAPE_MANIFEST emission.

    ``anchors`` pins the rebuilt upstream artifacts to the verified
    canonical digests (defaults = production constants). The rebuilt
    G1-B report's own bytes carry build metadata (date/code_commit), so
    its sha256 is self-derived, while rows/links/basis digests are
    canonical anchors. ``c2_authority`` supplies the C2 report that
    capsule-fixture explains authenticate against
    ``{report_path, expected_sha256}`` — default = the verified repo
    artifact at ``EXPECTED_C2_REPORT_SHA256``.
    """
    bundle_dir = Path(bundle_dir)
    tape_dir = Path(tape_dir)
    anchors = dict(anchors or DEFAULT_ANCHORS)

    rebuilt = (rebuild_universe or _rebuild_universe)(
        bundle_dir, fitrs_paths, firds_paths, gleif_dir, window,
        work_dir, date=date, generated_utc=generated_utc,
        expected_g1a_digest=anchors["g1a_semantic_digest"])

    # Anchors: a replay that cannot re-derive the canonical upstream
    # digests is NOT a reproduction — fail before materializing.
    for key, name in (("g1a_semantic_digest", "G1-A semantic digest"),
                      ("g1b_rows_digest", "G1-B rows digest"),
                      ("g1b_links_sha256", "G1-B reference links"),
                      ("g1b_basis_id", "G1-B reference basis")):
        if rebuilt[key] != anchors[key]:
            raise G1C4Error(
                f"rebuilt {name} {rebuilt[key]!r} != canonical anchor "
                f"{anchors[key]!r} — upstream replay diverged")

    from .g1c1 import build_report as _c1_build  # local: heavy import
    report, _pub_bytes, _chain_bytes = _c1_build(
        bundle_dir / "raw", bundle_dir / "runs",
        rebuilt["g1a_path"], rebuilt["g1b_rows_path"],
        rebuilt["g1b_report_path"], window=window, tape_dir=tape_dir,
        date=date, generated_utc=generated_utc,
        expected_g1a_digest=anchors["g1a_semantic_digest"],
        expected_g1b_digest=anchors["g1b_rows_digest"],
        expected_g1b_report_sha256=rebuilt["g1b_report_sha256"],
        expected_g1b_basis_id=anchors["g1b_basis_id"])

    dataset = report["dataset_semantic_digest"]
    c2 = c2_authority or {}
    explain_kw = {
        "c2_report_path": c2.get("report_path") or repo_file(
            "evidence/g1-c2/G1-C2_as_of_reconstruction.yaml"),
        "g1b_rows_path": rebuilt["g1b_rows_path"],
        "g1b_report_path": rebuilt["g1b_report_path"],
        "g1b_links_path": rebuilt["g1b_links_path"],
        "expected_c2_report_sha256": c2.get(
            "expected_sha256", EXPECTED_C2_REPORT_SHA256),
        "expected_dataset_digest": dataset,
        "expected_g1b_digest": anchors["g1b_rows_digest"],
        "expected_g1b_report_sha256": rebuilt["g1b_report_sha256"],
        "expected_g1b_basis_id": anchors["g1b_basis_id"],
        "expected_g1b_links_sha256": anchors["g1b_links_sha256"],
    }

    # Every tape build emits its manifest — indivisible (§4.4).
    manifest = build_tape_manifest(
        tape_dir, runs_dir=bundle_dir / "runs", raw_root=bundle_dir / "raw",
        bundle_dir=bundle_dir, fitrs_paths=fitrs_paths,
        firds_paths=firds_paths, gleif_dir=gleif_dir,
        g1b_report_path=rebuilt["g1b_report_path"], window=window,
        capsule_subjects=capsule_subjects, code_commit=code_commit,
        expected_g1b_report_sha256=rebuilt["g1b_report_sha256"],
        expected_g1b_basis_id=anchors["g1b_basis_id"],
        explain_kw=explain_kw)
    manifest_path = write_manifest(tape_dir, manifest)

    tables = report["tables"]
    return {
        "tape_dir": str(tape_dir),
        "g1a_semantic_digest": rebuilt["g1a_semantic_digest"],
        "g1b_rows_digest": rebuilt["g1b_rows_digest"],
        "g1b_links_sha256": rebuilt["g1b_links_sha256"],
        "g1b_basis_id": rebuilt["g1b_basis_id"],
        "publications_rows": tables["publications"]["row_count"],
        "trade_chains_rows": tables["trade_chains"]["row_count"],
        "publications_semantic_digest":
            tables["publications"]["semantic_digest"],
        "chains_semantic_digest":
            tables["trade_chains"]["semantic_digest"],
        "dataset_semantic_digest": dataset,
        "tape_manifest_sha256": _sha(manifest_path.read_bytes()),
    }


def reproducibility_check(
    bundle_dir: Path,
    *,
    fitrs_paths: list[Path],
    firds_paths: list[Path],
    gleif_dir: Path,
    window: dict[str, str],
    replay_roots: list[Path],
    capsule_subjects: list[dict[str, Any]] | None = None,
    date: str,
    generated_utc: str,
    anchors: dict[str, str] | None = None,
    rebuild_universe: Any | None = None,
    c2_authority: dict[str, Any] | None = None,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """§4.5 gate: N independent END-TO-END replays — each rebuilds G1-A
    from the sealed bundle, G1-B from the rebuilt G1-A + pinned
    reference snapshots, materializes the tape, and emits its
    TAPE_MANIFEST — MUST produce identical semantic digests and
    byte-identical representative capsules. Parquet byte-identity is NOT
    required."""
    if len(replay_roots) < 2:
        raise G1C4Error(
            "reproducibility requires at least two independent roots")
    runs: list[dict[str, Any]] = []
    for root in replay_roots:
        root = Path(root)
        runs.append(replay_materialization(
            bundle_dir, fitrs_paths=fitrs_paths, firds_paths=firds_paths,
            gleif_dir=gleif_dir, window=window,
            tape_dir=root / "tape", work_dir=root / "rebuild",
            date=date, generated_utc=generated_utc, anchors=anchors,
            capsule_subjects=capsule_subjects, code_commit=code_commit,
            rebuild_universe=rebuild_universe,
            c2_authority=c2_authority))
    ref = runs[0]
    for r in runs[1:]:
        for key in r:
            if key == "tape_dir":
                continue
            if r[key] != ref[key]:
                raise G1C4Error(
                    f"materialization {r['tape_dir']} diverged on "
                    f"{key}: {r[key]!r} != {ref[key]!r}")

    return {
        "materializations": runs,
        "g1a_semantic_digest_equal": True,
        "g1b_rows_digest_equal": True,
        "g1b_links_equal": True,
        "g1b_basis_id_equal": True,
        "publications_semantic_digest_equal": True,
        "chains_semantic_digest_equal": True,
        "dataset_semantic_digest_equal": True,
        "tape_manifests_identical": True,
        "capsule_bytes_byte_identical": True,
        "capsule_identity_note":
            "byte-identity across roots proven via identical "
            "capsule_fixtures in each emitted TAPE_MANIFEST",
        "parquet_byte_equality_required": False,
        "tape_reproducible": True,
    }


# ------------------------------------------------------------ evidence --

REPORT_NAME = "G1-C4_manifest_verify.yaml"


def generate_report(
    tape_dir: Path,
    *,
    evidence_dir: Path,
    date: str,
    generated_utc: str,
    code_commit: str | None = None,
    explain_kw: dict[str, Any] | None = None,
    full_args: dict[str, Any] | None = None,
    repro_args: dict[str, Any] | None = None,
) -> Path:
    """Emit the G1-C4 evidence report + the TAPE_MANIFEST copy.

    Executes the layers live at generation time: self-verification,
    full provenance (when ``full_args`` supplies bundle + snapshots),
    and the §4.5 reproducibility replay (when ``repro_args`` supplies
    the materialization inputs). Metadata only.
    """
    tape_dir = Path(tape_dir)
    manifest = load_manifest(tape_dir)
    self_res = verify_self(tape_dir, running_commit=code_commit,
                           explain_kw=explain_kw)
    full_res = (verify_full(tape_dir, running_commit=code_commit,
                            explain_kw=explain_kw, **full_args)
                if full_args else None)
    repro = reproducibility_check(**repro_args) if repro_args else None

    refs = manifest["references"]
    report: dict[str, Any] = {
        "gate": "G1",
        "issue_id": "G1-C4",
        "report": "TAPE_MANIFEST_VERIFY",
        "date": date,
        "generated_utc": generated_utc,
        "frozen_authority": {
            "spec": "docs/gates/G1.md §4.4/§4.5",
            "freeze_tag": "g1-freeze-v0",
            "freeze_commit":
                "2a5e4684b9a3f6066e6d2a53cbc37b920e2c9ffa",
        },
        "generator": {
            "module": "metrics.g1c4",
            "code_commit": _git_head(),
            "deterministic": True,
        },
        "baseline": {
            "note": "verified G1-C3 merge on main",
        },
        "tape_manifest": {
            "file": MANIFEST_NAME,
            "sha256": self_res["tape_manifest_sha256"],
            "schema_version": manifest["tape_manifest_schema_version"],
        },
        "input": manifest["input"],
        "reference_set_digests": {
            fam: refs[fam]["set_digest"] for fam in
            ("fitrs", "firds", "gleif")},
        "reference_basis_id": refs["basis_id"],
        "outputs": manifest["outputs"],
        "capsule_fixtures": manifest["capsule_fixtures"],
        "self_verification": self_res,
        "full_provenance": full_res or {
            "status": "NOT_RUN — --bundle/--fitrs/--firds/"
                      "--gleif-dir not supplied at generation"},
        "reproducibility": repro or {"status": "NOT_RUN"},
        "semantics": {
            "parquet_byte_equality_required": False,
            "identity_basis":
                "canonical JSONL semantic digests + canonical capsule "
                "bytes; physical Parquet bytes are non-authoritative",
            "self_verify_vs_provenance":
                "self-verification proves the tape matches its own "
                "manifest; full provenance authenticates the input "
                "BYTES (sealed bundle + pinned reference sets)",
        },
        "hygiene": {
            "provider_payloads_in_repo": False,
            "provider_payloads_in_capsules_or_manifest": False,
        },
    }
    ev = Path(evidence_dir) / "g1-c4"
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / REPORT_NAME
    path.write_bytes(yaml.safe_dump(
        report, sort_keys=False, allow_unicode=True).encode("utf-8"))
    sha = _sha(path.read_bytes())
    (ev / f"{path.stem}.sha256.txt").write_text(
        f"{sha} *{path.name}\n", encoding="utf-8")
    # commit a copy of the tape-local manifest (metadata only)
    (ev / MANIFEST_NAME).write_bytes(
        (tape_dir / MANIFEST_NAME).read_bytes())
    return path


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
