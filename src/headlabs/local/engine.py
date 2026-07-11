"""Query Engine — the tool-call loop for `headlabs local`.

    while iterations < max_iterations:
        response = provider.stream(history, tool_schemas)
        if no tool_calls: break
        for each tool_call:
            permission.check(...)          # may raise PermissionDenied
            result = tool.execute(...)
            history.append(tool_result)
        if any result.completes_run: break

Kept intentionally simple and explicit — this is the "Query Engine" concept
from the comparative study (Claude Code / Cline / OpenCode all center on this
loop), sized down to what a single-file MVP needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from headlabs.local.permission import PermissionDenied, PermissionManager
from headlabs.local.provider import ChatEvent, OpenAICompatibleProvider, ProviderError, ToolCall
from headlabs.local.tools.base import BaseTool, ToolResult

SYSTEM_PROMPT = """\
You are a coding agent in a terminal. You help users with software engineering tasks.

# Rules
- Be concise. Keep text between tool calls to 1-2 sentences MAX.
- Go straight to the point. Try the simplest approach first.
- NEVER explain what you're about to do. Just do it.
- NEVER show JSON of tool calls as text. Call the tool directly.
- When done, state ONLY the outcome in 1 sentence. Example: "Created `file.html`." or "Fixed the bug in `utils.py`."
- Do NOT summarize what the file contains, what columns a table has, or what features were added. The user can look at the file themselves.
- Do NOT add "you can open it in a browser" or "you can update it later" — that's obvious.
- Match response length to task complexity. Simple question = short answer.

# Tool routing
- read_file: read a specific file you know the path to
- glob: find files by name pattern (e.g. "**/*.py", "src/**/config*")
- grep: find files containing a regex pattern in their content
- edit_file: modify existing files (MUST read_file first to see exact content)
- bash: shell commands, create new files (heredoc), run tests, git ops
- execute_python: calculations, data processing, testing snippets
- web_search: current events, recent data, anything beyond your training data
- web_fetch: retrieve and read content from a specific URL
- browser_devtools: navigate, screenshot, inspect, or automate a web page
- todo_write: plan and track multi-step tasks
- ask_user_question: when you need clarification

# Parallel tool calls
- Call multiple independent tools in a single response for speed.
- Only sequence calls that depend on the result of a previous call.

# When to use web_search
- Current events, news, or data from 2025+
- Products, services, or APIs you're not certain about
- Anything that changes over time — ALWAYS search first, never guess

# When to use todo_write
- ANY task with 3+ steps: todo list BEFORE starting work
- Multiple tasks in one message: capture ALL as todos first
- Mark in_progress BEFORE starting, completed IMMEDIATELY after finishing
- Only 1 task in_progress at a time

# Task execution flow
1. If task has 3+ steps -> todo_write (plan)
2. Read/search to understand (parallel when possible)
3. Implement
4. VERIFY: run tests, execute script, check output before claiming done
5. Report result concisely and honestly

# Creating and editing files (CRITICAL)
- When user says "create/make/build/generate" a page, script, or file: your FIRST action must be calling bash to save the file. Do NOT render the content as text in chat first.
- New files: bash with heredoc (cat > file << 'EOF' ... EOF)
- Editing: edit_file (MUST read_file first — never propose changes to code you haven't read)
- PREFER editing existing files. Do NOT create new files unless truly necessary.
- Choose a sensible filename if the user didn't specify one.
- The file IS the deliverable. The chat response is just a 1-sentence confirmation.

# Error handling
- If a tool fails, DIAGNOSE why before switching approach.
- After diagnosis, try a different method IMMEDIATELY. Do not narrate.
- After 2 failed attempts at same approach, switch to fundamentally different method.

# Risk assessment
- Before destructive operations (rm -rf, git reset --hard, drop table, force-push): state what it does and ask for confirmation.
- Reversible local changes (editing files, running tests): proceed without asking.

# Code style
- Follow existing conventions. Check neighboring files before writing new code.
- Do NOT add comments, features, or abstractions beyond what was requested.
- Never assume a library is available — check package.json/pyproject.toml first.
- Report outcomes faithfully. Never claim "tests pass" if output shows failures.

# Verification (CRITICAL)
- After implementing, ALWAYS verify: run the test, execute the script, check the build.
- If tests fail, say so and fix — never claim success when output shows failure.

# Anti-hallucination
- Never describe code you haven't read. Read it first.
- Never state counts without computing them (bash wc -l or execute_python).
- Read EACH file separately when analyzing multiple files.
- If a tool fails, report honestly. Never fabricate success.
"""

# Safety net independent of --max-iterations: never loop forever even if
# misconfigured, regardless of user-supplied config.
HARD_ITERATION_CEILING = 200


@dataclass
class EngineEvent:
    """Emitted to the caller (CLI renderer) as the engine runs."""
    type: str  # "text", "tool_call", "tool_result", "permission_denied", "error", "done"
    text: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_output: str = ""
    is_error: bool = False


_REPETITION_THRESHOLD = 4  # same sentence repeated this many times = degenerate
_MIN_REPEATED_CHUNK_LEN = 40  # ignore very short repeated chunks (e.g. "\n\n")


def _has_degenerate_repetition(text: str) -> bool:
    """Detect if the text contains the same phrase repeated N+ times consecutively.
    Uses a simple substring counting approach: sample a chunk from the middle of the
    text and check how many times it appears overall. If the ratio is high, it's
    degenerate repetition."""
    if len(text) < _MIN_REPEATED_CHUNK_LEN * _REPETITION_THRESHOLD:
        return False

    # Try different chunk sizes, from larger (more specific) to smaller
    for chunk_len in (150, 100, 60, 40):
        if len(text) < chunk_len * _REPETITION_THRESHOLD:
            continue
        # Sample a chunk from 40% into the text (where repetition typically establishes)
        sample_start = len(text) * 2 // 5
        candidate = text[sample_start:sample_start + chunk_len].strip()
        if len(candidate) < _MIN_REPEATED_CHUNK_LEN:
            continue
        # Count occurrences of this exact substring
        count = text.count(candidate)
        if count >= _REPETITION_THRESHOLD:
            return True
    return False


def _truncate_at_repetition(text: str) -> str:
    """Truncate text to keep only up to the second occurrence of the repeating chunk."""
    for chunk_len in (150, 100, 60, 40):
        if len(text) < chunk_len * 2:
            continue
        sample_start = len(text) * 2 // 5
        candidate = text[sample_start:sample_start + chunk_len].strip()
        if len(candidate) < _MIN_REPEATED_CHUNK_LEN:
            continue
        count = text.count(candidate)
        if count >= _REPETITION_THRESHOLD:
            # Find the second occurrence and cut there
            first_idx = text.find(candidate)
            if first_idx >= 0:
                second_idx = text.find(candidate, first_idx + len(candidate))
                if second_idx >= 0:
                    return text[:second_idx].rstrip() + "\n\n[... geração interrompida por repetição]"
    # Fallback: return the first third
    return text[:len(text) // 3].rstrip() + "\n\n[... geração interrompida por repetição]"


class QueryEngine:
    def __init__(
        self,
        provider: OpenAICompatibleProvider,
        tools: list[type[BaseTool]],
        permission_manager: PermissionManager,
        *,
        cwd: str,
        max_iterations: int = 30,
        system_prompt: str = SYSTEM_PROMPT,
    ):
        self.provider = provider
        self.tools_by_name: dict[str, type[BaseTool]] = {t.name: t for t in tools}
        self.permission_manager = permission_manager
        self.cwd = cwd
        self.max_iterations = min(max_iterations, HARD_ITERATION_CEILING)
        self.system_prompt = system_prompt

        # Project rules: if .headlabs/rules.md exists in cwd, append to system prompt.
        # This is the equivalent of Claude Code's CLAUDE.md — project-specific rules
        # that override/extend the base system prompt without editing engine code.
        full_prompt = system_prompt
        rules_path = Path(cwd) / ".headlabs" / "rules.md"
        if rules_path.exists():
            try:
                rules_content = rules_path.read_text().strip()
                if rules_content:
                    full_prompt += f"\n\n# Project rules (from .headlabs/rules.md)\n{rules_content}"
            except OSError:
                pass  # silently skip if unreadable

        self.history: list[dict] = [{"role": "system", "content": full_prompt}]

    def _tool_schemas(self) -> list[dict]:
        return [t.to_api_schema() for t in self.tools_by_name.values()]

    def _run_tool_call(self, call: ToolCall) -> EngineEvent:
        tool_cls = self.tools_by_name.get(call.name)
        if tool_cls is None:
            return EngineEvent(
                type="tool_result",
                tool_name=call.name,
                tool_output=f"Unknown tool: {call.name}",
                is_error=True,
            )

        try:
            raw_input = call.parsed_arguments()
        except ProviderError as exc:
            return EngineEvent(
                type="tool_result", tool_name=call.name, tool_output=str(exc), is_error=True
            )

        try:
            validated = tool_cls.validate_input(raw_input)
        except Exception as exc:  # pydantic.ValidationError et al.
            return EngineEvent(
                type="tool_result",
                tool_name=call.name,
                tool_output=f"Invalid arguments for {call.name}: {exc}",
                is_error=True,
            )

        input_dict = validated.model_dump()
        needs_permission = tool_cls.requires_permission(input_dict)
        try:
            self.permission_manager.check(call.name, input_dict, needs_permission=needs_permission)
        except PermissionDenied as exc:
            return EngineEvent(
                type="permission_denied", tool_name=call.name, tool_output=str(exc), is_error=True
            )

        tool = tool_cls()
        result: ToolResult = tool.execute(input_dict, cwd=self.cwd)
        return EngineEvent(
            type="tool_result",
            tool_name=call.name,
            tool_input=input_dict,
            tool_output=result.output,
            is_error=result.is_error,
        )

    def run(self, user_message: str, on_event: Callable[[EngineEvent], None] | None = None) -> str:
        """Run the loop until the model stops calling tools. Returns the final
        text response. ``on_event`` is called for every intermediate event
        (streamed text, tool calls, tool results) so the CLI can render
        progress live."""
        emit = on_event or (lambda _e: None)
        self.history.append({"role": "user", "content": user_message})

        final_text_parts: list[str] = []
        # Bounded retries for the "stopped with no tools and no text" failure mode --
        # see the empty_response_retries_left check below. Small and fixed on purpose:
        # this is a safety net for a rare stall, not a general retry-until-it-works loop.
        empty_response_retries_left = 2
        consecutive_errors: list[str] = []  # track consecutive error outputs for loop detection
        ERROR_LOOP_THRESHOLD = 3

        for _ in range(self.max_iterations):
            text_buf: list[str] = []
            tool_calls: list[ToolCall] = []

            try:
                for event in self.provider.stream(self.history, self._tool_schemas()):
                    if event.type == "text_delta":
                        text_buf.append(event.text)
                        emit(EngineEvent(type="text", text=event.text))
                        # Real-time repetition detection: if the accumulated text
                        # contains the same sentence/phrase repeated 4+ times
                        # consecutively, stop generation early. This is a confirmed
                        # failure mode of the 8B model (degenerative repetition in
                        # streaming) that floods the terminal with identical text.
                        if len(text_buf) > 100 and len(text_buf) % 20 == 0:
                            full = "".join(text_buf)
                            if _has_degenerate_repetition(full):
                                emit(EngineEvent(
                                    type="error",
                                    text="Repetição degenerativa detectada — geração interrompida.",
                                    is_error=True,
                                ))
                                # Truncate to just the first occurrence
                                text_buf = [_truncate_at_repetition(full)]
                                break
                    elif event.type == "tool_calls":
                        tool_calls = event.tool_calls
            except ProviderError as exc:
                emit(EngineEvent(type="error", text=str(exc), is_error=True))
                raise

            assistant_text = "".join(text_buf)
            # Some servers (confirmed: Ollama, via a real HTTP 400 "invalid
            # message content type: <nil>") reject `content: null` even
            # though it's valid per the OpenAI schema when tool_calls is
            # present. Always send a string -- empty string, never None --
            # to stay compatible with the strictest server implementations.
            assistant_msg: dict = {"role": "assistant", "content": assistant_text}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in tool_calls
                ]
            self.history.append(assistant_msg)

            if not tool_calls:
                if assistant_text:
                    final_text_parts.append(assistant_text)
                    emit(EngineEvent(type="done", text=assistant_text))
                    return "".join(final_text_parts) or assistant_text

                # Real, reproducible failure mode observed with an 8B model: after several
                # successful tool calls, the model sometimes stops calling tools AND produces
                # no text at all (confirmed deterministic across repeated runs with
                # temperature=0 on the same prompt). Silently returning "" is worse than
                # retrying once with an explicit nudge -- give it one bounded chance to
                # synthesize an answer from what it already has before giving up for real.
                if empty_response_retries_left > 0:
                    empty_response_retries_left -= 1
                    self.history.append({
                        "role": "user",
                        "content": (
                            "You stopped without producing any text. Based on the tool "
                            "results already in this conversation, write your answer now."
                        ),
                    })
                    continue

                emit(EngineEvent(
                    type="error",
                    text="Model stopped without calling tools or producing text, even after a retry nudge",
                    is_error=True,
                ))
                return "".join(final_text_parts)

            if assistant_text:
                final_text_parts.append(assistant_text)

            denied_any = False
            for call in tool_calls:
                try:
                    _tool_input = call.parsed_arguments()
                except Exception:
                    _tool_input = {}
                emit(EngineEvent(type="tool_call", tool_name=call.name, tool_input=_tool_input))
                result_event = self._run_tool_call(call)
                emit(result_event)

                if result_event.type == "permission_denied":
                    denied_any = True
                    tool_message_content = f"Permission denied: {result_event.tool_output}"
                else:
                    tool_message_content = result_event.tool_output

                self.history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": tool_message_content,
                    }
                )

            if denied_any:
                # Let the model see the denial and decide how to proceed on the
                # next turn rather than aborting the whole run outright.
                continue

            # ── Error-loop detection: if tool calls keep failing with similar errors,
            # inject a correction message to break the model out of the stuck pattern.
            # Check if this turn's tool results contain errors.
            last_tool_msgs = [
                m for m in self.history[-len(tool_calls) * 2:]
                if m.get("role") == "tool"
            ]
            turn_has_errors = any(
                "not found" in m.get("content", "").lower()
                or "error" in m.get("content", "")[:80].lower()
                for m in last_tool_msgs
            )
            if turn_has_errors:
                consecutive_errors.append(last_tool_msgs[-1].get("content", "")[:150])
            else:
                consecutive_errors = []

            if len(consecutive_errors) >= ERROR_LOOP_THRESHOLD:
                self.history.append({
                    "role": "user",
                    "content": (
                        f"STOP. You repeated the same failing action {ERROR_LOOP_THRESHOLD}+ times. "
                        f"Last error: {consecutive_errors[-1][:200]}. "
                        "Try a COMPLETELY different approach. "
                        "If file not found: use bash (cat > file << 'EOF' ...) to create it. "
                        "If URL fails: try a different URL or skip it. "
                        "Do NOT repeat the same failing tool call."
                    ),
                })
                consecutive_errors = []

        emit(EngineEvent(type="error", text="Max iterations reached without completion", is_error=True))
        return "".join(final_text_parts)
