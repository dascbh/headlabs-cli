"""Permission engine for `headlabs local`.

Decoupled from tool execution logic (the tool only declares *whether* an
invocation needs approval via ``requires_permission``; this module decides
*if* that approval is already granted and, if not, prompts the user).

Decisions of "always allow this tool" are persisted per-project in
``.headlabs/local_permissions.json`` under the current working directory —
scoped to the project being worked on, not global, so a blanket approval in
one repo doesn't silently apply to another.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PERMISSIONS_SUBPATH = Path(".headlabs") / "local_permissions.json"


class PermissionDenied(Exception):
    """Raised when the user rejects a tool invocation."""


@dataclass
class PermissionDecision:
    allowed: bool
    remember: bool = False


def _permissions_path(cwd: str) -> Path:
    return Path(cwd) / PERMISSIONS_SUBPATH


def _load_always_allowed(cwd: str) -> set[str]:
    path = _permissions_path(cwd)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    return set(data.get("always_allow", []))


def _save_always_allowed(cwd: str, tool_names: set[str]) -> None:
    path = _permissions_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"always_allow": sorted(tool_names)}, indent=2))


# A prompt function takes (tool_name, input_data) and returns "yes" / "no" / "always".
PromptFn = Callable[[str, dict], str]


def default_terminal_prompt(tool_name: str, input_data: dict) -> str:
    """Default terminal-based yes/no/always prompt."""
    print(f"\n\033[33m? Allow tool '{tool_name}'?\033[0m")
    for key, value in input_data.items():
        preview = str(value)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        print(f"    {key}: {preview}")
    while True:
        answer = input("  [y]es / [n]o / [a]lways for this tool > ").strip().lower()
        if answer in ("y", "yes"):
            return "yes"
        if answer in ("n", "no", ""):
            return "no"
        if answer in ("a", "always"):
            return "always"
        print("  Please answer y, n, or a.")


class PermissionManager:
    """Modes:

    - ``default``: prompts for every tool call that requires permission,
      unless already remembered as "always allow" for this project.
    - ``auto``: bypasses all prompts (equivalent to Claude Code's
      ``bypassPermissions`` / Cline's full auto-approve). Opt-in per run via
      ``--yes``, never the default — a destructive bash/edit call should
      never run unattended unless explicitly requested.
    """

    def __init__(self, cwd: str, *, mode: str = "default", prompt_fn: PromptFn | None = None):
        if mode not in ("default", "auto"):
            raise ValueError(f"Unknown permission mode: {mode!r}")
        self.cwd = cwd
        self.mode = mode
        self.prompt_fn = prompt_fn or default_terminal_prompt
        self._always_allowed = _load_always_allowed(cwd)

    def check(self, tool_name: str, input_data: dict, *, needs_permission: bool) -> None:
        """Raises ``PermissionDenied`` if the call should not proceed."""
        if not needs_permission:
            return
        if self.mode == "auto":
            return
        if tool_name in self._always_allowed:
            return

        answer = self.prompt_fn(tool_name, input_data)
        if answer == "always":
            self._always_allowed.add(tool_name)
            _save_always_allowed(self.cwd, self._always_allowed)
            return
        if answer != "yes":
            raise PermissionDenied(f"User denied permission for tool '{tool_name}'")
