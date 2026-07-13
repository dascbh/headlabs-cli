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
    elif subcmd == "inspect":
        _cmd_local_inspect(args)
    elif subcmd == "backlog":
        _cmd_local_backlog(args)
    elif subcmd == "fix":
        _cmd_local_fix(args)
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


def _build_engine(args, *, tools=None, system_prompt=None, cwd=None) -> QueryEngine:
    cfg = load_local_config()
    if not cfg.is_configured():
        print("headlabs local is not configured. Run:")
        print("  headlabs local config --base-url <url> --model <model>")
        sys.exit(2)

    provider = OpenAICompatibleProvider(cfg)
    cwd = cwd or os.getcwd()
    mode = "auto" if getattr(args, "yes", False) else "default"
    permission_manager = PermissionManager(cwd, mode=mode)
    kwargs = {}
    if system_prompt is not None:
        kwargs["system_prompt"] = system_prompt
    return QueryEngine(
        provider,
        tools if tools is not None else ALL_TOOLS,
        permission_manager,
        cwd=cwd,
        max_iterations=cfg.max_iterations,
        **kwargs,
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


# ─── Inspect a local project ────────────────────────────────────────────────

_SEV_COLOR = {"critical": "\033[31m", "high": "\033[31m",
              "medium": "\033[33m", "low": "\033[2m"}


def _inspect_tools(with_browser: bool):
    """Read-only tool subset for the inspection pass (never edit_file)."""
    from headlabs.local.tools import (
        ReadFileTool, GlobTool, GrepTool, WebFetchTool, BashTool, ReportFindingTool,
    )
    tools = [ReadFileTool, GlobTool, GrepTool, WebFetchTool, BashTool, ReportFindingTool]
    if with_browser:
        from headlabs.local.tools import BrowserDevtoolsTool
        tools.append(BrowserDevtoolsTool)
    return tools


def _fix_tools():
    from headlabs.local.tools import (
        ReadFileTool, GlobTool, GrepTool, EditFileTool, BashTool,
    )
    return [ReadFileTool, GlobTool, GrepTool, EditFileTool, BashTool]


def _render_findings(items: list[dict], role: str) -> None:
    print(f"\n\033[1mInspeção concluída\033[0m — role \033[1m{role}\033[0m")
    if not items:
        print("  Nenhum problema encontrado.\n")
        return
    print(f"  {len(items)} issue(s) registrada(s)\n")
    print(f"  \033[1mIssues\033[0m ({len(items)})")
    for it in items:
        sev = it.get("severity", "medium")
        color = _SEV_COLOR.get(sev, "\033[2m")
        print(f"    {color}[{sev}]\033[0m {it.get('resource', '')} — {it.get('title', '')}")
        if it.get("description"):
            print(f"          {it['description']}")
        if it.get("fix"):
            print(f"          \033[2mfix: {it['fix']}\033[0m")
    print()


def _build_browser_auth(args):
    """Assemble a BrowserAuth from the --auth-* flags, or None if none were
    given. Exits(2) with a clear message on malformed input."""
    from headlabs.local.browser_auth import BrowserAuth
    try:
        auth = BrowserAuth.from_cli(
            storage=getattr(args, "auth_storage", None),
            basic=getattr(args, "auth_basic", None),
            headers=getattr(args, "auth_header", None),
        )
    except ValueError as e:
        print(f"  \033[31m{e}\033[0m")
        sys.exit(2)
    return None if auth.is_empty() else auth


def _serve_and_inspect(args, directory, run):
    """If --serve is set, build+start the dev server, call ``run(url)`` with the
    health-checked URL, then tear the server down. Otherwise call ``run`` with
    the explicit --url (or None). ``run`` takes a single ``url`` argument."""
    if not getattr(args, "serve", False):
        return run(getattr(args, "url", None))
    from headlabs.local.serve import detect_run_commands, ServedApp, ServeError
    no_build = getattr(args, "no_build", False)
    try:
        plan = detect_run_commands(
            directory,
            serve_cmd=getattr(args, "serve_cmd", None),
            port=getattr(args, "port", None),
            build=not no_build,
        )
    except ValueError as e:
        print(f"  \033[31m{e}\033[0m")
        sys.exit(2)
    print(f"  \033[36m⚙\033[0m Servindo app local ({plan.framework}, {plan.manager})")
    try:
        with ServedApp(plan, do_install=getattr(args, "install", False),
                       do_build=not no_build,
                       log_cb=lambda m: print(f"    \033[2m{m}\033[0m")) as app:
            return run(app.url)
    except ServeError as e:
        print(f"  \033[31mFalha ao servir o app: {e}\033[0m")
        sys.exit(1)


def _cmd_local_inspect(args) -> None:
    if getattr(args, "provider", "self-hosted") == "platform":
        return _cmd_local_inspect_platform(args)

    directory = os.path.abspath(getattr(args, "directory", None) or ".")
    if not os.path.isdir(directory):
        print(f"Not a directory: {directory}")
        sys.exit(2)

    role = getattr(args, "role", "qa") or "qa"
    context = getattr(args, "inspect_context", None)
    auth = _build_browser_auth(args)
    _serve_and_inspect(args, directory,
                       lambda url: _do_self_hosted_inspect(args, directory, role, context, url, auth))


def _do_self_hosted_inspect(args, directory, role, context, url, auth) -> None:
    from headlabs.local import backlog as backlog_mod
    from headlabs.local.inspector import (
        build_inspector_prompt, inspect_task_message, fetch_skills, parse_findings_fallback,
    )

    skills = fetch_skills(getattr(args, "skill", None) or [])
    prompt = build_inspector_prompt(role, context=context, url=url, skills=skills)

    # The browser_devtools tool is a module-level singleton; set auth on it
    # before the inspection so any navigation it drives is authenticated.
    if auth is not None and url:
        from headlabs.local.tools import browser_devtools as _bd
        _bd._worker.set_auth(auth)

    engine = _build_engine(args, tools=_inspect_tools(bool(url)),
                           system_prompt=prompt, cwd=directory)

    before_ids = {i.get("id") for i in backlog_mod.load_backlog(directory)}
    final_text = ""
    try:
        final_text = engine.run(inspect_task_message(role, url), on_event=_render_event)
    except ProviderError:
        sys.exit(1)
    finally:
        engine.provider.close()

    after = backlog_mod.load_backlog(directory)
    new_items = [i for i in after if i.get("id") not in before_ids]
    # Fallback: model described issues in prose/JSON instead of calling the tool.
    if not new_items and final_text:
        for f in parse_findings_fallback(final_text):
            new_items.append(backlog_mod.add_finding(directory, role=role, **f))

    # The model doesn't reliably echo the role into report_finding; the CLI owns
    # the authoritative role (`--role`), so stamp it onto this run's items.
    new_ids = [i["id"] for i in new_items]
    backlog_mod.restamp_role(directory, new_ids, role)
    id_set = set(new_ids)
    new_items = [i for i in backlog_mod.load_backlog(directory) if i["id"] in id_set]

    _render_findings(new_items, role)
    if new_items:
        print(f"  \033[36m📋\033[0m {len(new_items)} item(ns) em {backlog_mod.BACKLOG_SUBPATH}")
        print(f"     Ver: headlabs local backlog")

    if getattr(args, "fix", False) and new_items:
        _apply_local_fix(args, directory, [i for i in new_items if i.get("status") != "done"])


def _cmd_local_inspect_platform(args) -> None:
    """Inspect a local project via the HeadLabs platform (Claude-backed agent).
    The cloud runtime can't read the local disk, so the CLI bundles the source
    client-side and ships it to a declarative inspector agent using the same
    invoke+poll pattern as `agents`/`labs`."""
    from headlabs.client import HeadLabsClient
    from headlabs.local import backlog as backlog_mod
    from headlabs.local.inspector import (
        build_code_bundle, ensure_platform_agent, platform_findings_from_result,
    )

    directory = os.path.abspath(getattr(args, "directory", None) or ".")
    if not os.path.isdir(directory):
        print(f"Not a directory: {directory}")
        sys.exit(2)
    role = getattr(args, "role", "qa") or "qa"
    context = getattr(args, "inspect_context", None)
    url = getattr(args, "url", None)

    client = HeadLabsClient()

    # `usability` runs a two-layer inspection: the CLI drives the browser MCP
    # directly for the DETERMINISTIC objective findings (axe WCAG + responsive +
    # perf + runtime — 100% reproducible, no LLM), then a grounded synthesizer
    # agent adds the HEURISTIC layer on top. This removes the LLM from the
    # objective-findings path entirely, so those never vary between runs.
    if role == "usability":
        from headlabs.local.serve import is_local_url
        auth = _build_browser_auth(args)
        if not url and not getattr(args, "serve", False):
            print("  \033[31m--role usability requer --url <URL do front-end rodando> ou --serve\033[0m")
            sys.exit(2)
        _serve_and_inspect(args, directory, lambda u: _run_usability_platform(
            client, directory, u, context, args,
            auth=auth, use_local=(getattr(args, "serve", False) or is_local_url(u))))
        return

    print(f"  \033[36m⚙\033[0m Inspeção via plataforma (Claude) — role \033[1m{role}\033[0m")
    bundle = build_code_bundle(directory)
    print(f"    Empacotados {len(bundle)} bytes de código de {os.path.basename(directory)}")
    try:
        agent_id = ensure_platform_agent(client)
    except Exception as e:
        print(f"  \033[31mNão foi possível provisionar o agente da plataforma: {e}\033[0m")
        sys.exit(1)
    instruction = f"Inspect this project as a {role} specialist and return JSON findings."
    if context:
        instruction += f" User focus: {context}."
    input_data = {"question": f"{instruction}\n\n{bundle}", "role": role}

    try:
        exec_id, tenant_id, stream_id = client.invoke(agent_id, input_data)
    except Exception as e:
        print(f"  \033[31mFalha ao invocar o agente da plataforma: {e}\033[0m")
        sys.exit(1)
    print(f"    Execução: {exec_id}")
    print("    \033[2mAguardando inspeção da plataforma...\033[0m")
    try:
        result = client.poll(exec_id, tenant_id=tenant_id, stream_id=stream_id, timeout=600)
    except Exception as e:
        print(f"  \033[31mErro no poll da execução: {e}\033[0m")
        sys.exit(1)

    before_ids = {i.get("id") for i in backlog_mod.load_backlog(directory)}
    new_items = []
    for f in platform_findings_from_result(result):
        new_items.append(backlog_mod.add_finding(directory, role=role, **f))
    new_items = [i for i in new_items if i.get("id") not in before_ids]
    new_ids = [i["id"] for i in new_items]
    backlog_mod.restamp_role(directory, new_ids, role, origin="platform")
    id_set = set(new_ids)
    new_items = [i for i in backlog_mod.load_backlog(directory) if i["id"] in id_set]

    _render_findings(new_items, role)
    if new_items:
        print(f"  \033[36m📋\033[0m {len(new_items)} item(ns) em {backlog_mod.BACKLOG_SUBPATH}")
        print(f"     Ver: headlabs local backlog")
    elif getattr(result, "summary", ""):
        print(f"  \033[2m{result.summary}\033[0m")

    if getattr(args, "fix", False) and new_items:
        _apply_local_fix(args, directory, [i for i in new_items if i.get("status") != "done"])


def _obtain_browser_signals(url, auth, use_local):
    """Return ``(axe, mobile)`` deterministic browser-check dicts for ``url``.

    - ``use_local`` (localhost or --serve): drive a LOCAL Playwright — the remote
      MCP can't reach the user's machine — and this path also carries auth.
    - otherwise: call the remote browser-devtools MCP (public URL, no auth).

    Both return the identical dict shape ``deterministic_usability_findings``
    consumes, so the objective-findings logic stays single-sourced.
    """
    from headlabs.local.inspector import call_browser_mcp
    if use_local:
        from headlabs.local.browser_probe import run_local_usability_probe
        return run_local_usability_probe(url, auth)
    axe = call_browser_mcp("a11y_audit", {"url": url}, tries=7)
    mobile = call_browser_mcp("inspect_page", {"url": url, "viewport": "mobile", "wait_ms": 1200}, tries=4)
    return axe, mobile


def _run_usability_platform(client, directory, url, context, args, *, auth=None, use_local=False) -> None:
    """Two-layer usability inspection of a LIVE url.

    Layer 1 (deterministic): axe WCAG audit + mobile inspect_page, run either via
    a LOCAL Playwright (localhost/--serve, and the only path that can carry auth)
    or the remote browser-devtools MCP (public URL). The raw signals become
    grounded, reproducible findings — no LLM, so they never vary.
    Layer 2 (heuristic): a grounded synthesizer agent receives those same tool
    results and adds only what a rules engine can't catch (content clarity,
    form burden, missing states). The two layers are merged and deduped.
    """
    import json
    from headlabs.local import backlog as backlog_mod
    from headlabs.local.inspector import (
        deterministic_usability_findings,
        ensure_usability_agent, platform_findings_from_result,
    )
    role = "usability"

    where = "browser local" if use_local else "MCP remoto"
    print(f"  \033[36m⚙\033[0m Inspeção de usabilidade — {url}")
    print(f"    \033[2mCamada determinística ({where}): axe-core (WCAG) + inspeção mobile...\033[0m")
    axe, mobile = _obtain_browser_signals(url, auth, use_local)
    for probe, res in (("a11y_audit", axe), ("inspect_page", mobile)):
        if isinstance(res, dict) and res.get("error"):
            print(f"  \033[33m⚠ browser {probe}: {res['error']}\033[0m")

    det = deterministic_usability_findings(axe, mobile)
    print(f"    \033[2m{len(det)} finding(s) objetivo(s) (determinístico)\033[0m")

    # Heuristic layer — grounded on the SAME tool results (agent is a synthesizer,
    # not a tool-caller), so it can't hallucinate or duplicate the objective set.
    heur = []
    tool_ctx = json.dumps({"a11y_audit": axe, "inspect_mobile": mobile},
                          ensure_ascii=False)[:14000]
    try:
        agent_id = ensure_usability_agent(client)
        instruction = (f"Live URL: {url}\nBrowser check results already computed:\n{tool_ctx}\n\n"
                       "Return ONLY additional HEURISTIC usability findings as a JSON array. "
                       "Do NOT repeat WCAG/axe, responsive, runtime or performance issues "
                       "already present in the data above.")
        if context:
            instruction += f"\nUser focus: {context}."
        exec_id, tenant_id, stream_id = client.invoke(agent_id, {"question": instruction, "url": url})
        print(f"    \033[2mCamada heurística (síntese Claude) — execução {exec_id}...\033[0m")
        result = client.poll(exec_id, tenant_id=tenant_id, stream_id=stream_id, timeout=600)
        heur = platform_findings_from_result(result)
        print(f"    \033[2m{len(heur)} finding(s) heurístico(s)\033[0m")
    except Exception as e:
        print(f"  \033[33m⚠ Camada heurística indisponível ({e}); usando só a determinística\033[0m")

    before_ids = {i.get("id") for i in backlog_mod.load_backlog(directory)}
    for f in det + heur:
        backlog_mod.add_finding(directory, role=role, **f)
    new_items = [i for i in backlog_mod.load_backlog(directory) if i.get("id") not in before_ids]
    new_ids = [i["id"] for i in new_items]
    backlog_mod.restamp_role(directory, new_ids, role, origin="platform")
    new_items = [i for i in backlog_mod.load_backlog(directory) if i["id"] in set(new_ids)]

    _render_findings(new_items, role)
    if new_items:
        print(f"  \033[36m📋\033[0m {len(new_items)} item(ns) em {backlog_mod.BACKLOG_SUBPATH}")
        print("     Ver: headlabs local backlog")
    else:
        print("  \033[2mNenhum problema de usabilidade encontrado.\033[0m")

    if getattr(args, "fix", False) and new_items:
        _apply_local_fix(args, directory, [i for i in new_items if i.get("status") != "done"])


def _cmd_local_backlog(args) -> None:
    from headlabs.local import backlog as backlog_mod
    directory = os.path.abspath(getattr(args, "directory", None) or ".")
    items = backlog_mod.load_backlog(directory)
    if not items:
        print("Backlog vazio — rode: headlabs local inspect")
        return
    open_items = [b for b in items if b.get("status") != "done"]
    done_items = [b for b in items if b.get("status") == "done"]
    print(f"\033[1mBacklog local\033[0m ({len(open_items)} aberto(s), {len(done_items)} concluído(s))\n")
    for it in open_items:
        sev = it.get("severity", "medium")
        color = _SEV_COLOR.get(sev, "\033[2m")
        print(f"  {color}[{sev}]\033[0m {it.get('resource', '')} — {it.get('title', '')}")
        if it.get("fix"):
            print(f"        \033[2mfix: {it['fix']}\033[0m")
    if done_items:
        print(f"\n  \033[2m{len(done_items)} item(ns) concluído(s)\033[0m")


def _cmd_local_fix(args) -> None:
    from headlabs.local import backlog as backlog_mod
    directory = os.path.abspath(getattr(args, "directory", None) or ".")
    items = [b for b in backlog_mod.load_backlog(directory) if b.get("status") != "done"]
    if not items:
        print("Backlog vazio — nada para corrigir. Rode: headlabs local inspect")
        return
    _apply_local_fix(args, directory, items)


def _apply_local_fix(args, directory: str, items: list[dict]) -> None:
    """Apply fixes for the given backlog items, then run the test loop and mark
    items done if the suite goes green. Reuses autofix.py's edit→test→fix cycle."""
    from headlabs.local import backlog as backlog_mod
    from headlabs.local.inspector import build_fix_prompt_from_findings, FIX_SYSTEM_PROMPT

    if not items:
        return
    print(f"\n  \033[36m⚙\033[0m Corrigindo {len(items)} item(ns)...")
    engine = _build_engine(args, tools=_fix_tools(),
                           system_prompt=FIX_SYSTEM_PROMPT, cwd=directory)
    try:
        engine.run(build_fix_prompt_from_findings(items), on_event=_render_event)
    except ProviderError:
        sys.exit(1)
    finally:
        engine.provider.close()

    passed = _run_autofix_loop(args, directory)
    if passed:
        for it in items:
            backlog_mod.set_status(directory, it["id"], "done")
        print(f"  \033[32m✓\033[0m {len(items)} item(ns) marcado(s) como done (testes verdes).")
    elif passed is None:
        print("  \033[2m(sem comando de teste detectado — itens deixados abertos para revisão)\033[0m")
    else:
        print("  \033[33mTestes ainda falhando — itens deixados abertos para revisão.\033[0m")


def _run_autofix_loop(args, directory: str):
    """Run the detected test command; on failure, feed the output back and retry
    (up to MAX_AUTOFIX_RETRIES). Returns True (green), False (still red), or None
    (no test command detected)."""
    cmd = detect_test_command(directory)
    if not cmd:
        return None
    for attempt in range(MAX_AUTOFIX_RETRIES):
        result = run_test_command(cmd, directory)
        if result.success:
            print(f"  \033[32m✓\033[0m Testes passaram ({cmd.command})")
            return True
        print(f"  \033[33m✗\033[0m Testes falharam — tentando corrigir ({attempt + 1}/{MAX_AUTOFIX_RETRIES})")
        from headlabs.local.inspector import FIX_SYSTEM_PROMPT
        engine = _build_engine(args, tools=_fix_tools(),
                               system_prompt=FIX_SYSTEM_PROMPT, cwd=directory)
        try:
            engine.run(build_fix_prompt(result, []), on_event=_render_event)
        except ProviderError:
            break
        finally:
            engine.provider.close()
    return run_test_command(cmd, directory).success


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
