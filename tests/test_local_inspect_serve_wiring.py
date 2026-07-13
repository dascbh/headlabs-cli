"""Integration tests for the local_cli glue that wires --serve, --auth-* and the
local/remote browser routing together (scenario 3 at the orchestration layer).

Uses a real background http.server (via serve_cmd override) and a real headless
Chromium; the platform heuristic layer is stubbed out with a MagicMock client so
the flow degrades to the deterministic layer, which is what these assert.
"""
import argparse
import socket
from unittest.mock import MagicMock

import pytest

from headlabs.local import backlog as backlog_mod
from headlabs.local_cli import (
    _build_browser_auth, _obtain_browser_signals, _run_usability_platform, _serve_and_inspect,
)
from headlabs.local.serve import is_local_url


def _browser_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


_BAD_HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body><img src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==">
<form><input type="text" name="q"></form></body></html>"""


def _args(**kw):
    base = dict(serve=False, serve_cmd=None, port=None, install=False, no_build=True,
                url=None, auth_storage=None, auth_basic=None, auth_header=None, fix=False)
    base.update(kw)
    return argparse.Namespace(**base)


# ── auth flag assembly ──────────────────────────────────────────────────────

def test_build_browser_auth_none_when_no_flags():
    assert _build_browser_auth(_args()) is None


def test_build_browser_auth_from_flags():
    auth = _build_browser_auth(_args(auth_basic="u:p", auth_header=["A: b"]))
    assert auth is not None
    assert auth.http_credentials == ("u", "p")
    assert auth.extra_http_headers == {"A": "b"}


def test_build_browser_auth_bad_flag_exits(capsys):
    with pytest.raises(SystemExit) as ei:
        _build_browser_auth(_args(auth_basic="nopass"))
    assert ei.value.code == 2


# ── _serve_and_inspect wraps the lifecycle and passes the healthy url ────────

def test_serve_and_inspect_without_serve_passes_url_through():
    got = {}
    _serve_and_inspect(_args(serve=False, url="https://example.com"),
                       ".", lambda url: got.setdefault("url", url))
    assert got["url"] == "https://example.com"


def test_serve_and_inspect_builds_serves_and_tears_down(tmp_path):
    (tmp_path / "index.html").write_text(_BAD_HTML)
    port = _free_port()
    args = _args(serve=True, serve_cmd=f"python3 -m http.server {port} --bind 127.0.0.1",
                 port=port, no_build=True)
    seen = {}

    def run(url):
        seen["url"] = url
        assert is_local_url(url)
        import urllib.request
        assert urllib.request.urlopen(url, timeout=5).status == 200
        return "done"

    result = _serve_and_inspect(args, str(tmp_path), run)
    assert result == "done"
    assert seen["url"] == f"http://localhost:{port}"
    # Server is torn down: the port is free again.
    import urllib.error, urllib.request
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen(f"http://localhost:{port}", timeout=2)


# ── browser-signal routing ──────────────────────────────────────────────────

@pytest.mark.skipif(not _browser_available(), reason="headless Chromium not available")
def test_obtain_browser_signals_local_uses_real_probe(tmp_path):
    (tmp_path / "index.html").write_text(_BAD_HTML)
    port = _free_port()
    from headlabs.local.serve import ServedApp, detect_run_commands
    plan = detect_run_commands(str(tmp_path),
                               serve_cmd=f"python3 -m http.server {port} --bind 127.0.0.1", port=port)
    with ServedApp(plan, do_build=False, startup_timeout=20) as app:
        axe, mobile = _obtain_browser_signals(app.url, None, use_local=True)
    assert axe["violation_count"] >= 1
    assert mobile["accessibility"]["imgs_without_alt"] >= 1


# ── full usability path (deterministic layer) writes to the backlog ─────────

@pytest.mark.skipif(not _browser_available(), reason="headless Chromium not available")
def test_run_usability_platform_local_writes_findings(tmp_path):
    (tmp_path / "index.html").write_text(_BAD_HTML)
    port = _free_port()
    from headlabs.local.serve import ServedApp, detect_run_commands
    plan = detect_run_commands(str(tmp_path),
                               serve_cmd=f"python3 -m http.server {port} --bind 127.0.0.1", port=port)

    client = MagicMock()  # heuristic layer will fail to unpack invoke() → deterministic-only
    with ServedApp(plan, do_build=False, startup_timeout=20) as app:
        _run_usability_platform(client, str(tmp_path), app.url, None, _args(),
                                auth=None, use_local=True)

    items = backlog_mod.load_backlog(str(tmp_path))
    assert items, "expected deterministic usability findings in the backlog"
    assert any(str(i.get("resource", "")).startswith("wcag:")
               or "WCAG" in (i.get("title", "")) for i in items)
