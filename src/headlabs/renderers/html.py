"""HTML report renderer."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from headlabs.result import Result


def render_html(result: Result, path: str) -> None:
    """Generate a dark-themed HTML report."""
    cost_rows = ""
    for acct, amt in result.cost_summary.items():
        cost_rows += f"<tr><td>{acct}</td><td>${amt:,.2f}</td></tr>\n"

    findings_html = ""
    for insight in result.insights:
        sev = insight.get("severity", "info")
        badge_color = {"critical": "#e74c3c", "high": "#e67e22", "medium": "#f39c12", "low": "#27ae60"}.get(sev, "#95a5a6")
        findings_html += f"""<div class="finding">
            <span class="badge" style="background:{badge_color}">{sev.upper()}</span>
            <span>{insight.get('title', '')}</span>
            <p>{insight.get('description', '')}</p>
        </div>\n"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>HeadLabs Report</title>
<style>
body{{background:#1a1a2e;color:#eee;font-family:system-ui,sans-serif;padding:2rem;margin:0}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}
th,td{{padding:.5rem 1rem;border:1px solid #333;text-align:left}}
th{{background:#16213e}}
.finding{{padding:.75rem;margin:.5rem 0;background:#16213e;border-radius:6px}}
.badge{{padding:2px 8px;border-radius:4px;font-size:.75rem;color:#fff;margin-right:.5rem}}
.total{{font-size:1.5rem;color:#00d2ff;margin:1rem 0}}
.meta{{color:#888;font-size:.8rem;margin-top:2rem}}
</style></head>
<body>
<h1>HeadLabs FinOps Report</h1>
<p>{result.summary}</p>
<div class="total">Potential Savings: ${result.total_saving_usd:,.2f}</div>
<h2>Cost by Account</h2>
<table><tr><th>Account</th><th>Cost (USD)</th></tr>
{cost_rows}</table>
<h2>Findings</h2>
{findings_html}
<div class="meta">Generated: {datetime.utcnow().isoformat()}Z | Account: {result.account_id} | Status: {result.status}</div>
</body></html>"""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(html)
