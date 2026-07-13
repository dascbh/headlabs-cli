"""Checklist-driven usability evaluation.

Turns the free-form heuristic layer into a CALIBRATED, auditable pass: instead of
the agent inventing whatever findings it wants, the user supplies an explicit
checklist of criteria and the agent must return a verdict for EACH item
(pass / fail / n-a) grounded in the rendered page and the deterministic
browser-check results. Failed items become backlog findings; the full per-item
report is printed. The deterministic layer still runs underneath, so the objective
signals remain agent-independent — the checklist only constrains the subjective
layer to the user's own criteria.

File format is tolerant markdown/plain text — one criterion per line:

    # Formulários            <- headers/blank lines ignored
    - [ ] Todo campo tem label visível
    - (high) Botão primário destacado em cada tela
    1. Estados de loading/erro presentes

An optional ``(critical|high|medium|low)`` prefix sets that item's severity when
it fails (otherwise the agent's severity, else ``medium``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_SEVERITIES = {"critical", "high", "medium", "low"}
_MAX_ITEMS = 50
_SEV_PREFIX = re.compile(r"^\((critical|high|medium|low)\)\s*(.*)$", re.IGNORECASE)


@dataclass
class ChecklistItem:
    text: str
    severity: str | None = None  # user-specified; None → agent decides


def _strip_marker(line: str) -> str:
    line = re.sub(r"^[-*+]\s+", "", line)        # bullet
    line = re.sub(r"^\d+[.)]\s+", "", line)      # numbered list
    line = re.sub(r"^\[[ xX]?\]\s*", "", line)   # checkbox
    return line.strip()


def parse_checklist(text: str) -> list[ChecklistItem]:
    """Parse checklist text into items. Ignores blank lines and markdown headers.
    Caps at _MAX_ITEMS to keep the prompt bounded."""
    items: list[ChecklistItem] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body = _strip_marker(line)
        if not body:
            continue
        sev = None
        m = _SEV_PREFIX.match(body)
        if m:
            sev = m.group(1).lower()
            body = m.group(2).strip()
        if body:
            items.append(ChecklistItem(text=body, severity=sev))
        if len(items) >= _MAX_ITEMS:
            break
    return items


def build_checklist_instruction(items: list[ChecklistItem], url: str, tool_ctx: str,
                                context: str | None = None) -> str:
    """Build the agent instruction that forces a per-item verdict in order."""
    lines = "\n".join(f"{i + 1}. {it.text}" for i, it in enumerate(items))
    instr = (
        f"You are auditing the live page {url} for usability against a FIXED checklist.\n"
        f"Automated browser-check results (axe WCAG + mobile inspect) already computed:\n{tool_ctx}\n\n"
        "Evaluate EACH checklist item below against the page, grounded ONLY in the rendered "
        "text / DOM summary and the results above. Do not add items that aren't listed.\n"
        "Return ONLY a JSON array with exactly one object per item, IN THE SAME ORDER:\n"
        '{"n": <item number>, "verdict": "pass"|"fail"|"na", '
        '"evidence": "<what you observed on the page>", '
        '"severity": "critical"|"high"|"medium"|"low", "fix": "<how to fix, only if fail>"}\n'
        "Use \"na\" when the page doesn't have the element the item refers to.\n\n"
        f"Checklist:\n{lines}"
    )
    if context:
        instr += f"\n\nExtra focus from the user: {context}."
    return instr


def _looks_like_verdicts(v) -> bool:
    return (isinstance(v, list) and v and isinstance(v[0], dict)
            and ("verdict" in v[0] or "n" in v[0]))


def _coerce_array(result) -> list:
    """Pull the verdict array out of a platform Result, whatever shape it took.

    The agent may wrap the array under any key (observed: ``checklist_results``),
    so after the known keys we fall back to the first verdict-shaped list value.
    """
    from headlabs.local.inspector import _extract_json_array
    raw = getattr(result, "raw_output", None)
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for k in ("checklist_results", "results", "items", "verdicts", "findings"):
            if isinstance(raw.get(k), list):
                return raw[k]
        for v in raw.values():                       # any verdict-shaped list value
            if _looks_like_verdicts(v):
                return v
        if raw.get("answer"):
            return _extract_json_array(raw["answer"]) or []
    return _extract_json_array(getattr(result, "summary", "") or "") or []


def evaluate_results(result, items: list[ChecklistItem]) -> tuple[list[dict], list[dict]]:
    """Map the agent's verdict array back onto the checklist.

    Returns ``(report, findings)``:
    - report: one ``{"text", "verdict", "evidence"}`` per checklist item (in order).
    - findings: ``add_finding`` kwargs for each FAILED item (stable ``checklist:<n>``
      dedup key). Severity precedence: item's own > agent's > ``medium``.
    """
    arr = _coerce_array(result)
    by_idx: dict[int, dict] = {}
    for i, obj in enumerate(arr):
        if not isinstance(obj, dict):
            continue
        n = obj.get("n")
        idx = int(n) - 1 if (isinstance(n, int) or (isinstance(n, str) and n.isdigit())) else i
        by_idx[idx] = obj

    report, findings = [], []
    for idx, it in enumerate(items):
        obj = by_idx.get(idx, {})
        verdict = str(obj.get("verdict", "")).strip().lower()
        if verdict not in {"pass", "fail", "na"}:
            verdict = "na"
        evidence = str(obj.get("evidence", "")).strip()
        report.append({"text": it.text, "verdict": verdict, "evidence": evidence})
        if verdict == "fail":
            sev = it.severity or str(obj.get("severity", "medium")).strip().lower()
            if sev not in _SEVERITIES:
                sev = "medium"
            findings.append({
                "severity": sev,
                "title": f"Checklist: {it.text}",
                "detail": evidence or "Item da checklist reprovado.",
                "fix": str(obj.get("fix", "")).strip(),
                "file": f"checklist:{idx + 1}",
            })
    return report, findings
