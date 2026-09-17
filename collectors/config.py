"""Load source interface descriptors from config/sources/*.yaml.

Semantics live in docs/gates/G0.md; this module only reads the frozen FACTS
(entrypoints, access mode, retrieval intervals) that operators maintain.
It never invents a URL and never falls back to a hardcoded default.

The descriptors ship inside the wheel as the ``config.sources`` package
(the repository's ``config/sources/`` directory is the packaged resource —
there is exactly one copy). The default resolution is
``importlib.resources``-based: no cwd probing, because the operator's
working directory has no contractual relationship with the installed
package. ``--config-dir`` remains the explicit override.
"""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised when a source descriptor is missing or invalid."""


def _require(conf: dict, field: str, source_id: str) -> None:
    if field not in conf or conf[field] in (None, ""):
        raise ConfigError(f"config/sources/{source_id}.yaml: missing required field '{field}'")


def load_source_config(path: Path | Traversable) -> dict:
    """Load and minimally validate a single source descriptor YAML file."""
    if not path.is_file():
        raise ConfigError(f"source descriptor not found: {path}")
    with path.open(encoding="utf-8") as fh:
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


def load_all_sources(sources_dir: Path | Traversable) -> dict[str, dict]:
    """Load every *.yaml source descriptor in a directory, keyed by source_id.

    Accepts a filesystem ``Path`` (``--config-dir``) or a packaged
    ``Traversable`` (``importlib.resources``). A missing directory is a
    ConfigError, not an empty result: iterating a nonexistent path yields
    nothing, which would otherwise let a mistyped or unresolvable
    --config-dir degrade to a silent no-op run.
    """
    if not sources_dir.is_dir():
        raise ConfigError(f"source descriptor directory not found: {sources_dir}")
    result: dict[str, dict] = {}
    for path in sorted(sources_dir.iterdir(), key=lambda p: p.name):
        if not path.name.endswith(".yaml"):
            continue
        conf = load_source_config(path)
        result[conf["source_id"]] = conf
    return result


def default_config_dir() -> Traversable:
    """Default source descriptors, shipped inside the distribution.

    ``config/sources/`` is packaged as the ``config.sources`` package, so
    the same directory serves both an editable checkout and an installed
    wheel. A distribution missing its descriptors is a packaging defect and
    fails closed with ConfigError.
    """
    try:
        return resources.files("config.sources")
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "packaged source descriptors not found ('config.sources' is not "
            "installed); reinstall the distribution or pass --config-dir"
        ) from exc
