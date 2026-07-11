"""GlobTool — find files matching a glob pattern.

Mirrors Claude Code's GlobTool: read-only, returns matching paths sorted by
modification time (most recent first), so the model sees the files most
likely to be relevant to an in-progress task first.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult

MAX_RESULTS = 200
DEFAULT_EXCLUDES = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".cache"}


class GlobInput(BaseModel):
    pattern: str = Field(..., description="Glob pattern, e.g. '**/*.py', 'src/**/*.ts'")
    path: str | None = Field(None, description="Directory to search from (defaults to cwd)")


class GlobTool(BaseTool):
    name = "glob"
    description = (
        "Find files matching a glob pattern (e.g. '**/*.py'). Returns matching paths "
        "sorted by modification time, most recent first. Respects common ignore "
        "directories (.git, node_modules, __pycache__, venv, dist, build)."
    )
    input_schema = GlobInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return False

    @staticmethod
    def is_read_only() -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = GlobInput.model_validate(input_data)
        base = Path(parsed.path) if parsed.path else Path(cwd)
        if not base.is_absolute():
            base = Path(cwd) / base

        if not base.exists():
            return ToolResult(output=f"Directory not found: {base}", is_error=True)
        if not base.is_dir():
            return ToolResult(output=f"Not a directory: {base}", is_error=True)

        try:
            matches = list(base.glob(parsed.pattern))
        except (ValueError, NotImplementedError) as exc:
            return ToolResult(output=f"Invalid glob pattern {parsed.pattern!r}: {exc}", is_error=True)

        files = [
            m for m in matches
            if m.is_file() and not any(part in DEFAULT_EXCLUDES for part in m.parts)
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        truncated = len(files) > MAX_RESULTS
        files = files[:MAX_RESULTS]

        if not files:
            return ToolResult(output=f"No files matched pattern {parsed.pattern!r} in {base}")

        lines = [str(f) for f in files]
        if truncated:
            lines.append(f"... (truncated to {MAX_RESULTS} most recent results)")

        return ToolResult(output="\n".join(lines))
