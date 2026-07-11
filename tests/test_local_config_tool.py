"""Unit tests for headlabs.local.tools.config_tool."""
from headlabs.local import config as local_config
from headlabs.local.tools.config_tool import ConfigTool


def _isolate_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "local_config.json"
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_FILE", cfg_file)


def test_config_get_returns_current_values(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    local_config.save_local_config(
        local_config.LocalConfig(base_url="http://x:8000/v1", model="test-model", max_iterations=15)
    )
    result = ConfigTool().execute({"action": "get"}, cwd=".")
    assert not result.is_error
    assert "http://x:8000/v1" in result.output
    assert "test-model" in result.output
    assert "15" in result.output


def test_config_get_masks_api_key(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    local_config.save_local_config(local_config.LocalConfig(api_key="super-secret-value"))
    result = ConfigTool().execute({"action": "get"}, cwd=".")
    assert "super-secret-value" not in result.output
    assert "(set)" in result.output


def test_config_set_max_iterations(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    result = ConfigTool().execute({"action": "set", "key": "max_iterations", "value": "50"}, cwd=".")
    assert not result.is_error
    cfg = local_config.load_local_config()
    assert cfg.max_iterations == 50


def test_config_set_rejects_non_writable_field(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    result = ConfigTool().execute({"action": "set", "key": "base_url", "value": "http://evil"}, cwd=".")
    assert result.is_error
    assert "not writable" in result.output


def test_config_set_rejects_invalid_numeric_value(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    result = ConfigTool().execute({"action": "set", "key": "max_iterations", "value": "not_a_number"}, cwd=".")
    assert result.is_error
    assert "Invalid numeric value" in result.output


def test_config_set_requires_key_and_value(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    result = ConfigTool().execute({"action": "set"}, cwd=".")
    assert result.is_error


def test_config_unknown_action(tmp_path, monkeypatch):
    _isolate_config(tmp_path, monkeypatch)
    result = ConfigTool().execute({"action": "delete"}, cwd=".")
    assert result.is_error
    assert "Unknown action" in result.output


def test_config_get_never_requires_permission():
    assert ConfigTool.requires_permission({"action": "get"}) is False


def test_config_set_requires_permission():
    assert ConfigTool.requires_permission({"action": "set"}) is True
