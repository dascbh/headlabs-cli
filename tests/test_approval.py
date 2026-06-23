"""Tests for the approval-gate flow (forward-compatible CLI side)."""

import io

import headlabs.client as C
from headlabs.progress import ProgressReporter


def _client():
    return C.HeadLabsClient(api_key="pk_x:sk_y", api_url="http://x")


def test_prompt_approval_non_interactive_rejects():
    # StringIO is not a TTY -> fail safe to reject (no unattended mutation).
    rep = ProgressReporter(stream=io.StringIO())
    assert rep.prompt_approval({"action": "eks:UpdateClusterVersion"}) == "reject"


def test_approve_execution_posts_decision(monkeypatch):
    captured = {}

    class _R:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, **kw):
        captured["url"] = url
        captured["json"] = json
        return _R()

    monkeypatch.setattr(C.requests, "post", fake_post)
    ok = _client().approve_execution("e1", "g1", "approve", tenant_id="cactus-gaming")
    assert ok is True
    assert "/executions/e1/approvals/g1" in captured["url"]
    assert "tenant_id=cactus-gaming" in captured["url"]
    assert captured["json"] == {"decision": "approve"}


def test_handle_approval_sends_handler_decision(monkeypatch):
    cl = _client()
    sent = {}
    monkeypatch.setattr(C.HeadLabsClient, "approve_execution",
                        lambda self, e, g, d, t: sent.update(exec=e, gate=g, decision=d, tenant=t) or True)
    ev = {"type": "approval_request", "detail": {"gate_id": "g9", "action": "x"}}
    cl._handle_approval(ev, "e1", "cactus-gaming", lambda detail: "approve")
    assert sent == {"exec": "e1", "gate": "g9", "decision": "approve", "tenant": "cactus-gaming"}


def test_handle_approval_fails_safe_to_reject_without_handler(monkeypatch):
    cl = _client()
    sent = {}
    monkeypatch.setattr(C.HeadLabsClient, "approve_execution",
                        lambda self, e, g, d, t: sent.update(decision=d) or True)
    ev = {"type": "approval_request", "detail": {"gate_id": "g9"}}
    cl._handle_approval(ev, "e1", "t", None)   # no handler
    assert sent["decision"] == "reject"


def test_poll_routes_approval_request_to_handler(monkeypatch):
    cl = _client()
    decisions = []
    # First poll surfaces an approval_request (status awaiting_approval),
    # second poll completes.
    batches = [
        {"events": [{"type": "approval_request", "detail": {"gate_id": "g1", "action": "delete"}}],
         "last_seq": 1, "status": "awaiting_approval"},
        {"events": [], "last_seq": 1, "status": "succeeded"},
    ]
    calls = {"i": 0}

    def fake_events(self, s, since=0, tenant_id="platform"):
        i = min(calls["i"], len(batches) - 1)
        calls["i"] += 1
        return batches[i]

    monkeypatch.setattr(C.HeadLabsClient, "get_events", fake_events)
    monkeypatch.setattr(C.HeadLabsClient, "_get_execution",
                        lambda self, e, t: {"status": "succeeded", "output": {"summary": "ok"}})
    monkeypatch.setattr(C.HeadLabsClient, "approve_execution",
                        lambda self, e, g, d, t: decisions.append((g, d)) or True)

    rep = ProgressReporter(stream=io.StringIO())
    res = cl.poll("e1", tenant_id="t", stream_id="e1", reporter=rep,
                  approval_handler=lambda detail: "approve")
    assert res.status == "succeeded"
    assert decisions == [("g1", "approve")]   # gate routed + approved, then resumed
