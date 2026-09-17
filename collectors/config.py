"""Load source interface descriptors from config/sources/*.yaml.

Semantics live in docs/gates/G0.md; this module only reads the frozen FACTS
(entrypoints, access mode, retrieval intervals) that operators maintain.
It never invents a URL and never falls back to a hardcoded default.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised when a source descriptor is missing or invalid."""


def _require(conf: dict, field: str, source_id: str) -> None:
    if field not in conf or conf[field] in (None, ""):
        raise ConfigError(f"config/sources/{source_id}.yaml: missing required field '{field}'")


def load_source_config(path: Path) -> dict:
    """Load and minimally validate a single source descriptor YAML file."""
    if not path.is_file():
        raise ConfigError(f"source descriptor not found: {path}")
    with open(path, encoding="utf-8") as fh:
        conf = yaml.safe_load(fh) or {}
    if not isinstance(conf, dict) or "source_id" not in conf:
        raise ConfigError(f"{path}: missing 'source_id'")
    _require(conf, "source_id", conf["source_id"])
    _require(conf, "allowed_hosts", conf["source_id"])
    hosts = conf["allowed_hosts"]
    if not isinstance(hosts, list) or not hosts or not all(
            isinstance(h, str) and "://" not in h and "/" not in h
            for h in hosts):
        raise ConfigError(
            f"{path}: 'allowed_hosts' must be a non-empty list of bare "
            f"hostnames (no scheme, no path)")
    return conf


def load_all_sources(sources_dir: Path) -> dict[str, dict]:
    """Load every *.yaml source descriptor in a directory, keyed by source_id.

    A missing directory is a ConfigError, not an empty result: Path.glob on a
    nonexistent path yields nothing, which would otherwise let a mistyped or
    unresolvable --config-dir degrade to a silent no-op run.
    """
    if not sources_dir.is_dir():
        raise ConfigError(f"source descriptor directory not found: {sources_dir}")
    result: dict[str, dict] = {}
    for path in sorted(sources_dir.glob("*.yaml")):
        conf = load_source_config(path)
        result[conf["source_id"]] = conf
    return result


def default_config_dir() -> Path:
    """Default source-descriptor directory.

    The descriptors live in the repository checkout (``config/sources/``) and
    are NOT shipped in the wheel. Resolution order: the package's checkout
    root, then the current working directory. If neither exists, returns the
    conventional relative path so the loader fails with a clear ConfigError.
    """
    pkg_root = Path(__file__).resolve().parents[1]
    candidate = pkg_root / "config" / "sources"
    if candidate.is_dir():
        return candidate
    cwd_candidate = Path.cwd() / "config" / "sources"
    if cwd_candidate.is_dir():
        return cwd_candidate
    return candidate
