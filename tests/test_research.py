"""Tests for research mode (mocked — no network).

Research is a loop variant (mode="research") that runs amplified investigative
research and returns findings instead of building. These tests assert the
request contract (POST /labs-v2 + POST /loops with mode=research) and the
findings rendering, without touching the network.
"""

import io
import json
from types import SimpleNamespace

import pytest

import headlabs.labsctl as L


class _Recorder:
    """Stand-in HeadLabsClient that records requests and returns canned bodies."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def request(self, method, path, *, params=None, json=None, timeout=30):
        self.calls.append({"method": method, "path": path, "json": json})
        key = f"{method} {path}"
        resp = self.responses.get(key)
        return resp() if callable(resp) else (resp or {})


def _args(**kw):
    base = dict(intent=None, lab=None, name=None, stack=None, depth="deep",
                sources=None, output="table", watch=False, wait=False,
                quiet=False, verbose=False, tenant=None, research_cmd="create")
    base.update(kw)
    return SimpleNamespace(**base)


# ── _research_opts ────────────────────────────────────────────────────────────

def test_research_opts_defaults_to_deep():
    assert L._research_opts(_args()) == {"depth": "deep"}


def test_research_opts_parses_sources_and_depth():
    opts = L._research_opts(_args(depth="exhaustive", sources="web, docs ,repo"))
    assert opts == {"depth": "exhaustive", "sources": ["web", "docs", "repo"]}


def test_research_opts_rejects_bad_depth():
    with pytest.raises(SystemExit):
        L._research_opts(_args(depth="bogus"))


# ── _research_create: request contract ────────────────────────────────────────

def test_research_create_makes_lab_then_research_loop(monkeypatch):
    rec = _Recorder({
        "POST /labs-v2": {"lab_id": "lab_1", "name": "url-shortener-trends"},
        "POST /loops": {"loop_id": "loop_1"},
    })
    monkeypatch.setattr(L, "HeadLabsClient", lambda *a, **k: rec)
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)

    L._research_create(_args(intent="tendências de url shorteners", depth="deep"))

    paths = [c["path"] for c in rec.calls]
    assert paths == ["/labs-v2", "/loops"]
    loop_body = rec.calls[1]["json"]
    assert loop_body["mode"] == "research"
    assert loop_body["lab_id"] == "lab_1"
    assert loop_body["research"] == {"depth": "deep"}
    assert loop_body["intent"] == "tendências de url shorteners"


def test_research_create_uses_existing_lab_no_lab_creation(monkeypatch):
    rec = _Recorder({
        "GET /labs-v2": [{"lab_id": "lab_42", "name": "notes"}],
        "POST /loops": {"loop_id": "loop_9"},
    })
    monkeypatch.setattr(L, "HeadLabsClient", lambda *a, **k: rec)
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)

    L._research_create(_args(intent="rate limiting strategies", lab="notes",
                             sources="web,repo"))

    methods_paths = [(c["method"], c["path"]) for c in rec.calls]
    assert ("POST", "/labs-v2") not in methods_paths  # reused existing lab
    loop_call = next(c for c in rec.calls if c["path"] == "/loops")
    assert loop_call["json"]["lab_id"] == "lab_42"
    assert loop_call["json"]["research"]["sources"] == ["web", "repo"]


def test_research_create_requires_intent(monkeypatch):
    monkeypatch.setattr(L, "HeadLabsClient", lambda *a, **k: _Recorder())
    with pytest.raises(SystemExit):
        L._research_create(_args(intent=None))


def test_research_create_json_output(monkeypatch):
    rec = _Recorder({
        "POST /labs-v2": {"lab_id": "lab_x", "name": "n"},
        "POST /loops": {"loop_id": "loop_x"},
    })
    monkeypatch.setattr(L, "HeadLabsClient", lambda *a, **k: rec)
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)

    L._research_create(_args(intent="topic", output="json"))
    out = json.loads(buf.getvalue())
    assert out == {"lab_id": "lab_x", "job_id": "loop_x",
                   "mode": "research", "created_lab": True}


# ── _render_findings ──────────────────────────────────────────────────────────

def test_render_findings_full(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    loop = {
        "lab_id": "lab_1",
        "findings": {
            "summary": "O mercado de X está crescendo.",
            "key_findings": [{"title": "Achado A", "detail": "detalhe A"}, "Achado B"],
            "opportunities": [{"title": "Ideia 1", "confidence": "alta"}],
            "sources": [{"title": "Fonte 1", "url": "http://e.com"}],
            "report_path": "research/x.md",
        },
    }
    L._render_findings(loop)
    text = buf.getvalue()
    assert "Resumo da pesquisa" in text
    assert "Achado A" in text and "Achado B" in text
    assert "Ideia 1" in text
    assert "Fonte 1" in text
    assert "research/x.md" in text


def test_render_findings_empty_points_to_status(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    L._render_findings({"loop_id": "loop_1"})
    text = buf.getvalue()
    assert "finalizados" in text.lower()       # findings are being finalized (not "missing")
    assert "status loop_1" in text


def test_await_findings_polls_until_present(monkeypatch):
    # status flips to complete before findings are written: _await_findings
    # should re-fetch until findings appear.
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_request(method, path, **kw):
        calls["n"] += 1
        return {"findings": {"summary": "ready"}} if calls["n"] >= 2 else {}

    client = SimpleNamespace(request=fake_request)
    out = L._await_findings(client, "loop_1", {"status": "complete"}, attempts=5, delay=0)
    assert out["findings"]["summary"] == "ready"
    assert calls["n"] == 2


def test_await_findings_returns_immediately_if_present():
    client = SimpleNamespace(request=lambda *a, **k: pytest.fail("should not refetch"))
    loop = {"findings": {"summary": "already here"}}
    assert L._await_findings(client, "loop_1", loop) is loop


# ── _loops_list mode filter ───────────────────────────────────────────────────

def test_loops_list_mode_filter_research(monkeypatch):
    loops = [
        {"loop_id": "l1", "mode": "research", "intent": "a"},
        {"loop_id": "l2", "intent": "b"},  # no mode → build
        {"loop_id": "l3", "mode": "build", "intent": "c"},
    ]
    rec = _Recorder({"GET /loops": loops})
    monkeypatch.setattr(L, "HeadLabsClient", lambda *a, **k: rec)
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)

    L._loops_list(SimpleNamespace(lab=None, status=None, active=False,
                                  mode="research", quiet=True, output="table"))
    out = buf.getvalue().split()
    assert out == ["l1"]


# ── CLI parsing: simplified positional form ───────────────────────────────────

def _parse_research(argv, monkeypatch):
    """Run the real CLI parser for `research ...` and capture the namespace
    handed to the command (without executing any network call)."""
    import headlabs.cli as cli
    captured = {}
    monkeypatch.setattr(cli.labsctl, "cmd_research",
                        lambda args: captured.update(args=args))
    monkeypatch.setattr("sys.argv", ["headlabs"] + argv)
    cli.main()
    return captured["args"]


def test_cli_research_positional_intent_defaults(monkeypatch):
    ns = _parse_research(["research", "estado da arte em rate limiting"], monkeypatch)
    assert ns.intent == "estado da arte em rate limiting"
    assert ns.depth == "deep"        # default, no flag needed
    assert ns.sources is None        # None => server uses all available sources


def test_cli_research_positional_with_watch(monkeypatch):
    ns = _parse_research(["research", "tema", "-w"], monkeypatch)
    assert ns.intent == "tema" and ns.watch is True


def test_cli_research_back_compat_dash_i(monkeypatch):
    ns = _parse_research(["research", "-i", "via flag", "--depth", "exhaustive"], monkeypatch)
    assert ns.intent == "via flag" and ns.depth == "exhaustive"


def test_cli_research_alias_rsch(monkeypatch):
    ns = _parse_research(["rsch", "outro tema", "--sources", "web,docs"], monkeypatch)
    assert ns.intent == "outro tema" and ns.sources == "web,docs"
