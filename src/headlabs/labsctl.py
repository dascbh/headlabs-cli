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
_TERMINAL = _TERMINAL_OK | _TERMINAL_FAIL | _TERMINAL_CANCEL

_LOOP_PHASES = ["orchestrator", "researcher", "architect", "planner",
                "executor", "validator", "deliverer"]

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
    """Build the loop gate config from --auto-approve / --gate flags.
    None => server defaults (pause after architecture, plan, and destructive)."""
    if getattr(args, "auto_approve", False):
        return {"after_architect": False, "after_planner": False,
                "before_destructive": False, "auto_approve": [], "require_approval": []}
    gates = getattr(args, "gate", None)
    if gates:
        chosen = {_GATE_MAP[g.strip()] for g in gates.split(",") if g.strip() in _GATE_MAP}
        return {flag: (flag in chosen) for flag in _GATE_MAP.values()}
    return None  # server DEFAULT_GATES


# ════════════════════════════════════════════════════════════════════════════
# labs
# ════════════════════════════════════════════════════════════════════════════

def cmd_labs(args):
    sub = getattr(args, "labs_cmd", None)
    return {
        "create": _labs_create, "list": _labs_list, "ls": _labs_list,
        "get": _labs_get, "describe": _labs_describe, "repo": _labs_repo,
        "push": _labs_push, "archive": _labs_archive,
    }.get(sub, _labs_list)(args)


def _labs_list(args):
    client = HeadLabsClient()
    labs = client.request("GET", "/labs-v2") or []
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
    loop = client.request("POST", "/loops", json={"intent": intent, "lab_id": lab_id,
                                                   **({"gates": gates} if gates is not None else {})})
    job_id = loop.get("loop_id")
    if getattr(args, "output", "table") == "json":
        print(json.dumps({"lab_id": lab_id, "job_id": job_id, "name": name}, indent=2))
    else:
        print(_c(f"✓ Lab criado: {lab_id}", "green") + f"  ({name})")
        print(_c(f"✓ Build iniciado: {job_id}", "green") +
              (_c("  [auto-approve]", "dim") if gates and not any(gates.get(g) for g in _GATE_MAP.values()) else ""))
        print(_c(f"  Acompanhe:  headlabs loops watch {job_id}", "dim"))
        print(_c(f"  ou:         headlabs status {job_id}", "dim"))
    if getattr(args, "watch", False) or getattr(args, "wait", False):
        return _follow(client, job_id, watch=getattr(args, "watch", False), args=args)


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
            **({"gates": gates} if gates is not None else {})}
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
    loops = client.request("GET", "/loops") or []
    if getattr(args, "lab", None):
        lab = _resolve_lab(client, args.lab)
        loops = [l for l in loops if l.get("lab_id") == lab["lab_id"]]
    sfilter = getattr(args, "status", None)
    if sfilter:
        loops = [l for l in loops if display_status(l) == sfilter or l.get("status") == sfilter]
    if getattr(args, "active", False):
        loops = [l for l in loops if l.get("status") not in _TERMINAL]
    if getattr(args, "quiet", False):
        for l in loops:
            print(l.get("loop_id"))
        return
    cols = [
        ("JOB_ID", lambda l: l.get("loop_id", "")),
        ("LAB", lambda l: _trunc(l.get("lab_id", ""), 16)),
        ("INTENT", lambda l: _trunc(l.get("intent"), 36)),
        ("STAGE", lambda l: l.get("phase", "")),
        ("STATUS", lambda l: display_status(l), "status"),
        ("AGE", lambda l: _age(l.get("started_at"))),
    ]
    if getattr(args, "output", "table") == "wide":
        cols.insert(5, ("ITER", lambda l: f"{l.get('iterations',0)}/{l.get('max_iterations',5)}"))
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
    cur_idx = _LOOP_PHASES.index(phase) if phase in _LOOP_PHASES else -1
    print(f"{_c('Job', 'bold')}:     {loop.get('loop_id')}   (lab: {loop.get('lab_id')})")
    print(f"Intent:  {_trunc(loop.get('intent'), 100)}")
    print(f"Status:  {_color_status(display_status(loop))}    Iteração: {loop.get('iterations',0)}/{loop.get('max_iterations',5)}    Início: {_age(loop.get('started_at'))} atrás")
    print(_c("Pipeline:", "bold"))
    for i, ph in enumerate(_LOOP_PHASES):
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
    res = loop.get("resources_created") or []
    if res:
        print(f"\n{_c('Recursos:', 'bold')} {', '.join(str(r) for r in res[:8])}")
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
    return _follow(HeadLabsClient(), args.job_id, watch=True, args=args)


def _loops_review(args):
    """Convene the senior review panel on the loop's gate artifact (async)."""
    client = HeadLabsClient()
    body = {}
    if getattr(args, "reviewers", None):
        body["reviewers"] = [r.strip() for r in args.reviewers.split(",") if r.strip()]
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


def _follow(client: HeadLabsClient, job_id: str, *, watch: bool, args=None) -> int:
    """Follow a build: render phase transitions + live tool events; stop on
    terminal status or a pending gate. Returns/exits with a semantic code."""
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
                norm = "succeeded" if status in _TERMINAL_OK else ("cancelled" if status in _TERMINAL_CANCEL else "failed")
                reporter.finish(norm)
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
