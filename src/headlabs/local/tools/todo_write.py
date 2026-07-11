"""TodoWriteTool — write a structured task list, persisted per project.

Mirrors Claude Code's TodoWriteTool: lets the model track its own multi-step
plan explicitly instead of only holding it implicitly in the conversation.
Persisted to ``.headlabs/local_todos.json`` under the working directory --
same scoping convention as permission.py's per-project rules -- so a plan
started in one project doesn't leak into another, and survives across
`headlabs local run` invocations within the same project.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult

TODOS_SUBPATH = Path(".headlabs") / "local_todos.json"
VALID_STATUSES = {"pending", "in_progress", "completed"}


class TodoItem(BaseModel):
    content: str = Field(..., description="Task description")
    status: str = Field("pending", description="One of: pending, in_progress, completed")


class TodoWriteInput(BaseModel):
    todos: list[TodoItem] = Field(..., description="The full task list (replaces any previous list)")


def _todos_path(cwd: str) -> Path:
    return Path(cwd) / TODOS_SUBPATH


class TodoWriteTool(BaseTool):
    name = "todo_write"
    description = (
        "Write the current task list, replacing any previous one. Each item has "
        "'content' and 'status' (pending, in_progress, or completed). Use this to "
        "track progress on multi-step tasks -- write the full list each time, not "
        "just the changed items."
    )
    input_schema = TodoWriteInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return False  # local bookkeeping file, not a destructive action

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = TodoWriteInput.model_validate(input_data)

        invalid = [t.status for t in parsed.todos if t.status not in VALID_STATUSES]
        if invalid:
            return ToolResult(
                output=(
                    f"Invalid status value(s) {invalid}; must be one of {sorted(VALID_STATUSES)}"
                ),
                is_error=True,
            )

        path = _todos_path(cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [t.model_dump() for t in parsed.todos]
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

        if not payload:
            return ToolResult(output="Task list cleared.")

        lines = []
        for item in payload:
            marker = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} {item['content']}")
        return ToolResult(output="Task list updated:\n" + "\n".join(lines))
