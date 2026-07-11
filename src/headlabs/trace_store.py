"""Local persistence for agent traces (``~/.headlabs/traces/``).

Each trace is stored as a single JSON file named ``{trace_id}.json`` containing
the full :class:`~headlabs.tracing.AgentTrace` (envelope + events + metrics +
result). A compact, append-friendly index (``index.jsonl``) keeps one summary
line per trace so ``headlabs trace list`` is O(index) instead of having to open
every trace file.

The store is intentionally local-first and dependency-free: traces are owned by
the operator, queryable offline, and never uploaded anywhere by this module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from headlabs.config import CONFIG_DIR
from headlabs.tracing import AgentTrace

TRACES_DIR = CONFIG_DIR / "traces"
_INDEX = TRACES_DIR / "index.jsonl"

# Cap the index so it cannot grow without bound; trace files are kept until the
# operator prunes them, but the index reflects the most recent N.
_MAX_INDEX_ENTRIES = 1000


def traces_dir() -> Path:
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    return TRACES_DIR


def _trace_path(trace_id: str) -> Path:
    # Guard against path traversal from a malformed/hostile trace_id.
    safe = os.path.basename(str(trace_id))
    return traces_dir() / f"{safe}.json"


def _index_entry(trace: AgentTrace) -> dict:
    m = trace.metrics
    return {
        "trace_id": trace.trace_id,
        "workflow": trace.workflow,
        "agent_id": trace.agent_id,
        "account_id": trace.account_id,
        "status": trace.status,
        "started_at": trace.started_at,
        "ended_at": trace.ended_at,
        "duration_s": m.duration_s,
        "tool_calls": m.tool_calls,
        "llm_calls": m.llm_calls,
        "total_tokens": m.total_tokens,
        "cost_usd": m.cost_usd,
        "errors": m.errors,
    }


def save_trace(trace: AgentTrace) -> Path:
    """Persist a trace as JSON and upsert its summary into the index.

    Returns the path written. Best-effort on the index (a corrupt index never
    blocks saving the trace itself).
    """
    path = _trace_path(trace.trace_id)
    path.write_text(json.dumps(trace.to_dict(), indent=2, default=str, ensure_ascii=False))
    try:
        _upsert_index(_index_entry(trace))
    except Exception:
        pass
    return path


def _read_index() -> list[dict]:
    if not _INDEX.exists():
        return []
    out: list[dict] = []
    for line in _INDEX.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_index(entries: list[dict]) -> None:
    traces_dir()
    entries = entries[-_MAX_INDEX_ENTRIES:]
    _INDEX.write_text("\n".join(json.dumps(e, default=str, ensure_ascii=False)
                                for e in entries) + ("\n" if entries else ""))


def _upsert_index(entry: dict) -> None:
    entries = [e for e in _read_index() if e.get("trace_id") != entry.get("trace_id")]
    entries.append(entry)
    _write_index(entries)


def list_traces(limit: int = 20, *, agent_id: Optional[str] = None,
                workflow: Optional[str] = None) -> list[dict]:
    """Return up to ``limit`` trace summaries, newest first, optionally filtered
    by ``agent_id`` and/or ``workflow``."""
    entries = _read_index()
    if agent_id:
        entries = [e for e in entries if e.get("agent_id") == agent_id]
    if workflow:
        entries = [e for e in entries if e.get("workflow") == workflow]
    entries.sort(key=lambda e: e.get("started_at", 0), reverse=True)
    return entries[:limit]


def load_trace(trace_id: str) -> Optional[AgentTrace]:
    """Load a full trace by id, or ``None`` if it does not exist."""
    path = _trace_path(trace_id)
    if not path.exists():
        return None
    return AgentTrace.from_dict(json.loads(path.read_text()))


def resolve_trace(prefix: str) -> Optional[AgentTrace]:
    """Load a trace by exact id or unique id prefix (e.g. ``trace_ab12``).

    Returns the trace on an exact/unique-prefix hit; ``None`` when nothing
    matches or the prefix is ambiguous (callers should report ambiguity).
    """
    exact = load_trace(prefix)
    if exact is not None:
        return exact
    matches = [e["trace_id"] for e in _read_index()
               if str(e.get("trace_id", "")).startswith(prefix)]
    if len(matches) == 1:
        return load_trace(matches[0])
    return None


def latest_trace(*, agent_id: Optional[str] = None,
                 workflow: Optional[str] = None,
                 before: Optional[str] = None) -> Optional[AgentTrace]:
    """Return the most recent trace (optionally filtered), used as the baseline
    for comparisons. ``before`` excludes a specific trace_id (so a fresh run can
    find the prior one)."""
    for e in list_traces(limit=_MAX_INDEX_ENTRIES, agent_id=agent_id, workflow=workflow):
        if before and e.get("trace_id") == before:
            continue
        t = load_trace(e["trace_id"])
        if t is not None:
            return t
    return None


def delete_trace(trace_id: str) -> bool:
    """Delete a trace file and drop it from the index. Returns True if a file
    was removed."""
    path = _trace_path(trace_id)
    existed = path.exists()
    if existed:
        path.unlink()
    try:
        _write_index([e for e in _read_index() if e.get("trace_id") != trace_id])
    except Exception:
        pass
    return existed
