"""Base tool contract for the `headlabs local` agent runtime.

Each tool is a self-contained module: input schema (pydantic), permission
requirement, and execution logic together — the same coupling pattern used by
Claude Code, Cline and OpenCode, chosen because it keeps everything about
"what a tool does and how it's gated" in one place instead of spread across
a registry and a separate permission table.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel


@dataclass
class ToolResult:
    """Outcome of executing a tool call.

    ``completes_run``, inspired by Cline's explicit termination signal, lets a
    tool declare "the task is done" instead of the engine having to infer it
    from the model simply not calling any more tools — relevant if this loop
    is ever driven non-interactively.
    """
    output: str
    is_error: bool = False
    completes_run: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTool(ABC):
    """Subclass and set ``name``, ``description``, ``input_schema``."""

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[type[BaseModel]]

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        """Whether this specific invocation needs user approval.

        Default: everything requires approval. Read-only tools override this
        to return False; destructive tools (bash, edit) can inspect
        ``input_data`` to special-case obviously-safe invocations later, but
        default to "always ask" until a permission rule says otherwise.
        """
        return True

    @staticmethod
    def is_read_only() -> bool:
        return False

    @abstractmethod
    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        """Run the tool. ``input_data`` is pre-validated against ``input_schema``."""
        raise NotImplementedError

    @classmethod
    def to_api_schema(cls) -> dict:
        """JSON-schema-like description sent to the provider as a tool definition."""
        return {
            "name": cls.name,
            "description": cls.description,
            "input_schema": cls.input_schema.model_json_schema(),
        }

    @classmethod
    def validate_input(cls, raw_input: dict) -> BaseModel:
        return cls.input_schema.model_validate(raw_input)
