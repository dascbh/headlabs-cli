"""Tests for agent name resolution, validation, and suggestions."""

import json

import pytest

import headlabs.agentnames as A


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the cache at a tmp dir so we don't touch the real ~/.headlabs."""
    monkeypatch.setattr(A, "_CACHE", tmp_path / "agents_cache.json")
    monkeypatch.setattr(A, "CONFIG_DIR", tmp_path)
    return tmp_path


class FakeClient:
    def __init__(self, ids=None, fail=False):
        self._ids = ids or ["finops-advisor", "threat-detector", "compliance-auditor"]
        self._fail = fail

    def list_remote_agents(self, timeout=15):
        if self._fail:
            raise RuntimeError("network down")
        return [{"id": i} for i in self._ids]


def test_registry_alias_resolves_without_network(isolated):
    # finops alias → agent_id; a failing client proves no network is needed.
    assert A.resolve_agent_id(FakeClient(fail=True), "finops", kind="run") == "finops-advisor"


def test_registry_alias_chat_kind(isolated):
    assert A.resolve_agent_id(FakeClient(fail=True), "finops", kind="chat") == "finops-advisor"


def test_known_agent_id_passes_through_without_network(isolated):
    assert A.resolve_agent_id(FakeClient(fail=True), "threat-detector") == "threat-detector"


def test_remote_id_validates_and_caches(isolated):
    # compliance-auditor isn't a registry value's exact match? it is (compliance alias).
    # Use a purely-remote id instead.
    c = FakeClient(ids=["custom-agent-x"])
    assert A.resolve_agent_id(c, "custom-agent-x") == "custom-agent-x"
    # cache now contains it (offline catalog sees it)
    assert "custom-agent-x" in A.catalog_names()


def test_typo_raises_with_suggestions(isolated):
    with pytest.raises(A.AgentNotFound) as ei:
        A.resolve_agent_id(FakeClient(), "finopadvisor")
    assert "finops-advisor" in ei.value.suggestions


def test_unknown_with_offline_platform_is_best_effort(isolated):
    # Platform unreachable and no cache → don't block; return as-is.
    assert A.resolve_agent_id(FakeClient(fail=True), "mystery", validate=True) == "mystery"


def test_validate_false_skips_network(isolated):
    assert A.resolve_agent_id(FakeClient(fail=True), "whatever", validate=False) == "whatever"


def test_suggest_substring_first(isolated):
    names = ["finops-advisor", "finops", "threat-detector"]
    out = A.suggest("finopadvisor", names)
    assert out[0] == "finops-advisor"


def test_suggest_empty_pool():
    assert A.suggest("x", []) == []


def test_catalog_includes_registry_and_cache(isolated):
    A._write_cache(["remote-only-agent"])
    names = A.catalog_names()
    assert "finops" in names                 # registry alias
    assert "finops-advisor" in names          # registry id
    assert "remote-only-agent" in names       # cached remote id


def test_refresh_catalog_writes_cache(isolated):
    ids = A.refresh_catalog(FakeClient(ids=["a", "b"]))
    assert set(ids) == {"a", "b"}
    assert set(A._read_cache()[0]) == {"a", "b"}


def test_refresh_catalog_falls_back_to_cache_on_failure(isolated):
    A._write_cache(["cached-agent"])
    ids = A.refresh_catalog(FakeClient(fail=True))
    assert ids == ["cached-agent"]


def test_live_catalog_fetches_fresh_and_includes_new_agents(isolated):
    # A newly-published agent not in the local registry shows up live.
    c = FakeClient(ids=["finops-advisor", "brand-new-agent"])
    names = A.live_catalog(c, timeout=3)
    assert "brand-new-agent" in names
    assert "finops" in names  # local registry merged in
    # and it updated the cache
    assert "brand-new-agent" in A._read_cache()[0]


def test_live_catalog_passes_short_timeout(isolated):
    seen = {}

    class C2:
        def list_remote_agents(self, timeout=15):
            seen["timeout"] = timeout
            return [{"id": "x"}]

    A.live_catalog(C2(), timeout=2)
    assert seen["timeout"] == 2


def test_live_catalog_falls_back_when_platform_down(isolated):
    A._write_cache(["cached-agent"])
    names = A.live_catalog(FakeClient(fail=True), timeout=1)
    assert "cached-agent" in names
    assert "finops" in names  # registry still present
