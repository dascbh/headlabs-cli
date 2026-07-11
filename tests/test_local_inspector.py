"""Unit tests for headlabs.local.inspector — prompt building, skill bridge,
fallback parsing — plus an end-to-end engine run driving report_finding via a
fake scripted provider (no network, no real LLM)."""
from __future__ import annotations

from headlabs.local import backlog, inspector
from headlabs.local.engine import QueryEngine
from headlabs.local.permission import PermissionManager
from headlabs.local.provider import ChatEvent, ToolCall
from headlabs.local.tools import ReportFindingTool, ReadFileTool


# ── prompt building ──────────────────────────────────────────────────────────

def test_prompt_includes_role_focus_and_context():
    p = inspector.build_inspector_prompt("backend", context="focus on auth")
    assert "backend focus" in p
    assert "focus on auth" in p
    assert "report_finding" in p


def test_prompt_url_block_only_when_url_given():
    with_url = inspector.build_inspector_prompt("frontend", url="http://localhost:5173")
    without = inspector.build_inspector_prompt("frontend")
    assert "localhost:5173" in with_url and "browser_devtools" in with_url
    assert "browser_devtools" not in without


def test_unknown_role_falls_back_to_qa():
    p = inspector.build_inspector_prompt("bogus")
    assert "qa focus" in p


def test_fix_prompt_lists_findings():
    items = [{"severity": "high", "resource": "a.py:1", "title": "Bug", "fix": "do x"}]
    p = inspector.build_fix_prompt_from_findings(items)
    assert "a.py:1" in p and "Bug" in p and "do x" in p


# ── skill bridge ─────────────────────────────────────────────────────────────

def test_fetch_skills_empty_when_no_ids():
    assert inspector.fetch_skills(None) == ""
    assert inspector.fetch_skills([]) == ""


def test_fetch_skills_injects_content(monkeypatch):
    class FakeClient:
        def request(self, method, path, **kwargs):
            assert method == "GET" and path.startswith("/resources/skill/")
            return {"content": "checklist body"}
    monkeypatch.setattr("headlabs.client.HeadLabsClient", FakeClient)
    out = inspector.fetch_skills(["sec-checklist"])
    assert "sec-checklist" in out and "checklist body" in out


def test_fetch_skills_survives_unreachable(monkeypatch):
    class FakeClient:
        def request(self, *a, **k):
            raise RuntimeError("offline")
    monkeypatch.setattr("headlabs.client.HeadLabsClient", FakeClient)
    assert inspector.fetch_skills(["x"]) == ""


# ── fallback parsing ─────────────────────────────────────────────────────────

def test_parse_findings_fallback_array():
    txt = 'issues:\n[{"severity":"high","title":"T","detail":"D","fix":"F","file":"a.py","line":3}]'
    out = inspector.parse_findings_fallback(txt)
    assert out == [{"severity": "high", "title": "T", "detail": "D",
                    "fix": "F", "file": "a.py", "line": 3}]


def test_parse_findings_fallback_object_wrapper():
    txt = '{"findings": [{"title": "T", "description": "D"}]}'
    out = inspector.parse_findings_fallback(txt)
    assert len(out) == 1 and out[0]["title"] == "T" and out[0]["detail"] == "D"


def test_parse_findings_fallback_no_json():
    assert inspector.parse_findings_fallback("no findings here") == []


# ── end-to-end: engine drives report_finding ─────────────────────────────────

class _FakeProvider:
    def __init__(self, turns):
        self._turns = turns
        self.call_count = 0

    def stream(self, messages, tool_defs=None, **kwargs):
        turn = self._turns[self.call_count]
        self.call_count += 1
        yield from turn

    def close(self):
        pass


def test_engine_records_finding_via_tool(tmp_path):
    d = str(tmp_path)
    provider = _FakeProvider([
        [ChatEvent(type="tool_calls", tool_calls=[ToolCall(
            id="1", name="report_finding",
            arguments='{"severity":"high","title":"SQLi","detail":"concat","fix":"params","file":"app.py","line":9,"role":"security"}',
        )]), ChatEvent(type="done", finish_reason="tool_calls")],
        [ChatEvent(type="text_delta", text="Found 1 issue."),
         ChatEvent(type="done", finish_reason="stop")],
    ])
    pm = PermissionManager(d, mode="default", prompt_fn=lambda *a: "yes")
    engine = QueryEngine(provider, [ReportFindingTool, ReadFileTool], pm,
                         cwd=d, system_prompt="inspect", max_iterations=10)
    final = engine.run(inspector.inspect_task_message("security"))

    assert "Found 1 issue" in final
    items = backlog.load_backlog(d)
    assert len(items) == 1
    assert items[0]["resource"] == "app.py:9" and items[0]["severity"] == "high"
