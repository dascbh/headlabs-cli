"""BashTool — execute a shell command and capture its output.

Always requires permission: this is the one tool capable of arbitrary side
effects, so it never gets a read-only fast path like ReadFileTool.
"""
from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult

DEFAULT_TIMEOUT_S = 120
MAX_OUTPUT_CHARS = 20_000


class BashInput(BaseModel):
    command: str = Field(..., description="Shell command to execute")
    timeout_s: int = Field(DEFAULT_TIMEOUT_S, description="Kill the command after this many seconds")


class BashTool(BaseTool):
    name = "bash"
    description = "Execute a shell command in the current working directory and return stdout/stderr."
    input_schema = BashInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = BashInput.model_validate(input_data)
        try:
            proc = subprocess.run(
                parsed.command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=parsed.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                output=f"Command timed out after {parsed.timeout_s}s: {parsed.command}",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(output=f"Failed to execute command: {exc}", is_error=True)

        stdout = proc.stdout[:MAX_OUTPUT_CHARS]
        stderr = proc.stderr[:MAX_OUTPUT_CHARS]
        parts = []
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        parts.append(f"exit_code: {proc.returncode}")
        output = "\n\n".join(parts)

        return ToolResult(output=output, is_error=proc.returncode != 0)
