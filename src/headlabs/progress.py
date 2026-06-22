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

_ICONS = {
    "tool_use": "🔧",
    "thinking": "💭",
    "step": "›",
    "status": "•",
    "error": "✗",
    "warn": "⚠",
}

_DIM = "\033[2m"
_RESET = "\033[0m"
_CLEAR_LINE = "\r\033[K"


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

    def header(self, text: str) -> None:
        if self.quiet:
            return
        self._println("")
        self._println(f"{_DIM}{text}{_RESET}" if self.tty else text)

    def phase(self, text: str, detail: Optional[str] = None) -> None:
        """A completed local step (checklist line)."""
        if self.quiet:
            return
        line = f"  ✓ {text}"
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
        """Render one streamed event."""
        etype = ev.get("type", "")
        level = ev.get("level", "info")
        label = ev.get("label") or ev.get("tool") or etype
        self._event_count += 1
        if etype == "tool_use":
            self._tool_count += 1

        # Keep the spinner label tracking the latest meaningful action.
        if etype in ("tool_use", "step", "thinking"):
            self._label = label

        if level == "error":
            self._emit_line(f"    {_ICONS['error']} {label}")
            return
        if self.quiet:
            return
        # By default surface status/step/tool_use/thinking; verbose adds the rest.
        if etype not in _ICONS and not self.verbose:
            return
        icon = _ICONS.get(etype, "·")
        self._emit_line(f"    {icon} {label}")

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
            self._println(f"  ✓ Concluído em {elapsed}{tools}")
        elif status == "timeout":
            self._println(f"  ✗ Tempo esgotado após {elapsed}")
        else:
            self._println(f"  ✗ {status} após {elapsed}{tools}")

    # ── internals ────────────────────────────────────────────────────────────

    def _spin(self) -> None:
        frames = itertools.cycle(_SPINNER_FRAMES)
        while not self._stop.is_set():
            with self._lock:
                elapsed = _fmt_elapsed(time.time() - (self._start_ts or time.time()))
                self.out.write(f"{_CLEAR_LINE}  {next(frames)} {self._label}   {_DIM}{elapsed}{_RESET}")
                self.out.flush()
            time.sleep(0.1)

    def _emit_line(self, line: str) -> None:
        with self._lock:
            if self.tty:
                self.out.write(_CLEAR_LINE)
                self.out.write(line + "\n")
            else:
                self.out.write(line + "\n")
            self.out.flush()

    def _println(self, line: str) -> None:
        with self._lock:
            if self.tty:
                self.out.write(_CLEAR_LINE)
            self.out.write(line + "\n")
            self.out.flush()
