"""Typed event contract and trace model for agent executions.

This module is the single source of truth for *what an agent run looks like* as
structured data. Every surface (``run``, ``chat``, ``agents test``, the SDK)
normalizes the loosely-typed events emitted by the platform
(``GET /executions/{id}/events``) into the typed model defined here, so the
output is consistent and machine-readable regardless of entry point.

Design (aligned with the OpenTelemetry GenAI semantic conventions and the
OpenAI Agents SDK trace/span model):

- A :class:`AgentTrace` is one end-to-end workflow (e.g. a single ``run`` or one
  ``chat`` turn). It owns a flat, ordered list of :class:`AgentEvent` plus
  rolled-up :class:`TraceMetrics` (tool calls, LLM calls, tokens, cost,
  duration, errors).
- An :class:`AgentEvent` is a single timestamped operation. Events carry span
  identity (``span_id`` / ``parent_span_id``) so a hierarchy can be
  reconstructed, and a discriminated ``type`` that selects the meaning of the
  ``data`` payload.

The serialization is **versioned** (:data:`SCHEMA_VERSION`). The JSON Schema is
exposed via :func:`event_schema` / :func:`trace_schema` and validated in CI, so
the output contract evolves under semantic versioning (additive = safe; a
breaking change requires a major bump).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ── schema version ──────────────────────────────────────────────────────────-
# Bump the MAJOR when an existing field changes meaning or is removed; bump the
# MINOR for additive, backward-compatible changes (new optional fields/types).
SCHEMA_VERSION = "1.0"

# ── event taxonomy ────────────────────────────────────────────────────────────
# The canonical event types. The platform's raw stream uses a looser set
# (``status``, ``step``, ``tool_use``, ``thinking``, ``handoff``,
# ``approval_request``, ``error``); :meth:`AgentEvent.from_raw` maps those onto
# this taxonomy without losing information.
EVENT_TYPES = (
    "agent_start",      # an agent began executing
    "agent_end",        # an agent finished
    "llm_call",         # a model generation (carries token usage when known)
    "tool_use",         # a tool invocation started
    "tool_result",      # a tool invocation returned
    "thinking",         # model reasoning / chain-of-thought summary
    "handoff",          # one agent delegating to another
    "approval_request", # a mutating action awaiting human approval
    "status",           # a milestone / phase marker
    "step",             # a sub-step marker
    "error",            # an error occurred
    "metric",           # an out-of-band metric (tokens, cost, …)
)

# Raw platform event type → canonical type. Unknown types pass through.
_RAW_TYPE_MAP = {
    "status": "status",
    "step": "step",
    "tool_use": "tool_use",
    "tool_result": "tool_result",
    "thinking": "thinking",
    "handoff": "handoff",
    "approval_request": "approval_request",
    "error": "error",
    "llm_call": "llm_call",
    "generation": "llm_call",
    "agent_start": "agent_start",
    "agent_end": "agent_end",
    "metric": "metric",
}

# Rough per-1K-token pricing (USD) used only for *estimated* cost when the
# platform does not report cost directly. Keyed by a model-family substring.
# These are deliberately conservative defaults; the platform may override cost
# by emitting a ``metric`` event with ``cost_usd``.
_MODEL_PRICING = {
    "opus": (0.015, 0.075),       # (input_per_1k, output_per_1k)
    "sonnet": (0.003, 0.015),
    "haiku": (0.0008, 0.004),
    "gpt-4o": (0.0025, 0.010),
    "gpt-4": (0.03, 0.06),
}
_DEFAULT_PRICING = (0.003, 0.015)


def new_trace_id() -> str:
    return "trace_" + uuid.uuid4().hex


def new_span_id() -> str:
    return "span_" + uuid.uuid4().hex[:16]


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate generation cost in USD from token counts and a model name.

    Falls back to a Sonnet-class price when the model is unknown. Returns 0.0
    when there are no tokens to price.
    """
    if not input_tokens and not output_tokens:
        return 0.0
    m = (model or "").lower()
    inp, out = _DEFAULT_PRICING
    for key, price in _MODEL_PRICING.items():
        if key in m:
            inp, out = price
            break
    return round((input_tokens / 1000.0) * inp + (output_tokens / 1000.0) * out, 6)


@dataclass
class AgentEvent:
    """One timestamped operation within a trace.

    ``type`` is one of :data:`EVENT_TYPES`. ``data`` holds the type-specific
    payload (kept as a free dict for forward-compatibility, but populated with
    well-known keys by :meth:`from_raw`). ``span_id`` / ``parent_span_id`` allow
    reconstructing a span tree; the platform does not always supply them, in
    which case they are synthesized.
    """

    type: str
    seq: int = 0
    ts: float = field(default_factory=time.time)
    trace_id: str = ""
    span_id: str = field(default_factory=new_span_id)
    parent_span_id: Optional[str] = None
    agent_id: str = ""
    label: str = ""
    level: str = "info"  # info | warn | error
    tool: Optional[str] = None
    data: dict = field(default_factory=dict)

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def from_raw(cls, raw: dict, *, trace_id: str = "",
                 default_agent: str = "") -> "AgentEvent":
        """Normalize a raw platform event into a typed :class:`AgentEvent`.

        The raw shape is ``{seq, ts, type, label, tool?, level?, detail?,
        agent?}``. Unknown keys are preserved under ``data`` so nothing is lost.
        """
        raw = raw or {}
        raw_type = str(raw.get("type", "status"))
        etype = _RAW_TYPE_MAP.get(raw_type, raw_type)
        detail = raw.get("detail")
        data: dict = dict(detail) if isinstance(detail, dict) else ({} if detail is None else {"value": detail})

        # Preserve any extra top-level keys that aren't part of the envelope.
        _envelope = {"seq", "ts", "type", "label", "tool", "level", "detail", "agent",
                     "span_id", "parent_span_id", "trace_id"}
        for k, v in raw.items():
            if k not in _envelope and k not in data:
                data[k] = v

        return cls(
            type=etype,
            seq=int(raw.get("seq", 0) or 0),
            ts=float(raw.get("ts", time.time()) or time.time()),
            trace_id=trace_id or str(raw.get("trace_id", "")),
            span_id=str(raw.get("span_id") or new_span_id()),
            parent_span_id=raw.get("parent_span_id"),
            agent_id=str(raw.get("agent") or default_agent or ""),
            label=str(raw.get("label") or raw.get("tool") or etype),
            level=str(raw.get("level", "info") or "info"),
            tool=raw.get("tool"),
            data=data,
        )

    # ── derived token/cost accessors ──────────────────────────────────────────
    def input_tokens(self) -> int:
        for k in ("input_tokens", "prompt_tokens", "tokens_in"):
            v = self.data.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    def output_tokens(self) -> int:
        for k in ("output_tokens", "completion_tokens", "tokens_out"):
            v = self.data.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    def model(self) -> str:
        return str(self.data.get("model") or self.data.get("model_id") or "")

    def reported_cost(self) -> Optional[float]:
        v = self.data.get("cost_usd")
        return float(v) if isinstance(v, (int, float)) else None

    def is_error(self) -> bool:
        return self.type == "error" or self.level == "error"

    # ── serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        # Drop empty optional fields to keep the wire format compact.
        if d.get("parent_span_id") is None:
            d.pop("parent_span_id", None)
        if d.get("tool") is None:
            d.pop("tool", None)
        if not d.get("data"):
            d.pop("data", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "AgentEvent":
        return cls(
            type=d.get("type", "status"),
            seq=int(d.get("seq", 0) or 0),
            ts=float(d.get("ts", 0.0) or 0.0),
            trace_id=d.get("trace_id", ""),
            span_id=d.get("span_id") or new_span_id(),
            parent_span_id=d.get("parent_span_id"),
            agent_id=d.get("agent_id", ""),
            label=d.get("label", ""),
            level=d.get("level", "info"),
            tool=d.get("tool"),
            data=d.get("data", {}) or {},
        )


@dataclass
class TraceMetrics:
    """Rolled-up, queryable metrics for a trace.

    Updated incrementally as events are recorded so a partial trace always has
    consistent metrics.
    """

    tool_calls: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: int = 0
    duration_s: float = 0.0
    tools_used: dict = field(default_factory=dict)  # tool name -> call count

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TraceMetrics":
        d = d or {}
        return cls(
            tool_calls=int(d.get("tool_calls", 0) or 0),
            llm_calls=int(d.get("llm_calls", 0) or 0),
            input_tokens=int(d.get("input_tokens", 0) or 0),
            output_tokens=int(d.get("output_tokens", 0) or 0),
            cost_usd=float(d.get("cost_usd", 0.0) or 0.0),
            errors=int(d.get("errors", 0) or 0),
            duration_s=float(d.get("duration_s", 0.0) or 0.0),
            tools_used=dict(d.get("tools_used", {}) or {}),
        )


@dataclass
class AgentTrace:
    """One end-to-end agent workflow: identity + ordered events + metrics.

    Use :meth:`record` to append normalized events (metrics update
    incrementally), then :meth:`finalize` once a terminal status is known. The
    trace is the persisted, comparable, exportable unit of observability.
    """

    trace_id: str = field(default_factory=new_trace_id)
    workflow: str = ""             # "run", "chat", "test", …
    agent_id: str = ""
    schema_version: str = SCHEMA_VERSION
    account_id: str = ""
    profile: str = ""
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    events: list = field(default_factory=list)        # list[AgentEvent]
    metrics: TraceMetrics = field(default_factory=TraceMetrics)
    result: Optional[dict] = None                     # serialized Result
    meta: dict = field(default_factory=dict)          # free-form (question, days, …)

    # ── recording ──────────────────────────────────────────────────────────--
    def record(self, event: AgentEvent) -> AgentEvent:
        """Append an event and fold its contribution into the metrics."""
        if not event.trace_id:
            event.trace_id = self.trace_id
        if not event.seq:
            event.seq = len(self.events) + 1
        self.events.append(event)
        self._fold(event)
        return event

    def record_raw(self, raw: dict, *, default_agent: str = "") -> AgentEvent:
        """Convenience: normalize a raw platform event and record it."""
        return self.record(AgentEvent.from_raw(
            raw, trace_id=self.trace_id, default_agent=default_agent or self.agent_id))

    def _fold(self, ev: AgentEvent) -> None:
        m = self.metrics
        if ev.type == "tool_use":
            m.tool_calls += 1
            name = ev.tool or ev.label or "?"
            m.tools_used[name] = m.tools_used.get(name, 0) + 1
        elif ev.type == "llm_call":
            m.llm_calls += 1
        if ev.is_error():
            m.errors += 1
        it, ot = ev.input_tokens(), ev.output_tokens()
        m.input_tokens += it
        m.output_tokens += ot
        rc = ev.reported_cost()
        if rc is not None:
            m.cost_usd = round(m.cost_usd + rc, 6)
        elif it or ot:
            m.cost_usd = round(m.cost_usd + estimate_cost(ev.model(), it, ot), 6)

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def finalize(self, status: str, result: Optional[dict] = None) -> "AgentTrace":
        self.status = status
        self.ended_at = time.time()
        self.metrics.duration_s = round(self.ended_at - self.started_at, 3)
        if result is not None:
            self.result = result
        return self

    # ── serialization ─────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "workflow": self.workflow,
            "agent_id": self.agent_id,
            "account_id": self.account_id,
            "profile": self.profile,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "metrics": self.metrics.to_dict(),
            "meta": self.meta,
            "result": self.result,
            "events": [e.to_dict() for e in self.events],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AgentTrace":
        t = cls(
            trace_id=d.get("trace_id") or new_trace_id(),
            workflow=d.get("workflow", ""),
            agent_id=d.get("agent_id", ""),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            account_id=d.get("account_id", ""),
            profile=d.get("profile", ""),
            status=d.get("status", "running"),
            started_at=float(d.get("started_at", 0.0) or 0.0),
            ended_at=d.get("ended_at"),
            metrics=TraceMetrics.from_dict(d.get("metrics", {})),
            result=d.get("result"),
            meta=d.get("meta", {}) or {},
        )
        t.events = [AgentEvent.from_dict(e) for e in d.get("events", [])]
        return t


# ── JSON Schema (versioned contract) ──────────────────────────────────────────
def event_schema() -> dict:
    """JSON Schema for a single :class:`AgentEvent`. Used to validate the output
    contract in CI."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "HeadLabs AgentEvent",
        "type": "object",
        "required": ["type", "seq", "ts"],
        "properties": {
            "type": {"type": "string", "enum": list(EVENT_TYPES)},
            "seq": {"type": "integer", "minimum": 0},
            "ts": {"type": "number"},
            "trace_id": {"type": "string"},
            "span_id": {"type": "string"},
            "parent_span_id": {"type": ["string", "null"]},
            "agent_id": {"type": "string"},
            "label": {"type": "string"},
            "level": {"type": "string", "enum": ["info", "warn", "error"]},
            "tool": {"type": ["string", "null"]},
            "data": {"type": "object"},
        },
        "additionalProperties": False,
    }


def trace_schema() -> dict:
    """JSON Schema for a full :class:`AgentTrace`."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "HeadLabs AgentTrace",
        "type": "object",
        "required": ["schema_version", "trace_id", "workflow", "status", "events"],
        "properties": {
            "schema_version": {"type": "string"},
            "trace_id": {"type": "string"},
            "workflow": {"type": "string"},
            "agent_id": {"type": "string"},
            "account_id": {"type": "string"},
            "profile": {"type": "string"},
            "status": {"type": "string"},
            "started_at": {"type": "number"},
            "ended_at": {"type": ["number", "null"]},
            "metrics": {"type": "object"},
            "meta": {"type": "object"},
            "result": {"type": ["object", "null"]},
            "events": {"type": "array", "items": event_schema()},
        },
        "additionalProperties": False,
    }
