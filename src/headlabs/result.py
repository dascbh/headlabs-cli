"""Result model for agent executions."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Result:
    status: str = "success"
    raw_output: dict = field(default_factory=dict)
    insights: list[dict] = field(default_factory=list)
    summary: str = ""
    total_saving_usd: float = 0.0
    account_id: str = ""
    cost_summary: dict = field(default_factory=dict)

    def to_html(self, path: str) -> None:
        from headlabs.renderers.html import render_html
        render_html(self, path)

    def to_json(self, path: str) -> None:
        from headlabs.renderers.json_renderer import render_json
        render_json(self, path)

    def to_markdown(self) -> str:
        from headlabs.renderers.markdown import render_markdown
        return render_markdown(self)
