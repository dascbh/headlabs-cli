"""Tests for spec-driven creation (`agents create --spec` / `mcps create --spec`).

All network/LLM interaction is mocked: a FakeClient stands in for
HeadLabsClient and yields a canned architect draft from ``chat_stream``. No
docker, no platform calls, no interactive input.
"""

import io
import json
import os
from types import SimpleNamespace

import pytest

import headlabs.cli as cli


# ── Fake client ───────────────────────────────────────────────────────────────

class FakeClient:
    """Stand-in HeadLabsClient. ``answers`` is either a single architect answer
    (used for every turn) or a list consumed one-per-turn (the last repeats)."""

    def __init__(self, answers):
        self._answers = answers if isinstance(answers, list) else [answers]
        self._turn = 0
        self.created = []          # create_agent calls
        self.mcps_created = []     # create_mcp calls
        self.requests = []         # generic request() calls
        self.chat_messages = []    # architect prompts seen
        self.chat_agent_ids = []   # agent_id seen by chat_stream, per turn
        self.remote_agents = [{"id": "helper-agent"}, {"id": "agent-architect"}]
        self.existing_agent_ids = set()   # agents that answer GET /agents/<id> with 200
        self.existing_mcp_ids = set()     # mcps that answer GET /mcps/<id> with 200
        self.list_remote_agents_fails = False
        self.create_agent_fails = False
        self.create_mcp_fails = False
        self.deploy_runtime_id = "rt-fake-123"   # simulates a confirmed active runtime

    # architect turn
    def chat_stream(self, agent_id, session_id, message, context=None,
                    history=None, tenant_id=None, approval_handler=None):
        self.chat_agent_ids.append(agent_id)
        self.chat_messages.append(message)
        idx = min(self._turn, len(self._answers) - 1)
        self._turn += 1
        yield {"type": "done", "message": self._answers[idx]}

    def request(self, method, path, *, json=None, params=None, timeout=30):
        self.requests.append({"method": method, "path": path, "json": json})
        if method == "GET" and path == "/mcps":
            return [{"id": "slack"}, {"id": "github"}]
        if method == "GET" and path.startswith("/agents/"):
            agent_id = path.split("/agents/", 1)[1]
            if agent_id in self.existing_agent_ids:
                return {"id": agent_id, "status": "active", "visibility": "private"}
            raise RuntimeError("404 agent not found")
        if method == "GET" and path.startswith("/mcps/"):
            mcp_id = path.split("/mcps/", 1)[1]
            if mcp_id in self.existing_mcp_ids:
                return {"id": mcp_id, "kind": "mcp"}
            raise RuntimeError("404 mcp not found")
        if method == "POST" and path == "/mcps":
            if self.create_mcp_fails:
                raise RuntimeError("create_mcp failed")
            self.mcps_created.append(json)
            return {"id": json.get("id"), "kind": "mcp"}
        if method == "POST" and path.endswith("/deploy"):
            return {"runtime_id": self.deploy_runtime_id} if self.deploy_runtime_id else {}
        return {}

    def list_remote_agents(self):
        if self.list_remote_agents_fails:
            raise RuntimeError("platform unreachable")
        return list(self.remote_agents)

    def resolve_tenant(self, refresh=False):
        return "tenant-x"

    def create_agent(self, agent_id, display_name, prompt, tools=None,
                     description="", model=None):
        if self.create_agent_fails:
            raise RuntimeError("create_agent failed")
        self.created.append({
            "agent_id": agent_id, "display_name": display_name,
            "prompt": prompt, "tools": tools, "description": description,
        })
        return {"status": "created", "runtime_id": "rt-1"}

    def create_mcp(self, mcp_id, display_name, description="", framework="container"):
        return self.request("POST", "/mcps", json={
            "id": mcp_id, "display_name": display_name,
            "description": description, "framework": framework,
        })

    def publish_mcp(self, mcp_id):
        self.requests.append({"method": "POST", "path": f"/mcps/{mcp_id}/publish", "json": None})
        return {"id": mcp_id, "visibility": "public"}

    def unpublish_mcp(self, mcp_id):
        self.requests.append({"method": "POST", "path": f"/mcps/{mcp_id}/unpublish", "json": None})
        return {"id": mcp_id, "visibility": "private"}


def _install_fake(monkeypatch, draft_obj):
    """Patch HeadLabsClient (imported locally inside cli functions) + silence
    tenant lookups, and capture stdout. ``draft_obj`` may be a dict (serialized
    to JSON), a raw string, or a list of such (one per architect turn)."""
    def _ser(x):
        return x if isinstance(x, str) else json.dumps(x)
    answers = [_ser(x) for x in draft_obj] if isinstance(draft_obj, list) else _ser(draft_obj)
    fake = FakeClient(answers)
    monkeypatch.setattr("headlabs.client.HeadLabsClient", lambda *a, **k: fake)
    monkeypatch.setattr("headlabs.config.get_tenant", lambda: None)
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    return fake, buf


def _mcp_answer(design: dict, code: str) -> str:
    """Build a two-block architect answer: ```json design + ```python code."""
    return ("```json\n" + json.dumps(design) + "\n```\n\n"
            "```python\n" + code + "\n```\n")


# ── _read_spec ────────────────────────────────────────────────────────────────

def test_read_spec_reads_and_strips(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text("  a Slack MCP that posts messages  \n")
    assert cli._read_spec(str(p)) == "a Slack MCP that posts messages"


def test_read_spec_missing_exits(tmp_path):
    with pytest.raises(SystemExit):
        cli._read_spec(str(tmp_path / "nope.md"))


def test_read_spec_empty_exits(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("   \n")
    with pytest.raises(SystemExit):
        cli._read_spec(str(p))


# ── _parse_json_draft ─────────────────────────────────────────────────────────

def test_parse_json_draft_plain():
    assert cli._parse_json_draft('{"a": 1}') == {"a": 1}


def test_parse_json_draft_fenced_with_prose():
    ans = 'Sure!\n```json\n{"id": "x", "nested": {"k": 2}}\n```\nDone.'
    assert cli._parse_json_draft(ans) == {"id": "x", "nested": {"k": 2}}


def test_parse_json_draft_garbage_returns_none():
    assert cli._parse_json_draft("no json here") is None
    assert cli._parse_json_draft("") is None


def test_parse_json_draft_handles_braces_inside_string_values():
    # Regression: a naive {/} depth counter miscounts braces embedded in a
    # string value (Python code with dicts/f-strings/docstrings) and truncates
    # the object. This draft has a '}' appearing *before* a '{' inside the
    # server_code docstring — the exact shape that broke `mcps create --spec`.
    draft = {
        "id": "mcp-cclasstrib",
        "name": "MCP cClassTrib",
        "requirements": ["cryptography"],
        "tools": ["consultar_classificacao_tributaria", "detalhar_cclasstrib"],
        "server_code": (
            'from mcp.server.fastmcp import FastMCP\n'
            'mcp = FastMCP("cclasstrib")\n\n'
            '@mcp.tool()\n'
            'def detalhar_cclasstrib(code: str) -> dict:\n'
            '    """Fecha }chave antes de abrir { — quebra contagem ingênua."""\n'
            '    payload = {"code": code, "fmt": f"{code}-x"}\n'
            '    return {"ok": True, "payload": payload}\n\n'
            'app = mcp.streamable_http_app()\n'
        ),
    }
    raw = "```json\n" + json.dumps(draft, ensure_ascii=False) + "\n```"
    assert cli._parse_json_draft(raw) == draft


def test_parse_json_draft_skips_prose_with_stray_brace():
    ans = 'Aqui está o objeto { veja } abaixo:\n{"id": "x", "n": 1}'
    assert cli._parse_json_draft(ans) == {"id": "x", "n": 1}


def test_parse_json_draft_repairs_invalid_backslash_escape():
    # Reproduces the real failure: a string value with an invalid JSON escape
    # (here '\T' from a Windows path). Strict json.loads rejects it; the
    # lenient repair doubles the stray backslash so it parses.
    raw = '{"id": "x", "note": "win path C:\\Temp"}'   # single backslash before T
    assert cli._parse_json_draft(raw) == {"id": "x", "note": "win path C:\\Temp"}


# ── _extract_fenced_block ─────────────────────────────────────────────────────

def test_extract_fenced_block_python():
    ans = 'meta\n```json\n{"id": "x"}\n```\n```python\nprint("hi")\nx = {"a": 1}\n```\n'
    assert cli._extract_fenced_block(ans, ("python", "py")) == 'print("hi")\nx = {"a": 1}'


def test_extract_fenced_block_unclosed():
    ans = '```python\nprint(1)\nmore = 2\n'   # model forgot the closing fence
    assert cli._extract_fenced_block(ans, ("python",)) == 'print(1)\nmore = 2'


def test_extract_fenced_block_absent_returns_none():
    assert cli._extract_fenced_block('{"id": "x"}', ("python",)) is None


# ── agents create --spec (with approval gate) ─────────────────────────────────

def _answer(monkeypatch, value):
    """Feed a single canned answer to the next input() prompt."""
    monkeypatch.setattr("builtins.input", lambda *a: value)


def test_agents_create_spec_creates_after_approval(monkeypatch, tmp_path):
    draft = {
        "type": "single", "id": "quarterly-analyst",
        "name": "Quarterly Analyst", "description": "Analyzes quarterly data",
        "tools_native": ["web_search", "table_get"],
        "mcps": ["slack"], "workers": [],
        "prompt": "You are a financial analyst...",
    }
    fake, buf = _install_fake(monkeypatch, draft)
    fake.existing_agent_ids = {"agent-designer"}   # dedicated designer already provisioned
    _answer(monkeypatch, "y")   # approve the gate

    spec = tmp_path / "agent-spec.md"
    spec.write_text("Quero um agente que analisa dados trimestrais.")

    args = SimpleNamespace(intent=None, spec=str(spec), id=None, name=None,
                           prompt=None, prompt_file=None, tools=None,
                           description=None, model=None, tenant=None,
                           verbose=False, subcmd="create")
    cli.cmd_agents_create(args)

    assert len(fake.created) == 1
    created = fake.created[0]
    assert created["agent_id"] == "quarterly-analyst"
    assert created["display_name"] == "Quarterly Analyst"
    assert created["tools"] == ["web_search", "table_get"]
    # MCP association is PATCHed onto the agent manifest
    patch = next(r for r in fake.requests if r["method"] == "PATCH")
    assert patch["path"] == "/agents/quarterly-analyst"
    assert patch["json"]["manifest"]["mcp"] == [{"server": "slack"}]
    out = buf.getvalue()
    assert "Análise concluída" in out               # full review shown
    assert "You are a financial analyst..." in out  # full prompt shown
    assert "✓ Agent 'quarterly-analyst' created" in out


def test_agents_create_spec_declined_creates_nothing(monkeypatch, tmp_path):
    draft = {
        "type": "single", "id": "quarterly-analyst", "name": "QA",
        "description": "d", "tools_native": [], "mcps": [], "workers": [],
        "prompt": "p",
    }
    fake, buf = _install_fake(monkeypatch, draft)
    fake.existing_agent_ids = {"agent-designer"}
    _answer(monkeypatch, "n")   # decline the gate

    spec = tmp_path / "s.md"
    spec.write_text("algo")
    args = SimpleNamespace(intent=None, spec=str(spec), id=None, name=None,
                           prompt=None, prompt_file=None, tools=None,
                           description=None, model=None, tenant=None,
                           verbose=False, subcmd="create")
    cli.cmd_agents_create(args)

    assert fake.created == []
    assert not any(r["method"] == "PATCH" for r in fake.requests)
    assert "cancelada" in buf.getvalue().lower()


def test_agents_create_spec_non_interactive_fails_safe(monkeypatch, tmp_path):
    # No stdin (EOFError) → gate defaults to no → nothing is created.
    draft = {"type": "single", "id": "x", "name": "X", "description": "d",
             "tools_native": [], "mcps": [], "workers": [], "prompt": "p"}
    fake, _ = _install_fake(monkeypatch, draft)
    fake.existing_agent_ids = {"agent-designer"}

    def _eof(*a):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)

    spec = tmp_path / "s.md"
    spec.write_text("algo")
    args = SimpleNamespace(intent=None, spec=str(spec), id=None, name=None,
                           prompt=None, prompt_file=None, tools=None,
                           description=None, model=None, tenant=None,
                           verbose=False, subcmd="create")
    cli.cmd_agents_create(args)

    assert fake.created == []


def test_agents_create_spec_supervisor_gets_invoke_agent(monkeypatch, tmp_path):
    draft = {
        "type": "supervisor", "id": "orchestrator", "name": "Orch",
        "description": "coordinates", "tools_native": [],
        "mcps": [], "workers": ["helper-agent"], "prompt": "coordinate.",
    }
    fake, _ = _install_fake(monkeypatch, draft)
    fake.existing_agent_ids = {"agent-designer"}
    _answer(monkeypatch, "y")

    spec = tmp_path / "s.md"
    spec.write_text("um supervisor que coordena workers")
    args = SimpleNamespace(intent=None, spec=str(spec), id=None, name=None,
                           prompt=None, prompt_file=None, tools=None,
                           description=None, model=None, tenant=None,
                           verbose=False, subcmd="create")
    cli.cmd_agents_create(args)

    assert fake.created[0]["tools"] == ["invoke_agent"]


# ── agent-designer: dedicated agent + fallback ────────────────────────────────
#
# Mirrors the mcp-architect tests below exactly. Regression coverage for the
# 2026-07-05 bug: the generic agent-architect's own platform persona tells it
# to research AND create directly via its own create_agent tool — sending it
# an ad-hoc "return ONLY this JSON" prompt (the old pattern) conflicts with
# that persona, and reproduced twice against the real platform as a
# conversational greeting instead of either a design or a created agent. The
# fix is a dedicated, tool-less (design-only) agent-designer, exactly like
# mcp-architect for the MCP pipeline.

def test_ensure_agent_designer_returns_true_when_already_exists(monkeypatch):
    fake, _ = _install_fake(monkeypatch, "{}")
    fake.existing_agent_ids = {"agent-designer"}
    assert cli._ensure_agent_designer(fake) is True
    assert fake.created == []              # already exists — no create_agent call


def test_ensure_agent_designer_creates_when_absent(monkeypatch):
    fake, _ = _install_fake(monkeypatch, "{}")
    fake.existing_agent_ids = set()
    assert cli._ensure_agent_designer(fake) is True
    assert fake.created[0]["agent_id"] == "agent-designer"
    assert "Agent Designer" in fake.created[0]["prompt"]     # persona embedded
    assert "create_agent" not in fake.created[0]["tools"]    # design-only, no creation tool


def test_ensure_agent_designer_false_when_create_fails(monkeypatch):
    fake, _ = _install_fake(monkeypatch, "{}")
    fake.existing_agent_ids = set()
    fake.create_agent_fails = True
    assert cli._ensure_agent_designer(fake) is False


def test_agents_create_spec_uses_dedicated_designer_when_available(monkeypatch, tmp_path):
    draft = {"type": "single", "id": "x", "name": "X", "description": "d",
             "tools_native": [], "mcps": [], "workers": [], "prompt": "p"}
    fake, buf = _install_fake(monkeypatch, draft)
    fake.existing_agent_ids = {"agent-designer"}   # already provisioned
    _answer(monkeypatch, "y")

    spec = tmp_path / "s.md"
    spec.write_text("algo")
    args = SimpleNamespace(intent=None, spec=str(spec), id=None, name=None,
                           prompt=None, prompt_file=None, tools=None,
                           description=None, model=None, tenant=None,
                           verbose=False, subcmd="create")
    cli.cmd_agents_create(args)

    assert fake.chat_agent_ids == ["agent-designer"]
    assert "agent-designer indisponível" not in buf.getvalue()   # no fallback warning


def test_agents_create_spec_falls_back_to_generic_architect_when_designer_unavailable(monkeypatch, tmp_path):
    draft = {"type": "single", "id": "x", "name": "X", "description": "d",
             "tools_native": [], "mcps": [], "workers": [], "prompt": "p"}
    fake, buf = _install_fake(monkeypatch, draft)
    fake.existing_agent_ids = set()
    fake.create_agent_fails = True   # _ensure_agent_designer's create attempt fails too
    _answer(monkeypatch, "y")

    spec = tmp_path / "s.md"
    spec.write_text("algo")
    args = SimpleNamespace(intent=None, spec=str(spec), id=None, name=None,
                           prompt=None, prompt_file=None, tools=None,
                           description=None, model=None, tenant=None,
                           verbose=False, subcmd="create")
    # create_agent_fails also blocks the REAL agent creation at the end —
    # this test only asserts the fallback routing + warning, not a full
    # successful creation.
    cli.cmd_agents_create(args)

    assert fake.chat_agent_ids == ["agent-architect"]
    assert "agent-designer indisponível" in buf.getvalue()


# ── mcps create --spec (design + code pipeline) ───────────────────────────────

@pytest.fixture(autouse=True)
def _no_real_subprocess_verification(monkeypatch, request):
    """`_mcps_create` runs the generated server as a real subprocess to verify
    it speaks MCP. Unit tests must not pay that cost (or depend on the `mcp`
    package's runtime behavior) — stub it to a canned success unless a test
    opts out via the `real_verify` marker."""
    if "real_verify" in request.keywords:
        yield
        return
    monkeypatch.setattr(cli, "_verify_mcp_behavior",
                        lambda mcp_dir, **kw: {"ok": True, "tools": ["classify"], "warnings": []})
    yield


_SERVER_CODE = (
    'from mcp.server.fastmcp import FastMCP\n'
    'mcp = FastMCP("cclasstrib", host="0.0.0.0", stateless_http=True)\n\n'
    '@mcp.tool()\n'
    'def classify(text: str) -> str:\n'
    '    """Classify a fiscal document."""\n'
    '    return "ok"\n\n'
    'app = mcp.streamable_http_app()\n\n'
    'if __name__ == "__main__":\n'
    '    import os\n'
    '    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))\n'
)

_DESIGN = {
    "id": "cclasstrib", "name": "CClass Trib",
    "description": "Classifica tributação",
    "dependencies": ["cryptography"],
    "config": [{"env": "CFF_CERT_PATH", "secret": True, "required": True,
                "description": "caminho do certificado"}],
    "auth": {"type": "mtls"},
    "tools": [{"name": "classify", "description": "classifica doc",
               "params": [{"name": "text", "type": "str", "required": True}]}],
}


def test_mcps_create_spec_scaffolds_and_deploys(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")   # approve the gate
    pushed = {}
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pushed.update(mcp_id=args.mcp_id))

    spec = tmp_path / "spec-mcp-cclasstrib.md"
    spec.write_text("Um MCP que classifica documentos fiscais brasileiros.")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    mcp_dir = tmp_path / "mcps" / "cclasstrib"
    assert (mcp_dir / "server.py").read_text() == _SERVER_CODE   # code from ```python
    reqs = (mcp_dir / "requirements.txt").read_text()
    assert "mcp>=1.2.0" in reqs and "cryptography" in reqs        # deps from design
    env = (mcp_dir / ".env.example").read_text()                 # config → .env.example
    assert "CFF_CERT_PATH" in env
    assert fake._turn == 1                                        # 1 turn (happy path)
    assert pushed == {"mcp_id": "cclasstrib"}
    out = buf.getvalue()
    assert "Análise concluída" in out and "Auth:" in out and "server.py (" in out
    assert "MCP scaffolded" in out


def test_mcps_create_spec_removes_stale_local_dir_on_confirm(monkeypatch, tmp_path):
    # Regression: a leftover ./mcps/<id>/ from a previous/aborted attempt
    # must not force the user to redo the (slow) architect turn — offer to
    # remove it (confirmed) and continue with the already-approved design.
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")   # both confirms answered "y": approve gate, then remove stale dir
    pushed = {}
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pushed.update(mcp_id=args.mcp_id))

    stale_dir = tmp_path / "mcps" / "cclasstrib"
    stale_dir.mkdir(parents=True)
    (stale_dir / "server.py").write_text("# stale leftover from a previous run\n")

    spec = tmp_path / "spec-mcp-cclasstrib.md"
    spec.write_text("Um MCP que classifica documentos fiscais brasileiros.")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    mcp_dir = tmp_path / "mcps" / "cclasstrib"
    assert (mcp_dir / "server.py").read_text() == _SERVER_CODE   # stale content replaced
    assert pushed == {"mcp_id": "cclasstrib"}
    out = buf.getvalue()
    # NOTE: the "já existe localmente" text is the input() PROMPT itself
    # (passed as input()'s first arg, not printed via sys.stdout), so it is
    # not visible in the captured buffer here — the observable contract is
    # the behavior: stale dir got replaced and the pipeline continued.
    assert "MCP scaffolded" in out


def test_mcps_create_spec_keeps_stale_local_dir_on_decline(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")   # gate approved once — but the loop below
    # forces the SECOND confirm (remove stale dir) to answer "n" while the
    # first (gate approval) still answers "y", by swapping input() mid-run.
    calls = {"n": 0}
    def _input_seq(prompt):
        calls["n"] += 1
        return "y" if calls["n"] == 1 else "n"
    monkeypatch.setattr("builtins.input", _input_seq)
    pushed = {}
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pushed.update(mcp_id=args.mcp_id))

    stale_dir = tmp_path / "mcps" / "cclasstrib"
    stale_dir.mkdir(parents=True)
    (stale_dir / "server.py").write_text("# stale leftover from a previous run\n")

    spec = tmp_path / "spec-mcp-cclasstrib.md"
    spec.write_text("Um MCP que classifica documentos fiscais brasileiros.")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    # Untouched: still the stale content, nothing pushed.
    assert (stale_dir / "server.py").read_text() == "# stale leftover from a previous run\n"
    assert pushed == {}
    out = buf.getvalue()
    assert "Cancelado" in out


def test_mcps_create_spec_declined_creates_nothing(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "n")   # decline the gate
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pytest.fail("declined must not push"))

    spec = tmp_path / "spec.md"
    spec.write_text("um serviço")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert not (tmp_path / "mcps").exists()     # nothing scaffolded
    assert "cancelada" in buf.getvalue().lower()


def test_mcps_create_spec_auto_repairs_invalid_code(monkeypatch, tmp_path):
    # Turn 1 returns syntactically broken code → validation fails → the repair
    # turn returns valid code → the run recovers without human intervention.
    bad = 'from mcp.server.fastmcp import FastMCP\nmcp = FastMCP("x")\ndef broken(\n'
    answers = [_mcp_answer(_DESIGN, bad), "```python\n" + _SERVER_CODE + "\n```"]
    fake, buf = _install_fake(monkeypatch, answers)
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    pushed = {}
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pushed.update(mcp_id=args.mcp_id))

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert fake._turn == 2                       # design turn + 1 repair turn
    assert "Ajustando" in buf.getvalue()
    assert pushed == {"mcp_id": "cclasstrib"}
    assert (tmp_path / "mcps" / "cclasstrib" / "server.py").read_text() == _SERVER_CODE


def test_mcps_create_spec_gives_up_after_repairs(monkeypatch, tmp_path):
    # Persistently broken code that never changes → the convergence guard stops
    # after the first repair returns identical code, scaffolds nothing, no push.
    bad = 'def broken(\n'
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, bad))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pytest.fail("invalid code must not push"))

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert not (tmp_path / "mcps").exists()
    assert "validação" in buf.getvalue()
    assert fake._turn == 2                        # 1 design + 1 repair (then converged)


def test_mcps_create_spec_behavioral_crash_blocks_and_triggers_repair(monkeypatch, tmp_path):
    # Real-world regression: code passes AST validation (right substrings) but
    # CRASHES on startup (e.g. `from mcp import FastMCP` instead of the
    # correct submodule path). This must be treated exactly like an AST
    # failure — blocking, with auto-repair — never silently deployed.
    calls = {"n": 0}

    def fake_verify(mcp_dir, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "skipped": False,
                    "error": "server.py falhou ao iniciar: ImportError: cannot import name 'FastMCP'"}
        return {"ok": True, "tools": ["classify"], "warnings": []}

    monkeypatch.setattr(cli, "_verify_mcp_behavior", fake_verify, raising=True)
    answers = [_mcp_answer(_DESIGN, _SERVER_CODE), "```python\n" + _SERVER_CODE + "\n```"]
    fake, buf = _install_fake(monkeypatch, answers)
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    pushed = {}
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pushed.update(mcp_id=args.mcp_id))

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert fake._turn == 2                          # design turn + 1 repair turn
    assert "Ajustando" in buf.getvalue()
    assert "falhou ao iniciar" in buf.getvalue()
    assert pushed == {"mcp_id": "cclasstrib"}        # recovered and proceeded


def test_mcps_create_spec_behavioral_crash_gives_up_after_repairs(monkeypatch, tmp_path):
    # The behavioral crash never gets fixed → must abort, never push, never
    # silently accept a server that doesn't run.
    def fake_verify(mcp_dir, **kw):
        return {"ok": False, "skipped": False, "error": "ImportError: cannot import name 'FastMCP'"}

    monkeypatch.setattr(cli, "_verify_mcp_behavior", fake_verify, raising=True)
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pytest.fail("must not push a server that crashes on startup"))

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert not (tmp_path / "mcps").exists()
    assert "não passou na validação" in buf.getvalue()


def test_mcps_create_spec_infra_unavailable_does_not_block(monkeypatch, tmp_path):
    # Behavioral check can't run (e.g. no free port, missing runtime dep in the
    # user's environment) → skipped=True → must NOT block creation, since we
    # can't attribute the failure to the generated code.
    def fake_verify(mcp_dir, **kw):
        return {"ok": False, "skipped": True, "error": "não foi possível iniciar o processo"}

    monkeypatch.setattr(cli, "_verify_mcp_behavior", fake_verify, raising=True)
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    pushed = {}
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pushed.update(mcp_id=args.mcp_id))

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert fake._turn == 1                            # no repair triggered
    assert "não pôde rodar" in buf.getvalue()
    assert pushed == {"mcp_id": "cclasstrib"}


def test_mcps_create_spec_sanitizes_backslash_decorator(monkeypatch, tmp_path):
    # The real-world failure: `@mcp.tool()` came back as `\@mcp.tool()` (a stray
    # backslash from JSON-escape repair). Deterministic sanitization must fix it
    # in a SINGLE turn — no repair round-trips.
    corrupted = _SERVER_CODE.replace("@mcp.tool()", "\\@mcp.tool()")
    assert "\\@mcp.tool()" in corrupted           # sanity: corruption present
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, corrupted))
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    pushed = {}
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pushed.update(mcp_id=args.mcp_id))

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert fake._turn == 1                         # fixed deterministically, no repair
    written = (tmp_path / "mcps" / "cclasstrib" / "server.py").read_text()
    assert "@mcp.tool()" in written and "\\@" not in written
    assert pushed == {"mcp_id": "cclasstrib"}


def test_mcps_create_spec_no_deploy_skips_push(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pytest.fail("--no-deploy must skip push"))

    spec = tmp_path / "spec.md"
    spec.write_text("um serviço qualquer")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=True, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert (tmp_path / "mcps" / "cclasstrib" / "server.py").exists()
    assert "headlabs mcps push cclasstrib" in buf.getvalue()


def test_mcps_create_spec_id_override(monkeypatch, tmp_path):
    fake, _ = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    monkeypatch.setattr(cli, "_mcps_push", lambda args: None)

    spec = tmp_path / "spec.md"
    spec.write_text("algo")
    args = SimpleNamespace(spec=str(spec), id="forced-id", profile=None,
                           message=None, tenant=None, no_deploy=False,
                           wait=False, mcps_cmd="create")
    cli._mcps_create(args)

    assert (tmp_path / "mcps" / "forced-id" / "server.py").exists()
    assert not (tmp_path / "mcps" / "cclasstrib").exists()


def test_mcps_create_spec_sanitizes_id_override(monkeypatch, tmp_path):
    fake, _ = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    pushed = {}
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pushed.update(mcp_id=args.mcp_id))

    spec = tmp_path / "spec.md"
    spec.write_text("algo")
    args = SimpleNamespace(spec=str(spec), id="Weird Id!!", profile=None,
                           message=None, tenant=None, no_deploy=False,
                           wait=False, mcps_cmd="create")
    cli._mcps_create(args)

    assert (tmp_path / "mcps" / "weird-id").exists()
    assert pushed == {"mcp_id": "weird-id"}


def test_mcps_create_spec_undesignable_aborts(monkeypatch, tmp_path):
    # Architect returns something with no usable design (no tools) → abort.
    fake, buf = _install_fake(monkeypatch, "desculpe, não entendi a spec")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pytest.fail("must not push without a design"))

    spec = tmp_path / "spec.md"
    spec.write_text("algo")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert not (tmp_path / "mcps").exists()
    assert "interpretar o design" in buf.getvalue()


# ── architect error handling ──────────────────────────────────────────────────

def test_mcps_create_spec_architect_error_aborts(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, {"unused": True})

    # Make chat_stream emit an error event instead of a draft.
    def erroring_stream(*a, **k):
        yield {"type": "error", "error": "model unavailable"}
    fake.chat_stream = erroring_stream
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_mcps_push",
                        lambda args: pytest.fail("must not push on architect error"))

    spec = tmp_path / "spec.md"
    spec.write_text("algo")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert not (tmp_path / "mcps").exists()
    assert "model unavailable" in buf.getvalue()


# ── mcp-architect: dedicated agent + fallback ─────────────────────────────────

def test_ensure_mcp_architect_returns_true_when_already_exists(monkeypatch):
    fake, _ = _install_fake(monkeypatch, "{}")
    fake.existing_agent_ids = {"mcp-architect"}
    assert cli._ensure_mcp_architect(fake) is True
    assert fake.created == []              # already exists — no create_agent call


def test_ensure_mcp_architect_creates_when_absent(monkeypatch):
    fake, _ = _install_fake(monkeypatch, "{}")
    fake.existing_agent_ids = set()
    assert cli._ensure_mcp_architect(fake) is True
    assert fake.created[0]["agent_id"] == "mcp-architect"
    assert "MCP Architect" in fake.created[0]["prompt"]     # persona embedded


def test_ensure_mcp_architect_false_when_create_fails(monkeypatch):
    fake, _ = _install_fake(monkeypatch, "{}")
    fake.existing_agent_ids = set()
    fake.create_agent_fails = True
    assert cli._ensure_mcp_architect(fake) is False


def test_mcps_create_uses_dedicated_architect_when_available(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    fake.existing_agent_ids = {"mcp-architect"}        # already provisioned
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    monkeypatch.setattr(cli, "_mcps_push", lambda args: None)

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert fake.chat_agent_ids == ["mcp-architect"]
    assert "mcp-architect indisponível" not in buf.getvalue()   # no fallback warning


def test_mcps_create_falls_back_to_generic_architect_when_unavailable(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    fake.existing_agent_ids = set()
    fake.create_agent_fails = True                      # provisioning fails
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    monkeypatch.setattr(cli, "_mcps_push", lambda args: None)

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    assert fake.chat_agent_ids == ["agent-architect"]
    assert "mcp-architect indisponível" in buf.getvalue()
    # generic fallback prompts must still carry the full authoring contract
    assert "MCP authoring contract" in fake.chat_messages[0]


def test_mcps_create_dedicated_architect_prompt_omits_repeated_knowledge(monkeypatch, tmp_path):
    fake, _ = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    fake.existing_agent_ids = {"mcp-architect"}
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")
    monkeypatch.setattr(cli, "_mcps_push", lambda args: None)

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    # the per-call prompt to the dedicated agent should NOT repeat the full
    # contract (it lives in the agent's own persona instead)
    assert "MCP authoring contract" not in fake.chat_messages[0]


# ── MCP design model / validation (unit) ──────────────────────────────────────

def test_normalize_mcp_design_coerces_loose_shapes():
    d = cli._normalize_mcp_design({
        "id": "svc", "requirements": "httpx, pandas",
        "auth": "apikey",
        "env": {"API_KEY": ""},
        "tools": ["ping", {"name": "run", "params": ["x", {"name": "n", "type": "int"}]}],
    })
    assert d["dependencies"] == ["httpx", "pandas"]     # string → list
    assert d["auth"] == {"type": "apikey"}              # string → dict
    assert d["config"][0]["env"] == "API_KEY"           # dict env → config list
    assert d["tools"][0] == {"name": "ping", "description": "", "params": [],
                             "returns": "", "logic": "",
                             "side_effects": False, "idempotent": True}
    assert [p["name"] for p in d["tools"][1]["params"]] == ["x", "n"]
    assert d["tools"][1]["params"][1]["type"] == "int"


def test_normalize_mcp_design_rejects_empty():
    assert cli._normalize_mcp_design({"id": "x"}) is None            # no tools
    assert cli._normalize_mcp_design({"tools": [{"name": "t"}]}) is None  # no id/name
    assert cli._normalize_mcp_design({"name": "X", "tools": [{"name": "t"}]})["name"] == "X"
    assert cli._normalize_mcp_design("not a dict") is None


def test_validate_server_code():
    ok, err = cli._validate_server_code(_SERVER_CODE)
    assert ok and err == ""
    ok, err = cli._validate_server_code("def broken(")
    assert not ok and "SyntaxError" in err
    ok, err = cli._validate_server_code("x = 1\n")     # parses but not an MCP
    assert not ok and "faltando" in err


def test_validate_server_code_rejects_wrong_import_path():
    # Real-world failure: `from mcp import FastMCP` parses fine and contains
    # the substring "FastMCP", but FastMCP is not exported from the top-level
    # `mcp` package — it raises ImportError at runtime. The wrong import must
    # be caught here, not only by the (much slower) behavioral check.
    bad = _SERVER_CODE.replace(
        "from mcp.server.fastmcp import FastMCP", "from mcp import FastMCP")
    ok, err = cli._validate_server_code(bad)
    assert not ok
    assert "import incorreto" in err


def test_sanitize_python_code_fixes_backslash_artifacts():
    # \@decorator → @decorator
    assert cli._sanitize_python_code("\\@mcp.tool()\ndef f(): ...") == \
        "@mcp.tool()\ndef f(): ..."
    # stray leading backslash on a line → dropped, indentation kept
    assert cli._sanitize_python_code("    \\x = 1") == "    x = 1"
    # a legitimate end-of-line continuation is left untouched
    assert cli._sanitize_python_code("a = 1 + \\\n    2") == "a = 1 + \\\n    2"


def test_sanitize_python_code_restores_pii_scrubbed_decorators():
    # Real-world failure: the platform's PII scrubber treats `@name(` as an
    # email/handle-like pattern and redacts every `@mcp.tool()` decorator to
    # `\pii_<hash>()` before the trace reaches the client. Since every such
    # occurrence in generated MCP code IS a decorator, restoring it is safe —
    # including when the SAME hash repeats across multiple tools (observed:
    # all 7 tools in one response shared the identical `\pii_8d9040d12a()`).
    code = (
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('x', host='0.0.0.0', stateless_http=True)\n"
        "\\pii_8d9040d12a()\n"
        "def a(): return {}\n\n"
        "\\pii_8d9040d12a()\n"
        "def b(): return {}\n\n"
        "app = mcp.streamable_http_app()\n\n"
        'if __name__ == "__main__":\n'
        "    import os\n"
        '    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))\n'
    )
    sanitized = cli._sanitize_python_code(code)
    assert "\\pii_" not in sanitized
    assert sanitized.count("@mcp.tool()") == 2
    ok, err = cli._validate_server_code(sanitized)
    assert ok, err


def test_sanitize_python_code_restores_pii_glued_to_previous_line():
    # Real-world failure (observed 2026-07-03): the PII scrubber can swallow
    # the newline BEFORE the decorator too, gluing `\pii_<hash>()` to the tail
    # of the previous line (a comment banner) instead of starting its own
    # line. A line-start-anchored regex misses this entirely.
    code = (
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('x', host='0.0.0.0', stateless_http=True)\n"
        "# ============================================================\n"
        "# TOOLS\n"
        "# ============================================================\\pii_8d9040d12a()\n"
        "def consultar(cst: str) -> dict:\n"
        "    return {}\n\n"
        "app = mcp.streamable_http_app()\n\n"
        'if __name__ == "__main__":\n'
        "    import os\n"
        '    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))\n'
    )
    sanitized = cli._sanitize_python_code(code)
    assert "\\pii_" not in sanitized
    assert "@mcp.tool()" in sanitized
    # The decorator must be on its own line, not glued to the comment banner.
    assert "===\n@mcp.tool()" in sanitized or "===\n\n@mcp.tool()" in sanitized
    ok, err = cli._validate_server_code(sanitized)
    assert ok, err


def test_parse_and_extract_survive_prose_wrapped_design_and_code():
    # Real-world response shape: the architect wraps the two blocks in
    # section headers ("DESIGN", "IMPLEMENTATION") and "---" separators rather
    # than returning ONLY the two fenced blocks. Both the JSON design and the
    # PII-scrubbed Python code must still be recovered.
    answer = (
        "Vou criar o MCP completo.\n\n---\n\nDESIGN\n\n```json\n"
        '{"id": "svc", "name": "Svc", "tools": [{"name": "consultar", "description": "x"}]}\n'
        "```\n\n---\n\nIMPLEMENTATION\n\n```python\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('svc', host='0.0.0.0', stateless_http=True)\n"
        "\\pii_8d9040d12a()\n"
        "def consultar() -> dict:\n"
        "    return {}\n\n"
        "app = mcp.streamable_http_app()\n\n"
        'if __name__ == "__main__":\n'
        "    import os\n"
        '    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))\n'
        "```\n\n---\n\nENTREGA CONCLUÍDA\n"
    )
    design = cli._normalize_mcp_design(
        cli._parse_json_draft(cli._extract_fenced_block(answer, ("json",)) or answer))
    assert design is not None and design["id"] == "svc"

    code = cli._sanitize_python_code(cli._extract_fenced_block(answer, ("python", "py")))
    assert "\\pii_" not in code
    ok, err = cli._validate_server_code(code)
    assert ok, err


def test_loads_tolerant_repairs_and_falls_back():
    from headlabs.client import _loads_tolerant
    assert _loads_tolerant('{"a": 1}') == {"a": 1}                 # valid
    assert _loads_tolerant('{"a": "C:\\Temp"}') == {"a": "C:\\Temp"}  # invalid \T repaired
    assert _loads_tolerant('not json at all') == {"answer": "not json at all"}
    assert _loads_tolerant({"already": "dict"}) == {"already": "dict"}


# ── _validate_mcp_contract (design-quality lint) ──────────────────────────────

_GOOD_DESIGN = {
    "id": "svc", "name": "Svc",
    "tools": [{
        "name": "fetch_usage_limits",
        "description": "Retorna os limites de uso atuais do serviço informado. "
                        "Use quando o usuário quiser saber quanto ainda pode consumir. "
                        "Somente leitura, sem efeitos colaterais.",
        "params": [{"name": "service_id", "type": "str", "required": True,
                    "description": "id do serviço a consultar"}],
        "side_effects": False, "idempotent": True,
    }],
}

_GOOD_CODE = "try:\n    pass\nexcept Exception:\n    erro = {}\n"


def test_validate_mcp_contract_clean_design_has_no_warnings():
    assert cli._validate_mcp_contract(_GOOD_DESIGN, _GOOD_CODE) == []


def test_validate_mcp_contract_flags_generic_tool_name():
    design = {"id": "x", "tools": [{"name": "get_data", "description": "x" * 30,
                                    "params": [], "side_effects": False, "idempotent": True}]}
    warnings = cli._validate_mcp_contract(design, _GOOD_CODE)
    assert any("genérico" in w for w in warnings)


def test_validate_mcp_contract_flags_versioned_name():
    design = {"id": "x", "tools": [{"name": "fetch_data_v2", "description": "x" * 30,
                                    "params": [], "side_effects": False, "idempotent": True}]}
    warnings = cli._validate_mcp_contract(design, _GOOD_CODE)
    assert any("versionado" in w for w in warnings)


def test_validate_mcp_contract_flags_missing_or_short_description():
    design = {"id": "x", "tools": [
        {"name": "fetch_widget", "description": "", "params": [], "side_effects": False, "idempotent": True},
        {"name": "fetch_gadget", "description": "curta", "params": [], "side_effects": False, "idempotent": True},
    ]}
    warnings = cli._validate_mcp_contract(design, _GOOD_CODE)
    assert any("sem descrição" in w for w in warnings)
    assert any("muito curta" in w for w in warnings)


def test_validate_mcp_contract_flags_undocumented_param():
    design = {"id": "x", "tools": [{
        "name": "fetch_widget", "description": "x" * 30,
        "params": [{"name": "id", "type": "str", "required": True, "description": ""}],
        "side_effects": False, "idempotent": True,
    }]}
    warnings = cli._validate_mcp_contract(design, _GOOD_CODE)
    assert any("sem descrição" in w and "id" in w for w in warnings)


def test_validate_mcp_contract_flags_non_idempotent_side_effect():
    design = {"id": "x", "tools": [{
        "name": "create_ticket", "description": "x" * 30, "params": [],
        "side_effects": True, "idempotent": False,
    }]}
    warnings = cli._validate_mcp_contract(design, _GOOD_CODE)
    assert any("idempotency key" in w for w in warnings)


def test_validate_mcp_contract_flags_missing_structured_errors():
    warnings = cli._validate_mcp_contract(_GOOD_DESIGN, "app = mcp.streamable_http_app()\n")
    assert any("erro estruturado" in w for w in warnings)


# ── behavioral verification (real MCP protocol against a local subprocess) ───

@pytest.mark.real_verify
def test_probe_mcp_server_reports_connection_failure():
    import asyncio
    result = asyncio.run(cli._probe_mcp_server("http://127.0.0.1:1/mcp", timeout=1.0))
    assert result["ok"] is False
    assert result["error"]


@pytest.mark.real_verify
def test_verify_mcp_behavior_missing_dir_soft_fails(tmp_path):
    result = cli._verify_mcp_behavior(str(tmp_path / "does-not-exist"), timeout=2.0)
    assert result["ok"] is False
    assert result.get("skipped") is True
    assert result["error"]


@pytest.mark.real_verify
def test_verify_mcp_behavior_runs_and_verifies_real_server(tmp_path):
    # No mock: actually scaffold a minimal server and run _verify_mcp_behavior
    # against it as a real subprocess speaking the real MCP protocol. Skips
    # gracefully if the `mcp` package isn't importable in this environment.
    pytest.importorskip("mcp")
    mcp_dir = cli._scaffold_mcp("realcheck", "Real Check", "d", _SERVER_CODE,
                               base_dir=str(tmp_path))
    result = cli._verify_mcp_behavior(mcp_dir, timeout=15.0)
    assert result["ok"] is True, result.get("error")
    assert "classify" in result["tools"]


def test_slugify_id_normalizes():
    assert cli._slugify_id("My Cool_MCP") == "my-cool-mcp"
    assert cli._slugify_id("already-kebab") == "already-kebab"
    assert cli._slugify_id("  spaced  ") == "spaced"
    assert cli._slugify_id("") == ""


def test_slugify_id_neutralizes_path_traversal():
    # No slashes/dots survive → cannot escape ./mcps/<id>/ or break an ECR tag
    slug = cli._slugify_id("../../etc/passwd")
    assert "/" not in slug and ".." not in slug
    assert slug == "etc-passwd"




# ── _confirm (approval gate) ──────────────────────────────────────────────────

def test_confirm_accepts_affirmatives(monkeypatch):
    for val in ("y", "yes", "s", "sim", "YES", "  Sim "):
        monkeypatch.setattr("builtins.input", lambda *a, _v=val: _v)
        assert cli._confirm("ok?") is True


def test_confirm_rejects_negatives_and_empty(monkeypatch):
    for val in ("n", "no", "não", "", "x"):
        monkeypatch.setattr("builtins.input", lambda *a, _v=val: _v)
        assert cli._confirm("ok?") is False


def test_confirm_fails_safe_on_eof(monkeypatch):
    def _eof(*a):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert cli._confirm("ok?") is False           # default no
    assert cli._confirm("ok?", default=True) is True


def test_print_bad_draft_saves_full_output_to_file_and_shows_summary(monkeypatch, tmp_path):
    # The terminal should stay clean (short summary + file path); the FULL raw
    # answer must still be recoverable, just persisted to disk instead of
    # dumped into the user's terminal on every failure.
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    answer = "HEAD_MARKER" + ("x" * 5000) + "TAIL_MARKER"
    cli._print_bad_draft(answer)
    out = buf.getvalue()
    assert str(len(answer)) in out                # length reported
    assert "HEAD_MARKER" in out                    # first-line preview shown
    assert answer not in out                       # NOT dumped verbatim to the terminal
    drafts = list((tmp_path / "drafts").glob("bad-draft-*.txt"))
    assert len(drafts) == 1
    assert drafts[0].read_text(encoding="utf-8") == answer   # full content preserved on disk
    assert str(drafts[0]) in out                   # path surfaced to the user


# ── mcps push: visibility / auth / auto-publish ───────────────────────────────

def test_mcps_push_private_default_does_not_publish(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    _mock_docker_and_deploy(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mcps" / "svc").mkdir(parents=True)
    (tmp_path / "mcps" / "svc" / "server.py").write_text(_SERVER_CODE)

    args = SimpleNamespace(mcp_id="svc", profile=None, message=None, wait=False,
                           visibility="private", auth="api-key")
    cli._mcps_push(args)

    assert not any(r["path"].endswith("/publish") for r in fake.requests)
    assert "MCP privado" in buf.getvalue()


def test_mcps_push_public_with_runtime_auto_publishes(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    _mock_docker_and_deploy(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mcps" / "svc").mkdir(parents=True)
    (tmp_path / "mcps" / "svc" / "server.py").write_text(_SERVER_CODE)

    args = SimpleNamespace(mcp_id="svc", profile=None, message=None, wait=False,
                           visibility="public", auth="api-key")
    cli._mcps_push(args)

    publishes = [r for r in fake.requests if r["path"] == "/mcps/svc/publish"]
    assert len(publishes) == 1
    assert "MCP publicado" in buf.getvalue()


def test_mcps_push_public_without_runtime_does_not_publish(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    fake.deploy_runtime_id = None      # deploy did not confirm a runtime
    _mock_docker_and_deploy(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mcps" / "svc").mkdir(parents=True)
    (tmp_path / "mcps" / "svc" / "server.py").write_text(_SERVER_CODE)

    args = SimpleNamespace(mcp_id="svc", profile=None, message=None, wait=False,
                           visibility="public", auth="api-key")
    cli._mcps_push(args)

    assert not any(r["path"].endswith("/publish") for r in fake.requests)
    assert "sem runtime_id confirmado" in buf.getvalue()


def test_mcps_push_rejects_private_with_auth_none(monkeypatch, tmp_path, capsys):
    fake, _ = _install_fake(monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mcps" / "svc").mkdir(parents=True)

    args = SimpleNamespace(mcp_id="svc", profile=None, message=None, wait=False,
                           visibility="private", auth="none")
    with pytest.raises(SystemExit):
        cli._mcps_push(args)


def test_mcps_push_public_with_auth_none_notes_authless(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    _mock_docker_and_deploy(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mcps" / "svc").mkdir(parents=True)
    (tmp_path / "mcps" / "svc" / "server.py").write_text(_SERVER_CODE)

    args = SimpleNamespace(mcp_id="svc", profile=None, message=None, wait=False,
                           visibility="public", auth="none")
    cli._mcps_push(args)

    assert "Authless" in buf.getvalue()


# ── mcps publish / unpublish (standalone commands) ────────────────────────────

def test_mcps_publish_requires_active_runtime(monkeypatch):
    fake, buf = _install_fake(monkeypatch, "{}")

    def request_no_runtime(method, path, json=None, **kw):
        if method == "GET" and path == "/mcps/svc":
            return {"id": "svc", "kind": "mcp"}   # no runtime_id
        return {}
    monkeypatch.setattr(fake, "request", request_no_runtime)

    cli._mcps_publish(SimpleNamespace(mcp_id="svc"))

    assert "não tem runtime ativo" in buf.getvalue()


def test_mcps_publish_succeeds_with_active_runtime(monkeypatch):
    fake, buf = _install_fake(monkeypatch, "{}")

    def request_with_runtime(method, path, json=None, **kw):
        if method == "GET" and path == "/mcps/svc":
            return {"id": "svc", "kind": "mcp", "runtime_id": "rt-1"}
        return {}
    monkeypatch.setattr(fake, "request", request_with_runtime)

    cli._mcps_publish(SimpleNamespace(mcp_id="svc"))

    assert "publicado" in buf.getvalue()


def test_mcps_unpublish_calls_client(monkeypatch):
    fake, buf = _install_fake(monkeypatch, "{}")
    cli._mcps_unpublish(SimpleNamespace(mcp_id="svc"))
    assert "despublicado" in buf.getvalue()


def test_cli_parses_mcps_publish_and_unpublish(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "cmd_mcps", lambda args: captured.update(args=args))
    monkeypatch.setattr("sys.argv", ["headlabs", "mcps", "publish", "svc"])
    cli.main()
    assert captured["args"].mcp_id == "svc"
    assert captured["args"].mcps_cmd == "publish"

    captured.clear()
    monkeypatch.setattr("sys.argv", ["headlabs", "mcps", "unpublish", "svc"])
    cli.main()
    assert captured["args"].mcps_cmd == "unpublish"


def test_cli_parses_mcps_create_visibility_and_auth(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "cmd_mcps", lambda args: captured.update(args=args))
    monkeypatch.setattr("sys.argv", ["headlabs", "mcps", "create", "--spec", "/tmp/x.md",
                                     "--visibility", "public", "--auth", "none"])
    cli.main()
    assert captured["args"].visibility == "public"
    assert captured["args"].auth == "none"


def test_cli_mcps_create_visibility_defaults_to_private(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "cmd_mcps", lambda args: captured.update(args=args))
    monkeypatch.setattr("sys.argv", ["headlabs", "mcps", "create", "--spec", "/tmp/x.md"])
    cli.main()
    assert captured["args"].visibility == "private"
    assert captured["args"].auth == "api-key"


# ── mcps delete ────────────────────────────────────────────────────────────────

def test_mcps_delete_confirmed_deletes_remote(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    _answer(monkeypatch, "y")

    cli._mcps_delete(SimpleNamespace(mcp_id="svc", local=False, yes=False))

    assert any(r["method"] == "DELETE" and r["path"] == "/mcps/svc" for r in fake.requests)
    assert "deletado" in buf.getvalue()


def test_mcps_delete_declined_does_not_call_api(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    _answer(monkeypatch, "n")

    cli._mcps_delete(SimpleNamespace(mcp_id="svc", local=False, yes=False))

    assert not any(r["method"] == "DELETE" for r in fake.requests)
    assert "Cancelado" in buf.getvalue()


def test_mcps_delete_yes_flag_skips_confirmation(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.setattr("builtins.input",
                        lambda *a: pytest.fail("--yes must skip the prompt"))

    cli._mcps_delete(SimpleNamespace(mcp_id="svc", local=False, yes=True))

    assert any(r["method"] == "DELETE" and r["path"] == "/mcps/svc" for r in fake.requests)


def test_mcps_delete_local_removes_directory(monkeypatch, tmp_path):
    # local removal now happens by default (no --local needed)
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)
    mcp_dir = tmp_path / "mcps" / "svc"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "server.py").write_text("x = 1\n")

    cli._mcps_delete(SimpleNamespace(mcp_id="svc", local=False, yes=True))

    assert not mcp_dir.exists()
    assert "Removido" in buf.getvalue()


def test_mcps_delete_local_noop_when_dir_absent(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)

    cli._mcps_delete(SimpleNamespace(mcp_id="svc", local=False, yes=True))

    assert "não existe localmente" in buf.getvalue()


def test_mcps_delete_404_still_removes_local(monkeypatch, tmp_path):
    # Regression: `create` can leave a local ./mcps/<id>/ that was never
    # registered on the platform (scaffolded, then aborted/failed before
    # POST /mcps). `delete` must not treat the resulting 404 as a hard
    # failure that blocks local cleanup — that's exactly the case the user
    # needs cleaned up.
    import requests
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)
    mcp_dir = tmp_path / "mcps" / "svc"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "server.py").write_text("x = 1\n")

    def not_found_request(method, path, json=None, **kw):
        if method == "DELETE":
            resp = SimpleNamespace(status_code=404)
            raise requests.HTTPError("404 Client Error: Not Found", response=resp)
        return {}
    monkeypatch.setattr(fake, "request", not_found_request)

    cli._mcps_delete(SimpleNamespace(mcp_id="svc", local=False, yes=True))

    assert not mcp_dir.exists()                       # local cleanup still happened
    out = buf.getvalue()
    assert "não existe na HeadLabs" in out
    assert "Removido" in out


def test_mcps_delete_api_failure_skips_local_removal(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)
    mcp_dir = tmp_path / "mcps" / "svc"
    mcp_dir.mkdir(parents=True)

    def failing_request(method, path, json=None, **kw):
        if method == "DELETE":
            raise RuntimeError("connection refused")
        return {}
    monkeypatch.setattr(fake, "request", failing_request)

    cli._mcps_delete(SimpleNamespace(mcp_id="svc", local=False, yes=True))

    assert mcp_dir.exists()               # local dir untouched — a real (non-404) failure
    assert "erro ao deletar" in buf.getvalue()


def test_cli_parses_mcps_delete(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "cmd_mcps", lambda args: captured.update(args=args))
    monkeypatch.setattr("sys.argv", ["headlabs", "mcps", "delete", "svc", "--local", "-y"])
    cli.main()
    assert captured["args"].mcp_id == "svc"
    assert captured["args"].local is True
    assert captured["args"].yes is True
    assert captured["args"].mcps_cmd == "delete"


# ── agents delete ────────────────────────────────────────────────────────────
# Mirrors the mcps delete suite above exactly — same UX contract (confirm
# unless --yes, local ./agents/<id>/ always removed if present, a 404 from
# the platform is not a hard failure that blocks local cleanup).

def test_agents_delete_confirmed_deletes_remote(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    _answer(monkeypatch, "y")

    cli.cmd_agents_delete(SimpleNamespace(agent_id="svc", local=False, yes=False))

    assert any(r["method"] == "DELETE" and r["path"] == "/agents/svc" for r in fake.requests)
    assert "deletado" in buf.getvalue()


def test_agents_delete_declined_does_not_call_api(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    _answer(monkeypatch, "n")

    cli.cmd_agents_delete(SimpleNamespace(agent_id="svc", local=False, yes=False))

    assert not any(r["method"] == "DELETE" for r in fake.requests)
    assert "Cancelado" in buf.getvalue()


def test_agents_delete_yes_flag_skips_confirmation(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.setattr("builtins.input",
                        lambda *a: pytest.fail("--yes must skip the prompt"))

    cli.cmd_agents_delete(SimpleNamespace(agent_id="svc", local=False, yes=True))

    assert any(r["method"] == "DELETE" and r["path"] == "/agents/svc" for r in fake.requests)


def test_agents_delete_local_removes_directory(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)
    agent_dir = tmp_path / "agents" / "svc"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.py").write_text("x = 1\n")

    cli.cmd_agents_delete(SimpleNamespace(agent_id="svc", local=False, yes=True))

    assert not agent_dir.exists()
    assert "Removido" in buf.getvalue()


def test_agents_delete_local_noop_when_dir_absent(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)

    cli.cmd_agents_delete(SimpleNamespace(agent_id="svc", local=False, yes=True))

    assert "não existe localmente" in buf.getvalue()


def test_agents_delete_404_still_removes_local(monkeypatch, tmp_path):
    # Regression (same case as mcps delete): `create` can leave a local
    # ./agents/<id>/ that was never registered on the platform. A 404 on
    # DELETE must not block the local cleanup the user actually needs.
    import requests
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)
    agent_dir = tmp_path / "agents" / "svc"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.py").write_text("x = 1\n")

    def not_found_request(method, path, json=None, **kw):
        if method == "DELETE":
            resp = SimpleNamespace(status_code=404)
            raise requests.HTTPError("404 Client Error: Not Found", response=resp)
        return {}
    monkeypatch.setattr(fake, "request", not_found_request)

    cli.cmd_agents_delete(SimpleNamespace(agent_id="svc", local=False, yes=True))

    assert not agent_dir.exists()                      # local cleanup still happened
    out = buf.getvalue()
    assert "não existe na HeadLabs" in out
    assert "Removido" in out


def test_agents_delete_api_failure_skips_local_removal(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    monkeypatch.chdir(tmp_path)
    agent_dir = tmp_path / "agents" / "svc"
    agent_dir.mkdir(parents=True)

    def failing_request(method, path, json=None, **kw):
        if method == "DELETE":
            raise RuntimeError("connection refused")
        return {}
    monkeypatch.setattr(fake, "request", failing_request)

    cli.cmd_agents_delete(SimpleNamespace(agent_id="svc", local=False, yes=True))

    assert agent_dir.exists()             # local dir untouched — a real (non-404) failure
    assert "erro ao deletar" in buf.getvalue()


def test_cli_parses_agents_delete(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "cmd_agents", lambda args: captured.update(args=args))
    monkeypatch.setattr("sys.argv", ["headlabs", "agents", "delete", "svc", "--local", "-y"])
    cli.main()
    assert captured["args"].agent_id == "svc"
    assert captured["args"].local is True
    assert captured["args"].yes is True
    assert captured["args"].subcmd == "delete"


def test_cli_parses_agents_rm_alias(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "cmd_agents", lambda args: captured.update(args=args))
    monkeypatch.setattr("sys.argv", ["headlabs", "agents", "rm", "svc", "-y"])
    cli.main()
    assert captured["args"].agent_id == "svc"
    assert captured["args"].subcmd == "delete"


# ── mcps push: platform registration (kind="mcp") ─────────────────────────────
# Regression coverage for a real gap: uploading source + deploying a runtime
# never created the `kind: "mcp"` record that `mcps list`/`get_mcp` filter on,
# so a fully built+deployed MCP could be invisible forever. `_mcps_push` must
# register it first (idempotently) before doing anything else.

def _mock_docker_and_deploy(monkeypatch, fake):
    """Stub out subprocess (docker build/push/login) and the deploy call so
    `_mcps_push` can run to completion without touching Docker/network."""
    import subprocess as _subprocess

    class _Ok:
        returncode = 0
        stderr = ""
    monkeypatch.setattr(_subprocess, "run", lambda *a, **k: _Ok())


def test_mcps_push_registers_mcp_when_absent(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    _mock_docker_and_deploy(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mcps" / "svc").mkdir(parents=True)
    (tmp_path / "mcps" / "svc" / "server.py").write_text(_SERVER_CODE)

    args = SimpleNamespace(mcp_id="svc", profile=None, message=None, wait=False)
    cli._mcps_push(args)

    posts = [r for r in fake.requests if r["method"] == "POST" and r["path"] == "/mcps"]
    assert len(posts) == 1
    assert posts[0]["json"]["id"] == "svc"
    assert posts[0]["json"]["framework"] == "container"
    assert "registrado na plataforma" in buf.getvalue()


def test_mcps_push_skips_registration_when_already_exists(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    fake.existing_mcp_ids = {"svc"}
    _mock_docker_and_deploy(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mcps" / "svc").mkdir(parents=True)
    (tmp_path / "mcps" / "svc" / "server.py").write_text(_SERVER_CODE)

    args = SimpleNamespace(mcp_id="svc", profile=None, message=None, wait=False)
    cli._mcps_push(args)

    posts = [r for r in fake.requests if r["method"] == "POST" and r["path"] == "/mcps"]
    assert posts == []                              # not re-registered
    assert "registrado na plataforma" not in buf.getvalue()


def test_mcps_push_passes_through_display_name_and_description(monkeypatch, tmp_path):
    fake, buf = _install_fake(monkeypatch, "{}")
    _mock_docker_and_deploy(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mcps" / "svc").mkdir(parents=True)
    (tmp_path / "mcps" / "svc" / "server.py").write_text(_SERVER_CODE)

    args = SimpleNamespace(mcp_id="svc", profile=None, message=None, wait=False,
                           display_name="Serviço X", description="faz coisas")
    cli._mcps_push(args)

    posts = [r for r in fake.requests if r["method"] == "POST" and r["path"] == "/mcps"]
    assert posts[0]["json"]["display_name"] == "Serviço X"
    assert posts[0]["json"]["description"] == "faz coisas"


def test_mcps_create_spec_registers_mcp_before_push(monkeypatch, tmp_path):
    # End-to-end: `mcps create --spec` must reach a registered MCP, not just a
    # built+deployed one — this is the exact regression that was reported.
    fake, buf = _install_fake(monkeypatch, _mcp_answer(_DESIGN, _SERVER_CODE))
    fake.existing_agent_ids = {"mcp-architect"}
    _mock_docker_and_deploy(monkeypatch, fake)
    monkeypatch.chdir(tmp_path)
    _answer(monkeypatch, "y")

    spec = tmp_path / "spec.md"
    spec.write_text("um MCP")
    args = SimpleNamespace(spec=str(spec), id=None, profile=None, message=None,
                           tenant=None, no_deploy=False, wait=False,
                           mcps_cmd="create")
    cli._mcps_create(args)

    posts = [r for r in fake.requests if r["method"] == "POST" and r["path"] == "/mcps"]
    assert len(posts) == 1
    assert posts[0]["json"]["id"] == "cclasstrib"
    assert posts[0]["json"]["display_name"] == "CClass Trib"


# ── CLI parsing wires the flags ───────────────────────────────────────────────

def test_cli_parses_agents_create_spec(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "cmd_agents",
                        lambda args: captured.update(args=args))
    monkeypatch.setattr("sys.argv",
                        ["headlabs", "agents", "create", "--spec", "/tmp/x.md"])
    cli.main()
    assert captured["args"].spec == "/tmp/x.md"
    assert captured["args"].subcmd == "create"


def test_cli_parses_mcps_create_spec(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "cmd_mcps",
                        lambda args: captured.update(args=args))
    monkeypatch.setattr("sys.argv",
                        ["headlabs", "mcps", "create", "--spec", "/tmp/m.md",
                         "--no-deploy"])
    cli.main()
    assert captured["args"].spec == "/tmp/m.md"
    assert captured["args"].mcps_cmd == "create"
    assert captured["args"].no_deploy is True
