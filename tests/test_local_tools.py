"""Unit tests for headlabs.local.tools — read_file, edit_file, bash."""
from headlabs.local.tools.bash import BashTool
from headlabs.local.tools.edit_file import EditFileTool
from headlabs.local.tools.read_file import ReadFileTool


# ── read_file ────────────────────────────────────────────────────────────────

def test_read_file_returns_numbered_lines(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("line1\nline2\nline3\n")
    result = ReadFileTool().execute({"path": "a.py"}, cwd=str(tmp_path))
    assert not result.is_error
    assert "1\tline1" in result.output
    assert "3\tline3" in result.output


def test_read_file_respects_line_range(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)))
    result = ReadFileTool().execute({"path": "a.py", "start_line": 3, "end_line": 5}, cwd=str(tmp_path))
    assert "line3" in result.output
    assert "line5" in result.output
    assert "line6" not in result.output
    assert "line2" not in result.output


def test_read_file_not_found(tmp_path):
    result = ReadFileTool().execute({"path": "missing.py"}, cwd=str(tmp_path))
    assert result.is_error
    assert "not found" in result.output.lower()


def test_read_file_rejects_directory(tmp_path):
    (tmp_path / "subdir").mkdir()
    result = ReadFileTool().execute({"path": "subdir"}, cwd=str(tmp_path))
    assert result.is_error


def test_read_file_is_read_only_and_never_needs_permission():
    assert ReadFileTool.is_read_only() is True
    assert ReadFileTool.requires_permission({"path": "x"}) is False


def test_read_file_absolute_path(tmp_path):
    f = tmp_path / "abs.py"
    f.write_text("hello")
    result = ReadFileTool().execute({"path": str(f)}, cwd="/nonexistent/cwd")
    assert not result.is_error
    assert "hello" in result.output


# ── edit_file ────────────────────────────────────────────────────────────────

def test_edit_file_replaces_unique_match(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def foo():\n    return 1\n")
    result = EditFileTool().execute(
        {"path": "a.py", "old_text": "return 1", "new_text": "return 2"}, cwd=str(tmp_path)
    )
    assert not result.is_error
    assert f.read_text() == "def foo():\n    return 2\n"


def test_edit_file_no_match_returns_error_with_hint(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def foo():\n    return 1\n")
    result = EditFileTool().execute(
        {"path": "a.py", "old_text": "return  1", "new_text": "return 2"}, cwd=str(tmp_path)
    )
    assert result.is_error
    assert "not found" in result.output.lower()
    # closest-match hint should surface the actual content for the model to self-correct
    assert "return 1" in result.output


def test_edit_file_ambiguous_match_returns_error(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\nx = 1\n")
    result = EditFileTool().execute(
        {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, cwd=str(tmp_path)
    )
    assert result.is_error
    assert "2 locations" in result.output


def test_edit_file_not_found(tmp_path):
    result = EditFileTool().execute(
        {"path": "missing.py", "old_text": "a", "new_text": "b"}, cwd=str(tmp_path)
    )
    assert result.is_error


def test_edit_file_always_requires_permission():
    assert EditFileTool.requires_permission({}) is True


# ── bash ─────────────────────────────────────────────────────────────────────

def test_bash_runs_command_and_captures_stdout(tmp_path):
    result = BashTool().execute({"command": "echo hello"}, cwd=str(tmp_path))
    assert not result.is_error
    assert "hello" in result.output
    assert "exit_code: 0" in result.output


def test_bash_nonzero_exit_marked_as_error(tmp_path):
    result = BashTool().execute({"command": "exit 3"}, cwd=str(tmp_path))
    assert result.is_error
    assert "exit_code: 3" in result.output


def test_bash_runs_in_given_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    result = BashTool().execute({"command": "ls"}, cwd=str(tmp_path))
    assert "marker.txt" in result.output


def test_bash_timeout(tmp_path):
    result = BashTool().execute({"command": "sleep 5", "timeout_s": 1}, cwd=str(tmp_path))
    assert result.is_error
    assert "timed out" in result.output.lower()


def test_bash_always_requires_permission():
    assert BashTool.requires_permission({"command": "ls"}) is True


# ── schema export ────────────────────────────────────────────────────────────

def test_tool_to_api_schema_shape():
    schema = ReadFileTool.to_api_schema()
    assert schema["name"] == "read_file"
    assert "description" in schema
    assert schema["input_schema"]["type"] == "object"
    assert "path" in schema["input_schema"]["properties"]
