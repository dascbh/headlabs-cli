"""Unit tests for ChatRenderer (headlabs.local.render).

Tests focus on behavior: compact/verbose mode, grouping, loop detection,
and correct lifecycle (no open Live displays after end_turn). Not
pixel-perfect output assertions (depends on terminal width).
"""
from __future__ import annotations

from rich.console import Console

from headlabs.local.engine import EngineEvent
from headlabs.local.render import ChatRenderer, LOOP_THRESHOLD


def _console() -> Console:
    return Console(width=100, force_terminal=False, no_color=True, record=True)


def _make_tool_events(name: str, output: str, is_error: bool = False) -> list[EngineEvent]:
    return [
        EngineEvent(type="tool_call", tool_name=name, tool_input={"arg": "val"}),
        EngineEvent(type="tool_result", tool_name=name, tool_output=output, is_error=is_error),
    ]


def test_renderer_compact_mode_shows_one_line_per_tool():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.begin_turn()
    for ev in _make_tool_events("read_file", "line1\nline2\nline3\nline4\nline5"):
        renderer.on_event(ev)
    renderer.on_event(EngineEvent(type="done", text=""))
    renderer.end_turn()

    output = console.export_text()
    # In compact mode, should show preview with "+N lines" indicator
    assert "read_file" in output
    assert "+4 lines" in output or "+5 lines" in output or "Ctrl+O" in output


def test_renderer_verbose_mode_shows_full_output():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.verbose = True
    renderer.begin_turn()
    for ev in _make_tool_events("bash", "line1\nline2\nline3\nline4\nline5"):
        renderer.on_event(ev)
    renderer.on_event(EngineEvent(type="done", text=""))
    renderer.end_turn()

    output = console.export_text()
    assert "line1" in output
    assert "line3" in output
    assert "line5" in output


def test_renderer_groups_consecutive_same_tool_calls():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.begin_turn()
    # 4 consecutive read_file calls with DIFFERENT arguments (realistic: reading different files)
    for i in range(4):
        renderer.on_event(EngineEvent(
            type="tool_call", tool_name="read_file", tool_input={"path": f"file_{i}.py"}
        ))
        renderer.on_event(EngineEvent(
            type="tool_result", tool_name="read_file", tool_output=f"content of file {i}"
        ))
    renderer.on_event(EngineEvent(type="done", text=""))
    renderer.end_turn()

    output = console.export_text()
    # Should show grouped display with ×4
    assert "×4" in output
    assert "read_file" in output


def test_renderer_does_not_group_different_tool_calls():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.begin_turn()
    for ev in _make_tool_events("read_file", "content A"):
        renderer.on_event(ev)
    for ev in _make_tool_events("bash", "output B"):
        renderer.on_event(ev)
    renderer.on_event(EngineEvent(type="done", text=""))
    renderer.end_turn()

    output = console.export_text()
    # Both should appear as individual lines, no grouping
    assert "read_file" in output
    assert "bash" in output
    assert "×" not in output


def test_renderer_detects_loop_and_stops():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.begin_turn()

    # Repeat the exact same tool call LOOP_THRESHOLD times
    for _ in range(LOOP_THRESHOLD + 1):
        renderer.on_event(EngineEvent(
            type="tool_call", tool_name="browser_devtools",
            tool_input={"action": "click", "selector": "#signup-button"}
        ))
        renderer.on_event(EngineEvent(
            type="tool_result", tool_name="browser_devtools",
            tool_output="timeout", is_error=True
        ))

    renderer.end_turn()

    output = console.export_text()
    assert "Loop detectado" in output or "loop" in output.lower()
    # After loop detection, further events should be suppressed
    assert renderer._loop_detected is True


def test_renderer_no_false_loop_with_different_args():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.begin_turn()

    # Same tool name but different arguments each time — NOT a loop
    for i in range(LOOP_THRESHOLD + 1):
        renderer.on_event(EngineEvent(
            type="tool_call", tool_name="read_file",
            tool_input={"path": f"file_{i}.py"}
        ))
        renderer.on_event(EngineEvent(
            type="tool_result", tool_name="read_file",
            tool_output=f"content {i}"
        ))

    renderer.end_turn()

    output = console.export_text()
    assert "Loop detectado" not in output
    assert renderer._loop_detected is False


def test_renderer_toggle_verbose():
    renderer = ChatRenderer(_console())
    assert renderer.verbose is False
    renderer.toggle_verbose()
    assert renderer.verbose is True
    renderer.toggle_verbose()
    assert renderer.verbose is False


def test_renderer_user_message_rendering():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.render_user_message("hello world")

    output = console.export_text()
    assert "hello world" in output
    assert "▌" in output  # visual indicator for user turns


def test_renderer_handles_text_only_turn():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.begin_turn()
    renderer.on_event(EngineEvent(type="text", text="Just a plain answer."))
    renderer.on_event(EngineEvent(type="done", text=""))
    renderer.end_turn()

    assert renderer._live is None
    assert renderer._tool_live is None


def test_renderer_handles_error_event():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.begin_turn()
    renderer.on_event(EngineEvent(type="error", text="connection lost", is_error=True))
    renderer.end_turn()

    output = console.export_text()
    assert "connection lost" in output


def test_renderer_handles_permission_denied():
    console = _console()
    renderer = ChatRenderer(console)
    renderer.begin_turn()
    renderer.on_event(EngineEvent(type="tool_call", tool_name="bash"))
    renderer.on_event(EngineEvent(type="permission_denied", tool_name="bash", tool_output="user denied"))
    renderer.end_turn()

    output = console.export_text()
    assert "denied" in output


def test_renderer_iteration_counter_in_spinner():
    """The tool call spinner should include a step counter like [1/15]."""
    console = _console()
    renderer = ChatRenderer(console, max_iterations=15)
    renderer.begin_turn()
    renderer.on_event(EngineEvent(type="tool_call", tool_name="read_file", tool_input={"path": "x"}))
    # At this point, the spinner is live — we can check the iteration counter state
    assert renderer._iteration == 1
    renderer.on_event(EngineEvent(type="tool_result", tool_name="read_file", tool_output="ok"))
    renderer.on_event(EngineEvent(type="tool_call", tool_name="bash", tool_input={"command": "ls"}))
    assert renderer._iteration == 2
    renderer.on_event(EngineEvent(type="tool_result", tool_name="bash", tool_output="ok"))
    renderer.on_event(EngineEvent(type="done", text=""))
    renderer.end_turn()


def test_renderer_begin_turn_resets_state():
    renderer = ChatRenderer(_console())
    renderer.begin_turn()
    renderer._iteration = 5
    renderer._loop_detected = True
    renderer._recent_hashes = ["a", "b", "c"]
    renderer.begin_turn()  # should reset
    assert renderer._iteration == 0
    assert renderer._loop_detected is False
    assert renderer._recent_hashes == []


def test_renderer_multiple_sequential_tool_calls():
    """Many tool calls in one turn — all get rendered without crash."""
    console = _console()
    renderer = ChatRenderer(console)
    renderer.begin_turn()
    for i in range(8):
        renderer.on_event(EngineEvent(
            type="tool_call", tool_name="glob", tool_input={"pattern": f"**/{i}*.py"}
        ))
        renderer.on_event(EngineEvent(
            type="tool_result", tool_name="glob", tool_output=f"file_{i}.py"
        ))
    renderer.on_event(EngineEvent(type="text", text="Done."))
    renderer.on_event(EngineEvent(type="done", text=""))
    renderer.end_turn()

    output = console.export_text()
    assert "glob" in output
    assert "×8" in output  # grouped
