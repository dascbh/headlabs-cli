"""Unit tests for headlabs.local.engine.QueryEngine — driven entirely by a
fake, scripted provider so the tool-call loop is tested without any network
or real LLM server."""
from __future__ import annotations

import pytest

from headlabs.local.engine import EngineEvent, QueryEngine
from headlabs.local.permission import PermissionManager
from headlabs.local.provider import ChatEvent, ProviderError, ToolCall
from headlabs.local.tools.base import BaseTool, ToolResult
from pydantic import BaseModel


class _EchoInput(BaseModel):
    text: str


class _EchoTool(BaseTool):
    """A trivial, always-allowed tool used only in these tests."""
    name = "echo"
    description = "Echoes text back"
    input_schema = _EchoInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return False

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        return ToolResult(output=f"echoed: {input_data['text']}")


class _DenyPermissionTool(BaseTool):
    """A tool that always requires permission, for denial-path tests."""
    name = "danger"
    description = "Requires approval"
    input_schema = _EchoInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        return ToolResult(output="should not run")


class FakeProvider:
    """Replays a scripted sequence of turns; each turn is a list of ChatEvents.
    ``close()`` is a no-op so QueryEngine/CLI cleanup code doesn't need mocking.
    """

    def __init__(self, turns: list[list[ChatEvent]]):
        self._turns = turns
        self.call_count = 0

    def stream(self, messages, tool_defs=None, **kwargs):
        turn = self._turns[self.call_count]
        self.call_count += 1
        yield from turn

    def close(self) -> None:
        pass


def _engine(provider, tools, *, mode="default", prompt_fn=None, tmp_path=None, max_iterations=10) -> QueryEngine:
    cwd = str(tmp_path) if tmp_path else "."
    pm = PermissionManager(cwd, mode=mode, prompt_fn=prompt_fn or (lambda *a: "yes"))
    return QueryEngine(provider, tools, pm, cwd=cwd, max_iterations=max_iterations)


def test_engine_returns_plain_text_when_model_calls_no_tools(tmp_path):
    provider = FakeProvider([
        [ChatEvent(type="text_delta", text="Hello "), ChatEvent(type="text_delta", text="world"),
         ChatEvent(type="done", finish_reason="stop")],
    ])
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path)
    result = engine.run("hi")
    assert result == "Hello world"
    assert provider.call_count == 1


def test_engine_executes_tool_call_and_feeds_result_back(tmp_path):
    provider = FakeProvider([
        [ChatEvent(type="tool_calls", tool_calls=[
            ToolCall(id="1", name="echo", arguments='{"text": "ping"}')
        ]), ChatEvent(type="done", finish_reason="tool_calls")],
        [ChatEvent(type="text_delta", text="done"), ChatEvent(type="done", finish_reason="stop")],
    ])
    events = []
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path)
    result = engine.run("please echo ping", on_event=events.append)

    assert result == "done"
    assert provider.call_count == 2
    tool_result_events = [e for e in events if e.type == "tool_result"]
    assert tool_result_events[0].tool_output == "echoed: ping"
    # The tool result must have been appended to history for the second turn.
    tool_messages = [m for m in engine.history if m.get("role") == "tool"]
    assert tool_messages[-1]["content"] == "echoed: ping"


def test_engine_stops_on_unknown_tool_without_crashing(tmp_path):
    provider = FakeProvider([
        [ChatEvent(type="tool_calls", tool_calls=[
            ToolCall(id="1", name="does_not_exist", arguments="{}")
        ])],
        [ChatEvent(type="text_delta", text="ok"), ChatEvent(type="done", finish_reason="stop")],
    ])
    events = []
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path)
    engine.run("call a bad tool", on_event=events.append)
    unknown_tool_event = next(e for e in events if e.type == "tool_result")
    assert unknown_tool_event.is_error
    assert "Unknown tool" in unknown_tool_event.tool_output


def test_engine_handles_invalid_tool_arguments_gracefully(tmp_path):
    provider = FakeProvider([
        [ChatEvent(type="tool_calls", tool_calls=[
            ToolCall(id="1", name="echo", arguments="{not valid json")
        ])],
        [ChatEvent(type="text_delta", text="ok"), ChatEvent(type="done", finish_reason="stop")],
    ])
    events = []
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path)
    engine.run("bad args", on_event=events.append)
    err_event = next(e for e in events if e.type == "tool_result")
    assert err_event.is_error


def test_engine_handles_missing_required_field_gracefully(tmp_path):
    provider = FakeProvider([
        [ChatEvent(type="tool_calls", tool_calls=[
            ToolCall(id="1", name="echo", arguments="{}")  # missing required "text"
        ])],
        [ChatEvent(type="text_delta", text="ok"), ChatEvent(type="done", finish_reason="stop")],
    ])
    events = []
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path)
    engine.run("missing field", on_event=events.append)
    err_event = next(e for e in events if e.type == "tool_result")
    assert err_event.is_error
    assert "Invalid arguments" in err_event.tool_output


def test_engine_permission_denial_is_fed_back_to_model_not_raised(tmp_path):
    provider = FakeProvider([
        [ChatEvent(type="tool_calls", tool_calls=[
            ToolCall(id="1", name="danger", arguments='{"text": "boom"}')
        ])],
        [ChatEvent(type="text_delta", text="understood, stopping"), ChatEvent(type="done", finish_reason="stop")],
    ])
    events = []
    engine = _engine(provider, [_DenyPermissionTool], prompt_fn=lambda *a: "no", tmp_path=tmp_path)
    result = engine.run("do something dangerous", on_event=events.append)

    assert result == "understood, stopping"
    denial_events = [e for e in events if e.type == "permission_denied"]
    assert len(denial_events) == 1
    tool_messages = [m for m in engine.history if m.get("role") == "tool"]
    assert "Permission denied" in tool_messages[-1]["content"]


def test_engine_auto_mode_bypasses_permission_prompt(tmp_path):
    prompt_calls = []
    provider = FakeProvider([
        [ChatEvent(type="tool_calls", tool_calls=[
            ToolCall(id="1", name="danger", arguments='{"text": "boom"}')
        ])],
        [ChatEvent(type="text_delta", text="done"), ChatEvent(type="done", finish_reason="stop")],
    ])
    engine = _engine(
        provider, [_DenyPermissionTool], mode="auto",
        prompt_fn=lambda *a: prompt_calls.append(a) or "no",
        tmp_path=tmp_path,
    )
    engine.run("do something dangerous")
    assert prompt_calls == []  # auto mode never prompts


def test_engine_propagates_provider_error(tmp_path):
    class BrokenProvider:
        def stream(self, *a, **k):
            raise ProviderError("connection refused")
            yield  # pragma: no cover - unreachable, keeps this a generator

        def close(self):
            pass

    engine = _engine(BrokenProvider(), [_EchoTool], tmp_path=tmp_path)
    with pytest.raises(ProviderError):
        engine.run("hi")


def test_engine_respects_max_iterations_ceiling(tmp_path):
    """A provider that always calls a tool and never stops must not loop forever."""
    infinite_tool_call = [ChatEvent(type="tool_calls", tool_calls=[
        ToolCall(id="1", name="echo", arguments='{"text": "again"}')
    ])]
    provider = FakeProvider([infinite_tool_call] * 5)
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path, max_iterations=3)
    events = []
    engine.run("loop forever", on_event=events.append)
    assert provider.call_count == 3
    assert any(e.type == "error" for e in events)


def test_engine_seeds_history_with_system_prompt(tmp_path):
    provider = FakeProvider([[ChatEvent(type="done", finish_reason="stop")]])
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path)
    assert engine.history[0]["role"] == "system"


def test_engine_never_sends_null_content_when_calling_tools(tmp_path):
    """Regression test: Ollama rejected assistant messages with content=null
    with HTTP 400 'invalid message content type: <nil>', observed in a real
    session where the model called a tool without generating any text.
    Content must always be a string (possibly empty), never None."""
    provider = FakeProvider([
        [ChatEvent(type="tool_calls", tool_calls=[
            ToolCall(id="1", name="echo", arguments='{"text": "ping"}')
        ])],  # model calls a tool with zero text output
        [ChatEvent(type="text_delta", text="done"), ChatEvent(type="done", finish_reason="stop")],
    ])
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path)
    engine.run("do something")

    assistant_messages = [m for m in engine.history if m.get("role") == "assistant"]
    for msg in assistant_messages:
        assert msg["content"] is not None
        assert isinstance(msg["content"], str)


def test_engine_retries_once_on_empty_response_and_recovers(tmp_path):
    """Regression test: a real 8B model was observed to deterministically stop
    with NO tool calls and NO text after several successful tool calls in the
    same conversation (confirmed reproducible 3/3 times against a live
    server). Silently returning "" is a worse user experience than nudging
    the model once to synthesize from what it already has."""
    provider = FakeProvider([
        [ChatEvent(type="tool_calls", tool_calls=[
            ToolCall(id="1", name="echo", arguments='{"text": "ping"}')
        ])],
        [ChatEvent(type="done", finish_reason="stop")],  # stops with empty text -- the failure
        [ChatEvent(type="text_delta", text="Here is the answer"), ChatEvent(type="done", finish_reason="stop")],
    ])
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path)
    result = engine.run("do something")

    assert result == "Here is the answer"
    nudge_messages = [
        m for m in engine.history
        if m.get("role") == "user" and "stopped without producing any text" in m.get("content", "")
    ]
    assert len(nudge_messages) == 1


def test_engine_gives_up_after_exhausting_empty_response_retries(tmp_path):
    """If the model keeps stopping with no text even after retries, the
    engine must give up cleanly (empty string + error event) rather than
    looping forever or crashing."""
    provider = FakeProvider([[ChatEvent(type="done", finish_reason="stop")]] * 10)
    engine = _engine(provider, [_EchoTool], tmp_path=tmp_path, max_iterations=10)
    events = []
    result = engine.run("do something", on_event=events.append)

    assert result == ""
    assert any(e.type == "error" for e in events)
