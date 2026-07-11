"""Unit tests for headlabs.local.tools.glob_tool."""
import time

from headlabs.local.tools.glob_tool import GlobTool


def test_glob_finds_matching_files(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    (tmp_path / "c.txt").write_text("x")

    result = GlobTool().execute({"pattern": "*.py"}, cwd=str(tmp_path))
    assert not result.is_error
    assert "a.py" in result.output
    assert "b.py" in result.output
    assert "c.txt" not in result.output


def test_glob_recursive_pattern(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "nested").mkdir()
    (tmp_path / "src" / "nested" / "deep.py").write_text("x")

    result = GlobTool().execute({"pattern": "**/*.py"}, cwd=str(tmp_path))
    assert "deep.py" in result.output


def test_glob_excludes_default_ignored_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.py").write_text("x")
    (tmp_path / "real.py").write_text("x")

    result = GlobTool().execute({"pattern": "**/*.py"}, cwd=str(tmp_path))
    assert "real.py" in result.output
    assert ".git" not in result.output


def test_glob_no_matches(tmp_path):
    result = GlobTool().execute({"pattern": "*.nonexistent"}, cwd=str(tmp_path))
    assert not result.is_error
    assert "No files matched" in result.output


def test_glob_sorted_by_mtime_most_recent_first(tmp_path):
    old = tmp_path / "old.py"
    old.write_text("x")
    time.sleep(0.05)
    new = tmp_path / "new.py"
    new.write_text("x")

    result = GlobTool().execute({"pattern": "*.py"}, cwd=str(tmp_path))
    lines = result.output.splitlines()
    assert lines[0].endswith("new.py")
    assert lines[1].endswith("old.py")


def test_glob_directory_not_found(tmp_path):
    result = GlobTool().execute({"pattern": "*.py", "path": str(tmp_path / "missing")}, cwd=str(tmp_path))
    assert result.is_error
    assert "not found" in result.output.lower()


def test_glob_path_is_not_a_directory(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    result = GlobTool().execute({"pattern": "*.py", "path": str(f)}, cwd=str(tmp_path))
    assert result.is_error


def test_glob_never_requires_permission():
    assert GlobTool.requires_permission({}) is False
    assert GlobTool.is_read_only() is True


def test_glob_relative_path_resolved_against_cwd(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.py").write_text("x")
    result = GlobTool().execute({"pattern": "*.py", "path": "sub"}, cwd=str(tmp_path))
    assert "file.py" in result.output
