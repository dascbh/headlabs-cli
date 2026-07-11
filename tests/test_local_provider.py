"""Unit tests for headlabs.local.provider — SSE parsing and tool_call delta
accumulation, exercised against httpx.MockTransport (no real LLM server)."""
import json

import httpx
import pytest

from headlabs.local.config import LocalConfig
from headlabs.local.provider import OpenAICompatibleProvider, ProviderError, ToolCall


def _sse_response(chunks: list[dict]) -> httpx.Response:
    lines = []
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk)}")
    lines.append("data: [DONE]")
    body = "\n\n".join(lines) + "\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _provider_with_transport(handler) -> OpenAICompatibleProvider:
    cfg = LocalConfig(base_url="http://localhost:8000/v1", model="test-model")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleProvider(cfg, client=client)


def test_url_appends_v1_chat_completions_when_missing():
    cfg = LocalConfig(base_url="http://localhost:8000", model="m")
    provider = OpenAICompatibleProvider(cfg, client=httpx.Client())
    assert provider._url() == "http://localhost:8000/v1/chat/completions"


def test_url_does_not_double_append_v1():
    cfg = LocalConfig(base_url="http://localhost:8000/v1", model="m")
    provider = OpenAICompatibleProvider(cfg, client=httpx.Client())
    assert provider._url() == "http://localhost:8000/v1/chat/completions"


def test_missing_config_raises_provider_error():
    cfg = LocalConfig(base_url=None, model=None)
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider(cfg)


def test_stream_text_only_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([
            {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]},
        ])

    provider = _provider_with_transport(handler)
    events = list(provider.stream([{"role": "user", "content": "hi"}]))
    text_events = [e for e in events if e.type == "text_delta"]
    assert [e.text for e in text_events] == ["Hello", " world"]
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "stop"


def test_stream_accumulates_fragmented_tool_call_deltas():
    """OpenAI streams tool call name/arguments across multiple chunks, keyed by
    index — this is the trickiest part of the wire format to get right."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_123", "function": {"name": "read_", "arguments": ""}}
            ]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "file", "arguments": '{"path"'}}
            ]}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ': "a.py"}'}}
            ]}, "finish_reason": "tool_calls"}]},
        ])

    provider = _provider_with_transport(handler)
    events = list(provider.stream([{"role": "user", "content": "read a.py"}]))
    tool_call_events = [e for e in events if e.type == "tool_calls"]
    assert len(tool_call_events) == 1
    calls = tool_call_events[0].tool_calls
    assert len(calls) == 1
    assert calls[0].id == "call_123"
    assert calls[0].name == "read_file"
    assert calls[0].parsed_arguments() == {"path": "a.py"}


def test_stream_accumulates_multiple_parallel_tool_calls_by_index():
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_a", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
                {"index": 1, "id": "call_b", "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}},
            ]}, "finish_reason": "tool_calls"}]},
        ])

    provider = _provider_with_transport(handler)
    events = list(provider.stream([{"role": "user", "content": "read both"}]))
    calls = [e for e in events if e.type == "tool_calls"][0].tool_calls
    assert len(calls) == 2
    assert calls[0].parsed_arguments() == {"path": "a.py"}
    assert calls[1].parsed_arguments() == {"path": "b.py"}


def test_stream_raises_provider_error_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error")

    provider = _provider_with_transport(handler)
    with pytest.raises(ProviderError, match="HTTP 500"):
        list(provider.stream([{"role": "user", "content": "hi"}]))


def test_stream_raises_provider_error_on_transport_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = _provider_with_transport(handler)
    with pytest.raises(ProviderError, match="Could not reach LLM server"):
        list(provider.stream([{"role": "user", "content": "hi"}]))


def test_tool_call_parsed_arguments_invalid_json_raises_provider_error():
    call = ToolCall(id="1", name="bash", arguments="{not json")
    with pytest.raises(ProviderError):
        call.parsed_arguments()


def test_tool_call_empty_arguments_parses_to_empty_dict():
    call = ToolCall(id="1", name="bash", arguments="")
    assert call.parsed_arguments() == {}


def test_stream_sends_tools_in_openai_function_envelope():
    captured_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return _sse_response([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])

    provider = _provider_with_transport(handler)
    tool_defs = [{"name": "read_file", "description": "reads a file", "input_schema": {"type": "object"}}]
    list(provider.stream([{"role": "user", "content": "hi"}], tool_defs))

    assert captured_payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "reads a file",
                "parameters": {"type": "object"},
            },
        }
    ]
