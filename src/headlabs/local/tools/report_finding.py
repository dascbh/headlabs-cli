"""ReportFindingTool — the inspector records one structured finding.

Used only by ``headlabs local inspect`` (deliberately NOT part of
``ALL_TOOLS`` used by ``run``/``chat``). Each call validates a ``Finding``
against its pydantic schema (the engine does this automatically at
``engine.py:validate_input``) and appends it to
``.headlabs/local_backlog.json`` via ``backlog.py``, so findings survive the
run and feed ``headlabs local backlog`` / ``headlabs local fix``.

Making the model emit findings through a validated tool call is far more
reliable with small (8B) models than asking it to print a well-formed JSON
blob at the end — the schema is enforced per call and the model gets a clear
success/error signal each time.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from headlabs.local import backlog
from headlabs.local.tools.base import BaseTool, ToolResult


class FindingInput(BaseModel):
    severity: str = Field(..., description="One of: critical, high, medium, low")
    title: str = Field(..., description="Short one-line summary of the issue")
    detail: str = Field(..., description="What is wrong and why it matters, citing evidence")
    fix: str = Field("", description="Concrete suggested fix")
    file: str = Field("", description="Relevant file path (relative to the project), if localized")
    line: int | None = Field(None, description="Relevant line number, if localized")
    role: str = Field("qa", description="Inspector role this finding belongs to")


class ReportFindingTool(BaseTool):
    name = "report_finding"
    description = (
        "Record ONE inspection finding. Call this once per distinct issue you find. "
        "Provide severity (critical/high/medium/low), a short title, a detailed "
        "explanation citing evidence, and a concrete suggested fix. Include file and "
        "line when the issue is localized to a specific place."
    )
    input_schema = FindingInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        # Local bookkeeping only (writes a project-local JSON file), like
        # todo_write — no approval prompt.
        return False

    @staticmethod
    def is_read_only() -> bool:
        # No effect on the system under inspection; it only records a finding.
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = FindingInput.model_validate(input_data)
        item = backlog.add_finding(
            cwd,
            role=parsed.role,
            severity=parsed.severity,
            title=parsed.title,
            detail=parsed.detail,
            fix=parsed.fix,
            file=parsed.file,
            line=parsed.line,
        )
        return ToolResult(output=f"Recorded [{item['severity']}] {item['title']}")
