"""
Labs & Loops control — professional CLI surface (kubectl/aws-cli inspired).

Resources:
  lab   — a project workspace that groups loops + accumulates a repository.
  loop  — a build job: the pipeline orchestrator→researcher→architect→planner→
          executor→validator→deliverer, gated by human approvals.

Conventions:
  - resource-verb commands; async-first with --wait / -w (watch) to follow.
  - -o table|wide|json (table default; plain when piped).
  - semantic exit codes for CI: 0 ok, 1 build failed, 2 usage, 4 gate rejected.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from headlabs.client import HeadLabsClient

# ── exit codes ────────────────────────────────────────────────────────────────
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REJECTED, EXIT_TIMEOUT = 0, 1, 2, 4, 8

# ── status taxonomy ─────────────────────────────────────────────────────────--
_TERMINAL_OK = {"complete", "completed", "succeeded", "done"}
_TERMINAL_FAIL = {"failed", "error", "dlq", "timed_out"}
_TERMINAL_CANCEL = {"cancelled", "canceled"}
# "partial" is a genuine terminal status the backend assigns when the
# deliverer's ground-truth guards (entrypoint missing, frontend has no real
# content, frontend calls a forbidden non-public endpoint, etc.) reject an
# otherwise-finished build — see api/routers/loops.py's deliverer callback.
# Root cause this closes: `headlabs loops watch` never recognized "partial" as
# terminal, so it polled forever (14h observed live on loop_9122b3da23b0,
# which actually finished in ~1h30) until the user gave up and hit Ctrl+C —
# which _follow's KeyboardInterrupt handler then mislabeled as "Cancelado",
# even though the loop had already reached a real terminal state on the
# backend long before. Treated as a (non-crash) failure outcome: the build
# produced SOMETHING but didn't fully pass its own guards.
_TERMINAL_PARTIAL = {"partial"}
_TERMINAL = _TERMINAL_OK | _TERMINAL_FAIL | _TERMINAL_CANCEL | _TERMINAL_PARTIAL

_LOOP_PHASES = ["orchestrator", "researcher", "architect", "planner",
                "executor", "validator", "deliverer"]

# Research mode (mode="research"): amplified investigative research only — no
# architect/planner/executor build. Broad web search + deep analysis, ending in
# a synthesized findings report that becomes context for later builds.
_RESEARCH_PHASES = ["orchestrator", "researcher", "analyst", "synthesizer",
                    "deliverer"]

# Research depth → server effort budget (breadth of sources, recursion depth).
_RESEARCH_DEPTHS = ("quick", "standard", "deep", "exhaustive")

# CLI gate name → server gate flag
_GATE_MAP = {"architecture": "after_architect", "plan": "after_planner",
             "destructive": "before_destructive"}

# ── ANSI / TTY ────────────────────────────────────────────────────────────────
_C = {"reset": "\033[0m", "dim": "\033[2m", "red": "\033[31m", "green": "\033[32m",
      "yellow": "\033[33m", "cyan": "\033[36m", "bold": "\033[1m"}
_STATUS_COLOR = {
    "running": "cyan", "executing": "cyan", "thinking": "cyan", "in_progress": "cyan",
    "active": "green", "succeeded": "green", "complete": "green", "completed": "green", "done": "green",
    "failed": "red", "error": "red", "dlq": "red", "timed_out": "red",
    "cancelled": "yellow", "paused": "yellow", "awaiting_approval": "yellow",
}


def _tty() -> bool:
    return sys.stdout.isatty()


def _c(text: str, color: str) -> str:
    if not _tty() or color not in _C:
        return str(text)
    return f"{_C[color]}{text}{_C['reset']}"


def _color_status(s: str) -> str:
    return _c(s, _STATUS_COLOR.get((s or "").lower(), "reset"))


def display_status(item: dict) -> str:
    """Human display status for a loop: awaiting_approval when a gate pends."""
    if item.get("pending_gate"):
        return "awaiting_approval"
    return item.get("status", "?")


def _age(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return "-"
    if secs < 0:
        secs = 0
    for unit, div in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= div:
            return f"{int(secs // div)}{unit}"
    return f"{int(secs)}s"


def _trunc(s, n: int) -> str:
    s = "" if s is None else str(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


# ── output rendering ──────────────────────────────────────────────────────────

def render(items, columns, output: str, *, empty: str = "Nothing found.") -> None:
    """Render a list of dicts. `columns` = list of (TITLE, fn(item)->str, color?)."""
    if output == "json":
        print(json.dumps(items, indent=2, default=str, ensure_ascii=False))
        return
    if not items:
        if _tty():
            print(_c(empty, "dim"))
        return
    cols = columns
    rows = []
    for it in items:
        rows.append([(fn(it) or "") for (_t, fn, *_) in cols])
    widths = [len(t) for (t, *_rest) in cols]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(_strip_ansi(cell)))
    # header
    header = "  ".join(_c(t.ljust(widths[i]), "bold") for i, (t, *_r) in enumerate(cols))
    print(header)
    for ridx, row in enumerate(rows):
        out = []
        for i, (_t, _fn, *meta) in enumerate(cols):
            cell = row[i]
            pad = widths[i] - len(_strip_ansi(cell))
            if meta and meta[0] == "status":
                cell = _color_status(cell)
            out.append(cell + (" " * pad))
        print("  ".join(out).rstrip())


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _slug(text: str, words: int = 5) -> str:
    s = re.sub(r"[^a-z0-9\s-]", "", (text or "").lower())
    parts = s.split()[:words]
    return "-".join(parts) or "lab"


# ── lab resolution ──────────────────────────────────────────────────────────--

def _resolve_lab(client: HeadLabsClient, ref: str) -> dict:
    """Resolve a lab by id or (case-insensitive) name. Exits 2 if not found."""
    labs = client.request("GET", "/labs-v2") or []
    for lab in labs:
        if lab.get("lab_id") == ref:
            return lab
    matches = [l for l in labs if (l.get("name") or "").lower() == ref.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _die(f"'{ref}' é ambíguo: {len(matches)} labs com esse nome — use o lab_id.", EXIT_USAGE)
    _die(f"Lab '{ref}' não encontrado.", EXIT_USAGE)


def _die(msg: str, code: int = EXIT_USAGE) -> None:
    print(_c(f"erro: {msg}", "red"), file=sys.stderr)
    sys.exit(code)


def _gates_from_args(args) -> Optional[dict]:
    """Build the loop gate + judge config from flags.
    None => server defaults (pause after architecture, plan, and destructive)."""
    auto = getattr(args, "auto_approve", False)
    gate = getattr(args, "gate", None)
    judges = getattr(args, "judges", None)
    judge_model = getattr(args, "judge_model", None)
    gate_mode = getattr(args, "gate_mode", None)
    max_revise = getattr(args, "max_revise", None)
    if not (auto or gate or judges or judge_model or gate_mode or max_revise is not None):
        return None  # server DEFAULT_GATES
    if auto:
        g = {"after_architect": False, "after_planner": False, "before_destructive": False,
             "auto_approve": [], "require_approval": []}
    elif gate:
        chosen = {_GATE_MAP[x.strip()] for x in gate.split(",") if x.strip() in _GATE_MAP}
        g = {flag: (flag in chosen) for flag in _GATE_MAP.values()}
    else:
        g = {"after_architect": True, "after_planner": True, "before_destructive": True}
    # Judge scope: --gate-mode human disables the panel; judge/judge+human default to full.
    if gate_mode == "human":
        g["judges"] = "off"
    elif judges:
        g["judges"] = judges
    elif gate_mode in ("judge", "judge+human"):
        g["judges"] = "full"
    if gate_mode in ("judge", "judge+human"):
        g["gate_mode"] = gate_mode      # judge=autonomous, judge+human=panel informs, human decides
    if judge_model:
        g["judge_model"] = judge_model  # fast | standard
    if max_revise is not None:
        g["max_revise"] = int(max_revise)
    return g


# ════════════════════════════════════════════════════════════════════════════
# labs
# ════════════════════════════════════════════════════════════════════════════

def cmd_labs(args):
    sub = getattr(args, "labs_cmd", None)
    return {
        "create": _labs_create, "list": _labs_list, "ls": _labs_list,
        "get": _labs_get, "describe": _labs_describe, "repo": _labs_repo,
        "push": _labs_push, "archive": _labs_archive, "outputs": _labs_outputs,
        "rebuild": _labs_rebuild, "backlog": _labs_backlog, "fix": _labs_fix, "inspect": _labs_inspect,
    }.get(sub, _labs_list)(args)


def _labs_list(args):
    client = HeadLabsClient()
    labs = client.request("GET", "/labs-v2") or []
    labs.sort(key=lambda l: l.get("updated_at") or l.get("created_at") or "", reverse=True)
    if getattr(args, "quiet", False):
        for l in labs:
            print(l.get("lab_id"))
        return
    cols = [
        ("LAB_ID", lambda l: l.get("lab_id", "")),
        ("NAME", lambda l: _trunc(l.get("name"), 28)),
        ("STACK", lambda l: _trunc(",".join(l.get("stack") or []), 28)),
        ("LOOPS", lambda l: str(l.get("loop_count", 0))),
        ("STATUS", lambda l: l.get("status", ""), "status"),
        ("AGE", lambda l: _age(l.get("created_at"))),
    ]
    if getattr(args, "output", "table") == "wide":
        cols.insert(5, ("LAST_RUN", lambda l: _age(l.get("last_loop_at"))))
    render(labs, cols, getattr(args, "output", "table"), empty="Nenhum lab. Crie com: headlabs labs create -i \"...\"")


def _labs_get(args):
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    out = getattr(args, "output", "table")
    if out == "json":
        print(json.dumps(lab, indent=2, default=str, ensure_ascii=False))
        return
    print(f"{_c('Lab', 'bold')}:     {lab.get('name')}  ({lab.get('lab_id')})")
    print(f"Status:  {_color_status(lab.get('status',''))}    Loops: {lab.get('loop_count',0)}")
    print(f"Stack:   {', '.join(lab.get('stack') or []) or '-'}")
    print(f"Desc:    {_trunc(lab.get('description'), 100)}")
    print(f"Criado:  {_age(lab.get('created_at'))} atrás")


def _labs_describe(args):
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    _labs_get(args)
    try:
        lineage = client.request("GET", f"/labs-v2/{lab['lab_id']}/lineage") or []
        print(f"\n{_c('Loops (lineage):', 'bold')}")
        for lp in lineage[-10:]:
            print(f"  {_color_status(lp.get('status','')):<22} {_trunc(lp.get('intent'),60)}  ({_age(lp.get('started_at'))})")
        repo = client.request("GET", f"/labs-v2/{lab['lab_id']}/repository") or []
        print(f"\n{_c('Repositório:', 'bold')} {len(repo)} arquivo(s)  ·  headlabs labs repo {lab['lab_id']} --tree")
    except Exception as exc:  # noqa: BLE001
        print(_c(f"(detalhes indisponíveis: {exc})", "dim"))


def _resource_endpoint(client: HeadLabsClient, base: str, kind: str, name: str) -> dict:
    """Map a created resource (type:name) to its ready-to-use access endpoint/URL."""
    try:
        if kind == "function":
            return {"endpoint": f"POST {base}/functions/{name}/invoke"}
        if kind == "table":
            return {"endpoint": f"{base}/tables/{name}/items",
                    "example": "GET=listar · PUT=inserir · DELETE /items/{pk}"}
        if kind == "agent":
            return {"endpoint": f"POST {base}/agents/{name}/invoke"}
        if kind in ("kb", "knowledge_base", "knowledge-base"):
            return {"endpoint": f"POST {base}/knowledge-bases/{name}/query"}
        if kind == "mcp":
            return {"endpoint": f"https://mcps.headlabs.ai/{name}/mcp"}
        if kind == "storage":
            meta = client.request("GET", f"/storage/{name}") or {}
            sid = meta.get("storage_id") or name
            return {"endpoint": meta.get("url") or meta.get("site_url") or f"https://{sid}.apps.headlabs.ai/"}
        if kind == "container":
            st = client.request("GET", f"/containers/{name}/status") or {}
            ep = st.get("endpoint") or st.get("url")
            if ep:
                return {"endpoint": ep if str(ep).startswith("http") else f"http://{ep}"}
            return {"endpoint": "(provisionando endpoint…)"}
    except Exception:  # noqa: BLE001 — best-effort lookup
        return {"endpoint": "(indisponível)"}
    return {"endpoint": ""}


def _labs_outputs(args):
    """Outputs: published sites (browser URLs), API endpoints, and files —
    grouped. Sources resources from the MOST RECENT terminal build only (not
    the entire lineage) — a `labs rebuild` destroys everything and starts
    fresh, so resources_created from earlier loops (superseded/cancelled)
    describe resources that no longer physically exist. Root cause this
    closes: aggregating the whole lineage produced ghost duplicates (e.g.
    'discovery-orchestrator' from one rebuild attempt AND
    'discovery_orchestrator' from a later one, neither ever coexisting in
    the same real build) and endpoints marked '(indisponível)' for resources
    that were correctly destroyed by a prior rebuild's teardown — not a real
    availability problem, just stale bookkeeping from a loop that no longer
    represents the lab's actual current state."""
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    lid = lab["lab_id"]
    base = client.api_url.rstrip("/")
    loops = client.request("GET", f"/labs-v2/{lid}/lineage") or []
    terminal = [l for l in loops if str(l.get("status", "")).lower() in ("complete", "partial")]
    terminal.sort(key=lambda x: x.get("started_at", ""))
    current = [terminal[-1]] if terminal else []
    if not current and loops:
        # No successful build yet — fall back to the most recent loop overall
        # so users mid-build still see partial progress instead of nothing.
        loops_sorted = sorted(loops, key=lambda x: x.get("started_at", ""))
        current = [loops_sorted[-1]]
    seen: set = set()
    sites, apis = [], []
    file_count = 0
    for lp in current:
        loop = client.request("GET", f"/loops/{lp.get('loop_id')}") or {}
        for r in (loop.get("resources_created") or []):
            if ":" not in r or r in seen:
                continue
            seen.add(r)
            kind, nm = r.split(":", 1)
            if kind == "file" or "/" in nm:      # files / sub-paths aren't standalone resources
                file_count += 1
                continue
            entry = {"type": kind, "resource": nm, **_resource_endpoint(client, base, kind, nm)}
            if kind == "storage":
                # Only treat as a published site if it likely has a frontend (index.html)
                # Data buckets (raw-*, *-artifacts, *-exports, *-snapshots) are APIs, not sites
                data_patterns = ("raw-", "artifact", "export", "snapshot", "backup", "cache", "log")
                if any(p in nm.lower() for p in data_patterns):
                    entry["type"] = "data"
                    apis.append(entry)
                else:
                    sites.append(entry)
            else:
                apis.append(entry)

    if getattr(args, "output", "table") == "json":
        print(json.dumps({"lab_id": lid, "name": lab.get("name", ""),
                          "sites": sites, "apis": apis, "files": file_count},
                         indent=2, ensure_ascii=False))
        return
    if not sites and not apis and not file_count:
        print(_c("Sem recursos ainda (nenhum loop concluiu criação).", "dim"))
        return
    print(f"\n{_c('Outputs', 'bold')}  ·  {lab.get('name','')}  ({lid})")
    if sites:
        print(f"\n{_c('🌐 Sites publicados', 'bold')}")
        for o in sorted(sites, key=lambda x: x["resource"]):
            print(f"  {o['resource']:<24} {o.get('endpoint','')}")
    if apis:
        print(f"\n{_c('API & recursos', 'bold')}")
        for o in sorted(apis, key=lambda x: (x["type"], x["resource"])):
            tlabel = f"{o['type']:<9}"
            print(f"  {_c(tlabel, 'cyan')} {o['resource']:<24} {o.get('endpoint','')}")
            if o.get("example"):
                print(f"  {'':<9} {'':<24} {_c(o['example'], 'dim')}")
    if file_count:
        print(f"\n{_c('Arquivos', 'bold')}: {file_count}  ·  navegue com: headlabs labs repo {lid} --tree")
    print(_c(f"\n  json: headlabs labs outputs {lid} -o json", "dim"))


def _labs_backlog(args):
    """Show the lab's inspection backlog (issues/fixes from inspector runs)."""
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    lab_id = lab["lab_id"]
    try:
        backlog = client.request("GET", f"/labs-v2/{lab_id}/backlog")
    except Exception:
        backlog = []
    if not backlog:
        print(_c("  Backlog vazio — rode headlabs labs inspect " + lab_id + " para gerar.", "dim"))
        return
    open_items = [b for b in backlog if b.get("status") != "done"]
    done_items = [b for b in backlog if b.get("status") == "done"]
    print(f"\n  {_c('Backlog', 'bold')} · {lab.get('name', lab_id)}  ({len(open_items)} abertos, {len(done_items)} concluídos)\n")
    for bl in open_items:
        sev = bl.get("severity", "?")
        scolor = "red" if sev in ("critical", "high") else ("yellow" if sev == "medium" else "dim")
        print(f"  {_c(f'[{sev}]', scolor)} {bl.get('resource', '')}")
        print(f"        {bl.get('description', '')}")
        print(_c(f"        id: {bl.get('id', '?')} · fonte: {bl.get('source', '?')}", "dim"))
    if done_items:
        print(f"\n  {_c('Concluídos', 'dim')} ({len(done_items)})")
        for bl in done_items[-3:]:
            print(f"    ✓ {bl.get('resource', '')} — {bl.get('description', '')[:80]}")


def _labs_fix(args):
    """Trigger a targeted remediation from the lab's OPEN backlog, without
    re-running the inspection. Groups open items by the loop_id they came from
    (a backlog can span several inspect runs / builds) and remediates the most
    recent one — the loop the current build's resources actually live in."""
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    lab_id = lab["lab_id"]
    try:
        backlog = client.request("GET", f"/labs-v2/{lab_id}/backlog")
    except Exception:
        backlog = []
    open_items = [b for b in (backlog or []) if b.get("status") != "done"]
    if not open_items:
        print(_c("  Backlog vazio — nada para corrigir. Rode: headlabs labs inspect "
                 + lab_id, "dim"))
        return

    # Backlog items carry loop_id since the inspect fix (older items may not —
    # fall back to the lab's latest build so those aren't silently dropped).
    loop_id = getattr(args, "loop", None)
    if not loop_id:
        with_loop = [b.get("loop_id") for b in open_items if b.get("loop_id")]
        if with_loop:
            loop_id = max(set(with_loop), key=with_loop.count)  # most common = current build
        else:
            try:
                lab_loops = client.request("GET", f"/loops?lab_id={lab_id}")
            except Exception:
                lab_loops = []
            builds = [l for l in lab_loops if l.get("status") in ("complete", "failed", "partial")]
            if not builds:
                _die(f"Nenhum loop concluído encontrado no lab {lab_id}. Use --loop <id>.", EXIT_USAGE)
            builds.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
            loop_id = builds[0]["loop_id"]

    targeted = [b for b in open_items if not b.get("loop_id") or b.get("loop_id") == loop_id]
    skipped = len(open_items) - len(targeted)

    issues = [{"resource": b.get("resource", ""), "severity": b.get("severity", "medium"),
              "detail": b.get("description", "")} for b in targeted]
    fixes = [{"resource": b.get("resource", ""), "action": b["fix"]}
             for b in targeted if b.get("fix")]

    print(f"  {_c('⚙', 'cyan')} Corrigindo {len(targeted)} item(ns) do backlog de {_c(lab_id, 'bold')} "
          f"(loop {loop_id})")
    if skipped:
        print(_c(f"    ({skipped} item(ns) de outro build ignorados — use --loop para incluí-los)", "dim"))
    for iss in issues:
        sev = iss.get("severity", "?")
        scolor = "red" if sev in ("critical", "high") else ("yellow" if sev == "medium" else "dim")
        print(f"    {_c(f'[{sev}]', scolor)} {iss.get('resource', '?')}")

    try:
        res = client.request("POST", f"/loops/{loop_id}/remediate",
                             json={"feedback": getattr(args, "intent", None)
                                   or "corrigir issues do backlog", "issues": issues, "fixes": fixes})
        print(f"  {_c('✓', 'green')} Remediação disparada ({res.get('issues', len(issues))} issues). "
              f"Acompanhe: headlabs loops watch {loop_id}")
    except Exception as e:
        _die(f"Não foi possível disparar remediação: {e}", EXIT_FAILED)
        return

    if getattr(args, "watch", False) or getattr(args, "wait", False):
        return _follow(client, loop_id, watch=getattr(args, "watch", False), args=args)


def _labs_inspect(args):
    """Run QA/specialist inspection on the lab's product (wrapper for cmd_inspect)."""
    # Map the positional 'lab' to the --lab attribute expected by cmd_inspect
    args.lab = getattr(args, "lab", None)
    return cmd_inspect(args)


def _labs_rebuild(args):
    """Rebuild = destroy all resources this lab created and build fresh from scratch,
    reusing the lab's research. The instruction refines the original intent."""
    client = HeadLabsClient()
    if not getattr(args, "intent", None):
        _die("rebuild requer -i/--intent com a instrução (o que mudar/recriar)", EXIT_USAGE)
    lab = _resolve_lab(client, args.lab)
    lid = lab["lab_id"]
    loops = client.request("GET", f"/labs-v2/{lid}/lineage") or []
    loops = sorted(loops, key=lambda l: l.get("started_at") or "")
    if not loops:
        _die("nenhum loop encontrado neste lab para rebuildar", EXIT_USAGE)
    # Prefer the most recent terminal (complete/failed) loop as the intent
    # source, but fall back to the most recent loop overall (cancelled,
    # superseded, awaiting_approval, etc.) — the rebuild endpoint only reads
    # lab_id + intent off it, both of which exist regardless of how the loop
    # ended. Requiring a terminal loop meant a lab where every attempt got
    # cancelled or superseded could never be rebuilt again.
    terminal = [l for l in loops if str(l.get("status", "")).lower() in ("complete", "failed", "partial")]
    loop_id = terminal[-1]["loop_id"] if terminal else loops[-1]["loop_id"]
    print(_c(f"⚠  rebuild vai DESTRUIR todos os recursos do lab {lid} e reconstruir do zero.", "yellow"))
    res = client.request("POST", f"/loops/{loop_id}/rebuild",
                         json={"instruction": args.intent,
                               "auto_approve": bool(getattr(args, "auto_approve", False))})
    new_id = res.get("loop_id", loop_id)
    print(_c(f"↻ rebuild: {new_id}", "green") +
          _c(f"  ({res.get('resources_destroyed', 0)} recursos destruídos)", "dim"))
    if getattr(args, "watch", False):
        return _follow(client, new_id, watch=True, args=args)


def _labs_repo(args):
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    lid = lab["lab_id"]
    if getattr(args, "cat", None):
        data = client.request("GET", f"/labs-v2/{lid}/repository/file", params={"path": args.cat})
        sys.stdout.write(data.get("content", ""))
        return
    files = client.request("GET", f"/labs-v2/{lid}/repository") or []
    if getattr(args, "output", "table") == "json":
        print(json.dumps(files, indent=2, default=str, ensure_ascii=False))
        return
    if not files:
        print(_c("Repositório vazio (nenhum loop concluiu arquivos ainda).", "dim"))
        return
    for f in sorted(files, key=lambda x: x.get("path", "")):
        print(f"  {f.get('size',0):>8}  {_c(f.get('language',''),'dim'):<14}  {f.get('path')}")
    print(_c(f"\n{len(files)} arquivo(s). Ver conteúdo: headlabs labs repo {lid} --cat <path>", "dim"))


def _labs_push(args):
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    token = getattr(args, "token", None) or __import__("os").environ.get("GITHUB_TOKEN")
    if not token:
        _die("informe --token ou defina GITHUB_TOKEN", EXIT_USAGE)
    body = {"repo": args.repo, "branch": getattr(args, "branch", "main") or "main",
            "token": token, "message": getattr(args, "message", None) or "Push from HeadLabs"}
    res = client.request("POST", f"/labs-v2/{lab['lab_id']}/push-github", json=body, timeout=120)
    print(_c(f"✓ push: {args.repo}", "green"))
    if getattr(args, "output", "table") == "json":
        print(json.dumps(res, indent=2, default=str, ensure_ascii=False))


def _labs_archive(args):
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    client.request("DELETE", f"/labs-v2/{lab['lab_id']}")
    print(_c(f"✓ lab arquivado: {lab.get('name')} ({lab['lab_id']})", "green"))


def _labs_create(args):
    """Create a lab workspace AND kick off the first build loop (the job)."""
    client = HeadLabsClient()
    intent = getattr(args, "intent", None)
    if not intent:
        _die("informe o objetivo do build com -i/--intent", EXIT_USAGE)
    name = getattr(args, "name", None) or _slug(intent)
    stack = [s.strip() for s in (getattr(args, "stack", None) or "").split(",") if s.strip()]
    lab = client.request("POST", "/labs-v2", json={"name": name, "description": intent, "stack": stack})
    lab_id = lab["lab_id"]
    gates = _gates_from_args(args)
    dry = getattr(args, "dry_run", False)
    loop = client.request("POST", "/loops", json={"intent": intent, "lab_id": lab_id,
                                                   **({"gates": gates} if gates is not None else {}),
                                                   **({"dry_run": True} if dry else {})})
    job_id = loop.get("loop_id")
    if getattr(args, "output", "table") == "json":
        print(json.dumps({"lab_id": lab_id, "job_id": job_id, "name": name}, indent=2))
    else:
        print(_c(f"✓ Lab criado: {lab_id}", "green") + f"  ({name})")
        tag = "  [dry-run · valida contrato, não cria recursos]" if dry else ""
        print(_c(f"✓ Build iniciado: {job_id}", "green") +
              (_c(tag, "yellow") if dry else
               (_c("  [auto-approve]", "dim") if gates and not any(gates.get(g) for g in _GATE_MAP.values()) else "")))
        print(_c(f"  Acompanhe:  headlabs loops watch {job_id}", "dim"))
        print(_c(f"  ou:         headlabs status {job_id}", "dim"))
    if getattr(args, "watch", False) or getattr(args, "wait", False):
        return _follow(client, job_id, watch=getattr(args, "watch", False), args=args)


# ════════════════════════════════════════════════════════════════════════════
# research (mode="research" loops — investigate, don't build)
# ════════════════════════════════════════════════════════════════════════════

def _research_opts(args) -> dict:
    """Research knobs sent to the server under the loop's ``research`` key.

    ``depth`` scales the investigation budget (breadth of sources + recursion);
    ``sources`` restricts/expands where the agent looks (default: web + the
    lab's accumulated repository)."""
    depth = (getattr(args, "depth", None) or "deep").strip().lower()
    if depth not in _RESEARCH_DEPTHS:
        _die(f"--depth inválido: {depth} (use {', '.join(_RESEARCH_DEPTHS)})", EXIT_USAGE)
    opts = {"depth": depth}
    sources = getattr(args, "sources", None)
    if sources:
        opts["sources"] = [s.strip() for s in sources.split(",") if s.strip()]
    return opts


def cmd_research(args):
    """``research "<tema>"`` — investigate a topic with amplified search (web +
    a broad investigative agent) and return findings. No build.

    Defaults cover the common case (deep depth, all available sources), so no
    flags are needed normally. Follow-up reuses the loop surface, which is
    mode-aware: ``headlabs status <id>``, ``headlabs loops watch <id>``,
    ``headlabs loops list --mode research``."""
    if getattr(args, "research_cmd", None) == "build":
        return _research_build(args)
    return _research_create(args)


def _research_create(args):
    client = HeadLabsClient()
    intent = getattr(args, "intent", None)
    if not intent:
        _die("informe o tema a investigar com -i/--intent", EXIT_USAGE)
    # Research accumulates findings in a lab (the context base). Reuse an
    # existing lab when given; otherwise spin up a fresh workspace to hold it.
    if getattr(args, "lab", None):
        lab = _resolve_lab(client, args.lab)
        lab_id, created = lab["lab_id"], False
    else:
        name = getattr(args, "name", None) or _slug(intent)
        stack = [s.strip() for s in (getattr(args, "stack", None) or "").split(",") if s.strip()]
        lab = client.request("POST", "/labs-v2",
                              json={"name": name, "description": intent, "stack": stack})
        lab_id, created = lab["lab_id"], True
    body = {"intent": intent, "lab_id": lab_id, "mode": "research",
            "research": _research_opts(args)}
    loop = client.request("POST", "/loops", json=body)
    job_id = loop.get("loop_id")
    if getattr(args, "output", "table") == "json":
        print(json.dumps({"lab_id": lab_id, "job_id": job_id, "mode": "research",
                          "created_lab": created}, indent=2))
    else:
        if created:
            print(_c(f"✓ Lab criado: {lab_id}", "green") + f"  ({lab.get('name')})")
        print(_c(f"✓ Pesquisa iniciada: {job_id}", "green") +
              _c(f"  [{body['research']['depth']}]", "dim"))
        print(_c(f"  Acompanhe:  headlabs loops watch {job_id}", "dim"))
        print(_c(f"  ou:         headlabs status {job_id}", "dim"))
    if getattr(args, "watch", False) or getattr(args, "wait", False):
        return _follow(client, job_id, watch=getattr(args, "watch", False),
                       args=args, mode="research")


def _research_build(args):
    """``research build --lab <lab> -i "..."`` — build a solution from the lab's
    research findings. Starts at architect (skips orchestrator/researcher)."""
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    lab_id = lab["lab_id"]
    gates = _gates_from_args(args)
    body = {"intent": args.intent, "lab_id": lab_id, "mode": "research_build"}
    if gates:
        body["gates"] = gates
    loop = client.request("POST", "/loops", json=body)
    job_id = loop.get("loop_id")
    print(f"  {_c('✓', 'green')} Build a partir da pesquisa: {_c(job_id, 'bold')}  (lab {lab_id})")
    print(_c(f"  Acompanhe: headlabs loops watch {job_id}", "dim"))
    if getattr(args, "watch", False) or getattr(args, "wait", False):
        return _follow(client, job_id, watch=getattr(args, "watch", False), args=args)


def _await_findings(client: HeadLabsClient, job_id: str, loop: dict,
                    attempts: int = 15, delay: float = 3.0) -> dict:
    """Re-fetch the loop until `findings` is populated.

    The deliverer marks the loop `complete` (via update_loop_state) a beat
    BEFORE the platform stores the final `findings` in its end-of-run callback,
    so a loop snapshot taken the instant status flips may not carry findings yet.
    Poll for up to ~45s to close that gap before rendering."""
    if loop.get("findings"):
        return loop
    for _ in range(attempts):
        time.sleep(delay)
        try:
            fresh = client.request("GET", f"/loops/{job_id}")
        except Exception:  # noqa: BLE001
            continue
        if fresh.get("findings"):
            return fresh
        loop = fresh or loop
    return loop


def _render_findings(loop: dict) -> None:
    """Render a research loop's findings: executive summary, key findings,
    opportunities (paths/ideas), and sources. Degrades gracefully when the
    server has not (yet) attached structured findings."""
    findings = loop.get("findings")
    if not findings:
        print(_c("Pesquisa concluída — os findings estão sendo finalizados.", "dim"))
        if loop.get("loop_id"):
            print(_c(f"  Rode em instantes: headlabs status {loop['loop_id']}", "dim"))
        return
    summary = findings.get("summary") or findings.get("overview")
    if summary:
        import textwrap
        print(f"\n{_c('Resumo da pesquisa', 'bold')}")
        for ln in textwrap.wrap(str(summary).strip(), width=78):
            print(f"  {ln}")

    key = findings.get("key_findings") or findings.get("themes") or findings.get("findings") or []
    if key:
        print(f"\n{_c('Principais achados', 'bold')} ({len(key)})")
        for f in key:
            if isinstance(f, dict):
                title = f.get("title") or f.get("finding") or f.get("detail") or ""
                detail = f.get("detail") if f.get("title") else None
            else:
                title, detail = str(f), None
            print(f"  {_c('●', 'cyan')} {title}")
            if detail and detail != title:
                import textwrap as _tw
                for ln in _tw.wrap(str(detail), width=90):
                    print(f"      {_c(ln, 'dim')}")

    opps = findings.get("opportunities") or findings.get("ideas") or findings.get("paths") or []
    if opps:
        print(f"\n{_c('Caminhos / ideias', 'bold')} ({len(opps)})")
        for o in opps:
            if isinstance(o, dict):
                title = o.get("title") or o.get("idea") or o.get("detail") or ""
                conf = o.get("confidence")
                tail = _c(f"   ({conf})", "dim") if conf else ""
            else:
                title, tail = str(o), ""
            print(f"  {_c('→', 'green')} {title}{tail}")

    sources = findings.get("sources") or []
    if sources:
        print(f"\n{_c('Fontes', 'bold')} ({len(sources)})")
        for s in sources:
            if isinstance(s, dict):
                title = s.get("title") or s.get("name") or ""
                url = s.get("url") or s.get("href") or ""
                print(f"  {_c('-', 'dim')} {title or url}  {_c(url if title else '', 'dim')}")
            else:
                print(f"  {_c('-', 'dim')} {s}")

    report = findings.get("report_path") or findings.get("report")
    if report and loop.get("lab_id"):
        print(_c(f"\nRelatório completo: headlabs labs repo {loop['lab_id']} --cat {report}", "dim"))


# ════════════════════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════════════════
# inspect
# ════════════════════════════════════════════════════════════════════════════

def cmd_inspect(args):
    """Invoke the loop-inspector on a lab's built product. Runs QA/specialist
    inspection, shows findings, and optionally triggers a fix cycle."""
    client = HeadLabsClient()
    lab = _resolve_lab(client, args.lab)
    lab_id = lab["lab_id"]

    # Find the latest build loop in this lab (or use --loop)
    loop_id = getattr(args, "loop", None)
    if not loop_id:
        # Query loops for this specific lab (server-side filter, paginated)
        try:
            lab_loops = client.request("GET", f"/loops?lab_id={lab_id}")
        except Exception:
            lab_loops = []
        # Prefer non-research builds; accept failed (still has resources to inspect)
        builds = [l for l in lab_loops if l.get("mode") != "research"
                  and l.get("status") in ("complete", "failed", "partial", "validating", "delivering")]
        if not builds:
            builds = [l for l in lab_loops if l.get("status") in ("complete", "failed", "partial")]
        if not builds:
            builds = lab_loops  # last resort: any loop in this lab
        if not builds:
            _die(f"Nenhum loop encontrado no lab {lab_id}. Use --loop <id>.", EXIT_USAGE)
        builds.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        loop_id = builds[0]["loop_id"]

    # Get loop details for resources/architecture
    loop = client.request("GET", f"/loops/{loop_id}")
    resources = loop.get("resources_created") or []
    architecture = loop.get("architecture") or {}
    role = getattr(args, "role", "qa")

    # Derive endpoints
    api_base = f"https://api.headlabs.ai/api/v1/apps/{lab_id}"
    fn_endpoints = [f"{api_base}/functions/{r.replace('function:', '')}"
                    for r in resources if r.startswith("function:")]
    # Same site-vs-data storage classification as `labs outputs` (labsctl.py) —
    # a data/export bucket (raw-*, *-artifacts, *-exports, *-snapshots, etc.)
    # is not a browsable site and was previously counted/inspected as one,
    # inflating the "Sites" count and pointing the inspector's http_get at a
    # bucket that was never meant to serve HTML.
    _DATA_STORAGE_PATTERNS = ("raw-", "artifact", "export", "snapshot", "backup", "cache", "log")
    storage_names = [r.replace("storage:", "") for r in resources if r.startswith("storage:")]
    name_filtered = [nm for nm in storage_names if not any(p in nm.lower() for p in _DATA_STORAGE_PATTERNS)]
    # Second pass: name doesn't catch every case (a data storage can have any
    # name — e.g. 'company-documents' matched none of the DATA patterns above
    # but genuinely never had an index.html, confirmed live: 403 AccessDenied
    # forever, correctly, since CloudFront+OAC has no key to serve). Ground
    # truth beats naming heuristics — check for a real index.html via the
    # storage's own file listing before calling it a "site".
    site_names = []
    for nm in name_filtered:
        try:
            files = client.request("GET", f"/storage/{nm}/files") or []
            if any(f.get("key") == "index.html" for f in files):
                site_names.append(nm)
        except Exception:  # noqa: BLE001 — best-effort; if we can't check, don't inflate the site count
            pass
    site_urls = [f"https://{nm}.apps.headlabs.ai/" for nm in site_names]


    print(f"  {_c('⚙', 'cyan')} Inspecionando lab {_c(lab_id, 'bold')} (loop {loop_id})")
    print(f"    Role: {_c(role, 'bold')} | Recursos: {len(resources)} | Sites: {len(site_urls)} | Functions: {len(fn_endpoints)}")

    # Invoke inspector
    body = {"input": {
        "intent": loop.get("intent", ""),
        "tenant_id": "platform",
        "loop_id": loop_id,
        "lab_id": lab_id,
        "role": role,
        "resources_created": resources,
        "architecture": architecture,
        "site_urls": site_urls,
        "function_endpoints": fn_endpoints,
    }}
    # User context/question for the inspector
    user_intent = getattr(args, "inspect_intent", None)
    if user_intent:
        body["input"]["user_context"] = user_intent
        body["input"]["intent"] = f"{loop.get('intent', '')} | FOCO DO USUÁRIO: {user_intent}"
    resp = client.request("POST", "/agents/loop-inspector/invoke", json=body)
    exec_id = resp.get("exec_id")
    print(f"    Execução: {exec_id}")
    print(_c("    Aguardando inspeção...", "dim"))

    # Poll for result. -w/--watch and --wait both mean "block until terminal" —
    # previously this loop ALWAYS gave up after a fixed 240s regardless of these
    # flags (they were accepted by the arg parser but never read here), so any
    # inspection slower than 4min (labs with 20+ resources routinely are) silently
    # timed out even when the user explicitly asked to wait.
    follow = bool(getattr(args, "watch", False) or getattr(args, "wait", False))
    result, attempt = None, 0
    while True:
        time.sleep(12)
        attempt += 1
        try:
            ex = client.request("GET", f"/executions/{exec_id}?tenant_id=platform")
        except Exception:
            if not follow and attempt >= 20:
                break
            continue
        if ex.get("status") in ("succeeded", "partial", "failed"):
            out = ex.get("output", "")
            if isinstance(out, str) and "_s3" in out:
                import boto3
                ref = json.loads(out)["_s3"][5:]
                bkt, _, key = ref.partition("/")
                result = json.loads(boto3.client("s3", region_name="us-east-1")
                                   .get_object(Bucket=bkt, Key=key)["Body"].read())
            elif isinstance(out, str):
                try:
                    result = json.loads(out)
                except Exception:
                    result = {"summary": out}
            elif isinstance(out, dict):
                result = out
            break
        if not follow and attempt >= 20:
            break  # default (no -w/--wait): give up after ~4min, as before
        if follow and attempt % 5 == 0:
            print(_c(f"    ⏳ Ainda inspecionando… ({attempt * 12}s, status={ex.get('status', '?')})", "dim"))

    if not result:
        print(_c("    ⏳ Inspeção ainda rodando. Verifique depois.", "yellow"))
        return

    # Render result
    status = result.get("status", "?")
    color = "green" if status == "pass" else ("yellow" if status == "partial" else "red")
    print(f"\n  {_c('Inspeção concluída', 'bold')} — {_c(status.upper(), color)}")
    print(f"  {result.get('summary', '')}\n")

    issues = result.get("issues") or []
    if issues:
        print(f"  {_c('Issues', 'bold')} ({len(issues)})")
        for iss in issues:
            sev = iss.get("severity", "?")
            scolor = "red" if sev in ("critical", "high") else ("yellow" if sev == "medium" else "dim")
            print(f"    {_c(f'[{sev}]', scolor)} {iss.get('resource', '?')}")
            print(f"          {iss.get('detail', '')}")
            if iss.get("fix"):
                print(f"          {_c('fix: ' + iss['fix'], 'dim')}")
        print()

    fixes = result.get("fixes_required") or []
    if fixes:
        print(f"  {_c('Fixes sugeridos', 'bold')} ({len(fixes)})")
        for f in fixes:
            if isinstance(f, dict):
                resource = f.get("resource") or f.get("priority") or ""
                action = f.get("action") or f.get("fix") or f.get("detail") or ""
            else:
                resource, action = "", str(f)
            print(f"    {_c('→', 'green')} {_c(resource, 'bold') + ': ' if resource else ''}{action}")
        print()

    # Persist fixes as backlog items in the lab (actionable for the team/UI).
    # Pair each issue with its suggested fix by resource so `headlabs labs fix`
    # can later trigger a remediation straight from the backlog, without
    # re-running the inspection (the fix text and loop_id ride along).
    fix_by_resource = {}
    for f in fixes:
        if isinstance(f, dict):
            res = f.get("resource") or f.get("priority") or ""
            action = f.get("action") or f.get("fix") or f.get("detail") or ""
            if res and action:
                fix_by_resource[res] = action
    backlog_items = []
    for iss in issues:
        entry = {
            "severity": iss.get("severity", "medium"),
            "resource": iss.get("resource", ""),
            "description": iss.get("detail", ""),
            "source": f"inspector/{role} (loop {loop_id})",
            "loop_id": loop_id,
        }
        fix_text = fix_by_resource.get(iss.get("resource", ""))
        if fix_text:
            entry["fix"] = fix_text
        backlog_items.append(entry)
    if backlog_items:
        try:
            resp = client.request("POST", f"/labs-v2/{lab_id}/backlog",
                                  json={"items": backlog_items})
            print(f"  {_c('📋', 'cyan')} {resp.get('added', len(backlog_items))} itens adicionados ao backlog do lab")
            print(_c(f"     Ver: headlabs labs backlog {lab_id}", "dim"))
        except Exception:
            pass  # best-effort — don't break the inspect flow

    # --fix: trigger a targeted remediation (planner surgical fix → re-exec → re-validate)
    if getattr(args, "fix", False) and issues and status in ("fail", "partial"):
        print(_c("  Disparando ciclo de correção (remediação)...", "cyan"))
        try:
            res = client.request("POST", f"/loops/{loop_id}/remediate",
                                 json={"feedback": result.get("summary", "") or "corrigir issues da inspeção",
                                       "issues": issues, "fixes": fixes})
            print(f"  {_c('✓', 'green')} Remediação disparada ({res.get('issues', len(issues))} issues). "
                  f"Acompanhe: headlabs loops watch {loop_id}")
        except Exception as e:
            print(f"  {_c('Não foi possível disparar remediação: ' + str(e), 'red')}")


# ════════════════════════════════════════════════════════════════════════════
# loops
# ════════════════════════════════════════════════════════════════════════════

def cmd_loops(args):
    sub = getattr(args, "loops_cmd", None)
    return {
        "create": _loops_create, "list": _loops_list, "ls": _loops_list,
        "status": _loops_status, "get": _loops_status, "describe": _loops_status,
        "watch": _loops_watch, "logs": _loops_logs,
        "approve": _loops_approve, "reject": _loops_reject,
        "pause": _loops_pause, "resume": _loops_resume,
        "cancel": _loops_cancel, "retry": _loops_retry, "iterate": _loops_iterate,
        "review": _loops_review, "panel": _loops_panel,
    }.get(sub, _loops_list)(args)


def _loops_create(args):
    client = HeadLabsClient()
    if not getattr(args, "intent", None):
        _die("informe o objetivo com -i/--intent", EXIT_USAGE)
    lab = _resolve_lab(client, args.lab) if getattr(args, "lab", None) else None
    gates = _gates_from_args(args)
    body = {"intent": args.intent, **({"lab_id": lab["lab_id"]} if lab else {}),
            **({"gates": gates} if gates is not None else {}),
            **({"dry_run": True} if getattr(args, "dry_run", False) else {})}
    loop = client.request("POST", "/loops", json=body)
    job_id = loop.get("loop_id")
    if getattr(args, "output", "table") == "json":
        print(json.dumps(loop, indent=2, default=str, ensure_ascii=False))
    else:
        print(_c(f"✓ Build iniciado: {job_id}", "green") + (f"  (lab {lab['lab_id']})" if lab else ""))
        print(_c(f"  Acompanhe: headlabs loops watch {job_id}", "dim"))
    if getattr(args, "watch", False) or getattr(args, "wait", False):
        return _follow(client, job_id, watch=getattr(args, "watch", False), args=args)


def _loops_list(args):
    client = HeadLabsClient()
    # Server-side params for lab/status let the backend paginate efficiently.
    # `mode` is filtered client-side only: it has a default ("build" when the
    # field is absent) that we own, so we never depend on the backend honoring
    # it — correctness stays with the CLI.
    params = []
    if getattr(args, "lab", None):
        lab = _resolve_lab(client, args.lab)
        params.append(f"lab_id={lab['lab_id']}")
    if getattr(args, "status", None):
        params.append(f"status={args.status}")
    qs = "?" + "&".join(params) if params else ""
    loops = client.request("GET", f"/loops{qs}") or []
    if getattr(args, "mode", None):
        loops = [l for l in loops if (l.get("mode") or "build") == args.mode]
    if getattr(args, "active", False):
        loops = [l for l in loops if l.get("status") not in _TERMINAL]
    if getattr(args, "quiet", False):
        for l in loops:
            print(l.get("loop_id"))
        return
    # Sort by age: most recent first
    loops.sort(key=lambda l: l.get("started_at") or l.get("updated_at") or "", reverse=True)
    cols = [
        ("JOB_ID", lambda l: l.get("loop_id", "")),
        ("LAB", lambda l: _trunc(l.get("lab_id", ""), 16)),
        ("MODE", lambda l: l.get("mode") or "build"),
        ("INTENT", lambda l: _trunc(l.get("intent"), 36)),
        ("STAGE", lambda l: l.get("phase", "")),
        ("STATUS", lambda l: display_status(l), "status"),
        ("AGE", lambda l: _age(l.get("started_at"))),
    ]
    if getattr(args, "output", "table") == "wide":
        cols.insert(6, ("ITER", lambda l: f"{l.get('iterations',0)}/{l.get('max_iterations',5)}"))
    render(loops, cols, getattr(args, "output", "table"),
           empty="Nenhum build. Inicie com: headlabs loops create --lab <lab> -i \"...\"")


def _loops_status(args):
    client = HeadLabsClient()
    loop = _get_loop(client, args.job_id)
    out = getattr(args, "output", "table")
    if out == "json":
        print(json.dumps(loop, indent=2, default=str, ensure_ascii=False))
        return _exit_for(loop)
    phase = loop.get("phase", "")
    is_research = loop.get("mode") == "research"
    phases = _RESEARCH_PHASES if is_research else _LOOP_PHASES
    cur_idx = phases.index(phase) if phase in phases else -1
    print(f"{_c('Job', 'bold')}:     {loop.get('loop_id')}   (lab: {loop.get('lab_id')})")
    print(f"Intent:  {_trunc(loop.get('intent'), 100)}")
    _kind = "Pesquisa" if is_research else "Build"
    print(f"Modo:    {_kind}    Status:  {_color_status(display_status(loop))}    "
          f"Iteração: {loop.get('iterations',0)}/{loop.get('max_iterations',5)}    "
          f"Início: {_age(loop.get('started_at'))} atrás")
    print(_c("Pipeline:", "bold"))
    for i, ph in enumerate(phases):
        if cur_idx < 0:
            mark, col = "·", "dim"
        elif i < cur_idx:
            mark, col = "✔", "green"
        elif i == cur_idx:
            mark, col = "▸", "cyan"
        else:
            mark, col = "·", "dim"
        print(f"  {_c(mark, col)} {ph}")
    if loop.get("pending_gate"):
        print(_c(f"\n⏸  Gate pendente: {loop['pending_gate']} — aprove com: "
                 f"headlabs loops approve {loop.get('loop_id')}", "yellow"))
    ga = loop.get("gate_assessment")
    if ga:
        verds = "  ".join(f"{v.get('role')}={v.get('verdict')}({v.get('score')})" for v in ga.get("verdicts", []))
        print(f"\n{_c('Banca', 'bold')} ({ga.get('gate')}): {_color_status(ga.get('recommendation',''))} "
              f"(agg {ga.get('aggregate_score')})  ·  {verds}")
        print(_c(f"   detalhe: headlabs loops panel {loop.get('loop_id')}", "dim"))
    # Interface contract (architecture's declared usage surface)
    arch = loop.get("architecture") or {}
    if isinstance(arch, dict) and "architecture" in arch:
        arch = arch["architecture"]
    if isinstance(arch, dict) and arch.get("interface"):
        print(f"\n{_c('Contrato', 'bold')}: interface={_c(arch.get('interface'), 'cyan')}  "
              f"entrypoint={arch.get('entrypoint', '-')}")
        planned = []
        for kind in ("storage", "functions", "tables", "agents", "mcps", "containers", "knowledge_bases"):
            for r in (arch.get(kind) or []):
                if isinstance(r, dict) and r.get("name"):
                    planned.append(f"{kind[:-1] if kind.endswith('s') else kind}:{r['name']}")
        if planned:
            print(f"  {_c('Planejado', 'dim')}: {', '.join(planned[:12])}")
        if loop.get("dry_run"):
            print(_c("  ⓘ dry-run: contrato validado, nenhum recurso criado.", "yellow"))

    res = loop.get("resources_created") or []
    if res:
        print(f"\n{_c('Recursos:', 'bold')} {', '.join(str(r) for r in res[:8])}")
    if is_research and (loop.get("status") or "").lower() in _TERMINAL_OK:
        _render_findings(_await_findings(client, args.job_id, loop))
    return _exit_for(loop)


def _loops_logs(args):
    client = HeadLabsClient()
    loop = _get_loop(client, args.job_id)
    trace = loop.get("agents_trace") or []
    phase_filter = getattr(args, "phase", None)
    for t in trace:
        if phase_filter and t.get("agent") != phase_filter:
            continue
        ts = (t.get("timestamp", "") or "")[11:19]
        print(f"  {_c(ts,'dim')}  {_c(t.get('agent',''),'cyan'):<14} {_trunc(t.get('action') or t.get('comment'), 80)}")


def _loops_approve(args):
    client = HeadLabsClient()
    res = client.request("POST", f"/loops/{args.job_id}/gate",
                         json={"action": "approve", "comment": getattr(args, "note", "") or ""})
    print(_c(f"✓ gate '{res.get('gate')}' aprovado — build retomado", "green"))
    if getattr(args, "watch", False):
        return _follow(client, args.job_id, watch=True, args=args)


def _loops_reject(args):
    client = HeadLabsClient()
    if not getattr(args, "note", None):
        _die("reject requer --note explicando o motivo", EXIT_USAGE)
    res = client.request("POST", f"/loops/{args.job_id}/gate",
                         json={"action": "reject", "comment": args.note})
    print(_c(f"↩ gate '{res.get('gate')}' rejeitado — refazendo fase anterior", "yellow"))


def _loops_pause(args):
    HeadLabsClient().request("POST", f"/loops/{args.job_id}/pause")
    print(_c(f"⏸ build pausado: {args.job_id}", "yellow"))


def _loops_resume(args):
    HeadLabsClient().request("POST", f"/loops/{args.job_id}/resume")
    print(_c(f"▶ build retomado: {args.job_id}", "green"))


def _loops_cancel(args):
    HeadLabsClient().request("DELETE", f"/loops/{args.job_id}")
    print(_c(f"✗ build cancelado: {args.job_id}", "yellow"))


def _loops_retry(args):
    client = HeadLabsClient()
    client.request("POST", f"/loops/{args.job_id}/retry")
    print(_c(f"↻ retry: {args.job_id}", "green"))
    if getattr(args, "watch", False):
        return _follow(client, args.job_id, watch=True, args=args)


def _loops_iterate(args):
    client = HeadLabsClient()
    if not getattr(args, "intent", None):
        _die("iterate requer -i/--intent com o ajuste desejado", EXIT_USAGE)
    res = client.request("POST", f"/loops/{args.job_id}/iterate", json={"intent": args.intent})
    new_id = res.get("loop_id", args.job_id)
    print(_c(f"↻ nova iteração: {new_id}", "green"))
    if getattr(args, "watch", False):
        return _follow(client, new_id, watch=True, args=args)


def _loops_watch(args):
    job_id = args.job_id
    if job_id.startswith("lab_"):
        _die(f"'{job_id}' é um lab, não um loop.\n"
             f"  Use: headlabs loops list --lab {job_id}\n"
             f"  Ou:  headlabs status", EXIT_USAGE)
    if not job_id.startswith("loop_"):
        _die(f"'{job_id}' não parece ser um loop_id válido.", EXIT_USAGE)
    return _follow(HeadLabsClient(), job_id, watch=True, args=args)


def _loops_review(args):
    """Convene the senior review panel on the loop's gate artifact (async)."""
    client = HeadLabsClient()
    body = {}
    if getattr(args, "reviewers", None):
        body["reviewers"] = [r.strip() for r in args.reviewers.split(",") if r.strip()]
    if getattr(args, "judges", None):
        body["judges"] = args.judges
    if getattr(args, "judge_model", None):
        body["judge_model"] = args.judge_model
    res = client.request("POST", f"/loops/{args.job_id}/review", json=body)
    print(_c(f"⚖  Banca convocada · gate {res.get('gate')} · {', '.join(res.get('reviewers', []))}", "cyan"))
    print(_c(f"   Parecer (assíncrono): headlabs loops panel {args.job_id}", "dim"))
    if getattr(args, "watch", False):
        return _follow(client, args.job_id, watch=True, args=args)


def _loops_panel(args):
    """Show the senior review panel's assessment for a loop."""
    client = HeadLabsClient()
    loop = _get_loop(client, args.job_id)
    a = loop.get("gate_assessment")
    ps = loop.get("panel_status")
    if not a:
        print(_c(f"Sem parecer da banca ainda (panel_status={ps or '-'}). "
                 f"Convoque: headlabs loops review {args.job_id}", "dim"))
        return
    if getattr(args, "output", "table") == "json":
        print(json.dumps(a, indent=2, default=str, ensure_ascii=False))
        return
    print(f"{_c('Banca de revisão', 'bold')} · gate {a.get('gate')} · "
          f"recomendação: {_color_status(a.get('recommendation', ''))} "
          f"(agregado {a.get('aggregate_score')})")
    for v in a.get("verdicts", []):
        print(f"  {_color_status(v.get('verdict', '')):<22} {v.get('role', ''):<12} "
              f"score={v.get('score')}  skills={','.join(v.get('skills_used', []) or [])}")
        for i in (v.get("issues") or [])[:6]:
            sev = i.get("severity", "")
            col = "red" if sev == "critical" else ("yellow" if sev in ("high", "medium") else "dim")
            print(f"      {_c(sev, col)}: {_trunc(i.get('detail'), 96)}")
        if v.get("error"):
            print(f"      {_c('erro', 'red')}: {_trunc(v.get('error'), 90)}")


# ── top-level status shortcut ─────────────────────────────────────────────────

def cmd_status(args):
    if getattr(args, "job_id", None):
        return _loops_status(args)
    # no arg → list active builds
    args.active = True
    return _loops_list(args)


# ── shared: fetch / follow ────────────────────────────────────────────────────

def _get_loop(client: HeadLabsClient, job_id: str) -> dict:
    try:
        return client.request("GET", f"/loops/{job_id}")
    except Exception:  # noqa: BLE001
        _die(f"build '{job_id}' não encontrado.", EXIT_USAGE)


def _exit_for(loop: dict) -> int:
    st = (loop.get("status") or "").lower()
    if st in _TERMINAL_FAIL:
        sys.exit(EXIT_FAILED)
    return EXIT_OK


def _follow(client: HeadLabsClient, job_id: str, *, watch: bool, args=None, mode=None) -> int:
    """Follow a build: render phase transitions + live tool events; stop on
    terminal status or a pending gate. Returns/exits with a semantic code.

    ``mode`` hints the kind of job ("research" vs build); when the finished
    loop is a research run, its synthesized findings are rendered on success."""
    from headlabs.progress import ProgressReporter
    from headlabs.config import get_tenant

    tenant = (getattr(args, "tenant", None) if args else None) or client.resolve_tenant() or get_tenant() or "platform"
    reporter = ProgressReporter(quiet=getattr(args, "quiet", False) if args else False,
                                verbose=getattr(args, "verbose", False) if args else False)
    reporter.begin_wait(f"Build {job_id[:14]}…")
    since, last_phase, last_status = 0, None, None
    deadline = time.time() + (getattr(args, "timeout", 0) if args else 0) * 1 if (args and getattr(args, "timeout", 0)) else None
    code = EXIT_OK
    try:
        while True:
            try:
                loop = client.request("GET", f"/loops/{job_id}")
            except Exception:  # noqa: BLE001
                loop = {}
            status = (loop.get("status") or "").lower()
            phase = loop.get("phase")
            # live tool/thinking events from the loop's agent executions
            try:
                body = client.get_events(job_id, since, tenant)
                for ev in body.get("events", []):
                    reporter.event(ev)
                since = body.get("last_seq", since)
            except Exception:  # noqa: BLE001
                pass
            if phase and phase != last_phase:
                reporter.event({"type": "status", "label": f"Fase: {phase}"})
                last_phase = phase
            if loop.get("pending_gate"):
                reporter.finish("paused")
                print(_c(f"⏸  Gate pendente: {loop['pending_gate']} — "
                         f"aprove: headlabs loops approve {job_id}", "yellow"))
                return EXIT_OK
            if status in _TERMINAL:
                if status in _TERMINAL_OK:
                    norm = "succeeded"
                elif status in _TERMINAL_CANCEL:
                    norm = "cancelled"
                elif status in _TERMINAL_PARTIAL:
                    norm = "partial"
                else:
                    norm = "failed"
                reporter.finish(norm)
                if norm == "partial":
                    print(_c("⚠  Build concluído PARCIALMENTE — um ou mais guards de qualidade "
                             f"rejeitaram o resultado (veja: headlabs status {job_id}).", "yellow"))
                if norm == "succeeded" and (mode == "research" or loop.get("mode") == "research"):
                    # findings land a beat after status flips to complete — wait for them
                    loop = _await_findings(client, job_id, loop)
                    _render_findings(loop)
                code = EXIT_FAILED if status in _TERMINAL_FAIL else EXIT_OK
                break
            if deadline and time.time() > deadline:
                reporter.finish("timeout")
                return EXIT_TIMEOUT
            time.sleep(4)
    except KeyboardInterrupt:
        reporter.finish("cancelled")
        print(_c(f"\n(build continua rodando — headlabs status {job_id})", "dim"))
        return EXIT_OK
    if not watch:  # --wait returns the code; watch is interactive
        sys.exit(code)
    return code
