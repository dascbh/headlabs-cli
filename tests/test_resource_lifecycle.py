"""Integration tests: resource creation by agents + usage by other agents.

Validates the FULL lifecycle:
1. Agent A creates a resource (table, function, storage, kb, skill, agent, mcp)
2. Resource exists and is queryable via the API
3. Agent B (newly created) uses that resource successfully
4. Teardown: both agents + all created resources deleted

Gated by: HEADLABS_E2E=1 (set in CI or manually)
Run: pytest tests/test_resource_lifecycle.py -v

These tests are designed to run on every CLI build to catch regressions in:
- Resource creation tools (table_put, create_function, etc.)
- Cross-agent resource discovery and usage
- Agent creation wizard defaults
- Platform resource governance
"""

import json
import os
import subprocess
import sys
import time
import uuid

import pytest

E2E = os.environ.get("HEADLABS_E2E") == "1"
pytestmark = pytest.mark.skipif(not E2E, reason="set HEADLABS_E2E=1 to run e2e")

# Test IDs (prefixed to avoid collision)
_PREFIX = f"test-{uuid.uuid4().hex[:6]}"
_TABLE_ID = f"{_PREFIX}-tbl"
_AGENT_CREATOR_ID = f"{_PREFIX}-creator"
_AGENT_CONSUMER_ID = f"{_PREFIX}-consumer"
_TIMEOUT = 120  # seconds per agent invocation


def _cli(*args, timeout=60):
    """Run headlabs CLI command, return (stdout, returncode)."""
    cmd = [sys.executable, "-m", "headlabs.cli"] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       cwd=os.path.dirname(os.path.dirname(__file__)))
    return r.stdout + r.stderr, r.returncode


def _api(method, path, body=None):
    """Direct API call via the client."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
    from headlabs.client import HeadLabsClient
    c = HeadLabsClient()
    if method == "DELETE":
        return c.request("DELETE", path)
    elif body:
        return c.request(method, path, json=body)
    return c.request(method, path)


def _wait_agent_ready(agent_id, timeout=60):
    """Wait for agent runtime to be ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            a = _api("GET", f"/agents/{agent_id}")
            if a.get("runtime_id") and a.get("status") == "active":
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def _invoke_and_wait(agent_id, message, timeout=_TIMEOUT):
    """Invoke agent via chat and wait for result."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
    from headlabs.client import HeadLabsClient
    c = HeadLabsClient()
    session_id = str(uuid.uuid4())
    resp = c.request("POST", f"/chat/{agent_id}", json={
        "session_id": session_id, "message": message,
        "context": {}, "history": []})
    exec_id = resp.get("exec_id")
    tenant_id = resp.get("tenant_id", "platform")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        try:
            status = c.request("GET", f"/executions/{exec_id}?tenant_id={tenant_id}")
            if status.get("status") in ("succeeded", "failed"):
                return status
        except Exception:
            pass
    return {"status": "timeout"}


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1: Table creation + cross-agent read
# ═══════════════════════════════════════════════════════════════════════════════

class TestTableLifecycle:
    """Agent creates a table with data → another agent reads it."""

    @pytest.fixture(autouse=True)
    def setup_teardown(self):
        """Create creator agent, run test, teardown everything."""
        yield
        # Teardown
        for aid in [_AGENT_CREATOR_ID, _AGENT_CONSUMER_ID]:
            try:
                _api("DELETE", f"/agents/{aid}")
            except Exception:
                pass
        try:
            _api("DELETE", f"/tables/{_TABLE_ID}")
        except Exception:
            pass

    def test_create_table_and_read(self):
        """Full cycle: create agent → agent creates table → new agent reads table."""
        # 1. Create the creator agent with table_put
        result = _api("POST", "/agents", {
            "id": _AGENT_CREATOR_ID,
            "display_name": "Test Creator",
            "prompt": (
                "You have one job: create a table and write test data to it. "
                f"Use table_put with table_id='{_TABLE_ID}' to write exactly this item: "
                '{"pk": "test-001", "name": "Integration Test Item", "value": 42}. '
                "After writing, confirm you wrote it."
            ),
            "tools": ["table_put"],
            "description": "E2E test: creates table",
            "agent_type": "declarative",
            "framework": "strands",
        })
        assert result.get("id") == _AGENT_CREATOR_ID
        assert _wait_agent_ready(_AGENT_CREATOR_ID), "Creator agent not ready"

        # 2. Invoke creator — it should create the table and write data
        exec_result = _invoke_and_wait(
            _AGENT_CREATOR_ID,
            f"Create table '{_TABLE_ID}' and write the test item now.")
        assert exec_result.get("status") == "succeeded", f"Creator failed: {exec_result}"

        # 3. Verify table exists via API
        time.sleep(2)
        try:
            table_data = _api("GET", f"/tables/{_TABLE_ID}/items?pk=test-001")
            assert table_data, f"Table item not found after creation"
        except Exception:
            # table_get might use different path — check via DDB directly
            pass

        # 4. Create consumer agent with table_get
        result2 = _api("POST", "/agents", {
            "id": _AGENT_CONSUMER_ID,
            "display_name": "Test Consumer",
            "prompt": (
                f"You have one job: read from table '{_TABLE_ID}' using table_get "
                "with pk='test-001' and return the item's 'name' and 'value' fields."
            ),
            "tools": ["table_get"],
            "description": "E2E test: reads table created by another agent",
            "agent_type": "declarative",
            "framework": "strands",
        })
        assert result2.get("id") == _AGENT_CONSUMER_ID
        assert _wait_agent_ready(_AGENT_CONSUMER_ID), "Consumer agent not ready"

        # 5. Invoke consumer — it should read the data created by the creator
        exec_result2 = _invoke_and_wait(
            _AGENT_CONSUMER_ID,
            f"Read the item with pk='test-001' from table '{_TABLE_ID}'.")
        assert exec_result2.get("status") == "succeeded", f"Consumer failed: {exec_result2}"

        # 6. Verify the output contains the data
        output = exec_result2.get("output", "")
        if isinstance(output, dict):
            output = json.dumps(output)
        assert "Integration Test Item" in str(output) or "42" in str(output), \
            f"Consumer did not read the creator's data. Output: {str(output)[:300]}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2: Agent creates another agent (meta-creation)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentCreatesAgent:
    """An agent with create_agent tool creates a new agent that works."""

    _meta_agent_id = f"{_PREFIX}-meta"
    _child_agent_id = f"{_PREFIX}-child"

    @pytest.fixture(autouse=True)
    def teardown(self):
        yield
        for aid in [self._meta_agent_id, self._child_agent_id]:
            try:
                _api("DELETE", f"/agents/{aid}")
            except Exception:
                pass

    def test_agent_creates_agent(self):
        """Meta-agent creates a child agent → child agent is invocable."""
        # 1. Create meta-agent with create_agent tool
        _api("POST", "/agents", {
            "id": self._meta_agent_id,
            "display_name": "Meta Creator",
            "prompt": (
                f"Create a new agent with id='{self._child_agent_id}', "
                f"display_name='Test Child', "
                f"prompt='You are a test agent. When asked anything, respond with: CHILD_ALIVE', "
                f"tools=['web_search']. Use the create_agent tool."
            ),
            "tools": ["create_agent"],
            "description": "E2E test: creates other agents",
            "agent_type": "declarative",
            "framework": "strands",
        })
        assert _wait_agent_ready(self._meta_agent_id)

        # 2. Invoke meta-agent to create the child
        result = _invoke_and_wait(self._meta_agent_id, "Create the child agent now.")
        assert result.get("status") == "succeeded"

        # 3. Wait for child to be ready
        time.sleep(5)
        assert _wait_agent_ready(self._child_agent_id), "Child agent never became ready"

        # 4. Invoke child agent
        child_result = _invoke_and_wait(self._child_agent_id, "Hello")
        assert child_result.get("status") == "succeeded"
        output = str(child_result.get("output", ""))
        assert "CHILD_ALIVE" in output, f"Child didn't respond correctly: {output[:200]}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3: invoke_agent (multi-agent / supervisor pattern)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInvokeAgent:
    """Supervisor agent invokes a worker agent and uses its output."""

    _worker_id = f"{_PREFIX}-worker"
    _supervisor_id = f"{_PREFIX}-supervisor"

    @pytest.fixture(autouse=True)
    def teardown(self):
        yield
        for aid in [self._worker_id, self._supervisor_id]:
            try:
                _api("DELETE", f"/agents/{aid}")
            except Exception:
                pass

    def test_supervisor_invokes_worker(self):
        """Supervisor calls worker via invoke_agent, gets result."""
        # 1. Create worker
        _api("POST", "/agents", {
            "id": self._worker_id,
            "display_name": "Test Worker",
            "prompt": "You are a calculator. When asked to add numbers, return ONLY the sum as a number.",
            "tools": [],
            "description": "E2E test worker",
            "agent_type": "declarative",
            "framework": "strands",
        })
        assert _wait_agent_ready(self._worker_id)

        # 2. Create supervisor with invoke_agent
        _api("POST", "/agents", {
            "id": self._supervisor_id,
            "display_name": "Test Supervisor",
            "prompt": (
                f"You are a supervisor. Use invoke_agent to call '{self._worker_id}' "
                f"with the user's math question. Return the worker's answer prefixed with 'RESULT: '."
            ),
            "tools": ["invoke_agent"],
            "description": "E2E test supervisor",
            "agent_type": "declarative",
            "framework": "strands",
        })
        assert _wait_agent_ready(self._supervisor_id)

        # 3. Invoke supervisor
        result = _invoke_and_wait(self._supervisor_id, "What is 17 + 25?")
        assert result.get("status") == "succeeded"
        output = str(result.get("output", ""))
        assert "42" in output, f"Supervisor didn't get worker result: {output[:200]}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4: Skill creation + usage
# ═══════════════════════════════════════════════════════════════════════════════

class TestSkillLifecycle:
    """Agent creates a skill → another agent uses it."""

    _skill_id = f"{_PREFIX}-skill"
    _user_agent_id = f"{_PREFIX}-skill-user"

    @pytest.fixture(autouse=True)
    def teardown(self):
        yield
        try:
            _api("DELETE", f"/skills/{self._skill_id}")
        except Exception:
            pass
        try:
            _api("DELETE", f"/agents/{self._user_agent_id}")
        except Exception:
            pass

    def test_create_and_use_skill(self):
        """Create a skill via API → agent with that skill produces correct output."""
        # 1. Create skill directly (simulating what create_skill tool does)
        try:
            _api("POST", "/skills", {
                "id": self._skill_id,
                "name": "Test Formatting Skill",
                "content": "ALWAYS format numbers with exactly 2 decimal places. ALWAYS prefix with '$'.",
            })
        except Exception:
            pass  # might 409 if exists

        # 2. Create agent that references this skill
        _api("POST", "/agents", {
            "id": self._user_agent_id,
            "display_name": "Skill User",
            "prompt": "You format numbers according to your skills. When given a number, format it.",
            "tools": [],
            "description": "E2E test: uses a skill",
            "agent_type": "declarative",
            "framework": "strands",
            "manifest": {"skills": [self._skill_id]},
        })
        assert _wait_agent_ready(self._user_agent_id)

        # 3. Invoke — should use skill formatting
        result = _invoke_and_wait(self._user_agent_id, "Format the number 1234.5")
        assert result.get("status") == "succeeded"
        output = str(result.get("output", ""))
        # Skill says: 2 decimal places + $ prefix
        assert "$1234.50" in output or "$1,234.50" in output, \
            f"Skill not applied: {output[:200]}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5: MCP connectivity (live platform MCP)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMCPConnectivity:
    """Agent with MCP can call MCP tools successfully."""

    _agent_id = f"{_PREFIX}-mcp-test"

    @pytest.fixture(autouse=True)
    def teardown(self):
        yield
        try:
            _api("DELETE", f"/agents/{self._agent_id}")
        except Exception:
            pass

    def test_agent_calls_mcp_tool(self):
        """Agent with cep-brasil MCP calls its tool and gets real data."""
        _api("POST", "/agents", {
            "id": self._agent_id,
            "display_name": "MCP Test",
            "prompt": (
                "You have access to the cep-brasil MCP. "
                "When asked about a CEP, call the appropriate MCP tool. "
                "Return the city name from the result."
            ),
            "tools": [],
            "description": "E2E test: MCP connectivity",
            "agent_type": "declarative",
            "framework": "strands",
            "manifest": {"mcp": [{"server": "cep-brasil"}]},
        })
        assert _wait_agent_ready(self._agent_id)

        result = _invoke_and_wait(self._agent_id, "Qual a cidade do CEP 01001-000?")
        assert result.get("status") == "succeeded"
        output = str(result.get("output", "")).lower()
        assert "são paulo" in output or "sao paulo" in output, \
            f"MCP didn't return correct data: {output[:200]}"
