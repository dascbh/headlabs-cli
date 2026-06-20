"""Markdown report renderer."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from headlabs.result import Result


def render_markdown(result: Result) -> str:
    """Return formatted markdown report."""
    lines = [
        "# HeadLabs Report",
        "",
        f"**Status:** {result.status}",
        f"**Account:** {result.account_id}",
        f"**Potential Savings:** ${result.total_saving_usd:,.2f}",
        "",
        "## Summary",
        result.summary,
        "",
        "## Cost by Account",
        "| Account | Cost (USD) |",
        "|---------|-----------|",
    ]
    for acct, amt in result.cost_summary.items():
        lines.append(f"| {acct} | ${amt:,.2f} |")

    if result.insights:
        lines += ["", "## Findings"]
        for i in result.insights:
            sev = i.get("severity", "info").upper()
            lines.append(f"- **[{sev}]** {i.get('title', '')} — {i.get('description', '')}")

    return "\n".join(lines)
