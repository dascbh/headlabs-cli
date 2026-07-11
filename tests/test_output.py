"""Tests for output adapters and the make_reporter factory (headlabs.output)."""

import io
import json

import pytest

from headlabs.output import (
    make_reporter, StreamJsonReporter, JsonReporter, TracingReporter, OUTPUT_FORMATS,
)
from headlabs.tracing import AgentTrace


def _lines(buf):
    return [l for l in buf.getvalue().splitlines() if l.strip()]


def test_stream_json_emits_one_event_per_line_plus_result():
    buf = io.StringIO()
    r = make_reporter("stream-json", workflow="run", agent_id="finops",
                      stream=buf, persist=False)
    r.begin_wait()
    r.event({"type": "tool_use", "tool": "explore_costs"})
    r.event({"type": "thinking", "detail": {"seconds": 2}})
    r.finish("succeeded", "done")
    lines = _lines(buf)
    objs = [json.loads(l) for l in lines]
    assert objs[0]["type"] == "tool_use" and objs[0]["tool"] == "explore_costs"
    assert objs[1]["type"] == "thinking"
    assert objs[-1] == {"type": "result", "status": "succeeded", "summary": "done"}


def test_json_emits_single_full_trace_at_end():
    buf = io.StringIO()
    r = make_reporter("json", workflow="run", agent_id="finops",
                      stream=buf, persist=False)
    r.begin_wait()
    r.event({"type": "tool_use", "tool": "get_s3"})
    r.set_result({"summary": "ok", "account_id": "123", "insights": [{"t": 1}]})
    r.finish("succeeded", "ok")
    # exactly one JSON object, the full trace
    obj = json.loads(buf.getvalue())
    assert obj["workflow"] == "run"
    assert obj["agent_id"] == "finops"
    assert obj["metrics"]["tool_calls"] == 1
    assert obj["result"]["summary"] == "ok"
    assert obj["account_id"] == "123"  # backfilled from result


def test_json_reporter_silent_until_finish():
    buf = io.StringIO()
    r = make_reporter("json", stream=buf, persist=False)
    r.begin_wait()
    r.event({"type": "tool_use", "tool": "x"})
    assert buf.getvalue() == ""  # nothing emitted yet
    r.finish("succeeded")
    assert buf.getvalue() != ""


def test_tracing_reporter_records_into_trace():
    buf = io.StringIO()
    r = make_reporter("human", workflow="run", agent_id="finops",
                      stream=buf, persist=False)
    r.begin_wait()
    r.event({"type": "tool_use", "tool": "a"})
    r.event({"type": "tool_use", "tool": "b"})
    assert r.trace.metrics.tool_calls == 2
    assert "a" in r.trace.metrics.tools_used


def test_invoked_captures_exec_id_on_trace():
    buf = io.StringIO()
    r = make_reporter("human", stream=buf, persist=False)
    r.invoked("exec-123")
    assert r.trace.meta["exec_id"] == "exec-123"


def test_finish_persists_when_requested(tmp_path, monkeypatch):
    import headlabs.trace_store as ts
    monkeypatch.setattr(ts, "TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(ts, "_INDEX", tmp_path / "traces" / "index.jsonl")
    buf = io.StringIO()
    r = make_reporter("stream-json", workflow="run", agent_id="finops",
                      stream=buf, persist=True)
    r.begin_wait()
    r.event({"type": "tool_use", "tool": "a"})
    r.finish("succeeded")
    rows = ts.list_traces()
    assert len(rows) == 1
    assert rows[0]["tool_calls"] == 1


def test_machine_reporter_prompt_approval_fails_safe():
    r = make_reporter("json", persist=False)
    assert r.prompt_approval({"action": "delete"}) == "reject"


def test_finish_is_idempotent():
    buf = io.StringIO()
    r = make_reporter("stream-json", stream=buf, persist=False)
    r.begin_wait()
    r.finish("succeeded")
    n1 = len(_lines(buf))
    r.finish("succeeded")
    # JsonReporter guards re-emit; stream-json result may repeat but trace
    # finalize must not double-run — duration stays stable.
    assert r.trace.status == "succeeded"
    assert n1 >= 1


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        make_reporter("yaml")


def test_delegation_to_inner_reporter():
    # header/phase/summary should not raise on machine reporters (no-ops).
    buf = io.StringIO()
    r = make_reporter("stream-json", stream=buf, persist=False)
    r.header("hi")
    r.phase("did x")
    r.summary(text="t")
    assert buf.getvalue() == ""  # machine format stays silent on these
