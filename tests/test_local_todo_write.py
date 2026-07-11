"""Unit tests for headlabs.local.tools.todo_write."""
import json

from headlabs.local.tools.todo_write import TodoWriteTool, _todos_path


def test_todo_write_creates_file_and_persists(tmp_path):
    result = TodoWriteTool().execute(
        {"todos": [{"content": "Write tests", "status": "in_progress"}]}, cwd=str(tmp_path)
    )
    assert not result.is_error
    path = _todos_path(str(tmp_path))
    assert path.exists()
    data = json.loads(path.read_text())
    assert data == [{"content": "Write tests", "status": "in_progress"}]


def test_todo_write_renders_status_markers(tmp_path):
    result = TodoWriteTool().execute(
        {
            "todos": [
                {"content": "Done task", "status": "completed"},
                {"content": "Active task", "status": "in_progress"},
                {"content": "Future task", "status": "pending"},
            ]
        },
        cwd=str(tmp_path),
    )
    assert "[x] Done task" in result.output
    assert "[~] Active task" in result.output
    assert "[ ] Future task" in result.output


def test_todo_write_replaces_previous_list(tmp_path):
    TodoWriteTool().execute({"todos": [{"content": "First", "status": "pending"}]}, cwd=str(tmp_path))
    TodoWriteTool().execute({"todos": [{"content": "Second", "status": "pending"}]}, cwd=str(tmp_path))

    data = json.loads(_todos_path(str(tmp_path)).read_text())
    assert len(data) == 1
    assert data[0]["content"] == "Second"


def test_todo_write_empty_list_clears(tmp_path):
    TodoWriteTool().execute({"todos": [{"content": "X", "status": "pending"}]}, cwd=str(tmp_path))
    result = TodoWriteTool().execute({"todos": []}, cwd=str(tmp_path))
    assert "cleared" in result.output.lower()
    data = json.loads(_todos_path(str(tmp_path)).read_text())
    assert data == []


def test_todo_write_invalid_status_rejected(tmp_path):
    result = TodoWriteTool().execute(
        {"todos": [{"content": "X", "status": "bogus_status"}]}, cwd=str(tmp_path)
    )
    assert result.is_error
    assert "Invalid status" in result.output


def test_todo_write_default_status_is_pending(tmp_path):
    result = TodoWriteTool().execute({"todos": [{"content": "No status given"}]}, cwd=str(tmp_path))
    assert not result.is_error
    assert "[ ] No status given" in result.output


def test_todo_write_scoped_per_project_directory(tmp_path):
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()

    TodoWriteTool().execute({"todos": [{"content": "Task A", "status": "pending"}]}, cwd=str(proj_a))
    TodoWriteTool().execute({"todos": [{"content": "Task B", "status": "pending"}]}, cwd=str(proj_b))

    data_a = json.loads(_todos_path(str(proj_a)).read_text())
    data_b = json.loads(_todos_path(str(proj_b)).read_text())
    assert data_a[0]["content"] == "Task A"
    assert data_b[0]["content"] == "Task B"


def test_todo_write_never_requires_permission():
    assert TodoWriteTool.requires_permission({}) is False
