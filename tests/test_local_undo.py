"""Unit tests for headlabs.local.undo — undo stack and git revert logic."""
from __future__ import annotations

from unittest.mock import patch, MagicMock
import subprocess

from headlabs.local.engine import EngineEvent
from headlabs.local.undo import (
    UndoStack, is_git_repo, git_checkout_files, extract_edited_files_from_events,
)


def test_undo_stack_push_and_pop():
    stack = UndoStack("/tmp")
    assert not stack.can_undo()
    stack.push(["file1.py", "file2.py"])
    assert stack.can_undo()
    assert stack.depth == 1
    entry = stack.pop()
    assert entry.files == ["file1.py", "file2.py"]
    assert not stack.can_undo()


def test_undo_stack_multiple_entries():
    stack = UndoStack("/tmp")
    stack.push(["a.py"])
    stack.push(["b.py", "c.py"])
    assert stack.depth == 2
    entry = stack.pop()
    assert entry.files == ["b.py", "c.py"]  # LIFO
    entry = stack.pop()
    assert entry.files == ["a.py"]


def test_undo_stack_empty_push_ignored():
    stack = UndoStack("/tmp")
    stack.push([])
    assert not stack.can_undo()


def test_undo_stack_peek():
    stack = UndoStack("/tmp")
    stack.push(["x.py"])
    entry = stack.peek()
    assert entry.files == ["x.py"]
    assert stack.can_undo()  # peek doesn't remove


def test_is_git_repo_true(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    assert is_git_repo(str(tmp_path)) is True


def test_is_git_repo_false(tmp_path):
    assert is_git_repo(str(tmp_path)) is False


def test_git_checkout_files_success(tmp_path):
    # Set up a git repo with a committed file, then modify it
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)
    f = tmp_path / "test.py"
    f.write_text("original")
    subprocess.run(["git", "add", "test.py"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    f.write_text("modified by agent")

    success, msg = git_checkout_files(["test.py"], str(tmp_path))
    assert success is True
    assert "Reverted" in msg
    assert f.read_text() == "original"


def test_git_checkout_empty_list():
    success, msg = git_checkout_files([], "/tmp")
    assert success is False
    assert "No files" in msg


def test_extract_edited_files_from_events():
    events = [
        EngineEvent(type="tool_call", tool_name="edit_file", tool_input={"path": "src/main.py"}),
        EngineEvent(type="tool_result", tool_name="edit_file", tool_input={"path": "src/main.py"},
                    tool_output="Edited src/main.py (-2 +3 lines)", is_error=False),
        EngineEvent(type="tool_call", tool_name="read_file"),
        EngineEvent(type="tool_result", tool_name="read_file", tool_output="content"),
        EngineEvent(type="tool_call", tool_name="edit_file", tool_input={"path": "src/util.py"}),
        EngineEvent(type="tool_result", tool_name="edit_file", tool_input={"path": "src/util.py"},
                    tool_output="Edited src/util.py (-1 +1 lines)", is_error=False),
    ]
    files = extract_edited_files_from_events(events)
    assert files == ["src/main.py", "src/util.py"]


def test_extract_edited_files_excludes_errors():
    events = [
        EngineEvent(type="tool_result", tool_name="edit_file", tool_input={"path": "bad.py"},
                    tool_output="old_text not found", is_error=True),
    ]
    files = extract_edited_files_from_events(events)
    assert files == []


def test_extract_edited_files_no_duplicates():
    events = [
        EngineEvent(type="tool_result", tool_name="edit_file", tool_input={"path": "x.py"},
                    tool_output="Edited x.py", is_error=False),
        EngineEvent(type="tool_result", tool_name="edit_file", tool_input={"path": "x.py"},
                    tool_output="Edited x.py", is_error=False),
    ]
    files = extract_edited_files_from_events(events)
    assert files == ["x.py"]
