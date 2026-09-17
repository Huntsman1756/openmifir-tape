"""Repository hygiene guard.

Mechanically enforces the AGENTS.md hard rules that are checkable from the
working tree:

  1. No provider data in the repository — nothing tracked under data/, and no
     tracked file named like a provider payload (e.g. *-bmea-posttrade.json,
     BAPA-POST2-*.csv) outside fixtures/.
  2. Evidence is metadata only — tracked files under evidence/ must use a
     metadata extension and stay small.
  3. No committed secrets — high-signal credential patterns are scanned on
     tracked text files. This is best-effort DETECTION of known leak
     shapes, not proof of absence: it cannot recognize every credential
     format, and it does not scan git history.
  4. data/ stays gitignored.

Runs offline, stdlib only. Exits non-zero and prints every violation found.
Used by CI; can be run locally:  python tools/check_hygiene.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Filename patterns that identify provider payloads regardless of location.
PROVIDER_PAYLOAD_NAMES = [
    re.compile(r"\d{4}-\d{2}-\d{2}-bmea-posttrade\.json$"),
    re.compile(r"BAPA-POST2-.*\.csv$", re.IGNORECASE),
]

# Where synthetic fixture payloads are allowed to live.
PAYLOAD_EXEMPT_DIRS = ("fixtures/",)

# Evidence may only contain metadata artifacts.
EVIDENCE_ALLOWED_SUFFIXES = {".yaml", ".yml", ".json", ".jsonl", ".log", ".md", ".txt"}
EVIDENCE_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB - far above any real manifest/log

# Any tracked file above this size is suspicious for a payload/binary.
MAX_TRACKED_BYTES = 5 * 1024 * 1024

# High-signal secret patterns. Deliberately conservative: these must not
# produce false positives on legitimate code or docs.
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY"), "private key block"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "AWS temporary key id"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "GitHub personal access token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{22,}"), "GitHub fine-grained token"),
    (re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"), "Slack token"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "Google API key"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
]

# Binary-ish bytes that mean "don't bother scanning this file as text".
_TEXT_SAMPLE = 4096


def tracked_files() -> list[str]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable not found on PATH")
    out = subprocess.run(  # noqa: S603 - fixed argv on a resolved git path
        [git, "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" in f.read(_TEXT_SAMPLE)
    except OSError:
        return True


def check_gitignore(violations: list[str]) -> None:
    gi = REPO_ROOT / ".gitignore"
    if not gi.exists():
        violations.append(".gitignore is missing")
        return
    text = gi.read_text(encoding="utf-8", errors="replace")
    if not any(line.strip() == "data/" for line in text.splitlines()):
        violations.append(".gitignore does not ignore the data/ directory")


def main() -> int:
    violations: list[str] = []
    check_gitignore(violations)

    for rel in tracked_files():
        path = REPO_ROOT / rel
        name = Path(rel).name

        if rel == "data" or rel.startswith("data/"):
            violations.append(f"provider data tracked: {rel}")

        if not rel.startswith(PAYLOAD_EXEMPT_DIRS):
            for pat in PROVIDER_PAYLOAD_NAMES:
                if pat.search(name):
                    violations.append(f"provider payload filename tracked: {rel}")

        if rel.startswith("evidence/"):
            if path.suffix.lower() not in EVIDENCE_ALLOWED_SUFFIXES:
                violations.append(f"non-metadata file in evidence/: {rel}")
            elif path.exists() and path.stat().st_size > EVIDENCE_MAX_BYTES:
                violations.append(f"oversized evidence file ({path.stat().st_size} B): {rel}")

        try:
            size = path.stat().st_size
        except OSError:
            violations.append(f"tracked file unreadable: {rel}")
            continue
        if size > MAX_TRACKED_BYTES:
            violations.append(f"tracked file exceeds {MAX_TRACKED_BYTES} B: {rel}")
            continue
        if _looks_binary(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for pat, label in SECRET_PATTERNS:
            if pat.search(text):
                violations.append(f"possible {label} in tracked file: {rel}")

    if violations:
        print("Repository hygiene check FAILED:")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("Repository hygiene check passed "
          "(tracked-file scan - see module docstring for scope).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
