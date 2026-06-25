"""End-to-end tests that drive the REAL `headlabs` CLI against the live platform.

Gated by environment (they create real resources and take minutes):
  HEADLABS_E2E=1         enable e2e (smoke + light checks)
  HEADLABS_E2E_BUILD=1   also run the full autonomous build (slow, ~5 min)

Everything goes through the CLI (subprocess) + JSON output + exit codes — the
same surface a user/CI uses. This file is the reference harness the other build
scenarios (L2..L12) replicate: create -> poll to terminal -> assert the real
resources exist -> teardown.
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
    """Run the headlabs CLI in a subprocess. Returns (returncode, parsed)."""
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
    return proc.returncode, parsed


def _poll_until_terminal(job_id, timeout=600, interval=15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rc, data = cli("loops", "status", job_id, "-o", "json")
        if isinstance(data, dict):
            last = data
            if (data.get("status") or "").lower() in (_TERMINAL_OK | _TERMINAL_FAIL):
                return data
        time.sleep(interval)
    return last or {}


# -- L0 smoke (fast, no build) -------------------------------------------------

def test_l0_labs_list_json_ok():
    rc, data = cli("labs", "list", "-o", "json")
    assert rc == 0
    assert isinstance(data, list)


def test_l0_loops_list_json_ok():
    rc, data = cli("loops", "list", "-o", "json")
    assert rc == 0
    assert isinstance(data, list)


# -- L1 autonomous build end-to-end (slow) -------------------------------------

@pytest.mark.skipif(not E2E_BUILD, reason="set HEADLABS_E2E_BUILD=1 to run the full build")
def test_l1_autonomous_build_creates_real_resources():
    """labs create --auto-approve runs the whole pipeline unattended and
    provisions a real managed table + function."""
    name = f"e2e-l1-{int(time.time())}"
    lab_id = None
    try:
        rc, created = cli(
            "labs", "create",
            "-i", "API de notas: criar e listar notas, persistindo numa tabela",
            "--name", name, "--stack", "python", "--auto-approve", "-o", "json",
        )
        assert rc == 0 and isinstance(created, dict), f"create failed: {created!r}"
        lab_id, job_id = created["lab_id"], created["job_id"]

        final = _poll_until_terminal(job_id, timeout=600)
        status = (final.get("status") or "").lower()
        assert status in _TERMINAL_OK, f"build did not complete: status={status}"

        # Real, functional resources were provisioned (managed primitives).
        resources = [str(r) for r in (final.get("resources_created") or [])]
        assert any(r.startswith("table:") for r in resources), f"no table created: {resources}"
        assert any(r.startswith("function:") for r in resources), f"no function created: {resources}"
    finally:
        if lab_id:
            cli("labs", "archive", lab_id)
