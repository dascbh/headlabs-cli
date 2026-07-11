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

_USABILITY_AGENT_PROMPT = """\
You are a usability & accessibility inspector for live web front-ends. You are
given a URL of a RUNNING site and MUST use your browser tools to inspect the live
page (not source code):

- a11y_audit(url): objective WCAG 2.0/2.1 A&AA violations (impact, rule, help).
- inspect_page(url, viewport='mobile'): responsive issues (horizontal_overflow,
  small_tap_targets), performance (fcp_ms, load_ms), runtime console_errors /
  page_errors, an accessibility summary, and a rendered-text excerpt.
- Optionally inspect_page(url, viewport='desktop') to compare layouts.

Evaluate usability across: accessibility (WCAG), responsive/mobile layout,
perceived performance, runtime errors that break the experience, and — from the
rendered text and DOM summary — content clarity and form/interaction burden.

Return ONLY a JSON array of findings, each:
{"severity":"critical|high|medium|low","title":"...","detail":"... cite the tool
evidence (rule id, metric, count) ...","fix":"..."}
Ground every finding in the tool results — do not invent. If the page is solid,
return []."""


def ensure_usability_agent(client) -> str:
    """Provision the dedicated usability agent (idempotent) and ensure the
    browser-devtools MCP is attached to its manifest. Returns its id."""
    try:
        existing = {a.get("id") for a in client.list_remote_agents()}
    except Exception:
        existing = set()
    if USABILITY_AGENT_ID not in existing:
        client.create_agent(
            agent_id=USABILITY_AGENT_ID,
            display_name="Usability Inspector",
            prompt=_USABILITY_AGENT_PROMPT,
            description="Live front-end usability & accessibility inspection via a headless browser (browser-devtools MCP).",
        )
    # Attach the browser MCP (idempotent — safe to PATCH every run).
    try:
        client.request("PATCH", f"/agents/{USABILITY_AGENT_ID}",
                       json={"manifest": {"skills": [], "tools_native": [],
                                          "mcp": [{"server": BROWSER_MCP_ID}]}})
    except Exception:
        pass
    return USABILITY_AGENT_ID


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
