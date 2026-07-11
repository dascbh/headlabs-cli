"""Tests for OTel/OTLP export (headlabs.otel)."""

from headlabs.otel import trace_to_otlp, _hex_trace_id, _hex_span_id, _attr
from headlabs.tracing import AgentTrace


def _trace():
    t = AgentTrace(workflow="run", agent_id="finops", account_id="123")
    t.record_raw({"type": "tool_use", "tool": "explore_costs",
                  "detail": {"input_tokens": 100, "output_tokens": 50, "model": "sonnet"}})
    t.record_raw({"type": "error", "label": "boom", "level": "error"})
    t.finalize("succeeded", {"summary": "ok"})
    return t


def test_otlp_structure_has_root_plus_event_spans():
    t = _trace()
    doc = trace_to_otlp(t)
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    # root + 2 events
    assert len(spans) == 3
    root = spans[0]
    assert root["name"].startswith("run:")
    # children reference the root or a declared parent
    assert all("traceId" in s for s in spans)


def test_hex_ids_have_correct_length_and_are_deterministic():
    assert len(_hex_trace_id("trace_x")) == 32
    assert len(_hex_span_id("span_x")) == 16
    assert _hex_trace_id("trace_x") == _hex_trace_id("trace_x")


def test_genai_attributes_present_on_tool_span():
    t = _trace()
    doc = trace_to_otlp(t)
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    tool_span = next(s for s in spans if s["name"].startswith("execute_tool"))
    keys = {a["key"] for a in tool_span["attributes"]}
    assert "gen_ai.operation.name" in keys
    assert "gen_ai.tool.name" in keys
    assert "gen_ai.usage.input_tokens" in keys
    assert "gen_ai.request.model" in keys


def test_error_event_maps_to_error_status():
    t = _trace()
    doc = trace_to_otlp(t)
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    err = next(s for s in spans if s["name"] == "error")
    assert err["status"]["code"] == 2  # STATUS_ERROR


def test_resource_has_service_name():
    doc = trace_to_otlp(_trace(), service_name="my-svc")
    attrs = doc["resourceSpans"][0]["resource"]["attributes"]
    sn = next(a for a in attrs if a["key"] == "service.name")
    assert sn["value"]["stringValue"] == "my-svc"


def test_attr_typing():
    assert _attr("k", True)["value"] == {"boolValue": True}
    assert _attr("k", 5)["value"] == {"intValue": "5"}
    assert _attr("k", 1.5)["value"] == {"doubleValue": 1.5}
    assert _attr("k", "s")["value"] == {"stringValue": "s"}


def test_timestamps_are_nanosecond_strings():
    t = _trace()
    doc = trace_to_otlp(t)
    root = doc["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert root["startTimeUnixNano"].isdigit()
    assert int(root["endTimeUnixNano"]) >= int(root["startTimeUnixNano"])
