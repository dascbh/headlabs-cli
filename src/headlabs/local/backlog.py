"""Local inspection backlog — issues/fixes persisted per project.

Mirrors the platform `labs inspect` backlog (see labsctl.py) but stored
locally in ``.headlabs/local_backlog.json`` — the same per-project scoping
convention as ``todo_write.py``'s ``local_todos.json`` and
``permission.py``'s ``local_permissions.json``. Written by the
``report_finding`` tool during ``headlabs local inspect`` and read back by
``headlabs local backlog`` / ``headlabs local fix``.

Item shape is intentionally the same as the platform backlog item
(``severity``/``resource``/``description``/``fix``/``source``/``status``) so
the local and platform inspection UX can converge later without a data
migration.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

BACKLOG_SUBPATH = Path(".headlabs") / "local_backlog.json"
VALID_SEVERITIES = ("critical", "high", "medium", "low")


def _backlog_path(cwd: str) -> Path:
    return Path(cwd) / BACKLOG_SUBPATH


def _item_id(resource: str, title: str) -> str:
    """Stable id from resource+title so re-running the inspector dedupes the
    same finding instead of piling duplicates into the backlog."""
    return hashlib.sha1(f"{resource}::{title}".encode()).hexdigest()[:12]


def load_backlog(cwd: str) -> list[dict]:
    path = _backlog_path(cwd)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def save_backlog(cwd: str, items: list[dict]) -> None:
    path = _backlog_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False))


def add_finding(
    cwd: str,
    *,
    role: str,
    severity: str,
    title: str,
    detail: str = "",
    fix: str = "",
    file: str = "",
    line: int | None = None,
) -> dict:
    """Append a finding to the backlog, deduped by resource+title. Returns the
    item (whether newly added or already present)."""
    severity = severity if severity in VALID_SEVERITIES else "medium"
    if file:
        resource = f"{file}:{line}" if line else file
    else:
        resource = title
    item = {
        "id": _item_id(resource, title),
        "severity": severity,
        "resource": resource,
        "title": title,
        "description": detail,
        "fix": fix,
        "source": f"inspector/{role} (local)",
        "status": "open",
    }
    items = load_backlog(cwd)
    by_id = {i.get("id"): i for i in items}
    if item["id"] not in by_id:
        items.append(item)
        save_backlog(cwd, items)
        return item
    return by_id[item["id"]]


def restamp_role(cwd: str, item_ids, role: str, origin: str = "local") -> None:
    """Rewrite the ``source`` of the given items to the authoritative inspection
    role and origin. The ``report_finding`` tool records whatever ``role`` the
    model passed (often the default), but the real role is the one the CLI was
    invoked with — so the CLI stamps it after the run instead of trusting the
    model. ``origin`` distinguishes the self-hosted loop ('local') from the
    platform (Claude) provider ('platform')."""
    ids = set(item_ids)
    if not ids:
        return
    items = load_backlog(cwd)
    changed = False
    for it in items:
        if it.get("id") in ids:
            it["source"] = f"inspector/{role} ({origin})"
            changed = True
    if changed:
        save_backlog(cwd, items)


def set_status(cwd: str, item_id: str, status: str) -> bool:
    """Update one item's status (e.g. 'open' -> 'done'). Returns True if found."""
    items = load_backlog(cwd)
    changed = False
    for it in items:
        if it.get("id") == item_id:
            it["status"] = status
            changed = True
    if changed:
        save_backlog(cwd, items)
    return changed
