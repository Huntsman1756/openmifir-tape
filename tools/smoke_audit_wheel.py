"""Run with python -I after installing the wheel: offline audit CLI smoke.

Runtime must come from the wheel; checkout tests supply synthetic fixtures
only. Synthetic expected hashes are injected in memory, never shipped as
relaxed production authority. No provider data or endpoint is involved.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import metrics
import metrics.g1c3 as explain
import metrics.g1c4 as verify
from metrics.authority import AUDIT_FILES

checkout = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


require(not Path(metrics.__file__).resolve().is_relative_to(checkout), "runtime came from checkout, not wheel")
sys.path.insert(0, str(checkout / "tests"))  # synthetic fixtures, not runtime packages
sys.path.append(str(checkout))  # non-packaged tools/ compatibility fixture helper
from test_g1c4 import _basic_env  # noqa: E402

with tempfile.TemporaryDirectory(prefix="omt-audit-wheel-", dir=os.environ.get("RUNNER_TEMP")) as directory:
    root = Path(directory)
    env = _basic_env(root / "fixture")
    kw = env["explain_kw"]
    authority = root / "authority"
    for key, (folder, name) in AUDIT_FILES.items():
        dest = authority / "evidence" / folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(kw[key], dest)
    for constant, key in (
            ("EXPECTED_C2_REPORT_SHA256", "expected_c2_report_sha256"),
            ("EXPECTED_DATASET_SEMANTIC_DIGEST", "expected_dataset_digest"),
            ("EXPECTED_G1B_ROWS_DIGEST", "expected_g1b_digest"),
            ("EXPECTED_G1B_REPORT_SHA256", "expected_g1b_report_sha256"),
            ("EXPECTED_G1B_BASIS_ID", "expected_g1b_basis_id"),
            ("EXPECTED_G1B_LINKS_SHA256", "expected_g1b_links_sha256")):
        setattr(explain, constant, kw[key])
    empty = root / "empty"
    empty.mkdir()
    with contextlib.chdir(empty):
        for module in ("metrics.g1c3", "metrics.g1c4", "metrics.g1d"):
            subprocess.run([sys.executable, "-I", "-m", module, "--help"],  # noqa: S603 - fixed interpreter/args
                           check=True, capture_output=True)
        out = io.BytesIO()
        stdout = io.TextIOWrapper(out, encoding="utf-8", write_through=True)
        with contextlib.redirect_stdout(stdout):
            require(explain.main(["--tape-dir", str(env["tape"]), "--authority-dir", str(authority),
                                  "--row", env["pub_id"]]) == 0, "explain failed")
        require(json.loads(out.getvalue())["subject"], "capsule subject missing")
        out = io.BytesIO()
        stdout = io.TextIOWrapper(out, encoding="utf-8", write_through=True)
        with contextlib.redirect_stdout(stdout):
            require(verify.main([str(env["tape"]), "--authority-dir", str(authority)]) == 0, "verify failed")
        require(b"self_verification: PASS" in out.getvalue(), "self verification did not pass")
        require(b"NOT_RUN" in out.getvalue(), "self verify incorrectly claims full provenance")
        missing = root / "missing-authority"
        try:
            verify.main([str(env["tape"]), "--authority-dir", str(missing)])
        except explain.G1C3Error:
            pass
        else:
            raise AssertionError("explicit missing authority silently fell back")
        (authority / "evidence/g1-b1/G1-B1_reference_links.jsonl").write_bytes(b"{}\n")
        try:
            explain.main(["--tape-dir", str(env["tape"]), "--authority-dir", str(authority),
                          "--row", env["pub_id"]])
        except explain.G1C3Error:
            pass
        else:
            raise AssertionError("mutated authority passed")
print("installed audit CLI smoke PASS: empty cwd, explicit authority, missing/mutated fail closed")
