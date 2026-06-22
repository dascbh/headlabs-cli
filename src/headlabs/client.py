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


def _collected_summary(collected: dict) -> str:
    """Short human summary of locally-collected data for the progress line."""
    if not isinstance(collected, dict):
        return ""
    parts = []
    svcs = collected.get("top_services")
    if isinstance(svcs, dict) and svcs:
        parts.append(f"{len(svcs)} serviços")
    accts = collected.get("by_account")
    if isinstance(accts, dict) and accts:
        parts.append(f"{len(accts)} contas")
    total = collected.get("total_usd")
    if isinstance(total, (int, float)):
        parts.append(f"${total:,.0f}")
    return " · ".join(parts)


def _ephemeral_credentials(session) -> dict | None:
    """Short-lived AWS credentials for the agent to read the CLIENT's account.

    Option B: the agent runs on HeadLabs infrastructure but accesses the
    client's account using these credentials, then stores nothing. We never
    transmit long-lived keys:

    - SSO / assumed-role profiles already carry a session token (temporary) —
      forward as-is.
    - Static IAM-user keys: mint a short-lived session via STS GetSessionToken
      so the long-lived key never leaves the machine.

    Returns ``{aws_access_key_id, aws_secret_access_key, aws_session_token?}``
    or ``None`` if no credentials are available.
    """
    try:
        creds = session.get_credentials()
        if creds is None:
            return None
        frozen = creds.get_frozen_credentials()
        if frozen.token:
            return {
                "aws_access_key_id": frozen.access_key,
                "aws_secret_access_key": frozen.secret_key,
                "aws_session_token": frozen.token,
            }
        # Static long-lived keys: exchange for a short-lived session token.
        try:
            tok = session.client("sts").get_session_token(
                DurationSeconds=3600
            )["Credentials"]
            return {
                "aws_access_key_id": tok["AccessKeyId"],
                "aws_secret_access_key": tok["SecretAccessKey"],
                "aws_session_token": tok["SessionToken"],
            }
        except Exception:
            # Best effort: forward static keys (no token). Caller should warn.
            return {
                "aws_access_key_id": frozen.access_key,
                "aws_secret_access_key": frozen.secret_key,
            }
    except Exception:
        return None


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

    # Terminal execution states: succeeded plus the failure family.
    _TERMINAL = ("succeeded", "failed", "dlq", "timed_out", "cancelled", "rejected", "partial")

    def invoke(self, agent_id: str, input_data: dict) -> tuple[str, str, str]:
        """Invoke an agent. Returns (exec_id, tenant_id, stream_id).

        The execution is created under the tenant that owns the API key; the
        server echoes that tenant in the response, and it MUST be supplied as
        the ``tenant_id`` query param when polling. ``stream_id`` is the id to
        poll for progress events (``root_trace_id``, which equals ``exec_id``
        for non-map executions).
        """
        resp = requests.post(
            f"{self.api_url}/agents/{agent_id}/invoke",
            json={"input": input_data},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        exec_id = data["exec_id"]
        tenant_id = data.get("tenant_id", "platform")
        stream_id = data.get("root_trace_id") or exec_id
        return exec_id, tenant_id, stream_id

    def get_events(self, stream_id: str, since: int = 0,
                   tenant_id: str = "platform") -> dict:
        """Fetch progress events for an execution stream since ``since`` seq.

        Returns ``{exec_id, status, events: [...], last_seq}``. Each event has
        ``seq, ts, type, label, tool?, level``.
        """
        resp = requests.get(
            f"{self.api_url}/executions/{stream_id}/events"
            f"?since={since}&tenant_id={tenant_id}",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _get_execution(self, exec_id: str, tenant_id: str) -> dict:
        resp = requests.get(
            f"{self.api_url}/executions/{exec_id}?tenant_id={tenant_id}",
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _result_from_execution(data: dict) -> Result:
        status = data.get("status")
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

    def poll(self, exec_id: str, timeout: int = 600, tenant_id: str = "platform",
             stream_id: str | None = None, reporter=None) -> Result:
        """Poll for execution result.

        ``tenant_id`` must match the tenant the execution was created under
        (returned by :meth:`invoke`); the wrong tenant returns 404. When a
        ``reporter`` is supplied, live progress events are consumed from the
        event stream and rendered as they arrive; otherwise this falls back to
        plain status polling (with ``.`` heartbeat) for SDK/library use.
        """
        stream = stream_id or exec_id
        since = 0
        deadline = time.time() + timeout
        if reporter is not None:
            reporter.begin_wait()
        final_status = None
        while time.time() < deadline:
            status = None
            if reporter is not None:
                # Drive both rendering and terminal detection off the event
                # stream (it returns status alongside the events).
                try:
                    body = self.get_events(stream, since, tenant_id)
                    for ev in body.get("events", []):
                        reporter.event(ev)
                    since = body.get("last_seq", since)
                    status = body.get("status")
                except Exception:
                    status = None
            if status is None:
                # No reporter, or events unavailable: read execution status.
                try:
                    status = self._get_execution(exec_id, tenant_id).get("status")
                except Exception:
                    status = None
            if status in self._TERMINAL:
                final_status = status
                break
            time.sleep(1.5 if reporter is not None else 5)
            if reporter is None:
                print(".", end="", flush=True)

        if final_status is None:
            if reporter is not None:
                reporter.finish("timeout")
            else:
                print()
            return Result(status="timeout")

        data = self._get_execution(exec_id, tenant_id)
        result = self._result_from_execution(data)
        if reporter is not None:
            reporter.finish(final_status, result.summary)
        else:
            print()
        return result

    def run(self, agent_id: str, aws_profile: str, reporter=None, **kwargs: Any) -> Result:
        """Run an agent against the CLIENT's account.

        Option B (client-side credential management): short-lived session
        credentials are derived from the operator's LOCAL AWS profile and sent
        to the ephemeral agent, which reads the client's account and stores
        nothing. Long-lived keys never leave the machine. Without credentials
        the agent fails closed server-side (it never touches another account).

        When ``reporter`` is provided, local phases and the agent's live
        progress events are rendered as the run proceeds.
        """
        import boto3

        # Open a session with the client's profile — credentials stay local.
        session = boto3.Session(profile_name=aws_profile)

        # Resolve account ID from the client's profile (or explicit override).
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        account_id = kwargs.pop("account_id", None) or identity["Account"]
        if reporter is not None:
            reporter.phase("Perfil AWS resolvido", account_id)

        # Derive short-lived credentials for the agent to read this account.
        creds = _ephemeral_credentials(session)
        if reporter is not None:
            if creds and creds.get("aws_session_token"):
                reporter.phase("Credenciais temporárias preparadas")
            elif creds:
                reporter.phase("Credenciais preparadas", "estáticas (sem session token)")
            else:
                reporter.phase("Sem credenciais AWS", "a execução pode ser bloqueada")

        # Build the agent input: account + short-lived credentials. The agent
        # fetches live data itself using these (no local collection needed).
        input_data = {
            "tenant_id": "ALL",
            "lookback_days": kwargs.get("days", 30),
            "aws_region": session.region_name or "us-east-1",
        }
        if account_id:
            input_data["account_id"] = account_id
        if creds:
            input_data.update(creds)
        if kwargs.get("question"):
            input_data["question"] = kwargs["question"]

        exec_id, tenant_id, stream_id = self.invoke(agent_id, input_data)
        if reporter is not None:
            reporter.invoked(exec_id)
        else:
            print(f"  Account: {account_id}")
            print(f"  Exec: {exec_id[:8]}...")
        result = self.poll(exec_id, tenant_id=tenant_id, stream_id=stream_id, reporter=reporter)
        result.account_id = result.account_id or account_id
        return result

    def resolve_tenant(self, refresh: bool = False) -> str | None:
        """Resolve the tenant that owns the configured API key.

        The tenant is a property of the key itself, so it must never be
        guessed. Uses ``GET /api-keys/me``, which returns the tenant for the
        calling key only (no listing, no cross-tenant exposure). The result is
        cached for the client's lifetime (``refresh=True`` re-queries). Returns
        ``None`` only when it genuinely cannot be determined (endpoint
        unavailable, or an admin key with no bound tenant).
        """
        cached = getattr(self, "_tenant_cache", "unset")
        if cached != "unset" and not refresh:
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
        stream = resp_json.get("root_trace_id") or exec_id
        if not exec_id:
            yield {"type": "error", "error": "No exec_id returned"}
            return

        # Consume the live event stream: yield tool_use/status progress as it
        # arrives, then the final answer from the execution output.
        terminal = ("succeeded", "partial", "failed", "dlq",
                    "timed_out", "cancelled", "rejected")
        since = 0
        deadline = _time.time() + 480
        notfound = 0
        retried_tenant = False
        while _time.time() < deadline:
            status = None
            try:
                body = self.get_events(stream, since, poll_tenant)
                for ev in body.get("events", []):
                    if ev.get("type") in ("tool_use", "status", "step", "thinking"):
                        yield {"type": "progress", "event": ev}
                since = body.get("last_seq", since)
                status = body.get("status")
                notfound = 0
            except requests.HTTPError as exc:
                if getattr(exc.response, "status_code", None) == 404:
                    # Almost always a tenant mismatch: the execution lives under
                    # a different tenant than we're polling. Recover by
                    # re-resolving the key's tenant once; otherwise fail fast
                    # with a clear message instead of silently timing out.
                    if not retried_tenant:
                        retried_tenant = True
                        fresh = self.resolve_tenant(refresh=True)
                        if fresh and fresh != poll_tenant:
                            poll_tenant = fresh
                            continue
                    notfound += 1
                    if notfound >= 3:
                        yield {"type": "error",
                               "error": (f"Execução não encontrada no tenant "
                                         f"'{poll_tenant}'. Passe --tenant <id> "
                                         f"ou configure 'tenant' no config.")}
                        return
                    _time.sleep(1.5)
                    continue
                status = None
            except Exception:
                status = None

            if status in terminal:
                data = self._get_execution(exec_id, poll_tenant)
                raw = data.get("output", "{}")
                output = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if status in ("succeeded", "partial"):
                    msg = (output.get("answer") or output.get("response")
                           or output.get("message") or str(output))
                    yield {"type": "done", "message": msg}
                else:
                    err = output.get("error") if isinstance(output, dict) else None
                    yield {"type": "error", "error": err or f"Agent {status}"}
                return
            _time.sleep(1.5)
        yield {"type": "error", "error": "Timeout waiting for response (>480s)"}
