"""Unit + contract tests for the labs/loops CLI surface (labsctl).

No network/AWS: HeadLabsClient is replaced with a FakeClient that records the
(method, path, body) calls and returns canned responses.
"""

import types

import pytest

import headlabs.labsctl as L


# ── helpers ──────────────────────────────────────────────────────────────────

def mkargs(**over):
    base = dict(
        intent=None, name=None, stack=None, lab=None, job_id=None, note=None,
        output="table", quiet=False, watch=False, wait=False,
        auto_approve=False, gate=None, judges=None, judge_model=None,
        gate_mode=None, max_revise=None, timeout=0, verbose=False,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


class FakeClient:
    def __init__(self, router=None):
        self.calls = []
        self.router = router or (lambda m, p, body: {})

    def request(self, method, path, json=None, **kw):
        self.calls.append((method, path, json))
        return self.router(method, path, json)

    def resolve_tenant(self, refresh=False):
        return "platform"

    def get_events(self, *a, **k):
        return {"events": [], "last_seq": 0, "status": "running"}


@pytest.fixture
def fake(monkeypatch):
    inst = FakeClient()
    monkeypatch.setattr(L, "HeadLabsClient", lambda *a, **k: inst)
    return inst


# ── _gates_from_args (gate/judge config) ─────────────────────────────────────

def test_gates_none_when_no_flags():
    assert L._gates_from_args(mkargs()) is None  # server defaults


def test_gates_auto_approve_zeros_all():
    g = L._gates_from_args(mkargs(auto_approve=True))
    assert g["after_architect"] is False
    assert g["after_planner"] is False
    assert g["before_destructive"] is False


# ── _labs_inspect: -w/--wait must actually block (regression) ───────────────
#
# Bug: the poll loop had a hardcoded `for _ in range(20)` (~240s) regardless of
# the -w/--watch or --wait flags — they were accepted by the arg parser but
# never read inside _labs_inspect, so any inspection slower than 4min silently
# "timed out" even when the user explicitly asked to wait for it.

def _inspect_router(exec_status_sequence):
    """Router: /labs-v2 → one lab; /loops?lab_id=... → one build; /loops/{id} →
    that build's detail; POST /agents/loop-inspector/invoke → an exec_id; GET
    /executions/{id} → pops the next status off exec_status_sequence each call."""
    state = {"i": 0}

    def router(method, path, body):
        if path == "/labs-v2":
            return [{"lab_id": "lab_x", "name": "x"}]
        if path.startswith("/loops?lab_id="):
            return [{"loop_id": "loop_x", "status": "complete", "mode": "build",
                     "updated_at": "2026-01-01T00:00:00Z"}]
        if path == "/loops/loop_x":
            return {"loop_id": "loop_x", "intent": "test", "resources_created": [],
                    "architecture": {}}
        if path == "/agents/loop-inspector/invoke":
            return {"exec_id": "exec_x"}
        if path.startswith("/executions/exec_x"):
            idx = min(state["i"], len(exec_status_sequence) - 1)
            state["i"] += 1
            status = exec_status_sequence[idx]
            if status == "running":
                return {"status": "running"}
            return {"status": status, "output": '{"status": "pass", "issues": []}'}
        return {}
    return router


def test_inspect_without_wait_gives_up_after_fixed_attempts(monkeypatch, fake):
    # 30 "running" responses > the old fixed 20-attempt cap — without -w/--wait,
    # the CLI must still stop around the same ~20-attempt default (unchanged
    # behavior for the default/non-blocking case).
    fake.router = _inspect_router(["running"] * 30)
    sleeps = []
    monkeypatch.setattr(L.time, "sleep", lambda s: sleeps.append(s))
    L._labs_inspect(mkargs(lab="lab_x", role="qa", watch=False, wait=False))
    assert len(sleeps) <= 21  # gave up around the old ~20-attempt ceiling


def test_inspect_with_wait_blocks_past_old_ceiling(monkeypatch, fake):
    # Same 30 "running" responses, but with --wait the CLI must keep polling
    # PAST the old 20-attempt ceiling and pick up the eventual terminal status —
    # this is the exact bug: -w/--wait must actually change behavior.
    fake.router = _inspect_router(["running"] * 25 + ["succeeded"])
    sleeps = []
    monkeypatch.setattr(L.time, "sleep", lambda s: sleeps.append(s))
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    L._labs_inspect(mkargs(lab="lab_x", role="qa", watch=False, wait=True))
    assert len(sleeps) > 20, "with --wait, polling must continue past the old fixed ceiling"
    assert any("Inspeção concluída" in p for p in printed), \
        "with --wait the CLI must have picked up the eventual terminal result, not given up early"


# ── _labs_fix: remediate straight from the backlog, no re-inspection ────────

def _fix_router(backlog, remediate_response=None, loops_response=None):
    calls = {"remediate_body": None}

    def router(method, path, body):
        if path == "/labs-v2":
            return [{"lab_id": "lab_x", "name": "x"}]
        if path == "/labs-v2/lab_x/backlog":
            return backlog
        if path.startswith("/loops?lab_id="):
            return loops_response or [{"loop_id": "loop_fallback", "status": "complete",
                                       "updated_at": "2026-01-01T00:00:00Z"}]
        if method == "POST" and path == "/loops/loop_x/remediate":
            calls["remediate_body"] = body
            return remediate_response or {"issues": len((body or {}).get("issues", []))}
        if method == "POST" and path == "/loops/loop_fallback/remediate":
            calls["remediate_body"] = body
            return remediate_response or {"issues": len((body or {}).get("issues", []))}
        return {}
    return router, calls


def test_fix_empty_backlog_does_not_call_remediate(fake):
    fake.router, calls = _fix_router(backlog=[])
    L._labs_fix(mkargs(lab="lab_x"))
    assert calls["remediate_body"] is None


def test_fix_sends_open_items_from_their_loop_with_fix_text(fake):
    backlog = [
        {"id": "bl_1", "resource": "function:discover-co", "severity": "critical",
         "description": "TypeError on join", "fix": "flatten the list first",
         "loop_id": "loop_x", "status": "open"},
        {"id": "bl_2", "resource": "table:companies", "severity": "medium",
         "description": "zeroed fields", "loop_id": "loop_x", "status": "open"},
        {"id": "bl_3", "resource": "old:thing", "severity": "low",
         "description": "stale item", "loop_id": "loop_x", "status": "done"},  # excluded: done
    ]
    fake.router, calls = _fix_router(backlog=backlog)
    L._labs_fix(mkargs(lab="lab_x"))
    body = calls["remediate_body"]
    assert body is not None
    assert len(body["issues"]) == 2  # only the 2 open items, not the done one
    resources = {i["resource"] for i in body["issues"]}
    assert resources == {"function:discover-co", "table:companies"}
    # The fix text for discover-co must be forwarded so the planner has a
    # concrete action, not just the problem description.
    fixes_by_resource = {f["resource"]: f["action"] for f in body["fixes"]}
    assert fixes_by_resource["function:discover-co"] == "flatten the list first"
    assert "table:companies" not in fixes_by_resource  # no fix text was stored for it


def test_fix_falls_back_to_latest_build_when_backlog_has_no_loop_id(fake):
    # Older backlog items (pre-fix) have no loop_id — must not crash, must
    # resolve the lab's latest completed build instead.
    backlog = [{"id": "bl_1", "resource": "table:x", "severity": "medium",
               "description": "issue", "status": "open"}]  # no loop_id
    fake.router, calls = _fix_router(
        backlog=backlog,
        loops_response=[{"loop_id": "loop_fallback", "status": "complete",
                         "updated_at": "2026-01-01T00:00:00Z"}])
    L._labs_fix(mkargs(lab="lab_x"))
    assert calls["remediate_body"] is not None
    assert len(calls["remediate_body"]["issues"]) == 1


def test_fix_explicit_loop_overrides_backlog_grouping(fake):
    backlog = [{"id": "bl_1", "resource": "table:x", "severity": "medium",
               "description": "issue", "loop_id": "loop_other", "status": "open"}]
    fake.router, calls = _fix_router(backlog=backlog)
    L._labs_fix(mkargs(lab="lab_x", loop="loop_x"))
    # loop_other items are for a different build than the one explicitly
    # requested — must be excluded (skipped), not force-applied to loop_x.
    assert calls["remediate_body"] is None or len(calls["remediate_body"].get("issues", [])) == 0


def test_gates_select_subset():
    g = L._gates_from_args(mkargs(gate="architecture,plan"))
    assert g == {"after_architect": True, "after_planner": True, "before_destructive": False}


def test_gates_mode_human_disables_panel():
    g = L._gates_from_args(mkargs(gate_mode="human"))
    assert g["judges"] == "off"


def test_gates_mode_judge_enables_full_panel():
    g = L._gates_from_args(mkargs(gate_mode="judge"))
    assert g["judges"] == "full"
    assert g["gate_mode"] == "judge"


def test_gates_judge_model_and_max_revise():
    g = L._gates_from_args(mkargs(judges="full", judge_model="fast", max_revise=3))
    assert g["judges"] == "full" and g["judge_model"] == "fast" and g["max_revise"] == 3


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_slug():
    assert L._slug("API REST de Notas!") == "api-rest-de-notas"
    assert L._slug("") == "lab"


def test_trunc():
    assert L._trunc("hello world", 5) == "hell…"
    assert L._trunc(None, 5) == ""


def test_display_status_gate_pending():
    assert L.display_status({"pending_gate": "after_architect", "status": "planning"}) == "awaiting_approval"
    assert L.display_status({"status": "executing"}) == "executing"


def test_strip_ansi():
    assert L._strip_ansi("\033[32mok\033[0m") == "ok"


def test_exit_for_failed_exits_1():
    with pytest.raises(SystemExit) as e:
        L._exit_for({"status": "failed"})
    assert e.value.code == L.EXIT_FAILED
    assert L._exit_for({"status": "complete"}) == L.EXIT_OK


# ── render ───────────────────────────────────────────────────────────────────

def test_render_json(capsys):
    L.render([{"a": 1}], [("A", lambda x: str(x["a"]))], "json")
    import json
    assert json.loads(capsys.readouterr().out) == [{"a": 1}]


def test_render_table(capsys):
    L.render([{"name": "x"}], [("NAME", lambda r: r["name"])], "table")
    out = capsys.readouterr().out
    assert "NAME" in out and "x" in out


# ── _resolve_lab ─────────────────────────────────────────────────────────────

def test_resolve_lab_by_id(fake):
    fake.router = lambda m, p, b: [{"lab_id": "lab_1", "name": "alpha"}]
    assert L._resolve_lab(fake, "lab_1")["lab_id"] == "lab_1"


def test_resolve_lab_by_name_ci(fake):
    fake.router = lambda m, p, b: [{"lab_id": "lab_1", "name": "Alpha"}]
    assert L._resolve_lab(fake, "alpha")["lab_id"] == "lab_1"


def test_resolve_lab_ambiguous_exits_usage(fake):
    fake.router = lambda m, p, b: [{"lab_id": "a", "name": "dup"}, {"lab_id": "b", "name": "dup"}]
    with pytest.raises(SystemExit) as e:
        L._resolve_lab(fake, "dup")
    assert e.value.code == L.EXIT_USAGE


def test_resolve_lab_not_found_exits_usage(fake):
    fake.router = lambda m, p, b: []
    with pytest.raises(SystemExit) as e:
        L._resolve_lab(fake, "ghost")
    assert e.value.code == L.EXIT_USAGE


# ── contract: labs create ────────────────────────────────────────────────────

def test_labs_create_posts_lab_then_loop(fake, capsys):
    def router(m, p, b):
        if p == "/labs-v2":
            return {"lab_id": "lab_X"}
        if p == "/loops":
            return {"loop_id": "loop_Y"}
        return {}
    fake.router = router
    L._labs_create(mkargs(intent="API de notas", name="notes", stack="python,fastapi",
                          auto_approve=True, output="json"))
    paths = [(m, p) for (m, p, _b) in fake.calls]
    assert ("POST", "/labs-v2") in paths and ("POST", "/loops") in paths
    lab_body = next(b for (m, p, b) in fake.calls if p == "/labs-v2")
    assert lab_body == {"name": "notes", "description": "API de notas", "stack": ["python", "fastapi"]}
    loop_body = next(b for (m, p, b) in fake.calls if p == "/loops")
    assert loop_body["intent"] == "API de notas" and loop_body["lab_id"] == "lab_X"
    assert "gates" in loop_body  # auto-approve sends an explicit gate config
    import json
    out = json.loads(capsys.readouterr().out)
    assert out == {"lab_id": "lab_X", "job_id": "loop_Y", "name": "notes"}


# ── contract: loops create / approve / reject ────────────────────────────────

def test_loops_create_requires_intent(fake):
    with pytest.raises(SystemExit) as e:
        L._loops_create(mkargs(intent=None))
    assert e.value.code == L.EXIT_USAGE


def test_loops_create_posts_loop_with_gates(fake):
    fake.router = lambda m, p, b: [{"lab_id": "lab_1", "name": "alpha"}] if p == "/labs-v2" else {"loop_id": "loop_Z"}
    L._loops_create(mkargs(intent="add rate limiting", lab="alpha", gate="plan"))
    loop_body = next(b for (m, p, b) in fake.calls if p == "/loops")
    assert loop_body["intent"] == "add rate limiting"
    assert loop_body["lab_id"] == "lab_1"
    assert loop_body["gates"]["after_planner"] is True
    assert loop_body["gates"]["after_architect"] is False


def test_loops_approve_posts_gate_action(fake):
    fake.router = lambda m, p, b: {"gate": "after_architect"}
    L._loops_approve(mkargs(job_id="loop_1", note="ok"))
    m, p, b = fake.calls[-1]
    assert (m, p) == ("POST", "/loops/loop_1/gate")
    assert b == {"action": "approve", "comment": "ok"}


def test_loops_reject_requires_note(fake):
    with pytest.raises(SystemExit) as e:
        L._loops_reject(mkargs(job_id="loop_1", note=None))
    assert e.value.code == L.EXIT_USAGE


def test_loops_reject_posts_reject_action(fake):
    fake.router = lambda m, p, b: {"gate": "after_planner"}
    L._loops_reject(mkargs(job_id="loop_1", note="use Postgres"))
    m, p, b = fake.calls[-1]
    assert (m, p) == ("POST", "/loops/loop_1/gate")
    assert b == {"action": "reject", "comment": "use Postgres"}


# ── contract: labs list (quiet = ids only) ───────────────────────────────────

def test_labs_list_quiet_prints_ids(fake, capsys):
    fake.router = lambda m, p, b: [{"lab_id": "lab_1"}, {"lab_id": "lab_2"}]
    L._labs_list(mkargs(quiet=True))
    assert capsys.readouterr().out.split() == ["lab_1", "lab_2"]


# ── contract: loops iterate ──────────────────────────────────────────────────

def test_loops_iterate_requires_intent(fake):
    with pytest.raises(SystemExit) as e:
        L._loops_iterate(mkargs(job_id="loop_1", intent=None))
    assert e.value.code == L.EXIT_USAGE


def test_loops_iterate_posts_iteration(fake):
    fake.router = lambda m, p, b: {"loop_id": "loop_2"}
    L._loops_iterate(mkargs(job_id="loop_1", intent="trocar para Redis no rate limit"))
    m, p, b = fake.calls[-1]
    assert (m, p) == ("POST", "/loops/loop_1/iterate")
    assert b == {"intent": "trocar para Redis no rate limit"}


# ── _labs_rebuild: must not require a terminal loop ─────────────────────────
#
# Root cause: `headlabs labs rebuild` refused with "nenhum build concluído
# neste lab para rebuildar" for a lab where every loop had ended up
# cancelled/superseded/awaiting_approval — none complete or failed. The
# rebuild API only needs lab_id + intent (present on ANY loop regardless of
# status), so the CLI's terminal-only filter was an unnecessary dead end.

def test_rebuild_falls_back_to_most_recent_loop_when_none_terminal(monkeypatch):
    def router(method, path, body):
        if path == "/labs-v2":
            return [{"lab_id": "lab_1", "name": "x"}]
        if path == "/labs-v2/lab_1/lineage":
            return [
                {"loop_id": "loop_old", "status": "superseded", "started_at": "2026-01-01T00:00:00"},
                {"loop_id": "loop_new", "status": "cancelled", "started_at": "2026-01-02T00:00:00"},
            ]
        if path == "/loops/loop_new/rebuild":
            return {"loop_id": "loop_rebuilt", "resources_destroyed": 2}
        raise AssertionError(f"unexpected call: {method} {path}")
    client = FakeClient(router)
    monkeypatch.setattr(L, "HeadLabsClient", lambda *a, **k: client)
    L._labs_rebuild(mkargs(intent="recrie tudo", lab="lab_1", auto_approve=True))
    rebuild_calls = [c for c in client.calls if c[1] == "/loops/loop_new/rebuild"]
    assert len(rebuild_calls) == 1, "must call rebuild on the most recent loop when none are terminal"


def test_rebuild_prefers_terminal_loop_when_available(monkeypatch):
    def router(method, path, body):
        if path == "/labs-v2":
            return [{"lab_id": "lab_1", "name": "x"}]
        if path == "/labs-v2/lab_1/lineage":
            return [
                {"loop_id": "loop_complete", "status": "complete", "started_at": "2026-01-01T00:00:00"},
                {"loop_id": "loop_cancelled", "status": "cancelled", "started_at": "2026-01-02T00:00:00"},
            ]
        if path == "/loops/loop_complete/rebuild":
            return {"loop_id": "loop_rebuilt", "resources_destroyed": 2}
        raise AssertionError(f"unexpected call: {method} {path}")
    client = FakeClient(router)
    monkeypatch.setattr(L, "HeadLabsClient", lambda *a, **k: client)
    L._labs_rebuild(mkargs(intent="recrie tudo", lab="lab_1", auto_approve=True))
    rebuild_calls = [c for c in client.calls if c[1] == "/loops/loop_complete/rebuild"]
    assert len(rebuild_calls) == 1, "must prefer the terminal loop as intent source when one exists"


def test_rebuild_dies_when_lab_has_no_loops_at_all(monkeypatch):
    def router(method, path, body):
        if path == "/labs-v2":
            return [{"lab_id": "lab_1", "name": "x"}]
        if path == "/labs-v2/lab_1/lineage":
            return []
        raise AssertionError(f"unexpected call: {method} {path}")
    client = FakeClient(router)
    monkeypatch.setattr(L, "HeadLabsClient", lambda *a, **k: client)
    with pytest.raises(SystemExit) as ei:
        L._labs_rebuild(mkargs(intent="recrie tudo", lab="lab_1", auto_approve=True))
    assert ei.value.code == L.EXIT_USAGE


# ── _follow: "partial" must be recognized as terminal ───────────────────────
#
# Root cause observed live (loop_9122b3da23b0): the loop finished on the
# backend with status="partial" (a deliverer ground-truth guard rejected the
# build, e.g. missing entrypoint or forbidden frontend endpoint — see
# api/routers/loops.py's deliverer callback) at 01:40 UTC, but `headlabs loops
# watch` kept polling for ~14 hours because "partial" was never in _TERMINAL —
# only complete/failed/cancelled were recognized. The user eventually gave up
# and hit Ctrl+C, which _follow's KeyboardInterrupt handler then mislabeled as
# "Cancelado", even though the loop had reached a real terminal state on the
# backend over 13 hours earlier.

def test_follow_recognizes_partial_as_terminal_immediately(monkeypatch):
    calls = {"n": 0}

    def router(method, path, body):
        if path == "/loops/loop_x":
            calls["n"] += 1
            return {"status": "partial", "phase": "done", "mode": "build"}
        return {}
    client = FakeClient(router)
    sleeps = []
    monkeypatch.setattr(L.time, "sleep", lambda s: sleeps.append(s))
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    with pytest.raises(SystemExit) as ei:
        L._follow(client, "loop_x", watch=False, args=mkargs())
    assert ei.value.code == L.EXIT_OK
    assert sleeps == [], "must recognize partial as terminal on the FIRST poll, not keep polling forever"
    assert calls["n"] == 1


def test_follow_does_not_poll_forever_on_partial(monkeypatch):
    # Regression guard for the exact 14h-hang: even if partial were somehow
    # missed on the first poll, it must never poll indefinitely — simulate a
    # long-but-finite sequence and confirm _follow terminates well within it.
    statuses = ["executing"] * 5 + ["partial"]
    state = {"i": 0}

    def router(method, path, body):
        if path == "/loops/loop_x":
            idx = min(state["i"], len(statuses) - 1)
            state["i"] += 1
            return {"status": statuses[idx], "phase": "executor", "mode": "build"}
        return {}
    client = FakeClient(router)
    sleeps = []
    monkeypatch.setattr(L.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(SystemExit) as ei:
        L._follow(client, "loop_x", watch=False, args=mkargs())
    assert ei.value.code == L.EXIT_OK
    assert state["i"] == 6, "must have stopped right after the partial status appeared"


def test_follow_partial_prints_warning_not_generic_success(monkeypatch):
    def router(method, path, body):
        if path == "/loops/loop_x":
            return {"status": "partial", "phase": "done", "mode": "build"}
        return {}
    client = FakeClient(router)
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    with pytest.raises(SystemExit):
        L._follow(client, "loop_x", watch=False, args=mkargs())
    assert any("PARCIALMENTE" in p for p in printed), \
        "user must be told the build only partially passed its guards, not just 'succeeded'"


def test_follow_still_recognizes_complete_and_failed_and_cancelled(monkeypatch):
    for status, expect_code in (("complete", L.EXIT_OK), ("failed", L.EXIT_FAILED), ("cancelled", L.EXIT_OK)):
        def router(method, path, body, _status=status):
            if path == "/loops/loop_x":
                return {"status": _status, "phase": "done", "mode": "build"}
            return {}
        client = FakeClient(router)
        monkeypatch.setattr(L.time, "sleep", lambda s: None)
        with pytest.raises(SystemExit) as ei:
            L._follow(client, "loop_x", watch=False, args=mkargs())
        assert ei.value.code == expect_code, f"status={status} should yield code={expect_code}, got {ei.value.code}"

