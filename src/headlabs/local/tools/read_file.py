"""ReadFileTool — read a text file's contents, optionally a line range."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult

MAX_READ_BYTES = 512_000


class ReadFileInput(BaseModel):
    path: str = Field(..., description="Path to the file, absolute or relative to cwd")
    start_line: int | None = Field(None, description="1-based first line to include")
    end_line: int | None = Field(None, description="1-based last line to include (inclusive)")


class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "Read the contents of a text file. Optionally restrict to a line range "
        "via start_line/end_line (both 1-based, inclusive)."
    )
    input_schema = ReadFileInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return False  # read-only, safe by construction

    @staticmethod
    def is_read_only() -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = ReadFileInput.model_validate(input_data)
        target = Path(parsed.path)
        if not target.is_absolute():
            target = Path(cwd) / target

        if not target.exists():
            return ToolResult(output=f"File not found: {target}", is_error=True)
        if target.is_dir():
            return ToolResult(output=f"Path is a directory, not a file: {target}", is_error=True)

        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            return ToolResult(
                output=(
                    f"File too large ({size} bytes > {MAX_READ_BYTES} limit): {target}. "
                    "Use start_line/end_line to read a slice."
                ),
                is_error=True,
            )

        try:
            text = target.read_text(errors="replace")
        except OSError as exc:
            return ToolResult(output=f"Could not read {target}: {exc}", is_error=True)

        lines = text.splitlines()
        start = (parsed.start_line or 1) - 1
        end = parsed.end_line if parsed.end_line is not None else len(lines)
        start = max(0, start)
        end = min(len(lines), end)
        selected = lines[start:end]

        numbered = "\n".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(selected))
        return ToolResult(output=numbered or "(empty file or empty range)")
