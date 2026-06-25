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


def cmd_agents(args):
    """List agents — local registry + remote platform."""
    from headlabs.client import HeadLabsClient

    if hasattr(args, 'subcmd') and args.subcmd == 'create':
        return cmd_agents_create(args)
    if hasattr(args, 'subcmd') and args.subcmd == 'deploy':
        return cmd_agents_deploy(args)

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
    """Create a declarative agent.

    With no --prompt/--prompt-file/--id: launch the AGENTIC creation wizard, where
    an assistant conducts the whole creation (asks purpose, conversational vs
    invocation vs both, description, proposes the design, and creates it).
    With flags: one-shot programmatic creation (for scripts/power users).
    """
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
    """Agentic agent-creation wizard: an assistant conducts the whole creation
    interview in the terminal — it greets, asks the purpose, whether the agent is
    conversational / invocation / both, the description, proposes a design, and
    creates the agent on your approval. Powered by a backend architect agent."""
    import uuid
    from headlabs.client import HeadLabsClient
    from headlabs.progress import ProgressReporter
    from headlabs.config import get_tenant

    client = HeadLabsClient()
    session_id = str(uuid.uuid4())
    tenant_id = getattr(args, "tenant", None) or get_tenant()
    history: list[dict] = []

    print("HeadLabs · criação de agente assistida")
    print("   O assistente vai conduzir a criação. Digite /exit para sair.\n")

    # Kickoff: the assistant speaks first and runs the interview.
    pending = (
        "Inicie agora a criação de um novo agente comigo. Conduza a entrevista, "
        "uma pergunta por vez: apresente-se em uma linha e pergunte primeiro o "
        "propósito do agente; em seguida pergunte se ele será CONVERSACIONAL, de "
        "INVOCAÇÃO (estruturado) ou AMBOS, e peça uma descrição curta. Quando tiver "
        "o necessário, proponha o design (id, nome, prompt, tools, schema se aplicável) "
        "e crie ao receber meu aceite."
    )
    show_pending = False  # don't echo the internal kickoff as a user line

    try:
        while True:
            reporter = ProgressReporter(quiet=False, verbose=getattr(args, "verbose", False))
            reporter.begin_wait("Pensando…")
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
            print(f"\n{answer}\n")
            history.append({"role": "user", "content": pending})
            history.append({"role": "assistant", "content": answer})
            history = history[-24:]

            try:
                user_input = input("Você: ").strip()
            except EOFError:
                break
            if user_input in ("/exit", "/quit"):
                break
            if not user_input:
                continue
            pending = user_input
            show_pending = True
    except KeyboardInterrupt:
        pass
    print("\nEncerrado.")


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
    platform_path = (
        cfg.get("platform_path")
        or os.environ.get("HEADLABS_PLATFORM_PATH")
        or _find_platform_repo()
    )
    if not platform_path or not os.path.isfile(os.path.join(platform_path, "Dockerfile.agent")):
        print("\033[31merro: repo da plataforma não encontrado.\033[0m")
        print("\033[2m  Configure: headlabs config --platform-path /caminho/para/headlabs-platform\033[0m")
        print("\033[2m  Ou defina HEADLABS_PLATFORM_PATH no env.\033[0m")
        sys.exit(2)

    # ── 2. Determine Dockerfile + image tag ───────────────────────────────────
    # "loops-latest" tag → Dockerfile.loops (all loop agents); otherwise Dockerfile.agent
    ACCOUNT = "688128002471"
    REGION = "us-east-1"
    ECR = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/headlabs-agents"
    image_uri = f"{ECR}:{tag}"

    if tag == "loops-latest" or agent_id.startswith("loop-"):
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
    r = subprocess.run(cmd, cwd=platform_path, capture_output=True, text=True)
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
    p_run.add_argument("agent", help="Agent name (e.g. finops)")
    p_run.add_argument("--profile", required=True, help="AWS profile name")
    p_run.add_argument("--account-id", help="Target AWS account ID (defaults to profile's account)")
    p_run.add_argument("--days", type=int, default=30, help="Days of data to analyze")
    p_run.add_argument("--question", help="Ask a specific question")
    p_run.add_argument("--quiet", action="store_true", help="Suppress progress output")
    p_run.add_argument("--verbose", action="store_true", help="Show every progress event")
    p_run.add_argument("--output", choices=["json", "html", "md"], default="html", help="Output format")
    p_run.add_argument("--no-browser", action="store_true", help="Don't open browser")
    p_run.set_defaults(func=cmd_run)

    # agents (alias: agent)
    p_agents = sub.add_parser("agents", aliases=["agent"], help="List or create agents")
    p_agents_sub = p_agents.add_subparsers(dest="subcmd")
    p_agents.set_defaults(func=cmd_agents)
    # agents create — no flags launches the agentic creation wizard
    p_ac = p_agents_sub.add_parser("create", help="Create an agent (no flags = guided agentic wizard)")
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

    # agents deploy
    p_ad = p_agents_sub.add_parser("deploy", help="Deploy an agent (build+push+update runtime)")
    p_ad.add_argument("agent_id", help="Agent ID to deploy")
    p_ad.add_argument("--tag", help="ECR image tag (default: agent_id; loop agents auto-use loops-latest)")
    p_ad.add_argument("--profile", help="AWS profile for ECR auth (default: env AWS_PROFILE)")
    p_ad.add_argument("--force", action="store_true", help="Skip extended health check")
    p_ad.add_argument("--wait", action="store_true", help="Block until deployment completes")
    p_ad.set_defaults(func=cmd_agents, subcmd="deploy")

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
