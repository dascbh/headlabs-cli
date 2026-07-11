"""`headlabs local` command handlers — config / run / chat.

Wired into headlabs.cli's argparse subparsers; kept in its own module so the
already-large cli.py doesn't grow further, and so this feature stays visibly
separate from the platform-facing commands (`run`, `chat`, `agents`, `run --local`).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from headlabs.local.config import load_local_config, save_local_config, update_local_config
from headlabs.local.engine import EngineEvent, QueryEngine
from headlabs.local.permission import PermissionManager
from headlabs.local.provider import OpenAICompatibleProvider, ProviderError
from headlabs.local.render import ChatRenderer
from headlabs.local.tools import ALL_TOOLS
from headlabs.local.autofix import (
    MAX_AUTOFIX_RETRIES, detect_test_command, run_test_command, build_fix_prompt,
)
from headlabs.local.compaction import needs_compaction, build_compaction_messages, format_for_summarization, apply_compaction, SUMMARY_PROMPT
from headlabs.local.undo import UndoStack, is_git_repo, git_checkout_files, extract_edited_files_from_events


# ─── Plain-text renderer for `headlabs local run` (scriptable, piped) ──────


def _render_event(ev: EngineEvent) -> None:
    """Plain-text fallback renderer used by `headlabs local run` (single-shot,
    non-interactive, scriptable). `headlabs local chat` uses `ChatRenderer`."""
    if ev.type == "text":
        print(ev.text, end="", flush=True)
    elif ev.type == "tool_call":
        print(f"\n\033[36m● {ev.tool_name}\033[0m", flush=True)
    elif ev.type == "tool_result":
        color = "\033[31m" if ev.is_error else "\033[2m"
        preview = ev.tool_output.strip().splitlines()
        preview_text = preview[0][:200] if preview else ""
        more = f" (+{len(preview) - 1} lines)" if len(preview) > 1 else ""
        print(f"  {color}{preview_text}{more}\033[0m", flush=True)
    elif ev.type == "permission_denied":
        print(f"\n\033[33m⊘ {ev.tool_output}\033[0m", flush=True)
    elif ev.type == "error":
        print(f"\n\033[31mError: {ev.text}\033[0m", flush=True)
    elif ev.type == "done":
        print()


# ─── Command routing ────────────────────────────────────────────────────────


def cmd_local(args) -> None:
    subcmd = getattr(args, "local_cmd", None)
    if subcmd == "config":
        _cmd_local_config(args)
    elif subcmd == "run":
        _cmd_local_run(args)
    elif subcmd == "chat":
        _cmd_local_chat(args)
    else:
        _cmd_local_status()


def _cmd_local_status() -> None:
    cfg = load_local_config()
    if not cfg.is_configured():
        print("headlabs local is not configured. Run:")
        print("  headlabs local config --base-url <url> --model <model>")
        return
    print(f"base_url: {cfg.base_url}")
    print(f"model:    {cfg.model}")
    print(f"api_key:  {'(set)' if cfg.api_key else '(none)'}")
    print(f"max_iterations: {cfg.max_iterations}")


def _cmd_local_config(args) -> None:
    if not any([args.base_url, args.model, args.api_key, args.max_iterations]):
        _cmd_local_status()
        return
    cfg = update_local_config(
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        max_iterations=args.max_iterations,
    )
    print(f"Saved to ~/.headlabs/local_config.json")
    print(f"  base_url: {cfg.base_url}")
    print(f"  model:    {cfg.model}")


def _build_engine(args) -> QueryEngine:
    cfg = load_local_config()
    if not cfg.is_configured():
        print("headlabs local is not configured. Run:")
        print("  headlabs local config --base-url <url> --model <model>")
        sys.exit(2)

    provider = OpenAICompatibleProvider(cfg)
    cwd = os.getcwd()
    mode = "auto" if getattr(args, "yes", False) else "default"
    permission_manager = PermissionManager(cwd, mode=mode)
    return QueryEngine(
        provider,
        ALL_TOOLS,
        permission_manager,
        cwd=cwd,
        max_iterations=cfg.max_iterations,
    )


# ─── Single-shot run ────────────────────────────────────────────────────────


def _cmd_local_run(args) -> None:
    engine = _build_engine(args)
    try:
        engine.run(args.prompt, on_event=_render_event)
    except ProviderError:
        sys.exit(1)
    finally:
        engine.provider.close()


# ─── Interactive chat ───────────────────────────────────────────────────────

_SLASH_COMMANDS_HELP = """\
  /exit, /quit   — sair do chat
  /clear         — limpar tela e histórico da conversa
  /compact       — modo compacto (tool outputs resumidos, default)
  /verbose       — modo verbose (tool outputs completos)
  /model         — mostrar modelo/server atuais
  /undo          — reverter última edição de arquivo (via git)
  /vi            — ativar keybindings Vim no prompt
  /emacs         — ativar keybindings Emacs (default)
  /help          — mostrar este menu"""


def _print_welcome_banner(console, cfg, permission_mode: str) -> None:
    """Print a styled welcome banner with model, server, and keybindings info."""
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("model", f"[bold]{cfg.model}[/bold]")
    table.add_row("server", cfg.base_url)
    table.add_row("permission", permission_mode)
    table.add_row("max_iter", str(cfg.max_iterations))

    console.print()
    console.print("[bold]headlabs local[/bold]", style="green")
    console.print(table)
    console.print(
        "[dim]↑↓ history • Ctrl+O compact/verbose • /help commands • /exit quit[/dim]"
    )
    console.print()


def _cmd_local_chat(args) -> None:
    cfg = load_local_config()
    if not cfg.is_configured():
        print("headlabs local is not configured. Run:")
        print("  headlabs local config --base-url <url> --model <model>")
        sys.exit(2)

    provider = OpenAICompatibleProvider(cfg)
    cwd = os.getcwd()
    mode = "auto" if getattr(args, "yes", False) else "default"
    permission_manager = PermissionManager(cwd, mode=mode)
    engine = QueryEngine(
        provider,
        ALL_TOOLS,
        permission_manager,
        cwd=cwd,
        max_iterations=cfg.max_iterations,
    )

    from rich.console import Console
    console = Console()

    renderer = ChatRenderer(console, max_iterations=cfg.max_iterations)

    # ── Input setup with prompt_toolkit ──
    history_path = Path.home() / ".headlabs" / "local_chat_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    vi_mode = False  # toggled by /vi and /emacs

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()

        @kb.add("c-o")
        def _toggle_verbose(event):
            """Ctrl+O toggles compact/verbose."""
            renderer.toggle_verbose()
            mode_name = "verbose" if renderer.verbose else "compact"
            # Print feedback below current input
            event.app.output.write(f"\r\n[mode: {mode_name}]\r\n")
            event.app.output.flush()

        session = PromptSession(
            history=FileHistory(str(history_path)),
            key_bindings=kb,
        )

        def get_input() -> str:
            return session.prompt(
                "> ",
                vi_mode=vi_mode,
            )

    except ImportError:
        def get_input() -> str:
            return input("> ")

    # ── Welcome banner ──
    _print_welcome_banner(console, cfg, mode)

    # ── Initialize Tier 1 features ──
    undo_stack = UndoStack(cwd)
    has_git = is_git_repo(cwd)
    test_cmd = detect_test_command(cwd)

    if test_cmd:
        console.print(f"[dim]auto-fix: {test_cmd.command} (from {test_cmd.source})[/dim]")
    if has_git:
        console.print(f"[dim]undo: git disponível (/undo para reverter edições)[/dim]")
    console.print()

    # ── Main loop ──
    try:
        while True:
            try:
                user_input = get_input().strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break

            if not user_input:
                continue

            # ── Slash commands ──
            if user_input.startswith("/"):
                cmd = user_input.lower().split()[0]

                if cmd in ("/exit", "/quit"):
                    break
                elif cmd == "/clear":
                    console.clear()
                    engine.history = [engine.history[0]]  # keep system prompt only
                    console.print("[dim]Conversa limpa.[/dim]")
                    continue
                elif cmd == "/compact":
                    renderer.verbose = False
                    console.print("[dim]Modo compacto ativado.[/dim]")
                    continue
                elif cmd == "/verbose":
                    renderer.verbose = True
                    console.print("[dim]Modo verbose ativado.[/dim]")
                    continue
                elif cmd == "/model":
                    console.print(f"[dim]model: {cfg.model}[/dim]")
                    console.print(f"[dim]server: {cfg.base_url}[/dim]")
                    continue
                elif cmd == "/undo":
                    if not undo_stack.can_undo():
                        console.print("[dim]Nada para desfazer.[/dim]")
                    elif not has_git:
                        console.print("[yellow]Undo requer git — diretório não é um repositório git.[/yellow]")
                    else:
                        entry = undo_stack.pop()
                        success, msg = git_checkout_files(entry.files, cwd)
                        style = "green" if success else "red"
                        console.print(f"[{style}]{msg}[/{style}]")
                    continue
                elif cmd == "/vi":
                    vi_mode = True
                    console.print("[dim]Vim keybindings ativados.[/dim]")
                    continue
                elif cmd == "/emacs":
                    vi_mode = False
                    console.print("[dim]Emacs keybindings ativados (default).[/dim]")
                    continue
                elif cmd == "/help":
                    console.print(_SLASH_COMMANDS_HELP)
                    continue
                else:
                    console.print(f"[dim]Comando desconhecido: {cmd}. Use /help.[/dim]")
                    continue

            # ── Context compaction check (before sending to model) ──
            context_limit = getattr(cfg, "context_length", 16384) or 16384
            if needs_compaction(engine.history, context_limit):
                console.print("[dim]⟳ Compactando contexto...[/dim]")
                to_summarize, _ = build_compaction_messages(engine.history)
                summary_text = format_for_summarization(to_summarize)
                # Ask the model to summarize (one-shot, no tools)
                summary_messages = [
                    {"role": "system", "content": "You are a helpful assistant that summarizes conversations concisely."},
                    {"role": "user", "content": f"{SUMMARY_PROMPT}\n\nConversation to summarize:\n{summary_text}"},
                ]
                summary_result = ""
                try:
                    for event in provider.stream(summary_messages, tools=[]):
                        if event.type == "text_delta":
                            summary_result += event.text
                except ProviderError:
                    summary_result = summary_text[:2000]  # fallback: just truncate

                engine.history = apply_compaction(engine.history, summary_result)
                console.print("[dim]✓ Contexto compactado.[/dim]")

            # ── Render user message and run engine ──
            renderer.begin_turn()
            turn_events: list[EngineEvent] = []

            def _collect_and_render(ev: EngineEvent) -> None:
                turn_events.append(ev)
                renderer.on_event(ev)

            try:
                engine.run(user_input, on_event=_collect_and_render)
            except ProviderError:
                pass  # already rendered via "error" EngineEvent
            finally:
                renderer.end_turn()

            # ── Track edits for /undo ──
            edited_files = extract_edited_files_from_events(turn_events)
            if edited_files:
                undo_stack.push(edited_files)

            # ── Auto-fix loop: if edits were made and a test command exists, run it ──
            if edited_files and test_cmd:
                for attempt in range(MAX_AUTOFIX_RETRIES):
                    test_result = run_test_command(test_cmd, cwd)
                    if test_result.success:
                        console.print(f"[green]✓ Tests passing ({test_cmd.command})[/green]")
                        break
                    else:
                        console.print(
                            f"[yellow]✗ Tests failed (attempt {attempt + 1}/{MAX_AUTOFIX_RETRIES})"
                            f" — asking model to fix...[/yellow]"
                        )
                        fix_prompt = build_fix_prompt(test_result, edited_files)
                        renderer.begin_turn()
                        turn_events = []
                        try:
                            engine.run(fix_prompt, on_event=_collect_and_render)
                        except ProviderError:
                            break
                        finally:
                            renderer.end_turn()
                        # Track new edits from the fix attempt
                        new_edits = extract_edited_files_from_events(turn_events)
                        if new_edits:
                            edited_files = new_edits
                            undo_stack.push(new_edits, description=f"auto-fix attempt {attempt + 1}")
                else:
                    console.print(
                        f"[red]✗ Tests still failing after {MAX_AUTOFIX_RETRIES} auto-fix attempts. "
                        f"Fix manually or try again.[/red]"
                    )

    finally:
        engine.provider.close()
