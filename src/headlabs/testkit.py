"""Closed-loop agent testing: structured evaluation reports + baseline diff.

``headlabs agents test`` invokes an agent, has a critic score it across fixed
dimensions, and (with ``--fix``) patches the agent and re-tests. Historically
the result was ANSI-only and the re-test used a *different* scoring scale, so
there was no way to tell whether a fix actually helped.

This module makes the loop rigorous and verifiable:

- :class:`TestEvaluation` is the normalized, schema-stable evaluation (overall
  score, verdict, per-dimension scores+evidence, issues, fix instructions,
  measured exec metrics).
- Every test is persisted as an :class:`~headlabs.tracing.AgentTrace`
  (``workflow="test"``) so runs are listed/compared with the same ``trace``
  tooling as everything else.
- :func:`compare` produces a structured before/after delta and a closed-loop
  verdict (``IMPROVED`` / ``REGRESSED`` / ``UNCHANGED``), so ``--fix`` can prove
  whether it worked.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from headlabs.tracing import AgentTrace

# Canonical evaluation dimensions (0-100 each), kept stable as a contract.
DIMENSIONS = (
    "task_completion", "reasoning_quality", "tool_correctness",
    "step_efficiency", "output_structure", "accuracy", "safety",
)

# Verdict thresholds (overall score) — single source of truth.
_PASS_AT = 80
_NEEDS_WORK_AT = 60

# A delta this small (absolute score points) counts as noise → UNCHANGED.
_NOISE = 3


@dataclass
class TestEvaluation:
    """Normalized result of one adversarial test of an agent."""

    agent_id: str
    scenario: str = ""
    score: int = 0                      # overall 0-100
    verdict: str = "FAIL"               # PASS | NEEDS_WORK | FAIL
    dimensions: dict = field(default_factory=dict)   # name -> {score, evidence}
    top_issues: list = field(default_factory=list)
    fix_instructions: list = field(default_factory=list)
    exec_time_s: float = 0.0
    tool_calls: int = 0
    exec_trace_id: str = ""             # the agent execution trace, if captured
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


def verdict_for(score: int) -> str:
    if score >= _PASS_AT:
        return "PASS"
    if score >= _NEEDS_WORK_AT:
        return "NEEDS_WORK"
    return "FAIL"


def parse_evaluation(raw_answer: str) -> Optional[dict]:
    """Extract the critic's JSON object from a (possibly fenced) raw answer.

    Returns the parsed dict, or ``None`` when no valid object with a ``score``
    field is found. Tolerant of markdown fences and surrounding prose.
    """
    if not raw_answer:
        return None
    cleaned = raw_answer.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    start = cleaned.find("{")
    if start < 0:
        return None
    depth, end = 0, start
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        obj = json.loads(cleaned[start:end])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or "score" not in obj:
        return None
    return obj


def normalize(raw_eval: dict, *, agent_id: str, scenario: str = "",
              exec_time_s: float = 0.0, tool_calls: int = 0,
              exec_trace_id: str = "") -> TestEvaluation:
    """Build a :class:`TestEvaluation` from the critic's parsed JSON, filling
    in a verdict when missing and coercing dimension scores to ints."""
    raw_eval = raw_eval or {}
    try:
        score = int(round(float(raw_eval.get("score", 0) or 0)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    dims_in = raw_eval.get("dimensions", {})
    dims: dict = {}
    if isinstance(dims_in, dict):
        for name, d in dims_in.items():
            if isinstance(d, dict):
                try:
                    s = int(round(float(d.get("score", 0) or 0)))
                except (TypeError, ValueError):
                    s = 0
                dims[name] = {"score": max(0, min(100, s)),
                              "evidence": str(d.get("evidence", ""))[:300]}

    verdict = raw_eval.get("verdict")
    if verdict not in ("PASS", "NEEDS_WORK", "FAIL"):
        verdict = verdict_for(score)

    return TestEvaluation(
        agent_id=agent_id,
        scenario=scenario,
        score=score,
        verdict=verdict,
        dimensions=dims,
        top_issues=[str(x) for x in (raw_eval.get("top_issues") or [])][:10],
        fix_instructions=[str(x) for x in (raw_eval.get("fix_instructions") or [])][:10],
        exec_time_s=round(exec_time_s, 3),
        tool_calls=int(tool_calls or 0),
        exec_trace_id=exec_trace_id,
    )


# ── persistence (reuses the trace store) ──────────────────────────────────────
def to_trace(ev: TestEvaluation) -> AgentTrace:
    """Wrap an evaluation as a persistable ``workflow="test"`` trace."""
    trace = AgentTrace(workflow="test", agent_id=ev.agent_id,
                       meta={"scenario": ev.scenario,
                             "exec_trace_id": ev.exec_trace_id})
    trace.metrics.tool_calls = ev.tool_calls
    trace.metrics.duration_s = ev.exec_time_s
    trace.finalize(ev.verdict, ev.to_dict())
    return trace


def persist(ev: TestEvaluation) -> AgentTrace:
    from headlabs import trace_store
    trace = to_trace(ev)
    trace_store.save_trace(trace)
    return trace


def baseline(agent_id: str, *, before_trace_id: str = "") -> Optional[TestEvaluation]:
    """Return the most recent prior test evaluation for an agent, if any."""
    from headlabs import trace_store
    t = trace_store.latest_trace(agent_id=agent_id, workflow="test",
                                 before=before_trace_id)
    if t is None or not isinstance(t.result, dict):
        return None
    r = t.result
    return TestEvaluation(
        agent_id=r.get("agent_id", agent_id),
        scenario=r.get("scenario", ""),
        score=int(r.get("score", 0) or 0),
        verdict=r.get("verdict", "FAIL"),
        dimensions=r.get("dimensions", {}) or {},
        top_issues=r.get("top_issues", []) or [],
        fix_instructions=r.get("fix_instructions", []) or [],
        exec_time_s=float(r.get("exec_time_s", 0.0) or 0.0),
        tool_calls=int(r.get("tool_calls", 0) or 0),
        exec_trace_id=r.get("exec_trace_id", ""),
        ts=float(r.get("ts", t.started_at) or t.started_at),
    )


# ── comparison (the closed loop) ──────────────────────────────────────────────
def compare(before: Optional[TestEvaluation], after: TestEvaluation) -> dict:
    """Structured before/after comparison + closed-loop verdict.

    With no baseline, the verdict is ``BASELINE`` (first measurement). Otherwise
    it is ``IMPROVED`` / ``REGRESSED`` / ``UNCHANGED`` based on the overall score
    delta (with a small noise band), plus per-dimension deltas.
    """
    if before is None:
        return {
            "verdict": "BASELINE",
            "score": {"before": None, "after": after.score, "delta": None},
            "dimensions": {k: {"before": None, "after": v.get("score"), "delta": None}
                           for k, v in after.dimensions.items()},
            "tool_calls": {"before": None, "after": after.tool_calls, "delta": None},
            "exec_time_s": {"before": None, "after": after.exec_time_s, "delta": None},
        }

    delta = after.score - before.score
    if delta > _NOISE:
        loop = "IMPROVED"
    elif delta < -_NOISE:
        loop = "REGRESSED"
    else:
        loop = "UNCHANGED"

    dims: dict = {}
    for name in set(before.dimensions) | set(after.dimensions):
        b = before.dimensions.get(name, {}).get("score")
        a = after.dimensions.get(name, {}).get("score")
        d = (a - b) if (isinstance(a, (int, float)) and isinstance(b, (int, float))) else None
        dims[name] = {"before": b, "after": a, "delta": d}

    return {
        "verdict": loop,
        "score": {"before": before.score, "after": after.score, "delta": delta},
        "dimensions": dims,
        "tool_calls": {"before": before.tool_calls, "after": after.tool_calls,
                       "delta": after.tool_calls - before.tool_calls},
        "exec_time_s": {"before": before.exec_time_s, "after": after.exec_time_s,
                        "delta": round(after.exec_time_s - before.exec_time_s, 3)},
    }


def report_json(after: TestEvaluation, *, before: Optional[TestEvaluation] = None,
                comparison: Optional[dict] = None) -> dict:
    """Assemble the full machine-readable test report."""
    return {
        "schema": "headlabs.test/v1",
        "agent_id": after.agent_id,
        "scenario": after.scenario,
        "evaluation": after.to_dict(),
        "baseline": before.to_dict() if before else None,
        "comparison": comparison if comparison is not None else compare(before, after),
    }
