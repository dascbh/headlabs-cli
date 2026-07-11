"""OpenAI-compatible chat completion provider.

Talks the ``/v1/chat/completions`` protocol (request/response + streaming SSE +
tool_calls in the OpenAI format) — the de-facto contract implemented by vLLM,
Ollama, LM Studio, TGI, SGLang and llama.cpp server. This is the only provider
`headlabs local` needs: self-hosted or not, if it speaks this protocol, it
works here without new code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

from headlabs.local.config import LocalConfig


class ProviderError(Exception):
    """Raised for HTTP/transport/parsing failures talking to the LLM server."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as emitted by the model; caller parses it

    def parsed_arguments(self) -> dict:
        try:
            return json.loads(self.arguments) if self.arguments else {}
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"Tool call {self.name!r} returned invalid JSON arguments: {exc}"
            ) from exc


@dataclass
class ChatEvent:
    """One increment of a streamed response.

    ``type`` is one of: "text_delta", "tool_calls", "usage", "done".
    Only one of the payload fields is populated depending on ``type``.
    """
    type: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    finish_reason: str | None = None


def _tool_schema_for_api(tool_defs: list[dict]) -> list[dict]:
    """``tool_defs`` are already ``{"name", "description", "input_schema"}``;
    wrap them in the OpenAI ``{"type": "function", "function": {...}}`` envelope.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tool_defs
    ]


class OpenAICompatibleProvider:
    """Minimal client for the OpenAI chat completions protocol.

    Deliberately does not depend on litellm or the OpenAI SDK: the surface
    needed (request building, SSE parsing, tool_call delta accumulation) is
    small and keeping it in-house avoids being hostage to a third-party
    dependency's breaking changes for what is, at its core, a well-documented
    stable wire format.
    """

    def __init__(self, config: LocalConfig, *, client: httpx.Client | None = None):
        if not config.is_configured():
            raise ProviderError(
                "Local provider not configured. Run: headlabs local config "
                "--base-url <url> --model <model>"
            )
        self._config = config
        self._client = client or httpx.Client(timeout=config.timeout_s)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenAICompatibleProvider":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def _url(self) -> str:
        base = self._config.base_url.rstrip("/")
        # Accept both "http://host:8000" and "http://host:8000/v1" style base URLs.
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def stream(
        self,
        messages: list[dict],
        tool_defs: list[dict] | None = None,
        *,
        temperature: float = 0.0,
    ) -> Iterator[ChatEvent]:
        """Stream a chat completion, yielding ``ChatEvent`` increments.

        Accumulates fragmented ``tool_calls`` deltas (OpenAI streams each
        tool call's name/arguments across multiple chunks, indexed by
        position) and emits a single consolidated ``ChatEvent(type="tool_calls")``
        once the stream signals completion for that turn.
        """
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            # Repetition penalties — critical for 8B models that tend to repeat
            # sentences/phrases even at temperature=0. These are OpenAI-compatible
            # parameters supported by Ollama's /v1/chat/completions endpoint.
            # frequency_penalty penalizes tokens proportionally to how often they
            # appeared so far; presence_penalty penalizes any token that appeared
            # at all (even once). Both reduce degenerate repetition without
            # significantly changing response quality for novel content.
            "frequency_penalty": 0.3,
            "presence_penalty": 0.1,
        }
        if tool_defs:
            payload["tools"] = _tool_schema_for_api(tool_defs)
            payload["tool_choice"] = "auto"

        # index -> accumulated {id, name, arguments}
        pending_calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None

        try:
            with self._client.stream(
                "POST", self._url(), json=payload, headers=self._headers()
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode(errors="replace")
                    raise ProviderError(
                        f"LLM server returned HTTP {resp.status_code}: {body[:500]}"
                    )
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    if not choices:
                        if chunk.get("usage"):
                            yield ChatEvent(type="usage", usage=chunk["usage"])
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    finish_reason = choice.get("finish_reason") or finish_reason

                    if delta.get("content"):
                        yield ChatEvent(type="text_delta", text=delta["content"])

                    for tc_delta in delta.get("tool_calls") or []:
                        idx = tc_delta.get("index", 0)
                        slot = pending_calls.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc_delta.get("id"):
                            slot["id"] = tc_delta["id"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

                    if chunk.get("usage"):
                        yield ChatEvent(type="usage", usage=chunk["usage"])

        except httpx.TransportError as exc:
            raise ProviderError(
                f"Could not reach LLM server at {self._config.base_url}: {exc}"
            ) from exc

        if pending_calls:
            calls = [
                ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=slot["arguments"])
                for i, slot in sorted(pending_calls.items())
            ]
            yield ChatEvent(type="tool_calls", tool_calls=calls, finish_reason=finish_reason)

        yield ChatEvent(type="done", finish_reason=finish_reason or "stop")
