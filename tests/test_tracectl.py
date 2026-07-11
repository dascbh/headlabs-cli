"""Tests for trace control rendering/diff logic (headlabs.tracectl)."""

from headlabs import tracectl
from headlabs.tracing import AgentTrace


def _trace(tools=(), findings=0, savings=0.0, status="succeeded"):
    t = AgentTrace(workflow="run", agent_id="finops")
    for name in tools:
        t.record_raw({"type": "tool_use", "tool": name})
    t.finalize(status, {"summary": "s",
                        "insights": [{"title": f"f{i}"} for i in range(findings)],
                        "total_saving_usd": savings})
    return t


def test_diff_data_metrics_and_findings():
    a = _trace(tools=["x", "y"], findings=10, savings=500)
    b = _trace(tools=["x", "z"], findings=14, savings=800)
    d = tracectl._diff_data(a, b)
    assert d["metrics"]["tool_calls"]["delta"] == 0
    assert d["findings"] == {"a": 10, "b": 14, "delta": 4}
    assert d["total_saving_usd"]["delta"] == 300.0
    assert d["tools"]["added"] == ["z"]
    assert d["tools"]["removed"] == ["y"]
    assert d["tools"]["shared"] == ["x"]


def test_result_findings_supports_both_keys():
    t = AgentTrace()
    t.result = {"findings": [1, 2, 3]}
    assert len(tracectl._result_findings(t)) == 3
    t.result = {"insights": [1, 2]}
    assert len(tracectl._result_findings(t)) == 2
    t.result = None
    assert tracectl._result_findings(t) == []


def test_short_and_fmt_dur():
    assert tracectl._short("trace_abcdef1234567890", 8) == "trace_ab"
    assert tracectl._fmt_dur(75) == "01:15"


def test_diff_handles_missing_results():
    a = _trace(tools=["x"])
    a.result = None
    b = _trace(tools=["x"])
    d = tracectl._diff_data(a, b)
    assert d["findings"]["a"] == 0


def test_list_json_output(tmp_path, monkeypatch, capsys):
    import headlabs.trace_store as ts
    monkeypatch.setattr(ts, "TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(ts, "_INDEX", tmp_path / "traces" / "index.jsonl")
    ts.save_trace(_trace(tools=["a"]))
    from types import SimpleNamespace
    tracectl._trace_list(SimpleNamespace(limit=20, agent=None, workflow=None, output="json"))
    import json
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["tool_calls"] == 1
