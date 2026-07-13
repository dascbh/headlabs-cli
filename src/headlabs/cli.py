"""HeadLabs CLI entry point."""
# PYTHON_ARGCOMPLETE_OK

import argparse
import json
import os
import sys
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path

from headlabs.config import CONFIG_DIR, REPORTS_DIR, load_config, save_config
from headlabs.agents.registry import AGENT_REGISTRY
from headlabs import labsctl
from headlabs.local_cli import cmd_local


def _print_agent_not_found(exc) -> None:
    """Render a friendly 'agent not found' message with suggestions."""
    print(f"\033[31merro: agente '{exc.name}' não encontrado.\033[0m", file=sys.stderr)
    if exc.suggestions:
        joined = ", ".join(exc.suggestions)
        print(f"  \033[2mVocê quis dizer:\033[0m {joined}", file=sys.stderr)
    print(f"  \033[2mListe todos:\033[0m headlabs agents", file=sys.stderr)


def _print_http_error(exc) -> None:
    """Render an HTTP error from the platform cleanly (no stack trace)."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    url = getattr(resp, "url", "")
    if code == 404 and "/agents/" in str(url) and "/invoke" in str(url):
        # The agent id in the path doesn't exist on the platform.
        import re as _re
        m = _re.search(r"/agents/([^/]+)/invoke", str(url))
        name = m.group(1) if m else "?"
        from headlabs import agentnames
        _print_agent_not_found(agentnames.AgentNotFound(
            name, suggestions=agentnames.suggest(name)))
        return
    detail = ""
    try:
        body = resp.json() if resp is not None else None
        if isinstance(body, dict):
            detail = body.get("message") or body.get("error") or ""
    except Exception:
        detail = ""
    print(f"\033[31merro: a plataforma respondeu HTTP {code}.\033[0m", file=sys.stderr)
    if detail:
        print(f"  {str(detail)[:200]}", file=sys.stderr)
    elif url:
        print(f"  \033[2m{url}\033[0m", file=sys.stderr)


def _agent_completer(prefix, **kwargs):
    """argcomplete completer for agent name arguments.

    On every Tab press it queries the platform **live** (short timeout) for the
    current agent list, merged with the local registry, and falls back to the
    cached/local catalog if the platform is slow or unreachable — so completion
    is always up to date without ever hanging the shell.
    """
    try:
        from headlabs.client import HeadLabsClient
        from headlabs import agentnames
        names = agentnames.live_catalog(HeadLabsClient(), timeout=3)
    except Exception:
        try:
            from headlabs import agentnames
            names = agentnames.catalog_names()
        except Exception:
            names = []
    return [n for n in names if n.startswith(prefix)]


def cmd_run(args):
    """Run an agent."""
    from headlabs.client import HeadLabsClient
    from headlabs.output import make_reporter
    from headlabs import trace_store, agentnames

    agent_name = args.agent
    client = HeadLabsClient()

    # Resolve a friendly alias (e.g. "finops") or a platform id (e.g.
    # "finops-advisor") to the canonical id, validating it exists *before* we
    # spend time resolving AWS credentials and invoking. An unknown name fails
    # fast with "did you mean …" suggestions instead of a raw 404.
    try:
        agent_id = agentnames.resolve_agent_id(client, agent_name, kind="run")
    except agentnames.AgentNotFound as exc:
        _print_agent_not_found(exc)
        sys.exit(2)

    kwargs = {"days": args.days}
    if args.question:
        kwargs["question"] = args.question
    if args.account_id:
        kwargs["account_id"] = args.account_id

    fmt = getattr(args, "output_format", "human") or "human"
    human = fmt == "human"
    # Every run is observed through a trace-recording reporter; the chosen
    # format only changes presentation (human TTY / json / stream-json), while
    # a structured AgentTrace is always captured and persisted.
    reporter = make_reporter(
        fmt, workflow="run", agent_id=agent_id, profile=args.profile,
        meta={"days": args.days, "question": args.question,
              "account_id": args.account_id},
        quiet=getattr(args, "quiet", False),
        verbose=getattr(args, "verbose", False),
    )
    if human:
        reporter.header(f"{agent_id}  ·  {args.profile}  ·  {args.days}d")

    if getattr(args, "local", False):
        result = _run_local(agent_id, args, reporter, kwargs)
    else:
        result = client.run(agent_id, args.profile, reporter=reporter,
                            approval_handler=reporter.prompt_approval, **kwargs)

    # Attach the structured result to the trace and re-persist so the stored
    # trace is complete (the in-poll finalize may have run before the result
    # was known on the local path).
    from dataclasses import asdict
    if hasattr(reporter, "set_result"):
        reporter.set_result(asdict(result))
        if not reporter.trace.account_id:
            reporter.trace.account_id = result.account_id
        trace_store.save_trace(reporter.trace)
    trace_id = getattr(getattr(reporter, "trace", None), "trace_id", "")

    if result.status == "timeout":
        if human:
            print("Error: agent timed out. Try again or check headlabs.ai dashboard.")
        sys.exit(1)
    if result.status == "failed":
        if human:
            print(f"Error: agent failed: {result.summary[:150] if result.summary else 'unknown'}")
        sys.exit(1)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    # Always save both HTML + JSON report artifacts.
    html_path = str(REPORTS_DIR / f"{agent_name}_{ts}.html")
    json_path = str(REPORTS_DIR / f"{agent_name}_{ts}.json")
    result.to_html(html_path)
    result.to_json(json_path)

    # Machine formats (json / stream-json) already emitted the full trace via
    # the reporter; printing anything else would corrupt the stream.
    if not human:
        return

    # Final summary block (suppressed under --quiet, where we just emit paths).
    if getattr(args, "quiet", False):
        print(html_path)
        print(json_path)
    else:
        reporter.summary(
            text=result.summary,
            findings=result.insights,
            savings=result.total_saving_usd,
            reports=[html_path, json_path],
        )
        if trace_id:
            print(f"  \033[2mTrace: {trace_id[:14]}  ·  headlabs trace show {trace_id[:14]}\033[0m")


def _run_local(agent_id, args, reporter, kwargs):
    """Run the agent locally via Docker, streaming NDJSON events from stdout."""
    import subprocess
    import json as _json
    import boto3
    from headlabs.client import _ephemeral_credentials
    from headlabs.result import Result
    from headlabs.config import load_config

    profile = args.profile
    session = boto3.Session(profile_name=profile)
    sts = session.client("sts")
    account_id = kwargs.pop("account_id", None) or sts.get_caller_identity()["Account"]
    reporter.phase("Perfil AWS resolvido", account_id)

    creds = _ephemeral_credentials(session)
    if not creds:
        reporter.phase("Sem credenciais AWS", "execução pode falhar")

    cfg = load_config()

    # Resolve agent image: local ./agents/<id>/ or pre-built ECR tag
    local_dir = os.path.join(os.getcwd(), "agents", agent_id)
    image_tag = f"headlabs-local:{agent_id}"
    if os.path.isfile(os.path.join(local_dir, "Dockerfile")):
        reporter.phase("Build local", f"./agents/{agent_id}/")
        r = subprocess.run(
            ["docker", "build", "--platform", "linux/arm64", "-t", image_tag, "."],
            cwd=local_dir, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"\033[31merro: docker build falhou\033[0m\n{r.stderr[-300:]}")
            sys.exit(1)
    else:
        print(f"\033[31merro: ./agents/{agent_id}/ não encontrado.\033[0m")
        print(f"\033[2m  Baixe o agente primeiro: headlabs agents pull {agent_id}\033[0m")
        sys.exit(2)

    # Build the input event (same as client.run builds)
    input_data = {
        "tenant_id": "ALL",
        "lookback_days": kwargs.get("days", 30),
        "aws_region": session.region_name or "us-east-1",
        "account_id": account_id,
    }
    if creds:
        input_data.update(creds)
    if kwargs.get("question"):
        input_data["question"] = kwargs["question"]
    event = {"input": input_data, "_stream_id": "local"}

    # Docker run: stdin=event, stdout=NDJSON stream, env vars injected
    env_args = []
    env_vars = {
        "AGENT_ID": agent_id,
        "HEADLABS_LOCAL": "1",
        "HEADLABS_API_KEY": cfg.get("api_key", ""),
        "HEADLABS_API_URL": cfg.get("api_url", "https://api.headlabs.ai/api/v1"),
        "AWS_DEFAULT_REGION": session.region_name or "us-east-1",
    }
    if creds:
        env_vars["AWS_ACCESS_KEY_ID"] = creds.get("aws_access_key_id", "")
        env_vars["AWS_SECRET_ACCESS_KEY"] = creds.get("aws_secret_access_key", "")
        if creds.get("aws_session_token"):
            env_vars["AWS_SESSION_TOKEN"] = creds["aws_session_token"]
    for k, v in env_vars.items():
        env_args += ["-e", f"{k}={v}"]

    reporter.phase("Executando local", image_tag)
    reporter.begin_wait("Agente local processando…")

    cmd = ["docker", "run", "--rm", "-i", "--platform", "linux/arm64"] + env_args + [
        image_tag, "python", "-m", "headlabs_sdk.sdk.invoke_local"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    proc.stdin.write(_json.dumps(event))
    proc.stdin.close()

    # Stream NDJSON events line-by-line
    output = None
    error = None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "result":
            output = ev.get("output", {})
        elif etype == "error":
            error = ev.get("error", "unknown error")
        else:
            # Filter XML noise from parallel tool invocations
            lbl = ev.get("label", "")
            dtxt = (ev.get("detail") or {}).get("text", "") if isinstance(ev.get("detail"), dict) else ""
            if "<invoke" in lbl or "<invoke" in dtxt:
                continue
            reporter.event(ev)

    proc.wait()

    # Build the structured result first, attach it to the trace (when the
    # reporter records one), then finalize — so machine output and the
    # persisted trace include the result.
    if error and not output:
        result = Result(status="failed", summary=error)
        status = "failed"
    else:
        output = output or {}
        result = Result(
            status="succeeded",
            raw_output=output,
            insights=output.get("insights") or output.get("findings", []),
            summary=output.get("summary", ""),
            total_saving_usd=output.get("total_saving_usd", 0.0),
            account_id=account_id,
            cost_summary=output.get("cost_summary", {}),
        )
        status = "succeeded"

    if hasattr(reporter, "set_result"):
        from dataclasses import asdict
        reporter.set_result(asdict(result))
    reporter.finish(status)
    return result


def cmd_agents(args):
    """List agents — local registry + remote platform."""
    from headlabs.client import HeadLabsClient

    if hasattr(args, 'subcmd') and args.subcmd == 'create':
        return cmd_agents_create(args)
    if hasattr(args, 'subcmd') and args.subcmd == 'deploy':
        return cmd_agents_deploy(args)
    if hasattr(args, 'subcmd') and args.subcmd == 'init':
        return cmd_agents_init(args)
    if hasattr(args, 'subcmd') and args.subcmd == 'push':
        return cmd_agents_push(args)
    if hasattr(args, 'subcmd') and args.subcmd == 'pull':
        return cmd_agents_pull(args)
    if hasattr(args, 'subcmd') and args.subcmd == 'update':
        return cmd_agents_update(args)
    if hasattr(args, 'subcmd') and args.subcmd == 'test':
        return cmd_agents_test(args)
    if hasattr(args, 'subcmd') and args.subcmd == 'delete':
        return cmd_agents_delete(args)

    client = HeadLabsClient()
    remote = client.list_remote_agents()
    # Keep the tab-completion cache warm with the latest platform agent ids.
    try:
        from headlabs import agentnames
        agentnames._write_cache([a.get("id") for a in (remote or []) if a.get("id")])
    except Exception:
        pass
    # The architects are internal engines behind `agents create`/`mcps create`,
    # not user-facing agents.
    _internal_architects = {_ARCHITECT_AGENT_ID, _MCP_ARCHITECT_AGENT_ID, _AGENT_DESIGNER_AGENT_ID}
    remote = [a for a in (remote or []) if a.get("id") not in _internal_architects]

    if remote:
        print(f"{'ID':<28} {'Status':<10} {'Type':<12} Description")
        print("-" * 80)
        for a in remote:
            print(f"{a.get('id',''):<28} {a.get('status','?'):<10} {a.get('agent_type','code'):<12} {(a.get('description','') or '')[:40]}")
    else:
        print("(Showing local registry — remote unavailable)")
        print(f"{'Name':<18} {'Agent ID':<22} Description")
        print("-" * 70)
        for name, cfg in AGENT_REGISTRY.items():
            print(f"{name:<18} {cfg['agent_id']:<22} {cfg['description']}")


def cmd_agents_delete(args):
    """Hard-delete an agent from the platform (deactivates its runtime and
    removes the record) and the local ./agents/<id>/ project, if present.

    Mirrors _mcps_delete exactly: local removal always runs — including when
    the remote call 404s (the record never existed there, e.g. a `create`
    that failed after scaffolding but before registration). `--local` is
    kept as a no-op flag for backward compatibility.

    Irreversible — gated behind an explicit confirmation unless --yes is
    passed (e.g. for non-interactive/scripted use)."""
    from headlabs.client import HeadLabsClient
    import requests
    client = HeadLabsClient()
    agent_id = args.agent_id
    agent_dir = os.path.join(os.getcwd(), "agents", agent_id)
    has_local = os.path.isdir(agent_dir)

    if not getattr(args, "yes", False):
        print(f"\033[33m  Isto vai deletar permanentemente o agente '{agent_id}' na HeadLabs "
              f"(runtime + registro).\033[0m")
        if has_local:
            print(f"\033[33m  Também vai remover ./agents/{agent_id}/ localmente.\033[0m")
        if not _confirm(f"  Confirma a exclusão de '{agent_id}'?"):
            print("  \033[33mCancelado.\033[0m")
            return

    remote_ok = False
    try:
        client.request("DELETE", f"/agents/{agent_id}")
        print(f"\033[32m✓ Agent '{agent_id}' deletado na HeadLabs.\033[0m")
        remote_ok = True
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            # Never existed remotely (or already gone) — not an error worth
            # stopping for; the local cleanup below is what the user actually
            # needs in this case.
            print(f"\033[2m  Agent '{agent_id}' não existe na HeadLabs (nada a deletar lá).\033[0m")
        else:
            print(f"\033[31merro ao deletar na HeadLabs: {exc}\033[0m")
            return
    except Exception as exc:
        print(f"\033[31merro ao deletar na HeadLabs: {exc}\033[0m")
        return

    if has_local:
        import shutil
        shutil.rmtree(agent_dir)
        print(f"\033[32m✓ Removido: agents/{agent_id}/\033[0m")
    elif remote_ok:
        print(f"\033[2m  agents/{agent_id}/ não existe localmente — nada a remover.\033[0m")


def cmd_agents_create(args):
    """Create an agent.

    Inline (one-shot):  headlabs agents create "quero um agente que..."
    Interactive:        headlabs agents create  (prompts for input)
    Programmatic:      headlabs agents create --id x --prompt "..."
    """
    # Agentic creation (NLP → research → create)
    intent = getattr(args, "intent", None)
    spec_path = getattr(args, "spec", None)
    if spec_path:
        # Spec-driven: the file is the authoritative intent. The architect
        # interprets it and we create straight from the draft (non-interactive).
        args._inline_intent = _read_spec(spec_path)
        args._auto_accept = True
        return cmd_agent_create_interactive(args)
    if intent:
        # Inline: skip the interactive prompt, go straight
        args._inline_intent = intent
        return cmd_agent_create_interactive(args)
    if not getattr(args, "prompt", None) and not getattr(args, "prompt_file", None) and not getattr(args, "id", None):
        return cmd_agent_create_interactive(args)

    from headlabs.client import HeadLabsClient

    if not args.id:
        print("Error: --id required for non-interactive create (or omit all flags for the wizard)", file=sys.stderr)
        sys.exit(1)
    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text()
    if not prompt:
        print("Error: --prompt or --prompt-file required", file=sys.stderr)
        sys.exit(1)

    tools = [t.strip() for t in args.tools.split(",")] if args.tools else []
    client = HeadLabsClient()
    result = client.create_agent(
        agent_id=args.id,
        display_name=args.name or args.id,
        prompt=prompt,
        model=args.model,
        tools=tools,
        description=args.description or "",
    )
    status = result.get("status", "?")
    rid = result.get("runtime_id", "")
    print(f"✓ Agent '{args.id}' created (status={status})")
    if rid:
        print(f"   Runtime: {rid} — agent is immediately invocable")
    if result.get("activation_error"):
        print(f"   ! Activation: {result['activation_error']}")


# Backend agent that powers the agentic creation wizard (not a user-facing agent).
_ARCHITECT_AGENT_ID = "agent-architect"

# Dedicated backend agent for MCP authoring (also internal, not user-facing).
# Unlike the generic agent-architect (instructed ad-hoc via the user prompt on
# every call), this agent's PERSONA embeds the authoring contract, so it does
# not need to be re-taught the rules each turn — shorter prompts, less
# narration, more consistent output. The persona text itself is assembled
# right after _MCP_AUTHORING_KNOWLEDGE is defined (see below).
_MCP_ARCHITECT_AGENT_ID = "mcp-architect"

# Dedicated backend agent for declarative-agent DESIGN (not creation).
#
# WHY THIS EXISTS: the generic agent-architect (_ARCHITECT_AGENT_ID) is a
# conversational, one-shot agent on the platform — its own PERSONA instructs
# it to research AND create directly via its own create_agent/push_agent_source
# tools. Sending it an ad-hoc "return ONLY this JSON" prompt (the pattern the
# CLI used before this agent existed) contradicts that persona: faced with
# conflicting instructions, the model fell back to its default conversational
# greeting instead of either creating the agent or returning the JSON the CLI
# expected — confirmed 2026-07-05, reproduced twice with a real spec, see
# ~/.headlabs/drafts/bad-draft-*.txt from that session.
#
# The fix mirrors _MCP_ARCHITECT_AGENT_ID exactly: a dedicated agent whose
# PERSONA says up front "you are invoked programmatically, not by a human"
# and whose tools are LIMITED TO web_search/web_fetch — no create_agent, no
# push_agent_source. It can only design, never create. The CLI (not the
# agent) remains the sole actor that calls create_agent, exactly like the
# MCP pipeline never lets mcp-architect call push_mcp_source itself.
_AGENT_DESIGNER_AGENT_ID = "agent-designer"


_AGENT_DESIGNER_PERSONA = (
    "You are the Agent Designer: a specialist that designs declarative "
    "AI agents (system prompt + tools + MCPs + optional worker agents) for "
    "the HeadLabs platform.\n\n"
    "You are invoked programmatically by a CLI pipeline, not by a human "
    "chatting. Your output is parsed by code. This means:\n"
    "- Respond with ONLY the ```json design block requested. NO preamble, "
    "NO greeting, NO section titles, NO summary or checklist after the "
    "JSON, NO closing remarks. Any prose outside the block is discarded "
    "and wastes tokens.\n"
    "- You NEVER create the agent yourself. You have no create_agent or "
    "push_agent_source tool, and even if you did, creation is the CLI's "
    "job, not yours — a human always approves the design before anything "
    "is created on the platform.\n"
    "- Never wrap explanations around the block. If you must reason, do it "
    "implicitly by producing a correct design — do not narrate your "
    "reasoning.\n\n"
    "Use web_search/web_fetch to ground the design in real domain "
    "knowledge (terminology, APIs, standards) before writing the system "
    "prompt — a generic prompt with no domain grounding is a failure.\n\n"
    "CRITICAL — if the design references any MCP's tools by name in the "
    "system prompt you write, you MUST call list_mcp_tools(mcp_id) FIRST "
    "and use ONLY the real tool names/descriptions it returns. NEVER guess "
    "or infer plausible-sounding tool names from the MCP's id or "
    "description — a prompt naming tools that don't exist on the real "
    "server causes the resulting agent to hallucinate tool calls that fail "
    "silently or produce wrong results (confirmed real defect, 2026-07-07: "
    "a design for 'mcp-cclasstrib' invented 'consultar_cclasstrib', "
    "'buscar_por_ncm', etc. — none of which exist; the real tools are "
    "'detalhar_cclasstrib', 'consultar_classificacao_tributaria', and "
    "others only list_mcp_tools would have revealed).\n"
)


def _ensure_agent_designer(client) -> bool:
    """Idempotently create the dedicated agent-designer agent on the platform
    if it doesn't exist yet. Returns True if the agent is available (already
    existed or was just created), False if creation failed (caller falls back
    to the generic agent-architect).

    Mirrors _ensure_mcp_architect exactly, including the existence check via
    a direct GET (internal architect agents are private and excluded from
    list_remote_agents()).
    """
    try:
        client.request("GET", f"/agents/{_AGENT_DESIGNER_AGENT_ID}")
        return True
    except Exception:
        pass  # not found (or transient error) — fall through to (re)create
    try:
        client.create_agent(
            agent_id=_AGENT_DESIGNER_AGENT_ID,
            display_name="Agent Designer",
            prompt=_AGENT_DESIGNER_PERSONA,
            tools=["web_search", "web_fetch", "list_mcp_tools"],
            description="Internal agent: designs declarative HeadLabs agents from a spec (design only, never creates).",
        )
        return True
    except Exception:
        return False


def _read_spec(path: str) -> str:
    """Read a specification file (agent/MCP) and return its text.

    Exits with a friendly message if the path is missing/unreadable/empty.
    """
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        print(f"\033[31merro: spec não encontrada: {path}\033[0m", file=sys.stderr)
        sys.exit(2)
    try:
        text = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"\033[31merro: não foi possível ler a spec: {exc}\033[0m", file=sys.stderr)
        sys.exit(2)
    if not text:
        print(f"\033[31merro: spec vazia: {path}\033[0m", file=sys.stderr)
        sys.exit(2)
    return text


def _slugify_id(raw: str) -> str:
    """Normalize an identifier to a safe kebab-case slug.

    Lowercases, replaces any run of non-alphanumeric chars with a single
    hyphen, and trims leading/trailing hyphens. This neutralizes path
    traversal (``../``) and shell/ECR-tag-unsafe characters, since the id is
    used both as a filesystem path (``./mcps/<id>/``) and a docker image tag.
    Returns ``""`` if nothing usable remains.
    """
    import re
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-")


def _repair_json_escapes(s: str) -> str:
    """Double any backslash that isn't part of a valid JSON escape sequence.

    LLMs embedding text/code in a JSON string frequently emit invalid escapes
    (``\\p``, ``\\d``, a lone ``\\`` before a Windows path, etc.), which makes
    the whole object unparseable. Valid JSON escapes are ``\\" \\\\ \\/ \\b \\f
    \\n \\r \\t \\uXXXX`` — anything else gets its backslash doubled so it
    becomes a literal backslash instead of a fatal parse error.
    """
    import re
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)


def _parse_json_draft(answer: str):
    """Extract the first JSON object from an LLM answer.

    Tolerates surrounding prose and ```json fences. Returns a dict, or None if
    no valid JSON object can be parsed.

    Uses ``json.JSONDecoder.raw_decode`` (string/escape aware) rather than
    counting ``{``/``}``: naive brace-matching miscounts braces that live
    *inside* string values. As a safety net it retries once with invalid
    backslash escapes repaired (see :func:`_repair_json_escapes`).
    """
    import json as _json, re
    if not answer:
        return None
    # Strip a leading ```/```json fence and any trailing fence.
    cleaned = re.sub(r'^\s*```[^\n]*\n?', '', answer.strip())
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    decoder = _json.JSONDecoder()
    # Try the raw text first, then a repaired copy. For each, try every '{' as
    # a start position: the first that yields a dict wins (prose or a stray '{'
    # before the object is skipped over).
    for candidate in (cleaned, _repair_json_escapes(cleaned)):
        start = candidate.find("{")
        while start != -1:
            try:
                obj, _ = decoder.raw_decode(candidate, start)
                if isinstance(obj, dict):
                    return obj
            except _json.JSONDecodeError:
                pass
            start = candidate.find("{", start + 1)
    return None


def _extract_fenced_block(answer: str, langs) -> str | None:
    """Return the verbatim contents of the first ```<lang> fenced code block.

    ``langs`` is a tuple of accepted language tags (e.g. ``("python", "py")``).
    Code carried in a fenced block needs no escaping, so this is the robust,
    size-independent channel for large payloads like ``server.py`` — unlike
    inlining code as a JSON string, which breaks on the first bad escape.
    Returns None if no such block is present.
    """
    import re
    if not answer:
        return None
    for lang in langs:
        # Prefer a properly closed block: ```lang\n ... \n```
        m = re.search(rf'```{lang}[^\n]*\n(.*?)\n```', answer,
                      re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)
        # Fallback: an unclosed block (model forgot the closing fence).
        m = re.search(rf'```{lang}[^\n]*\n(.*)', answer,
                      re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).rstrip("` \n")
    return None


def _print_bad_draft(answer: str) -> None:
    """Report an unparseable architect draft. The full raw output is always
    persisted to disk for debugging (never lost), but the terminal only shows
    a short summary + the file path — dumping tens of thousands of raw JSON
    chars on every failure is noise, not signal, for the end user."""
    answer = answer or ""
    drafts_dir = CONFIG_DIR / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = drafts_dir / f"bad-draft-{ts}.txt"
    try:
        path.write_text(answer, encoding="utf-8")
        saved_msg = str(path)
    except OSError as exc:
        saved_msg = f"(falha ao salvar: {exc})"
    preview = answer.strip().splitlines()[0][:160] if answer.strip() else ""
    print(f"  \033[31m✗ Draft inválido ({len(answer)} chars). Não foi possível "
          f"extrair JSON.\033[0m")
    if preview:
        print(f"  \033[2m{preview}{'…' if len(answer.strip()) > 160 else ''}\033[0m")
    print(f"  \033[2mOutput bruto completo salvo em: {saved_msg}\033[0m")


def _confirm(question: str, default: bool = False) -> bool:
    """Terminal yes/no gate. Fail-safe: on non-interactive stdin (EOF) or an
    empty answer, return ``default`` (which callers set to False so nothing is
    created without an explicit 'yes'). Accepts en/pt affirmatives."""
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not ans:
        return default
    return ans in ("y", "yes", "s", "sim")


def _review_agent_draft(agent_type, agent_id, name, description,
                        tools_list, mcps_list, workers_list, prompt) -> None:
    """Print the FULL proposed agent design (including the complete system
    prompt) for the post-analysis approval gate."""
    import textwrap
    print("\n  \033[1mAnálise concluída — revise o agente proposto:\033[0m\n")
    print(f"    Tipo:        {agent_type}")
    print(f"    ID:          {agent_id}")
    print(f"    Nome:        {name}")
    print(f"    Descrição:   {description}")
    print(f"    Tools:       {', '.join(tools_list) or '—'}")
    print(f"    MCPs:        {', '.join(mcps_list) or '—'}")
    if workers_list:
        print(f"    Workers:     {', '.join(workers_list)}")
    print(f"\n    \033[1mSystem prompt ({len(prompt)} chars):\033[0m")
    print(textwrap.indent(prompt, "    "))


def _review_mcp_draft(mcp_id, design, server_code, contract_warnings=None, behavior=None) -> None:
    """Print the FULL proposed MCP design (metadata, deps, auth, config surface,
    tools) plus the complete server.py, the contract lint, and the behavioral
    verification result, for the post-analysis approval gate."""
    import textwrap
    tools = design.get("tools", [])
    config = design.get("config", [])
    auth = (design.get("auth") or {}).get("type", "none")
    print("\n  \033[1mAnálise concluída — revise o MCP proposto:\033[0m\n")
    print(f"    ID:           {mcp_id}")
    print(f"    Nome:         {design.get('name', '')}")
    print(f"    Descrição:    {design.get('description', '')}")
    print(f"    Auth:         {auth}")
    print(f"    Dependencies: {', '.join(design.get('dependencies') or []) or '—'}")
    if config:
        print(f"    Config (env):")
        for c in config:
            flags = []
            if c.get("required"):
                flags.append("required")
            if c.get("secret"):
                flags.append("secret")
            tag = f" ({', '.join(flags)})" if flags else ""
            print(f"      · {c.get('env','')}{tag}  {c.get('description','')}".rstrip())
    print(f"    Tools ({len(tools)}):")
    for t in tools:
        params = ", ".join(p.get("name", "") for p in t.get("params", []))
        effect = "write" if t.get("side_effects") else "read-only"
        idem = "idempotente" if t.get("idempotent") else "NÃO idempotente"
        print(f"      · {t.get('name','')}({params})  [{effect}, {idem}]")
        if t.get("description"):
            print(f"          {t['description']}")

    if contract_warnings:
        print(f"\n    \033[33mLint de contrato ({len(contract_warnings)} avisos):\033[0m")
        for w in contract_warnings:
            print(f"      ⚠ {w}")
    else:
        print(f"\n    \033[32mLint de contrato: sem avisos.\033[0m")

    if behavior is not None:
        if behavior.get("ok"):
            print(f"    \033[32mVerificação comportamental: OK — protocolo MCP respondeu, "
                  f"{len(behavior.get('tools', []))} tools descobertas.\033[0m")
            for w in behavior.get("warnings", []):
                print(f"      ⚠ {w}")
        elif behavior.get("skipped"):
            print(f"    \033[33mVerificação comportamental: não executada "
                  f"({behavior.get('error','?')[:120]}).\033[0m")
        else:
            print(f"    \033[31mVerificação comportamental: FALHOU — "
                  f"{behavior.get('error','?')[:200]}\033[0m")

    lines = server_code.count("\n") + 1
    print(f"\n    \033[1mserver.py ({lines} linhas):\033[0m")
    print("    " + "─" * 64)
    print(textwrap.indent(server_code.rstrip("\n"), "    "))
    print("    " + "─" * 64)


def _unwrap_nested_envelope(text: str) -> str:
    """Undo a real-world model quirk: instead of returning the design/code
    fenced blocks directly, the model sometimes emits its own reply already
    serialized as ``{"message": "...", "answer": "...", "tools_used": []}``
    (the SAME envelope shape ``agent_base.py`` wraps chat replies in) — as a
    single-line JSON string, escaped ``\\n`` and all. This is model output
    variance, not a transport bug: ``str(result)`` on the agent SDK's return
    value can legitimately stringify to that shape.

    Detects the pattern by trying to parse ``text`` as a JSON object with a
    ``message``/``answer`` string field and, if found, returns that field's
    (now real, unescaped) content instead of the outer wrapper. Returns
    ``text`` unchanged if it doesn't look like this envelope, so this is safe
    to call unconditionally before any fenced-block extraction."""
    import json as _json
    stripped = text.strip()
    if not stripped.startswith("{"):
        return text
    try:
        obj = _json.loads(stripped)
    except _json.JSONDecodeError:
        try:
            obj = _json.loads(_repair_json_escapes(stripped))
        except _json.JSONDecodeError:
            return text
    if not isinstance(obj, dict):
        return text
    inner = obj.get("answer") or obj.get("message") or obj.get("response")
    if isinstance(inner, str) and inner.strip():
        return inner
    return text


def _run_architect(client, draft_prompt: str, tenant_id, reporter,
                   agent_id: str = _ARCHITECT_AGENT_ID) -> str:
    """Stream one turn against the given architect agent and return the final
    message (defaults to the generic agent-architect for backward
    compatibility with the agent-creation wizard).

    Progress events are forwarded to ``reporter``. Raises on transport failure
    (the caller decides how to surface it).
    """
    import uuid
    session_id = str(uuid.uuid4())
    answer = ""
    try:
        for event in client.chat_stream(agent_id, session_id, draft_prompt,
                                         context={}, history=[], tenant_id=tenant_id):
            etype = event.get("type")
            if etype == "progress":
                reporter.event(event["event"])
            elif etype == "done":
                answer = event.get("message", "")
            elif etype == "error":
                raise RuntimeError(event.get("error") or "architect failed")
        reporter.finish("succeeded")
    except Exception:
        reporter.finish("failed")
        raise
    return _unwrap_nested_envelope(answer)


def _agent_build_prompt(intent: str, mcps_available: list, agents_available: list,
                        include_knowledge: bool = True) -> str:
    """Build the design prompt for a declarative agent.

    ``include_knowledge`` mirrors _mcp_build_prompt: skip the full framing
    (False) when talking to the dedicated agent-designer, whose PERSONA
    already establishes "respond with only the JSON block, you never create
    anything yourself" — repeating it here would just be narrated back.
    Keep it (True, default) for the generic agent-architect fallback, which
    has no such persona and needs the contract spelled out inline.
    """
    framing = (
        "You are an agent architect. Based on the user's intent, generate a "
        "COMPLETE agent spec as JSON. Respond with ONLY the JSON object "
        "below — no greeting, no preamble, no prose outside it.\n\n"
        if include_knowledge else ""
    )
    return (
        f"{framing}"
        f"USER INTENT: {intent}\n\n"
        f"PLATFORM RESOURCES:\n"
        f"  MCPs available: {mcps_available}\n"
        f"  Agents available (for multi-agent/supervisor): {agents_available}\n"
        f"  Native tools: web_search, web_fetch, invoke_agent, table_get, table_put, kb_retrieve\n\n"
        "DECIDE the agent type based on the intent:\n"
        "- 'single' if the agent works alone with tools/MCPs\n"
        "- 'supervisor' if it needs to coordinate other agents\n"
        "- 'worker' if it's meant to be called by a supervisor\n\n"
        "Return ONLY this JSON:\n"
        "{\n"
        '  "type": "single|supervisor|worker",\n'
        '  "id": "<kebab-case>",\n'
        '  "name": "<display name>",\n'
        '  "description": "<one line>",\n'
        '  "tools_native": ["<tool>", ...],\n'
        '  "mcps": ["<mcp-id>", ...],\n'
        '  "workers": ["<agent-id>", ...],\n'
        '  "prompt": "<full system prompt with MCP tools documented>"\n'
        "}\n\n"
        "RULES:\n"
        "- If supervisor: include invoke_agent in tools_native and list workers.\n"
        "- Select MCPs from the available list that match the intent.\n"
        "- In the prompt, document each MCP's tools by name (query the MCP mentally).\n"
        "- Be specific and actionable in the prompt. No filler.\n"
        "- ONLY include write tools (table_put, container_deploy, storage_upload) if the agent needs to persist/accumulate data across runs. If the agent only generates a one-time report, it doesn't need them.\n"
        "- Prefer the minimal tool set that accomplishes the goal.\n"
    )


def cmd_agent_create_interactive(args):
    """Structured wizard: describe → AI drafts (type, tools, MCPs, prompt) → review."""
    import uuid, json as _json, re
    from headlabs.client import HeadLabsClient
    from headlabs.progress import ProgressReporter
    from headlabs.config import get_tenant, load_config

    client = HeadLabsClient()
    tenant_id = getattr(args, "tenant", None) or get_tenant()

    print("\033[1m  HeadLabs · Agent Creation Wizard\033[0m\n")

    # ── STEP 1: Intent ────────────────────────────────────────────────────────
    print("  \033[1mSTEP 1\033[0m  O que o agente deve fazer?")
    inline = getattr(args, "_inline_intent", None)
    if inline:
        intent = inline
        print(f"    → {intent}\n")
    else:
        try:
            intent = input("    → ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not intent:
            return
        print()

    # ── STEP 2: AI drafts everything ──────────────────────────────────────────
    print("  \033[1mSTEP 2\033[0m  Projetando…")
    reporter = ProgressReporter(quiet=False, verbose=False)
    reporter.begin_wait("Analisando intent e recursos da plataforma…")

    # Gather platform resources
    try:
        mcps_available = [m.get("id") for m in client.request("GET", "/mcps")]
    except Exception:
        mcps_available = []
    try:
        agents_available = [a.get("id") for a in client.list_remote_agents()
                            if a.get("id") not in (_ARCHITECT_AGENT_ID, _AGENT_DESIGNER_AGENT_ID)]
    except Exception:
        agents_available = []

    # Prefer the dedicated agent-designer (persona embeds the "design only,
    # never create, JSON-only output" contract — see _AGENT_DESIGNER_PERSONA
    # for why the generic agent-architect can't be trusted with an ad-hoc
    # prompt: its own persona tells it to research AND create directly via
    # its own create_agent tool, which conflicts with a "return only JSON"
    # instruction and previously caused it to fall back to a conversational
    # greeting instead of either creating or designing). Falls back to the
    # generic architect if the dedicated one can't be reached/created.
    use_dedicated = _ensure_agent_designer(client)
    architect_id = _AGENT_DESIGNER_AGENT_ID if use_dedicated else _ARCHITECT_AGENT_ID
    if not use_dedicated:
        print("  \033[33m⚠ agent-designer indisponível — usando agent-architect genérico\033[0m")

    draft_prompt = _agent_build_prompt(intent, mcps_available, agents_available,
                                       include_knowledge=not use_dedicated)

    answer = ""
    try:
        answer = _run_architect(client, draft_prompt, tenant_id, reporter, agent_id=architect_id)
    except Exception as exc:
        print(f"  \033[31m✗ {exc}\033[0m")
        return

    # Parse draft
    draft = _parse_json_draft(answer)

    if not draft:
        _print_bad_draft(answer)
        return

    # ── STEP 3: Review ────────────────────────────────────────────────────────
    # With --spec, the spec file is authoritative: we present the FULL proposed
    # design and gate creation behind a single terminal yes/no (no field-by-
    # field editing), then create only on explicit approval.
    auto_accept = getattr(args, "_auto_accept", False)
    if auto_accept:
        agent_type = draft.get("type", "single")
        agent_id = _slugify_id(draft.get("id", ""))
        name = draft.get("name", "")
        description = draft.get("description", "")
        tools_list = [t.strip() for t in draft.get("tools_native", []) if str(t).strip()]
        mcps_list = [m.strip() for m in draft.get("mcps", []) if str(m).strip()]
        workers_list = [w.strip() for w in draft.get("workers", []) if str(w).strip()] \
            if agent_type == "supervisor" else []
        prompt = draft.get("prompt", "")
        _review_agent_draft(agent_type, agent_id, name, description,
                            tools_list, mcps_list, workers_list, prompt)
        if not _confirm("\n  Autorizar criação deste agente na HeadLabs?"):
            print("  \033[33mCriação cancelada.\033[0m")
            return
        return _agent_create_finalize(
            client, agent_type, agent_id, name, description,
            tools_list, mcps_list, workers_list, prompt)

    print(f"\n  \033[1mSTEP 3\033[0m  Review (Enter=aceitar, ou digite novo valor)\n")

    agent_type = draft.get("type", "single")
    print(f"    \033[2mTipo sugerido: {agent_type}\033[0m")
    t = input(f"    Tipo [{agent_type}]: ").strip()
    agent_type = t if t else agent_type

    agent_id = input(f"    ID [{draft.get('id','')}]: ").strip() or draft.get("id", "")
    name = input(f"    Nome [{draft.get('name','')}]: ").strip() or draft.get("name", "")
    description = input(f"    Descrição [{draft.get('description','')[:60]}]: ").strip() or draft.get("description", "")

    tools_default = ", ".join(draft.get("tools_native", []))
    tools = input(f"    Tools [{tools_default}]: ").strip() or tools_default
    tools_list = [t.strip() for t in tools.split(",") if t.strip()]

    mcps_default = ", ".join(draft.get("mcps", []))
    mcps = input(f"    MCPs [{mcps_default}]: ").strip() or mcps_default
    mcps_list = [m.strip() for m in mcps.split(",") if m.strip()]

    workers_list = []
    if agent_type == "supervisor":
        workers_default = ", ".join(draft.get("workers", []))
        workers = input(f"    Workers [{workers_default}]: ").strip() or workers_default
        workers_list = [w.strip() for w in workers.split(",") if w.strip()]

    prompt = draft.get("prompt", "")
    print(f"\n    \033[2mPrompt ({len(prompt)} chars):\033[0m")
    print(f"    \033[2m{prompt[:150]}…\033[0m")
    edit = input("    (Enter=aceitar, e=editar): ").strip()
    if edit.lower() == "e":
        print("    (Cole o prompt, termine com linha vazia)")
        lines = []
        while True:
            l = input("    ")
            if l == "": break
            lines.append(l)
        if lines:
            prompt = "\n".join(lines)

    # ── Create ────────────────────────────────────────────────────────────────
    return _agent_create_finalize(
        client, agent_type, agent_id, name, description,
        tools_list, mcps_list, workers_list, prompt)


def _agent_create_finalize(client, agent_type, agent_id, name, description,
                           tools_list, mcps_list, workers_list, prompt):
    """Create the declarative agent on the platform and print a summary.

    Shared by the interactive wizard and the spec-driven (auto-accept) path.
    """
    print(f"\n  \033[1mCriando…\033[0m")

    if not agent_id:
        print("  \033[31m✗ draft sem 'id' — não é possível criar o agente\033[0m")
        return
    if agent_type == "supervisor" and "invoke_agent" not in tools_list:
        tools_list.append("invoke_agent")

    try:
        result = client.create_agent(
            agent_id=agent_id, display_name=name or agent_id, prompt=prompt,
            tools=tools_list, description=description)
        if mcps_list:
            client.request("PATCH", f"/agents/{agent_id}", json={
                "manifest": {"tools_native": tools_list, "mcp": [{"server": m} for m in mcps_list]}})

        print(f"\n  \033[32m✓ Agent '{agent_id}' created\033[0m")
        print(f"    Tipo:    {agent_type}")
        print(f"    Tools:   {tools_list}")
        print(f"    MCPs:    {mcps_list}")
        if workers_list:
            print(f"    Workers: {workers_list}")
        if result.get("runtime_id"):
            print(f"    Runtime: {result['runtime_id']}")
        print(f"\n    \033[2mTest: headlabs agents test {agent_id} --tools\033[0m")
    except Exception as exc:
        print(f"  \033[31m✗ {exc}\033[0m")


def cmd_agents_update(args):
    """Incrementally adjust an agent via NLP instruction — full pipeline.

    1. Reads current agent state (prompt, tools, skills, source)
    2. Sends to architect with the instruction
    3. Architect applies the change:
       - Declarative: update_agent (PATCH prompt/tools/skills)
       - Code agent: pull source → modify → push_agent_source (new version)
    4. If code agent was modified: auto-deploy (build + push ECR + update runtime)
    """
    import subprocess
    import uuid
    from headlabs.client import HeadLabsClient
    from headlabs.progress import ProgressReporter
    from headlabs.config import get_tenant

    client = HeadLabsClient()
    agent_id = args.id
    instruction = args.instruction

    # Get current agent state
    try:
        agent = client.request("GET", f"/agents/{agent_id}")
    except Exception as exc:
        print(f"\033[31merro: agente '{agent_id}' não encontrado: {exc}\033[0m")
        sys.exit(2)

    agent_type = agent.get("agent_type", "declarative")
    manifest = agent.get("manifest", {})

    # For code agents, also fetch the source so architect can modify it
    source_info = ""
    if agent_type == "code":
        try:
            src = client.request("GET", f"/agents/{agent_id}/source")
            import base64
            files_preview = {}
            for k, v in (src.get("files", {}) or {}).items():
                content = base64.b64decode(v).decode("utf-8", errors="replace")
                files_preview[k] = content[:2000]  # truncate large files for context
            source_info = f"\nSource (v{src.get('version','?')}):\n"
            for fname, content in files_preview.items():
                source_info += f"\n--- {fname} ---\n{content}\n"
        except Exception:
            source_info = "\n(Source não disponível no S3)\n"

    current_state = (
        f"Agent ID: {agent_id}\n"
        f"Display name: {agent.get('display_name','')}\n"
        f"Description: {agent.get('description','')}\n"
        f"Type: {agent_type}\n"
        f"Tools: {manifest.get('tools_native', [])}\n"
        f"Skills: {manifest.get('skills', [])}\n"
        f"Prompt (current):\n{agent.get('prompt','')[:3000]}\n"
        f"{source_info}"
    )

    message = (
        f"[INSTRUÇÃO DO SISTEMA: Faça um AJUSTE INCREMENTAL no agente '{agent_id}'. "
        f"NÃO recrie do zero. Aplique APENAS a mudança pedida. "
        f"Mantenha tudo que não foi mencionado INTACTO.\n\n"
        f"REGRAS:\n"
        f"- Se o agente é DECLARATIVO: use update_agent(agent_id, ...) com apenas os campos alterados.\n"
        f"- Se o agente é CODE (type=code): modifique os arquivos necessários e use "
        f"push_agent_source(agent_id, files, message) com TODOS os arquivos (preservando os não-alterados). "
        f"Isso cria uma nova versão.\n"
        f"- Se a instrução pede uma NOVA TOOL que não existe no catálogo nativo: "
        f"implemente-a no tools.py e atualize o import/domain_tools no agent.py.\n"
        f"- Após push_agent_source, o deploy será feito automaticamente.]\n\n"
        f"ESTADO ATUAL DO AGENTE:\n{current_state}\n\n"
        f"INSTRUÇÃO DE AJUSTE: {instruction}"
    )

    session_id = str(uuid.uuid4())
    tenant_id = getattr(args, "tenant", None) or get_tenant()
    reporter = ProgressReporter(quiet=False, verbose=False)
    reporter.begin_wait("Aplicando ajuste…")

    answer = ""
    used_push = False
    try:
        for event in client.chat_stream(_ARCHITECT_AGENT_ID, session_id, message,
                                         context={}, history=[], tenant_id=tenant_id):
            et = event.get("type", "")
            if et == "progress":
                ev = event.get("event", {})
                reporter.event(ev)
                if ev.get("tool") == "push_agent_source":
                    used_push = True
            elif et == "done":
                answer = event.get("message", "")
            elif et == "error":
                reporter.finish("failed")
                print(f"  x {event.get('error')}")
                sys.exit(1)
        reporter.finish("succeeded")
    except Exception as exc:
        reporter.finish("failed")
        print(f"  x {exc}")
        sys.exit(1)

    print(f"\n{answer}\n")

    # If source was pushed (code agent), auto-deploy
    if used_push:
        print("\033[2m  Source atualizado. Deployando…\033[0m")
        from types import SimpleNamespace
        deploy_args = SimpleNamespace(
            agent_id=agent_id, tag=None, profile=getattr(args, "profile", None),
            force=False, wait=True)
        cmd_agents_deploy(deploy_args)
    else:
        print("\033[32m✓ Ajuste aplicado.\033[0m")




def _agents_test_reasoning(client, agent_id, profile, args):
    """Evaluate agent reasoning & action quality using LLM-as-judge (5 dimensions).

    Based on DeepEval/AgentEval framework patterns:
    1. Task Completion — did the agent accomplish the goal?
    2. Reasoning Relevancy — each reasoning step ties to the user request?
    3. Reasoning Coherence — logical, step-by-step process?
    4. Tool Correctness — right tools called for the task?
    5. Step Efficiency — avoided unnecessary loops/retries?
    """
    import json as _json, uuid, time
    from headlabs.config import get_tenant

    scenario = getattr(args, "scenario", None) or "Execute sua tarefa principal com raciocínio explícito."

    print(f"\033[1m  Reasoning Test: {agent_id}\033[0m")
    print(f"  Scenario: {scenario}")
    print()

    # 1. Get agent contract
    try:
        agent = client.request("GET", f"/agents/{agent_id}")
        manifest = agent.get("manifest", {})
        # Discover actual MCP tools available
        mcp_tools_available = {}
        for mcp_entry in manifest.get("mcp", []):
            mcp_id = mcp_entry.get("server", mcp_entry) if isinstance(mcp_entry, dict) else mcp_entry
            try:
                import httpx, json as _jj
                resp = httpx.post(f"https://mcps.headlabs.ai/{mcp_id}/mcp",
                                  json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
                                  headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                                  timeout=15)
                for line in resp.text.split("\n"):
                    if line.startswith("data:"):
                        body = _jj.loads(line[5:].strip())
                        mcp_tools_available[mcp_id] = [t.get("name") for t in body.get("result", {}).get("tools", [])]
            except Exception:
                pass
        contract = (f"Agent: {agent_id}\nDescription: {agent.get('description','')}\n"
                    f"Tools: {manifest.get('tools_native',[])}\n"
                    f"MCPs: {manifest.get('mcp',[])}\n"
                    f"MCP Tools Available: {mcp_tools_available}\n"
                    f"Prompt (truncated): {agent.get('prompt','')[:2000]}")
    except Exception:
        contract = f"Agent: {agent_id} (contract unavailable)"

    # 2. Invoke agent and capture full trace (reasoning + tool calls + output)
    session_id = str(uuid.uuid4())
    tenant_id = get_tenant()
    trace = []  # [{type, content}]
    final_output = ""
    t0 = time.time()

    print(f"  \033[2mInvocando {agent_id}…\033[0m")
    try:
        for event in client.chat_stream(agent_id, session_id, scenario,
                                        context={}, history=[], tenant_id=tenant_id):
            etype = event.get("type", "")
            if etype == "progress":
                ev = event.get("event", {})
                trace.append({"type": "progress", "content": str(ev)[:300]})
            elif etype == "done":
                final_output = event.get("message", "")
            elif etype == "error":
                trace.append({"type": "error", "content": event.get("error", "?")})
                break
    except Exception as exc:
        print(f"  \033[31m✗ Invocation failed: {exc}\033[0m")
        return

    elapsed = time.time() - t0
    print(f"  \033[2mCompleted in {elapsed:.1f}s, {len(trace)} trace events\033[0m")
    print()

    # 3. Send trace + output to critic for 5-dimension evaluation
    trace_text = "\n".join(f"[{t['type']}] {t['content']}" for t in trace[:50])
    eval_prompt = (
        f"You are an AI agent evaluator. Score this agent execution on 5 dimensions (0-10 each).\n\n"
        f"═══ AGENT CONTRACT ═══\n{contract}\n\n"
        f"═══ USER SCENARIO ═══\n{scenario}\n\n"
        f"═══ EXECUTION TRACE ({len(trace)} events) ═══\n{trace_text[:4000]}\n\n"
        f"═══ FINAL OUTPUT ═══\n{final_output[:3000]}\n\n"
        f"Score STRICTLY on these 5 dimensions:\n"
        f"1. task_completion (0-10): Did the agent accomplish the user's goal?\n"
        f"2. reasoning_relevancy (0-10): Each reasoning step ties to the request?\n"
        f"3. reasoning_coherence (0-10): Logical, consistent chain of thought?\n"
        f"4. tool_correctness (0-10): Called the right tools with right params?\n"
        f"5. step_efficiency (0-10): Avoided unnecessary loops, retries, calls?\n\n"
        f"Return ONLY valid JSON:\n"
        f'{{"task_completion":{{"score":N,"evidence":"..."}}, '
        f'"reasoning_relevancy":{{"score":N,"evidence":"..."}}, '
        f'"reasoning_coherence":{{"score":N,"evidence":"..."}}, '
        f'"tool_correctness":{{"score":N,"evidence":"..."}}, '
        f'"step_efficiency":{{"score":N,"evidence":"..."}}, '
        f'"overall_score":N, "verdict":"PASS|NEEDS_WORK|FAIL", "summary":"..."}}'
    )

    print(f"  \033[2mAvaliando com LLM-as-judge…\033[0m")
    eval_session = str(uuid.uuid4())
    answer = ""
    try:
        for event in client.chat_stream("agent-critic", eval_session, eval_prompt,
                                        context={}, history=[], tenant_id=tenant_id):
            if event.get("type") == "done":
                answer = event.get("message", "")
    except Exception as exc:
        print(f"  \033[31m✗ Critic failed: {exc}\033[0m")
        return

    # 4. Parse and render results
    import re
    evaluation = None
    try:
        json_match = re.search(r'\{[\s\S]*"overall_score"[\s\S]*\}', answer)
        if json_match:
            evaluation = _json.loads(json_match.group())
    except Exception:
        pass

    if not evaluation:
        print(f"  \033[33m⚠ Could not parse evaluation. Raw response:\033[0m")
        print(f"  {answer[:500]}")
        return

    dims = ["task_completion", "reasoning_relevancy", "reasoning_coherence",
            "tool_correctness", "step_efficiency"]

    overall = evaluation.get("overall_score", 0)
    verdict = evaluation.get("verdict", "?")
    color = "\033[32m" if verdict == "PASS" else ("\033[33m" if verdict == "NEEDS_WORK" else "\033[31m")

    print(f"\n  \033[1m{'DIMENSION':<25} {'SCORE':<8} EVIDENCE\033[0m")
    print(f"  {'-'*70}")
    for dim in dims:
        d = evaluation.get(dim, {})
        score = d.get("score", 0) if isinstance(d, dict) else 0
        evidence = d.get("evidence", "") if isinstance(d, dict) else ""
        bar = "█" * score + "░" * (10 - score)
        sc = "\033[32m" if score >= 7 else ("\033[33m" if score >= 4 else "\033[31m")
        print(f"  {dim:<25} {sc}{bar} {score}/10\033[0m  {evidence[:50]}")

    print(f"\n  {color}Overall: {overall}/10 — {verdict}\033[0m")
    summary = evaluation.get("summary", "")
    if summary:
        print(f"  {summary[:200]}")
    print()


def _agents_test_tools(client, agent_id, profile, args):
    """Invoke the agent and report tool call success/failure."""
    import json as _json, time
    from headlabs.config import get_tenant

    scenario = getattr(args, "scenario", None) or (
        "Smoke test: chame cada uma das suas tools disponíveis uma vez com parâmetros mínimos válidos. "
        "Objetivo: verificar que todas as tools respondem. Liste o resultado de cada uma."
    )

    print(f"\033[1m  Tool Test: {agent_id}\033[0m")
    print(f"  Scenario: {scenario}")
    print()

    # Get agent info (tools/MCPs configured)
    try:
        agent = client.request("GET", f"/agents/{agent_id}")
        manifest = agent.get("manifest", {})
        expected_tools = manifest.get("tools_native", [])
        expected_mcps = manifest.get("mcp", [])
        print(f"  \033[2mTools configuradas: {expected_tools or '(nenhuma nativa)'}\033[0m")
        print(f"  \033[2mMCPs: {[m.get('server','?') if isinstance(m,dict) else m for m in expected_mcps] or '(nenhum)'}\033[0m")
        print()
    except Exception:
        expected_tools = []
        expected_mcps = []

    # Invoke agent via chat_stream and capture tool calls from progress events
    import re as _re, uuid
    tool_calls = []  # [{name, elapsed, status, error}]
    t0 = time.time()
    session_id = str(uuid.uuid4())
    tenant_id = get_tenant()

    print(f"  \033[2mInvocando {agent_id}…\033[0m")
    try:
        for event in client.chat_stream(agent_id, session_id, scenario,
                                        context={}, history=[], tenant_id=tenant_id):
            etype = event.get("type", "")
            if etype == "progress":
                ev = event.get("event", {})
                if isinstance(ev, dict):
                    stage = ev.get("stage", "")
                    if stage in ("tool_call", "tool_use") or ev.get("type") == "tool_use":
                        tc = {"name": ev.get("tool", ev.get("name", "?")),
                              "elapsed": ev.get("elapsed", 0), "status": "ok", "error": None}
                        if ev.get("error"):
                            tc["status"] = "error"
                            tc["error"] = str(ev["error"])[:100]
                        tool_calls.append(tc)
                elif isinstance(ev, str):
                    m = _re.match(r'\s*-\s+(\S+)\s+\+(\d+):(\d+)', ev)
                    if m:
                        tool_calls.append({"name": m.group(1),
                                          "elapsed": int(m.group(2))*60+int(m.group(3)),
                                          "status": "ok", "error": None})
            elif etype == "tool_use":
                tool_calls.append({"name": event.get("name", "?"), "elapsed": 0,
                                   "status": "ok", "error": None})
            elif etype == "error":
                print(f"  \033[31m✗ Agent error: {event.get('error','?')}\033[0m")
                break
    except Exception as exc:
        print(f"  \033[31m✗ Invocation failed: {exc}\033[0m")

    total_time = time.time() - t0

    # Report
    if not tool_calls:
        print(f"  \033[33m⚠ Nenhum tool call detectado ({total_time:.1f}s)\033[0m")
        print(f"  \033[2m  O agente pode não ter usado tools neste cenário.\033[0m")
        return

    print(f"  {'TOOL':<30} {'TIME':<8} {'STATUS'}")
    print(f"  {'-'*60}")
    n_ok = 0
    n_fail = 0
    for tc in tool_calls:
        name = tc["name"]
        elapsed = f"{tc['elapsed']:.1f}s" if tc["elapsed"] else "—"
        if tc["status"] == "ok":
            print(f"  {name:<30} {elapsed:<8} \033[32m✓\033[0m")
            n_ok += 1
        else:
            print(f"  {name:<30} {elapsed:<8} \033[31m✗ {tc['error'][:50]}\033[0m")
            n_fail += 1

    print()
    print(f"  \033[1mSummary:\033[0m {len(tool_calls)} calls, {n_ok} ok, {n_fail} failed, {total_time:.1f}s total")
    if n_fail > 0:
        print(f"  \033[31m  {n_fail} tool(s) falharam — verifique credenciais/conectividade do MCP.\033[0m")
    else:
        print(f"  \033[32m  Todas as tools funcionaram.\033[0m")

    # MCP Coverage: check if the agent used the tools its MCPs expose
    if expected_mcps:
        print(f"\n  \033[1mMCP Coverage:\033[0m")
        tools_used = set(tc["name"] for tc in tool_calls)
        for mcp_entry in expected_mcps:
            mcp_id = mcp_entry.get("server", mcp_entry) if isinstance(mcp_entry, dict) else mcp_entry
            # Fetch tools available from MCP
            try:
                import httpx
                resp = httpx.post(f"https://mcps.headlabs.ai/{mcp_id}/mcp",
                                  json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
                                  headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                                  timeout=15)
                mcp_tools = []
                for line in resp.text.split("\n"):
                    if line.startswith("data:"):
                        import json as _j
                        body = _j.loads(line[5:].strip())
                        for t in body.get("result", {}).get("tools", []):
                            mcp_tools.append(t.get("name", ""))
            except Exception:
                mcp_tools = []

            if not mcp_tools:
                print(f"    {mcp_id}: \033[33m⚠ could not fetch tools\033[0m")
                continue

            used = tools_used & set(mcp_tools)
            unused = set(mcp_tools) - tools_used
            coverage = len(used) / len(mcp_tools) * 100 if mcp_tools else 0
            blocks = int(coverage) // 10
            bar = "█" * blocks + "░" * (10 - blocks)
            bc = "\033[32m" if coverage >= 50 else ("\033[33m" if coverage >= 20 else "\033[31m")
            print(f"    {mcp_id:<22} {bc}{bar} {coverage:.0f}%\033[0m  ({len(used)}/{len(mcp_tools)} tools used)")
            if unused and coverage < 80:
                relevant = [t for t in unused if not t.startswith("_")][:5]
                if relevant:
                    print(f"    \033[33m  ⚠ Unused: {', '.join(relevant)}\033[0m")
                    missed = [t for t in relevant if any(k in t for k in ["odd", "bet", "price", "score", "live"])]
                    if missed:
                        print(f"    \033[31m  ✗ Likely needed but not called: {', '.join(missed)}\033[0m")

                # Auto-fix: if --fix flag, patch the agent prompt with MCP tools docs
                if getattr(args, "fix", False):
                    tool_list = ", ".join(mcp_tools)
                    fix_instruction = (
                        f"Add explicit MCP tools documentation to the prompt: "
                        f"'MCP TOOLS ({mcp_id}): Available tools: {tool_list}. "
                        f"ALWAYS use these tools as primary data source before falling back to web_search. "
                        f"Get IDs from listing tools (e.g. jogos()) first, then call specific tools with the ID.'"
                    )
                    print(f"\n  \033[1mAuto-fixing: documenting MCP tools in prompt…\033[0m")
                    from types import SimpleNamespace
                    fix_args = SimpleNamespace(id=agent_id, instruction=fix_instruction,
                                              profile=profile, tenant=None)
                    from headlabs.cli import cmd_agents_update
                    cmd_agents_update(fix_args)
                    print(f"  \033[32m✓ Prompt updated with MCP tools documentation.\033[0m")


def cmd_agents_test(args):
    """Adversarial autocritical test: the critic agent evaluates another agent.

    1. Reads target agent's contract (prompt, tools, skills, schema)
    2. Invokes the target agent (real execution, captured as a trace)
    3. Sends contract + output to the critic for adversarial evaluation
    4. Persists a structured test report + compares against the prior baseline
    5. Renders score, dimensions, gaps, recommendations (+ baseline delta)
    6. If --fix: applies recommendations, RE-RUNS the same evaluation, and proves
       whether the fix improved the agent (IMPROVED / REGRESSED / UNCHANGED)

    With --tools: runs the agent and reports tool call success/failure (no critic).
    ``--output-format json`` emits the full structured report instead of ANSI.
    """
    from headlabs.client import HeadLabsClient
    from headlabs.config import get_tenant
    from headlabs import testkit

    client = HeadLabsClient()
    agent_id = args.agent_id
    profile = getattr(args, "profile", None)
    fmt = getattr(args, "output_format", "human") or "human"
    human = fmt == "human"

    # --tools mode: invoke agent and report tool call results
    if getattr(args, "tools", False):
        return _agents_test_tools(client, agent_id, profile, args)

    # --reasoning mode: evaluate reasoning/action quality via LLM-as-judge
    if getattr(args, "reasoning", False):
        return _agents_test_reasoning(client, agent_id, profile, args)

    scenario = getattr(args, "scenario", None)

    # 1-4. Produce a normalized evaluation (contract → invoke → critic → parse).
    ev = _adversarial_eval(client, agent_id, profile, scenario, quiet=not human)
    if ev is None:
        if not human:
            print(_json_dumps({"schema": "headlabs.test/v1", "agent_id": agent_id,
                               "error": "critic did not return valid JSON"}))
        sys.exit(1)

    # Baseline = the prior test of this agent (before we persist the new one).
    baseline = testkit.baseline(agent_id)
    test_trace = testkit.persist(ev)
    comparison = testkit.compare(baseline, ev)

    # 5. Render
    if human:
        _render_test_evaluation(ev, comparison=comparison, trace_id=test_trace.trace_id)
    else:
        report = testkit.report_json(ev, before=baseline, comparison=comparison)
        report["trace_id"] = test_trace.trace_id
        print(_json_dumps(report))

    # 6. Auto-fix: apply fix_instructions, re-run the SAME evaluation, compare.
    if getattr(args, "fix", False) and ev.fix_instructions:
        instruction = " | ".join(ev.fix_instructions[:3])
        if human:
            print(f"\033[1m  Applying fix…\033[0m")
            print(f"  \033[2m{instruction[:120]}\033[0m")
        from types import SimpleNamespace
        fix_args = SimpleNamespace(id=agent_id, instruction=instruction,
                                   profile=profile, tenant=None)
        cmd_agents_update(fix_args)

        if human:
            print(f"\n\033[1m  Re-validating after fix…\033[0m")
        after = _adversarial_eval(client, agent_id, profile, scenario, quiet=not human)
        if after is None:
            if not human:
                print(_json_dumps({"schema": "headlabs.test/v1", "agent_id": agent_id,
                                   "phase": "retest", "error": "critic did not return valid JSON"}))
            sys.exit(1)
        retest_trace = testkit.persist(after)
        loop = testkit.compare(ev, after)
        if human:
            _render_loop_verdict(ev, after, loop, trace_id=retest_trace.trace_id)
        else:
            report = testkit.report_json(after, before=ev, comparison=loop)
            report["trace_id"] = retest_trace.trace_id
            report["phase"] = "retest"
            print(_json_dumps(report))
        # CI-friendly: a regression after a fix is a failure.
        if loop.get("verdict") == "REGRESSED":
            sys.exit(1)


def _json_dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _adversarial_eval(client, agent_id, profile, scenario, *, quiet=False):
    """Run one full adversarial evaluation of an agent and return a normalized
    :class:`~headlabs.testkit.TestEvaluation` (or ``None`` if the critic did not
    produce valid JSON).

    Captures the agent's execution as a ``workflow="test-exec"`` trace so the
    evaluation references a real, inspectable run.
    """
    import json as _json
    import time as _time
    import uuid
    from headlabs.config import get_tenant
    from headlabs.output import make_reporter
    from headlabs import testkit

    # 1. Read target agent contract
    try:
        agent = client.request("GET", f"/agents/{agent_id}")
    except Exception:
        print(f"\033[31merro: agente '{agent_id}' não encontrado\033[0m", file=sys.stderr)
        sys.exit(2)

    manifest = agent.get("manifest", {})
    mcp_tools_info = {}
    for mcp_entry in manifest.get("mcp", []):
        mcp_id = mcp_entry.get("server", mcp_entry) if isinstance(mcp_entry, dict) else mcp_entry
        try:
            import httpx
            resp = httpx.post(f"https://mcps.headlabs.ai/{mcp_id}/mcp",
                              json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
                              headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                              timeout=15)
            for line in resp.text.split("\n"):
                if line.startswith("data:"):
                    body = _json.loads(line[5:].strip())
                    mcp_tools_info[mcp_id] = [t.get("name") for t in body.get("result", {}).get("tools", [])]
        except Exception:
            pass
    contract = (
        f"AGENT ID: {agent_id}\n"
        f"DESCRIPTION: {agent.get('description','')}\n"
        f"TYPE: {agent.get('agent_type','')}\n"
        f"TOOLS (native): {manifest.get('tools_native',[])}\n"
        f"MCPS: {manifest.get('mcp',[])}\n"
        f"MCP TOOLS AVAILABLE: {mcp_tools_info}\n"
        f"SKILLS: {manifest.get('skills',[])}\n"
        f"PROMPT:\n{agent.get('prompt','')[:4000]}\n"
    )

    # 2. Invoke the target agent (real execution), captured as a trace.
    reporter = make_reporter("human" if not quiet else "human",
                             workflow="test-exec", agent_id=agent_id,
                             profile=profile or "", quiet=quiet)
    _t0 = _time.time()
    n_tool_calls = 0
    reporter.begin_wait(f"Executando {agent_id}…")
    try:
        if profile:
            result = client.run(agent_id, profile, days=30, reporter=reporter,
                                question=scenario or None)
        else:
            exec_id, tenant_id, stream_id = client.invoke(agent_id, {
                "intent": scenario or "análise completa", "tenant_id": "ALL"})
            result = client.poll(exec_id, tenant_id=tenant_id, stream_id=stream_id,
                                 reporter=reporter)
        output_text = _json.dumps(result.raw_output, ensure_ascii=False, default=str)[:6000]
        # Prefer the measured tool count from the captured trace.
        n_tool_calls = getattr(reporter, "trace", None).metrics.tool_calls if hasattr(reporter, "trace") else 0
        if not n_tool_calls and isinstance(result.raw_output, dict):
            n_tool_calls = result.raw_output.get("tool_calls", 0)
    except Exception as exc:
        reporter.finish("failed")
        output_text = f"ERRO NA EXECUÇÃO: {str(exc)[:500]}"
    exec_time = _time.time() - _t0
    exec_trace_id = getattr(getattr(reporter, "trace", None), "trace_id", "")

    # 3. Critic evaluation with the canonical dimensions.
    dim_lines = ",\n".join(
        f'    "{d}": {{"score": <0-100>, "evidence": "<...>"}}' for d in testkit.DIMENSIONS)
    critic_input = (
        "You are a strict AI agent evaluator. Evaluate the agent execution below.\n\n"
        "RULES:\n"
        "- Score each dimension 0-100. Be harsh but fair.\n"
        "- Provide specific evidence (quote the output) for each score.\n"
        "- Recommendations must be concrete, actionable instructions for the agent's prompt.\n"
        "- Do NOT return markdown. Return ONLY the JSON object below, nothing else.\n\n"
        f"═══ AGENT CONTRACT ═══\n{contract}\n\n"
        f"═══ SCENARIO ═══\n{scenario or '(default analysis)'}\n\n"
        f"═══ AGENT OUTPUT ═══\n{output_text}\n\n"
        "Return this EXACT JSON structure (no other text):\n"
        "{\n"
        '  "score": <overall 0-100>,\n'
        '  "verdict": "PASS" | "NEEDS_WORK" | "FAIL",\n'
        '  "dimensions": {\n'
        f"{dim_lines}\n"
        '  },\n'
        '  "top_issues": ["<issue 1>", "<issue 2>"],\n'
        '  "fix_instructions": ["<concrete prompt change 1>", "<concrete prompt change 2>"]\n'
        "}"
    )

    session_id = str(uuid.uuid4())
    tenant_id = get_tenant()
    crep = make_reporter("human", workflow="test-critic", agent_id="agent-critic",
                         quiet=quiet, persist=False)
    if not quiet:
        print(f"\n\033[1m  Avaliação: {agent_id}\033[0m")
    crep.begin_wait("Critic analisando…")
    answer = ""
    try:
        for event in client.chat_stream("agent-critic", session_id, critic_input,
                                         context={}, history=[], tenant_id=tenant_id):
            et = event.get("type", "")
            if et == "progress":
                crep.event(event["event"])
            elif et == "done":
                answer = event.get("message", "")
            elif et == "error":
                crep.finish("failed")
                print(f"  ✗ {event.get('error')}", file=sys.stderr)
                return None
        crep.finish("succeeded")
    except Exception as exc:
        crep.finish("failed")
        print(f"  ✗ {exc}", file=sys.stderr)
        return None

    # 4. Parse + normalize.
    raw_eval = testkit.parse_evaluation(answer)
    if raw_eval is None:
        if not quiet:
            print(f"\n  \033[31m✗ Critic não retornou JSON válido. Raw:\033[0m")
            print(f"  {answer[:400]}")
        return None
    return testkit.normalize(raw_eval, agent_id=agent_id, scenario=scenario or "",
                             exec_time_s=exec_time, tool_calls=n_tool_calls,
                             exec_trace_id=exec_trace_id)


def _bar(score: int) -> str:
    blocks = max(0, min(10, int(score) // 10))
    return "█" * blocks + "░" * (10 - blocks)


def _score_color(s: int) -> str:
    return "\033[32m" if s >= 80 else ("\033[33m" if s >= 60 else "\033[31m")


def _render_test_evaluation(ev, *, comparison=None, trace_id: str = "") -> None:
    """Render a :class:`TestEvaluation` with the dimension bars and, when a
    baseline exists, a per-dimension delta against it."""
    from headlabs import testkit

    color = "\033[32m" if ev.verdict == "PASS" else ("\033[33m" if ev.verdict == "NEEDS_WORK" else "\033[31m")
    base = (comparison or {}).get("dimensions", {})

    print(f"\n  \033[1m{'DIMENSION':<22} {'SCORE':<14} {'Δ':<8} EVIDENCE\033[0m")
    print(f"  {'-'*74}")
    for dname in testkit.DIMENSIONS:
        d = ev.dimensions.get(dname, {})
        s = d.get("score", 0) if isinstance(d, dict) else 0
        evid = d.get("evidence", "") if isinstance(d, dict) else ""
        delta = base.get(dname, {}).get("delta")
        dtxt = _delta_txt(delta)
        sc = _score_color(s)
        print(f"  {dname:<22} {sc}{_bar(s)} {s:>3}/100\033[0m  {dtxt:<8} {evid[:40]}")

    # Measured (deterministic) dimensions.
    lat_score = max(0, min(100, int(100 - (ev.exec_time_s / 120) * 100)))
    print(f"  {'latency':<22} {_score_color(lat_score)}{_bar(lat_score)} {lat_score:>3}/100\033[0m  {'':<8} {ev.exec_time_s:.1f}s")
    n = ev.tool_calls
    tc_score = 50 if n == 0 else (100 if n <= 5 else (70 if n <= 15 else max(20, 100 - n * 2)))
    print(f"  {'tool_calls':<22} {_score_color(tc_score)}{_bar(tc_score)} {tc_score:>3}/100\033[0m  {'':<8} {n} calls")

    score_delta = (comparison or {}).get("score", {}).get("delta")
    print(f"\n  {'OVERALL':<22} {color}{_bar(ev.score)} {ev.score:>3}/100\033[0m  {_delta_txt(score_delta)}")

    if (comparison or {}).get("verdict") and comparison["verdict"] != "BASELINE":
        v = comparison["verdict"]
        vc = {"IMPROVED": "\033[32m", "REGRESSED": "\033[31m", "UNCHANGED": "\033[2m"}.get(v, "")
        sd = comparison.get("score", {})
        print(f"  \033[2mvs baseline:\033[0m {vc}{v}\033[0m "
              f"({sd.get('before')} → {sd.get('after')})")

    if ev.score >= 80:
        print(f"  \033[32mAgent is production-ready.\033[0m")
    elif ev.top_issues:
        print(f"\n  \033[1mPrioridade:\033[0m {ev.top_issues[0][:120]}")
        if ev.fix_instructions:
            print(f"  \033[1mAção:\033[0m {ev.fix_instructions[0][:120]}")
    if len(ev.top_issues) > 1:
        print(f"\n  \033[2mOutros issues:\033[0m")
        for g in ev.top_issues[1:5]:
            print(f"    \033[31m✗\033[0m {g[:120]}")
    if len(ev.fix_instructions) > 1:
        print(f"\n  \033[2mOutras ações:\033[0m")
        for f in ev.fix_instructions[1:5]:
            print(f"    \033[36m→\033[0m {f[:120]}")
    if trace_id:
        print(f"\n  \033[2mTrace: {trace_id[:14]}  ·  headlabs trace show {trace_id[:14]}\033[0m")
    print()


def _delta_txt(delta) -> str:
    if delta is None:
        return ""
    if delta > 0:
        return f"\033[32m▲+{delta}\033[0m"
    if delta < 0:
        return f"\033[31m▼{delta}\033[0m"
    return "\033[2m=\033[0m"


def _render_loop_verdict(before, after, loop, *, trace_id: str = "") -> None:
    """Render the closed-loop verdict after a --fix re-test."""
    v = loop.get("verdict", "?")
    vc = {"IMPROVED": "\033[32m", "REGRESSED": "\033[31m",
          "UNCHANGED": "\033[33m"}.get(v, "")
    sd = loop.get("score", {})
    print(f"\n  \033[1m  CLOSED-LOOP RESULT\033[0m")
    print(f"  {'-'*40}")
    print(f"  verdict     {vc}{v}\033[0m")
    print(f"  score       {sd.get('before')} → {sd.get('after')}  "
          f"({_delta_txt(sd.get('delta'))})")
    print(f"  tool_calls  {loop.get('tool_calls', {}).get('before')} → "
          f"{loop.get('tool_calls', {}).get('after')}")
    # Per-dimension movement (only those that changed).
    moved = {k: d for k, d in loop.get("dimensions", {}).items()
             if d.get("delta") not in (None, 0)}
    if moved:
        print(f"  dimensions:")
        for k, d in sorted(moved.items(), key=lambda kv: (kv[1].get('delta') or 0)):
            print(f"    {k:<22} {d.get('before')} → {d.get('after')}  {_delta_txt(d.get('delta'))}")
    if trace_id:
        print(f"\n  \033[2mTrace: {trace_id[:14]}  ·  headlabs trace diff {before.exec_trace_id[:10] if before.exec_trace_id else '<a>'} {trace_id[:10]}\033[0m")
    print()


def cmd_agents_deploy(args):
    """Build + push + deploy an agent. Abstracts Docker/ECR/AgentCore entirely.

    Flow: resolve platform repo → docker build → docker push ECR → POST /deploy → poll READY.
    The dev only runs: headlabs agents deploy <agent_id> [--wait]
    """
    import subprocess
    import time as _time
    from headlabs.client import HeadLabsClient
    from headlabs.config import load_config

    agent_id = args.agent_id
    tag = getattr(args, "tag", None) or agent_id
    client = HeadLabsClient()

    # ── 1. Resolve platform repo path ─────────────────────────────────────────
    cfg = load_config()
    # Try local ./agents/<id>/ first (self-contained, no platform repo needed),
    # then fall back to the platform repo for legacy/internal agents.
    local_agent_dir = os.path.join(os.getcwd(), "agents", agent_id)
    if os.path.isfile(os.path.join(local_agent_dir, "Dockerfile")):
        # Self-contained agent: build from its own Dockerfile
        platform_path = None
        build_context = local_agent_dir
        dockerfile = "Dockerfile"
        build_args = []
        print(f"\033[2m  Fonte: ./agents/{agent_id}/ (self-contained)\033[0m")
    else:
        platform_path = (
            cfg.get("platform_path")
            or os.environ.get("HEADLABS_PLATFORM_PATH")
            or _find_platform_repo()
        )
        if not platform_path or not os.path.isfile(os.path.join(platform_path, "Dockerfile.agent")):
            print("\033[31merro: agente não encontrado em ./agents/ nem no repo da plataforma.\033[0m")
            print("\033[2m  Crie com: headlabs agents init " + agent_id + "\033[0m")
            print("\033[2m  Ou configure: HEADLABS_PLATFORM_PATH=/caminho/headlabs-platform\033[0m")
            sys.exit(2)
        build_context = platform_path
        print(f"\033[2m  Fonte: {platform_path} (platform repo)\033[0m")

    # ── 2. Determine Dockerfile + image tag ───────────────────────────────────
    ACCOUNT = "688128002471"
    REGION = "us-east-1"
    ECR = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/headlabs-agents"
    image_uri = f"{ECR}:{tag}"

    if not platform_path:
        # Self-contained agent (from ./agents/<id>/)
        pass  # build_context, dockerfile, build_args already set
    elif tag == "loops-latest" or agent_id.startswith("loop-"):
        dockerfile = "Dockerfile.loops"
        if tag != "loops-latest":
            tag = "loops-latest"
            image_uri = f"{ECR}:{tag}"
        build_args = []
    else:
        dockerfile = "Dockerfile.agent"
        module = agent_id.replace("-", "_")
        build_args = ["--build-arg", f"AGENT_ID={agent_id}",
                      "--build-arg", f"AGENT_MODULE={module}"]

    # ── 3. Docker build ───────────────────────────────────────────────────────
    print(f"\033[2m  Building {image_uri}…\033[0m")
    cmd = ["docker", "build", "--platform", "linux/arm64",
           "-f", dockerfile, "-t", image_uri, "."] + build_args
    r = subprocess.run(cmd, cwd=build_context, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"\033[31merro: docker build falhou\033[0m")
        print(r.stderr[-500:] if r.stderr else r.stdout[-500:])
        sys.exit(1)
    print(f"\033[32m  ✓ Build OK\033[0m")

    # ── 4. ECR login + push ───────────────────────────────────────────────────
    print(f"\033[2m  Pushing {tag}…\033[0m")
    profile = getattr(args, "profile", None) or os.environ.get("AWS_PROFILE", "")
    login_cmd = f"aws ecr get-login-password --region {REGION}"
    if profile:
        login_cmd += f" --profile {profile}"
    login_cmd += f" | docker login --username AWS --password-stdin {ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
    subprocess.run(login_cmd, shell=True, capture_output=True)

    r = subprocess.run(["docker", "push", image_uri], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"\033[31merro: docker push falhou\033[0m")
        print(r.stderr[-300:] if r.stderr else "")
        sys.exit(1)
    print(f"\033[32m  ✓ Push OK\033[0m")

    # ── 5. Trigger deploy via API (don't wait for EventBridge) ────────────────
    # For loop agents, do batch deploy
    if tag == "loops-latest":
        body = {"agents": ["loop-orchestrator", "loop-researcher", "loop-deliverer",
                           "loop-architect", "loop-planner", "loop-executor", "loop-validator"],
                "image_tag": tag, "strategy": "parallel"}
        try:
            resp = client.request("POST", "/deploy", json=body)
        except Exception as exc:
            print(f"\033[31merro na API: {exc}\033[0m")
            sys.exit(2)
        print(f"\033[32m✓ Deploy batch iniciado: {resp.get('group_id')}\033[0m")
        deps = resp.get("deployments", [])
    else:
        try:
            resp = client.request("POST", f"/agents/{agent_id}/deploy",
                                  json={"image_tag": tag, "force": getattr(args, "force", False)})
        except Exception as exc:
            print(f"\033[31merro na API: {exc}\033[0m")
            sys.exit(2)
        dep_id = resp.get("deployment_id")
        print(f"\033[32m✓ Deploy iniciado: {dep_id}\033[0m  ({agent_id})")
        deps = [{"deployment_id": dep_id, "agent_id": agent_id}]

    if not getattr(args, "wait", False):
        return

    # ── 6. Poll until all deployments complete ────────────────────────────────
    deadline = _time.time() + 300
    pending = {d["deployment_id"]: d["agent_id"] for d in deps}
    while pending and _time.time() < deadline:
        _time.sleep(5)
        for dep_id, aid in list(pending.items()):
            try:
                st = client.request("GET", f"/agents/{aid}/deployments/{dep_id}")
            except Exception:
                continue
            status = st.get("status", "in_progress")
            if status == "succeeded":
                print(f"\033[32m  ✓ {aid} → v{st.get('version','?')}\033[0m")
                del pending[dep_id]
            elif status == "failed":
                print(f"\033[31m  ✗ {aid}: {st.get('error','?')[:100]}\033[0m")
                del pending[dep_id]
    if pending:
        print(f"\033[33m⏱ Timeout. Pendentes: {list(pending.values())}\033[0m")
        sys.exit(8)
    print(f"\033[32m✓ Todos deployados.\033[0m")


def _find_platform_repo() -> str | None:
    """Try common locations for the headlabs-platform repo."""
    candidates = [
        os.path.expanduser("~/Documents/headlabs.ai/headlabs-platform"),
        os.path.expanduser("~/Documents/headlabs-platform"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "headlabs-platform"),
    ]
    for p in candidates:
        if os.path.isfile(os.path.join(p, "Dockerfile.agent")):
            return p
    return None


def cmd_agents_init(args):
    """Scaffold a new code agent in the current directory. Only the name is
    required; everything else has sensible defaults. The generated structure is
    self-contained — no need for the headlabs-platform repo."""

    agent_id = args.agent_id
    agent_dir = os.path.join(os.getcwd(), "agents", agent_id)
    if os.path.exists(agent_dir):
        print(f"\033[31merro: agents/{agent_id}/ já existe.\033[0m")
        sys.exit(2)

    # Resolve params with defaults
    model = getattr(args, "model", None) or "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    tools = [t.strip() for t in (getattr(args, "tools", None) or "web_search,web_fetch").split(",") if t.strip()]
    framework = getattr(args, "framework", None) or "strands"
    memory = getattr(args, "memory", None) or "null"
    display = agent_id.replace("-", " ").title()
    module = agent_id.replace("-", "_")

    # Prompt: inline, file, or default template
    prompt_file = getattr(args, "prompt_file", None)
    prompt_inline = getattr(args, "prompt", None)
    if prompt_file:
        prompt_text = open(prompt_file).read().strip()
    elif prompt_inline:
        prompt_text = prompt_inline
    else:
        prompt_text = (
            f"Você é um agente especialista ({display}). Analise a intenção do usuário,\n"
            f"use as ferramentas disponíveis para coletar dados REAIS e citáveis,\n"
            f"e retorne uma resposta estruturada e acionável.\n\n"
            f"Não invente dados. Se algo não for encontrado, diga explicitamente.\n"
            f"Retorne APENAS JSON com schema {module.title().replace('_','')}Output."
        )

    # Schema
    schema_text = getattr(args, "schema", None)
    if schema_text and os.path.isfile(schema_text):
        schema_content = open(schema_text).read()
    else:
        schema_content = f'''from pydantic import BaseModel
from typing import Optional


class {module.title().replace("_","")}Input(BaseModel):
    intent: str
    tenant_id: str
    loop_id: str = ""


class {module.title().replace("_","")}Output(BaseModel):
    summary: str
    findings: list[dict] = []
    sources: list[str] = []
    processing_time_ms: Optional[int] = None
'''

    # Tools
    tool_imports = ", ".join(tools)
    platform_tools = {"web_search", "web_fetch", "list_tenant_mcps"}
    custom = [t for t in tools if t not in platform_tools]
    if not custom:
        tools_content = f'''"""Read-only domain tools for {display}."""
from headlabs_sdk.sdk.platform_tools import {tool_imports}
'''
    else:
        builtin_imports = [t for t in tools if t in platform_tools]
        tools_content = f'''"""Read-only domain tools for {display}."""
try:
    from strands import tool
except ImportError:
    def tool(f): return f
'''
        if builtin_imports:
            tools_content += f"\nfrom headlabs_sdk.sdk.platform_tools import {', '.join(builtin_imports)}\n"
        for t in custom:
            tools_content += f'''

@tool
def {t}() -> dict:
    """TODO: implement {t}."""
    return {{"error": "not implemented"}}
'''

    # Agent
    input_cls = f"{module.title().replace('_','')}Input"
    output_cls = f"{module.title().replace('_','')}Output"
    agent_content = f'''"""{display} agent."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from headlabs_sdk.sdk import HeadLabsAgentBase, InvocationContext
from schema import {input_cls}, {output_cls}
from tools import {tool_imports}


_SYSTEM_PROMPT = """
{prompt_text}
""".strip()


class {module.title().replace("_","")}Agent(HeadLabsAgentBase):
    input_schema = {input_cls}
    output_schema = {output_cls}
    system_prompt = _SYSTEM_PROMPT
    domain_tools = [{tool_imports}]

    def build_message(self, input_data: {input_cls}, ctx: InvocationContext) -> str:
        return (
            f"{{input_data.intent}}\\n\\n"
            f"tenant_id: {{input_data.tenant_id}}\\n"
            f"Retorne APENAS JSON com schema {output_cls}."
        )


handler = {module.title().replace("_","")}Agent.as_handler()
'''

    # Config
    config_content = f'''agent:
  id:          {agent_id}
  version:     "1.0.0"
  framework:   {framework}
  category:    general
  description: "{display}"

llm:
  model:      {model}
  max_tokens: 4096

memory:
  short:  {memory}
  medium: null
  long:   null

skills: []

tools:
  native: []
  mcp:    []

guardrail: null

tenant:
  partition_enabled: true
  isolation:         shared

observability:
  otel:            true
  langfuse_traces: false
  xray:            true
'''

    # Requirements
    reqs = "strands-agents>=0.5\npydantic>=2\n"

    # Test
    test_content = f'''"""Canary test: verify output schema parses."""
from schema import {output_cls}


def test_output_schema_minimal():
    out = {output_cls}.model_validate({{"summary": "test"}})
    assert out.summary == "test"
    assert out.findings == []
'''

    # Dockerfile (self-contained — dev doesn't need the platform repo)
    dockerfile_content = f'''# Auto-generated by headlabs agents init
FROM 688128002471.dkr.ecr.us-east-1.amazonaws.com/headlabs-agents:sdk-base
ENV AGENT_ID={agent_id} PORT=8080
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY . /app/agents/{agent_id}
COPY . /app/agents/{module}
EXPOSE 8080
CMD ["python", "-m", "headlabs_sdk.sdk.runtime"]
'''

    # Write all files
    os.makedirs(agent_dir, exist_ok=True)
    os.makedirs(os.path.join(agent_dir, "tests"), exist_ok=True)

    files = {
        "agent.py": agent_content,
        "schema.py": schema_content,
        "tools.py": tools_content,
        "config.yaml": config_content,
        "requirements.txt": reqs,
        "Dockerfile": dockerfile_content,
        "__init__.py": "",
        "tests/__init__.py": "",
        "tests/test_agent.py": test_content,
    }
    for name, content in files.items():
        with open(os.path.join(agent_dir, name), "w") as f:
            f.write(content)

    print(f"\033[32m✓ Agent criado: agents/{agent_id}/\033[0m")
    print(f"\033[2m  agent.py      handler + prompt + build_message\033[0m")
    print(f"\033[2m  schema.py     input/output models\033[0m")
    print(f"\033[2m  tools.py      domain tools ({', '.join(tools)})\033[0m")
    print(f"\033[2m  Dockerfile    self-contained (deploy sem precisar do repo da plataforma)\033[0m")
    print(f"\033[2m  config.yaml · requirements.txt · tests/\033[0m")
    print()
    print(f"  Edite, depois:")
    print(f"  \033[2mheadlabs agents deploy {agent_id} --wait\033[0m")


def cmd_agents_push(args):
    """Push local agent source to the platform + deploy.

    1. Reads all source files from ./agents/<id>/
    2. Uploads them to the platform (POST /agents/:id/source)
    3. Triggers deploy (build + push ECR + update runtime)
    """
    import base64
    from headlabs.client import HeadLabsClient

    agent_id = args.agent_id
    agent_dir = os.path.join(os.getcwd(), "agents", agent_id)
    if not os.path.isdir(agent_dir):
        print(f"\033[31merro: ./agents/{agent_id}/ não encontrado.\033[0m")
        print(f"\033[2m  Crie com: headlabs agents init {agent_id}\033[0m")
        sys.exit(2)

    # Collect source files (skip __pycache__, .pyc, tests output)
    files = {}
    for root, dirs, fnames in os.walk(agent_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache", "node_modules")]
        for fname in fnames:
            if fname.endswith(".pyc"):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, agent_dir)
            with open(full, "rb") as f:
                content = f.read()
            files[rel] = base64.b64encode(content).decode()

    # Upload source to platform (versioned — each push = new immutable version)
    print(f"\033[2m  Uploading {len(files)} files…\033[0m")
    try:
        resp = client.request("POST", f"/agents/{agent_id}/source",
                              json={"files": files, "message": getattr(args, "message", "") or ""},
                              timeout=30)
        v = resp.get("version", "?")
        print(f"\033[32m  ✓ Source v{v}\033[0m")
    except Exception as exc:
        print(f"\033[33m  (source upload skipped: {str(exc)[:80]})\033[0m")

    # Trigger deploy (reuse cmd_agents_deploy logic)
    args.tag = None
    args.force = False
    cmd_agents_deploy(args)


def cmd_agents_pull(args):
    """Pull a remote agent's source to ./agents/<id>/.

    Downloads the source files previously pushed via `agents push`, or falls
    back to generating a scaffold from the agent's metadata (prompt, config)."""
    import base64
    from headlabs.client import HeadLabsClient

    agent_id = args.agent_id
    agent_dir = os.path.join(os.getcwd(), "agents", agent_id)
    if os.path.isdir(agent_dir):
        try:
            ans = input(f"  ./agents/{agent_id}/ já existe. Substituir? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes", "s", "sim"):
            print("\033[2m  Cancelado.\033[0m")
            return
        import shutil
        shutil.rmtree(agent_dir)

    client = HeadLabsClient()

    # Try to get source files from platform
    try:
        params = {}
        ver = getattr(args, "version", None)
        if ver:
            params["version"] = ver
        resp = client.request("GET", f"/agents/{agent_id}/source", params=params)
        files = resp.get("files", {})
        source_version = resp.get("version")
        available = resp.get("versions_available", [])
    except Exception:
        files = {}
        source_version = None
        available = []

    if files:
        os.makedirs(agent_dir, exist_ok=True)
        for rel_path, b64_content in files.items():
            full = os.path.join(agent_dir, rel_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(base64.b64decode(b64_content))
        # Always write a correct Dockerfile (generated, not user code)
        module = agent_id.replace("-", "_")
        with open(os.path.join(agent_dir, "Dockerfile"), "w") as f:
            f.write(
                f"FROM 688128002471.dkr.ecr.us-east-1.amazonaws.com/headlabs-agents:sdk-base\n"
                f"ENV AGENT_ID={agent_id} PORT=8080\n"
                f"COPY requirements.txt /tmp/requirements.txt\n"
                f"RUN pip install --no-cache-dir -r /tmp/requirements.txt\n"
                f"COPY . /app/agents/{agent_id}\n"
                f"COPY . /app/agents/{module}\n"
                f"EXPOSE 8080\n"
                f'CMD ["python", "-m", "headlabs_sdk.sdk.runtime"]\n'
            )
        print(f"\033[32m✓ Pull: agents/{agent_id}/ v{source_version} ({len(files)} files)\033[0m")
        if available and len(available) > 1:
            print(f"\033[2m  Versões disponíveis: {available}\033[0m")

        # Pull skills + prompt from the store (gives dev full context)
        try:
            meta = client.request("GET", f"/agents/{agent_id}")
            manifest = meta.get("manifest") or {}
            skill_ids = manifest.get("skills") or []
            if skill_ids:
                skills_dir = os.path.join(agent_dir, "skills")
                os.makedirs(skills_dir, exist_ok=True)
                for sid in skill_ids:
                    try:
                        sk = client.request("GET", f"/resources/skill/{sid}")
                        with open(os.path.join(skills_dir, f"{sid}.md"), "w") as f:
                            f.write(sk.get("content", ""))
                    except Exception:
                        pass
                print(f"\033[2m  Skills: {', '.join(skill_ids)} → skills/\033[0m")
        except Exception:
            pass
        # Pull the production prompt from the store (may differ from agent.py if hot-updated)
        try:
            prompt_resp = client.request("GET", f"/prompts/{agent_id}", params={"label": "production"})
            prompt_content = prompt_resp.get("content", "")
            if prompt_content:
                with open(os.path.join(agent_dir, "prompt.md"), "w") as f:
                    f.write(f"<!-- Prompt v{prompt_resp.get('version','?')} (production) -->\n")
                    f.write(prompt_content)
                print(f"\033[2m  Prompt (store): v{prompt_resp.get('version','?')} → prompt.md\033[0m")
        except Exception:
            pass
    else:
        # Fallback: generate scaffold from agent metadata
        print(f"\033[2m  Source não encontrado na plataforma. Gerando scaffold do metadata…\033[0m")
        try:
            meta = client.request("GET", f"/agents/{agent_id}")
        except Exception as exc:
            print(f"\033[31merro: agente '{agent_id}' não encontrado: {exc}\033[0m")
            sys.exit(2)
        # Use init with the agent's prompt/description
        from types import SimpleNamespace
        init_args = SimpleNamespace(
            agent_id=agent_id,
            model=meta.get("model"),
            tools=",".join(meta.get("manifest", {}).get("tools_native", []) or []) or None,
            framework=meta.get("framework"),
            memory=None,
            prompt=meta.get("prompt"),
            prompt_file=None,
            schema=None,
        )
        cmd_agents_init(init_args)
        return

    print(f"  \033[2mEdite e faça push: headlabs agents push {agent_id}\033[0m")


def cmd_schedule(args):
    """Schedule commands: set, remove, list."""
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()
    sub = getattr(args, "sched_cmd", None)

    if sub == "set":
        r = client.request("POST", f"/agents/{args.agent_id}/schedule",
                           json={"cron": args.cron, "question": getattr(args, "question", "") or ""})
        print(f"\033[32m✓ Schedule: {args.agent_id} → {r.get('cron')}\033[0m")
        print(f"\033[2m  Rule: {r.get('schedule_name')}\033[0m")
    elif sub == "remove":
        client.request("DELETE", f"/agents/{args.agent_id}/schedule")
        print(f"\033[32m✓ Schedule removido: {args.agent_id}\033[0m")
    else:
        items = client.request("GET", "/schedules")
        if not items:
            print("Nenhum schedule configurado.")
            return
        print(f"{'AGENT':<28} {'CRON':<20} {'ENABLED':<8}")
        print("-" * 58)
        for s in items:
            print(f"{s.get('agent_id',''):<28} {s.get('cron',''):<20} {s.get('enabled','')}")


def cmd_marketplace(args):
    """Unified marketplace: list all agents + MCPs available to the tenant."""
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()

    print(f"\033[1m{'TYPE':<8} {'ID':<30} {'STATUS':<10} {'DESCRIPTION'}\033[0m")
    print("─" * 90)

    # Agents
    try:
        agents = client.list_remote_agents()
        for a in (agents or []):
            if a.get("id") == "agent-architect":
                continue
            print(f"\033[36m{'agent':<8}\033[0m {a.get('id',''):<30} {a.get('status','?'):<10} {(a.get('description','') or '')[:40]}")
    except Exception:
        pass

    # MCPs
    try:
        tools = client.list_tools()
        mcps = [t for t in tools if t.get("type") == "mcp"]
        for m in mcps:
            print(f"\033[33m{'mcp':<8}\033[0m {m.get('id',''):<30} {'active':<10} {(m.get('name','') or '')[:40]}")
    except Exception:
        pass


def cmd_webhook(args):
    """Webhook commands: create, get, remove."""
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()
    sub = getattr(args, "hook_cmd", None)

    if sub == "create":
        r = client.request("POST", f"/agents/{args.agent_id}/webhook")
        print(f"\033[32m✓ Webhook criado\033[0m")
        print(f"  URL:   {r.get('url')}")
        print(f"  Token: {r.get('auth_token')}")
        print(f"\033[2m  Uso: curl -X POST {r.get('url')} -H 'Authorization: Bearer {r.get('auth_token')}' -d '{{...}}'\033[0m")
    elif sub == "get":
        r = client.request("GET", f"/agents/{args.agent_id}/webhook")
        print(f"  Hook: {r.get('hook_id')}")
        print(f"  Agent: {r.get('agent_id')}")
        print(f"  Enabled: {r.get('enabled')}")
    elif sub == "remove":
        client.request("DELETE", f"/hooks/{args.hook_id}")
        print(f"\033[32m✓ Webhook removido\033[0m")
    else:
        print("Use: headlabs webhook create <agent_id>")


def cmd_trigger(args):
    """Event triggers: set an MCP event to invoke an agent."""
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()
    sub = getattr(args, "trigger_cmd", None)

    if sub == "set":
        body = {
            "source": args.source,
            "event": getattr(args, "event", "") or "any",
            "filter": {},
        }
        flt = getattr(args, "filter", None)
        if flt:
            # Parse "key=value,key2=value2"
            for pair in flt.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    body["filter"][k.strip()] = v.strip()
        try:
            r = client.request("POST", f"/agents/{args.agent_id}/trigger", json=body)
            print(f"\033[32m✓ Trigger: {args.agent_id} ← {args.source}:{body['event']}\033[0m")
            print(f"\033[2m  ID: {r.get('trigger_id')}\033[0m")
            # Also create a webhook so the MCP source can call back
            hook = client.request("POST", f"/agents/{args.agent_id}/webhook")
            print(f"\033[2m  Webhook URL (configure no {args.source}): {hook.get('url')}\033[0m")
            print(f"\033[2m  Token: {hook.get('auth_token')}\033[0m")
        except Exception as exc:
            print(f"\033[31merro: {exc}\033[0m")
    elif sub == "remove":
        client.request("DELETE", f"/agents/{args.agent_id}/trigger/{args.trigger_id}")
        print(f"\033[32m✓ Trigger removido\033[0m")
    else:
        # List
        try:
            items = client.request("GET", "/triggers")
            if not items:
                print("Nenhum trigger configurado.")
                return
            print(f"{'AGENT':<25} {'SOURCE':<15} {'EVENT':<15} {'FILTER'}")
            print("-" * 70)
            for t in items:
                print(f"{t.get('agent_id',''):<25} {t.get('source',''):<15} {t.get('event',''):<15} {t.get('filter','')}")
        except Exception:
            print("Nenhum trigger configurado.")


def cmd_pipeline(args):
    """Pipeline commands: create, run, list, status."""
    import json as _json
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()
    sub = getattr(args, "pipe_cmd", None)

    if sub == "create":
        agents = [a.strip() for a in args.steps.split(",") if a.strip()]
        steps = [{"agent": a, "output_key": f"step_{i}_{a}"} for i, a in enumerate(agents)]
        # Thread: each step gets input_from the previous
        for i in range(1, len(steps)):
            steps[i]["input_from"] = steps[i-1]["output_key"]
        r = client.request("POST", "/pipelines", json={
            "name": args.name, "steps": steps,
            "description": getattr(args, "description", "") or ""})
        print(f"\033[32m✓ Pipeline criada: {r.get('pipeline_id')}\033[0m")
        print(f"  {' → '.join(agents)}")
    elif sub == "run":
        inp = {}
        if getattr(args, "input", None):
            try:
                inp = _json.loads(args.input)
            except Exception:
                inp = {"question": args.input}
        r = client.request("POST", f"/pipelines/{args.pipeline_id}/run", json={"input": inp})
        print(f"\033[32m✓ Pipeline executando: {r.get('run_id')}\033[0m ({r.get('steps')} steps)")
        print(f"\033[2m  Status: headlabs pipeline status {args.pipeline_id}\033[0m")
    elif sub == "status":
        r = client.request("GET", f"/pipelines/{args.pipeline_id}")
        print(f"Pipeline: {r.get('name')} ({r.get('pipeline_id')})")
        print(f"Steps: {' → '.join(s.get('agent','') for s in r.get('steps',[]))}")
        last = r.get("last_run")
        if last:
            print(f"\nÚltimo run: {last.get('run_id')} — {last.get('status')}")
            for res in last.get("results", []):
                st = "✓" if res.get("status") == "succeeded" else "✗"
                print(f"  {st} {res.get('agent')}")
        else:
            print("\n\033[2m  Nenhum run ainda. Execute: headlabs pipeline run {}\033[0m".format(args.pipeline_id))
    else:
        items = client.request("GET", "/pipelines")
        if not items:
            print("Nenhuma pipeline.")
            return
        print(f"{'ID':<20} {'NAME':<30} {'STEPS'}")
        print("-" * 58)
        for p in items:
            print(f"{p.get('pipeline_id',''):<20} {p.get('name',''):<30} {p.get('steps','')}")


def cmd_mcps(args):
    """MCP lifecycle: init, push, pull, dev, connect."""
    sub = getattr(args, "mcps_cmd", None)
    if sub == "init":
        return _mcps_init(args)
    if sub == "create":
        return _mcps_create(args)
    if sub == "push":
        return _mcps_push(args)
    if sub == "pull":
        return _mcps_pull(args)
    if sub == "dev":
        return _mcps_dev(args)
    if sub == "connect":
        return _mcps_connect(args)
    if sub == "delete":
        return _mcps_delete(args)
    if sub == "publish":
        return _mcps_publish(args)
    if sub == "unpublish":
        return _mcps_unpublish(args)
    if sub == "test":
        return _mcps_test(args)
    # Default (bare `headlabs mcps` or `headlabs mcps list`): list MCPs
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()
    try:
        mcps = client.request("GET", "/mcps")
    except Exception:
        mcps = []
    if not mcps:
        print("No MCPs found.")
        return
    print(f"{'ID':<28} {'Name':<30}")
    print("-" * 60)
    for m in sorted(mcps, key=lambda x: x.get("id", "")):
        print(f"{m.get('id',''):<28} {m.get('display_name','') or m.get('name',''):<30}")


def _mcps_test(args):
    """Test an MCP server: connect, discover tools, validate schemas, optionally invoke."""
    import asyncio, json, time

    mcp_id = args.mcp_id
    url = getattr(args, "url", None)
    invoke = getattr(args, "invoke", False)

    # Resolve URL: explicit, local, or platform
    if not url:
        if getattr(args, "local", False):
            port = getattr(args, "port", 8000) or 8000
            url = f"http://localhost:{port}/mcp"
        else:
            url = f"https://mcps.headlabs.ai/{mcp_id}/mcp"

    print(f"\033[1m  MCP Test: {mcp_id}\033[0m")
    print(f"  Endpoint: {url}")
    print()

    async def _run():
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {}
        # Add auth for platform MCPs
        if "mcps.headlabs.ai" in url:
            try:
                from headlabs.client import HeadLabsClient
                c = HeadLabsClient()
                api_key = c.api_key or ""
                if api_key:
                    import base64
                    # api_key is already the combined "pk_xxx:sk_xxx" pair (see
                    # HeadLabsClient._headers) — do NOT append an extra ':', that
                    # corrupts the secret_key the gateway parses via partition(":").
                    cred = base64.b64encode(api_key.encode()).decode()
                    headers["Authorization"] = f"Basic {cred}"
            except Exception:
                pass

        t0 = time.time()
        try:
            async with streamablehttp_client(url, headers=headers, timeout=30, terminate_on_close=False) as (read, write, _):
                async with ClientSession(read, write) as session:
                    # 1. Initialize
                    await session.initialize()
                    t_init = time.time() - t0
                    info = session.server_info if hasattr(session, 'server_info') else None
                    sname = getattr(info, 'name', '?') if info else '?'
                    sver = getattr(info, 'version', '?') if info else '?'
                    print(f"  \033[32m✓ initialize\033[0m  {t_init:.2f}s  server={sname} v{sver}")

                    # 2. List tools
                    t1 = time.time()
                    result = await session.list_tools()
                    tools = result.tools if hasattr(result, 'tools') else []
                    t_list = time.time() - t1
                    print(f"  \033[32m✓ tools/list\033[0m  {t_list:.2f}s  {len(tools)} tools discovered")
                    print()

                    # 3. Validate each tool
                    issues = []
                    print(f"  {'TOOL':<30} {'PARAMS':<6} {'SCHEMA':<8} {'STATUS'}")
                    print(f"  {'-'*70}")
                    for tool in tools:
                        name = tool.name
                        desc = tool.description or ""
                        schema = tool.inputSchema if hasattr(tool, 'inputSchema') else {}
                        props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
                        required = (schema.get("required") or []) if isinstance(schema, dict) else []
                        n_params = len(props)

                        # Semantic checks
                        tool_issues = []
                        if not desc:
                            tool_issues.append("no description")
                        elif len(desc) < 10:
                            tool_issues.append("description too short")
                        if n_params > 0 and not required:
                            tool_issues.append("no required params defined")
                        for pname, pval in props.items():
                            if isinstance(pval, dict) and not pval.get("description") and not (
                                pval.get("type") or pval.get("anyOf") or pval.get("oneOf") or pval.get("allOf")):
                                # `type` is absent but so is any of the union
                                # keywords — this is a genuinely untyped param.
                                # Optional[str]-style params compile to
                                # {"anyOf": [{"type": "string"}, {"type": "null"}]}
                                # (no top-level "type"), which IS typed and must
                                # not be flagged.
                                tool_issues.append(f"param '{pname}' lacks type")

                        status = "\033[32m✓ ok\033[0m" if not tool_issues else f"\033[33m⚠ {'; '.join(tool_issues)}\033[0m"
                        schema_ok = "✓" if isinstance(schema, dict) and schema.get("type") == "object" else "⚠"
                        print(f"  {name:<30} {n_params:<6} {schema_ok:<8} {status}")
                        if tool_issues:
                            issues.extend([(name, i) for i in tool_issues])

                    print()

                    # 4. Optional: invoke each tool with empty/minimal args
                    if invoke:
                        print("  \033[1mInvocation tests (dry-run with minimal args):\033[0m")
                        for tool in tools:
                            name = tool.name
                            schema = tool.inputSchema if hasattr(tool, 'inputSchema') else {}
                            props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
                            required = (schema.get("required") or []) if isinstance(schema, dict) else []
                            # Build minimal args from required params
                            test_args = {}
                            for p in required:
                                ptype = props.get(p, {}).get("type", "string") if isinstance(props.get(p), dict) else "string"
                                if ptype == "integer":
                                    test_args[p] = 1
                                elif ptype == "number":
                                    test_args[p] = 1.0
                                elif ptype == "boolean":
                                    test_args[p] = True
                                elif ptype == "array":
                                    test_args[p] = []
                                else:
                                    test_args[p] = "test"
                            try:
                                t2 = time.time()
                                r = await session.call_tool(name, test_args)
                                elapsed = time.time() - t2
                                content = r.content if hasattr(r, 'content') else []
                                text = content[0].text[:80] if content and hasattr(content[0], 'text') else str(content)[:80]
                                is_error = r.isError if hasattr(r, 'isError') else False
                                if is_error:
                                    print(f"    {name:<28} \033[31m✗ error\033[0m {elapsed:.2f}s  {text}")
                                else:
                                    print(f"    {name:<28} \033[32m✓\033[0m       {elapsed:.2f}s  {text}")
                            except Exception as e:
                                print(f"    {name:<28} \033[31m✗ {str(e)[:60]}\033[0m")
                        print()

                    # Summary
                    total = time.time() - t0
                    n_ok = len(tools) - len(set(i[0] for i in issues))
                    n_warn = len(set(i[0] for i in issues))
                    print(f"  \033[1mSummary:\033[0m {len(tools)} tools, {n_ok} ok, {n_warn} warnings, {total:.2f}s total")
                    if issues:
                        print(f"  \033[33mIssues:\033[0m")
                        for tname, issue in issues[:10]:
                            print(f"    • {tname}: {issue}")
                    else:
                        print(f"  \033[32mAll tools pass schema & semantic validation.\033[0m")

        except Exception as e:
            print(f"  \033[31m✗ Connection failed: {e}\033[0m")
            return

    asyncio.run(_run())


def _mcps_connect(args):
    """Store tenant credentials for an MCP (so the runtime can authenticate)."""
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()
    mcp_id = args.mcp_id
    token = getattr(args, "token", None)
    if not token:
        try:
            token = input(f"  Token/API key for {mcp_id}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not token:
        print("\033[31merro: token vazio\033[0m")
        return
    try:
        client.request("POST", f"/mcps/{mcp_id}/credentials",
                       json={"token": token})
        print(f"\033[32m✓ Credenciais salvas: {mcp_id}\033[0m")
        print(f"\033[2m  Agentes com manifest.mcp=[{{\"server\":\"{mcp_id}\"}}] usarão este token.\033[0m")
    except Exception as exc:
        print(f"\033[31merro: {exc}\033[0m")


def _mcps_delete(args):
    """Hard-delete an MCP from the platform (deactivates its runtime and
    removes the record) and the local ./mcps/<id>/ project, if present.

    Local removal always runs — including when the remote call 404s (the
    record never existed there, e.g. a `create` that failed after scaffolding
    but before registration; see the real-world case that motivated this).
    `--local` is kept as a no-op flag for backward compatibility.

    Irreversible — gated behind an explicit confirmation unless --yes is
    passed (e.g. for non-interactive/scripted use)."""
    from headlabs.client import HeadLabsClient
    import requests
    client = HeadLabsClient()
    mcp_id = args.mcp_id
    mcp_dir = os.path.join(os.getcwd(), "mcps", mcp_id)
    has_local = os.path.isdir(mcp_dir)

    if not getattr(args, "yes", False):
        print(f"\033[33m  Isto vai deletar permanentemente o MCP '{mcp_id}' na HeadLabs "
              f"(runtime + registro).\033[0m")
        if has_local:
            print(f"\033[33m  Também vai remover ./mcps/{mcp_id}/ localmente.\033[0m")
        if not _confirm(f"  Confirma a exclusão de '{mcp_id}'?"):
            print("  \033[33mCancelado.\033[0m")
            return

    remote_ok = False
    try:
        client.request("DELETE", f"/mcps/{mcp_id}")
        print(f"\033[32m✓ MCP '{mcp_id}' deletado na HeadLabs.\033[0m")
        remote_ok = True
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            # Never existed remotely (or already gone) — not an error worth
            # stopping for; the local cleanup below is what the user actually
            # needs in this case.
            print(f"\033[2m  MCP '{mcp_id}' não existe na HeadLabs (nada a deletar lá).\033[0m")
        else:
            print(f"\033[31merro ao deletar na HeadLabs: {exc}\033[0m")
            return
    except Exception as exc:
        print(f"\033[31merro ao deletar na HeadLabs: {exc}\033[0m")
        return

    if has_local:
        import shutil
        shutil.rmtree(mcp_dir)
        print(f"\033[32m✓ Removido: mcps/{mcp_id}/\033[0m")
    elif remote_ok:
        print(f"\033[2m  mcps/{mcp_id}/ não existe localmente — nada a remover.\033[0m")


def _mcps_publish(args):
    """Publish an MCP (visibility=public): makes it discoverable/callable.

    Mirrors the platform's own gate for /publish: requires an active
    runtime_id. Fails with a clear message rather than a raw HTTP error if
    the MCP has no runtime yet (e.g. `mcps push` was never run)."""
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()
    mcp_id = args.mcp_id
    try:
        item = client.request("GET", f"/mcps/{mcp_id}")
    except Exception as exc:
        print(f"\033[31merro: MCP '{mcp_id}' não encontrado na plataforma: {exc}\033[0m")
        return
    if not item.get("runtime_id"):
        print(f"\033[31merro: '{mcp_id}' não tem runtime ativo — publique após um deploy "
              f"bem-sucedido (headlabs mcps push {mcp_id}).\033[0m")
        return
    try:
        client.publish_mcp(mcp_id)
        print(f"\033[32m✓ MCP publicado (visibility=public): {mcp_id}\033[0m")
    except Exception as exc:
        print(f"\033[31merro ao publicar: {exc}\033[0m")


def _mcps_unpublish(args):
    """Unpublish an MCP (visibility=private): instant kill-switch."""
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()
    mcp_id = args.mcp_id
    try:
        client.unpublish_mcp(mcp_id)
        print(f"\033[32m✓ MCP despublicado (visibility=private): {mcp_id}\033[0m")
    except Exception as exc:
        print(f"\033[31merro ao despublicar: {exc}\033[0m")


# ── MCP authoring: knowledge, design model, validation ────────────────────────
# Injected "skill": the invariants every MCP shares + the error taxonomy + auth
# patterns + production-readiness practices (tool naming, structured errors,
# health checks, idempotency). Sourced from the MCP spec, Anthropic's
# mcp-builder guidance, and documented production failure modes (SERF/CABP/
# ATBA). Keeping this in one place makes generated MCPs consistent and lets the
# design/codegen/repair turns share the same contract.
_MCP_AUTHORING_KNOWLEDGE = """\
MCP authoring contract (FastMCP, Python 3.12) — this is the EXACT contract
validated end-to-end against the HeadLabs AgentCore MCP runtime. Deviating from
it causes real, confirmed failures (init timeout or 421 Misdirected Request) —
it is not a style preference.

STRUCTURE (AgentCore MCP runtime contract — do not deviate)
- Import: `from mcp.server.fastmcp import FastMCP` (NEVER `from mcp import FastMCP`
  — that name is not exported from the top-level `mcp` package and fails at
  import time).
- Create ONE instance with BOTH of these arguments:
    mcp = FastMCP("<id>", host="0.0.0.0", stateless_http=True)
  `host="0.0.0.0"` is required for the container to be reachable; AgentCore
  injects `Mcp-Session-Id` itself, so `stateless_http=True` keeps the server
  portable across serverless instances — omitting either breaks the runtime.
- Each capability is an `@mcp.tool()` function with typed arguments and a
  JSON-serializable dict return.
- Do NOT build a custom ASGI app, do NOT wrap the server in Starlette routes,
  do NOT add extra HTTP endpoints (no `/health`, no `/ready`, no manual
  `Mount`/`Route`), and do NOT serve via uvicorn. AgentCore expects the
  container to serve MCP directly on `0.0.0.0:8000/mcp` via `mcp.run(...)`.
  A custom ASGI/uvicorn setup produces an HTTP 307 redirect at `/mcp/` that the
  AgentCore proxy does not follow, which surfaces as HTTP 421 (Misdirected
  Request) — confirmed in production. `app = mcp.streamable_http_app()` may
  still be assigned (some hosts use it), but it must never be the thing that
  is actually run in the container entrypoint.

TOOL NAMING & DESCRIPTIONS (the tool list IS the interface an agent reasons over)
- Names: action-oriented, snake_case, unambiguous (e.g. `fetch_usage_limits`,
  NOT `get_data` or `handler_v2`). Never suffix versions in the name
  (`_v2`); evolve via optional fields instead.
- Descriptions: 2-4 sentences covering what it does, when to call it, what it
  returns, and its side effects (or explicitly "read-only, no side effects").
  Too short forces the agent to guess; too long dilutes the signal.

INPUT/OUTPUT SCHEMA
- Every parameter is typed with an explicit required/optional distinction and
  a one-line description. Prefer narrow, validated types (enums for closed
  sets) over free-form strings.
- Return structured JSON (dict) with named fields, never a single opaque string.

STRUCTURED ERRORS (SERF) — never let a tool crash or return a bare string
- On any failure, catch the exception and return a dict:
    {"erro": {"codigo": <CODE>, "mensagem": <human-readable>,
              "retryable": <bool>, "acao_sugerida": <concrete next step>}}
- `acao_sugerida` is read by the calling agent's LLM — make it a concrete
  instruction ("peça ao usuário para verificar X"), never "ocorreu um erro".
- Stable error codes (pick the closest fit): ENTRADA_INVALIDA (retryable=false,
  fix input), RECURSO_NAO_ENCONTRADO (retryable=false), RATE_LIMIT_LOCAL
  (retryable=true, backoff), UPSTREAM_INDISPONIVEL (retryable=true, backoff),
  AUTH_INVALIDO (retryable=false, escalate), TLS_INCOMPATIVEL (retryable=false).

IDEMPOTENCY & SIDE EFFECTS
- Classify every tool as read-only (safe to retry, cacheable) or
  side-effecting (creates/modifies state). Side-effecting tools MUST document
  this in their docstring and should be idempotent where feasible (e.g. accept
  an idempotency key, or make repeated calls converge to the same state)
  rather than duplicating the effect on retry.

AUTH & CONFIG
- Auth patterns: none | apikey (key/bearer header from env) | mtls (client cert
  from env, TLS>=1.2) | oauth (client-credentials). Pick based on the spec.
- Read ALL configuration and secrets from environment variables; never
  hard-code secrets.
- mTLS client certs (and any other binary/large secret) do NOT fit directly in
  an env var: AgentCore's environmentVariables has a hard 4096-byte-per-value
  cap, which a base64-encoded .pfx/.p12 always exceeds — confirmed in
  production (ValidationException on UpdateAgentRuntime). Instead:
    1. Read a short `HEADLABS_MCP_SECRET_ID` env var (set automatically by the
       platform when a secret was stored via `headlabs mcps secrets put`/
       `PUT /mcps/{id}/secrets` — never invent your own env var name for this).
    2. At import time, if present, call
       `boto3.client("secretsmanager", region_name="us-east-1").get_secret_value(
       SecretId=<that value>)`, `json.loads` the `SecretString`, and read the
       actual secret fields (e.g. `CFF_CERT_PATH_B64`, `CFF_CERT_PASSPHRASE`)
       from that dict — the runtime role already has scoped
       secretsmanager:GetSecretValue for this prefix.
    3. Decode the cert with `cryptography.hazmat.primitives.serialization.pkcs12`
       into a temp PEM (no persistent filesystem across deploys — always
       regenerate at startup), then build the `ssl.SSLContext` from that.
    4. Wrap step 2 in try/except and, on failure, leave the field empty —
       `_create_http_client()`-equivalent code must always RAISE a SERF error
       dict (never `return` one — a function that sometimes returns an
       httpx.Client and sometimes returns a bare dict corrupts the caller,
       which then does `dict.get(...)` on what it thinks is a response object).
  Add `boto3` and `cryptography` to requirements.txt whenever this path is used.
- Prefer resilient upstream calls: explicit timeouts, graceful degradation, and
  optional caching (memory/disk) with a TTL when a snapshot is reasonable.
- Liveness/readiness is NOT exposed via HTTP routes on this runtime (see
  STRUCTURE above). If a tool depends on an unhealthy upstream/cache, surface
  that through the SERF error contract on the affected tool call instead —
  e.g. a `status_cache`-style read-only tool is fine, an extra `/health` route
  is not.

PACKAGING
- mcp and httpx are ALWAYS installed; list only EXTRA pip deps. Do NOT add
  uvicorn — it is not used to run the server on this runtime.
- End the file EXACTLY with:
    app = mcp.streamable_http_app()
    if __name__ == "__main__":
        import os
        mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
"""

# Full persona for the dedicated mcp-architect agent: the authoring contract IS
# the agent's system prompt, so per-call prompts only need to carry the spec
# and the required output format — not re-teach the rules every turn.
_MCP_ARCHITECT_PERSONA = (
    "You are the MCP Architect: a specialist that designs and implements "
    "production-grade Model Context Protocol (MCP) servers using FastMCP "
    "(Python).\n\n"
    "You are invoked programmatically by a CLI pipeline, not by a human "
    "chatting. Your output is parsed by code. This means:\n"
    "- Respond with ONLY the two fenced blocks requested (```json design, "
    "```python server.py). NO preamble, NO section titles, NO summary or "
    "checklist after the code, NO closing remarks. Any prose outside the two "
    "blocks is discarded and wastes tokens.\n"
    "- Never wrap explanations around the blocks. If you must reason, do it "
    "implicitly by producing correct output — do not narrate your reasoning.\n\n"
    f"{_MCP_AUTHORING_KNOWLEDGE}"
)


def _ensure_mcp_architect(client) -> bool:
    """Idempotently create the dedicated mcp-architect agent on the platform if
    it doesn't exist yet. Returns True if the agent is available (already
    existed or was just created), False if creation failed (caller falls back
    to the generic architect).

    Checks existence via a direct ``GET /agents/<id>`` rather than
    ``list_remote_agents()``: internal architect agents are created with
    ``visibility: private`` (matching agent-architect's pattern) and are
    correctly excluded from the public listing — so the listing can never
    confirm this agent exists, even after it was created.
    """
    try:
        client.request("GET", f"/agents/{_MCP_ARCHITECT_AGENT_ID}")
        return True
    except Exception:
        pass  # not found (or transient error) — fall through to (re)create
    try:
        client.create_agent(
            agent_id=_MCP_ARCHITECT_AGENT_ID,
            display_name="MCP Architect",
            prompt=_MCP_ARCHITECT_PERSONA,
            tools=["web_search", "web_fetch"],
            description="Internal agent: designs and implements FastMCP servers from a spec.",
        )
        return True
    except Exception:
        return False


def _normalize_mcp_design(draft) -> dict | None:
    """Coerce whatever the architect returned into the canonical MCP design
    model. Tolerant by construction: accepts missing keys, strings where lists
    are expected, dicts-or-strings for tools/params/config/errors. Returns None
    only if there is nothing usable (no id/name, or no tools)."""
    if not isinstance(draft, dict):
        return None

    def _s(v, d=""):
        return str(v).strip() if v is not None else d

    def _as_list(v):
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return v
        return [v]

    d = {}
    d["id"] = _s(draft.get("id"))
    d["name"] = _s(draft.get("name")) or (d["id"].replace("-", " ").title() if d["id"] else "")
    d["description"] = _s(draft.get("description"))

    d["dependencies"] = [x for x in (_s(r) for r in _as_list(
        draft.get("dependencies") or draft.get("requirements"))) if x]

    config = []
    raw_cfg = draft.get("config") or draft.get("env") or []
    if isinstance(raw_cfg, dict):
        raw_cfg = [{"env": k, "default": v} for k, v in raw_cfg.items()]
    for c in (raw_cfg if isinstance(raw_cfg, list) else []):
        if isinstance(c, str):
            env = _s(c)
            if env:
                config.append({"env": env, "secret": False, "required": False,
                               "default": "", "description": ""})
        elif isinstance(c, dict):
            env = _s(c.get("env") or c.get("name") or c.get("key"))
            if env:
                config.append({
                    "env": env, "secret": bool(c.get("secret", False)),
                    "required": bool(c.get("required", False)),
                    "default": _s(c.get("default")), "description": _s(c.get("description")),
                })
    d["config"] = config

    auth = draft.get("auth")
    if isinstance(auth, str):
        auth = {"type": auth}
    if not isinstance(auth, dict):
        auth = {}
    auth["type"] = _s(auth.get("type") or "none") or "none"
    d["auth"] = auth

    d["upstream"] = draft.get("upstream") if isinstance(draft.get("upstream"), dict) else {}
    d["state"] = draft.get("state") if isinstance(draft.get("state"), dict) else {}

    errors = []
    for e in _as_list(draft.get("errors")):
        if isinstance(e, str) and e.strip():
            errors.append({"code": _s(e), "when": ""})
        elif isinstance(e, dict) and _s(e.get("code")):
            errors.append({"code": _s(e.get("code")), "when": _s(e.get("when"))})
    d["errors"] = errors

    tools = []
    for t in _as_list(draft.get("tools")):
        if isinstance(t, str) and t.strip():
            tools.append({"name": _s(t), "description": "", "params": [],
                          "returns": "", "logic": "",
                          "side_effects": False, "idempotent": True})
        elif isinstance(t, dict) and _s(t.get("name")):
            params = []
            for p in _as_list(t.get("params") or t.get("parameters")):
                if isinstance(p, str) and p.strip():
                    params.append({"name": _s(p), "type": "str", "required": False,
                                   "default": "", "description": ""})
                elif isinstance(p, dict) and _s(p.get("name")):
                    params.append({
                        "name": _s(p.get("name")), "type": _s(p.get("type") or "str"),
                        "required": bool(p.get("required", False)),
                        "default": _s(p.get("default")), "description": _s(p.get("description")),
                    })
            tools.append({
                "name": _s(t.get("name")), "description": _s(t.get("description")),
                "params": params, "returns": _s(t.get("returns")),
                "logic": _s(t.get("logic") or t.get("implementation")),
                "side_effects": bool(t.get("side_effects", False)),
                "idempotent": bool(t.get("idempotent", not t.get("side_effects", False))),
            })
    d["tools"] = tools

    if not (d["id"] or d["name"]) or not d["tools"]:
        return None
    return d


def _sanitize_python_code(code: str) -> str:
    """Deterministically undo the common corruptions that creep in when code is
    round-tripped through the platform's transport/observability layers.

    Two independent sources of corruption are neutralized here:

    1. JSON-escape repair (:func:`headlabs.client._loads_tolerant`) doubles
       invalid backslash escapes so the JSON parses, but that *preserves* a
       stray backslash that was never meant to be in the Python source (e.g.
       an LLM wrote ``\\@mcp.tool()`` — an invalid ``\\@`` escape). Left alone
       it becomes ``SyntaxError: unexpected character after line continuation
       character``.
    2. The platform's PII scrubber treats ``@name(`` as an email/handle-like
       pattern and redacts it to ``\\pii_<hash>()`` *before* the trace even
       reaches this client — this has been observed replacing every
       ``@mcp.tool()`` decorator with ``\\pii_8d9040d12a()``. Since every
       occurrence in generated MCP code is a decorator (the only place `@`
       starts a line in our authoring contract), it is always safe to restore
       it to ``@mcp.tool()``. The scrubber sometimes swallows the preceding
       newline as well, gluing ``\\pii_<hash>()`` to the tail of the previous
       line (e.g. a ``# ===`` comment banner) instead of starting a line of
       its own — the replacement is therefore NOT anchored to line-start and
       always reinserts the newline before the decorator.

    These fixes are localized and safe (a real Python continuation backslash
    is at end-of-line, never before ``@``/``pii_`` or at the start of a
    line's content)."""
    if not code:
        return code
    import re
    # Platform PII scrubbing artifact: `\pii_<hex>()` was originally
    # `@mcp.tool()` (the only `@name(...)` pattern our authoring contract
    # produces). The scrubber sometimes swallows the preceding newline too,
    # leaving `\pii_<hex>()` glued to the end of the previous line (e.g. a
    # comment banner) instead of starting its own line — so this must NOT be
    # anchored to line-start. Insert the newline back before restoring the
    # decorator.
    code = re.sub(r'\\pii_[0-9a-f]+\(\)', '\n@mcp.tool()', code)
    # `\@decorator` → `@decorator` (a backslash before '@' is never valid Python)
    code = code.replace("\\@", "@")
    # A stray backslash at the start of a line's content (line-continuation
    # artifact) — drop just that leading backslash, keep the indentation.
    code = re.sub(r'(?m)^([ \t]*)\\(?=\S)', r'\1', code)
    return code


_GENERIC_TOOL_NAMES = {"get_data", "getdata", "handler", "process", "run", "execute",
                       "do_it", "action", "tool", "call", "invoke", "func", "helper"}


def _validate_mcp_contract(design: dict, server_code: str) -> list[str]:
    """Lint the design + server.py against production-readiness practices that
    AST/syntax checks cannot see (tool naming, description quality, schema
    typing, side-effect documentation, structured errors).

    Returns a list of human-readable warnings (empty = no issues found). This
    is advisory, not a hard gate — it is surfaced to the user at the approval
    gate so they can judge severity, matching how `mcps test` reports tool
    warnings today.
    """
    warnings = []
    tools = design.get("tools", [])

    for t in tools:
        name = (t.get("name") or "").strip()
        desc = (t.get("description") or "").strip()
        if not name:
            warnings.append("uma tool não tem nome")
            continue
        if "_v" in name.lower() and any(ch.isdigit() for ch in name):
            warnings.append(f"tool '{name}': nome versionado (_v2) é anti-padrão — evolua via campos opcionais")
        bare = name.lower().replace("_", "")
        if bare in {g.replace("_", "") for g in _GENERIC_TOOL_NAMES}:
            warnings.append(f"tool '{name}': nome genérico/ambíguo — o agente pode não conseguir decidir quando chamá-la")
        if not desc:
            warnings.append(f"tool '{name}': sem descrição — o agente não terá como saber quando usá-la")
        elif len(desc) < 20:
            warnings.append(f"tool '{name}': descrição muito curta ({len(desc)} chars) — detalhe o quê/quando/retorno/efeitos")
        for p in t.get("params", []):
            if not (p.get("description") or "").strip():
                warnings.append(f"tool '{name}': parâmetro '{p.get('name','?')}' sem descrição")
        if t.get("side_effects") and t.get("idempotent") is False:
            warnings.append(f"tool '{name}': tem efeitos colaterais e não é idempotente — considere aceitar uma idempotency key")

    if not tools:
        warnings.append("design sem tools")

    if "erro" not in server_code and "error" not in server_code.lower():
        warnings.append("nenhum tratamento de erro estruturado detectado no código")

    return warnings


def _validate_server_code(code: str):
    """Deterministic gate before scaffolding. Returns (ok, error_message).

    Checks the server.py parses (AST) and honors the FastMCP invariants for
    the REAL AgentCore MCP runtime contract (validated end-to-end in
    production — see `_MCP_AUTHORING_KNOWLEDGE`): correct import path, an
    `mcp` instance constructed with `host="0.0.0.0", stateless_http=True`, at
    least one tool, the streamable-http entrypoint, `mcp.run(...)` as the
    actual thing executed (not uvicorn), and no Starlette/custom ASGI wiring
    (which causes a 307 redirect the AgentCore proxy doesn't follow -> 421).

    This is a syntactic/substring check, not a runtime one — it CANNOT catch
    every defect (e.g. an import that is spelled correctly but wrong in some
    other way). The real backstop is `_verify_mcp_behavior`, which actually
    runs the file; this function exists to fail fast on the common, cheap-to-
    detect mistakes before paying for a subprocess spin-up.
    """
    import ast
    import re
    if not code or not code.strip():
        return False, "server.py vazio"
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc.msg} (linha {exc.lineno})"
    missing = []
    if "from mcp.server.fastmcp import" not in code and "mcp.server.fastmcp" not in code:
        missing.append('import incorreto: precisa ser "from mcp.server.fastmcp import FastMCP" (não "from mcp import FastMCP")')
    elif "FastMCP" not in code:
        missing.append("import/instância FastMCP")
    if "streamable_http_app" not in code:
        missing.append("app = mcp.streamable_http_app()")
    if "@mcp.tool" not in code and ".tool(" not in code:
        missing.append("nenhuma @mcp.tool()")
    if "stateless_http" not in code or "True" not in code.split("stateless_http", 1)[-1][:20]:
        missing.append('FastMCP(...) precisa de stateless_http=True (contrato AgentCore)')
    if 'host="0.0.0.0"' not in code and "host='0.0.0.0'" not in code:
        missing.append('FastMCP(...) precisa de host="0.0.0.0" (contrato AgentCore)')
    if "mcp.run(" not in code:
        missing.append("mcp.run(transport=...) precisa ser o entrypoint executado")
    if "uvicorn" in code.lower():
        missing.append("uvicorn não é suportado neste runtime — remova; use apenas mcp.run(...)")
    if "starlette" in code.lower() or "Mount(" in code or re.search(r"\bRoute\(", code):
        missing.append("Starlette/rotas HTTP customizadas (ex.: /health, /ready) causam 421 no AgentCore — remova")
    if missing:
        return False, "faltando: " + "; ".join(missing)
    return True, ""


def _mcp_build_prompt(spec_text: str, id_hint: str, include_knowledge: bool = True) -> str:
    """Single-turn contract: a compact DESIGN as ```json (drives deterministic
    assembly of deps/config) PLUS the full server.py as ```python (robust,
    escaping-free channel). Code never lives inside the JSON.

    ``include_knowledge`` controls whether the full authoring contract is
    repeated in the prompt. Skip it (False) when talking to the dedicated
    mcp-architect agent, whose PERSONA already embeds the contract — shorter
    prompt, less for the model to restate/narrate. Keep it (True, default) for
    the generic agent-architect fallback, which has no domain persona."""
    knowledge = f"{_MCP_AUTHORING_KNOWLEDGE}\n" if include_knowledge else ""
    return (
        "You are an MCP (Model Context Protocol) server architect. Design a "
        "COMPLETE, self-contained MCP server from the user's specification.\n\n"
        f"{knowledge}"
        f"SPECIFICATION:\n{spec_text}\n\n"
        + (f"REQUIRED SERVER ID: {id_hint}\n\n" if id_hint else "")
        + "Return your answer in EXACTLY two parts, in this order:\n\n"
        "PART 1 — a compact DESIGN as a JSON object in a ```json block "
        "(structured metadata only, NO code):\n"
        "```json\n"
        "{\n"
        '  "id": "<kebab-case>", "name": "...", "description": "<one line>",\n'
        '  "dependencies": ["<extra pip pkg>"],\n'
        '  "config": [{"env":"VAR","secret":false,"required":true,"default":"","description":"..."}],\n'
        '  "auth": {"type":"none|apikey|bearer|mtls|oauth"},\n'
        '  "upstream": {"base_url":"...","timeout_ms":15000},\n'
        '  "state": {"cache":"none|memory|disk","ttl_hours":24},\n'
        '  "errors": [{"code":"UPSTREAM_INDISPONIVEL","when":"5xx/timeout"}],\n'
        '  "tools": [{"name":"action_oriented_snake_case","description":"2-4 sentences: what/when/returns/side-effects",'
        '"params":[{"name":"","type":"str","required":true,"description":""}],'
        '"returns":"...","logic":"...","side_effects":false,"idempotent":true}]\n'
        "}\n"
        "```\n\n"
        "PART 2 — the COMPLETE contents of server.py in a ```python block "
        "(raw source, no escaping), implementing the design above:\n"
        "```python\n"
        "# full server.py\n"
        "```\n\n"
        "RULES:\n"
        "- Output ONLY the two fenced blocks below, back to back. NO preamble "
        "  (\"I will now...\"), NO section titles, NO summary/checklist after, "
        "  NO closing remarks. Any text outside the two ```json/```python "
        "  blocks is wasted tokens and will be discarded.\n"
        "- Put code ONLY in the ```python block, NEVER inside the JSON.\n"
        "- server.py must be valid, importable Python with no placeholders/TODOs.\n"
        "- Honor the FULL authoring contract: action-oriented tool names,\n"
        "  2-4 sentence descriptions, typed params, structured SERF error dicts\n"
        "  on every failure path, FastMCP(..., host=\"0.0.0.0\", stateless_http=True),\n"
        "  and mcp.run(transport=...) as the ONLY server entrypoint (no Starlette,\n"
        "  no uvicorn, no /health or /ready routes).\n"
    )


async def _probe_mcp_server(url: str, timeout: float = 10.0) -> dict:
    """Speak the REAL MCP protocol against a running server: initialize,
    list tools, and check each tool's schema quality. This is the same
    behavioral check `headlabs mcps test` performs against a deployed MCP —
    reused here so a freshly generated server is verified by actually running
    it and talking the protocol, not just by parsing its source.

    Returns {"ok": bool, "tools": [...], "warnings": [...], "error": str|None}.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    try:
        async with streamablehttp_client(url, timeout=timeout, terminate_on_close=False) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tools = result.tools if hasattr(result, "tools") else []
                warnings = []
                for tool in tools:
                    desc = tool.description or ""
                    schema = tool.inputSchema if hasattr(tool, "inputSchema") else {}
                    props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
                    required = (schema.get("required") or []) if isinstance(schema, dict) else []
                    if not desc:
                        warnings.append(f"tool '{tool.name}': sem descrição no protocolo")
                    if props and not required:
                        warnings.append(f"tool '{tool.name}': nenhum parâmetro required no schema")
                return {"ok": True, "tools": [t.name for t in tools],
                       "warnings": warnings, "error": None}
    except Exception as exc:
        return {"ok": False, "tools": [], "warnings": [], "error": str(exc)}


def _verify_mcp_behavior(mcp_dir: str, port: int = 0, timeout: float = 12.0) -> dict:
    """Run the generated server.py as a real local subprocess and verify it
    over the actual MCP protocol (initialize + tools/list + schema checks).

    This is deliberately Docker-free (fast, no build step) so it runs on every
    `mcps create`, not just as an opt-in follow-up. Returns a result dict.

    Two distinct failure modes are reported differently:
    - Infrastructure problem (Python executable missing, can't bind a port):
      ``{"ok": False, "skipped": True, ...}`` — a soft-fail that must not
      block creation, since it can't verify quality OR correctness.
    - The generated server itself crashes on startup (ImportError,
      SyntaxError-at-runtime, unhandled exception in module-level code):
      ``{"ok": False, "skipped": False, ...}`` — a REAL failure. AST validation
      only proves the file parses and contains the right substrings; it
      cannot catch a wrong import path (e.g. ``from mcp import FastMCP``
      instead of ``from mcp.server.fastmcp import FastMCP``) or any other
      runtime-only defect. This is what actually separates "compiles" from
      "behaves like an MCP", and the caller must treat it as blocking —
      exactly like an AST validation failure — not as an advisory warning.

    Implementation note: FastMCP does not read a ``PORT`` env var — its bind
    port lives on ``mcp.settings.port``, set at construction time inside the
    generated code. A tiny wrapper script imports the generated ``server``
    module and overrides ``mcp.settings.port``/``host`` *before* calling
    ``mcp.run()``, so verification never collides with a port the generated
    code happened to hard-code, without needing to modify that code.
    """
    import socket
    import subprocess
    import time as _time

    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

    if not os.path.isdir(mcp_dir):
        return {"ok": False, "skipped": True, "error": f"diretório não encontrado: {mcp_dir}"}

    wrapper_path = os.path.join(mcp_dir, "_verify_entrypoint.py")
    wrapper_src = (
        "import sys, importlib.util\n"
        "spec = importlib.util.spec_from_file_location('server', 'server.py')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "sys.modules['server'] = mod\n"
        "spec.loader.exec_module(mod)\n"
        f"mod.mcp.settings.port = {port}\n"
        "mod.mcp.settings.host = '127.0.0.1'\n"
        "mod.mcp.run(transport='streamable-http')\n"
    )

    env = dict(os.environ)
    env["MCP_TRANSPORT"] = "streamable-http"
    proc = None
    try:
        with open(wrapper_path, "w") as f:
            f.write(wrapper_src)
        proc = subprocess.Popen(
            [sys.executable, "_verify_entrypoint.py"], cwd=mcp_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        deadline = _time.time() + timeout
        url = f"http://127.0.0.1:{port}/mcp"
        last_result = None
        while _time.time() < deadline:
            if proc.poll() is not None:
                # The subprocess started and then exited — this is the
                # GENERATED CODE crashing (import error, exception at module
                # scope, etc.), not an infra problem. Treat as a real failure.
                _out, err = proc.communicate()
                return {"ok": False, "skipped": False,
                       "error": f"server.py falhou ao iniciar: {err.strip()[-500:]}"}
            import asyncio
            last_result = asyncio.run(_probe_mcp_server(url, timeout=3.0))
            if last_result["ok"]:
                return last_result
            _time.sleep(0.5)
        return last_result or {"ok": False, "skipped": True, "error": "timeout ao conectar"}
    except FileNotFoundError as exc:
        return {"ok": False, "skipped": True, "error": f"não foi possível iniciar o processo: {exc}"}
    except Exception as exc:
        return {"ok": False, "skipped": True, "error": str(exc)}
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            os.remove(wrapper_path)
        except OSError:
            pass


def _mcp_repair_prompt(code: str, error: str, include_knowledge: bool = True) -> str:
    """Feed a failing server.py back with the exact validation error for one
    deterministic repair turn. Returns corrected code in a ```python block.

    See :func:`_mcp_build_prompt` for the ``include_knowledge`` rationale."""
    knowledge = f"{_MCP_AUTHORING_KNOWLEDGE}\n" if include_knowledge else ""
    return (
        "The following FastMCP server.py failed validation and must be fixed.\n\n"
        f"{knowledge}"
        f"VALIDATION ERROR:\n{error}\n\n"
        "CURRENT server.py:\n```python\n" + code + "\n```\n\n"
        "Return ONLY the COMPLETE corrected server.py in a single ```python "
        "block — no preamble, no explanation, no text before or after the "
        "block. Fix ONLY the reported error, keep all working logic, change "
        "nothing unnecessary.\n\n"
        "REMINDER — do not regress any of these while fixing the error above "
        "(a fix for one requirement must never drop another):\n"
        '  - mcp = FastMCP("<id>", host="0.0.0.0", stateless_http=True)  '
        "— exact three arguments, every time you touch this line\n"
        "  - app = mcp.streamable_http_app()\n"
        '  - if __name__ == "__main__": mcp.run(transport=os.environ.get('
        '"MCP_TRANSPORT", "streamable-http"))\n'
        "  - no Starlette/uvicorn/custom ASGI routes (Mount/Route//health//ready)"
    )


def _mcps_create(args):
    """Create an MCP server from a spec file via a resilient pipeline:

    1. Design + code (single architect turn): compact DESIGN as JSON drives
       deterministic assembly; full server.py comes in a ```python block.
    2. Resilient interpretation: normalize whatever came back to the canonical
       design model; extract code from the fenced block (escaping-free).
    3. Deterministic validation (AST + FastMCP invariants) with up to two
       automatic repair turns feeding the exact error back to the architect.
    4. Full-design approval gate (yes/no), then scaffold ./mcps/<id>/ and
       create on HeadLabs (unless --no-deploy).
    """
    from headlabs.client import HeadLabsClient
    from headlabs.progress import ProgressReporter
    from headlabs.config import get_tenant

    spec_path = getattr(args, "spec", None)
    if not spec_path:
        print("\033[31merro: --spec <arquivo> é obrigatório para mcps create\033[0m", file=sys.stderr)
        sys.exit(2)
    spec_text = _read_spec(spec_path)

    client = HeadLabsClient()
    tenant_id = getattr(args, "tenant", None) or get_tenant()
    id_hint = getattr(args, "id", None) or ""

    print("\033[1m  HeadLabs · MCP Creation (spec-driven)\033[0m\n")

    # Prefer the dedicated mcp-architect (persona embeds the authoring
    # contract → shorter prompts, less narration, more consistent output).
    # Falls back to the generic agent-architect if it can't be reached/created
    # — the pipeline must keep working even if the platform call fails.
    architect_id = _ARCHITECT_AGENT_ID
    use_dedicated = _ensure_mcp_architect(client)
    if use_dedicated:
        architect_id = _MCP_ARCHITECT_AGENT_ID
    else:
        print("  \033[33m⚠ mcp-architect indisponível — usando agent-architect genérico\033[0m")

    def _turn(prompt, label):
        reporter = ProgressReporter(quiet=False, verbose=False)
        reporter.begin_wait(label)
        return _run_architect(client, prompt, tenant_id, reporter, agent_id=architect_id)

    # ── 1. Design + code ──────────────────────────────────────────────────────
    try:
        answer = _turn(_mcp_build_prompt(spec_text, id_hint, include_knowledge=not use_dedicated),
                       "Interpretando a spec e projetando o MCP…")
    except Exception as exc:
        print(f"  \033[31m✗ {exc}\033[0m")
        return

    # ── 2. Resilient interpretation ───────────────────────────────────────────
    # Prefer the ```json block so a '{' inside the ```python code can't be
    # mistaken for the design object.
    design = _normalize_mcp_design(
        _parse_json_draft(_extract_fenced_block(answer, ("json",)) or answer))
    if not design:
        print("  \033[31m✗ Não foi possível interpretar o design do MCP.\033[0m")
        _print_bad_draft(answer)
        return

    mcp_id = _slugify_id(id_hint or design["id"] or design["name"])
    if not mcp_id:
        print("  \033[31m✗ design sem 'id' válido — não é possível criar o MCP\033[0m")
        return
    design["id"] = mcp_id

    server_code = _sanitize_python_code(_extract_fenced_block(answer, ("python", "py")) or "")

    # ── 3. Validate (AST + behavioral) with auto-repair ───────────────────────
    # Behavioral verification is strictly stronger than AST validation (AST
    # only proves the file parses and contains the right substrings; it
    # cannot catch a wrong import path or any other runtime-only defect — see
    # `_verify_mcp_behavior`'s docstring for the real-world case that exposed
    # this gap). Both checks feed the SAME repair loop and are equally
    # blocking: a server that "compiles" but crashes on startup must never
    # reach the approval gate.
    def _check(code):
        ok, err = _validate_server_code(code)
        if not ok:
            return ok, err, None
        display_ = design["name"] or mcp_id.replace("-", " ").title()
        desc_ = design["description"] or f"MCP server: {display_}"
        with tempfile.TemporaryDirectory(prefix=f"headlabs-mcp-verify-{mcp_id}-") as tmp:
            try:
                probe_dir = _scaffold_mcp(mcp_id, display_, desc_, code,
                                          design["dependencies"], design["config"],
                                          base_dir=tmp)
                behavior_ = _verify_mcp_behavior(probe_dir)
            except Exception as exc:
                behavior_ = {"ok": False, "skipped": True, "error": str(exc)}
        if behavior_.get("ok") or behavior_.get("skipped"):
            # Verified OK, or infra couldn't run it (soft-fail — not the
            # generated code's fault, don't block on something we can't check).
            return True, "", behavior_
        return False, behavior_.get("error", "falha na verificação comportamental"), behavior_

    print("  \033[2mVerificando comportamento (subindo o server localmente)…\033[0m")
    ok, err, behavior = _check(server_code)
    attempts = 0
    while not ok and attempts < 2:
        print(f"  \033[33m⟳ Ajustando o server.py ({err})…\033[0m")
        try:
            fix = _turn(_mcp_repair_prompt(server_code, err, include_knowledge=not use_dedicated),
                        "Corrigindo o server.py…")
        except Exception as exc:
            print(f"  \033[31m✗ {exc}\033[0m")
            return
        new_code = _sanitize_python_code(_extract_fenced_block(fix, ("python", "py")) or server_code)
        new_ok, new_err, new_behavior = _check(new_code)
        # Convergence guard: if the repair produced identical code or the exact
        # same error, another round won't help — stop wasting turns.
        if new_code == server_code or (not new_ok and new_err == err):
            server_code, ok, err, behavior = new_code, new_ok, new_err, new_behavior
            break
        server_code, ok, err, behavior = new_code, new_ok, new_err, new_behavior
        attempts += 1
    if not ok:
        print(f"  \033[31m✗ server.py não passou na validação: {err}\033[0m")
        _print_bad_draft(server_code)
        return

    display = design["name"] or mcp_id.replace("-", " ").title()
    desc = design["description"] or f"MCP server: {display}"
    contract_warnings = _validate_mcp_contract(design, server_code)
    if behavior and behavior.get("ok"):
        print(f"  \033[32m✓ Verificação comportamental: protocolo MCP respondeu, "
              f"{len(behavior['tools'])} tools\033[0m")
    elif behavior and behavior.get("skipped"):
        print(f"  \033[33m⚠ Verificação comportamental não pôde rodar: "
              f"{behavior.get('error','?')[:120]}\033[0m")

    # ── 4. Approval gate ──────────────────────────────────────────────────────
    _review_mcp_draft(mcp_id, design, server_code, contract_warnings, behavior)
    if not _confirm("\n  Autorizar criação deste MCP na HeadLabs?"):
        print("  \033[33mCriação cancelada.\033[0m")
        return

    try:
        _scaffold_mcp(mcp_id, display, desc, server_code,
                      design["dependencies"], design["config"])
    except FileExistsError:
        # The design was just approved above — don't make the user re-run the
        # whole (slow) architect turn just because a stale local dir from a
        # previous/aborted attempt is in the way. Offer to remove it, gated by
        # its own explicit confirmation (this deletes a local directory only,
        # never anything on the platform).
        mcp_dir = Path("mcps") / mcp_id
        if not _confirm(f"\n  mcps/{mcp_id}/ já existe localmente. Remover e recriar?"):
            print(f"  \033[31m✗ Cancelado. Remova mcps/{mcp_id}/ manualmente ou use outro id.\033[0m")
            return
        import shutil
        shutil.rmtree(mcp_dir, ignore_errors=True)
        try:
            _scaffold_mcp(mcp_id, display, desc, server_code,
                          design["dependencies"], design["config"])
        except FileExistsError:
            print(f"  \033[31m✗ Não foi possível remover mcps/{mcp_id}/. Remova manualmente.\033[0m")
            return

    print(f"  \033[32m✓ MCP scaffolded: mcps/{mcp_id}/\033[0m")

    if getattr(args, "no_deploy", False):
        print(f"\n  Revise e publique: \033[2mheadlabs mcps push {mcp_id} --wait\033[0m")
        return

    # Create on HeadLabs (source upload + build + deploy).
    args.mcp_id = mcp_id
    args.display_name = display
    args.description = desc
    print(f"\n  \033[1mCriando na HeadLabs…\033[0m")
    return _mcps_push(args)


def _scaffold_mcp(mcp_id, display, desc, server_code, extra_reqs=None,
                  config_vars=None, base_dir=None):
    """Write a self-contained MCP project to <base_dir>/mcps/<id>/ (default:
    ./mcps/<id>/) from generated code.

    ``base_dir`` lets callers materialize the project into a scratch directory
    for behavioral verification (see ``_verify_mcp_behavior``) without
    touching the real ``./mcps/`` tree until after approval.
    ``config_vars`` (the design's config surface) is materialized as an
    ``.env.example`` so the required environment/secrets are documented.
    Raises FileExistsError if the directory already exists.
    """
    mcp_dir = os.path.join(base_dir or os.getcwd(), "mcps", mcp_id)
    if os.path.exists(mcp_dir):
        raise FileExistsError(mcp_dir)

    config_yaml = (
        "mcp:\n"
        f"  id: {mcp_id}\n"
        f'  name: "{display}"\n'
        f'  description: "{desc}"\n'
        '  version: "1.0.0"\n'
        "  transport: streamable-http\n"
        "  port: 8080\n"
    )
    reqs = ["mcp>=1.2.0", "httpx>=0.27.0"]
    for r in (extra_reqs or []):
        if r not in reqs:
            reqs.append(r)
    requirements = "\n".join(reqs) + "\n"
    dockerfile = (
        # AgentCore MCP contract (validated E2E in production): container MUST
        # serve on 0.0.0.0:8000/mcp via mcp.run() — port 8080 causes an init
        # timeout. Built for linux/arm64 to match the platform's ECR/runtime arch.
        "FROM --platform=linux/arm64 python:3.12-slim\n"
        f"ENV MCP_ID={mcp_id} PORT=8000 PYTHONUNBUFFERED=1\n"
        "WORKDIR /app\n"
        "COPY requirements.txt /tmp/requirements.txt\n"
        "RUN pip install --no-cache-dir -r /tmp/requirements.txt\n"
        "COPY . /app\n"
        "EXPOSE 8000\n"
        'CMD ["python", "server.py"]\n'
    )
    test_py = (
        '"""Smoke test: verify MCP server imports cleanly."""\n'
        "def test_import():\n"
        "    import server\n"
        '    assert hasattr(server, "mcp")\n'
    )

    os.makedirs(mcp_dir, exist_ok=True)
    os.makedirs(os.path.join(mcp_dir, "tests"), exist_ok=True)
    files = {
        "server.py": server_code if server_code.endswith("\n") else server_code + "\n",
        "config.yaml": config_yaml,
        "requirements.txt": requirements,
        "Dockerfile": dockerfile,
        "tests/__init__.py": "",
        "tests/test_server.py": test_py,
    }
    if config_vars:
        lines = [f"# Environment configuration for {display}", ""]
        for c in config_vars:
            env = c.get("env") if isinstance(c, dict) else str(c)
            if not env:
                continue
            note = []
            if isinstance(c, dict):
                if c.get("required"):
                    note.append("required")
                if c.get("secret"):
                    note.append("secret")
                if c.get("description"):
                    note.append(c["description"])
            if note:
                lines.append(f"# {' · '.join(note)}")
            default = c.get("default", "") if isinstance(c, dict) else ""
            lines.append(f"{env}={default}")
            lines.append("")
        files[".env.example"] = "\n".join(lines).rstrip("\n") + "\n"
    for name, content in files.items():
        with open(os.path.join(mcp_dir, name), "w") as f:
            f.write(content)
    return mcp_dir


def _mcps_init(args):
    """Scaffold a new MCP server in ./mcps/<id>/."""
    mcp_id = args.mcp_id
    mcp_dir = os.path.join(os.getcwd(), "mcps", mcp_id)
    if os.path.exists(mcp_dir):
        print(f"\033[31merro: mcps/{mcp_id}/ já existe.\033[0m")
        sys.exit(2)

    display = mcp_id.replace("-", " ").title()
    desc = getattr(args, "description", None) or f"MCP server: {display}"

    server_py = f'''"""MCP server — {display}."""
from mcp.server.fastmcp import FastMCP
import os

mcp = FastMCP("{mcp_id}")


@mcp.tool()
def hello(name: str = "world") -> str:
    """Example tool — replace with your implementation."""
    return f"Hello, {{name}}!"


# ── Entrypoint ────────────────────────────────────────────────────────────────
app = mcp.streamable_http_app()

if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
'''

    config_yaml = f'''mcp:
  id: {mcp_id}
  name: "{display}"
  description: "{desc}"
  version: "1.0.0"
  transport: streamable-http
  port: 8080
'''

    requirements = "mcp>=1.2.0\nhttpx>=0.27.0\nuvicorn>=0.30\n"

    dockerfile = f'''FROM python:3.12-slim
ENV MCP_ID={mcp_id} PORT=8080 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY . /app
EXPOSE 8080
CMD ["python", "server.py"]
'''

    test_py = f'''"""Smoke test: verify MCP server imports cleanly."""
def test_import():
    import server
    assert hasattr(server, "mcp")
'''

    os.makedirs(mcp_dir, exist_ok=True)
    os.makedirs(os.path.join(mcp_dir, "tests"), exist_ok=True)
    files = {
        "server.py": server_py,
        "config.yaml": config_yaml,
        "requirements.txt": requirements,
        "Dockerfile": dockerfile,
        "tests/__init__.py": "",
        "tests/test_server.py": test_py,
    }
    for name, content in files.items():
        with open(os.path.join(mcp_dir, name), "w") as f:
            f.write(content)

    print(f"\033[32m✓ MCP criado: mcps/{mcp_id}/\033[0m")
    print(f"\033[2m  server.py     tools + FastMCP entrypoint\033[0m")
    print(f"\033[2m  Dockerfile    self-contained\033[0m")
    print(f"\033[2m  config.yaml · requirements.txt · tests/\033[0m")
    print()
    print(f"  Testar local: \033[2mheadlabs mcps dev {mcp_id}\033[0m")
    print(f"  Publicar:     \033[2mheadlabs mcps push {mcp_id} --wait\033[0m")


def _mcps_push(args):
    """Push MCP source (versioned) + build + deploy."""
    import subprocess
    import base64
    from headlabs.client import HeadLabsClient
    from headlabs.config import load_config

    mcp_id = args.mcp_id
    mcp_dir = os.path.join(os.getcwd(), "mcps", mcp_id)
    if not os.path.isdir(mcp_dir):
        print(f"\033[31merro: mcps/{mcp_id}/ não encontrado.\033[0m")
        sys.exit(2)

    # Reject the one unsafe combination: a private MCP with no auth would be
    # both unreachable through the platform's authorized gateway path AND,
    # were that gate ever bypassed, unprotected. --auth none is only
    # meaningful for a public MCP (explicitly authless by design, e.g. to add
    # as a zero-friction custom connector).
    visibility = getattr(args, "visibility", None) or "private"
    auth = getattr(args, "auth", None) or "api-key"
    if visibility == "private" and auth == "none":
        print("\033[31merro: --auth none só é válido com --visibility public "
              "(um MCP private sem auth não faz sentido: ele não seria "
              "descobrível mesmo assim, e se fosse, estaria desprotegido).\033[0m")
        sys.exit(2)

    client = HeadLabsClient()
    cfg = load_config()

    # 0. Register the MCP as a platform asset if it isn't one yet (idempotent:
    # skipped when it already exists, since create_mcp is NOT safe to re-call
    # — see HeadLabsClient.create_mcp). Without this, /source and /deploy would
    # version code and provision a runtime for an id that `mcps list` (which
    # filters on kind="mcp") would never surface — a real regression this
    # closes: MCPs could be built+deployed and still be invisible.
    display_name = getattr(args, "display_name", None) or mcp_id.replace("-", " ").title()
    description = getattr(args, "description", None) or ""
    try:
        client.request("GET", f"/mcps/{mcp_id}")
    except Exception:
        try:
            client.create_mcp(mcp_id, display_name, description, framework="container")
            print(f"\033[32m  ✓ MCP registrado na plataforma: {mcp_id}\033[0m")
        except Exception as exc:
            print(f"\033[33m  (registro do MCP: {str(exc)[:100]})\033[0m")

    # 1. Collect + upload source (versioned)
    files = {}
    for root, dirs, fnames in os.walk(mcp_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache")]
        for fname in fnames:
            if fname.endswith(".pyc"):
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, mcp_dir)
            with open(full, "rb") as f:
                files[rel] = base64.b64encode(f.read()).decode()

    print(f"\033[2m  Uploading {len(files)} files…\033[0m")
    try:
        resp = client.request("POST", f"/mcps/{mcp_id}/source",
                              json={"files": files, "message": getattr(args, "message", "") or ""},
                              timeout=30)
        print(f"\033[32m  ✓ Source v{resp.get('version', '?')}\033[0m")
    except Exception as exc:
        print(f"\033[33m  (source upload: {str(exc)[:80]})\033[0m")

    # 2. Docker build + push ECR
    # Tag by content hash, not just mcp_id: AgentCore's update_agent_runtime
    # only triggers a real container replacement when containerUri actually
    # changes. ECR tags are mutable — pushing the same tag again overwrites
    # it in place, so containerUri stays byte-identical across pushes and
    # the runtime keeps serving the OLD container indefinitely (confirmed:
    # a real push with new code/env never took effect until the tag itself
    # changed). A content hash makes every real change produce a new tag,
    # forcing AgentCore to actually pull and restart.
    import hashlib
    _hasher = hashlib.sha256()
    for rel in sorted(files.keys()):
        _hasher.update(rel.encode())
        _hasher.update(files[rel].encode())
    content_hash = _hasher.hexdigest()[:12]
    image_tag = f"{mcp_id}-{content_hash}"

    ACCOUNT = "688128002471"
    REGION = "us-east-1"
    ECR = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/headlabs-mcps"
    image_uri = f"{ECR}:{image_tag}"

    print(f"\033[2m  Building {mcp_id}…\033[0m")
    r = subprocess.run(["docker", "build", "--platform", "linux/arm64", "-t", image_uri, "."],
                       cwd=mcp_dir, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"\033[31merro: docker build falhou\033[0m\n{r.stderr[-300:]}")
        sys.exit(1)
    print(f"\033[32m  ✓ Build OK\033[0m")

    profile = getattr(args, "profile", None) or os.environ.get("AWS_PROFILE", "")
    login_cmd = f"aws ecr get-login-password --region {REGION}"
    if profile:
        login_cmd += f" --profile {profile}"
    login_cmd += f" | docker login --username AWS --password-stdin {ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
    subprocess.run(login_cmd, shell=True, capture_output=True)

    print(f"\033[2m  Pushing {mcp_id}…\033[0m")
    r = subprocess.run(["docker", "push", image_uri], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"\033[31merro: docker push falhou\033[0m\n{r.stderr[-200:]}")
        sys.exit(1)
    print(f"\033[32m  ✓ Push OK\033[0m")

    # 3. Deploy (register/update MCP runtime)
    try:
        resp = client.request("POST", f"/mcps/{mcp_id}/deploy",
                              json={"image_tag": image_tag}, timeout=30)
        runtime_id = resp.get("runtime_id", "")
        print(f"\033[32m✓ MCP deployado: {mcp_id}\033[0m" +
              (f" (runtime: {runtime_id})" if runtime_id else ""))
    except Exception as exc:
        # If deploy endpoint doesn't exist yet, inform
        print(f"\033[33m  Deploy endpoint pendente: {str(exc)[:80]}\033[0m")
        print(f"\033[2m  A imagem está no ECR. O EventBridge pode triggar o deploy automaticamente.\033[0m")
        runtime_id = ""

    # 4. Publish (opt-in, matches the platform's agent-visibility governance:
    # default private, explicit publish only). The platform's own /publish
    # gate requires an active runtime_id — mirror that check here so we never
    # ask to publish something with no live runtime behind it.
    visibility = getattr(args, "visibility", None) or "private"
    auth = getattr(args, "auth", None) or "api-key"
    if visibility == "public":
        if not runtime_id:
            print(f"\033[33m  ⚠ Não publicado: sem runtime_id confirmado ainda "
                  f"(a plataforma exige um runtime ativo). Rode novamente ou "
                  f"publique manualmente depois: headlabs mcps publish {mcp_id}\033[0m")
        else:
            try:
                client.publish_mcp(mcp_id)
                print(f"\033[32m✓ MCP publicado (visibility=public): {mcp_id}\033[0m")
                if auth == "none":
                    print(f"\033[2m  Authless — qualquer cliente MCP pode conectar sem "
                          f"credencial (ex.: como custom connector no Claude).\033[0m")
                else:
                    print(f"\033[2m  Descoberto publicamente, mas o consumo ainda exige "
                          f"uma API key HeadLabs (Basic auth via ~/.headlabs/config.json).\033[0m")
            except Exception as exc:
                print(f"\033[31m  ✗ Falha ao publicar: {str(exc)[:120]}\033[0m")
    else:
        print(f"\033[2m  MCP privado (default) — descoberta/consumo restritos ao seu "
              f"tenant, autenticados pela sua API key HeadLabs "
              f"(~/.headlabs/config.json). Publique com: "
              f"headlabs mcps publish {mcp_id}\033[0m")

def _mcps_dev(args):
    """Run MCP locally via Docker on localhost:<port>."""
    import subprocess

    mcp_id = args.mcp_id
    mcp_dir = os.path.join(os.getcwd(), "mcps", mcp_id)
    if not os.path.isdir(mcp_dir):
        print(f"\033[31merro: mcps/{mcp_id}/ não encontrado.\033[0m")
        print(f"\033[2m  Crie com: headlabs mcps init {mcp_id}\033[0m")
        sys.exit(2)

    port = getattr(args, "port", 8080)
    image_tag = f"headlabs-local-mcp:{mcp_id}"

    print(f"\033[2m  Building {mcp_id}…\033[0m")
    r = subprocess.run(["docker", "build", "--platform", "linux/arm64", "-t", image_tag, "."],
                       cwd=mcp_dir, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"\033[31merro: docker build falhou\033[0m\n{r.stderr[-300:]}")
        sys.exit(1)

    print(f"\033[32m✓ MCP rodando: http://localhost:{port}/mcp\033[0m")
    print(f"\033[2m  Ctrl+C para parar\033[0m")
    print()
    try:
        subprocess.run(["docker", "run", "--rm", "-p", f"{port}:8080",
                        "--platform", "linux/arm64", image_tag])
    except KeyboardInterrupt:
        print("\n\033[2m  MCP encerrado.\033[0m")


def _mcps_pull(args):
    """Pull MCP source from the platform."""
    import base64
    from headlabs.client import HeadLabsClient

    mcp_id = args.mcp_id
    mcp_dir = os.path.join(os.getcwd(), "mcps", mcp_id)
    if os.path.isdir(mcp_dir):
        try:
            ans = input(f"  mcps/{mcp_id}/ já existe. Substituir? (y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes", "s", "sim"):
            print("\033[2m  Cancelado.\033[0m")
            return
        import shutil
        shutil.rmtree(mcp_dir)

    client = HeadLabsClient()
    try:
        params = {}
        ver = getattr(args, "version", None)
        if ver:
            params["version"] = ver
        resp = client.request("GET", f"/mcps/{mcp_id}/source", params=params)
        files = resp.get("files", {})
        source_version = resp.get("version")
    except Exception:
        files = {}
        source_version = None

    if files:
        os.makedirs(mcp_dir, exist_ok=True)
        for rel_path, b64_content in files.items():
            full = os.path.join(mcp_dir, rel_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(base64.b64decode(b64_content))
        # Always write correct Dockerfile
        with open(os.path.join(mcp_dir, "Dockerfile"), "w") as f:
            f.write(
                f"FROM python:3.12-slim\n"
                f"ENV MCP_ID={mcp_id} PORT=8080 PYTHONUNBUFFERED=1\n"
                f"WORKDIR /app\n"
                f"COPY requirements.txt /tmp/requirements.txt\n"
                f"RUN pip install --no-cache-dir -r /tmp/requirements.txt\n"
                f"COPY . /app\n"
                f"EXPOSE 8080\n"
                f'CMD ["python", "server.py"]\n'
            )
        print(f"\033[32m✓ Pull: mcps/{mcp_id}/ v{source_version} ({len(files)} files)\033[0m")
    else:
        print(f"\033[33m  Source não encontrado. Crie com: headlabs mcps init {mcp_id}\033[0m")
        return

    print(f"  \033[2mTestar: headlabs mcps dev {mcp_id}\033[0m")
    print(f"  \033[2mPush:   headlabs mcps push {mcp_id}\033[0m")


def cmd_skills(args):
    """List skills."""
    from headlabs.client import HeadLabsClient

    if hasattr(args, 'subcmd') and args.subcmd == 'create':
        return cmd_skills_create(args)
    if hasattr(args, 'subcmd') and args.subcmd == 'gotcha':
        return cmd_skills_gotcha(args)

    client = HeadLabsClient()
    skills = client.list_skills()
    if not skills:
        print("No skills found.")
        return
    print(f"{'ID':<30} {'Name':<25} Tenant")
    print("-" * 70)
    for s in skills:
        print(f"{s.get('id',''):<30} {s.get('name', s.get('id','')):<25} {s.get('tenant_id','platform')}")


def cmd_skills_create(args):
    """Create/update a skill from a file."""
    from headlabs.client import HeadLabsClient

    content = Path(args.file).read_text()
    name = args.name or args.id
    client = HeadLabsClient()
    client.create_skill(skill_id=args.id, name=name, content=content)
    print(f"✓ Skill '{args.id}' created/updated ({len(content)} chars)")


def cmd_skills_gotcha(args):
    """Append a learned gotcha to an inspection skill (the learning loop).
    Reads the skill, inserts the gotcha under the ## Gotchas section, saves it back."""
    from headlabs.client import HeadLabsClient
    client = HeadLabsClient()
    skill = client.request("GET", f"/resources/skill/{args.skill_id}")
    content = skill.get("content", "")
    bullet = f"- {args.text.strip()}"
    if "## Gotchas" in content:
        # Insert right after the Gotchas heading line
        lines = content.split("\n")
        out, inserted = [], False
        for ln in lines:
            out.append(ln)
            if not inserted and ln.strip().startswith("## Gotchas"):
                out.append(bullet)
                inserted = True
        content = "\n".join(out)
    else:
        content = content.rstrip() + "\n\n## Gotchas (learned)\n" + bullet + "\n"
    client.request("PATCH", f"/resources/skill/{args.skill_id}", json={"content": content})
    print(f"✓ Gotcha adicionado a '{args.skill_id}'. O inspector vai checar isso em toda inspeção futura.")


def cmd_tools(args):
    """List tools and MCPs."""
    from headlabs.client import HeadLabsClient

    client = HeadLabsClient()
    tools = client.list_tools()
    if not tools:
        print("No tools found.")
        return
    print(f"{'ID':<30} {'Type':<8} Name")
    print("-" * 60)
    for t in tools:
        print(f"{t['id']:<30} {t['type']:<8} {t['name']}")


def _render_chat_answer(answer: str) -> None:
    """Render an agent chat answer as a human-facing panel (rich), stripping
    emoji noise. Falls back to plain text without rich."""
    try:
        from rich.console import Console
        from rich.markdown import Markdown
        from rich.panel import Panel
        import re as _re
        _emoji_re = _re.compile("["
            "\U0001F1E0-\U0001F1FF"  # flags
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F680-\U0001F6FF"  # transport
            "\U0001F700-\U0001F77F"  # alchemical
            "\U0001F780-\U0001F7FF"  # geometric extended
            "\U0001F800-\U0001F8FF"  # supplemental arrows
            "\U0001F900-\U0001F9FF"  # supplemental symbols
            "\U0001FA00-\U0001FA6F"  # chess symbols
            "\U0001FA70-\U0001FAFF"  # symbols extended-A
            "\U00002702-\U000027B0"  # dingbats
            "\U000024C2-\U0001F251"  # enclosed chars
            "\U0000FE00-\U0000FE0F"  # variation selectors
            "\U00002600-\U000026FF"  # misc symbols
            "\U00002B50-\U00002B55"  # stars
            "\U0000200D"             # ZWJ
            "\U00002934-\U00002935"  # arrows
            "]+", flags=_re.UNICODE)
        _answer = _emoji_re.sub('', answer)
        _answer = _answer.replace('✅', '').replace('❌', '').replace('🔍', '')
        _console = Console(width=min(Console().width, 100))
        _console.print()
        _console.print(Panel(Markdown(_answer), title="Agent", border_style="dim", padding=(1, 2)))
        _console.print()
    except ImportError:
        print(f"\nAgent: {answer}\n")


def cmd_chat(args):
    """Interactive chat with an agent. Same credential model as `run`:
    the AWS profile is used to collect data LOCALLY (credentials stay local),
    and the collected data is sent to the agent so it reasons over the
    CLIENT's account — not the HeadLabs platform account.

    ``--output-format`` selects presentation: ``human`` (default; rich panels +
    live progress), ``json`` (one trace object per turn) or ``stream-json``
    (NDJSON events + a final result line per turn). In machine formats the
    prompt/banner are suppressed and each line read from stdin is one turn, so
    chat composes in pipelines."""
    import uuid
    from headlabs.client import HeadLabsClient
    from headlabs.output import make_reporter

    fmt = getattr(args, "output_format", "human") or "human"
    human = fmt == "human"

    client = HeadLabsClient()
    session_id = str(uuid.uuid4())

    # Resolve friendly name → platform chat agent id, validating it exists
    # before opening the session (clean error + suggestions on a typo).
    from headlabs import agentnames
    try:
        agent_id = agentnames.resolve_agent_id(client, args.agent, kind="chat")
    except agentnames.AgentNotFound as exc:
        _print_agent_not_found(exc)
        sys.exit(2)

    # Resolve account + region from the AWS profile and derive short-lived
    # credentials so the ephemeral agent reads the CLIENT's account (Option B).
    # Long-lived keys never leave the machine; without creds the agent fails
    # closed server-side.
    agent_input = {"tenant_id": "ALL", "aws_region": "us-east-1"}
    account_id = ""
    try:
        import boto3
        from headlabs.client import _ephemeral_credentials
        session = boto3.Session(profile_name=args.profile)
        agent_input["aws_region"] = session.region_name or "us-east-1"
        identity = session.client("sts").get_caller_identity()
        account_id = identity["Account"]
        agent_input["account_id"] = account_id
        if human:
            print(f"Account: {identity['Account']} (profile: {args.profile})")

        creds = _ephemeral_credentials(session)
        if creds:
            agent_input.update(creds)
        elif human:
            print("! Sem credenciais AWS resolvidas — a execução pode ser bloqueada pelo agente.")
    except Exception as exc:
        if human:
            print(f"! Could not resolve AWS profile '{args.profile}': {exc}")

    context = {"input": agent_input}
    # Language from config (default pt-BR)
    _lang = load_config().get("language", "pt-BR")
    context["language"] = _lang
    history = []  # client-side conversation history (user + assistant turns)

    # Tenant used to poll the chat execution. The /chat endpoint may not echo
    # the tenant, so resolve it from --tenant or config for non-platform keys.
    from headlabs.config import get_tenant
    tenant_id = getattr(args, "tenant", None) or get_tenant()

    if human:
        print(f"Chat with '{agent_id}' (session: {session_id[:8]}...)")
        print("   Type /exit or Ctrl+C to quit.\n")

    try:
        while True:
            try:
                user_input = input("You: " if human else "").strip()
            except EOFError:
                break
            if not user_input:
                continue
            if user_input in ("/exit", "/quit"):
                break

            # One trace per turn; the reporter records events and emits in the
            # selected format. The human reporter reuses the same live renderer
            # as `run` (dimmed tool lines, spinner — TTY-aware).
            reporter = make_reporter(
                fmt, workflow="chat", agent_id=agent_id, account_id=account_id,
                profile=getattr(args, "profile", ""),
                meta={"session_id": session_id, "message": user_input},
                quiet=getattr(args, "quiet", False),
                verbose=getattr(args, "verbose", False),
            )
            reporter.begin_wait("Pensando…")
            answer, err = None, None
            try:
                for event in client.chat_stream(agent_id, session_id, user_input,
                                                 context=context, history=history,
                                                 tenant_id=tenant_id,
                                                 approval_handler=reporter.prompt_approval):
                    etype = event.get("type", "")
                    if etype == "progress":
                        reporter.event(event["event"])
                    elif etype == "done":
                        answer = event.get("message", "")
                    elif etype == "error":
                        err = event.get("error", "?")
                if hasattr(reporter, "set_result"):
                    reporter.set_result({"answer": answer, "error": err,
                                         "session_id": session_id})
                reporter.finish("failed" if err else "succeeded", answer)
            except KeyboardInterrupt:
                reporter.finish("cancelled")
                if human:
                    print()
                continue
            except Exception as exc:
                reporter.finish("failed")
                if human:
                    print(f"  x {exc}")
                continue

            if human:
                if err:
                    print(f"  x Error: {err}")
                elif answer:
                    _render_chat_answer(answer)
            if answer:
                history.append({"role": "user", "content": user_input})
                history.append({"role": "assistant", "content": answer})
                history = history[-20:]  # cap context window
    except KeyboardInterrupt:
        pass
    if human:
        print("\nChat ended.")


def cmd_config(args):
    """Save configuration."""
    config = load_config()
    if args.key:
        config["api_key"] = args.key
    if getattr(args, "language", None):
        config["language"] = args.language
    save_config(config)
    print("Configuration saved to ~/.headlabs/config.json")


def cmd_completion(args):
    """Print shell tab-completion setup for agent names and commands.

    Usage:
        # bash:  add to ~/.bashrc
        eval "$(headlabs completion bash)"
        # zsh:   add to ~/.zshrc (after `autoload -U compinit && compinit`)
        eval "$(headlabs completion zsh)"
    """
    shell = getattr(args, "shell", None) or "bash"
    try:
        import argcomplete  # noqa: F401
    except ImportError:
        print("# tab-completion requires argcomplete:", file=sys.stderr)
        print("#   pip install 'headlabs[completion]'   (or: pip install argcomplete)",
              file=sys.stderr)
        sys.exit(1)
    if shell == "zsh":
        # argcomplete needs bash-completion compatibility in zsh.
        print('autoload -U bashcompinit && bashcompinit')
    print('eval "$(register-python-argcomplete headlabs)"')
    # Warm the agent-name cache so completion has names on first use.
    try:
        from headlabs.client import HeadLabsClient
        from headlabs import agentnames
        agentnames.refresh_catalog(HeadLabsClient())
    except Exception:
        pass


def cmd_report(args):
    """Open last report."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    reports = sorted(REPORTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        print("No reports found.", file=sys.stderr)
        sys.exit(1)
    path = reports[0]
    print(f"Opening: {path}")
    webbrowser.open(f"file://{path}")


def main():
    parser = argparse.ArgumentParser(prog="headlabs", description="HeadLabs AI Platform CLI")
    sub = parser.add_subparsers(dest="command")

    # ── Local agent runtime (standalone, self-hosted LLM) ───────────────────
    # Independent of `run`/`chat`/`agents`/`run --local`: those talk to the
    # HeadLabs platform (or a Dockerized platform agent). `headlabs local`
    # runs its own tool-call loop against any OpenAI-compatible endpoint
    # (vLLM, Ollama, LM Studio, TGI, ...) — no platform involved.
    p_local = sub.add_parser("local", help="Standalone agent runtime against a self-hosted LLM")
    p_local_sub = p_local.add_subparsers(dest="local_cmd")
    p_local.set_defaults(func=cmd_local)

    pl_config = p_local_sub.add_parser("config", help="Configure the self-hosted LLM endpoint")
    pl_config.add_argument("--base-url", help="Base URL of the OpenAI-compatible server (e.g. http://localhost:8000/v1)")
    pl_config.add_argument("--model", help="Model name/id as served by the endpoint")
    pl_config.add_argument("--api-key", help="API key/token, if the server requires one (often a dummy value for self-hosted)")
    pl_config.add_argument("--max-iterations", type=int, help="Max tool-call loop iterations per run (default: 30)")
    pl_config.set_defaults(func=cmd_local, local_cmd="config")

    pl_run = p_local_sub.add_parser("run", help="Run a single prompt through the local agent loop")
    pl_run.add_argument("prompt", help="Instruction for the agent")
    pl_run.add_argument("--yes", action="store_true", help="Auto-approve all tool calls (no prompts)")
    pl_run.set_defaults(func=cmd_local, local_cmd="run")

    pl_chat = p_local_sub.add_parser("chat", help="Interactive REPL against the local agent loop")
    pl_chat.add_argument("--yes", action="store_true", help="Auto-approve all tool calls (no prompts)")
    pl_chat.set_defaults(func=cmd_local, local_cmd="chat")

    from headlabs.local.inspector import ROLE_CHOICES as _INSPECT_ROLES
    pl_inspect = p_local_sub.add_parser("inspect", help="Inspect a local project directory (white-box QA)")
    pl_inspect.add_argument("directory", nargs="?", default=".", help="Project directory to inspect (default: .)")
    pl_inspect.add_argument("--role", default="qa", choices=_INSPECT_ROLES, help="Inspection specialist role (default: qa)")
    pl_inspect.add_argument("-i", "--context", dest="inspect_context", help="Extra context/focus for the inspector")
    pl_inspect.add_argument("--url", help="URL of the running front-end to inspect (e.g. http://localhost:5173)")
    pl_inspect.add_argument("--skill", action="append", metavar="ID", help="Platform skill id to inject into the prompt (repeatable)")
    pl_inspect.add_argument("--fix", action="store_true", help="Apply suggested fixes (enables edit_file + test loop)")
    pl_inspect.add_argument("--provider", default="self-hosted", choices=["self-hosted", "platform"], help="LLM backend (platform not yet available)")
    pl_inspect.add_argument("--yes", action="store_true", help="Auto-approve all tool calls (no prompts)")
    # ── Scenario 3: build/run the app locally, then inspect it ──
    pl_inspect.add_argument("--serve", action="store_true",
                            help="Build & start the project's dev server, inspect it, then tear it down (auto-detects the URL)")
    pl_inspect.add_argument("--serve-cmd", dest="serve_cmd",
                            help="Explicit command to start the dev server (overrides package.json detection)")
    pl_inspect.add_argument("--port", type=int, help="Port the dev server listens on (overrides framework default)")
    pl_inspect.add_argument("--install", action="store_true",
                            help="Run the package manager install step before building (with --serve)")
    pl_inspect.add_argument("--no-build", dest="no_build", action="store_true",
                            help="Skip the build step, start the dev server directly (with --serve)")
    # ── Authentication for login-gated / served-behind-auth pages ──
    pl_inspect.add_argument("--auth-storage", dest="auth_storage", metavar="FILE",
                            help="Path to a Playwright storageState JSON (a saved logged-in session)")
    pl_inspect.add_argument("--auth-basic", dest="auth_basic", metavar="USER:PASS",
                            help="HTTP Basic auth credentials for the inspected page")
    pl_inspect.add_argument("--auth-header", dest="auth_header", action="append", metavar="'K: V'",
                            help="Extra request header, e.g. 'Authorization: Bearer <token>' (repeatable)")
    # ── Auto form-login: log in and inspect any site in one command ──
    pl_inspect.add_argument("--login-url", dest="login_url", metavar="URL",
                            help="Login page URL — the tool fills the form, logs in, and inspects the authenticated app")
    pl_inspect.add_argument("--login-user", dest="login_user", metavar="USER",
                            help="Username/email for --login-url")
    pl_inspect.add_argument("--login-pass", dest="login_pass", metavar="PASS",
                            help="Password for --login-url (or set env HEADLABS_LOGIN_PASS)")
    pl_inspect.add_argument("--login-user-field", dest="login_user_field", metavar="CSS",
                            help="CSS selector for the username field (for non-standard login forms)")
    pl_inspect.add_argument("--login-pass-field", dest="login_pass_field", metavar="CSS",
                            help="CSS selector for the password field")
    pl_inspect.add_argument("--login-submit", dest="login_submit", metavar="CSS",
                            help="CSS selector for the submit button")
    pl_inspect.set_defaults(func=cmd_local, local_cmd="inspect")

    pl_backlog = p_local_sub.add_parser("backlog", help="Show the local inspection backlog")
    pl_backlog.add_argument("directory", nargs="?", default=".", help="Project directory (default: .)")
    pl_backlog.set_defaults(func=cmd_local, local_cmd="backlog")

    pl_lfix = p_local_sub.add_parser("fix", help="Apply fixes from the local inspection backlog")
    pl_lfix.add_argument("directory", nargs="?", default=".", help="Project directory (default: .)")
    pl_lfix.add_argument("--yes", action="store_true", help="Auto-approve all tool calls (no prompts)")
    pl_lfix.set_defaults(func=cmd_local, local_cmd="fix")

    # run
    p_run = sub.add_parser("run", help="Run an AI agent")

    # ── Marketplace (unified listing) ────────────────────────────────────────
    p_market = sub.add_parser("marketplace", aliases=["market"],
                              help="Unified marketplace: list all agents + MCPs available")
    p_market.set_defaults(func=cmd_marketplace)

    # ── Schedule ──────────────────────────────────────────────────────────────
    p_sched = sub.add_parser("schedule", help="Schedule an agent to run on cron")
    p_sched_sub = p_sched.add_subparsers(dest="sched_cmd")
    p_sched.set_defaults(func=cmd_schedule)

    ps_set = p_sched_sub.add_parser("set", help="Create/update a schedule")
    ps_set.add_argument("agent_id", help="Agent ID")
    ps_set.add_argument("--cron", required=True, help="Cron expression (e.g. '0 8 * * 1')")
    ps_set.add_argument("--question", help="Question to ask each run")
    ps_set.set_defaults(func=cmd_schedule, sched_cmd="set")

    ps_rm = p_sched_sub.add_parser("remove", help="Remove a schedule")
    ps_rm.add_argument("agent_id", help="Agent ID")
    ps_rm.set_defaults(func=cmd_schedule, sched_cmd="remove")

    ps_ls = p_sched_sub.add_parser("list", aliases=["ls"], help="List schedules")
    ps_ls.set_defaults(func=cmd_schedule, sched_cmd="list")

    # ── Webhook ───────────────────────────────────────────────────────────────
    p_hook = sub.add_parser("webhook", help="Create a webhook trigger for an agent")
    p_hook_sub = p_hook.add_subparsers(dest="hook_cmd")
    p_hook.set_defaults(func=cmd_webhook)

    ph_create = p_hook_sub.add_parser("create", help="Create webhook")
    ph_create.add_argument("agent_id", help="Agent ID")
    ph_create.set_defaults(func=cmd_webhook, hook_cmd="create")

    ph_get = p_hook_sub.add_parser("get", help="Get webhook for agent")
    ph_get.add_argument("agent_id", help="Agent ID")
    ph_get.set_defaults(func=cmd_webhook, hook_cmd="get")

    ph_rm = p_hook_sub.add_parser("remove", help="Remove webhook")
    ph_rm.add_argument("hook_id", help="Hook ID")
    ph_rm.set_defaults(func=cmd_webhook, hook_cmd="remove")

    # ── Pipeline ──────────────────────────────────────────────────────────────
    p_pipe = sub.add_parser("pipeline", help="Agent composition (A → B → C)")
    p_pipe_sub = p_pipe.add_subparsers(dest="pipe_cmd")
    p_pipe.set_defaults(func=cmd_pipeline)

    pp_create = p_pipe_sub.add_parser("create", help="Create a pipeline")
    pp_create.add_argument("--name", required=True, help="Pipeline name")
    pp_create.add_argument("--steps", required=True, help="Agents comma-separated (A,B,C)")
    pp_create.add_argument("--description", help="Description")
    pp_create.set_defaults(func=cmd_pipeline, pipe_cmd="create")

    pp_run = p_pipe_sub.add_parser("run", help="Execute a pipeline")
    pp_run.add_argument("pipeline_id", help="Pipeline ID")
    pp_run.add_argument("--input", help="JSON input for first step")
    pp_run.set_defaults(func=cmd_pipeline, pipe_cmd="run")

    pp_ls = p_pipe_sub.add_parser("list", aliases=["ls"], help="List pipelines")
    pp_ls.set_defaults(func=cmd_pipeline, pipe_cmd="list")

    pp_get = p_pipe_sub.add_parser("status", help="Get pipeline / last run")
    pp_get.add_argument("pipeline_id", help="Pipeline ID")
    pp_get.set_defaults(func=cmd_pipeline, pipe_cmd="status")
    p_run.add_argument("agent", help="Agent name (e.g. finops)").completer = _agent_completer
    p_run.add_argument("--profile", required=True, help="AWS profile name")
    p_run.add_argument("--account-id", help="Target AWS account ID (defaults to profile's account)")
    p_run.add_argument("--days", type=int, default=30, help="Days of data to analyze")
    p_run.add_argument("--question", help="Ask a specific question")
    p_run.add_argument("--quiet", action="store_true", help="Suppress progress output")
    p_run.add_argument("--verbose", action="store_true", help="Show every progress event")
    p_run.add_argument("--local", action="store_true", help="Run the agent locally via Docker (from ./agents/<id>/)")
    p_run.add_argument("--output", choices=["json", "html", "md"], default="html", help="Report file format saved to ./reports/")
    p_run.add_argument("--output-format", dest="output_format",
                       choices=["human", "json", "stream-json"], default="human",
                       help="stdout format: human (default), json (final trace), stream-json (NDJSON events)")
    p_run.add_argument("--no-browser", action="store_true", help="Don't open browser")
    p_run.set_defaults(func=cmd_run)

    # agents (alias: agent)
    p_agents = sub.add_parser("agents", aliases=["agent"], help="List or create agents")
    p_agents_sub = p_agents.add_subparsers(dest="subcmd")
    p_agents.set_defaults(func=cmd_agents)
    # agents list (alias for bare `agents`)
    p_agents_sub.add_parser("list", aliases=["ls"], help="List agents").set_defaults(func=cmd_agents, subcmd=None)
    # agents create — no flags launches the agentic creation wizard
    p_ac = p_agents_sub.add_parser("create", help="Create an agent (inline or interactive)")
    p_ac.add_argument("intent", nargs="?", help="What you want (inline, one-shot creation)")
    p_ac.add_argument("--spec", help="Path to a spec file; the architect interprets it and creates the agent non-interactively")
    p_ac.add_argument("--id", help="Agent ID (lowercase, hyphens). Omit for the guided wizard.")
    p_ac.add_argument("--name", help="Display name (defaults to id)")
    p_ac.add_argument("--prompt", help="System prompt (inline)")
    p_ac.add_argument("--prompt-file", help="System prompt from file")
    p_ac.add_argument("--model", default="us.anthropic.claude-sonnet-4-5-20250929-v1:0", help="Model ID")
    p_ac.add_argument("--tools", help="Native tools (comma-separated)")
    p_ac.add_argument("--description", help="Description")
    p_ac.add_argument("--tenant", help="Tenant ID for polling the wizard session")
    p_ac.add_argument("--verbose", action="store_true", help="Show every progress event")
    p_ac.set_defaults(func=cmd_agents, subcmd="create")

    # agents update — incremental adjustment via NLP instruction
    p_au = p_agents_sub.add_parser("update", help="Incrementally adjust an agent via NLP instruction")
    p_au.add_argument("--id", required=True, help="Agent ID to update")
    p_au.add_argument("--instruction", "-i", required=True, help="What to change (natural language)")
    p_au.add_argument("--profile", help="AWS profile (for deploy if code agent)")
    p_au.set_defaults(func=cmd_agents, subcmd="update")

    # agents test — adversarial autocritical evaluation
    p_at = p_agents_sub.add_parser("test", help="Adversarial test: critic agent evaluates another agent")
    p_at.add_argument("agent_id", help="Agent ID to test").completer = _agent_completer
    p_at.add_argument("--profile", help="AWS profile (for invoking the agent)")
    p_at.add_argument("--fix", action="store_true", help="Auto-apply recommendations via update")
    p_at.add_argument("--scenario", help="Custom test scenario (otherwise critic generates them)")
    p_at.add_argument("--tools", action="store_true", help="Focus on tool/MCP connectivity: invoke and report which tools succeed/fail")
    p_at.add_argument("--reasoning", action="store_true", help="Evaluate reasoning quality: task completion, coherence, relevancy, efficiency (LLM-as-judge)")
    p_at.add_argument("--output-format", dest="output_format",
                      choices=["human", "json"], default="human",
                      help="human (default) or json (structured test report + baseline comparison)")
    p_at.set_defaults(func=cmd_agents, subcmd="test")

    # agents deploy
    p_ad = p_agents_sub.add_parser("deploy", help="Deploy an agent (build+push+update runtime)")
    p_ad.add_argument("agent_id", help="Agent ID to deploy")
    p_ad.add_argument("--tag", help="ECR image tag (default: agent_id; loop agents auto-use loops-latest)")
    p_ad.add_argument("--profile", help="AWS profile for ECR auth (default: env AWS_PROFILE)")
    p_ad.add_argument("--force", action="store_true", help="Skip extended health check")
    p_ad.add_argument("--wait", action="store_true", help="Block until deployment completes")
    p_ad.set_defaults(func=cmd_agents, subcmd="deploy")

    # agents init
    p_ai = p_agents_sub.add_parser("init", help="Scaffold a new code agent (only name required)")
    p_ai.add_argument("agent_id", help="Agent ID (lowercase, hyphens)")
    p_ai.add_argument("--model", help="Bedrock model ID (default: sonnet)")
    p_ai.add_argument("--tools", help="Tools comma-separated (default: web_search,web_fetch)")
    p_ai.add_argument("--framework", choices=["strands", "langgraph"], help="Default: strands")
    p_ai.add_argument("--memory", choices=["null", "short", "medium"], help="Default: null")
    p_ai.add_argument("--prompt", help="System prompt inline")
    p_ai.add_argument("--prompt-file", dest="prompt_file", help="System prompt from file")
    p_ai.add_argument("--schema", help="Path to a custom schema.py file")
    p_ai.set_defaults(func=cmd_agents, subcmd="init")

    # agents push
    p_ap = p_agents_sub.add_parser("push", help="Push local agent to platform (upload source + deploy)")
    p_ap.add_argument("agent_id", help="Agent ID (must exist in ./agents/<id>/)")
    p_ap.add_argument("--profile", help="AWS profile for ECR auth")
    p_ap.add_argument("--message", "-m", help="Version commit message")
    p_ap.add_argument("--wait", action="store_true", help="Block until deploy completes")
    p_ap.set_defaults(func=cmd_agents, subcmd="push")

    # agents pull
    p_apl = p_agents_sub.add_parser("pull", help="Pull a remote agent's source to ./agents/<id>/")
    p_apl.add_argument("agent_id", help="Agent ID to pull")
    p_apl.add_argument("--version", type=int, help="Pull a specific version (default: latest/production)")
    p_apl.set_defaults(func=cmd_agents, subcmd="pull")

    # agents delete
    p_adel = p_agents_sub.add_parser("delete", aliases=["rm"], help="Delete an agent from the platform (irreversible)")
    p_adel.add_argument("agent_id", help="Agent ID to delete")
    p_adel.add_argument("--local", action="store_true",
                        help="No-op (kept for backward compat) — local ./agents/<id>/ is always removed if present")
    p_adel.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    p_adel.set_defaults(func=cmd_agents, subcmd="delete")

    # skills
    p_skills = sub.add_parser("skills", help="List or create skills")
    p_skills_sub = p_skills.add_subparsers(dest="subcmd")
    p_skills.set_defaults(func=cmd_skills)
    # skills create
    p_sc = p_skills_sub.add_parser("create", help="Create/update a skill from file")
    p_sc.add_argument("--id", required=True, help="Skill ID")
    p_sc.add_argument("--name", help="Display name")
    p_sc.add_argument("--file", required=True, help="Markdown file with skill content")
    p_sc.set_defaults(func=cmd_skills, subcmd="create")

    # skills gotcha — append a learned failure pattern to an inspection skill
    p_sg = p_skills_sub.add_parser("gotcha", help="Add a learned gotcha to an inspection skill (learning loop)")
    p_sg.add_argument("skill_id", help="Skill id (e.g. qa-inspection)")
    p_sg.add_argument("-i", "--text", required=True, help="The gotcha to encode (a failure pattern to catch)")
    p_sg.set_defaults(func=cmd_skills, subcmd="gotcha")

    # tools
    p_tools = sub.add_parser("tools", help="List available tools and MCPs")
    p_tools.set_defaults(func=cmd_tools)

    # ── MCPs lifecycle ────────────────────────────────────────────────────────
    p_mcps = sub.add_parser("mcps", aliases=["mcp"], help="MCP servers (init/push/pull/dev)")
    p_mcps_sub = p_mcps.add_subparsers(dest="mcps_cmd")
    p_mcps.set_defaults(func=cmd_mcps)

    pm_init = p_mcps_sub.add_parser("init", help="Scaffold a new MCP server (only name required)")
    pm_init.add_argument("mcp_id", help="MCP ID (lowercase, hyphens)")
    pm_init.add_argument("--description", help="What this MCP does")
    pm_init.set_defaults(func=cmd_mcps, mcps_cmd="init")

    pm_create = p_mcps_sub.add_parser("create", help="Create an MCP from a spec file (architect-designed)")
    pm_create.add_argument("--spec", required=True, help="Path to the MCP specification file")
    pm_create.add_argument("--id", help="Force the MCP id (else derived from the spec)")
    pm_create.add_argument("--profile", help="AWS profile for ECR auth (deploy step)")
    pm_create.add_argument("-m", "--message", help="Version commit message")
    pm_create.add_argument("--tenant", help="Tenant ID for the architect session")
    pm_create.add_argument("--no-deploy", dest="no_deploy", action="store_true",
                           help="Scaffold locally only; skip build/deploy to HeadLabs")
    pm_create.add_argument("--visibility", choices=["private", "public"], default="private",
                           help="private (default, safe): only your tenant can discover/call it. "
                                "public: auto-published after a successful deploy (requires an "
                                "active runtime — matches the platform's own /publish gate)")
    pm_create.add_argument("--auth", choices=["api-key", "none"], default="api-key",
                           help="Consumption auth for the MCP endpoint itself. api-key (default): "
                                "consumers authenticate with a HeadLabs API key (Basic auth via "
                                "~/.headlabs/config.json), required for private MCPs. none: "
                                "authless — only valid combined with --visibility public (a private "
                                "MCP with no auth would be unreachable AND unprotected, so this "
                                "combination is rejected)")
    pm_create.add_argument("--wait", action="store_true", help="Block until deploy completes")
    pm_create.set_defaults(func=cmd_mcps, mcps_cmd="create")

    pm_push = p_mcps_sub.add_parser("push", help="Push local MCP to platform (version + deploy)")
    pm_push.add_argument("mcp_id", help="MCP ID (must exist in ./mcps/<id>/)")
    pm_push.add_argument("--profile", help="AWS profile for ECR auth")
    pm_push.add_argument("-m", "--message", help="Version commit message")
    pm_push.add_argument("--display-name", dest="display_name",
                         help="Display name if the MCP needs to be registered (default: id title-cased)")
    pm_push.add_argument("--description", help="Description if the MCP needs to be registered")
    pm_push.add_argument("--visibility", choices=["private", "public"], default="private",
                         help="private (default, safe) or public (auto-published after a "
                              "successful deploy with an active runtime)")
    pm_push.add_argument("--auth", choices=["api-key", "none"], default="api-key",
                         help="Consumption auth for the MCP endpoint (see `mcps create --help`)")
    pm_push.add_argument("--wait", action="store_true", help="Block until deploy completes")
    pm_push.set_defaults(func=cmd_mcps, mcps_cmd="push")

    pm_pull = p_mcps_sub.add_parser("pull", help="Pull a remote MCP's source to ./mcps/<id>/")
    pm_pull.add_argument("mcp_id", help="MCP ID to pull")
    pm_pull.add_argument("--version", type=int, help="Pull a specific version")
    pm_pull.set_defaults(func=cmd_mcps, mcps_cmd="pull")

    pm_dev = p_mcps_sub.add_parser("dev", help="Run MCP locally (localhost:8080)")
    pm_dev.add_argument("mcp_id", help="MCP ID (must exist in ./mcps/<id>/)")
    pm_dev.add_argument("--port", type=int, default=8080, help="Local port (default: 8080)")
    pm_dev.set_defaults(func=cmd_mcps, mcps_cmd="dev")

    pm_conn = p_mcps_sub.add_parser("connect", help="Store tenant credentials for an MCP")
    pm_conn.add_argument("mcp_id", help="MCP ID to connect")
    pm_conn.add_argument("--token", help="API token/key (prompted if omitted)")
    pm_conn.set_defaults(func=cmd_mcps, mcps_cmd="connect")

    pm_pub = p_mcps_sub.add_parser("publish", help="Publish an MCP (visibility=public; requires an active runtime)")
    pm_pub.add_argument("mcp_id", help="MCP ID to publish")
    pm_pub.set_defaults(func=cmd_mcps, mcps_cmd="publish")

    pm_unpub = p_mcps_sub.add_parser("unpublish", help="Unpublish an MCP (visibility=private; instant kill-switch)")
    pm_unpub.add_argument("mcp_id", help="MCP ID to unpublish")
    pm_unpub.set_defaults(func=cmd_mcps, mcps_cmd="unpublish")

    pm_delete = p_mcps_sub.add_parser("delete", aliases=["rm"], help="Delete an MCP from the platform (irreversible)")
    pm_delete.add_argument("mcp_id", help="MCP ID to delete")
    pm_delete.add_argument("--local", action="store_true",
                           help="No-op (kept for backward compat) — local ./mcps/<id>/ is always removed if present")
    pm_delete.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt")
    pm_delete.set_defaults(func=cmd_mcps, mcps_cmd="delete")

    pm_test = p_mcps_sub.add_parser("test", help="Test an MCP: connect, discover tools, validate schemas")
    pm_test.add_argument("mcp_id", help="MCP ID to test")
    pm_test.add_argument("--url", help="Custom endpoint URL (overrides auto-resolution)")
    pm_test.add_argument("--local", action="store_true", help="Test against localhost:8000")
    pm_test.add_argument("--port", type=int, default=8000, help="Local port (with --local)")
    pm_test.add_argument("--invoke", action="store_true", help="Also invoke each tool with minimal test args")
    pm_test.set_defaults(func=cmd_mcps, mcps_cmd="test")

    pm_list = p_mcps_sub.add_parser("list", aliases=["ls"], help="List MCPs on the platform")
    pm_list.set_defaults(func=cmd_mcps, mcps_cmd="list")

    # ── Trigger (event-driven agent invocation) ───────────────────────────────
    p_trigger = sub.add_parser("trigger", help="Event triggers: MCP event → agent invocation")
    p_trigger_sub = p_trigger.add_subparsers(dest="trigger_cmd")
    p_trigger.set_defaults(func=cmd_trigger)

    pt_set = p_trigger_sub.add_parser("set", help="Set an event trigger")
    pt_set.add_argument("agent_id", help="Agent to invoke")
    pt_set.add_argument("--source", required=True, help="MCP source (e.g. slack, github)")
    pt_set.add_argument("--event", help="Event type (e.g. message, pull_request.opened)")
    pt_set.add_argument("--filter", help="Filter: key=value,key2=value2")
    pt_set.set_defaults(func=cmd_trigger, trigger_cmd="set")

    pt_ls = p_trigger_sub.add_parser("list", aliases=["ls"], help="List triggers")
    pt_ls.set_defaults(func=cmd_trigger, trigger_cmd="list")

    pt_rm = p_trigger_sub.add_parser("remove", help="Remove a trigger")
    pt_rm.add_argument("agent_id", help="Agent ID")
    pt_rm.add_argument("trigger_id", help="Trigger ID to remove")
    pt_rm.set_defaults(func=cmd_trigger, trigger_cmd="remove")

    # chat
    p_chat = sub.add_parser("chat", help="Interactive chat with an agent")
    p_chat.add_argument("agent", help="Agent ID").completer = _agent_completer
    p_chat.add_argument("--profile", default="default", help="AWS profile name")
    p_chat.add_argument("--tenant", help="Tenant ID for polling (defaults to config 'tenant', then 'platform')")
    p_chat.add_argument("--output-format", dest="output_format",
                        choices=["human", "json", "stream-json"], default="human",
                        help="stdout format: human (default), json (trace/turn), stream-json (NDJSON)")
    p_chat.add_argument("--quiet", action="store_true", help="Suppress progress output")
    p_chat.add_argument("--verbose", action="store_true", help="Show every progress event")
    p_chat.set_defaults(func=cmd_chat)

    # config
    p_config = sub.add_parser("config", help="Configure HeadLabs CLI")
    p_config.add_argument("--key", help="API key")
    p_config.add_argument("--language", help="Output language (e.g. pt-BR, en, es)")
    p_config.set_defaults(func=cmd_config)

    # completion (shell tab-completion setup)
    p_comp = sub.add_parser("completion", help="Print shell tab-completion setup (bash/zsh)")
    p_comp.add_argument("shell", nargs="?", choices=["bash", "zsh"], default="bash",
                        help="Target shell (default: bash)")
    p_comp.set_defaults(func=cmd_completion)

    # report
    p_report = sub.add_parser("report", help="Open reports")
    p_report.add_argument("--last", action="store_true", help="Open last report")
    p_report.set_defaults(func=cmd_report)

    # ── trace (observability) ────────────────────────────────────────────────
    from headlabs import tracectl
    p_trace = sub.add_parser("trace", help="Inspect, compare, and export agent execution traces")
    trace_sub = p_trace.add_subparsers(dest="trace_cmd")
    p_trace.set_defaults(func=tracectl.cmd_trace)

    tl = trace_sub.add_parser("list", aliases=["ls"], help="List recent traces (newest first)")
    tl.add_argument("--limit", type=int, default=20, help="Max traces to show")
    tl.add_argument("--agent", help="Filter by agent id")
    tl.add_argument("--workflow", help="Filter by workflow (run|chat|test)")
    tl.add_argument("-o", "--output", default="table", choices=["table", "json"])
    tl.set_defaults(func=tracectl.cmd_trace, trace_cmd="list")

    tshow = trace_sub.add_parser("show", help="Show a trace's full timeline")
    tshow.add_argument("trace_id", help="Trace id or unique prefix")
    tshow.add_argument("-o", "--output", default="table", choices=["table", "json"])
    tshow.set_defaults(func=tracectl.cmd_trace, trace_cmd="show")

    tdiff = trace_sub.add_parser("diff", help="Compare two traces")
    tdiff.add_argument("trace_a", help="Baseline trace id or prefix")
    tdiff.add_argument("trace_b", help="Comparison trace id or prefix")
    tdiff.add_argument("-o", "--output", default="table", choices=["table", "json"])
    tdiff.set_defaults(func=tracectl.cmd_trace, trace_cmd="diff")

    texp = trace_sub.add_parser("export", help="Export a trace (OTel/OTLP or raw JSON)")
    texp.add_argument("trace_id", help="Trace id or unique prefix")
    texp.add_argument("--format", default="otel", choices=["otel", "raw"],
                      help="otel = OTLP/JSON GenAI spans; raw = native trace JSON")
    texp.add_argument("--endpoint", help="OTLP/HTTP collector endpoint to POST to (otel only)")
    texp.set_defaults(func=tracectl.cmd_trace, trace_cmd="export")

    # ── labs (workspaces) ────────────────────────────────────────────────────
    def _add_common(p, *, output=True, watch=False, wait=False, tenant=False):
        if output:
            p.add_argument("-o", "--output", default="table", choices=["table", "wide", "json"])
        if watch:
            p.add_argument("-w", "--watch", action="store_true", help="Follow live")
        if wait:
            p.add_argument("--wait", action="store_true", help="Block until terminal (CI)")
        if tenant:
            p.add_argument("--tenant", help="Tenant for polling")
        p.add_argument("--quiet", action="store_true", help="IDs only / minimal output")
        p.add_argument("--verbose", action="store_true")

    p_labs = sub.add_parser("labs", aliases=["lab"], help="Project labs (workspaces grouping build loops)")
    labs_sub = p_labs.add_subparsers(dest="labs_cmd")
    p_labs.set_defaults(func=labsctl.cmd_labs)

    lc = labs_sub.add_parser("create", help="Create a lab and start the first build")
    lc.add_argument("-i", "--intent", required=True, help="Build objective (natural language)")
    lc.add_argument("--name", help="Lab name (default: slug of intent)")
    lc.add_argument("--stack", help="Tech stack, comma-separated (e.g. python,fastapi,cdk)")
    lc.add_argument("--auto-approve", dest="auto_approve", action="store_true", help="Resolve all gates automatically")
    lc.add_argument("--dry-run", dest="dry_run", action="store_true", help="Validate contract (orchestrator→architect→planner), create NO resources")
    lc.add_argument("--gate", help="Gates to KEEP: architecture,plan,destructive")
    lc.add_argument("--judges", choices=["off", "gate", "full"], help="Judge panel policy (default off)")
    lc.add_argument("--judge-model", dest="judge_model", choices=["fast", "standard"], help="Judge model (fast=Haiku, cheaper)")
    lc.add_argument("--gate-mode", dest="gate_mode", choices=["human", "judge", "judge+human"],
                    help="human=no panel; judge=panel auto-decides; judge+human=panel informs, human decides")
    lc.add_argument("--max-revise", dest="max_revise", type=int, help="Max auto-revise loops before escalating to human (judge mode, default 2)")
    _add_common(lc, watch=True, wait=True, tenant=True)
    lc.set_defaults(func=labsctl.cmd_labs, labs_cmd="create")

    ll = labs_sub.add_parser("list", aliases=["ls"], help="List labs")
    _add_common(ll)
    ll.set_defaults(func=labsctl.cmd_labs, labs_cmd="list")

    for _verb in ("get", "describe"):
        g = labs_sub.add_parser(_verb, help=f"{_verb} a lab")
        g.add_argument("lab", help="Lab id or name")
        _add_common(g)
        g.set_defaults(func=labsctl.cmd_labs, labs_cmd=_verb)

    lr = labs_sub.add_parser("repo", help="Browse the lab's repository")
    lr.add_argument("lab", help="Lab id or name")
    lr.add_argument("--tree", action="store_true", help="List files (default)")
    lr.add_argument("--cat", help="Print a file's content by path")
    _add_common(lr)
    lr.set_defaults(func=labsctl.cmd_labs, labs_cmd="repo")

    lp = labs_sub.add_parser("push", help="Push the lab repository to GitHub")
    lp.add_argument("lab", help="Lab id or name")
    lp.add_argument("--repo", required=True, help="owner/name")
    lp.add_argument("--branch", default="main")
    lp.add_argument("--token", help="GitHub token (or env GITHUB_TOKEN)")
    lp.add_argument("--message", help="Commit message")
    _add_common(lp)
    lp.set_defaults(func=labsctl.cmd_labs, labs_cmd="push")

    larch = labs_sub.add_parser("archive", help="Archive a lab")
    larch.add_argument("lab", help="Lab id or name")
    larch.set_defaults(func=labsctl.cmd_labs, labs_cmd="archive")

    lo = labs_sub.add_parser("outputs", help="Show the lab's outputs — created resources + ready-to-use endpoints/URLs")
    lo.add_argument("lab", help="Lab id or name")
    _add_common(lo)
    lo.set_defaults(func=labsctl.cmd_labs, labs_cmd="outputs")

    lrb = labs_sub.add_parser("rebuild", help="Destroy the lab's resources and rebuild fresh from scratch (reuses research)")
    lrb.add_argument("lab", help="Lab id or name")
    lrb.add_argument("-i", "--intent", required=True, help="Instruction refining the original intent")
    lrb.add_argument("--auto-approve", dest="auto_approve", action="store_true",
                     help="Disable gates for this rebuild (rapid prototyping)")
    _add_common(lrb, watch=True, wait=True, tenant=True)
    lrb.set_defaults(func=labsctl.cmd_labs, labs_cmd="rebuild")

    lbl = labs_sub.add_parser("backlog", help="Show the lab's inspection backlog (issues/fixes to address)")
    lbl.add_argument("lab", help="Lab id or name")
    _add_common(lbl)
    lbl.set_defaults(func=labsctl.cmd_labs, labs_cmd="backlog")

    lfix = labs_sub.add_parser("fix", help="Trigger a targeted remediation from the lab's OPEN backlog (no re-inspection)")
    lfix.add_argument("lab", help="Lab id or name")
    lfix.add_argument("-i", "--intent", help="Additional context for the remediation")
    lfix.add_argument("--loop", help="Specific loop id to remediate (default: the build the backlog items came from)")
    _add_common(lfix, watch=True, wait=True, tenant=True)
    lfix.set_defaults(func=labsctl.cmd_labs, labs_cmd="fix")

    linsp = labs_sub.add_parser("inspect", help="Run QA/specialist inspection on the lab's product")
    linsp.add_argument("lab", help="Lab id or name")
    linsp.add_argument("--role", choices=["qa", "ux", "security", "architect", "performance", "devops", "data", "frontend", "backend"],
                       default="qa", help="Inspector role (default: qa)")
    linsp.add_argument("-i", "--intent", dest="inspect_intent", help="Additional context/question for the inspector")
    linsp.add_argument("--loop", help="Specific loop id (default: latest build in the lab)")
    linsp.add_argument("--exec-id", dest="exec_id",
                       help="Resume polling an inspection already in progress (printed if you Ctrl+C "
                            "out of a previous `inspect` call) instead of starting a new one")
    linsp.add_argument("--fix", action="store_true", help="If issues found, trigger executor fix cycle")
    _add_common(linsp, watch=True, wait=True, tenant=True)
    linsp.set_defaults(func=labsctl.cmd_labs, labs_cmd="inspect")

    # ── loops (build jobs) ────────────────────────────────────────────────────
    p_loops = sub.add_parser("loops", aliases=["loop"], help="Build loops (jobs) inside labs")
    loops_sub = p_loops.add_subparsers(dest="loops_cmd")
    p_loops.set_defaults(func=labsctl.cmd_loops)

    oc = loops_sub.add_parser("create", help="Start a build in an existing lab")
    oc.add_argument("--lab", required=True, help="Lab id or name")
    oc.add_argument("-i", "--intent", required=True, help="Build objective")
    oc.add_argument("--auto-approve", dest="auto_approve", action="store_true")
    oc.add_argument("--dry-run", dest="dry_run", action="store_true", help="Validate contract, create NO resources")
    oc.add_argument("--gate", help="Gates to KEEP: architecture,plan,destructive")
    oc.add_argument("--judges", choices=["off", "gate", "full"], help="Judge panel policy (default off)")
    oc.add_argument("--judge-model", dest="judge_model", choices=["fast", "standard"], help="Judge model (fast=Haiku)")
    oc.add_argument("--gate-mode", dest="gate_mode", choices=["human", "judge", "judge+human"],
                    help="human=no panel; judge=panel auto-decides; judge+human=panel informs, human decides")
    oc.add_argument("--max-revise", dest="max_revise", type=int, help="Max auto-revise loops before escalating (judge mode, default 2)")
    _add_common(oc, watch=True, wait=True, tenant=True)
    oc.set_defaults(func=labsctl.cmd_loops, loops_cmd="create")

    ol = loops_sub.add_parser("list", aliases=["ls"], help="List builds")
    ol.add_argument("--lab", help="Filter by lab id or name")
    ol.add_argument("--status", help="Filter by status")
    ol.add_argument("--active", action="store_true", help="Only non-terminal builds")
    ol.add_argument("--mode", choices=["build", "research"], help="Filter by job mode")
    _add_common(ol)
    ol.set_defaults(func=labsctl.cmd_loops, loops_cmd="list")

    for _verb in ("status", "get", "describe"):
        o = loops_sub.add_parser(_verb, help=f"{_verb} a build")
        o.add_argument("job_id", help="Build (loop) id")
        _add_common(o)
        o.set_defaults(func=labsctl.cmd_loops, loops_cmd=_verb)

    ow = loops_sub.add_parser("watch", help="Follow a build live")
    ow.add_argument("job_id")
    ow.add_argument("--timeout", type=int, default=0, help="Seconds (0 = no timeout)")
    _add_common(ow, tenant=True)
    ow.set_defaults(func=labsctl.cmd_loops, loops_cmd="watch")

    og = loops_sub.add_parser("logs", help="Show the build's agent trace (waterfall); -w to follow live")
    og.add_argument("job_id")
    og.add_argument("--phase", help="Filter by phase/agent")
    _add_common(og, watch=True)
    og.set_defaults(func=labsctl.cmd_loops, loops_cmd="logs")

    oa = loops_sub.add_parser("approve", help="Approve the pending gate")
    oa.add_argument("job_id")
    oa.add_argument("--note", help="Optional comment")
    _add_common(oa, watch=True, tenant=True)
    oa.set_defaults(func=labsctl.cmd_loops, loops_cmd="approve")

    orj = loops_sub.add_parser("reject", help="Reject the pending gate (redo previous phase)")
    orj.add_argument("job_id")
    orj.add_argument("--note", required=True, help="Reason / feedback")
    _add_common(orj)
    orj.set_defaults(func=labsctl.cmd_loops, loops_cmd="reject")

    for _verb in ("pause", "resume", "cancel", "retry"):
        o = loops_sub.add_parser(_verb, help=f"{_verb} a build")
        o.add_argument("job_id")
        _add_common(o, watch=(_verb == "retry"), tenant=(_verb == "retry"))
        o.set_defaults(func=labsctl.cmd_loops, loops_cmd=_verb)

    oi = loops_sub.add_parser("iterate", help="New iteration with an adjustment")
    oi.add_argument("job_id")
    oi.add_argument("-i", "--intent", required=True, help="Adjustment to apply")
    _add_common(oi, watch=True, tenant=True)
    oi.set_defaults(func=labsctl.cmd_loops, loops_cmd="iterate")

    orv = loops_sub.add_parser("review", help="Convene the senior review panel on the gate")
    orv.add_argument("job_id")
    orv.add_argument("--reviewers", help="Competency seniors to convene (comma-sep): security,frontend,...")
    orv.add_argument("--judges", choices=["gate", "full"], help="gate=only the gate senior; full=+competency seniors")
    orv.add_argument("--judge-model", dest="judge_model", choices=["fast", "standard"], help="Judge model (fast=Haiku)")
    _add_common(orv, watch=True, tenant=True)
    orv.set_defaults(func=labsctl.cmd_loops, loops_cmd="review")

    opn = loops_sub.add_parser("panel", help="Show the senior review panel's assessment")
    opn.add_argument("job_id")
    _add_common(opn)
    opn.set_defaults(func=labsctl.cmd_loops, loops_cmd="panel")

    # ── research (mode="research" — investigate, don't build) ─────────────────
    # One simple command: `headlabs research "<tema>"`. Defaults already do the
    # right thing — deep investigation across all available sources — so no
    # flags are needed for the common case. Flags are only for exceptions.
    # Follow-up (list/status/watch) reuses the loop surface, which is mode-aware
    # and renders findings: `headlabs status <id>`, `headlabs loops watch <id>`,
    # `headlabs loops list --mode research`.
    p_research = sub.add_parser("research", aliases=["rsch"],
                                help="Investigate a topic (amplified web search + broad research agent) — returns findings, no build")
    p_research.add_argument("intent", nargs="?", default=argparse.SUPPRESS,
                            help="Topic/question to investigate (natural language)")
    p_research.add_argument("-i", "--intent", dest="intent", default=None,
                            help=argparse.SUPPRESS)  # back-compat alias for the positional
    p_research.add_argument("--lab", help="Accumulate findings in an existing lab (id or name); default: create a fresh one")
    p_research.add_argument("--name", help="Lab name when creating one (default: slug of the topic)")
    p_research.add_argument("--stack", help="Optional domain tags, comma-separated")
    p_research.add_argument("--depth", choices=["quick", "standard", "deep", "exhaustive"], default="deep",
                            help="Investigation depth (default: deep)")
    p_research.add_argument("--sources", help="Restrict sources, comma-separated (default: all available, e.g. web,docs,repo)")
    _add_common(p_research, watch=True, wait=True, tenant=True)
    p_research.set_defaults(func=labsctl.cmd_research, research_cmd=None)

    # research build — top-level sibling (avoids subparser conflict with positional intent)
    rb = sub.add_parser("research-build", aliases=["rb"],
                        help="Build a solution from a lab's research findings (starts at architect)")
    rb.add_argument("-i", "--intent", required=True, help="What to build (uses research findings as context)")
    rb.add_argument("--lab", required=True, help="Lab with research findings (id or name)")
    rb.add_argument("--judges", choices=["off", "gate", "full"], help="Judge panel policy")
    rb.add_argument("--judge-model", dest="judge_model", choices=["fast", "standard"], help="Judge model")
    rb.add_argument("--gate-mode", dest="gate_mode", choices=["human", "judge", "judge+human"], help="Gate decision mode")
    _add_common(rb, watch=True, wait=True, tenant=True)
    rb.set_defaults(func=labsctl.cmd_research, research_cmd="build")

    # ── status (top-level shortcut) ───────────────────────────────────────────
    p_status = sub.add_parser("status", help="Active builds (no arg) or a build's detail")
    p_status.add_argument("job_id", nargs="?", help="Build id (optional)")
    _add_common(p_status, tenant=True)
    p_status.set_defaults(func=labsctl.cmd_status)

    # Shell tab-completion (optional): enabled when argcomplete is installed and
    # the shell hook is registered (see `headlabs completion`). No-op otherwise.
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass

    # Pre-route: `research build ...` → `research-build ...` (avoids argparse
    # subparser conflict with the positional intent argument).
    _argv = sys.argv[1:]
    if len(_argv) >= 2 and _argv[0] in ("research", "rsch") and _argv[1] == "build":
        _argv = ["research-build"] + _argv[2:]
    elif len(_argv) >= 2 and _argv[0] in ("research", "rsch") and _argv[1] in ("list", "ls"):
        _argv = ["loops", "list", "--mode", "research"] + _argv[2:]
    elif len(_argv) >= 2 and _argv[0] in ("research", "rsch") and _argv[1] == "status":
        _argv = ["loops", "status"] + _argv[2:]

    args = parser.parse_args(_argv)
    if not args.command:
        parser.print_help()
        sys.exit(1)

    import requests as _requests
    from headlabs import agentnames as _agentnames
    try:
        args.func(args)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        sys.exit(130)
    except _agentnames.AgentNotFound as exc:
        _print_agent_not_found(exc)
        sys.exit(2)
    except _requests.HTTPError as exc:
        _print_http_error(exc)
        sys.exit(1)
    except _requests.RequestException as exc:
        print(f"\033[31merro de rede: {str(exc)[:200]}\033[0m", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
