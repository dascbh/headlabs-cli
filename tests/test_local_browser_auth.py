"""Unit tests for headlabs.local.browser_auth — CLI parsing and the mapping to
Playwright new_context kwargs. No browser involved."""
import json

import pytest

from headlabs.local.browser_auth import BrowserAuth


def test_empty_by_default():
    a = BrowserAuth()
    assert a.is_empty()
    assert a.context_kwargs() == {}


def test_from_cli_none():
    a = BrowserAuth.from_cli()
    assert a.is_empty()


def test_basic_auth_parses_and_maps():
    a = BrowserAuth.from_cli(basic="admin:s3cret")
    assert not a.is_empty()
    assert a.http_credentials == ("admin", "s3cret")
    assert a.context_kwargs() == {"http_credentials": {"username": "admin", "password": "s3cret"}}


def test_basic_auth_password_may_contain_colon():
    a = BrowserAuth.from_cli(basic="user:pa:ss:word")
    assert a.http_credentials == ("user", "pa:ss:word")


def test_basic_auth_without_colon_is_error():
    with pytest.raises(ValueError, match="user:password"):
        BrowserAuth.from_cli(basic="nopassword")


def test_headers_parse_and_strip():
    a = BrowserAuth.from_cli(headers=["Authorization: Bearer abc", "X-Env:staging"])
    assert a.extra_http_headers == {"Authorization": "Bearer abc", "X-Env": "staging"}
    assert a.context_kwargs()["extra_http_headers"]["Authorization"] == "Bearer abc"


def test_header_without_colon_is_error():
    with pytest.raises(ValueError, match="Key: Value"):
        BrowserAuth.from_cli(headers=["BadHeader"])


def test_header_with_empty_key_is_error():
    with pytest.raises(ValueError, match="empty key"):
        BrowserAuth.from_cli(headers=[": value"])


def test_storage_state_maps_when_file_exists(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [], "origins": []}))
    a = BrowserAuth.from_cli(storage=str(state))
    assert not a.is_empty()
    assert a.context_kwargs() == {"storage_state": str(state)}


def test_storage_state_missing_file_raises_on_kwargs():
    a = BrowserAuth(storage_state="/no/such/state.json")
    with pytest.raises(ValueError, match="storage_state file not found"):
        a.context_kwargs()


def test_all_three_combined():
    a = BrowserAuth.from_cli(basic="u:p", headers=["A: b"])
    kw = a.context_kwargs()
    assert kw["http_credentials"] == {"username": "u", "password": "p"}
    assert kw["extra_http_headers"] == {"A": "b"}
