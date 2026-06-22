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
    result = client.run(agent_id, args.profile, reporter=reporter, **kwargs)

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
                                                 tenant_id=tenant_id):
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

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
