"""Tests for trace persistence (headlabs.trace_store).

Uses monkeypatched module paths so traces are written to a tmp dir, never the
real ~/.headlabs.
"""

import importlib

import pytest

import headlabs.trace_store as ts
from headlabs.tracing import AgentTrace


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "traces"
    monkeypatch.setattr(ts, "TRACES_DIR", d)
    monkeypatch.setattr(ts, "_INDEX", d / "index.jsonl")
    return ts


def _trace(agent="finops", workflow="run", status="succeeded", tools=0, savings=0):
    t = AgentTrace(workflow=workflow, agent_id=agent, account_id="123")
    for i in range(tools):
        t.record_raw({"type": "tool_use", "tool": f"t{i}"})
    t.finalize(status, {"summary": "s", "insights": [], "total_saving_usd": savings})
    return t


def test_save_and_load(store):
    t = _trace(tools=2)
    path = store.save_trace(t)
    assert path.exists()
    loaded = store.load_trace(t.trace_id)
    assert loaded is not None
    assert loaded.trace_id == t.trace_id
    assert loaded.metrics.tool_calls == 2


def test_list_newest_first(store):
    a = _trace()
    a.started_at = 100
    store.save_trace(a)
    b = _trace()
    b.started_at = 200
    store.save_trace(b)
    rows = store.list_traces()
    assert [r["trace_id"] for r in rows] == [b.trace_id, a.trace_id]


def test_list_filters(store):
    store.save_trace(_trace(agent="finops", workflow="run"))
    store.save_trace(_trace(agent="threat", workflow="test"))
    assert len(store.list_traces(agent_id="finops")) == 1
    assert len(store.list_traces(workflow="test")) == 1


def test_resolve_by_prefix(store):
    t = _trace()
    store.save_trace(t)
    assert store.resolve_trace(t.trace_id).trace_id == t.trace_id
    assert store.resolve_trace(t.trace_id[:14]).trace_id == t.trace_id
    assert store.resolve_trace("trace_doesnotexist") is None


def test_resolve_ambiguous_prefix_returns_none(store):
    # Two traces; an empty-ish prefix matches both → ambiguous → None.
    store.save_trace(_trace())
    store.save_trace(_trace())
    assert store.resolve_trace("trace_") is None


def test_latest_excludes_before(store):
    a = _trace(agent="finops", workflow="test")
    a.started_at = 100
    store.save_trace(a)
    b = _trace(agent="finops", workflow="test")
    b.started_at = 200
    store.save_trace(b)
    # latest overall is b; excluding b yields a (the baseline).
    assert store.latest_trace(agent_id="finops", workflow="test").trace_id == b.trace_id
    assert store.latest_trace(agent_id="finops", workflow="test",
                              before=b.trace_id).trace_id == a.trace_id


def test_save_upserts_index(store):
    t = _trace(tools=1)
    store.save_trace(t)
    t.record_raw({"type": "tool_use", "tool": "extra"})
    store.save_trace(t)  # same trace_id, updated metrics
    rows = store.list_traces()
    assert len(rows) == 1
    assert rows[0]["tool_calls"] == 2


def test_delete(store):
    t = _trace()
    store.save_trace(t)
    assert store.delete_trace(t.trace_id) is True
    assert store.load_trace(t.trace_id) is None
    assert store.list_traces() == []


def test_path_traversal_is_neutralized(store):
    # A hostile id must not escape the traces dir.
    t = AgentTrace(workflow="run", agent_id="x")
    t.trace_id = "../../etc/passwd"
    t.finalize("succeeded", {})
    path = store.save_trace(t)
    assert path.parent == store.traces_dir()
