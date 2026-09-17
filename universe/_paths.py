"""Resolution of repo-root-relative resource paths.

Frozen spec artifacts (``fixtures/``, ``evidence/``) live in the repository
checkout and are NOT shipped inside the wheel. These helpers resolve them
relative to the checkout — whether the package is imported from the tree, an
editable install, or run from a different working directory — and otherwise
return the checkout-relative location so callers can raise their own
domain-specific not-found error.
"""

from __future__ import annotations

from pathlib import Path


def repo_file(*parts: str) -> Path:
    """Best-effort resolution of a repo-root-relative path.

    Tries the package's checkout root first, then the current working
    directory. If neither exists, returns the checkout-relative path anyway
    (callers must handle a missing file — always fail closed).
    """
    pkg_root = Path(__file__).resolve().parents[1]
    candidate = pkg_root.joinpath(*parts)
    if candidate.is_file():
        return candidate
    cwd_candidate = Path.cwd().joinpath(*parts)
    if cwd_candidate.is_file():
        return cwd_candidate
    return candidate
