"""WebSearchTool — search the web via the Brave Search API.

Follows the same secret-handling convention used elsewhere in this repo for
third-party API keys (see mcps/mcp-cclasstrib/server.py, agents/*/tools.py):
never hardcode the key, read it from AWS Secrets Manager at runtime using the
caller's AWS credentials.

Secret: ``headlabs/brave-search-api-key`` in Secrets Manager, us-east-1,
account 688128002471. Same secret used by the platform's declarative agent
runtimes (api/routers/agents.py) and CDK (BRAVE_API_KEY at deploy time) — this
tool is a client-side (local) equivalent of the platform's `web_search` tool.

Requires AWS credentials in the environment/profile with
``secretsmanager:GetSecretValue`` on that secret (the same profile used for
`headlabs run --profile ...` already has this, since it is a HeadLabs-managed
secret, not a customer one).
"""
from __future__ import annotations

import functools

import httpx
from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult

BRAVE_SECRET_ID = "headlabs/brave-search-api-key"
BRAVE_SECRET_REGION = "us-east-1"
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_RESULTS = 10


class WebSearchInput(BaseModel):
    query: str = Field(..., description="Search query")
    count: int = Field(5, description="Number of results to return (max 10)")


@functools.lru_cache(maxsize=1)
def _get_brave_api_key() -> str:
    """Fetch and cache the Brave Search API key from AWS Secrets Manager.

    Cached per-process: the key does not change during a single CLI session,
    and re-fetching on every search call would be wasteful and slower.
    Raises on failure -- callers must catch and surface a clear tool error,
    never fall back to a hardcoded value.
    """
    import boto3

    client = boto3.client("secretsmanager", region_name=BRAVE_SECRET_REGION)
    response = client.get_secret_value(SecretId=BRAVE_SECRET_ID)
    secret_string = response["SecretString"]

    # The secret may be stored as a bare string or as {"api_key": "..."} /
    # {"BRAVE_API_KEY": "..."} depending on how it was created; handle both
    # without assuming a specific shape.
    stripped = secret_string.strip()
    if stripped.startswith("{"):
        import json

        data = json.loads(stripped)
        for key in ("api_key", "BRAVE_API_KEY", "brave_search_api_key", "value"):
            if key in data:
                return data[key]
        # Single-key dict: take whatever the one value is.
        if len(data) == 1:
            return next(iter(data.values()))
        raise ValueError(
            f"Secret {BRAVE_SECRET_ID!r} is a JSON object with unrecognized keys: "
            f"{list(data.keys())}"
        )
    return stripped


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the web using Brave Search and return titles, URLs, and snippets."
    input_schema = WebSearchInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return False  # read-only, no side effects on the user's system

    @staticmethod
    def is_read_only() -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = WebSearchInput.model_validate(input_data)
        count = max(1, min(parsed.count, MAX_RESULTS))

        try:
            api_key = _get_brave_api_key()
        except Exception as exc:
            return ToolResult(
                output=(
                    f"Could not retrieve Brave Search API key from Secrets Manager "
                    f"({BRAVE_SECRET_ID}, {BRAVE_SECRET_REGION}): {exc}. "
                    "Ensure AWS credentials are configured (e.g. AWS_PROFILE) and have "
                    "secretsmanager:GetSecretValue on this secret."
                ),
                is_error=True,
            )

        try:
            response = httpx.get(
                BRAVE_SEARCH_URL,
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                params={"q": parsed.query, "count": count},
                timeout=15.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                output=f"Brave Search API returned HTTP {exc.response.status_code}: {exc.response.text[:300]}",
                is_error=True,
            )
        except httpx.HTTPError as exc:
            return ToolResult(output=f"Failed to reach Brave Search API: {exc}", is_error=True)

        data = response.json()
        results = data.get("web", {}).get("results", [])
        if not results:
            return ToolResult(output=f"No results found for query: {parsed.query!r}")

        lines = []
        for i, item in enumerate(results[:count], start=1):
            title = item.get("title", "(no title)")
            url = item.get("url", "")
            description = item.get("description", "")
            lines.append(f"{i}. {title}\n   {url}\n   {description}")

        return ToolResult(output="\n\n".join(lines))
