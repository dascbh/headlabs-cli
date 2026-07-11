"""AskUserQuestionTool — pause execution and ask the user a question.

Mirrors Claude Code's AskUserQuestionTool: lets the model request clarification
mid-task instead of guessing. Reuses stdin/stdout directly (same as
permission.py's default_terminal_prompt) rather than routing through the
engine's on_event callback, since this needs to block for a free-text answer,
not just render a status update.

In --yes / non-interactive runs this tool is disabled (see requires_permission
below is not the mechanism -- see engine wiring) since there is no user to ask;
callers should treat a missing question as a signal the model should proceed
with its best judgement instead of stalling a headless run.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult


class AskUserQuestionInput(BaseModel):
    question: str = Field(..., description="The question to ask the user")


class AskUserQuestionTool(BaseTool):
    name = "ask_user_question"
    description = (
        "Ask the user a clarifying question and wait for their free-text answer. "
        "Use this when you are missing information needed to proceed correctly, "
        "instead of guessing."
    )
    input_schema = AskUserQuestionInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return False  # asking a question has no side effect on the user's system

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = AskUserQuestionInput.model_validate(input_data)
        print(f"\n\033[33m? {parsed.question}\033[0m")
        try:
            answer = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return ToolResult(
                output="User did not answer (input closed). Proceed with your best judgement.",
                is_error=False,
            )
        if not answer:
            return ToolResult(output="User gave an empty answer. Proceed with your best judgement.")
        return ToolResult(output=answer)
