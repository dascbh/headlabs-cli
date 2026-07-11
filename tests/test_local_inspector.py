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


# ── platform provider helpers ────────────────────────────────────────────────

import types


def test_build_code_bundle_includes_source_excludes_vendored(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "notes.txt").write_text("ignore me")  # not a source ext
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "index.js").write_text("var x = 1")
    bundle = inspector.build_code_bundle(str(tmp_path))
    assert "FILE: app.py" in bundle
    assert "notes.txt" not in bundle
    assert "node_modules" not in bundle


def test_platform_findings_from_dict_wrapper():
    res = types.SimpleNamespace(raw_output={"answer": "", "findings": [
        {"severity": "critical", "title": "SQLi", "detail": "concat", "fix": "params", "file": "a.py", "line": 12},
    ]}, summary="")
    out = inspector.platform_findings_from_result(res)
    assert len(out) == 1 and out[0]["severity"] == "critical" and out[0]["line"] == 12


def test_platform_findings_handles_brackets_in_strings():
    # Regression: bracket chars inside a detail must not break structured extraction.
    res = types.SimpleNamespace(raw_output={"findings": [
        {"severity": "high", "title": "Arr", "detail": "uses list[0] and dict[key]", "fix": "x"},
    ]}, summary="")
    out = inspector.platform_findings_from_result(res)
    assert len(out) == 1 and "list[0]" in out[0]["detail"]


def test_platform_findings_empty_when_no_data():
    res = types.SimpleNamespace(raw_output={"answer": ""}, summary="")
    assert inspector.platform_findings_from_result(res) == []


def test_ensure_platform_agent_creates_when_missing():
    created = {}

    class FakeClient:
        def list_remote_agents(self):
            return [{"id": "other"}]

        def create_agent(self, **kwargs):
            created.update(kwargs)
            return {"id": kwargs["agent_id"]}

    agent_id = inspector.ensure_platform_agent(FakeClient())
    assert agent_id == inspector.PLATFORM_AGENT_ID
    assert created["agent_id"] == inspector.PLATFORM_AGENT_ID


def test_ensure_usability_agent_creates_and_syncs_prompt():
    created = {}
    patched = {}

    class FakeClient:
        def list_remote_agents(self):
            return []

        def create_agent(self, **kwargs):
            created.update(kwargs)
            return {"id": kwargs["agent_id"]}

        def request(self, method, path, **kwargs):
            patched["method"] = method
            patched["path"] = path
            patched["json"] = kwargs.get("json")
            return {}

    agent_id = inspector.ensure_usability_agent(FakeClient())
    assert agent_id == inspector.USABILITY_AGENT_ID
    assert created["agent_id"] == inspector.USABILITY_AGENT_ID
    # It is a pure synthesizer: prompt kept in sync, NO MCP attached (the CLI
    # drives the browser directly).
    assert patched["method"] == "PATCH"
    assert patched["json"]["prompt"] == inspector._USABILITY_AGENT_PROMPT
    assert patched["json"]["manifest"]["mcp"] == []


def test_deterministic_usability_findings_from_axe_and_mobile():
    axe = {"violations": [
        {"id": "color-contrast", "impact": "serious", "help": "Contrast too low",
         "description": "Elements must have sufficient contrast", "helpUrl": "http://x",
         "node_count": 3, "sample_targets": [".a", ".b"]},
    ]}
    mobile = {"accessibility": {"horizontal_overflow": True, "small_tap_targets": 4},
              "page_errors": ["TypeError: x is not a function"],
              "failed_requests": [], "performance": {"fcp_ms": 4000}}
    out = inspector.deterministic_usability_findings(axe, mobile)
    titles = [f["title"] for f in out]
    files = [f["file"] for f in out]
    assert "WCAG: color-contrast" in titles
    assert "wcag:color-contrast" in files  # stable dedup key
    assert any("Overflow" in t for t in titles)
    assert any("toque" in t for t in titles)
    assert any("JavaScript" in t for t in titles)
    assert any("Contentful" in t for t in titles)
    # axe 'serious' maps to 'high'
    assert out[0]["severity"] == "high"


def test_deterministic_usability_findings_tolerates_errors():
    assert inspector.deterministic_usability_findings({"error": "boom"}, {"error": "boom"}) == []


def test_usability_is_a_role_choice():
    assert "usability" in inspector.ROLE_CHOICES


def test_ensure_platform_agent_idempotent_when_present():
    class FakeClient:
        def list_remote_agents(self):
            return [{"id": inspector.PLATFORM_AGENT_ID}]

        def create_agent(self, **kwargs):
            raise AssertionError("must not create when agent already exists")

    assert inspector.ensure_platform_agent(FakeClient()) == inspector.PLATFORM_AGENT_ID


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
