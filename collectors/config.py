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
    with open(path, "r", encoding="utf-8") as fh:
        conf = yaml.safe_load(fh) or {}
    if not isinstance(conf, dict) or "source_id" not in conf:
        raise ConfigError(f"{path}: missing 'source_id'")
    _require(conf, "source_id", conf["source_id"])
    return conf


def load_all_sources(sources_dir: Path) -> dict[str, dict]:
    """Load every *.yaml source descriptor in a directory, keyed by source_id."""
    result: dict[str, dict] = {}
    for path in sorted(sources_dir.glob("*.yaml")):
        conf = load_source_config(path)
        result[conf["source_id"]] = conf
    return result
