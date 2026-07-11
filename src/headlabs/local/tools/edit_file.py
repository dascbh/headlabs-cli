"""EditFileTool — search/replace based file editing.

The model supplies an exact ``old_text`` snippet and its ``new_text``
replacement. This mirrors Aider/Cline's SEARCH/REPLACE approach rather than
unified diffs: it is far more forgiving of models that don't emit perfectly
formatted diffs, at the cost of requiring ``old_text`` to match exactly
(including whitespace). On mismatch, the tool returns a detailed error
message back to the model (nearest-match hint) instead of silently failing,
so the model can self-correct on the next turn.
"""
from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult


class EditFileInput(BaseModel):
    path: str = Field(..., description="Path to the file, absolute or relative to cwd")
    old_text: str = Field(..., description="Exact text to find (must match exactly once)")
    new_text: str = Field(..., description="Text to replace it with")


def _closest_match_hint(content: str, needle: str) -> str:
    """Best-effort hint when old_text isn't found verbatim: locate the most
    similar block of the same line-count in the file, to help the model see
    what's actually there (trailing whitespace, indentation drift, etc.)."""
    needle_lines = needle.splitlines()
    if not needle_lines:
        return ""
    content_lines = content.splitlines()
    n = len(needle_lines)
    best_ratio, best_block = 0.0, ""
    for i in range(0, max(1, len(content_lines) - n + 1)):
        block = "\n".join(content_lines[i : i + n])
        ratio = difflib.SequenceMatcher(None, block, needle).ratio()
        if ratio > best_ratio:
            best_ratio, best_block = ratio, block
    if best_ratio > 0.5:
        return f"\n\nClosest match found in file (similarity {best_ratio:.0%}):\n{best_block}"
    return ""


class EditFileTool(BaseTool):
    name = "edit_file"
    description = (
        "Edit a file by replacing an exact block of text (old_text) with new "
        "text (new_text). old_text must match exactly once in the file, "
        "including whitespace/indentation. Use read_file first to see exact content."
    )
    input_schema = EditFileInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = EditFileInput.model_validate(input_data)
        target = Path(parsed.path)
        if not target.is_absolute():
            target = Path(cwd) / target

        if not target.exists():
            return ToolResult(output=f"File not found: {target}", is_error=True)

        try:
            content = target.read_text()
        except OSError as exc:
            return ToolResult(output=f"Could not read {target}: {exc}", is_error=True)

        count = content.count(parsed.old_text)
        if count == 0:
            hint = _closest_match_hint(content, parsed.old_text)
            return ToolResult(
                output=(
                    f"old_text not found in {target}. It must match exactly "
                    f"(including whitespace/indentation)." + hint
                ),
                is_error=True,
            )
        if count > 1:
            return ToolResult(
                output=(
                    f"old_text matches {count} locations in {target}; it must be "
                    "unique. Include more surrounding context to disambiguate."
                ),
                is_error=True,
            )

        new_content = content.replace(parsed.old_text, parsed.new_text, 1)
        try:
            target.write_text(new_content)
        except OSError as exc:
            return ToolResult(output=f"Could not write {target}: {exc}", is_error=True)

        added = parsed.new_text.count("\n") + 1
        removed = parsed.old_text.count("\n") + 1
        return ToolResult(output=f"Edited {target} (-{removed} +{added} lines)")
