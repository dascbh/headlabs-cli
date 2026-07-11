"""Agent name resolution, validation, and "did you mean" suggestions.

Friendly names (``finops``) and platform agent ids (``finops-advisor``) both
resolve to a canonical id to invoke. When a name matches nothing, we fail early
with a clear message and close-match suggestions instead of letting a raw 404
bubble up from the platform.

A small on-disk catalog cache (``~/.headlabs/agents_cache.json``) holds the
remote agent ids so name validation and **tab completion** work without a
network round-trip on the hot path (and offline).
"""

from __future__ import annotations

import difflib
import json
import time
from typing import Optional

from headlabs.config import CONFIG_DIR
from headlabs.agents.registry import AGENT_REGISTRY

_CACHE = CONFIG_DIR / "agents_cache.json"
_CACHE_TTL = 3600  # seconds; a stale cache is still used for completion/suggestions


class AgentNotFound(Exception):
    """Raised when an agent name resolves to no known agent. Carries the close
    matches so callers can render a friendly message."""

    def __init__(self, name: str, suggestions: Optional[list[str]] = None):
        self.name = name
        self.suggestions = suggestions or []
        super().__init__(f"agent {name!r} not found")


# ── local catalog (no network) ────────────────────────────────────────────────
def _local_names() -> set[str]:
    """Names known without any network: registry aliases + their ids."""
    names: set[str] = set()
    for alias, cfg in AGENT_REGISTRY.items():
        names.add(alias)
        if cfg.get("agent_id"):
            names.add(cfg["agent_id"])
        if cfg.get("chat_agent_id"):
            names.add(cfg["chat_agent_id"])
    return names


def _read_cache() -> tuple[list[str], float]:
    try:
        data = json.loads(_CACHE.read_text())
        return list(data.get("ids", [])), float(data.get("ts", 0))
    except Exception:
        return [], 0.0


def _write_cache(ids: list[str]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps({"ids": sorted(set(ids)), "ts": time.time()}))
    except Exception:
        pass


def refresh_catalog(client, *, timeout: int = 15) -> list[str]:
    """Fetch remote agent ids and update the cache. Best-effort: returns the
    cached list (possibly empty) if the platform is unavailable."""
    try:
        remote = client.list_remote_agents(timeout=timeout) or []
        ids = [a.get("id") for a in remote if a.get("id")]
        if ids:
            _write_cache(ids)
            return ids
    except Exception:
        pass
    return _read_cache()[0]


def live_catalog(client, *, timeout: int = 3) -> list[str]:
    """Agent names for **interactive tab-completion**: query the platform live
    on every call (short timeout) and merge with the local registry, updating
    the cache. Falls back to the cached/local catalog if the platform is slow
    or unreachable, so Tab never hangs and always returns something useful.
    """
    names = set(_local_names())
    try:
        remote = client.list_remote_agents(timeout=timeout) or []
        ids = [a.get("id") for a in remote if a.get("id")]
        if ids:
            _write_cache(ids)
            names.update(ids)
            return sorted(names)
    except Exception:
        pass
    names.update(_read_cache()[0])
    return sorted(names)


def catalog_names(client=None, *, refresh: bool = False) -> list[str]:
    """All known agent names for completion: local registry + cached/remote ids.

    Never raises. With ``refresh`` (and a client) it re-queries the platform;
    otherwise it uses the local registry plus the cached ids.
    """
    names = set(_local_names())
    if refresh and client is not None:
        names.update(refresh_catalog(client))
    else:
        names.update(_read_cache()[0])
    return sorted(names)


def suggest(name: str, names: Optional[list[str]] = None, n: int = 3) -> list[str]:
    """Close matches for a mistyped name (substring hits first, then fuzzy)."""
    pool = names if names is not None else catalog_names()
    if not pool:
        return []
    low = name.lower()
    # Substring containment is the strongest signal for typos like
    # "finopadvisor" → "finops-advisor".
    contains = [c for c in pool if low in c.lower() or c.lower() in low]
    fuzzy = difflib.get_close_matches(name, pool, n=n, cutoff=0.5)
    out: list[str] = []
    for c in contains + fuzzy:
        if c not in out:
            out.append(c)
    return out[:n]


# ── resolution + validation ────────────────────────────────────────────────--
def resolve_agent_id(client, name: str, *, kind: str = "run",
                     validate: bool = True) -> str:
    """Resolve a friendly name or platform id to the canonical id to invoke.

    ``kind`` selects ``chat_agent_id`` vs ``agent_id`` for registry aliases.
    With ``validate`` and an online platform, an unknown name raises
    :class:`AgentNotFound` (with suggestions). When the platform can't be
    reached, resolution is best-effort (returns the name as given) so offline
    use still works — a clean error then surfaces at the HTTP layer.
    """
    cfg = AGENT_REGISTRY.get(name)
    if cfg:
        if kind == "chat" and cfg.get("chat_agent_id"):
            return cfg["chat_agent_id"]
        return cfg.get("agent_id", name)

    # Direct platform id that also happens to be a known registry id value.
    if name in _local_names():
        return name

    if not validate:
        return name

    # Unknown locally → validate against the platform (and refresh the cache).
    remote_ids = refresh_catalog(client)
    if not remote_ids:
        # Platform unavailable: don't block; let the request layer report.
        return name
    if name in remote_ids:
        return name

    raise AgentNotFound(name, suggestions=suggest(name, remote_ids + list(_local_names())))
