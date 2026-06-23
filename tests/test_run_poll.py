"""Tests for invoke / poll / run (mocked — no network or AWS)."""

import io

import headlabs.client as C
from headlabs.progress import ProgressReporter


def _client():
    return C.HeadLabsClient(api_key="pk_x:sk_y", api_url="http://x")


class _Post:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_invoke_returns_exec_tenant_stream(monkeypatch):
    monkeypatch.setattr(C.requests, "post",
                        lambda *a, **k: _Post({"exec_id": "e1", "tenant_id": "t", "root_trace_id": "rt1"}))
    assert _client().invoke("a", {}) == ("e1", "t", "rt1")


def test_invoke_stream_defaults_to_exec_id(monkeypatch):
    monkeypatch.setattr(C.requests, "post", lambda *a, **k: _Post({"exec_id": "e1"}))
    assert _client().invoke("a", {}) == ("e1", "platform", "e1")


def test_poll_renders_events_and_builds_result(monkeypatch):
    monkeypatch.setattr(C.HeadLabsClient, "get_events",
                        lambda self, s, since=0, tenant_id="platform":
                        {"events": [{"type": "tool_use", "tool": "explore_costs"}],
                         "last_seq": 1, "status": "succeeded"})
    monkeypatch.setattr(C.HeadLabsClient, "_get_execution",
                        lambda self, e, t: {"status": "succeeded",
                                            "output": {"summary": "S", "insights": [1, 2],
                                                       "total_saving_usd": 5.0}})
    buf = io.StringIO()
    rep = ProgressReporter(stream=buf)
    res = _client().poll("e1", tenant_id="cactus-gaming", stream_id="e1", reporter=rep)
    assert res.status == "succeeded"
    assert res.total_saving_usd == 5.0
    assert len(res.insights) == 2
    assert "explore_costs" in buf.getvalue()


def test_run_sends_ephemeral_creds_and_no_collected_data(monkeypatch):
    import boto3

    class _STS:
        def get_caller_identity(self):
            return {"Account": "908502692681"}

    class _Sess:
        region_name = "us-east-1"

        def __init__(self, profile_name=None):
            pass

        def client(self, name, **kw):
            return _STS()

    monkeypatch.setattr(boto3, "Session", _Sess)
    monkeypatch.setattr(C, "_ephemeral_credentials",
                        lambda s: {"aws_access_key_id": "ASIA", "aws_secret_access_key": "x",
                                   "aws_session_token": "tok"})
    captured = {}
    monkeypatch.setattr(C.HeadLabsClient, "invoke",
                        lambda self, a, inp: (captured.update(input=inp) or ("e1", "cactus-gaming", "e1")))
    monkeypatch.setattr(C.HeadLabsClient, "poll",
                        lambda self, e, timeout=600, tenant_id="platform", stream_id=None, reporter=None:
                        C.Result(status="succeeded", account_id=""))

    res = _client().run("finops-advisor", "cactus-performance", days=30, question="x")
    inp = captured["input"]
    assert inp["account_id"] == "908502692681"
    assert inp["aws_session_token"] == "tok"
    assert "collected_data" not in inp
    assert res.account_id == "908502692681"
