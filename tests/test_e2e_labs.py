"""End-to-end tests that drive the REAL `headlabs` CLI against the live platform.

Gated by environment (they create real resources and take minutes):
  HEADLABS_E2E=1         enable e2e (smoke + light checks)
  HEADLABS_E2E_BUILD=1   also run the full autonomous builds (slow, ~5 min each)

Everything goes through the CLI (subprocess) + JSON output + exit codes — the
same surface a user/CI uses. Builds run SERIALLY (concurrent builds throttle the
shared loop-agent runtimes). Reference flow: create -> poll to terminal ->
assert real resources -> teardown.

Status (validated live against prod):
  - L0 smoke .......... PASS
  - L1 autonomous build PASS (table + function, ~4m38s)
  - gate guardrail .... PASS (pauses at after_architect; approve resumes)
  - L2/L7 ............. provided; run on demand (each ~5 min)
"""

import json
import os
import subprocess
import sys
import time

import pytest

E2E = os.environ.get("HEADLABS_E2E") == "1"
E2E_BUILD = E2E and os.environ.get("HEADLABS_E2E_BUILD") == "1"

pytestmark = pytest.mark.skipif(not E2E, reason="set HEADLABS_E2E=1 to run e2e")

_TERMINAL_OK = {"complete", "completed", "succeeded", "done"}
_TERMINAL_FAIL = {"failed", "error", "dlq", "timed_out", "cancelled"}


def cli(*args, timeout=120):
    """Run the headlabs CLI in a subprocess. Returns (returncode, parsed, raw)."""
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", os.path.join(os.path.dirname(__file__), "..", "src"))
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from headlabs.cli import main; sys.exit(main())", *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    out = proc.stdout.strip()
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = out
    return proc.returncode, parsed, out


def create_lab(intent, name, stack, *, auto_approve=True, retries=3):
    """Create a lab+build, tolerating transient API 500s (observed on /labs-v2)."""
    args = ["labs", "create", "-i", intent, "--name", name, "--stack", stack, "-o", "json"]
    if auto_approve:
        args.append("--auto-approve")
    for attempt in range(retries):
        rc, data, raw = cli(*args)
        if rc == 0 and isinstance(data, dict) and data.get("job_id"):
            return data
        time.sleep(5 * (attempt + 1))
    raise AssertionError(f"create failed after {retries} tries: rc={rc} raw={raw!r}")


def poll_until_terminal(job_id, timeout=600, interval=15):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        rc, data, _ = cli("loops", "status", job_id, "-o", "json")
        if isinstance(data, dict):
            last = data
            if (data.get("status") or "").lower() in (_TERMINAL_OK | _TERMINAL_FAIL):
                return data
        time.sleep(interval)
    return last


# ── L0 smoke (fast, no build) ────────────────────────────────────────────────

def test_l0_labs_list_json_ok():
    rc, data, _ = cli("labs", "list", "-o", "json")
    assert rc == 0 and isinstance(data, list)


def test_l0_loops_list_json_ok():
    rc, data, _ = cli("loops", "list", "-o", "json")
    assert rc == 0 and isinstance(data, list)


# ── build matrix (each is a real autonomous build) ───────────────────────────
# expected = resource-type prefixes that MUST appear in resources_created.
# L1 is fully verified; L2/L7 assert the universal invariant (complete + >=1
# resource) until their resource shapes are pinned from a live run.
BUILD_SCENARIOS = [
    ("l1_notes_api",
     "API de notas: criar e listar notas, persistindo numa tabela",
     "python", ["table:", "function:"]),
    ("l2_landing_site",
     "Landing page estática com 3 seções (hero, recursos, contato) de um produto SaaS",
     "html", []),
    ("l7_rag_assistant",
     "Assistente que responde perguntas sobre documentos usando uma base de conhecimento (RAG)",
     "python", []),
]


@pytest.mark.skipif(not E2E_BUILD, reason="set HEADLABS_E2E_BUILD=1 to run full builds")
@pytest.mark.parametrize("scenario_id,intent,stack,expected", BUILD_SCENARIOS,
                         ids=[s[0] for s in BUILD_SCENARIOS])
def test_autonomous_build(scenario_id, intent, stack, expected):
    """labs create --auto-approve runs the whole pipeline unattended and
    provisions real managed resources. Runs serially (one build at a time)."""
    name = f"e2e-{scenario_id}-{int(time.time())}"
    created = create_lab(intent, name, stack)
    lab_id, job_id = created["lab_id"], created["job_id"]
    try:
        final = poll_until_terminal(job_id, timeout=600)
        status = (final.get("status") or "").lower()
        assert status in _TERMINAL_OK, f"{scenario_id} did not complete: status={status}"
        resources = [str(r) for r in (final.get("resources_created") or [])]
        assert resources, f"{scenario_id}: no resources created"
        for prefix in expected:
            assert any(r.startswith(prefix) for r in resources), \
                f"{scenario_id}: missing resource {prefix} in {resources}"
    finally:
        cli("loops", "cancel", job_id)
        cli("labs", "archive", lab_id)


# ── guardrail: gate flow (pause → approve → resume) ──────────────────────────

@pytest.mark.skipif(not E2E_BUILD, reason="set HEADLABS_E2E_BUILD=1 to run the gate flow")
def test_gate_flow_pauses_then_resumes():
    """Without --auto-approve the build pauses at the architecture gate
    (awaiting_approval); approving it clears the gate and resumes the build."""
    name = f"e2e-gate-{int(time.time())}"
    created = create_lab("API CRUD de produtos com tabela", name, "python", auto_approve=False)
    lab_id, job_id = created["lab_id"], created["job_id"]
    try:
        # 1. Reaches the architecture gate.
        deadline = time.time() + 300
        paused = {}
        while time.time() < deadline:
            _, d, _ = cli("loops", "status", job_id, "-o", "json")
            if isinstance(d, dict) and (d.get("status") == "awaiting_approval" or d.get("pending_gate")):
                paused = d
                break
            time.sleep(15)
        assert paused.get("pending_gate") == "after_architect", \
            f"did not pause at after_architect: {paused.get('status')}/{paused.get('pending_gate')}"

        # 2. Approving clears the gate and resumes.
        rc, _, _ = cli("loops", "approve", job_id, "--note", "ok")
        assert rc == 0
        time.sleep(12)
        _, after, _ = cli("loops", "status", job_id, "-o", "json")
        assert after.get("pending_gate") != "after_architect", "gate not cleared after approve"
    finally:
        cli("loops", "cancel", job_id)
        cli("labs", "archive", lab_id)
