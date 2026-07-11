"""ConfigTool — read or modify `headlabs local` configuration.

Mirrors Claude Code's ConfigTool. Deliberately scoped to a small, explicit
allowlist of fields (max_iterations, timeout_s) rather than arbitrary
key-value writes: base_url/model/api_key changes take effect for the *next*
run (the provider is already constructed for the current one), and letting
the model silently repoint itself at a different LLM endpoint mid-session is
a bigger blast radius than this tool should have by default.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from headlabs.local.config import load_local_config, update_local_config
from headlabs.local.tools.base import BaseTool, ToolResult

WRITABLE_FIELDS = {"max_iterations", "timeout_s"}


class ConfigInput(BaseModel):
    action: str = Field(..., description="One of: 'get' (read current config) or 'set' (update a field)")
    key: str | None = Field(None, description="Field to set (required for action='set')")
    value: str | None = Field(None, description="New value (required for action='set')")


class ConfigTool(BaseTool):
    name = "config"
    description = (
        "Get or set `headlabs local` runtime configuration. "
        f"Writable fields: {sorted(WRITABLE_FIELDS)}. "
        "base_url/model/api_key are read-only via this tool -- use `headlabs local config` "
        "from the shell to change the LLM endpoint."
    )
    input_schema = ConfigInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return input_data.get("action") == "set"

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = ConfigInput.model_validate(input_data)

        if parsed.action == "get":
            cfg = load_local_config()
            return ToolResult(
                output=(
                    f"base_url: {cfg.base_url}\n"
                    f"model: {cfg.model}\n"
                    f"api_key: {'(set)' if cfg.api_key else '(none)'}\n"
                    f"max_iterations: {cfg.max_iterations}\n"
                    f"timeout_s: {cfg.timeout_s}"
                )
            )

        if parsed.action == "set":
            if not parsed.key or parsed.value is None:
                return ToolResult(output="action='set' requires both 'key' and 'value'", is_error=True)
            if parsed.key not in WRITABLE_FIELDS:
                return ToolResult(
                    output=f"Field {parsed.key!r} is not writable via this tool. Writable: {sorted(WRITABLE_FIELDS)}",
                    is_error=True,
                )
            try:
                typed_value = int(parsed.value) if parsed.key == "max_iterations" else float(parsed.value)
            except ValueError:
                return ToolResult(output=f"Invalid numeric value for {parsed.key!r}: {parsed.value!r}", is_error=True)

            update_local_config(**{parsed.key: typed_value})
            return ToolResult(output=f"Set {parsed.key} = {typed_value}")

        return ToolResult(output=f"Unknown action {parsed.action!r}; expected 'get' or 'set'", is_error=True)
