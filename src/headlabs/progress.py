"""Live, TTY-aware progress feedback for agent runs.

Renders two kinds of signal:

1. Local pipeline phases the CLI controls (resolving profile/tenant, collecting
   data, invoking) — printed as completed checklist lines.
2. Live events streamed from the execution event endpoint
   (``GET /executions/{id}/events``): ``status``, ``step``, ``tool_use``,
   ``thinking`` — rendered Kiro-style as they arrive.

Design:
- On a TTY, a background spinner shows the current action + elapsed time, and
  event lines are printed above it.
- Without a TTY (pipe / CI / ``--output json``), output is plain, line-based,
  with no ANSI or spinner — safe to redirect and parse.
- ``--quiet`` suppresses everything but errors; ``--verbose`` shows every event.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from typing import Optional, TextIO

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_DIM = "\033[2m"
_RESET = "\033[0m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_CLEAR_LINE = "\r\033[K"

# Kiro-style status dot. `●` is a monochrome geometric glyph (not an emoji);
# colour is applied via ANSI on a TTY only.
_DOT = "●"


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


class ProgressReporter:
    """Renders live progress for a single agent run.

    Methods are safe to call whether or not stdout is a TTY; rendering adapts.
    All public methods are no-ops under ``quiet`` except errors and the final
    status line.
    """

    def __init__(self, *, stream: Optional[TextIO] = None,
                 quiet: bool = False, verbose: bool = False) -> None:
        self.out: TextIO = stream or sys.stdout
        self.quiet = quiet
        self.verbose = verbose
        self.tty = bool(getattr(self.out, "isatty", lambda: False)()) and not quiet
        self._lock = threading.Lock()
        self._spinner: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._label = "Processando…"
        self._start_ts: Optional[float] = None
        self._event_count = 0
        self._tool_count = 0

    # ── local pipeline phases ────────────────────────────────────────────────

    def _dot(self, color: str) -> str:
        """Colored status dot on a TTY, plain `●` otherwise."""
        return f"{color}{_DOT}{_RESET}" if self.tty else _DOT

    def header(self, text: str) -> None:
        if self.quiet:
            return
        self._println("")
        self._println(f"{_DIM}{text}{_RESET}" if self.tty else text)

    def phase(self, text: str, detail: Optional[str] = None) -> None:
        """A completed local step (green dot)."""
        if self.quiet:
            return
        line = f"  {self._dot(_GREEN)} {text}"
        if detail:
            line += f"   {_DIM}{detail}{_RESET}" if self.tty else f"   ({detail})"
        self._println(line)

    def invoked(self, exec_id: str) -> None:
        self.phase("Agente invocado", f"exec {exec_id[:8]}")

    # ── live waiting / streamed events ───────────────────────────────────────

    def begin_wait(self, label: str = "Agente processando…") -> None:
        self._label = label
        self._start_ts = time.time()
        if self.tty:
            self._stop.clear()
            self._spinner = threading.Thread(target=self._spin, daemon=True)
            self._spinner.start()
        elif not self.quiet:
            self._println(f"  {label}")

    def event(self, ev: dict) -> None:
        """Render one streamed event, Kiro-style.

        - ``status`` / ``step``: a coloured dot milestone line.
        - ``tool_use``: a single dimmed line ``- {tool}   +{elapsed}`` plus
          indented sub-lines when the backend supplies a ``detail`` dict.
        - ``thinking``: ``● Thought for Ns`` with the reasoning as a ``╰``
          continuation (shown only when the backend emits thinking events).
        - ``error`` (or level=error): a red dot line.
        """
        etype = ev.get("type", "")
        level = ev.get("level", "info")
        tool = ev.get("tool")
        label = ev.get("label") or tool or etype
        self._event_count += 1
        if etype == "tool_use":
            self._tool_count += 1
            self._label = tool or label
        elif etype in ("step", "thinking"):
            self._label = label

        if level == "error" or etype == "error":
            self._emit_block(f"  {self._dot(_RED)} {label}", self._detail_lines(ev))
            return
        if self.quiet:
            return

        if etype == "tool_use":
            name = tool or label
            el = _fmt_elapsed(time.time() - self._start_ts) if self._start_ts is not None else None
            if self.tty:
                line = f"  {_DIM}- {name}" + (f"   +{el}" if el else "") + _RESET
            else:
                line = f"  - {name}" + (f"   +{el}" if el else "")
            self._emit_block(line, self._detail_lines(ev))
        elif etype == "thinking":
            self._emit_block(self._thinking_line(ev), self._thinking_detail(ev))
        elif etype in ("status", "step"):
            self._emit_block(f"  {self._dot(_CYAN)} {label}", [])
        elif self.verbose:
            self._emit_block(f"  {self._dot(_CYAN)} {label}", [])

    def _thinking_line(self, ev: dict) -> str:
        """Primary line for a thinking event: '● Thought for Ns' (dimmed)."""
        detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
        secs = detail.get("seconds") or detail.get("duration_s")
        if secs is None and isinstance(detail.get("ms"), (int, float)):
            secs = round(detail["ms"] / 1000)
        head = f"Thought for {int(secs)}s" if isinstance(secs, (int, float)) else "Thinking"
        dot = self._dot(_DIM) if self.tty else _DOT
        return f"  {dot} {_DIM}{head}{_RESET}" if self.tty else f"  {_DOT} {head}"

    def _thinking_detail(self, ev: dict) -> list[str]:
        """The reasoning text as a '╰' continuation, if the event carries it."""
        detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
        text = detail.get("text") or detail.get("reasoning") or detail.get("summary")
        if not text and ev.get("label") not in (None, "thinking"):
            text = ev.get("label")
        if not text:
            return []
        text = str(text).strip().replace("\n", " ")[:240]
        return [f"      {_DIM}╰ {text}{_RESET}" if self.tty else f"      ╰ {text}"]

    def _detail_lines(self, ev: dict) -> list[str]:
        """Indented sub-lines for a tool call — only when the backend provides
        a ``detail`` dict (the tool id and elapsed already live on the primary
        line, so nothing is repeated here)."""
        lines: list[str] = []
        detail = ev.get("detail")
        if isinstance(detail, dict) and detail:
            # Free-text summary of the tool result, if present.
            for key in ("summary", "result", "text", "message"):
                val = detail.get(key)
                if val:
                    lines.append(f"      -> {str(val)[:160]}")
                    break
            # Compact key=value for small scalar fields (args, counts, etc.).
            kv = [
                f"{k}={v}" for k, v in detail.items()
                if k not in ("summary", "result", "text", "message")
                and isinstance(v, (str, int, float, bool)) and len(str(v)) <= 40
            ]
            if kv:
                seg = " · ".join(kv[:6])
                lines.append(f"      {_DIM}{seg}{_RESET}" if self.tty else f"      {seg}")
        return lines

    def finish(self, status: str, summary: Optional[str] = None) -> None:
        """Stop the spinner and print a terminal status line."""
        if self.tty and self._spinner is not None:
            self._stop.set()
            self._spinner.join(timeout=0.5)
            with self._lock:
                self.out.write(_CLEAR_LINE)
                self.out.flush()
        if self.quiet:
            return
        elapsed = _fmt_elapsed(time.time() - self._start_ts) if self._start_ts else ""
        tools = f" · {self._tool_count} tool calls" if self._tool_count else ""
        if status in ("succeeded", "partial"):
            self._println(f"  {self._dot(_GREEN)} Concluído em {elapsed}{tools}")
        elif status == "timeout":
            self._println(f"  {self._dot(_RED)} Tempo esgotado após {elapsed}")
        elif status == "cancelled":
            self._println(f"  {self._dot(_YELLOW)} Cancelado após {elapsed}")
        else:
            self._println(f"  {self._dot(_RED)} {status} após {elapsed}{tools}")

    def summary(self, *, text: Optional[str] = None, findings: Optional[list] = None,
                savings: Optional[float] = None, reports: Optional[list] = None,
                max_findings: int = 10) -> None:
        """Render a final summary block after completion: executive summary,
        top findings (by savings) with severity dot, total savings, reports."""
        if self.quiet:
            return
        import textwrap

        def b(s: str) -> str:
            return f"{_BOLD}{s}{_RESET}" if self.tty else s

        def dim(s: str) -> str:
            return f"{_DIM}{s}{_RESET}" if self.tty else s

        self._println("")
        if text:
            self._println(f"  {b('Resumo')}")
            for ln in textwrap.wrap(str(text).strip(), width=78):
                self._println(f"    {ln}")
            self._println("")

        findings = findings or []
        if findings:
            def sv(f):
                try:
                    return float(f.get("saving_usd") or 0)
                except (TypeError, ValueError):
                    return 0.0
            ordered = sorted(findings, key=sv, reverse=True)
            shown = ordered[:max_findings]
            self._println(f"  {b('Principais achados')} ({len(findings)})")
            sev_color = {"critical": _RED, "high": _YELLOW, "medium": _YELLOW, "low": _DIM}
            for f in shown:
                sev = (f.get("severity") or "info").lower()
                title = f.get("title") or f.get("finding") or f.get("description") or ""
                dot = self._dot(sev_color.get(sev, _CYAN))
                s = sv(f)
                tail = dim(f"   ${s:,.0f}/mo") if s else ""
                self._println(f"  {dot} [{sev.upper()}] {title}{tail}")
            if len(findings) > len(shown):
                self._println(f"    {dim(f'+{len(findings) - len(shown)} mais')}")
            self._println("")

        if savings:
            line = f"Economia potencial: ${savings:,.0f}/mês"
            self._println(f"  {_GREEN}{line}{_RESET}" if self.tty else f"  {line}")
        for r in (reports or []):
            self._println(f"  {dim('Relatório: ' + r)}")

    # ── internals ────────────────────────────────────────────────────────────

    def _spin(self) -> None:
        frames = itertools.cycle(_SPINNER_FRAMES)
        while not self._stop.is_set():
            with self._lock:
                elapsed = _fmt_elapsed(time.time() - (self._start_ts or time.time()))
                self.out.write(f"{_CLEAR_LINE}  {_CYAN}{next(frames)}{_RESET} {self._label}   {_DIM}{elapsed}{_RESET}")
                self.out.flush()
            time.sleep(0.1)

    def _emit_block(self, primary: str, sublines: list[str]) -> None:
        with self._lock:
            if self.tty:
                self.out.write(_CLEAR_LINE)
            self.out.write(primary + "\n")
            for sl in sublines:
                self.out.write(sl + "\n")
            self.out.flush()

    def _println(self, line: str) -> None:
        with self._lock:
            if self.tty:
                self.out.write(_CLEAR_LINE)
            self.out.write(line + "\n")
            self.out.flush()
