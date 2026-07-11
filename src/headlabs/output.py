"""Output adapters and the ``--output-format`` contract.

A single agent run is observed through a *reporter* object whose methods are
called by :mod:`headlabs.client` and the CLI commands as the run proceeds
(``header``, ``phase``, ``invoked``, ``begin_wait``, ``event``, ``finish``,
``summary``, ``prompt_approval``). This module provides three interchangeable
reporters selected by ``--output-format``:

- ``human`` — the existing :class:`~headlabs.progress.ProgressReporter`: TTY
  rendering with a spinner, coloured dots, and a final summary block.
- ``json`` — silent during the run; on ``finish`` it emits a single JSON object
  (the full trace + result) to stdout. For scripts that want the final answer.
- ``stream-json`` — emits one JSON object per line (NDJSON) as each event
  arrives, then a terminal ``{"type":"result", …}`` line. For live monitoring
  and piping into other tools/agents.

All three are wrapped by :class:`TracingReporter`, which records every event
into an :class:`~headlabs.tracing.AgentTrace` (so a trace is captured and
persisted regardless of the chosen format). :func:`make_reporter` wires the
right combination together.
"""

from __future__ import annotations

import json
import sys
from typing import Optional, TextIO

from headlabs.tracing import AgentEvent, AgentTrace

OUTPUT_FORMATS = ("human", "json", "stream-json")


# ── machine-readable reporters ────────────────────────────────────────────────
class _BaseMachineReporter:
    """Shared no-op surface so JSON reporters satisfy the same interface the
    human reporter exposes. Subclasses override only what they need."""

    def __init__(self, *, stream: Optional[TextIO] = None,
                 quiet: bool = False, verbose: bool = False) -> None:
        self.out: TextIO = stream or sys.stdout
        self.quiet = quiet
        self.verbose = verbose
        self.tty = False  # machine formats are never TTY-styled

    # local pipeline phases — silent for machine output (captured in the trace)
    def header(self, text: str) -> None: ...
    def phase(self, text: str, detail: Optional[str] = None) -> None: ...
    def invoked(self, exec_id: str) -> None: ...
    def begin_wait(self, label: str = "") -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def summary(self, **kwargs) -> None: ...

    def event(self, ev: dict) -> None: ...
    def finish(self, status: str, summary: Optional[str] = None) -> None: ...

    def prompt_approval(self, detail: dict) -> str:
        """Non-interactive: machine output never blocks on a human, so a
        mutating action is rejected (fail-safe), mirroring the human reporter's
        behaviour when there is no TTY."""
        return "reject"

    def _emit(self, obj: dict) -> None:
        self.out.write(json.dumps(obj, default=str, ensure_ascii=False) + "\n")
        self.out.flush()


class StreamJsonReporter(_BaseMachineReporter):
    """NDJSON: one normalized event per line, then a final ``result`` line."""

    def event(self, ev: dict) -> None:
        norm = AgentEvent.from_raw(ev)
        self._emit(norm.to_dict())

    def finish(self, status: str, summary: Optional[str] = None) -> None:
        obj = {"type": "result", "status": status}
        if summary:
            obj["summary"] = summary
        self._emit(obj)


class JsonReporter(_BaseMachineReporter):
    """Silent until the end, then emits one JSON object with the full trace and
    result. The trace is injected by :class:`TracingReporter` via
    :meth:`bind_trace`."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._trace: Optional[AgentTrace] = None
        self._emitted = False

    def bind_trace(self, trace: AgentTrace) -> None:
        self._trace = trace

    def finish(self, status: str, summary: Optional[str] = None) -> None:
        if self._emitted:
            return
        self._emitted = True
        if self._trace is not None:
            self._emit(self._trace.to_dict())
        else:  # defensive: no trace bound — still emit a minimal object
            obj = {"status": status}
            if summary:
                obj["summary"] = summary
            self._emit(obj)


# ── trace-recording wrapper ───────────────────────────────────────────────────
class TracingReporter:
    """Wrap any reporter and tee its events into an :class:`AgentTrace`.

    Delegates every attribute/method to the inner reporter, but intercepts
    ``event`` (to record a normalized event), ``invoked`` (to capture the
    platform exec id on the trace), and ``finish`` (to finalize the trace and
    let the store persist it). This keeps trace capture orthogonal to
    presentation: the same trace is built whether the user picked ``human``,
    ``json`` or ``stream-json``.
    """

    def __init__(self, inner, trace: AgentTrace, *, on_finish=None) -> None:
        self._inner = inner
        self.trace = trace
        self._on_finish = on_finish
        self._finished = False
        # Let a JSON reporter render the full trace at the end.
        if hasattr(inner, "bind_trace"):
            inner.bind_trace(trace)

    # intercepted -----------------------------------------------------------
    def event(self, ev: dict) -> None:
        self.trace.record_raw(ev, default_agent=self.trace.agent_id)
        self._inner.event(ev)

    def invoked(self, exec_id: str) -> None:
        self.trace.meta.setdefault("exec_id", exec_id)
        self._inner.invoked(exec_id)

    def finish(self, status: str, summary: Optional[str] = None) -> None:
        if not self._finished:
            self._finished = True
            self.trace.finalize(status)
            if summary and self.trace.result is None:
                self.trace.meta.setdefault("summary", summary)
        self._inner.finish(status, summary)
        if self._on_finish and self._finished:
            # Persist after the inner reporter has rendered, and only once.
            cb, self._on_finish = self._on_finish, None
            try:
                cb(self.trace)
            except Exception:
                pass

    def set_result(self, result: dict) -> None:
        self.trace.result = result
        # Backfill account id from the result when not already known (the CLI
        # often learns the account only after the run resolves it).
        if not self.trace.account_id and isinstance(result, dict):
            self.trace.account_id = result.get("account_id", "") or ""

    # everything else delegates ---------------------------------------------
    def __getattr__(self, name):
        return getattr(self._inner, name)


# ── factory ───────────────────────────────────────────────────────────────────
def make_reporter(output_format: str = "human", *, workflow: str = "",
                  agent_id: str = "", account_id: str = "", profile: str = "",
                  meta: Optional[dict] = None, quiet: bool = False,
                  verbose: bool = False, stream: Optional[TextIO] = None,
                  persist: bool = True):
    """Build a trace-recording reporter for the given output format.

    Returns a :class:`TracingReporter` wrapping the presentation reporter
    selected by ``output_format``. The wrapped ``.trace`` attribute is the live
    :class:`AgentTrace`; on ``finish`` it is finalized and (when ``persist``)
    written to the local trace store.
    """
    fmt = (output_format or "human").lower()
    if fmt not in OUTPUT_FORMATS:
        raise ValueError(f"unknown output format {output_format!r}; "
                         f"expected one of {OUTPUT_FORMATS}")

    if fmt == "human":
        from headlabs.progress import ProgressReporter
        inner = ProgressReporter(stream=stream, quiet=quiet, verbose=verbose)
    elif fmt == "stream-json":
        inner = StreamJsonReporter(stream=stream, quiet=quiet, verbose=verbose)
    else:  # json
        inner = JsonReporter(stream=stream, quiet=quiet, verbose=verbose)

    trace = AgentTrace(workflow=workflow, agent_id=agent_id,
                       account_id=account_id, profile=profile,
                       meta=dict(meta or {}))

    on_finish = None
    if persist:
        def on_finish(t: AgentTrace) -> None:
            from headlabs import trace_store
            trace_store.save_trace(t)

    return TracingReporter(inner, trace, on_finish=on_finish)
