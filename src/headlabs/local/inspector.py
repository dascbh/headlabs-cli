"""Inspector prompts, roles and helpers for ``headlabs local inspect``.

The "specialization" of the inspector is a versioned prompt shipped in the CLI
(not a remote agent): each role is a focus block appended to a shared base
prompt. This keeps the local inspector fully self-contained against a
self-hosted LLM, while still allowing optional enrichment from platform skills
(see :func:`fetch_skills`).
"""
from __future__ import annotations

import json

# Mirror the roles accepted by the platform `labs inspect` (see cli.py) so the
# local and platform inspectors share the same vocabulary.
ROLE_CHOICES = [
    "qa", "ux", "security", "architect", "performance",
    "devops", "data", "frontend", "backend", "usability",
]

_ROLE_FOCUS = {
    "qa": (
        "- Correctness bugs, unhandled edge cases, and broken happy paths.\n"
        "- Missing or weak tests for important behavior.\n"
        "- Error handling that swallows failures or crashes on bad input."
    ),
    "ux": (
        "- Confusing flows, missing loading/empty/error states.\n"
        "- Inconsistent copy, labels, and affordances.\n"
        "- Accessibility gaps (labels, contrast, keyboard nav)."
    ),
    "security": (
        "- Secrets or credentials committed to the repo.\n"
        "- Injection risks (SQL/command/template), missing input validation.\n"
        "- Broken authz/authn, unsafe deserialization, vulnerable dependencies\n"
        "  (inspect lockfiles; you may run read-only checks like `pip list` or\n"
        "  `npm audit` via bash)."
    ),
    "architect": (
        "- Poor separation of concerns, tight coupling, leaky abstractions.\n"
        "- Duplication that should be factored; modules doing too much.\n"
        "- Dependency direction and layering problems."
    ),
    "performance": (
        "- N+1 queries, unbounded loops, needless allocations.\n"
        "- Missing pagination/caching, synchronous work that blocks.\n"
        "- Obvious algorithmic complexity issues."
    ),
    "devops": (
        "- Dockerfile/CI problems, missing healthchecks, non-reproducible builds.\n"
        "- Config not following 12-factor; secrets in config files.\n"
        "- Missing pinned versions or lockfiles."
    ),
    "data": (
        "- Schema/migration issues, missing constraints or indexes.\n"
        "- Referential integrity gaps, unsafe migrations.\n"
        "- Inconsistent types and nullability."
    ),
    "frontend": (
        "- Console errors and failed network requests at runtime.\n"
        "- Missing loading/error states, render/hydration issues.\n"
        "- Accessibility gaps and obvious bundle/asset problems."
    ),
    "backend": (
        "- Error handling and input validation on endpoints.\n"
        "- Hardcoded secrets, missing authz checks.\n"
        "- API contract issues and inconsistent status codes."
    ),
    "usability": (
        "- Accessibility (WCAG): labels, contrast, alt text, ARIA, keyboard nav.\n"
        "- Responsive/mobile: horizontal overflow, tiny tap targets, broken layout.\n"
        "- Perceived performance and runtime console/JS errors that hurt the UX.\n"
        "- Content clarity, form/interaction burden, missing loading/error states.\n"
        "When a running URL is given (--url), drive the browser to inspect the live\n"
        "page (navigate, console/network, evaluate) rather than only the source."
    ),
}

_BASE_PROMPT = """\
You are a senior software inspector performing a {role} review of the project in
the current directory. Explore the codebase with the read-only tools (glob, grep,
read_file) and, when useful, run non-destructive shell commands with bash (e.g. a
linter, `git log`, listing dependencies). Do NOT modify any files — this is a
read-only inspection.

For EVERY concrete issue you find, call the `report_finding` tool exactly once, with:
- severity: one of critical, high, medium, low
- title: a short one-line summary
- detail: what is wrong and why it matters, citing the evidence you read
- fix: a concrete suggested correction
- file and line: when the issue is localized to a specific place

Rules:
- Base every finding on evidence you actually read. Do NOT invent issues.
- One `report_finding` call per distinct issue.
- When you have inspected the relevant areas, STOP and give a one-sentence summary
  (e.g. "Found 4 issues: 1 high, 3 medium.").

# {role} focus
{focus}
"""

_FRONTEND_URL_BLOCK = """\

# Running front-end at {url}
A dev server is expected at {url}. Use the browser_devtools tool to:
1. navigate to {url}
2. take a screenshot
3. read console logs (get_console_logs) and network requests (get_network_requests)
Turn any console errors and failed (4xx/5xx) requests into findings via
report_finding (severity by impact), citing the URL/message as evidence.
"""

FIX_SYSTEM_PROMPT = """\
You are a coding agent fixing issues in the project in the current directory.
Apply the MINIMAL change needed to resolve each issue. Use read_file to see exact
content before editing, edit_file to apply changes, and bash to run tests/linters.
Be concise. When done, state only what you changed in one sentence.
"""


def build_inspector_prompt(role: str, *, context: str | None = None,
                           url: str | None = None, skills: str = "") -> str:
    role = role if role in _ROLE_FOCUS else "qa"
    prompt = _BASE_PROMPT.format(role=role, focus=_ROLE_FOCUS[role])
    if url:
        prompt += _FRONTEND_URL_BLOCK.format(url=url)
    if context:
        prompt += f"\n# User focus\n{context}\n"
    if skills:
        prompt += f"\n# Skills (reference checklists)\n{skills}\n"
    return prompt


def inspect_task_message(role: str, url: str | None = None) -> str:
    msg = (f"Inspect this project as a {role} specialist. Report each issue you "
           f"find with the report_finding tool.")
    if url:
        msg += f" Also inspect the running front-end at {url}."
    return msg


def build_fix_prompt_from_findings(items: list[dict]) -> str:
    """Turn open backlog items into a single fix instruction for the coder."""
    lines = []
    for i, it in enumerate(items, 1):
        loc = it.get("resource", "")
        title = it.get("title") or it.get("description", "")
        fix = it.get("fix", "")
        lines.append(f"{i}. [{it.get('severity', 'medium')}] {loc}: {title}"
                     + (f"\n   Suggested fix: {fix}" if fix else ""))
    body = "\n".join(lines)
    return ("Fix the following inspection findings. Apply the minimal change for "
            "each, reading the file first to see exact content:\n\n" + body)


# ── platform provider (invoke a Claude-backed declarative agent) ─────────────

PLATFORM_AGENT_ID = "local-code-inspector"

_PLATFORM_AGENT_PROMPT = """\
You are a senior software inspector. You receive a bundle of source files from a
local project plus a target role (qa, ux, security, architect, performance,
devops, data, frontend, backend). Review the code as that specialist and find
real issues, citing evidence.

Return ONLY a JSON array (no prose, no markdown fences) of findings, each:
{"severity": "critical|high|medium|low", "title": "...", "detail": "... with
evidence ...", "fix": "concrete fix", "file": "path", "line": <int or null>}

Base every finding on the code you were given. Do not invent issues. If the code
is clean, return [].
"""

# Same excludes as GlobTool so the bundle mirrors what the local inspector sees.
_BUNDLE_EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                        "dist", "build", ".headlabs", ".mypy_cache", ".pytest_cache"}
_BUNDLE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rb", ".php", ".java",
                ".rs", ".html", ".css", ".yaml", ".yml", ".toml", ".cfg", ".ini",
                ".json", ".sh", ".env", ".sql", "Dockerfile", ".md"}
_BUNDLE_MAX_TOTAL = 120_000   # cap the whole bundle so the invoke payload stays sane
_BUNDLE_MAX_FILE = 20_000     # per-file cap


def build_code_bundle(directory: str) -> str:
    """Walk the project and concatenate its source files (bounded) into one
    text bundle to ship to the platform agent — the cloud runtime can't read
    the user's disk, so the CLI gathers the code client-side (the same read-only
    view the local inspector has) and sends it in the invoke payload."""
    import os

    parts, total = [], 0
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _BUNDLE_EXCLUDE_DIRS]
        for name in sorted(files):
            ext_ok = name in _BUNDLE_EXTS or os.path.splitext(name)[1] in _BUNDLE_EXTS
            if not ext_ok:
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, directory)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            if len(text) > _BUNDLE_MAX_FILE:
                text = text[:_BUNDLE_MAX_FILE] + "\n... (truncated)"
            block = f"===== FILE: {rel} =====\n{text}\n"
            if total + len(block) > _BUNDLE_MAX_TOTAL:
                parts.append(f"... (bundle truncated at {_BUNDLE_MAX_TOTAL} bytes)")
                return "\n".join(parts)
            parts.append(block)
            total += len(block)
    return "\n".join(parts) if parts else "(no source files found)"


# The usability inspector is a SEPARATE dedicated agent (not a role on the shared
# code inspector): it isolates prompt + tools + runtime. It carries the heavy,
# browser-devtools MCP so the code inspector — and, in production, the general
# loop-inspector — never pay the browser MCP's per-invocation session cost.
USABILITY_AGENT_ID = "usability-inspector"
BROWSER_MCP_ID = "browser-devtools"
BROWSER_MCP_ENDPOINT = "https://mcps.headlabs.ai/browser-devtools/mcp"

# The agent is a GROUNDED SYNTHESIZER, not a tool-caller: the CLI runs the
# deterministic browser checks (axe + inspect_page) and hands the results in.
# This removes the LLM from the objective-findings path entirely (those come
# straight from axe/browser → 100% reproducible), so the only variance left is
# the heuristic layer, which is additive. The agent needs no MCP of its own.
_USABILITY_AGENT_PROMPT = """\
You are a usability inspector. You receive a URL and the RESULTS of automated
browser checks already run on the live page (an axe-core WCAG audit plus a mobile
inspect_page with accessibility, responsive, performance and runtime signals).

Do NOT repeat issues the automated checks already captured (WCAG violations,
horizontal overflow, small tap targets, runtime/console errors, failed requests,
slow first-contentful-paint). Instead add HEURISTIC usability findings a rules
engine cannot catch, grounded in the provided rendered text and DOM summary:
content clarity and microcopy, form/interaction burden, missing loading/empty/
error states, navigation and flow, and information hierarchy.

Return ONLY a JSON array: [{"severity":"critical|high|medium|low","title":"...",
"detail":"...","fix":"..."}]. If you have no additional heuristic findings, return []."""


def ensure_usability_agent(client) -> str:
    """Provision the dedicated usability synthesizer agent (idempotent) and keep
    its prompt in sync. It carries NO MCP — the CLI runs the browser checks and
    passes results in, so the agent only synthesizes heuristic findings."""
    try:
        existing = {a.get("id") for a in client.list_remote_agents()}
    except Exception:
        existing = set()
    if USABILITY_AGENT_ID not in existing:
        client.create_agent(
            agent_id=USABILITY_AGENT_ID,
            display_name="Usability Inspector",
            prompt=_USABILITY_AGENT_PROMPT,
            description="Grounded synthesizer of heuristic usability findings from browser-check results.",
        )
    # Keep prompt in sync and ensure no MCP is attached (pure synthesizer).
    try:
        client.request("PATCH", f"/agents/{USABILITY_AGENT_ID}",
                       json={"prompt": _USABILITY_AGENT_PROMPT,
                             "manifest": {"skills": [], "tools_native": [], "mcp": []}})
    except Exception:
        pass
    return USABILITY_AGENT_ID


CHECKLIST_AGENT_ID = "usability-checklist"

_CHECKLIST_AGENT_PROMPT = """\
You are a usability auditor. You receive a live URL, the RESULTS of automated
browser checks already run on the page (axe-core WCAG audit + a mobile
inspect_page with rendered text and a DOM/accessibility summary), and a NUMBERED
checklist of criteria to judge.

Evaluate EACH checklist item against the page, grounded ONLY in the rendered
text/DOM summary and the results provided — never invent page content. Return
ONLY a JSON array with exactly one object per item, in the same order:
{"n": <item number>, "verdict": "pass"|"fail"|"na",
 "evidence": "<what you actually observed>",
 "severity": "critical"|"high"|"medium"|"low", "fix": "<how to fix, only if fail>"}

Use "pass" when the criterion is satisfied, "fail" when it is not, and "na" ONLY
when the page genuinely lacks the element the item refers to. Do not add, skip or
reorder items."""


def ensure_checklist_agent(client) -> str:
    """Provision the dedicated checklist-evaluator agent (idempotent) and keep its
    prompt in sync. Separate from the free-form usability agent so its system
    prompt matches the per-item verdict schema (a mismatched prompt makes the
    model return findings instead of verdicts)."""
    try:
        existing = {a.get("id") for a in client.list_remote_agents()}
    except Exception:
        existing = set()
    if CHECKLIST_AGENT_ID not in existing:
        client.create_agent(
            agent_id=CHECKLIST_AGENT_ID,
            display_name="Usability Checklist Auditor",
            prompt=_CHECKLIST_AGENT_PROMPT,
            description="Evaluates a user-supplied usability checklist item-by-item against a live page.",
        )
    try:
        client.request("PATCH", f"/agents/{CHECKLIST_AGENT_ID}",
                       json={"prompt": _CHECKLIST_AGENT_PROMPT,
                             "manifest": {"skills": [], "tools_native": [], "mcp": []}})
    except Exception:
        pass
    return CHECKLIST_AGENT_ID


def call_browser_mcp(tool: str, args: dict, tries: int = 4) -> dict:
    """Call a browser-devtools MCP tool directly (the deterministic path).
    Returns the tool's JSON dict, or ``{'error': ...}``. Retries transient
    gateway timeouts (a cold browser launch can trip the CloudFront window)."""
    import asyncio
    import base64
    import json as _json

    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except Exception as exc:  # noqa: BLE001
        return {"error": f"mcp client unavailable: {exc}"}

    from headlabs.client import HeadLabsClient
    c = HeadLabsClient()
    headers = {}
    if c.api_key:
        headers["Authorization"] = "Basic " + base64.b64encode(c.api_key.encode()).decode()

    async def _run():
        last = None
        for i in range(tries):
            try:
                async with streamablehttp_client(BROWSER_MCP_ENDPOINT, headers=headers,
                                                  timeout=90, terminate_on_close=False) as (r, w, _):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        res = await s.call_tool(tool, args)
                        txt = "".join(getattr(b, "text", "") for b in res.content)
                        return _json.loads(txt)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if i < tries - 1:
                    # Adaptive backoff: warm calls take ~8s, but a cold AgentCore
                    # runtime can take up to ~60s to spin up — keep retrying long
                    # enough to outlast a cold start rather than failing fast.
                    await asyncio.sleep(min(6 + i * 6, 24))
        return {"error": str(last)[:160]}

    return asyncio.run(_run())


_AXE_IMPACT_SEV = {"critical": "critical", "serious": "high", "moderate": "medium", "minor": "low"}


def deterministic_usability_findings(axe: dict, mobile: dict) -> list[dict]:
    """Build grounded, reproducible findings straight from the browser-check
    results (no LLM). Stable ``file`` keys (rule id / signal name) give stable
    dedup across runs — same site, same findings, every time."""
    out = []
    if isinstance(axe, dict) and "error" not in axe:
        for v in (axe.get("violations") or []):
            sev = _AXE_IMPACT_SEV.get(v.get("impact"), "medium")
            targets = ", ".join((v.get("sample_targets") or [])[:3])
            out.append({
                "severity": sev,
                "title": f"WCAG: {v.get('id')}",
                "detail": (f"{v.get('help', '')}. {v.get('node_count', 0)} elemento(s)."
                           + (f" Alvos: {targets}." if targets else "")
                           + (f" Ref: {v.get('helpUrl', '')}" if v.get("helpUrl") else "")),
                "fix": f"Corrigir a violação axe '{v.get('id')}': {v.get('description', '')}.",
                "file": f"wcag:{v.get('id')}",
            })
    if isinstance(mobile, dict) and "error" not in mobile:
        a = mobile.get("accessibility") or {}
        if a.get("horizontal_overflow"):
            out.append({"severity": "high", "title": "Overflow horizontal no mobile",
                        "detail": "A página ultrapassa a largura do viewport mobile (scroll horizontal).",
                        "fix": "Layout responsivo (max-width:100%, flex/grid, evitar larguras fixas).",
                        "file": "responsive:overflow"})
        if a.get("small_tap_targets"):
            out.append({"severity": "medium",
                        "title": f"{a['small_tap_targets']} alvo(s) de toque pequeno(s) (<40px)",
                        "detail": "Elementos clicáveis menores que ~40px dificultam o toque no mobile (WCAG 2.5.5).",
                        "fix": "Aumentar a área de toque para >=44x44px.",
                        "file": "responsive:tap-targets"})
        for pe in (mobile.get("page_errors") or [])[:10]:
            out.append({"severity": "high", "title": "Erro de JavaScript em runtime",
                        "detail": f"Exceção não tratada quebra a experiência: {pe}",
                        "fix": "Corrigir a exceção JavaScript.", "file": "runtime:pageerror"})
        for fr in (mobile.get("failed_requests") or [])[:8]:
            out.append({"severity": "medium", "title": "Requisição falha (recurso quebrado)",
                        "detail": f"{fr}", "fix": "Corrigir o recurso/endpoint ausente.",
                        "file": "runtime:failed-request"})
        perf = mobile.get("performance") or {}
        fcp = perf.get("fcp_ms")
        if isinstance(fcp, (int, float)) and fcp > 2500:
            out.append({"severity": "medium", "title": "First Contentful Paint lento",
                        "detail": f"FCP={int(fcp)}ms (>2.5s) prejudica a usabilidade percebida.",
                        "fix": "Otimizar carregamento (reduzir JS/CSS bloqueante, lazy-load, CDN).",
                        "file": "perf:fcp"})
    return out


def ensure_platform_agent(client) -> str:
    """Create the declarative inspector agent if it doesn't exist yet
    (idempotent). Returns its id."""
    try:
        existing = {a.get("id") for a in client.list_remote_agents()}
    except Exception:
        existing = set()
    if PLATFORM_AGENT_ID not in existing:
        client.create_agent(
            agent_id=PLATFORM_AGENT_ID,
            display_name="Local Code Inspector",
            prompt=_PLATFORM_AGENT_PROMPT,
            description="Reviews a bundle of local source files and returns JSON findings.",
        )
    return PLATFORM_AGENT_ID


def platform_findings_from_result(result) -> list[dict]:
    """Extract findings (add_finding kwargs) from a platform execution Result,
    whichever shape the agent returned them in."""
    raw = getattr(result, "raw_output", None)
    # Already-structured output: map the list directly (never round-trip through
    # the text parser, whose bracket matching isn't string-aware).
    if isinstance(raw, list):
        return _normalize_findings(raw)
    if isinstance(raw, dict):
        for key in ("findings", "issues"):
            if isinstance(raw.get(key), list):
                return _normalize_findings(raw[key])
        if raw.get("answer"):
            return parse_findings_fallback(raw["answer"])
    return parse_findings_fallback(getattr(result, "summary", "") or "")


def fetch_skills(skill_ids: list[str] | None) -> str:
    """Fetch skill content from the platform and concatenate it for prompt
    injection. Best-effort: a missing/unreachable skill (or no platform
    credentials) must never break a local inspection."""
    if not skill_ids:
        return ""
    try:
        from headlabs.client import HeadLabsClient
        client = HeadLabsClient()
    except Exception:
        return ""
    parts = []
    for sid in skill_ids:
        try:
            skill = client.request("GET", f"/resources/skill/{sid}")
            content = (skill or {}).get("content", "").strip()
            if content:
                parts.append(f"## {sid}\n{content}")
        except Exception:
            continue  # skip unreachable/unknown skills silently
    return "\n\n".join(parts)


_FINDING_KEYS = {"severity", "title", "detail", "description", "fix", "file", "line"}


def parse_findings_fallback(text: str) -> list[dict]:
    """Best-effort extraction of findings from the model's final text, for when
    it described issues in prose+JSON instead of calling report_finding. Looks
    for a JSON array of finding-like objects (or an object with a
    findings/issues array). Returns kwargs dicts for backlog.add_finding."""
    if not text:
        return []
    raw = _extract_json_array(text)
    return _normalize_findings(raw) if raw is not None else []


def _normalize_findings(items: list) -> list[dict]:
    """Coerce a list of finding-like dicts into add_finding kwargs. Shared by
    the text-fallback path and the platform (already-structured) path."""
    out = []
    for obj in items:
        if not isinstance(obj, dict) or not (obj.keys() & _FINDING_KEYS):
            continue
        title = str(obj.get("title") or obj.get("description") or "").strip()
        if not title:
            continue
        line = obj.get("line")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        out.append({
            "severity": str(obj.get("severity", "medium")).lower(),
            "title": title,
            "detail": str(obj.get("detail") or obj.get("description") or "").strip(),
            "fix": str(obj.get("fix", "")).strip(),
            "file": str(obj.get("file", "")).strip(),
            "line": line,
        })
    return out


def _extract_json_array(text: str) -> list | None:
    """Find the first balanced JSON array in the text and parse it. Also
    handles an object wrapping a 'findings'/'issues' array."""
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        while start >= 0:
            depth, end = 0, -1
            for i in range(start, len(text)):
                if text[i] == opener:
                    depth += 1
                elif text[i] == closer:
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > start:
                try:
                    parsed = json.loads(text[start:end])
                except (json.JSONDecodeError, ValueError):
                    parsed = None
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, dict):
                    for key in ("findings", "issues"):
                        if isinstance(parsed.get(key), list):
                            return parsed[key]
            start = text.find(opener, start + 1)
    return None
