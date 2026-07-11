"""Unit tests for headlabs.local.tools.web_search — Brave Search integration.

All AWS Secrets Manager and HTTP calls are mocked; no real network or AWS
credentials are used, keeping this test suite fast and hermetic.
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from headlabs.local.tools import web_search as web_search_module
from headlabs.local.tools.web_search import WebSearchTool, _get_brave_api_key


@pytest.fixture(autouse=True)
def _clear_api_key_cache():
    """The API key getter is process-cached via lru_cache; clear it between
    tests so mocked boto3 calls in one test don't leak into another."""
    _get_brave_api_key.cache_clear()
    yield
    _get_brave_api_key.cache_clear()


def _mock_secret_client(secret_string: str) -> MagicMock:
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": secret_string}
    return client


def test_get_brave_api_key_plain_string():
    with patch("boto3.client", return_value=_mock_secret_client("sk-brave-abc123")):
        assert _get_brave_api_key() == "sk-brave-abc123"


def test_get_brave_api_key_json_with_known_field():
    with patch("boto3.client", return_value=_mock_secret_client('{"api_key": "sk-brave-xyz"}')):
        assert _get_brave_api_key() == "sk-brave-xyz"


def test_get_brave_api_key_json_single_key_fallback():
    with patch("boto3.client", return_value=_mock_secret_client('{"some_odd_key": "sk-brave-123"}')):
        assert _get_brave_api_key() == "sk-brave-123"


def test_get_brave_api_key_json_unrecognized_multi_key_raises():
    with patch("boto3.client", return_value=_mock_secret_client('{"foo": "1", "bar": "2"}')):
        with pytest.raises(ValueError, match="unrecognized keys"):
            _get_brave_api_key()


def test_get_brave_api_key_uses_correct_secret_id_and_region():
    client = _mock_secret_client("sk-brave-abc")
    with patch("boto3.client", return_value=client) as boto3_client:
        _get_brave_api_key()
        boto3_client.assert_called_once_with("secretsmanager", region_name="us-east-1")
        client.get_secret_value.assert_called_once_with(SecretId="headlabs/brave-search-api-key")


def test_web_search_is_read_only_and_never_needs_permission():
    assert WebSearchTool.is_read_only() is True
    assert WebSearchTool.requires_permission({"query": "x"}) is False


def test_web_search_secrets_manager_failure_returns_graceful_error(tmp_path):
    with patch("boto3.client", side_effect=RuntimeError("no credentials")):
        result = WebSearchTool().execute({"query": "test"}, cwd=str(tmp_path))
    assert result.is_error
    assert "Brave Search API key" in result.output
    assert "no credentials" in result.output


def test_web_search_success_formats_results(tmp_path):
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {
        "web": {
            "results": [
                {"title": "Result One", "url": "https://a.example", "description": "First result"},
                {"title": "Result Two", "url": "https://b.example", "description": "Second result"},
            ]
        }
    }
    with patch("boto3.client", return_value=_mock_secret_client("sk-brave-abc")), \
         patch("httpx.get", return_value=fake_response) as mock_get:
        result = WebSearchTool().execute({"query": "test query", "count": 2}, cwd=str(tmp_path))

    assert not result.is_error
    assert "Result One" in result.output
    assert "https://a.example" in result.output
    assert "Result Two" in result.output
    # Confirm the API key was sent as the subscription token header, not logged/leaked elsewhere.
    _, kwargs = mock_get.call_args
    assert kwargs["headers"]["X-Subscription-Token"] == "sk-brave-abc"
    assert kwargs["params"]["q"] == "test query"
    assert kwargs["params"]["count"] == 2


def test_web_search_no_results():
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"web": {"results": []}}
    with patch("boto3.client", return_value=_mock_secret_client("sk-brave-abc")), \
         patch("httpx.get", return_value=fake_response):
        result = WebSearchTool().execute({"query": "nonexistent query xyz"}, cwd=".")
    assert not result.is_error
    assert "No results found" in result.output


def test_web_search_http_error_status():
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = "Unauthorized"
    error = httpx.HTTPStatusError("401", request=MagicMock(), response=fake_response)
    fake_response.raise_for_status.side_effect = error
    with patch("boto3.client", return_value=_mock_secret_client("sk-brave-bad")), \
         patch("httpx.get", return_value=fake_response):
        result = WebSearchTool().execute({"query": "test"}, cwd=".")
    assert result.is_error
    assert "401" in result.output


def test_web_search_transport_error():
    with patch("boto3.client", return_value=_mock_secret_client("sk-brave-abc")), \
         patch("httpx.get", side_effect=httpx.ConnectError("connection refused")):
        result = WebSearchTool().execute({"query": "test"}, cwd=".")
    assert result.is_error
    assert "Failed to reach Brave Search API" in result.output


def test_web_search_count_clamped_to_max():
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"web": {"results": []}}
    with patch("boto3.client", return_value=_mock_secret_client("sk-brave-abc")), \
         patch("httpx.get", return_value=fake_response) as mock_get:
        WebSearchTool().execute({"query": "test", "count": 999}, cwd=".")
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["count"] == web_search_module.MAX_RESULTS


def test_web_search_schema_shape():
    schema = WebSearchTool.to_api_schema()
    assert schema["name"] == "web_search"
    assert "query" in schema["input_schema"]["properties"]
    assert schema["input_schema"]["required"] == ["query"]


def test_api_key_is_cached_across_calls():
    """The lru_cache means boto3.client should only be invoked once even
    across multiple calls within the same process."""
    client = _mock_secret_client("sk-brave-cached")
    with patch("boto3.client", return_value=client) as boto3_client:
        _get_brave_api_key()
        _get_brave_api_key()
        _get_brave_api_key()
    assert boto3_client.call_count == 1
