"""HeadLabs API client."""

from __future__ import annotations

import time
from typing import Any

import boto3
import requests

from headlabs.config import get_api_key, get_api_url
from headlabs.result import Result
from headlabs.agents.registry import AGENT_REGISTRY
from headlabs.collectors.finops import FinOpsCollector


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

    def invoke(self, agent_id: str, input_data: dict) -> str:
        """Invoke an agent and return execution ID."""
        resp = requests.post(
            f"{self.api_url}/agents/{agent_id}/invoke",
            json=input_data,
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["execution_id"]

    def poll(self, exec_id: str, timeout: int = 300) -> Result:
        """Poll for execution result."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = requests.get(
                f"{self.api_url}/executions/{exec_id}",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") in ("completed", "failed"):
                return Result(
                    status=data["status"],
                    raw_output=data.get("output", {}),
                    insights=data.get("output", {}).get("insights", []),
                    summary=data.get("output", {}).get("summary", ""),
                    total_saving_usd=data.get("output", {}).get("total_saving_usd", 0.0),
                    account_id=data.get("output", {}).get("account_id", ""),
                    cost_summary=data.get("output", {}).get("cost_summary", {}),
                )
            time.sleep(5)
        return Result(status="timeout")

    def run(self, agent_id: str, aws_profile: str, **kwargs: Any) -> Result:
        """High-level: collect data locally, invoke agent, poll for result."""
        agent_cfg = None
        for cfg in AGENT_REGISTRY.values():
            if cfg["agent_id"] == agent_id:
                agent_cfg = cfg
                break
        if not agent_cfg:
            raise ValueError(f"Unknown agent: {agent_id}")

        collector_cls = COLLECTOR_MAP.get(agent_cfg["collector"])
        if not collector_cls:
            raise ValueError(f"No collector for: {agent_cfg['collector']}")

        session = boto3.Session(profile_name=aws_profile)
        collector = collector_cls(session)
        collected = collector.collect(**kwargs)

        input_data = {"collected_data": collected, **kwargs}
        exec_id = self.invoke(agent_id, input_data)
        return self.poll(exec_id)

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
        resp = requests.get(f"{self.api_url}/resources?rtype=skill", headers=self._headers(), timeout=15)
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
        resp = requests.get(f"{self.api_url}/resources?rtype=tool", headers=self._headers(), timeout=15)
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
