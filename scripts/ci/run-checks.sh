#!/usr/bin/env bash
# Gatekeeper checks: what autopilot publishes as `ci/netcup`. Deterministic and
# offline (no provider is contacted), mirroring the `test` job of
# .github/workflows/ci.yml: lint, repository hygiene, pytest.
set -euo pipefail
cd "$(dirname "$0")/../.."

step() { echo; echo "== STEP $*"; }

venv=".venv-ci"   # ignored by .gitignore (.venv*/), so ruff skips it too

step "venv + install .[dev]"
if command -v uv >/dev/null 2>&1; then
  uv venv --quiet --allow-existing --python ">=3.11" "$venv"
  uv pip install --quiet --python "$venv/bin/python" -e ".[dev]"
else
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install --quiet -e ".[dev]"
fi
py="$venv/bin/python"

step "ruff"
"$py" -m ruff check .

step "repository hygiene (no provider payloads, no secrets)"
"$py" tools/check_hygiene.py

step "pytest (offline)"
base="${TMPDIR:-/var/tmp}/omt-pytest-$$"
trap 'rm -rf "$base"' EXIT
"$py" -m pytest -q -p no:cacheprovider --basetemp "$base"

echo
echo "ALL CHECKS PASSED"
