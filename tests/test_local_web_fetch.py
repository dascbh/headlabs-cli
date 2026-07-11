"""Unit tests for headlabs.local.tools.web_fetch — HTTP mocked, no real network."""
from unittest.mock import MagicMock, patch

import httpx

from headlabs.local.tools.web_fetch import WebFetchTool, _html_to_text


def _mock_response(text: str, content_type: str = "text/html", status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"content-type": content_type}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


def test_html_to_text_strips_tags():
    html = "<html><body><h1>Title</h1><p>Some text</p></body></html>"
    text = _html_to_text(html)
    assert "Title" in text
    assert "Some text" in text
    assert "<h1>" not in text


def test_html_to_text_strips_scripts_and_styles():
    html = "<html><script>alert(1)</script><style>body{color:red}</style><p>Real content</p></html>"
    text = _html_to_text(html)
    assert "alert" not in text
    assert "color:red" not in text
    assert "Real content" in text


def test_html_to_text_decodes_entities():
    html = "<p>A &amp; B &lt;tag&gt; &quot;quoted&quot;</p>"
    text = _html_to_text(html)
    assert "A & B" in text
    assert "<tag>" in text
    assert '"quoted"' in text


def test_web_fetch_html_success():
    with patch("httpx.get", return_value=_mock_response("<h1>Hello</h1><p>World</p>")):
        result = WebFetchTool().execute({"url": "https://example.com"}, cwd=".")
    assert not result.is_error
    assert "Hello" in result.output
    assert "World" in result.output


def test_web_fetch_adds_https_scheme_if_missing():
    with patch("httpx.get", return_value=_mock_response("<p>ok</p>")) as mock_get:
        WebFetchTool().execute({"url": "example.com"}, cwd=".")
    args, _ = mock_get.call_args
    assert args[0] == "https://example.com"


def test_web_fetch_json_passthrough():
    with patch("httpx.get", return_value=_mock_response('{"key": "value"}', content_type="application/json")):
        result = WebFetchTool().execute({"url": "https://api.example.com"}, cwd=".")
    assert not result.is_error
    assert '"key": "value"' in result.output


def test_web_fetch_unsupported_content_type():
    with patch("httpx.get", return_value=_mock_response("binarydata", content_type="application/octet-stream")):
        result = WebFetchTool().execute({"url": "https://example.com/file.bin"}, cwd=".")
    assert result.is_error
    assert "Unsupported content type" in result.output


def test_web_fetch_http_error_status():
    with patch("httpx.get", return_value=_mock_response("not found", status_code=404)):
        result = WebFetchTool().execute({"url": "https://example.com/missing"}, cwd=".")
    assert result.is_error
    assert "404" in result.output


def test_web_fetch_transport_error():
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        result = WebFetchTool().execute({"url": "https://unreachable.example"}, cwd=".")
    assert result.is_error
    assert "Failed to fetch" in result.output


def test_web_fetch_truncates_long_content():
    long_html = "<p>" + ("x" * 20_000) + "</p>"
    with patch("httpx.get", return_value=_mock_response(long_html)):
        result = WebFetchTool().execute({"url": "https://example.com"}, cwd=".")
    assert "truncated" in result.output
    assert len(result.output) < 20_000


def test_web_fetch_empty_body():
    with patch("httpx.get", return_value=_mock_response("")):
        result = WebFetchTool().execute({"url": "https://example.com"}, cwd=".")
    assert not result.is_error
    assert "empty response" in result.output.lower()


def test_web_fetch_never_requires_permission():
    assert WebFetchTool.requires_permission({}) is False
    assert WebFetchTool.is_read_only() is True
