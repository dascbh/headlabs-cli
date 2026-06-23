"""Tests for chat_stream robustness (partial handling, 404 tenant logic)."""

import time

import requests

import headlabs.client as C


def _client():
    return C.HeadLabsClient(api_key="pk_x:sk_y", api_url="http://x")


class _Post:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _mock_post(monkeypatch, body):
    monkeypatch.setattr(C.requests, "post", lambda *a, **k: _Post(body))


def _http_404():
    resp = requests.Response()
    resp.status_code = 404
    return requests.HTTPError(response=resp)


def test_chat_succeeds_and_yields_answer(monkeypatch):
    _mock_post(monkeypatch, {"exec_id": "e1", "tenant_id": "cactus-gaming", "status": "running"})
    monkeypatch.setattr(C.HeadLabsClient, "get_events",
                        lambda self, s, since=0, tenant_id="platform":
                        {"events": [{"type": "tool_use", "tool": "t"}], "last_seq": 1, "status": "succeeded"})
    monkeypatch.setattr(C.HeadLabsClient, "_get_execution",
                        lambda self, e, t: {"status": "succeeded", "output": {"answer": "resposta"}})
    out = list(_client().chat_stream("a", "s", "m"))
    types = [e["type"] for e in out]
    assert "progress" in types and "done" in types
    assert next(e["message"] for e in out if e["type"] == "done") == "resposta"


def test_chat_partial_is_terminal(monkeypatch):
    _mock_post(monkeypatch, {"exec_id": "e1", "tenant_id": "t", "status": "running"})
    monkeypatch.setattr(C.HeadLabsClient, "get_events",
                        lambda self, s, since=0, tenant_id="platform":
                        {"events": [], "last_seq": 0, "status": "partial"})
    monkeypatch.setattr(C.HeadLabsClient, "_get_execution",
                        lambda self, e, t: {"status": "partial", "output": {"answer": "parcial"}})
    out = list(_client().chat_stream("a", "s", "m"))
    assert any(e["type"] == "done" and e["message"] == "parcial" for e in out)


def test_chat_404_fast_fails_with_clear_message(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    _mock_post(monkeypatch, {"exec_id": "e1", "tenant_id": "cactus-gaming", "status": "running"})

    def boom(self, s, since=0, tenant_id="platform"):
        raise _http_404()
    monkeypatch.setattr(C.HeadLabsClient, "get_events", boom)
    # resolve_tenant returns the same tenant -> no recovery -> fast fail
    monkeypatch.setattr(C.HeadLabsClient, "resolve_tenant", lambda self, refresh=False: "cactus-gaming")
    out = list(_client().chat_stream("a", "s", "m"))
    assert any(e["type"] == "error" and "não encontrada no tenant" in e["error"] for e in out)


def test_chat_404_recovers_by_reresolving_tenant(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    # initial poll tenant comes from the response ("platform"); the real tenant
    # is cactus-gaming, which resolve_tenant(refresh=True) provides.
    _mock_post(monkeypatch, {"exec_id": "e1", "tenant_id": "platform", "status": "running"})

    def ev(self, s, since=0, tenant_id="platform"):
        if tenant_id == "platform":
            raise _http_404()
        return {"events": [], "last_seq": 0, "status": "succeeded"}
    monkeypatch.setattr(C.HeadLabsClient, "get_events", ev)
    monkeypatch.setattr(C.HeadLabsClient, "resolve_tenant", lambda self, refresh=False: "cactus-gaming")
    monkeypatch.setattr(C.HeadLabsClient, "_get_execution",
                        lambda self, e, t: {"status": "succeeded", "output": {"answer": "ok"}})
    out = list(_client().chat_stream("a", "s", "m"))
    assert any(e["type"] == "done" and e["message"] == "ok" for e in out)
