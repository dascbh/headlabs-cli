"""GrepTool — search file contents by regex.

Mirrors Claude Code's GrepTool. Deliberately implemented with Python's `re`
module walking the filesystem, not a shelled-out `ripgrep`/`grep` binary:
`rg` is not guaranteed to be installed on the user's machine (confirmed
absent in this project's dev environment), and shelling out to `grep` would
tie behavior to whatever grep variant happens to be on PATH (BSD vs GNU grep
have different regex flavors). Pure Python is slower on very large trees but
portable and predictable everywhere headlabs-cli already runs.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult

MAX_FILES_SCANNED = 5000
MAX_MATCHES = 200
MAX_FILE_SIZE_BYTES = 2_000_000  # skip huge files (likely binaries/data, not source)
DEFAULT_EXCLUDES = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".cache"}


class GrepInput(BaseModel):
    pattern: str = Field(..., description="Regex pattern to search for")
    path: str | None = Field(None, description="Directory to search from (defaults to cwd)")
    glob: str | None = Field(None, description="Only search files matching this glob, e.g. '*.py'")
    case_sensitive: bool = Field(False, description="Case-sensitive match")


def _is_probably_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(1024)
        return b"\x00" in chunk
    except OSError:
        return True


class GrepTool(BaseTool):
    name = "grep"
    description = (
        "Search file contents for a regex pattern across a directory tree. "
        "Returns matching file:line:content, respecting common ignore directories."
    )
    input_schema = GrepInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return False

    @staticmethod
    def is_read_only() -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = GrepInput.model_validate(input_data)
        base = Path(parsed.path) if parsed.path else Path(cwd)
        if not base.is_absolute():
            base = Path(cwd) / base

        if not base.exists():
            return ToolResult(output=f"Directory not found: {base}", is_error=True)

        try:
            flags = 0 if parsed.case_sensitive else re.IGNORECASE
            regex = re.compile(parsed.pattern, flags)
        except re.error as exc:
            return ToolResult(output=f"Invalid regex {parsed.pattern!r}: {exc}", is_error=True)

        file_iter = base.rglob(parsed.glob) if parsed.glob else base.rglob("*")

        matches: list[str] = []
        scanned = 0
        truncated = False

        for path in file_iter:
            if not path.is_file():
                continue
            if any(part in DEFAULT_EXCLUDES for part in path.parts):
                continue

            scanned += 1
            if scanned > MAX_FILES_SCANNED:
                truncated = True
                break

            try:
                if path.stat().st_size > MAX_FILE_SIZE_BYTES or _is_probably_binary(path):
                    continue
                text = path.read_text(errors="ignore")
            except OSError:
                continue

            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{path}:{lineno}:{line.strip()[:300]}")
                    if len(matches) >= MAX_MATCHES:
                        truncated = True
                        break
            if truncated:
                break

        if not matches:
            return ToolResult(output=f"No matches found for pattern {parsed.pattern!r} in {base}")

        output = "\n".join(matches)
        if truncated:
            output += f"\n... (truncated to {MAX_MATCHES} matches / {MAX_FILES_SCANNED} files scanned)"

        return ToolResult(output=output)
