"""Unit tests for headlabs.local.permission."""
import json

import pytest

from headlabs.local.permission import PermissionDenied, PermissionManager


def test_no_permission_needed_skips_prompt_entirely(tmp_path):
    calls = []
    manager = PermissionManager(str(tmp_path), prompt_fn=lambda *a: calls.append(a) or "no")
    manager.check("read_file", {"path": "x"}, needs_permission=False)
    assert calls == []  # prompt never invoked


def test_auto_mode_bypasses_prompt(tmp_path):
    calls = []
    manager = PermissionManager(str(tmp_path), mode="auto", prompt_fn=lambda *a: calls.append(a) or "no")
    manager.check("bash", {"command": "rm -rf /"}, needs_permission=True)
    assert calls == []  # never prompted, even though needs_permission=True


def test_yes_answer_allows(tmp_path):
    manager = PermissionManager(str(tmp_path), prompt_fn=lambda *a: "yes")
    manager.check("bash", {"command": "ls"}, needs_permission=True)  # should not raise


def test_no_answer_raises_permission_denied(tmp_path):
    manager = PermissionManager(str(tmp_path), prompt_fn=lambda *a: "no")
    with pytest.raises(PermissionDenied):
        manager.check("bash", {"command": "rm -rf /"}, needs_permission=True)


def test_always_answer_persists_and_skips_future_prompts(tmp_path):
    prompt_calls = []
    manager = PermissionManager(str(tmp_path), prompt_fn=lambda *a: prompt_calls.append(a) or "always")

    manager.check("bash", {"command": "ls"}, needs_permission=True)
    assert len(prompt_calls) == 1

    # Second call for the same tool should not prompt again.
    manager.check("bash", {"command": "pwd"}, needs_permission=True)
    assert len(prompt_calls) == 1

    perms_file = tmp_path / ".headlabs" / "local_permissions.json"
    assert perms_file.exists()
    assert "bash" in json.loads(perms_file.read_text())["always_allow"]


def test_always_allow_is_scoped_per_tool_not_global(tmp_path):
    manager = PermissionManager(str(tmp_path), prompt_fn=lambda *a: "always")
    manager.check("bash", {"command": "ls"}, needs_permission=True)

    prompt_calls = []
    manager2 = PermissionManager(str(tmp_path), prompt_fn=lambda *a: prompt_calls.append(a) or "yes")
    manager2.check("edit_file", {"path": "x"}, needs_permission=True)
    assert len(prompt_calls) == 1  # edit_file was never marked "always", must still prompt


def test_always_allow_persists_across_manager_instances(tmp_path):
    m1 = PermissionManager(str(tmp_path), prompt_fn=lambda *a: "always")
    m1.check("bash", {"command": "ls"}, needs_permission=True)

    prompt_calls = []
    m2 = PermissionManager(str(tmp_path), prompt_fn=lambda *a: prompt_calls.append(a) or "no")
    m2.check("bash", {"command": "pwd"}, needs_permission=True)  # should not raise, no prompt
    assert prompt_calls == []


def test_invalid_mode_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        PermissionManager(str(tmp_path), mode="yolo")


def test_permissions_scoped_to_cwd_directory(tmp_path):
    dir_a = tmp_path / "project_a"
    dir_b = tmp_path / "project_b"
    dir_a.mkdir()
    dir_b.mkdir()

    ma = PermissionManager(str(dir_a), prompt_fn=lambda *a: "always")
    ma.check("bash", {"command": "ls"}, needs_permission=True)

    prompt_calls = []
    mb = PermissionManager(str(dir_b), prompt_fn=lambda *a: prompt_calls.append(a) or "no")
    with pytest.raises(PermissionDenied):
        mb.check("bash", {"command": "ls"}, needs_permission=True)
    assert len(prompt_calls) == 1  # project_b never got the "always allow", must prompt independently
