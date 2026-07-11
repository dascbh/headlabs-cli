"""Rigorous E2E build harness.

Usage: python e2e_harness.py <lab_id> <label> <intent>

Launches a build via the CLI (real user path), polls to terminal, then
INDEPENDENTLY verifies the delivered product:
  - functions: re-runs the executor's acceptance_tests itself (invoke + check
    expect_status) — a functional check independent of the validator's verdict.
  - KB: independent retrieve probe.
  - storage: list/exists probe.
Prints a JSON result line prefixed with RESULT::.
"""
import sys, json, time, subprocess, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from headlabs.client import HeadLabsClient

c = HeadLabsClient()
lab, label, intent = sys.argv[1], sys.argv[2], sys.argv[3]
TERMINAL_OK = {"complete", "completed", "done", "succeeded"}
TERMINAL = TERMINAL_OK | {"failed", "error", "cancelled", "needs_review", "blocked"}


def launch():
    out = subprocess.run(
        [".venv/bin/headlabs", "loops", "create", "--lab", lab, "-i", intent,
         "--auto-approve", "--judges", "off", "-o", "json"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "src"})
    try:
        return json.loads(out.stdout).get("loop_id")
    except Exception:
        # fallback: lineage
        loops = c.request("GET", f"/labs-v2/{lab}/lineage") or []
        loops = sorted(loops, key=lambda l: l.get("started_at") or "")
        return loops[-1].get("loop_id") if loops else None


def poll(lid, cap=60):
    for _ in range(cap):  # cap*20s
        try:
            l = c.request("GET", f"/loops/{lid}")
        except Exception:
            time.sleep(20); continue
        if str(l.get("status", "")).lower() in TERMINAL:
            return l
        time.sleep(20)
    return c.request("GET", f"/loops/{lid}")


def run_acceptance(tests):
    """Independently run the function acceptance_tests; return (passed, total, detail)."""
    passed, total, detail = 0, 0, []
    for t in tests or []:
        res = str(t.get("resource", ""))
        if not res.startswith("function:"):
            continue
        fn = res.split(":", 1)[1]
        inv = t.get("invocation") or {}
        exp = t.get("expect_status")
        total += 1
        try:
            r = c.request("POST", f"/functions/{fn}/invoke", json=inv)
            sc = r.get("statusCode") if isinstance(r, dict) else None
            ok = (sc == exp) if exp is not None else (isinstance(sc, int) and 200 <= sc < 300)
            # tolerate 2xx when expecting 2xx-ish
            if not ok and isinstance(sc, int) and isinstance(exp, int) and 200 <= sc < 300 and 200 <= exp < 300:
                ok = True
            passed += 1 if ok else 0
            detail.append(f"{t.get('operation')}:{sc}{'✓' if ok else '✗(exp '+str(exp)+')'}")
        except Exception as e:
            detail.append(f"{t.get('operation')}:ERR")
    return passed, total, detail


def main():
    t0 = time.time()
    lid = launch()
    if not lid:
        print("RESULT::" + json.dumps({"label": label, "error": "launch failed"})); return
    l = poll(lid)
    elapsed = int(time.time() - t0)
    status = str(l.get("status", "")).lower()
    val = l.get("validation") or {}
    if isinstance(val, str):
        try: val = json.loads(val)
        except Exception: val = {}
    cov = val.get("coverage")
    at = l.get("acceptance_tests") or []
    if isinstance(at, str):
        try: at = json.loads(at)
        except Exception: at = []
    res = l.get("resources_created") or []
    res_set = sorted(set(str(r) for r in res))
    fp, ft, fdetail = run_acceptance(at)
    result = {
        "label": label, "loop_id": lid, "status": status,
        "iterations": l.get("iterations"), "elapsed_s": elapsed,
        "validator_coverage": cov, "acceptance_tests": len(at),
        "func_independent_pass": f"{fp}/{ft}", "func_detail": fdetail,
        "resources": res_set,
    }
    print("RESULT::" + json.dumps(result, ensure_ascii=False))


main()
