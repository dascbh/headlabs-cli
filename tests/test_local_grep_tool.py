"""Unit tests for headlabs.local.tools.grep_tool."""
from headlabs.local.tools.grep_tool import GrepTool


def test_grep_finds_matching_lines(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")

    result = GrepTool().execute({"pattern": "def foo"}, cwd=str(tmp_path))
    assert not result.is_error
    assert "a.py:1:" in result.output
    assert "def foo" in result.output
    assert "def bar" not in result.output


def test_grep_case_insensitive_by_default(tmp_path):
    (tmp_path / "a.py").write_text("FOOBAR\n")
    result = GrepTool().execute({"pattern": "foobar"}, cwd=str(tmp_path))
    assert "FOOBAR" in result.output


def test_grep_case_sensitive_when_requested(tmp_path):
    (tmp_path / "a.py").write_text("FOOBAR\n")
    result = GrepTool().execute({"pattern": "foobar", "case_sensitive": True}, cwd=str(tmp_path))
    assert "No matches" in result.output


def test_grep_glob_filter_restricts_extension(tmp_path):
    (tmp_path / "a.py").write_text("target_string\n")
    (tmp_path / "b.txt").write_text("target_string\n")

    result = GrepTool().execute({"pattern": "target_string", "glob": "*.py"}, cwd=str(tmp_path))
    assert "a.py" in result.output
    assert "b.txt" not in result.output


def test_grep_no_matches(tmp_path):
    (tmp_path / "a.py").write_text("hello\n")
    result = GrepTool().execute({"pattern": "nonexistent_pattern_xyz"}, cwd=str(tmp_path))
    assert not result.is_error
    assert "No matches found" in result.output


def test_grep_invalid_regex_returns_error(tmp_path):
    result = GrepTool().execute({"pattern": "("}, cwd=str(tmp_path))
    assert result.is_error
    assert "Invalid regex" in result.output


def test_grep_excludes_default_ignored_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("findme\n")
    (tmp_path / "app.js").write_text("findme\n")

    result = GrepTool().execute({"pattern": "findme"}, cwd=str(tmp_path))
    assert "app.js" in result.output
    assert "node_modules" not in result.output


def test_grep_skips_binary_files(tmp_path):
    binary_file = tmp_path / "data.bin"
    binary_file.write_bytes(b"\x00\x01findme\x02\x03")
    text_file = tmp_path / "text.py"
    text_file.write_text("findme\n")

    result = GrepTool().execute({"pattern": "findme"}, cwd=str(tmp_path))
    assert "text.py" in result.output
    assert "data.bin" not in result.output


def test_grep_directory_not_found(tmp_path):
    result = GrepTool().execute({"pattern": "x", "path": str(tmp_path / "missing")}, cwd=str(tmp_path))
    assert result.is_error


def test_grep_never_requires_permission():
    assert GrepTool.requires_permission({}) is False
    assert GrepTool.is_read_only() is True


def test_grep_regex_pattern_matches_across_multiple_files(tmp_path):
    (tmp_path / "a.py").write_text("import os\n")
    (tmp_path / "b.py").write_text("import sys\n")
    (tmp_path / "c.py").write_text("import json\n")

    result = GrepTool().execute({"pattern": r"^import (os|sys)$"}, cwd=str(tmp_path))
    assert "a.py" in result.output
    assert "b.py" in result.output
    assert "c.py" not in result.output
