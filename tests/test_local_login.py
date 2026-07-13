"""Tests for headlabs.local.login — real form login against a real local server
with a real headless Chromium. Skipped when Chromium isn't available."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from headlabs.local.browser_auth import BrowserAuth
from headlabs.local.browser_probe import run_local_usability_probe
from headlabs.local.login import capture_login, LoginError

USER, PASSWORD = "admin@x.com", "secret"


def _browser_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _browser_available(), reason="headless Chromium not available")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _html(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<!doctype html><html><head><meta charset=utf-8></head><body>{body}</body></html>".encode())

    def do_GET(self):
        if self.path.startswith("/login"):
            self._html('<form method="POST" action="/dologin">'
                       '<input type="email" name="email" placeholder="Email">'
                       '<input type="password" name="password" placeholder="Senha">'
                       '<button type="submit">Entrar</button></form>')
        elif self.path.startswith("/app"):
            if "session=ok" in self.headers.get("Cookie", ""):
                self._html("<h1>welcome authed</h1><img src=x><input type=text>")
            else:
                self.send_response(302); self.send_header("Location", "/login"); self.end_headers()
        elif self.path.startswith("/noform"):
            self._html("<h1>nothing here</h1><p>no login form on this page</p>")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        q = parse_qs(self.rfile.read(n).decode())
        ok = q.get("email", [""])[0] == USER and q.get("password", [""])[0] == PASSWORD
        self.send_response(302)
        if ok:
            self.send_header("Set-Cookie", "session=ok; Path=/")
            self.send_header("Location", "/app")
        else:
            self.send_header("Location", "/login")
        self.end_headers()


@pytest.fixture
def login_server():
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()


def test_capture_login_succeeds_and_returns_session(login_server):
    state, landing = capture_login(f"{login_server}/login", USER, PASSWORD)
    assert landing.endswith("/app"), landing
    names = {c["name"] for c in state.get("cookies", [])}
    assert "session" in names


def test_captured_session_authenticates_the_probe(login_server):
    # Full chain: login → storage_state dict → probe sees the authed page.
    state, landing = capture_login(f"{login_server}/login", USER, PASSWORD)
    auth = BrowserAuth(storage_state=state)          # in-memory dict, no file
    _, mobile = run_local_usability_probe(landing, auth, wait_ms=300)
    assert "welcome authed" in (mobile.get("rendered_text_excerpt") or "")


def test_probe_without_login_is_redirected_to_login(login_server):
    _, mobile = run_local_usability_probe(f"{login_server}/app", None, wait_ms=300)
    txt = mobile.get("rendered_text_excerpt") or ""
    assert "welcome authed" not in txt  # gets bounced to /login (a form, no welcome)


def test_wrong_password_raises(login_server):
    with pytest.raises(LoginError, match="no session|still at"):
        capture_login(f"{login_server}/login", USER, "wrongpass")


def test_missing_fields_raises(login_server):
    # /noform is a plain page with no inputs → fields can't be located.
    with pytest.raises(LoginError, match="username/password fields"):
        capture_login(f"{login_server}/noform", USER, PASSWORD)


def test_storage_state_dict_maps_through():
    d = {"cookies": [{"name": "s", "value": "1", "domain": "x", "path": "/"}], "origins": []}
    auth = BrowserAuth(storage_state=d)
    assert not auth.is_empty()
    assert auth.context_kwargs()["storage_state"] is d
