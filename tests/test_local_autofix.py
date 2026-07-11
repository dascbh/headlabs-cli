"""Unit tests for headlabs.local.autofix — test command detection and fix prompt building."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

from headlabs.local.autofix import (
    detect_test_command, run_test_command, build_fix_prompt, AutofixTestCommand, AutofixResult,
)


def test_detect_from_config():
    cmd = detect_test_command("/tmp", configured_command="make check")
    assert cmd is not None
    assert cmd.command == "make check"
    assert cmd.source == "config"


def test_detect_from_pyproject_toml(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    cmd = detect_test_command(str(tmp_path))
    assert cmd is not None
    assert "pytest" in cmd.command
    assert cmd.source == "pyproject.toml"


def test_detect_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    cmd = detect_test_command(str(tmp_path))
    assert cmd is not None
    assert cmd.command == "npm test"
    assert cmd.source == "package.json"


def test_detect_from_makefile(tmp_path):
    (tmp_path / "Makefile").write_text("all:\n\techo hi\n\ntest:\n\tpytest\n")
    cmd = detect_test_command(str(tmp_path))
    assert cmd is not None
    assert cmd.command == "make test"
    assert cmd.source == "Makefile"


def test_detect_returns_none_when_nothing_found(tmp_path):
    cmd = detect_test_command(str(tmp_path))
    assert cmd is None


def test_run_test_command_success(tmp_path):
    (tmp_path / "test.sh").write_text("#!/bin/bash\necho ok\nexit 0\n")
    (tmp_path / "test.sh").chmod(0o755)
    result = run_test_command(AutofixTestCommand(command="bash test.sh", source="test"), str(tmp_path))
    assert result.success is True
    assert "ok" in result.output


def test_run_test_command_failure(tmp_path):
    (tmp_path / "test.sh").write_text("#!/bin/bash\necho FAIL\nexit 1\n")
    (tmp_path / "test.sh").chmod(0o755)
    result = run_test_command(AutofixTestCommand(command="bash test.sh", source="test"), str(tmp_path))
    assert result.success is False
    assert "FAIL" in result.output


def test_run_test_command_timeout(tmp_path):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 60)):
        result = run_test_command(AutofixTestCommand(command="sleep 999", source="test"), str(tmp_path))
    assert result.success is False
    assert "timed out" in result.output


def test_build_fix_prompt():
    result = AutofixResult(success=False, output="AssertionError: 1 != 2", command="pytest")
    prompt = build_fix_prompt(result, ["src/main.py", "src/util.py"])
    assert "src/main.py" in prompt
    assert "src/util.py" in prompt
    assert "AssertionError" in prompt
    assert "pytest" in prompt
    assert "fix" in prompt.lower()
