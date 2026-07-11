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
    "devops", "data", "frontend", "backend",
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
    if raw is None:
        return []
    out = []
    for obj in raw:
        if not isinstance(obj, dict):
            continue
        if not (obj.keys() & _FINDING_KEYS):
            continue
        title = str(obj.get("title") or obj.get("description") or "").strip()
        if not title:
            continue
        detail = str(obj.get("detail") or obj.get("description") or "").strip()
        line = obj.get("line")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        out.append({
            "severity": str(obj.get("severity", "medium")).lower(),
            "title": title,
            "detail": detail,
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
