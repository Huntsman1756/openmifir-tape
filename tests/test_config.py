"""Deterministic, offline tests for the G0-A1 config loader."""

from pathlib import Path

import pytest

from collectors.cli import main as g0a1_main
from collectors.config import (
    ConfigError,
    default_config_dir,
    load_all_sources,
    load_source_config,
)


def test_load_source_config_minimal(tmp_path: Path):
    f = tmp_path / "x.yaml"
    f.write_text(
        "source_id: test\nallowed_hosts: [example.test]\n"
        "entrypoint:\n  base_url: https://example.test\n", encoding="utf-8")
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


def test_load_missing_allowed_hosts_raises(tmp_path: Path):
    # Fail closed: without an explicit host allowlist the transport must not
    # be able to fetch anything.
    f = tmp_path / "bad.yaml"
    f.write_text("source_id: test\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_source_config(f)


def test_load_all_sources_missing_dir_raises(tmp_path: Path):
    # Fail closed: a nonexistent descriptor dir must error, not silently yield
    # zero sources (Path.glob returns empty on missing dirs).
    with pytest.raises(ConfigError):
        load_all_sources(tmp_path / "no-such-dir")


def test_load_all_sources_filters_yaml(tmp_path: Path):
    (tmp_path / "a.yaml").write_text(
        "source_id: a\nallowed_hosts: [a.test]\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text(
        "source_id: b\nallowed_hosts: [b.test]\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("source_id: c\n", encoding="utf-8")
    confs = load_all_sources(tmp_path)
    assert set(confs) == {"a", "b"}


def test_real_source_descriptors_load(tmp_path: Path):
    # The shipped descriptors must satisfy the loader contract.
    confs = load_all_sources(Path("config/sources"))
    assert set(confs) == {"bme_apa", "blb_apae"}
    assert confs["bme_apa"]["allowed_hosts"] == ["www.bolsasymercados.es"]
    assert confs["blb_apae"]["allowed_hosts"] == ["www.bloombergapa.com"]


def test_default_config_dir_loads_packaged_descriptors():
    # The default resolution is the packaged config.sources resource — it
    # must satisfy the same loader contract as a checkout directory.
    confs = load_all_sources(default_config_dir())
    assert set(confs) == {"bme_apa", "blb_apae"}
    assert confs["bme_apa"]["allowed_hosts"] == ["www.bolsasymercados.es"]


def test_default_config_dir_ignores_cwd(tmp_path: Path, monkeypatch):
    # No cwd fallback: an unrelated empty working directory must not affect
    # (or be probed by) the packaged-descriptor default.
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "config").exists()
    confs = load_all_sources(default_config_dir())
    assert set(confs) == {"bme_apa", "blb_apae"}


def test_list_sources_uses_packaged_descriptors(capsys):
    # The console entry point resolves packaged descriptors without
    # --config-dir and without touching the network.
    assert g0a1_main(["--list-sources"]) == 0
    assert capsys.readouterr().out.split() == ["blb_apae", "bme_apa"]


def test_list_sources_config_dir_override(tmp_path: Path, capsys):
    (tmp_path / "x.yaml").write_text(
        "source_id: x\nallowed_hosts: [x.test]\n", encoding="utf-8")
    assert g0a1_main(["--config-dir", str(tmp_path), "--list-sources"]) == 0
    assert capsys.readouterr().out.split() == ["x"]
