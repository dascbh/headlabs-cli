"""Tests for HeadLabsClient.resolve_tenant (tenant from the API key)."""

import headlabs.client as C


def _client():
    return C.HeadLabsClient(api_key="pk_x:sk_y", api_url="http://x")


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


def test_resolves_tenant_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        assert url.endswith("/api-keys/me")
        return _Resp({"tenant": "cactus-gaming", "is_platform": False})

    monkeypatch.setattr(C.requests, "get", fake_get)
    cl = _client()
    assert cl.resolve_tenant() == "cactus-gaming"
    assert cl.resolve_tenant() == "cactus-gaming"   # cached
    assert calls["n"] == 1
    assert cl.resolve_tenant(refresh=True) == "cactus-gaming"
    assert calls["n"] == 2                            # refresh re-queries


def test_empty_tenant_returns_none(monkeypatch):
    monkeypatch.setattr(C.requests, "get",
                        lambda u, **k: _Resp({"tenant": "", "is_platform": True}))
    assert _client().resolve_tenant() is None


def test_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(C.requests, "get", lambda u, **k: _Resp({}, status=403))
    assert _client().resolve_tenant() is None


def test_network_error_returns_none(monkeypatch):
    def boom(u, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(C.requests, "get", boom)
    assert _client().resolve_tenant() is None
