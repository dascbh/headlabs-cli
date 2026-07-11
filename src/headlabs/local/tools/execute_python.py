"""ExecutePythonTool — run Python code in an isolated subprocess.

Distinct from `bash` (which can already run `python3 script.py`, but mixes
"run a system command" with "execute code" conceptually, and has no
code-specific handling of tracebacks/timeouts). This tool:

- Always requires permission (arbitrary code execution).
- Runs in a fresh subprocess via `sys.executable -c <code>` -- never
  `exec()`/`eval()` in-process, so a crash, infinite loop, or hostile code
  cannot corrupt headlabs-cli's own process state or access its internals
  by accident (e.g. reading other tools' in-memory state).
- Captures stdout, stderr, and exit code separately, and preserves full
  Python tracebacks (the same as running the script directly in a
  terminal), unlike shelling through `bash -c "python3 ..."` where quoting
  can mangle multi-line code.
- Has its own timeout, independent of BashTool's, since code execution and
  arbitrary shell commands have different expected durations.
"""
from __future__ import annotations

import subprocess
import sys

from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult

DEFAULT_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 20_000


class ExecutePythonInput(BaseModel):
    code: str = Field(..., description="Python code to execute")
    timeout_s: int = Field(DEFAULT_TIMEOUT_S, description="Kill execution after this many seconds")


class ExecutePythonTool(BaseTool):
    name = "execute_python"
    description = (
        "Execute Python code in an isolated subprocess and return its stdout, stderr, "
        "and exit code. Use this for computation, data processing, or testing snippets -- "
        "not for running other programs (use bash for that)."
    )
    input_schema = ExecutePythonInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = ExecutePythonInput.model_validate(input_data)
        try:
            proc = subprocess.run(
                [sys.executable, "-c", parsed.code],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=parsed.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                output=f"Execution timed out after {parsed.timeout_s}s",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(output=f"Failed to start Python subprocess: {exc}", is_error=True)

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
