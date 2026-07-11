"""``headlabs trace`` — inspect, compare, and export persisted agent traces.

Subcommands (kubectl/aws-cli inspired, consistent with :mod:`headlabs.labsctl`):

- ``trace list``            recent traces (table | json), newest first
- ``trace show <id>``       full timeline of one trace (events + metrics)
- ``trace diff <a> <b>``    compare two traces (metrics, tools, findings, result)
- ``trace export <id>``     emit a trace as OTLP/JSON or raw JSON, or POST to a
                            collector with ``--endpoint``

Exit codes mirror the rest of the CLI: ``0`` ok, ``2`` usage / not found.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Optional

from headlabs import trace_store
from headlabs.tracing import AgentTrace

EXIT_OK, EXIT_USAGE = 0, 2

# ── ANSI / TTY (mirrors labsctl conventions) ─────────────────────────────────-
_C = {"reset": "\033[0m", "dim": "\033[2m", "red": "\033[31m", "green": "\033[32m",
      "yellow": "\033[33m", "cyan": "\033[36m", "bold": "\033[1m"}
_STATUS_COLOR = {
    "succeeded": "green", "complete": "green", "completed": "green", "partial": "yellow",
    "running": "cyan", "failed": "red", "dlq": "red", "timed_out": "red",
    "cancelled": "yellow", "rejected": "yellow", "timeout": "red",
}


def _tty() -> bool:
    return sys.stdout.isatty()


def _c(text, color: str) -> str:
    if not _tty() or color not in _C:
        return str(text)
    return f"{_C[color]}{text}{_C['reset']}"


def _color_status(s: str) -> str:
    return _c(s, _STATUS_COLOR.get((s or "").lower(), "reset"))


def _short(tid: str, n: int = 14) -> str:
    return (tid or "")[:n]


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    delta = max(0, datetime.now(timezone.utc).timestamp() - ts)
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def _fmt_dur(s: float) -> str:
    s = int(s or 0)
    return f"{s // 60:02d}:{s % 60:02d}"


def _die(msg: str, code: int = EXIT_USAGE) -> None:
    print(_c(f"erro: {msg}", "red"), file=sys.stderr)
    sys.exit(code)


def _output(args) -> str:
    return getattr(args, "output", "table") or "table"


# ── trace list ────────────────────────────────────────────────────────────────
def _trace_list(args) -> None:
    rows = trace_store.list_traces(
        limit=getattr(args, "limit", 20),
        agent_id=getattr(args, "agent", None),
        workflow=getattr(args, "workflow", None),
    )
    if _output(args) == "json":
        print(json.dumps(rows, indent=2, default=str, ensure_ascii=False))
        return
    if not rows:
        if _tty():
            print(_c("Nenhuma trace encontrada. Rode um agente para gerar uma.", "dim"))
        return
    cols = [("TRACE", 16), ("AGENT", 20), ("WORKFLOW", 10), ("STATUS", 12),
            ("AGE", 6), ("DUR", 7), ("TOOLS", 6), ("TOKENS", 9), ("COST", 9)]
    header = "  ".join(_c(t.ljust(w), "bold") for t, w in cols)
    print(header)
    for e in rows:
        cost = e.get("cost_usd") or 0
        cells = [
            _short(e.get("trace_id", "")).ljust(16),
            str(e.get("agent_id", ""))[:20].ljust(20),
            str(e.get("workflow", ""))[:10].ljust(10),
            _color_status(e.get("status", "?")).ljust(12 + (len(_color_status(e.get("status", "?"))) - len(e.get("status", "?")))),
            _ago(e.get("started_at")).ljust(6),
            _fmt_dur(e.get("duration_s", 0)).ljust(7),
            str(e.get("tool_calls", 0)).ljust(6),
            str(e.get("total_tokens", 0)).ljust(9),
            (f"${cost:.4f}" if cost else "-").ljust(9),
        ]
        print("  ".join(cells).rstrip())


# ── trace show ────────────────────────────────────────────────────────────────
def _trace_show(args) -> None:
    trace = trace_store.resolve_trace(args.trace_id)
    if trace is None:
        _die(f"trace '{args.trace_id}' não encontrada (ou prefixo ambíguo)")
    if _output(args) == "json":
        print(json.dumps(trace.to_dict(), indent=2, default=str, ensure_ascii=False))
        return

    m = trace.metrics
    print(_c(f"  {trace.workflow or 'run'} · {trace.agent_id}", "bold"))
    print(f"  trace      {trace.trace_id}")
    if trace.account_id:
        print(f"  account    {trace.account_id}")
    print(f"  status     {_color_status(trace.status)}")
    print(f"  duration   {_fmt_dur(m.duration_s)}  ({m.duration_s:.1f}s)")
    print(f"  tools      {m.tool_calls}   llm {m.llm_calls}   errors {m.errors}")
    if m.total_tokens:
        print(f"  tokens     {m.input_tokens} in / {m.output_tokens} out   "
              f"~${m.cost_usd:.4f}")
    if m.tools_used:
        top = sorted(m.tools_used.items(), key=lambda kv: kv[1], reverse=True)
        print(f"  tool mix   " + ", ".join(f"{k}×{v}" for k, v in top[:8]))

    # Timeline
    print()
    print(_c("  TIMELINE", "bold"))
    t0 = trace.started_at
    for ev in trace.events:
        rel = max(0.0, ev.ts - t0)
        marker = {
            "tool_use": _c("◆", "cyan"), "tool_result": _c("◇", "cyan"),
            "thinking": _c("✻", "dim"), "handoff": _c("→", "cyan"),
            "error": _c("✗", "red"), "approval_request": _c("⏸", "yellow"),
            "llm_call": _c("◆", "green"),
        }.get(ev.type, _c("·", "dim"))
        name = ev.tool or ev.label or ev.type
        extra = ""
        it, ot = ev.input_tokens(), ev.output_tokens()
        if it or ot:
            extra = _c(f"   {it}→{ot} tok", "dim")
        line = f"  {rel:6.1f}s  {marker} {name}{extra}"
        if ev.is_error():
            line = _c(line, "red")
        print(line)

    if trace.result:
        print()
        print(_c("  RESULT", "bold"))
        summary = (trace.result.get("summary") if isinstance(trace.result, dict) else None)
        if summary:
            print(f"  {str(summary)[:200]}")
        insights = trace.result.get("insights") if isinstance(trace.result, dict) else None
        if isinstance(insights, list) and insights:
            print(f"  {len(insights)} insights")


# ── trace diff ────────────────────────────────────────────────────────────────
def _result_findings(trace: AgentTrace) -> list:
    r = trace.result if isinstance(trace.result, dict) else {}
    return r.get("insights") or r.get("findings") or []


def _diff_data(a: AgentTrace, b: AgentTrace) -> dict:
    """Structured comparison of two traces (older ``a`` vs newer ``b``)."""
    ma, mb = a.metrics, b.metrics

    def delta(x, y):
        return round(y - x, 6)

    fa, fb = _result_findings(a), _result_findings(b)
    ra = a.result if isinstance(a.result, dict) else {}
    rb = b.result if isinstance(b.result, dict) else {}
    sav_a = float(ra.get("total_saving_usd", 0) or 0)
    sav_b = float(rb.get("total_saving_usd", 0) or 0)

    tools_a = set(ma.tools_used)
    tools_b = set(mb.tools_used)

    return {
        "a": {"trace_id": a.trace_id, "status": a.status, "started_at": a.started_at},
        "b": {"trace_id": b.trace_id, "status": b.status, "started_at": b.started_at},
        "metrics": {
            "duration_s": {"a": ma.duration_s, "b": mb.duration_s, "delta": delta(ma.duration_s, mb.duration_s)},
            "tool_calls": {"a": ma.tool_calls, "b": mb.tool_calls, "delta": delta(ma.tool_calls, mb.tool_calls)},
            "llm_calls": {"a": ma.llm_calls, "b": mb.llm_calls, "delta": delta(ma.llm_calls, mb.llm_calls)},
            "total_tokens": {"a": ma.total_tokens, "b": mb.total_tokens, "delta": delta(ma.total_tokens, mb.total_tokens)},
            "cost_usd": {"a": ma.cost_usd, "b": mb.cost_usd, "delta": delta(ma.cost_usd, mb.cost_usd)},
            "errors": {"a": ma.errors, "b": mb.errors, "delta": delta(ma.errors, mb.errors)},
        },
        "findings": {"a": len(fa), "b": len(fb), "delta": len(fb) - len(fa)},
        "total_saving_usd": {"a": sav_a, "b": sav_b, "delta": delta(sav_a, sav_b)},
        "tools": {
            "added": sorted(tools_b - tools_a),
            "removed": sorted(tools_a - tools_b),
            "shared": sorted(tools_a & tools_b),
        },
    }


def _trace_diff(args) -> None:
    a = trace_store.resolve_trace(args.trace_a)
    b = trace_store.resolve_trace(args.trace_b)
    if a is None:
        _die(f"trace '{args.trace_a}' não encontrada")
    if b is None:
        _die(f"trace '{args.trace_b}' não encontrada")
    data = _diff_data(a, b)
    if _output(args) == "json":
        print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
        return

    def arrow(d):
        if d > 0:
            return _c(f"▲ +{d:g}", "green")
        if d < 0:
            return _c(f"▼ {d:g}", "red")
        return _c("= 0", "dim")

    print(_c(f"  diff  {_short(a.trace_id)}  →  {_short(b.trace_id)}", "bold"))
    print(f"  {a.agent_id}  ({_color_status(a.status)} → {_color_status(b.status)})")
    print()
    print(_c(f"  {'METRIC':<16} {'A':>12} {'B':>12}   DELTA", "bold"))
    print(f"  {'-'*54}")
    mlabels = [("duration_s", "duration"), ("tool_calls", "tool calls"),
               ("llm_calls", "llm calls"), ("total_tokens", "tokens"),
               ("cost_usd", "cost $"), ("errors", "errors")]
    for key, label in mlabels:
        row = data["metrics"][key]
        print(f"  {label:<16} {row['a']:>12g} {row['b']:>12g}   {arrow(row['delta'])}")

    f = data["findings"]
    print(f"  {'findings':<16} {f['a']:>12} {f['b']:>12}   {arrow(f['delta'])}")
    s = data["total_saving_usd"]
    print(f"  {'savings $':<16} {s['a']:>12g} {s['b']:>12g}   {arrow(s['delta'])}")

    tools = data["tools"]
    if tools["added"]:
        print(f"\n  {_c('tools added:', 'green')} {', '.join(tools['added'])}")
    if tools["removed"]:
        print(f"  {_c('tools removed:', 'red')} {', '.join(tools['removed'])}")


# ── trace export ──────────────────────────────────────────────────────────────
def _trace_export(args) -> None:
    trace = trace_store.resolve_trace(args.trace_id)
    if trace is None:
        _die(f"trace '{args.trace_id}' não encontrada")
    fmt = getattr(args, "format", "otel") or "otel"
    endpoint = getattr(args, "endpoint", None)

    if fmt == "otel":
        from headlabs import otel
        if endpoint:
            code = otel.export_otlp_http(trace, endpoint)
            ok = 200 <= code < 300
            print(_c(f"  {'✓' if ok else '✗'} OTLP export → {endpoint}  (HTTP {code})",
                     "green" if ok else "red"))
            if not ok:
                sys.exit(1)
        else:
            print(json.dumps(otel.trace_to_otlp(trace), indent=2, ensure_ascii=False))
    else:  # raw
        print(json.dumps(trace.to_dict(), indent=2, default=str, ensure_ascii=False))


# ── dispatch ──────────────────────────────────────────────────────────────────
def cmd_trace(args) -> None:
    sub = getattr(args, "trace_cmd", None)
    if sub == "list" or sub is None:
        return _trace_list(args)
    if sub == "show":
        return _trace_show(args)
    if sub == "diff":
        return _trace_diff(args)
    if sub == "export":
        return _trace_export(args)
    _die(f"subcomando de trace desconhecido: {sub}")
