"""Unit tests for headlabs.local.config — no network, no real HOME writes."""
import json

from headlabs.local import config as local_config


def test_default_config_is_not_configured():
    cfg = local_config.LocalConfig()
    assert cfg.is_configured() is False


def test_configured_requires_base_url_and_model():
    assert local_config.LocalConfig(base_url="http://x", model=None).is_configured() is False
    assert local_config.LocalConfig(base_url=None, model="m").is_configured() is False
    assert local_config.LocalConfig(base_url="http://x", model="m").is_configured() is True


def test_load_local_config_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_FILE", tmp_path / "local_config.json")
    cfg = local_config.load_local_config()
    assert cfg == local_config.LocalConfig()


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    cfg_file = tmp_path / "local_config.json"
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_FILE", cfg_file)

    saved = local_config.LocalConfig(base_url="http://localhost:8000/v1", model="qwen", api_key="dummy")
    local_config.save_local_config(saved)

    assert cfg_file.exists()
    loaded = local_config.load_local_config()
    assert loaded == saved


def test_load_local_config_corrupt_file_returns_defaults(tmp_path, monkeypatch):
    cfg_file = tmp_path / "local_config.json"
    cfg_file.write_text("{not valid json")
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_FILE", cfg_file)
    cfg = local_config.load_local_config()
    assert cfg == local_config.LocalConfig()


def test_load_local_config_ignores_unknown_fields(tmp_path, monkeypatch):
    cfg_file = tmp_path / "local_config.json"
    cfg_file.write_text(json.dumps({"base_url": "http://x", "model": "m", "bogus_field": 123}))
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_FILE", cfg_file)
    cfg = local_config.load_local_config()
    assert cfg.base_url == "http://x"
    assert cfg.model == "m"
    assert not hasattr(cfg, "bogus_field")


def test_update_local_config_applies_only_non_none_overrides(tmp_path, monkeypatch):
    cfg_file = tmp_path / "local_config.json"
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_FILE", cfg_file)

    local_config.update_local_config(base_url="http://a", model="m1")
    cfg = local_config.update_local_config(model="m2")  # base_url untouched
    assert cfg.base_url == "http://a"
    assert cfg.model == "m2"
