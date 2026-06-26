"""HeadLabs CLI entry point."""

import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from headlabs.config import CONFIG_DIR, REPORTS_DIR, load_config, save_config
from headlabs.agents.registry import AGENT_REGISTRY
from headlabs import labsctl


def cmd_run(args):
    """Run an agent."""
    from headlabs.client import HeadLabsClient
    from headlabs.progress import ProgressReporter

    agent_name = args.agent
    # Accept either a friendly registry name (e.g. "finops") or a platform
    # agent id (e.g. "finops-advisor"), mirroring `chat`. Unknown names are
    # passed through to the platform, which validates them.
    agent_cfg = AGENT_REGISTRY.get(agent_name)
    agent_id = agent_cfg["agent_id"] if agent_cfg else agent_name

    client = HeadLabsClient()

    kwargs = {"days": args.days}
    if args.question:
        kwargs["question"] = args.question
    if args.account_id:
        kwargs["account_id"] = args.account_id

    reporter = ProgressReporter(
        quiet=getattr(args, "quiet", False),
        verbose=getattr(args, "verbose", False),
    )
    reporter.header(f"{agent_id}  ·  {args.profile}  ·  {args.days}d")

    if getattr(args, "local", False):
        result = _run_local(agent_id, args, reporter, kwargs)
    else:
        result = client.run(agent_id, args.profile, reporter=reporter,
                            approval_handler=reporter.prompt_approval, **kwargs)

    if result.status == "timeout":
        print("Error: agent timed out. Try again or check headlabs.ai dashboard.")
        sys.exit(1)
    if result.status == "failed":
        print(f"Error: agent failed: {result.summary[:150] if result.summary else 'unknown'}")
        sys.exit(1)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    # Always save both HTML + JSON
    html_path = str(REPORTS_DIR / f"{agent_name}_{ts}.html")
    json_path = str(REPORTS_DIR / f"{agent_name}_{ts}.json")
    result.to_html(html_path)
    result.to_json(json_path)

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
    reporter.finish("succeeded" if output else "failed")

    if error and not output:
        return Result(status="failed", summary=error)

    output = output or {}
    return Result(
        status="succeeded",
        raw_output=output,
        insights=output.get("insights") or output.get("findings", []),
        summary=output.get("summary", ""),
        total_saving_usd=output.get("total_saving_usd", 0.0),
        account_id=account_id,
        cost_summary=output.get("cost_summary", {}),
    )


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

    client = HeadLabsClient()
    remote = client.list_remote_agents()
    # The architect is an internal engine behind `agents create`, not a user-facing agent.
    remote = [a for a in (remote or []) if a.get("id") != _ARCHITECT_AGENT_ID]

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


def cmd_agents_create(args):
    """Create an agent.

    Inline (one-shot):  headlabs agents create "quero um agente que..."
    Interactive:        headlabs agents create  (prompts for input)
    Programmatic:      headlabs agents create --id x --prompt "..."
    """
    # Agentic creation (NLP → research → create)
    intent = getattr(args, "intent", None)
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


def cmd_agent_create_interactive(args):
    """One-shot agent creation: send the dev's intent, the architect researches
    and creates in a single invocation. If the architect needs clarification
    (critical gap after research), it asks once and the dev responds."""
    import uuid
    from headlabs.client import HeadLabsClient
    from headlabs.progress import ProgressReporter
    from headlabs.config import get_tenant

    client = HeadLabsClient()
    session_id = str(uuid.uuid4())
    tenant_id = getattr(args, "tenant", None) or get_tenant()

    print("HeadLabs · criação de agente")

    # Inline mode: intent already provided
    inline = getattr(args, "_inline_intent", None)
    if inline:
        user_input = inline
        print(f"   → {user_input}\n")
    else:
        print("   Descreva o que você quer. O architect pesquisa e cria.\n")
        try:
            user_input = input("→ ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not user_input:
            return

    history = []
    # Prefix: force one-shot behavior regardless of prompt cache state
    pending = (
        "[INSTRUÇÃO DO SISTEMA: Crie o agente/MCP AGORA, em uma única rodada. "
        "NÃO pergunte modo (sempre AMBOS), NÃO pergunte descrição (infira do pedido), "
        "NÃO faça entrevista. Pesquise, projete e crie. Se precisar de algo crítico "
        "após pesquisar, pergunte UMA vez.]\n\n"
        f"PEDIDO DO DEV: {user_input}"
    )

    try:
        while True:
            reporter = ProgressReporter(quiet=False, verbose=getattr(args, "verbose", False))
            reporter.begin_wait("Criando…")
            answer, err = None, None
            try:
                for event in client.chat_stream(_ARCHITECT_AGENT_ID, session_id, pending,
                                                 context={}, history=history, tenant_id=tenant_id):
                    et = event.get("type", "")
                    if et == "progress":
                        reporter.event(event["event"])
                    elif et == "done":
                        answer = event.get("message", "")
                    elif et == "error":
                        err = event.get("error", "?")
                reporter.finish("failed" if err else "succeeded")
            except KeyboardInterrupt:
                reporter.finish("cancelled")
                break
            except Exception as exc:
                reporter.finish("failed")
                print(f"  x {exc}")
                break

            if err:
                print(f"  x {err}")
                break
            if not answer or not answer.strip():
                print("\033[33m  (timeout — tentando novamente)\033[0m")
                continue

            print(f"\n{answer}\n")
            history.append({"role": "user", "content": pending})
            history.append({"role": "assistant", "content": answer})

            # Auto-exit when creation is done
            _done_signals = ("push_agent_source", "create_agent", "push_mcp_source",
                             "agente criado", "agent criado", "mcp criado",
                             "headlabs agents deploy", "headlabs mcps deploy",
                             "headlabs run", "headlabs chat",
                             "runtime", "ativado")
            if any(s in (answer or "").lower() for s in _done_signals):
                print("\033[32m✓ Pronto.\033[0m")
                break

            # Architect is asking for clarification — let dev respond
            if "?" in answer:
                try:
                    user_input = input("→ ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not user_input or user_input in ("/exit", "/quit"):
                    break
                pending = user_input
            else:
                # No question, no creation signal — done
                break
    except KeyboardInterrupt:
        pass


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


def cmd_agents_test(args):
    """Adversarial autocritical test: the critic agent evaluates another agent.

    1. Reads target agent's contract (prompt, tools, skills, schema)
    2. Invokes the target agent (real execution)
    3. Sends contract + output to the critic for adversarial evaluation
    4. Renders: score, dimensions, gaps, recommendations
    5. If --fix: auto-applies recommendations via update

    With --tools: runs the agent and reports tool call success/failure (no critic).
    """
    import json as _json
    import uuid
    from headlabs.client import HeadLabsClient
    from headlabs.progress import ProgressReporter
    from headlabs.config import get_tenant

    client = HeadLabsClient()
    agent_id = args.agent_id
    profile = getattr(args, "profile", None)

    # --tools mode: invoke agent and report tool call results
    if getattr(args, "tools", False):
        return _agents_test_tools(client, agent_id, profile, args)

    # 1. Read target agent contract
    try:
        agent = client.request("GET", f"/agents/{agent_id}")
    except Exception as exc:
        print(f"\033[31merro: agente '{agent_id}' não encontrado\033[0m")
        sys.exit(2)

    manifest = agent.get("manifest", {})
    contract = (
        f"AGENT ID: {agent_id}\n"
        f"DESCRIPTION: {agent.get('description','')}\n"
        f"TYPE: {agent.get('agent_type','')}\n"
        f"TOOLS: {manifest.get('tools_native',[])}\n"
        f"MCPS: {manifest.get('mcp',[])}\n"
        f"SKILLS: {manifest.get('skills',[])}\n"
        f"PROMPT:\n{agent.get('prompt','')[:4000]}\n"
    )

    # 2. Invoke the target agent (real execution)
    print(f"\033[2m  Executando {agent_id}…\033[0m")
    scenario = getattr(args, "scenario", None)
    try:
        if profile:
            # Use the run path (with AWS creds)
            result = client.run(agent_id, profile, days=30,
                                question=scenario or None)
            output_text = _json.dumps(result.raw_output, ensure_ascii=False, default=str)[:6000]
        else:
            # Invoke without AWS (for agents that don't need it)
            exec_id, tenant_id, stream_id = client.invoke(agent_id, {
                "intent": scenario or "análise completa",
                "tenant_id": "ALL",
            })
            result = client.poll(exec_id, tenant_id=tenant_id, stream_id=stream_id)
            output_text = _json.dumps(result.raw_output, ensure_ascii=False, default=str)[:6000]
    except Exception as exc:
        output_text = f"ERRO NA EXECUÇÃO: {str(exc)[:500]}"

    print(f"\033[2m  Output capturado ({len(output_text)} chars)\033[0m")

    # 3. Send to critic
    critic_input = (
        f"[INSTRUÇÃO: Avalie o output deste agente de forma adversarial. "
        f"Score 0-100 em 8 dimensões. Seja implacável.]\n\n"
        f"═══ CONTRATO DO AGENTE ═══\n{contract}\n\n"
        f"═══ OUTPUT REAL DA EXECUÇÃO ═══\n{output_text}\n\n"
        f"Avalie. Retorne JSON com schema: "
        f"{{score, verdict, dimensions[{{name,score,evidence,gap}}], "
        f"top_gaps[], recommendations[], adversarial_scenarios[]}}"
    )

    session_id = str(uuid.uuid4())
    tenant_id = get_tenant()
    reporter = ProgressReporter(quiet=False, verbose=False)
    print(f"\n\033[1m  Avaliação adversarial: {agent_id}\033[0m")
    reporter.begin_wait("Critic analisando…")

    answer = ""
    try:
        for event in client.chat_stream("agent-critic", session_id, critic_input,
                                         context={}, history=[], tenant_id=tenant_id):
            et = event.get("type", "")
            if et == "progress":
                reporter.event(event["event"])
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

    # 4. Render results
    # Try to parse JSON from the answer
    evaluation = None
    try:
        # Find JSON block in the answer
        import re
        json_match = re.search(r'\{[\s\S]*"score"[\s\S]*\}', answer)
        if json_match:
            evaluation = _json.loads(json_match.group())
    except Exception:
        pass

    if evaluation:
        score = evaluation.get("score", 0)
        verdict = evaluation.get("verdict", "?")
        color = "\033[32m" if verdict == "PASS" else ("\033[33m" if verdict == "NEEDS_WORK" else "\033[31m")

        print(f"\n  {color}{'━' * 50}\033[0m")
        print(f"  {color}  SCORE: {score}/100  ·  VERDICT: {verdict}\033[0m")
        print(f"  {color}{'━' * 50}\033[0m\n")

        dims = evaluation.get("dimensions", [])
        if dims:
            print("  \033[1mDimensões:\033[0m")
            for d in dims:
                s = d.get("score", 0)
                dc = "\033[32m" if s >= 80 else ("\033[33m" if s >= 60 else "\033[31m")
                print(f"    {dc}{s:>3}\033[0m  {d.get('name','')}")
                if d.get("gap"):
                    print(f"         \033[2m↳ {d['gap'][:120]}\033[0m")
            print()

        gaps = evaluation.get("top_gaps", [])
        if gaps:
            print("  \033[1mTop gaps:\033[0m")
            for g in gaps[:5]:
                print(f"    \033[31m✗\033[0m {g[:150]}")
            print()

        recs = evaluation.get("recommendations", [])
        if recs:
            print("  \033[1mRecomendações:\033[0m")
            for r in recs[:5]:
                print(f"    \033[36m→\033[0m {r[:150]}")
            print()
    else:
        # Fallback: print raw answer
        print(f"\n{answer}\n")

    # 5. Auto-fix if requested
    if getattr(args, "fix", False) and evaluation and evaluation.get("recommendations"):
        recs = evaluation["recommendations"]
        instruction = "; ".join(recs[:3])
        print(f"\033[2m  Aplicando fix: {instruction[:100]}…\033[0m")
        from types import SimpleNamespace
        fix_args = SimpleNamespace(id=agent_id, instruction=instruction,
                                   profile=profile, tenant=None)
        cmd_agents_update(fix_args)


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
    if sub == "push":
        return _mcps_push(args)
    if sub == "pull":
        return _mcps_pull(args)
    if sub == "dev":
        return _mcps_dev(args)
    if sub == "connect":
        return _mcps_connect(args)
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
                cfg = c._config()
                api_key = cfg.get("api_key", "")
                if api_key:
                    import base64
                    cred = base64.b64encode(f"{api_key}:".encode()).decode()
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
                            if isinstance(pval, dict) and not pval.get("description") and not pval.get("type"):
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

    client = HeadLabsClient()
    cfg = load_config()

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
    ACCOUNT = "688128002471"
    REGION = "us-east-1"
    ECR = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/headlabs-mcps"
    image_uri = f"{ECR}:{mcp_id}"

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
                              json={"image_tag": mcp_id}, timeout=30)
        print(f"\033[32m✓ MCP deployado: {mcp_id}\033[0m" +
              (f" (runtime: {resp.get('runtime_id','')})" if resp.get("runtime_id") else ""))
    except Exception as exc:
        # If deploy endpoint doesn't exist yet, inform
        print(f"\033[33m  Deploy endpoint pendente: {str(exc)[:80]}\033[0m")
        print(f"\033[2m  A imagem está no ECR. O EventBridge pode triggar o deploy automaticamente.\033[0m")


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


def cmd_chat(args):
    """Interactive chat with an agent. Same credential model as `run`:
    the AWS profile is used to collect data LOCALLY (credentials stay local),
    and the collected data is sent to the agent so it reasons over the
    CLIENT's account — not the HeadLabs platform account."""
    import uuid
    from headlabs.client import HeadLabsClient
    from headlabs.progress import ProgressReporter

    client = HeadLabsClient()
    session_id = str(uuid.uuid4())

    # Resolve friendly name → platform agent id (finops → finops-chat)
    agent_cfg = AGENT_REGISTRY.get(args.agent)
    agent_id = agent_cfg["chat_agent_id"] if agent_cfg and "chat_agent_id" in agent_cfg else args.agent

    # Resolve account + region from the AWS profile and derive short-lived
    # credentials so the ephemeral agent reads the CLIENT's account (Option B).
    # Long-lived keys never leave the machine; without creds the agent fails
    # closed server-side.
    agent_input = {"tenant_id": "ALL", "aws_region": "us-east-1"}
    try:
        import boto3
        from headlabs.client import _ephemeral_credentials
        session = boto3.Session(profile_name=args.profile)
        agent_input["aws_region"] = session.region_name or "us-east-1"
        identity = session.client("sts").get_caller_identity()
        agent_input["account_id"] = identity["Account"]
        print(f"Account: {identity['Account']} (profile: {args.profile})")

        creds = _ephemeral_credentials(session)
        if creds:
            agent_input.update(creds)
        else:
            print("! Sem credenciais AWS resolvidas — a execução pode ser bloqueada pelo agente.")
    except Exception as exc:
        print(f"! Could not resolve AWS profile '{args.profile}': {exc}")

    context = {"input": agent_input}
    history = []  # client-side conversation history (user + assistant turns)

    # Tenant used to poll the chat execution. The /chat endpoint may not echo
    # the tenant, so resolve it from --tenant or config for non-platform keys.
    from headlabs.config import get_tenant
    tenant_id = getattr(args, "tenant", None) or get_tenant()

    print(f"Chat with '{agent_id}' (session: {session_id[:8]}...)")
    print("   Type /exit or Ctrl+C to quit.\n")

    try:
        while True:
            try:
                user_input = input("You: ").strip()
            except EOFError:
                break
            if not user_input:
                continue
            if user_input in ("/exit", "/quit"):
                break

            # Reuse the same live renderer as `run`: dimmed tool lines with
            # elapsed time, detail sub-items, spinner — TTY-aware.
            reporter = ProgressReporter(
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
                reporter.finish("failed" if err else "succeeded")
            except KeyboardInterrupt:
                reporter.finish("cancelled")
                print()
                continue
            except Exception as exc:
                reporter.finish("failed")
                print(f"  x {exc}")
                continue

            if err:
                print(f"  x Error: {err}")
            elif answer:
                print(f"\nAgent: {answer}\n")
                history.append({"role": "user", "content": user_input})
                history.append({"role": "assistant", "content": answer})
                history = history[-20:]  # cap context window
    except KeyboardInterrupt:
        pass
    print("\nChat ended.")


def cmd_config(args):
    """Save configuration."""
    config = load_config()
    if args.key:
        config["api_key"] = args.key
    save_config(config)
    print("Configuration saved to ~/.headlabs/config.json")


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
    p_run.add_argument("agent", help="Agent name (e.g. finops)")
    p_run.add_argument("--profile", required=True, help="AWS profile name")
    p_run.add_argument("--account-id", help="Target AWS account ID (defaults to profile's account)")
    p_run.add_argument("--days", type=int, default=30, help="Days of data to analyze")
    p_run.add_argument("--question", help="Ask a specific question")
    p_run.add_argument("--quiet", action="store_true", help="Suppress progress output")
    p_run.add_argument("--verbose", action="store_true", help="Show every progress event")
    p_run.add_argument("--local", action="store_true", help="Run the agent locally via Docker (from ./agents/<id>/)")
    p_run.add_argument("--output", choices=["json", "html", "md"], default="html", help="Output format")
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
    p_at.add_argument("agent_id", help="Agent ID to test")
    p_at.add_argument("--profile", help="AWS profile (for invoking the agent)")
    p_at.add_argument("--fix", action="store_true", help="Auto-apply recommendations via update")
    p_at.add_argument("--scenario", help="Custom test scenario (otherwise critic generates them)")
    p_at.add_argument("--tools", action="store_true", help="Focus on tool/MCP connectivity: invoke and report which tools succeed/fail")
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

    pm_push = p_mcps_sub.add_parser("push", help="Push local MCP to platform (version + deploy)")
    pm_push.add_argument("mcp_id", help="MCP ID (must exist in ./mcps/<id>/)")
    pm_push.add_argument("--profile", help="AWS profile for ECR auth")
    pm_push.add_argument("-m", "--message", help="Version commit message")
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
    p_chat.add_argument("agent", help="Agent ID")
    p_chat.add_argument("--profile", default="default", help="AWS profile name")
    p_chat.add_argument("--tenant", help="Tenant ID for polling (defaults to config 'tenant', then 'platform')")
    p_chat.set_defaults(func=cmd_chat)

    # config
    p_config = sub.add_parser("config", help="Configure HeadLabs CLI")
    p_config.add_argument("--key", help="API key")
    p_config.set_defaults(func=cmd_config)

    # report
    p_report = sub.add_parser("report", help="Open reports")
    p_report.add_argument("--last", action="store_true", help="Open last report")
    p_report.set_defaults(func=cmd_report)

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

    # ── loops (build jobs) ────────────────────────────────────────────────────
    p_loops = sub.add_parser("loops", aliases=["loop"], help="Build loops (jobs) inside labs")
    loops_sub = p_loops.add_subparsers(dest="loops_cmd")
    p_loops.set_defaults(func=labsctl.cmd_loops)

    oc = loops_sub.add_parser("create", help="Start a build in an existing lab")
    oc.add_argument("--lab", required=True, help="Lab id or name")
    oc.add_argument("-i", "--intent", required=True, help="Build objective")
    oc.add_argument("--auto-approve", dest="auto_approve", action="store_true")
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

    og = loops_sub.add_parser("logs", help="Show the build's agent trace")
    og.add_argument("job_id")
    og.add_argument("--phase", help="Filter by phase/agent")
    _add_common(og)
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
    p_research.set_defaults(func=labsctl.cmd_research)

    # ── status (top-level shortcut) ───────────────────────────────────────────
    p_status = sub.add_parser("status", help="Active builds (no arg) or a build's detail")
    p_status.add_argument("job_id", nargs="?", help="Build id (optional)")
    _add_common(p_status, tenant=True)
    p_status.set_defaults(func=labsctl.cmd_status)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
