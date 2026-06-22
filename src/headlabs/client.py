"""HeadLabs API client."""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from headlabs.config import get_api_key, get_api_url
from headlabs.result import Result
from headlabs.agents.registry import AGENT_REGISTRY
from headlabs.collectors.finops import FinOpsCollector
from headlabs.collectors.generic import GenericCollector


# Maps an agent's `collector` name (from AGENT_REGISTRY) to the collector class
# that gathers data LOCALLY using the client's AWS profile. Agents without a
# dedicated collector fall back to GenericCollector (account identity only).
COLLECTOR_MAP = {
    "finops": FinOpsCollector,
}


class HeadLabsClient:
    """Client for the HeadLabs AI Platform API."""

    def __init__(self, api_key: str | None = None, api_url: str | None = None):
        self.api_key = api_key or get_api_key()
        self.api_url = api_url or get_api_url()

    def _headers(self) -> dict:
        import base64
        return {
            "Authorization": f"Basic {base64.b64encode(self.api_key.encode()).decode()}",
            "Content-Type": "application/json",
        }

    def invoke(self, agent_id: str, input_data: dict) -> tuple[str, str]:
        """Invoke an agent. Returns (exec_id, tenant_id).

        The execution is created under the tenant that owns the API key; the
        server echoes that tenant in the response, and it MUST be supplied as
        the ``tenant_id`` query param when polling the execution. Defaults to
        ``platform`` only if the server omits it.
        """
        resp = requests.post(
            f"{self.api_url}/agents/{agent_id}/invoke",
            json={"input": input_data},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["exec_id"], data.get("tenant_id", "platform")

    def poll(self, exec_id: str, timeout: int = 600, tenant_id: str = "platform") -> Result:
        """Poll for execution result.

        ``tenant_id`` must match the tenant the execution was created under
        (returned by :meth:`invoke`). Using the wrong tenant returns 404.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = requests.get(
                f"{self.api_url}/executions/{exec_id}?tenant_id={tenant_id}",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            # Terminal states: succeeded plus the failure family (failed/dlq/timed_out/cancelled/rejected).
            if status in ("succeeded", "failed", "dlq", "timed_out", "cancelled", "rejected"):
                raw = data.get("output", "{}")
                output = json.loads(raw) if isinstance(raw, str) else (raw or {})
                norm = "succeeded" if status == "succeeded" else "failed"
                default_summary = "" if status == "succeeded" else f"Execution {status}"
                if status == "dlq":
                    default_summary = "Execution sent to dead-letter queue after retries"
                elif status == "timed_out":
                    default_summary = "Execution timed out"
                elif status == "cancelled":
                    default_summary = "Execution cancelled by operator"
                elif status == "rejected":
                    default_summary = "Execution rejected at approval gate"
                return Result(
                    status=norm,
                    raw_output=output,
                    insights=output.get("insights", []) if isinstance(output, dict) else [],
                    summary=(output.get("summary") if isinstance(output, dict) else None) or default_summary,
                    total_saving_usd=output.get("total_saving_usd", 0.0) if isinstance(output, dict) else 0.0,
                    account_id=output.get("account_id", "") if isinstance(output, dict) else "",
                    cost_summary=output.get("cost_summary", {}) if isinstance(output, dict) else {},
                )
            time.sleep(5)
            print(".", end="", flush=True)
        print()
        return Result(status="timeout")

    def run(self, agent_id: str, aws_profile: str, **kwargs: Any) -> Result:
        """Run an agent against the CLIENT's account.

        Data is collected LOCALLY using the client's AWS profile (credentials
        never leave the machine), then the collected summary is sent to the
        agent for analysis. This is the documented security model.
        """
        import boto3

        # Open a session with the client's profile — credentials stay local.
        session = boto3.Session(profile_name=aws_profile)

        # Resolve account ID from the client's profile (or explicit override).
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        account_id = kwargs.pop("account_id", None) or identity["Account"]

        # Pick the collector for this agent and gather data with the client's
        # credentials. Agents without a dedicated collector use GenericCollector.
        agent_cfg = next(
            (cfg for cfg in AGENT_REGISTRY.values() if cfg.get("agent_id") == agent_id),
            None,
        )
        collector_name = agent_cfg.get("collector") if agent_cfg else None
        collector_cls = COLLECTOR_MAP.get(collector_name, GenericCollector)
        collected = collector_cls(session).collect(**kwargs)

        # Build input in the format the agent expects, INCLUDING the data
        # collected locally against the client's account.
        input_data = {
            "tenant_id": "ALL",
            "lookback_days": kwargs.get("days", 30),
            "aws_region": session.region_name or "us-east-1",
            "collected_data": collected,
        }
        if kwargs.get("question"):
            input_data["question"] = kwargs["question"]
        if account_id:
            input_data["account_id"] = account_id

        print(f"  Account: {account_id}")
        exec_id, tenant_id = self.invoke(agent_id, input_data)
        print(f"  Exec: {exec_id[:8]}...")
        result = self.poll(exec_id, tenant_id=tenant_id)
        result.account_id = result.account_id or account_id
        return result

    def resolve_tenant(self) -> str | None:
        """Resolve the tenant that owns the configured API key.

        The tenant is a property of the key itself, so it must never be
        guessed. Uses ``GET /api-keys/me``, which returns the tenant for the
        calling key only (no listing, no cross-tenant exposure). The result is
        cached for the client's lifetime. Returns ``None`` only when it
        genuinely cannot be determined (endpoint unavailable, or an admin key
        with no bound tenant).
        """
        cached = getattr(self, "_tenant_cache", "unset")
        if cached != "unset":
            return cached
        tenant = None
        try:
            resp = requests.get(
                f"{self.api_url}/api-keys/me", headers=self._headers(), timeout=15
            )
            if resp.status_code == 200:
                tenant = resp.json().get("tenant") or None
        except Exception:
            tenant = None
        self._tenant_cache = tenant
        return tenant

    def list_agents(self) -> list[dict]:
        """List available agents."""
        return [
            {"name": k, **v}
            for k, v in AGENT_REGISTRY.items()
        ]

    # ── Remote API methods (platform resources) ───────────────────────────────

    def list_remote_agents(self) -> list[dict]:
        """List agents deployed on the platform."""
        resp = requests.get(f"{self.api_url}/agents", headers=self._headers(), timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return []

    def create_agent(self, agent_id: str, display_name: str, prompt: str,
                     model: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                     tools: list[str] | None = None, description: str = "") -> dict:
        """Create a declarative agent on the platform."""
        body = {
            "id": agent_id,
            "display_name": display_name,
            "description": description or f"Agent: {display_name}",
            "agent_type": "declarative",
            "prompt": prompt,
            "model": model,
            "framework": "strands",
            "manifest": {"skills": [], "tools_native": tools or [], "mcp": []},
            "sectors": [],
            "commercial_model": "usage",
            "sla_p99_ms": 30000,
            "price_monthly": 0,
            "price_per_1k_tokens": 0,
            "listed": False,
        }
        resp = requests.post(f"{self.api_url}/agents", json=body, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def list_skills(self) -> list[dict]:
        """List skills on the platform."""
        resp = requests.get(f"{self.api_url}/resources/skill", headers=self._headers(), timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else data.get("items", data.get("resources", []))
        return []

    def create_skill(self, skill_id: str, name: str, content: str) -> dict:
        """Create or update a skill on the platform."""
        resp = requests.put(
            f"{self.api_url}/resources/skill/{skill_id}",
            json={"name": name, "content": content},
            headers=self._headers(), timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def list_tools(self) -> list[dict]:
        """List tools and MCPs on the platform."""
        tools = []
        resp = requests.get(f"{self.api_url}/resources/tool", headers=self._headers(), timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            items = data if isinstance(data, list) else data.get("items", data.get("resources", []))
            for t in items:
                tools.append({"id": t.get("id"), "type": "native", "name": t.get("name", t.get("id"))})
        resp2 = requests.get(f"{self.api_url}/mcps", headers=self._headers(), timeout=15)
        if resp2.status_code == 200:
            for m in resp2.json():
                tools.append({"id": m.get("id"), "type": "mcp", "name": m.get("display_name", m.get("id"))})
        return tools

    def chat_stream(self, agent_id: str, session_id: str, message: str,
                    context: dict | None = None, history: list | None = None,
                    tenant_id: str | None = None):
        """Send chat message, poll for result, yield done event.

        ``tenant_id`` overrides the tenant used to poll the execution. The
        /chat endpoint may not echo the tenant, so callers can pass it
        explicitly (e.g. from config/--tenant) for non-platform tenants.
        """
        import sys
        import time as _time

        resp = requests.post(
            f"{self.api_url}/chat/{agent_id}",
            json={"session_id": session_id, "message": message,
                  "context": context or {}, "history": history or []},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        resp_json = resp.json()
        exec_id = resp_json.get("exec_id")
        # Tenant priority: explicit override (config/--tenant) > value echoed
        # by the server > tenant resolved from the API key > platform. The
        # /chat endpoint does not echo tenant_id, so we resolve it from the
        # key itself instead of blindly defaulting to platform (which would
        # query the wrong tenant's executions and 404).
        poll_tenant = (
            tenant_id
            or resp_json.get("tenant_id")
            or self.resolve_tenant()
            or "platform"
        )
        if not exec_id:
            yield {"type": "error", "error": "No exec_id returned"}
            return

        # Poll for result
        deadline = _time.time() + 480
        while _time.time() < deadline:
            r = requests.get(
                f"{self.api_url}/executions/{exec_id}?tenant_id={poll_tenant}",
                headers=self._headers(), timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "succeeded":
                    raw = data.get("output", "{}")
                    output = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    msg = output.get("answer") or output.get("response") or output.get("message") or str(output)
                    yield {"type": "done", "message": msg}
                    return
                elif data.get("status") in ("failed", "dlq", "timed_out"):
                    status = data.get("status")
                    raw = data.get("output", "{}")
                    output = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    err = output.get("error") if isinstance(output, dict) else None
                    yield {"type": "error", "error": err or f"Agent {status}"}
                    return
            _time.sleep(2)
            sys.stdout.write(".")
            sys.stdout.flush()
        yield {"type": "error", "error": "Timeout waiting for response"}
