"""Tests for ProgressReporter (non-TTY rendering — plain, no ANSI/spinner)."""

import io

from headlabs.progress import ProgressReporter


def _rep(**kw):
    buf = io.StringIO()
    return ProgressReporter(stream=buf, **kw), buf


def test_status_and_tool_lines():
    r, b = _rep()
    r.begin_wait()
    r.event({"type": "status", "label": "Agente rodando"})
    r.event({"type": "tool_use", "tool": "explore_costs"})
    out = b.getvalue()
    assert "● Agente rodando" in out
    assert "- explore_costs" in out


def test_tool_detail_subitems_only_when_present():
    r, b = _rep()
    r.begin_wait()
    r.event({"type": "tool_use", "tool": "get_s3", "detail": {
        "summary": "42 buckets", "buckets": 42}})
    out = b.getvalue()
    assert "- get_s3" in out
    assert "-> 42 buckets" in out
    assert "buckets=42" in out


def test_thinking_renders_reasoning():
    r, b = _rep()
    r.begin_wait()
    r.event({"type": "thinking", "detail": {"seconds": 3, "text": "raciocinio aqui"}})
    out = b.getvalue()
    assert "Thought for 3s" in out
    assert "╰ raciocinio aqui" in out


def test_handoff_and_agent_indentation():
    r, b = _rep()
    r.begin_wait()
    r.event({"type": "handoff", "detail": {"from": "a", "to": "b", "task": "faça x"}})
    r.event({"type": "tool_use", "tool": "t1", "agent": "b"})
    out = b.getvalue()
    assert "→ delega para b" in out
    assert "faça x" in out
    # b is at delegation depth 1 -> two extra leading spaces before the base line
    assert "    - t1" in out


def test_quiet_suppresses_normal_events():
    r, b = _rep(quiet=True)
    r.begin_wait()
    r.event({"type": "tool_use", "tool": "x"})
    r.phase("perfil resolvido")
    assert b.getvalue() == ""


def test_quiet_still_surfaces_errors():
    r, b = _rep(quiet=True)
    r.event({"type": "status", "label": "acesso negado", "level": "error"})
    assert "acesso negado" in b.getvalue()


def test_finish_reports_tool_count():
    r, b = _rep()
    r.begin_wait()
    r.event({"type": "tool_use", "tool": "a"})
    r.event({"type": "tool_use", "tool": "b"})
    r.finish("succeeded")
    assert "2 tool calls" in b.getvalue()


def test_summary_block():
    r, b = _rep()
    r.summary(
        text="resumo executivo",
        findings=[
            {"severity": "critical", "title": "EKS ext support", "saving_usd": 360},
            {"severity": "low", "title": "logs sem retention", "saving_usd": 12},
        ],
        savings=1322,
        reports=["reports/x.html", "reports/x.json"],
    )
    out = b.getvalue()
    assert "Resumo" in out and "resumo executivo" in out
    assert "[CRITICAL] EKS ext support" in out
    assert "$360/mo" in out
    assert "Economia potencial: $1,322/mês" in out
    assert "reports/x.html" in out
    # sorted by savings: critical ($360) before low ($12)
    assert out.index("EKS ext support") < out.index("logs sem retention")


def test_thinking_shows_full_reasoning_no_truncation():
    """Full preview: long multi-paragraph reasoning must NOT be truncated."""
    r, b = _rep()
    r.begin_wait()
    long_text = ("Passo 1: criar tabelas. " * 20) + "\nPasso final: ENTREGAR_TUDO_COMPLETO"
    r.event({"type": "thinking", "detail": {"seconds": 2, "text": long_text}})
    out = b.getvalue()
    # the tail (which the old 240-char cap would have dropped) must be present
    assert "ENTREGAR_TUDO_COMPLETO" in out
    assert "╰ " in out
