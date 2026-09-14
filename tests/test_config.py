"""Deterministic, offline tests for the G0-A1 config loader."""

from pathlib import Path

import pytest

from collectors.config import ConfigError, load_all_sources, load_source_config


def test_load_source_config_minimal(tmp_path: Path):
    f = tmp_path / "x.yaml"
    f.write_text("source_id: test\nentrypoint:\n  base_url: https://example.test\n", encoding="utf-8")
    conf = load_source_config(f)
    assert conf["source_id"] == "test"
    assert conf["entrypoint"]["base_url"] == "https://example.test"


def test_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_source_config(tmp_path / "nope.yaml")


def test_load_missing_source_id_raises(tmp_path: Path):
    f = tmp_path / "bad.yaml"
    f.write_text("display: no source id\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_source_config(f)


def test_load_all_sources_filters_yaml(tmp_path: Path):
    (tmp_path / "a.yaml").write_text("source_id: a\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("source_id: b\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("source_id: c\n", encoding="utf-8")
    confs = load_all_sources(tmp_path)
    assert set(confs) == {"a", "b"}
