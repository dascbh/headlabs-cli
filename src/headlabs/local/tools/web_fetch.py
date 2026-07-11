"""WebFetchTool — fetch and extract content from a specific URL.

Complements WebSearchTool: web_search finds URLs, web_fetch reads one you
already know. HTML is stripped to a readable text approximation (headings,
paragraphs, links) rather than returned raw, since raw HTML wastes the
model's context budget on markup it does not need.
"""
from __future__ import annotations

import re

import httpx
from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult

MAX_CONTENT_CHARS = 15_000
REQUEST_TIMEOUT_S = 15.0


class WebFetchInput(BaseModel):
    url: str = Field(..., description="URL to fetch")


def _html_to_text(html: str) -> str:
    """Best-effort HTML-to-text: strip scripts/styles, collapse tags to
    whitespace, decode a handful of common entities. Not a full HTML parser
    (no extra dependency) -- good enough for reading articles/docs, not for
    preserving exact layout or extracting structured data."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    entities = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&apos;": "'",
    }
    for entity, char in entities.items():
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = "Fetch and extract readable text content from a specific URL."
    input_schema = WebFetchInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return False

    @staticmethod
    def is_read_only() -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = WebFetchInput.model_validate(input_data)
        url = parsed.url
        if not re.match(r"^https?://", url):
            url = f"https://{url}"

        try:
            response = httpx.get(
                url,
                headers={"User-Agent": "headlabs-local/1.0"},
                timeout=REQUEST_TIMEOUT_S,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                output=f"URL returned HTTP {exc.response.status_code}: {url}", is_error=True
            )
        except httpx.HTTPError as exc:
            return ToolResult(output=f"Failed to fetch {url}: {exc}", is_error=True)

        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            text = _html_to_text(response.text)
        elif "json" in content_type or "text" in content_type:
            text = response.text
        else:
            return ToolResult(
                output=f"Unsupported content type for text extraction: {content_type!r} ({url})",
                is_error=True,
            )

        truncated = len(text) > MAX_CONTENT_CHARS
        text = text[:MAX_CONTENT_CHARS]
        if truncated:
            text += "\n... (truncated)"

        return ToolResult(output=text or "(empty response body)")
