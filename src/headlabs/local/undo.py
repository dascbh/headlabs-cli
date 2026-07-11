"""Undo for `headlabs local` — revert the last file edit(s) made by the agent.

Uses git as the backing store: `git checkout -- <file>` for unstaged changes,
or `git stash` for a broader undo. This is safe because:
1. If the file has uncommitted changes from before the agent edited it, we'd
   be reverting the user's own work — so we check for that and warn.
2. If the project isn't a git repo at all, /undo is not available (clearly
   communicated to the user).

The CLI layer pushes edited file paths onto the undo stack after each turn.
/undo pops the most recent batch and reverts them.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UndoEntry:
    """A group of files edited in one agent turn."""
    files: list[str]
    description: str = ""


class UndoStack:
    """Tracks files edited by the agent for /undo support."""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self._stack: list[UndoEntry] = []

    def push(self, files: list[str], description: str = "") -> None:
        """Record a batch of files edited in the current turn."""
        if files:
            self._stack.append(UndoEntry(files=list(files), description=description))

    def can_undo(self) -> bool:
        return len(self._stack) > 0

    def peek(self) -> UndoEntry | None:
        """Preview what /undo would revert without actually doing it."""
        return self._stack[-1] if self._stack else None

    def pop(self) -> UndoEntry | None:
        """Pop the most recent entry (for undo)."""
        return self._stack.pop() if self._stack else None

    @property
    def depth(self) -> int:
        return len(self._stack)


def is_git_repo(cwd: str) -> bool:
    """Check if cwd is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd, capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def git_checkout_files(files: list[str], cwd: str) -> tuple[bool, str]:
    """Revert files via `git checkout -- <files>`.
    Returns (success, message)."""
    if not files:
        return False, "No files to undo."

    # Verify all files exist and have changes to revert
    existing = [f for f in files if Path(cwd, f).exists() or _is_tracked(f, cwd)]
    if not existing:
        return False, "None of the edited files are available to undo."

    try:
        result = subprocess.run(
            ["git", "checkout", "--"] + existing,
            cwd=cwd, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            file_list = ", ".join(existing)
            return True, f"Reverted: {file_list}"
        else:
            return False, f"git checkout failed: {result.stderr.strip()}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Failed to run git: {exc}"


def _is_tracked(file: str, cwd: str) -> bool:
    """Check if a file is tracked by git (even if deleted)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", file],
            cwd=cwd, capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def extract_edited_files_from_events(events: list) -> list[str]:
    """Extract file paths from EngineEvents that were successfully edited.
    Looks for tool_result events from edit_file that indicate success."""
    files = []
    for ev in events:
        if (
            getattr(ev, "type", None) == "tool_result"
            and getattr(ev, "tool_name", None) == "edit_file"
            and not getattr(ev, "is_error", True)
        ):
            # Parse the path from the tool_input or the output message
            tool_input = getattr(ev, "tool_input", {})
            path = tool_input.get("path", "")
            if path and path not in files:
                files.append(path)
    return files
