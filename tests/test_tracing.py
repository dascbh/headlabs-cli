"""Tests for the typed event/trace model (headlabs.tracing)."""

from headlabs.tracing import (
    AgentEvent, AgentTrace, TraceMetrics, estimate_cost,
    event_schema, trace_schema, SCHEMA_VERSION, EVENT_TYPES,
)


def test_from_raw_normalizes_known_fields():
    ev = AgentEvent.from_raw(
        {"seq": 3, "type": "tool_use", "tool": "explore_costs",
         "detail": {"input_tokens": 100, "output_tokens": 50, "model": "sonnet"}},
        trace_id="trace_x", default_agent="finops")
    assert ev.type == "tool_use"
    assert ev.seq == 3
    assert ev.tool == "explore_costs"
    assert ev.trace_id == "trace_x"
    assert ev.agent_id == "finops"
    assert ev.input_tokens() == 100
    assert ev.output_tokens() == 50
    assert ev.model() == "sonnet"


def test_from_raw_maps_generation_to_llm_call_and_preserves_unknown_keys():
    ev = AgentEvent.from_raw({"type": "generation", "custom_field": "keep-me"})
    assert ev.type == "llm_call"
    assert ev.data["custom_field"] == "keep-me"


def test_from_raw_handles_non_dict_detail():
    ev = AgentEvent.from_raw({"type": "status", "detail": "just a string"})
    assert ev.data["value"] == "just a string"


def test_error_detection():
    assert AgentEvent.from_raw({"type": "error"}).is_error()
    assert AgentEvent.from_raw({"type": "status", "level": "error"}).is_error()
    assert not AgentEvent.from_raw({"type": "status"}).is_error()


def test_event_dict_round_trip_drops_empty_optionals():
    ev = AgentEvent(type="status", seq=1, label="x")
    d = ev.to_dict()
    assert "parent_span_id" not in d and "tool" not in d and "data" not in d
    back = AgentEvent.from_dict(d)
    assert back.type == "status" and back.label == "x"


def test_estimate_cost_by_model_family():
    # opus is pricier than haiku for the same tokens
    assert estimate_cost("claude-opus-4", 1000, 1000) > estimate_cost("haiku", 1000, 1000)
    assert estimate_cost("unknown", 0, 0) == 0.0


def test_trace_records_and_folds_metrics():
    t = AgentTrace(workflow="run", agent_id="finops")
    t.record_raw({"type": "tool_use", "tool": "a"})
    t.record_raw({"type": "tool_use", "tool": "a"})
    t.record_raw({"type": "tool_use", "tool": "b"})
    t.record_raw({"type": "llm_call", "detail": {"input_tokens": 200, "output_tokens": 100, "model": "sonnet"}})
    t.record_raw({"type": "error", "level": "error"})
    m = t.metrics
    assert m.tool_calls == 3
    assert m.tools_used == {"a": 2, "b": 1}
    assert m.llm_calls == 1
    assert m.input_tokens == 200 and m.output_tokens == 100
    assert m.total_tokens == 300
    assert m.cost_usd > 0
    assert m.errors == 1


def test_trace_assigns_seq_and_trace_id():
    t = AgentTrace()
    e1 = t.record_raw({"type": "status"})
    e2 = t.record_raw({"type": "status"})
    assert e1.seq == 1 and e2.seq == 2
    assert e1.trace_id == t.trace_id


def test_reported_cost_overrides_estimate():
    t = AgentTrace()
    t.record_raw({"type": "metric", "detail": {"cost_usd": 1.23}})
    assert t.metrics.cost_usd == 1.23


def test_finalize_sets_duration_and_status():
    t = AgentTrace()
    t.started_at = 100.0
    t.finalize("succeeded", {"summary": "ok"})
    assert t.status == "succeeded"
    assert t.ended_at is not None
    assert t.metrics.duration_s >= 0
    assert t.result == {"summary": "ok"}


def test_trace_dict_round_trip():
    t = AgentTrace(workflow="run", agent_id="finops", account_id="123")
    t.record_raw({"type": "tool_use", "tool": "x"})
    t.finalize("succeeded", {"summary": "done"})
    d = t.to_dict()
    back = AgentTrace.from_dict(d)
    assert back.trace_id == t.trace_id
    assert back.workflow == "run"
    assert back.agent_id == "finops"
    assert back.account_id == "123"
    assert back.status == "succeeded"
    assert len(back.events) == 1
    assert back.metrics.tool_calls == 1
    assert back.result == {"summary": "done"}
    assert back.schema_version == SCHEMA_VERSION


def test_schemas_are_wellformed():
    es = event_schema()
    ts = trace_schema()
    assert es["type"] == "object"
    assert set(EVENT_TYPES).issubset(set(es["properties"]["type"]["enum"]))
    assert ts["properties"]["events"]["items"] == es


def test_metrics_from_dict_tolerates_missing():
    m = TraceMetrics.from_dict({})
    assert m.tool_calls == 0 and m.total_tokens == 0
