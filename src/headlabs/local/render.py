"""Rich terminal rendering for `headlabs local` chat — Claude Code-quality UX.

Design principles (derived from analysis of Claude Code, Goose, Aider, OpenCode):
1. Compact by default — one line per tool call, output hidden until expanded.
2. Automatic grouping — consecutive same-type tool calls collapse into one line.
3. Loop detection — warn and stop when the model repeats the exact same action.
4. Iteration counter — discrete "step N/max" so the user knows where they are.
5. Verbose on demand — Ctrl+O (or /verbose) expands all tool outputs in session.
6. Clean visual separation — user turns vs. assistant turns vs. tool calls via color/icons.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.text import Text

from headlabs.local.engine import EngineEvent

# ─── Visual constants ───────────────────────────────────────────────────────

MAX_COMPACT_LINES = 3  # tool output lines shown in compact mode
LOOP_THRESHOLD = 3  # same action repeated N times → loop detected

# Status icons (match Claude Code / Kiro TUI convention)
ICON_RUNNING = "⠋"  # static representation (Live uses Spinner for animation)
ICON_OK = "[green]✓[/green]"
ICON_ERROR = "[red]✗[/red]"
ICON_WARN = "[yellow]![/yellow]"
ICON_DENIED = "[yellow]⊘[/yellow]"

# Colors
COLOR_TOOL = "cyan"
COLOR_USER = "green"
COLOR_DIM = "dim"
COLOR_ERROR = "red"


@dataclass
class ToolCallRecord:
    """Tracks a completed tool call for grouping and loop detection."""
    name: str
    input_hash: str  # hash of arguments — for loop detection
    output_preview: str
    is_error: bool = False
    full_output: str = ""


@dataclass
class ToolGroup:
    """A group of consecutive same-name tool calls, rendered as one line."""
    name: str
    count: int = 0
    records: list[ToolCallRecord] = field(default_factory=list)


class ChatRenderer:
    """Stateful renderer for one or more `engine.run()` calls within a chat
    session. Create once per session, reuse across turns.

    Usage:
        renderer = ChatRenderer(console)
        renderer.begin_turn()  # before each engine.run()
        engine.run(msg, on_event=renderer.on_event)
        renderer.end_turn()
    """

    def __init__(self, console: Console | None = None, max_iterations: int = 15):
        self.console = console or Console()
        self.verbose = False  # toggled by Ctrl+O or /verbose
        self.max_iterations = max_iterations

        # Per-turn state (reset on begin_turn)
        self._text_buf: list[str] = []
        self._live: Live | None = None
        self._tool_live: Live | None = None
        self._iteration = 0
        self._pending_tool_name: str = ""
        self._pending_tool_input: dict = {}
        self._recent_hashes: list[str] = []  # for loop detection
        self._current_group: ToolGroup | None = None
        self._loop_detected = False
        self._last_refresh: float = 0.0
        self._thinking_live: Live | None = None
        self._first_event_received = False

    def begin_turn(self) -> None:
        """Reset per-turn state and show a thinking indicator."""
        self._text_buf = []
        self._live = None
        self._tool_live = None
        self._iteration = 0
        self._pending_tool_name = ""
        self._pending_tool_input = {}
        self._recent_hashes = []
        self._current_group = None
        self._loop_detected = False
        self._last_refresh = 0.0
        self._first_event_received = False

        # Show thinking spinner immediately — gives instant visual feedback
        # that the system is working. Disappears on first real event.
        self._thinking_live = Live(console=self.console, auto_refresh=True)
        self._thinking_live.start()
        self._thinking_live.update(
            Spinner("dots", text=Text(" Thinking...", style="dim"))
        )

    def toggle_verbose(self) -> None:
        """Toggle between compact and verbose mode."""
        self.verbose = not self.verbose

    # ─── Private helpers ────────────────────────────────────────────────────

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _stop_tool_live(self) -> None:
        if self._tool_live is not None:
            self._tool_live.stop()
            self._tool_live = None

    def _flush_group(self) -> None:
        """Render accumulated group if any, then reset."""
        if self._current_group is None:
            return
        grp = self._current_group
        self._current_group = None

        if grp.count == 1:
            rec = grp.records[0]
            self._render_single_tool_result(rec)
        else:
            # Grouped display: "✓ read_file ×5 (config.py, engine.py, ...)"
            names = [r.output_preview.split("\n")[0][:30] for r in grp.records[:3]]
            suffix = f", +{grp.count - 3} more" if grp.count > 3 else ""
            summary = ", ".join(names) + suffix
            icon = ICON_ERROR if any(r.is_error for r in grp.records) else ICON_OK
            self.console.print(
                Text.from_markup(
                    f"  {icon} [bold {COLOR_TOOL}]{grp.name}[/bold {COLOR_TOOL}] "
                    f"×{grp.count} [{COLOR_DIM}]({summary})[/{COLOR_DIM}]"
                )
            )

    def _render_single_tool_result(self, rec: ToolCallRecord) -> None:
        """Render one tool call result in compact or verbose mode."""
        icon = ICON_ERROR if rec.is_error else ICON_OK
        style = COLOR_ERROR if rec.is_error else COLOR_DIM

        if self.verbose:
            # Full output
            output_text = rec.full_output[:2000] if rec.full_output else "(empty)"
            self.console.print(
                Text.from_markup(
                    f"  {icon} [bold {COLOR_TOOL}]{rec.name}[/bold {COLOR_TOOL}]"
                )
            )
            for line in output_text.splitlines()[:20]:
                self.console.print(f"    [{style}]{line}[/{style}]")
            if len(output_text.splitlines()) > 20:
                self.console.print(f"    [{COLOR_DIM}](+{len(output_text.splitlines()) - 20} lines)[/{COLOR_DIM}]")
        else:
            # Compact: one line with preview
            lines = rec.full_output.strip().splitlines() if rec.full_output else []
            preview = lines[0][:80] if lines else "(empty)"
            more = ""
            if len(lines) > MAX_COMPACT_LINES:
                more = f" [{COLOR_DIM}](+{len(lines) - 1} lines, Ctrl+O to expand)[/{COLOR_DIM}]"
            self.console.print(
                Text.from_markup(
                    f"  {icon} [bold {COLOR_TOOL}]{rec.name}[/bold {COLOR_TOOL}] "
                    f"[{style}]{preview}[/{style}]{more}"
                )
            )

    def _hash_tool_call(self, name: str, input_data: dict) -> str:
        """Deterministic hash for detecting repeated identical tool calls."""
        raw = json.dumps({"name": name, **input_data}, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _check_loop(self, call_hash: str) -> bool:
        """Returns True if loop detected (same hash repeated LOOP_THRESHOLD times)."""
        self._recent_hashes.append(call_hash)
        # Check if the last N hashes are all the same
        if len(self._recent_hashes) >= LOOP_THRESHOLD:
            tail = self._recent_hashes[-LOOP_THRESHOLD:]
            if len(set(tail)) == 1:
                return True
        return False

    # ─── Public event handler ───────────────────────────────────────────────

    def on_event(self, ev: EngineEvent) -> None:
        if self._loop_detected:
            return  # suppress all events after loop detection

        # Dismiss thinking spinner on first real event
        if not self._first_event_received:
            self._first_event_received = True
            if self._thinking_live is not None:
                self._thinking_live.stop()
                self._thinking_live = None

        if ev.type == "text":
            self._stop_tool_live()
            self._flush_group()
            self._text_buf.append(ev.text)
            # Stream tokens directly to console — NO Live redraw.
            # This eliminates all flickering/duplication artifacts that came from
            # Live trying to overwrite previous output with updated markdown.
            # Markdown rendering happens only at the END (in _stop_live / done event),
            # not during streaming. This matches what the user expects: see text
            # appear progressively left-to-right, then get a clean final render.
            self.console.file.write(ev.text)
            self.console.file.flush()

        elif ev.type == "tool_call":
            self._stop_live()
            self._text_buf = []
            self._iteration += 1
            self._pending_tool_name = ev.tool_name
            self._pending_tool_input = ev.tool_input or {}

            # Check for loop before even showing the spinner
            call_hash = self._hash_tool_call(ev.tool_name, self._pending_tool_input)
            if self._check_loop(call_hash):
                self._loop_detected = True
                self._flush_group()
                self.console.print(
                    Text.from_markup(
                        f"\n  {ICON_WARN} [bold yellow]Loop detectado:[/bold yellow] "
                        f"mesma ação [bold {COLOR_TOOL}]{ev.tool_name}[/bold {COLOR_TOOL}] "
                        f"repetida {LOOP_THRESHOLD}× — interrompendo."
                    )
                )
                return

            # Show spinner with iteration counter
            step_label = f"[{COLOR_DIM}][{self._iteration}/{self.max_iterations}][/{COLOR_DIM}] "
            self._tool_live = Live(console=self.console, auto_refresh=False)
            self._tool_live.start()
            spinner = Spinner(
                "dots",
                text=Text.from_markup(
                    f" {step_label}[{COLOR_TOOL}]{ev.tool_name}[/{COLOR_TOOL}]"
                ),
            )
            self._tool_live.update(spinner, refresh=True)

        elif ev.type == "tool_result":
            self._stop_tool_live()

            rec = ToolCallRecord(
                name=ev.tool_name,
                input_hash=self._hash_tool_call(ev.tool_name, self._pending_tool_input),
                output_preview=ev.tool_output[:100] if ev.tool_output else "",
                is_error=ev.is_error,
                full_output=ev.tool_output or "",
            )

            # Grouping logic
            if self._current_group and self._current_group.name == ev.tool_name:
                self._current_group.count += 1
                self._current_group.records.append(rec)
            else:
                self._flush_group()
                self._current_group = ToolGroup(name=ev.tool_name, count=1, records=[rec])

        elif ev.type == "permission_denied":
            self._stop_live()
            self._stop_tool_live()
            self._flush_group()
            self.console.print(
                Text.from_markup(
                    f"  {ICON_DENIED} [{COLOR_TOOL}]{ev.tool_name or 'tool'}[/{COLOR_TOOL}] "
                    f"[yellow]{ev.tool_output}[/yellow]"
                )
            )

        elif ev.type == "error":
            self._stop_live()
            self._stop_tool_live()
            self._flush_group()
            self.console.print(f"[{COLOR_ERROR}]Error: {ev.text}[/{COLOR_ERROR}]")

        elif ev.type == "done":
            self._stop_live()
            self._stop_tool_live()
            self._flush_group()
            self.console.print()  # trailing blank line

    def end_turn(self) -> None:
        """Flush any open displays. Safe to call multiple times."""
        if self._thinking_live is not None:
            self._thinking_live.stop()
            self._thinking_live = None
        self._stop_live()
        self._stop_tool_live()
        self._flush_group()

    # ─── Convenience: render user message ───────────────────────────────────

    def render_user_message(self, text: str) -> None:
        """Render the user's input with visual distinction."""
        self.console.print(
            Text.from_markup(f"\n[bold {COLOR_USER}]▌[/bold {COLOR_USER}] {text}")
        )

    # Legacy compatibility: `finish()` still works
    finish = end_turn
