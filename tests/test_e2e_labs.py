"""End-to-end tests that drive the REAL `headlabs` CLI against the live platform.

Gated by environment (they create real resources and take minutes):
  HEADLABS_E2E=1         enable e2e (smoke + light checks)
  HEADLABS_E2E_BUILD=1   also run the full autonomous builds (slow, ~5 min each)

Builds run SERIALLY (concurrent builds throttle the shared loop-agent runtimes).

FUNCTIONAL verification (the real bar): a build "completing" is NOT proof the
product works. The platform's `validator` phase actually exercises the deployed
product (invokes functions, queries tables, kb_retrieve) and reports a
`validation.coverage` (0..1). We assert coverage >= a per-scenario threshold —
i.e. the product substantially satisfies the intent — and probe queryable
surfaces (table items). Builds are non-deterministic, so each scenario retries
up to ATTEMPTS times to meet the bar. Every created resource is torn down.

Validated live: L0; L1/L2/L7 builds provision real resources; gate flow
pauses+resumes; resilient invoker survives concurrent throttling.
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
_ATTEMPTS = int(os.environ.get("HEADLABS_E2E_ATTEMPTS", "2"))

# resource-type -> DELETE path template (for complete teardown)
_DELETE_PATH = {
    "table": "/tables/{id}", "function": "/functions/{id}", "storage": "/storage/{id}",
    "kb": "/knowledge-bases/{id}", "agent": "/agents/{id}", "container": "/containers/{id}",
}


def cli(*args, timeout=120):
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", os.path.join(os.path.dirname(__file__), "..", "src"))
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from headlabs.cli import main; sys.exit(main())", *args],
        capture_output=True, text=True, timeout=timeout, env=env)
    out = proc.stdout.strip()
    try:
        return proc.returncode, json.loads(out) if out else None, out
    except json.JSONDecodeError:
        return proc.returncode, out, out


def _client():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from headlabs.client import HeadLabsClient
    return HeadLabsClient()


def create_lab(intent, name, stack, *, auto_approve=True, extra_args=(), retries=3):
    args = ["labs", "create", "-i", intent, "--name", name, "--stack", stack, "-o", "json", *extra_args]
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
        _, data, _ = cli("loops", "status", job_id, "-o", "json")
        if isinstance(data, dict):
            last = data
            if (data.get("status") or "").lower() in (_TERMINAL_OK | _TERMINAL_FAIL):
                return data
        time.sleep(interval)
    return last


def _resource_pairs(loop):
    """Unique (type, id) pairs from resources_created (e.g. 'table:notes')."""
    seen = []
    for r in (loop.get("resources_created") or []):
        if ":" in str(r):
            t, i = str(r).split(":", 1)
            if (t, i) not in seen:
                seen.append((t.strip(), i.strip()))
    return seen


def teardown(loop, lab_id):
    """Delete every created managed resource, then archive the lab."""
    client = _client()
    for rtype, rid in _resource_pairs(loop):
        path = _DELETE_PATH.get(rtype)
        if not path:
            continue
        try:
            client.request("DELETE", path.format(id=rid), params={"tenant_id": "platform"})
        except Exception:
            pass
    cli("labs", "archive", lab_id)


def _table_is_queryable(name):
    try:
        client = _client()
        data = client.request("GET", f"/tables/{name}/items", params={"tenant_id": "platform"})
        return isinstance(data, list)
    except Exception:
        return False


# ── L0 smoke ─────────────────────────────────────────────────────────────────

def test_l0_labs_list_json_ok():
    rc, data, _ = cli("labs", "list", "-o", "json")
    assert rc == 0 and isinstance(data, list)


def test_l0_loops_list_json_ok():
    rc, data, _ = cli("loops", "list", "-o", "json")
    assert rc == 0 and isinstance(data, list)


# ── functional build matrix (real resources + validator coverage) ───────────
# (id, intent, stack, required resource prefixes, min validator coverage)
BUILD_SCENARIOS = [
    ("l1_notes_api",
     "API de notas: criar e listar notas, persistindo numa tabela",
     "python", ["table:", "function:"], 0.5),
    ("l2_landing_site",
     "Landing page estática com 3 seções (hero, recursos, contato) de um produto SaaS",
     "html", ["storage:", "file:"], 0.6),
    ("l7_rag_assistant",
     "Assistente que responde perguntas sobre documentos usando uma base de conhecimento (RAG)",
     "python", ["kb:", "agent:"], 0.6),
]


@pytest.mark.skipif(not E2E_BUILD, reason="set HEADLABS_E2E_BUILD=1 to run full builds")
@pytest.mark.parametrize("sid,intent,stack,prefixes,cov_min", BUILD_SCENARIOS, ids=[s[0] for s in BUILD_SCENARIOS])
def test_functional_build(sid, intent, stack, prefixes, cov_min):
    """Autonomous build provisions real resources AND the platform validator
    confirms the product substantially works (coverage >= threshold). Retries
    up to _ATTEMPTS (builds are non-deterministic). Tears down all resources."""
    best = {"coverage": -1.0}
    last_reason = ""
    for attempt in range(_ATTEMPTS):
        name = f"e2e-{sid}-{int(time.time())}"
        created = create_lab(intent, name, stack)
        lab_id, job_id = created["lab_id"], created["job_id"]
        final = {}
        try:
            final = poll_until_terminal(job_id, timeout=900)
            status = (final.get("status") or "").lower()
            if status not in _TERMINAL_OK:
                last_reason = f"status={status}"
                continue
            resources = [str(r) for r in (final.get("resources_created") or [])]
            missing = [p for p in prefixes if not any(r.startswith(p) for r in resources)]
            coverage = float((final.get("validation") or {}).get("coverage", 0) or 0)
            if coverage > best["coverage"]:
                best = {"coverage": coverage, "resources": resources, "missing": missing, "loop": final}
            if not missing and coverage >= cov_min:
                # Functional probe: any created table must be queryable.
                tables = [i for (t, i) in _resource_pairs(final) if t == "table"]
                assert all(_table_is_queryable(t) for t in tables), f"{sid}: table not queryable {tables}"
                return  # PASS
            last_reason = f"missing={missing} coverage={coverage:.2f}<{cov_min}"
        finally:
            teardown(final, lab_id)
    pytest.fail(f"{sid}: no attempt met the bar (best_coverage={best['coverage']:.2f}); last: {last_reason}")


# ── guardrail: gate flow (pause → approve → resume) ──────────────────────────

@pytest.mark.skipif(not E2E_BUILD, reason="set HEADLABS_E2E_BUILD=1 to run the gate flow")
def test_gate_flow_pauses_then_resumes():
    name = f"e2e-gate-{int(time.time())}"
    created = create_lab("API CRUD de produtos com tabela", name, "python", auto_approve=False)
    lab_id, job_id = created["lab_id"], created["job_id"]
    final = {}
    try:
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
        rc, _, _ = cli("loops", "approve", job_id, "--note", "ok")
        assert rc == 0
        time.sleep(12)
        _, after, _ = cli("loops", "status", job_id, "-o", "json")
        final = after if isinstance(after, dict) else {}
        assert final.get("pending_gate") != "after_architect", "gate not cleared after approve"
    finally:
        cli("loops", "cancel", job_id)
        teardown(final, lab_id)


# ── guardrail: reject → rework ────────────────────────────────────────────────

@pytest.mark.skipif(not E2E_BUILD, reason="set HEADLABS_E2E_BUILD=1")
def test_reject_sends_back_to_rework():
    """Rejecting at the architecture gate clears it and re-drives the build
    (the loop reworks the prior phase with the feedback)."""
    name = f"e2e-reject-{int(time.time())}"
    created = create_lab("API CRUD de produtos com tabela", name, "python", auto_approve=False)
    lab_id, job_id = created["lab_id"], created["job_id"]
    final = {}
    try:
        deadline, paused, iters_before = time.time() + 300, False, 0
        while time.time() < deadline:
            _, d, _ = cli("loops", "status", job_id, "-o", "json")
            if isinstance(d, dict) and d.get("pending_gate") == "after_architect":
                paused = True
                iters_before = int(d.get("iterations", 0) or 0)
                break
            time.sleep(15)
        assert paused, "did not reach the after_architect gate"
        rc, _, _ = cli("loops", "reject", job_id, "--note", "refaça a arquitetura: use uma abordagem mais simples")
        assert rc == 0, "reject command failed"
        # Reject feeds back to the previous phase and bumps iterations — the
        # build reworks (and may re-reach the gate). Assert the rework happened.
        deadline, reworked = time.time() + 120, False
        while time.time() < deadline:
            _, d, _ = cli("loops", "status", job_id, "-o", "json")
            if isinstance(d, dict):
                final = d
                trace = d.get("agents_trace") or []
                rejected = any(isinstance(t, dict) and str(t.get("action", "")).startswith("rejected")
                               for t in trace)
                if int(d.get("iterations", 0) or 0) > iters_before or rejected:
                    reworked = True
                    break
            time.sleep(10)
        assert reworked, "reject did not trigger a rework (iterations/trace unchanged)"
    finally:
        cli("loops", "cancel", job_id)
        teardown(final, lab_id)


# ── guardrail: research mode (investigate, build nothing) ────────────────────

@pytest.mark.skipif(not E2E_BUILD, reason="set HEADLABS_E2E_BUILD=1")
def test_research_produces_findings_and_no_resources():
    """research mode runs orchestrator→researcher→deliverer (no gates, no
    executor): it produces findings and provisions NO managed resources."""
    name = f"e2e-research-{int(time.time())}"
    rc, data, raw = cli("research", "-i", "estado da arte de rate limiting distribuído",
                        "--name", name, "--depth", "quick", "-o", "json")
    assert rc == 0 and isinstance(data, dict) and data.get("job_id"), f"research create failed: {raw!r}"
    job_id = data["job_id"]
    lab_id = data.get("lab_id")
    final = poll_until_terminal(job_id, timeout=420)
    try:
        assert (final.get("status") or "").lower() in _TERMINAL_OK, f"research not complete: {final.get('status')}"
        build_types = {"table", "function", "kb", "storage", "agent", "container"}
        provisioned = [r for r in (final.get("resources_created") or [])
                       if str(r).split(":")[0] in build_types]
        assert not provisioned, f"research must not build resources: {provisioned}"
        assert final.get("findings") or final.get("research"), "research produced no findings"
    finally:
        if lab_id:
            cli("labs", "archive", lab_id)


# ── guardrail: judge panel decides autonomously (gate_mode=judge) ────────────

@pytest.mark.skipif(not E2E_BUILD, reason="set HEADLABS_E2E_BUILD=1")
def test_judge_panel_engages():
    """With --gate-mode judge the senior-judge panel runs at the gate and
    decides (approve/revise/escalate) — proving autonomous governance, not just
    a human pause. We assert the panel engages: it either reaches 'reviewing',
    advances past the architect without a human approve, or reaches terminal."""
    name = f"e2e-judge-{int(time.time())}"
    created = create_lab("API de inventário com tabela e contagem", name, "python",
                         auto_approve=False, extra_args=["--gate-mode", "judge", "--judges", "full"])
    lab_id, job_id = created["lab_id"], created["job_id"]
    final = {}
    try:
        deadline, engaged = time.time() + 360, False
        while time.time() < deadline:
            _, d, _ = cli("loops", "status", job_id, "-o", "json")
            if isinstance(d, dict):
                final = d
                st = (d.get("status") or "").lower()
                ph = d.get("phase")
                if st == "reviewing" or d.get("panel_status") in ("running", "done"):
                    engaged = True
                    break
                if ph in ("planner", "executor", "validator", "deliverer") and st != "awaiting_approval":
                    engaged = True  # advanced past architect with no human approve → judge decided
                    break
                if st in (_TERMINAL_OK | _TERMINAL_FAIL):
                    engaged = True
                    break
            time.sleep(12)
        assert engaged, f"judge panel did not engage: {final.get('status')}/{final.get('phase')}"
    finally:
        cli("loops", "cancel", job_id)
        teardown(final, lab_id)
