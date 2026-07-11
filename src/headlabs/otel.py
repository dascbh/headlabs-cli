"""Export an :class:`~headlabs.tracing.AgentTrace` as OpenTelemetry spans.

This converts our internal trace model into the OpenTelemetry **GenAI semantic
conventions** (the CNCF standard adopted by AWS Bedrock AgentCore, Datadog,
Langfuse, Arize, Grafana Tempo, …). The output is the OTLP/JSON shape produced
by an OTLP exporter, so it can be POSTed to any OTLP/HTTP collector or saved for
import.

We deliberately depend on **no** OTel SDK: the OTLP/JSON structure is small and
stable, and emitting it directly keeps the CLI dependency-free. The attribute
names follow ``gen_ai.*`` / ``otel`` conventions:

- ``gen_ai.operation.name`` — ``invoke_agent`` | ``execute_tool`` | ``chat`` …
- ``gen_ai.agent.id`` / ``gen_ai.agent.name``
- ``gen_ai.tool.name``
- ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens``
- ``gen_ai.request.model``

Span kinds and the trace/span id encoding match the OTLP JSON spec (hex ids;
nanosecond unix timestamps as strings).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from headlabs.tracing import AgentTrace, AgentEvent

# OTLP span status codes
_STATUS_UNSET, _STATUS_OK, _STATUS_ERROR = 0, 1, 2

# Map our event types onto gen_ai operation names + OTLP span kind.
# SPAN_KIND_INTERNAL=1, CLIENT=3 (per OTLP spec).
_OP = {
    "agent_start": ("invoke_agent", 1),
    "agent_end": ("invoke_agent", 1),
    "llm_call": ("chat", 3),
    "tool_use": ("execute_tool", 3),
    "tool_result": ("execute_tool", 3),
    "thinking": ("reasoning", 1),
    "handoff": ("handoff", 1),
    "approval_request": ("approval", 1),
    "status": ("status", 1),
    "step": ("step", 1),
    "error": ("error", 1),
    "metric": ("metric", 1),
}


def _hex_trace_id(trace_id: str) -> str:
    """16-byte (32 hex) trace id derived deterministically from our string id."""
    return hashlib.sha256(trace_id.encode()).hexdigest()[:32]


def _hex_span_id(span_id: str) -> str:
    """8-byte (16 hex) span id derived deterministically from our string id."""
    return hashlib.sha256(span_id.encode()).hexdigest()[:16]


def _ns(ts: float) -> str:
    return str(int(ts * 1_000_000_000))


def _attr(key: str, value: Any) -> dict:
    """One OTLP KeyValue attribute, typed by the Python value."""
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def _event_to_span(ev: AgentEvent, trace: AgentTrace, *,
                   hex_trace: str, root_span: str, next_ts: float) -> dict:
    op, kind = _OP.get(ev.type, ("status", 1))
    attrs = [
        _attr("gen_ai.operation.name", op),
        _attr("gen_ai.agent.id", ev.agent_id or trace.agent_id),
        _attr("headlabs.event.type", ev.type),
        _attr("headlabs.event.seq", ev.seq),
    ]
    if ev.tool:
        attrs.append(_attr("gen_ai.tool.name", ev.tool))
    model = ev.model()
    if model:
        attrs.append(_attr("gen_ai.request.model", model))
    it, ot = ev.input_tokens(), ev.output_tokens()
    if it:
        attrs.append(_attr("gen_ai.usage.input_tokens", it))
    if ot:
        attrs.append(_attr("gen_ai.usage.output_tokens", ot))
    rc = ev.reported_cost()
    if rc is not None:
        attrs.append(_attr("headlabs.cost_usd", rc))

    # A point-in-time event becomes a short span [ts, next_ts]; the renderer
    # uses the next event's ts as the end (clamped to the trace end).
    end_ts = max(next_ts, ev.ts)
    span = {
        "traceId": hex_trace,
        "spanId": _hex_span_id(ev.span_id),
        "parentSpanId": _hex_span_id(ev.parent_span_id) if ev.parent_span_id else root_span,
        "name": f"{op}:{ev.tool}" if ev.tool else op,
        "kind": kind,
        "startTimeUnixNano": _ns(ev.ts),
        "endTimeUnixNano": _ns(end_ts),
        "attributes": attrs,
        "status": {"code": _STATUS_ERROR if ev.is_error() else _STATUS_OK},
    }
    return span


def trace_to_otlp(trace: AgentTrace, *, service_name: str = "headlabs") -> dict:
    """Convert a trace to an OTLP/JSON ``ExportTraceServiceRequest`` document.

    The root span represents the whole workflow; each event becomes a child
    span under it (or under its declared ``parent_span_id`` when present).
    """
    hex_trace = _hex_trace_id(trace.trace_id)
    root_span_hex = _hex_span_id(trace.trace_id + ":root")
    end = trace.ended_at or (trace.events[-1].ts if trace.events else trace.started_at)

    root = {
        "traceId": hex_trace,
        "spanId": root_span_hex,
        "name": f"{trace.workflow or 'agent'}:{trace.agent_id or 'run'}",
        "kind": 1,
        "startTimeUnixNano": _ns(trace.started_at),
        "endTimeUnixNano": _ns(end),
        "attributes": [
            _attr("gen_ai.operation.name", "invoke_agent"),
            _attr("gen_ai.agent.id", trace.agent_id),
            _attr("headlabs.workflow", trace.workflow),
            _attr("headlabs.account_id", trace.account_id),
            _attr("headlabs.trace_id", trace.trace_id),
            _attr("gen_ai.usage.input_tokens", trace.metrics.input_tokens),
            _attr("gen_ai.usage.output_tokens", trace.metrics.output_tokens),
            _attr("headlabs.tool_calls", trace.metrics.tool_calls),
            _attr("headlabs.cost_usd", trace.metrics.cost_usd),
        ],
        "status": {"code": _STATUS_ERROR if trace.status in
                   ("failed", "dlq", "timed_out", "error") else _STATUS_OK},
    }

    spans = [root]
    sorted_events = sorted(trace.events, key=lambda e: (e.ts, e.seq))
    for i, ev in enumerate(sorted_events):
        next_ts = sorted_events[i + 1].ts if i + 1 < len(sorted_events) else end
        spans.append(_event_to_span(ev, trace, hex_trace=hex_trace,
                                     root_span=root_span_hex, next_ts=next_ts))

    return {
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    _attr("service.name", service_name),
                    _attr("telemetry.sdk.name", "headlabs-cli"),
                ],
            },
            "scopeSpans": [{
                "scope": {"name": "headlabs.tracing", "version": trace.schema_version},
                "spans": spans,
            }],
        }],
    }


def export_otlp_http(trace: AgentTrace, endpoint: str, *,
                     service_name: str = "headlabs", timeout: int = 15) -> int:
    """POST the trace to an OTLP/HTTP collector (``/v1/traces``).

    Returns the HTTP status code. The endpoint may be the collector base URL
    (``/v1/traces`` is appended) or the full traces path.
    """
    import requests

    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url = url + "/v1/traces"
    payload = trace_to_otlp(trace, service_name=service_name)
    resp = requests.post(url, json=payload,
                         headers={"Content-Type": "application/json"}, timeout=timeout)
    return resp.status_code
