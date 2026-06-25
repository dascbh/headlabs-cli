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
