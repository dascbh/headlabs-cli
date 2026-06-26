"""HTML report renderer."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from headlabs.result import Result


def render_html(result: Result, path: str) -> None:
    """Generate a dark-themed HTML report (agent-agnostic)."""
    agent_name = getattr(result, "agent_id", "") or "HeadLabs"
    title = f"{agent_name} Report"

    # Cost section (only if cost data present)
    cost_section = ""
    cs = result.cost_summary
    if isinstance(cs, dict) and (cs.get("by_account") or cs.get("total_usd")):
        cost_rows = ""
        for acct in cs.get("by_account", []):
            if isinstance(acct, dict):
                cost_rows += f"<tr><td style='font-family:monospace'>{acct.get('account_id','')}</td><td style='text-align:right'>${float(acct.get('cost_usd',0)):,.2f}</td></tr>\n"
        if not cost_rows and cs.get("total_usd"):
            cost_rows = f"<tr><td>{result.account_id or 'Total'}</td><td style='text-align:right'>${float(cs['total_usd']):,.2f}</td></tr>\n"
        cost_section = f"""<h2 style="font-size:16px;color:#4ecdc4;margin-bottom:12px">Cost Summary</h2>
<table><tr><th>Account</th><th style="text-align:right">Cost (USD)</th></tr>
{cost_rows}</table>"""

    # Savings (only if > 0)
    savings_section = ""
    if result.total_saving_usd and result.total_saving_usd > 0:
        savings_section = f'<div class="total">${result.total_saving_usd:,.0f}/mo potential savings</div>'

    # Findings
    findings_html = ""
    for insight in result.insights:
        sev = insight.get("severity", "medium")
        badge_color = {"critical": "#ff4757", "high": "#ff6b35", "medium": "#ffc107", "low": "#555"}.get(sev, "#95a5a6")
        text_color = "#000" if sev == "medium" else "#fff"
        saving = insight.get("saving_usd")
        saving_html = f"<div style='color:#4ecdc4;font-weight:700;margin-top:8px'>${saving:,.0f}/mo</div>" if saving else ""
        action = insight.get("action", "")
        action_html = f"<div style='color:#4ecdc4;font-size:13px;font-family:monospace;background:#0a1a1a;padding:8px 12px;border-radius:4px;margin-top:8px'>{action}</div>" if action else ""

        findings_html += f"""<div style="background:#111;border:1px solid #222;border-radius:8px;padding:20px;margin-bottom:16px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <span style="font-weight:600;font-size:15px">{insight.get('title', '')}</span>
    <span style="font-size:11px;padding:2px 8px;border-radius:4px;font-weight:600;text-transform:uppercase;background:{badge_color};color:{text_color}">{sev}</span>
  </div>
  <div style="color:#ccc;font-size:14px;line-height:1.5">{insight.get('finding', '')}</div>
  {action_html}
  {saving_html}
</div>\n"""

    findings_section = ""
    if findings_html:
        findings_section = f'<h2 style="font-size:18px;margin:32px 0 16px">Findings ({len(result.insights)})</h2>\n{findings_html}'

    summary_text = result.summary or "Analysis complete."

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 40px; }}
.container {{ max-width: 900px; margin: 0 auto; }}
h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 8px; color: #fff; }}
.subtitle {{ color: #888; font-size: 14px; margin-bottom: 32px; }}
.summary {{ background: #1a1a2e; border: 1px solid #333; border-radius: 12px; padding: 24px; margin-bottom: 32px; }}
.summary h2 {{ font-size: 16px; color: #4ecdc4; margin-bottom: 12px; }}
.summary p {{ line-height: 1.6; white-space: pre-wrap; }}
.total {{ font-size: 28px; font-weight: 700; color: #4ecdc4; margin: 16px 0; }}
table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
th, td {{ padding: 8px 12px; border: 1px solid #333; }}
th {{ background: #1a1a2e; text-align: left; font-size: 13px; color: #888; }}
.footer {{ text-align: center; color: #555; font-size: 12px; margin-top: 40px; }}
</style>
</head>
<body>
<div class="container">
<h1>{title}</h1>
<p class="subtitle">Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} • {f"Account: {result.account_id} • " if result.account_id else ""}Status: {result.status}</p>

<div class="summary">
<h2>Summary</h2>
<p>{summary_text}</p>
{savings_section}
</div>

{cost_section}
{findings_section}

<p class="footer">{agent_name} • headlabs.ai</p>
</div>
</body>
</html>"""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(html)
