"""Tests for headlabs.local.serve — detection (pure) and the ServedApp
lifecycle (real background process). No browser involved."""
import json
import os
import socket
import time

import pytest

from headlabs.local.serve import (
    RunPlan, ServedApp, ServeError, detect_run_commands, is_local_url, _scan_url,
)


# ── is_local_url ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("http://localhost:5173", True),
    ("http://127.0.0.1:3000/path", True),
    ("http://0.0.0.0:8080", True),
    ("https://[::1]:4000", True),
    ("https://example.com", False),
    ("https://app.staging.acme.io/login", False),
    ("", False),
    (None, False),
])
def test_is_local_url(url, expected):
    assert is_local_url(url) is expected


# ── _scan_url ───────────────────────────────────────────────────────────────

def test_scan_url_vite_line():
    assert _scan_url("  ➜  Local:   http://localhost:5173/") == "http://localhost:5173/"


def test_scan_url_next_line():
    assert _scan_url("- Local:        http://localhost:3000") == "http://localhost:3000"


def test_scan_url_none_when_absent():
    assert _scan_url("compiling client and server...") is None


# ── detect_run_commands ─────────────────────────────────────────────────────

def _write_pkg(d, scripts=None, deps=None, dev=None):
    (d / "package.json").write_text(json.dumps({
        "name": "x", "scripts": scripts or {},
        "dependencies": deps or {}, "devDependencies": dev or {},
    }))


def test_detect_vite_npm(tmp_path):
    _write_pkg(tmp_path, scripts={"dev": "vite", "build": "vite build"}, dev={"vite": "^5"})
    plan = detect_run_commands(str(tmp_path))
    assert plan.serve_cmd == "npm run dev"
    assert plan.build_cmd == "npm run build"
    assert plan.install_cmd == "npm install"
    assert plan.port == 5173
    assert plan.framework == "vite"
    assert plan.url == "http://localhost:5173"


def test_detect_next_pnpm(tmp_path):
    _write_pkg(tmp_path, scripts={"dev": "next dev", "build": "next build"}, deps={"next": "14"})
    (tmp_path / "pnpm-lock.yaml").write_text("")
    plan = detect_run_commands(str(tmp_path))
    assert plan.manager == "pnpm"
    assert plan.serve_cmd == "pnpm dev"
    assert plan.build_cmd == "pnpm build"
    assert plan.port == 3000


def test_detect_cra_yarn(tmp_path):
    _write_pkg(tmp_path, scripts={"start": "react-scripts start"}, deps={"react-scripts": "5"})
    (tmp_path / "yarn.lock").write_text("")
    plan = detect_run_commands(str(tmp_path))
    assert plan.manager == "yarn"
    assert plan.serve_cmd == "yarn start"       # no "dev", falls back to "start"
    assert plan.build_cmd is None               # no build script
    assert plan.port == 3000


def test_detect_port_override(tmp_path):
    _write_pkg(tmp_path, scripts={"dev": "vite"}, dev={"vite": "^5"})
    plan = detect_run_commands(str(tmp_path), port=9999)
    assert plan.port == 9999


def test_detect_no_build_flag(tmp_path):
    _write_pkg(tmp_path, scripts={"dev": "vite", "build": "vite build"}, dev={"vite": "^5"})
    plan = detect_run_commands(str(tmp_path), build=False)
    assert plan.build_cmd is None


def test_serve_cmd_override_without_package_json(tmp_path):
    plan = detect_run_commands(str(tmp_path), serve_cmd="python -m http.server 8000", port=8000)
    assert plan.serve_cmd == "python -m http.server 8000"
    assert plan.port == 8000
    assert plan.framework == "custom"


def test_no_package_json_raises(tmp_path):
    with pytest.raises(ValueError, match="No package.json"):
        detect_run_commands(str(tmp_path))


def test_no_serve_script_raises(tmp_path):
    _write_pkg(tmp_path, scripts={"build": "vite build"}, dev={"vite": "^5"})
    with pytest.raises(ValueError, match="no dev/start/serve/preview script"):
        detect_run_commands(str(tmp_path))


# ── ServedApp lifecycle (real background process) ───────────────────────────

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_servedapp_starts_healthchecks_and_tears_down(tmp_path):
    (tmp_path / "index.html").write_text("<h1>served</h1>")
    port = _free_port()
    # Use a real long-running server via serve_cmd override (no package.json needed).
    plan = detect_run_commands(str(tmp_path),
                               serve_cmd=f"python3 -m http.server {port} --bind 127.0.0.1",
                               port=port)
    logs = []
    pid = None
    with ServedApp(plan, do_build=False, startup_timeout=20, log_cb=logs.append) as app:
        assert app.url == f"http://localhost:{port}"
        pid = app._proc.pid
        # The server actually answers.
        import urllib.request
        body = urllib.request.urlopen(app.url, timeout=5).read().decode()
        assert "served" in body
    # After the context exits, the process group is gone.
    assert pid is not None
    _assert_process_dead(pid)
    assert any("pronto" in m for m in logs)


def _assert_process_dead(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # dead
        time.sleep(0.1)
    pytest.fail(f"process {pid} still alive after teardown")


def test_servedapp_raises_when_server_exits_early(tmp_path):
    # A command that exits immediately (never serves) must surface as ServeError.
    plan = detect_run_commands(str(tmp_path),
                               serve_cmd="python3 -c \"import sys; sys.exit(1)\"",
                               port=_free_port())
    with pytest.raises(ServeError, match="exited early"):
        with ServedApp(plan, do_build=False, startup_timeout=10):
            pass


def test_servedapp_build_failure_raises(tmp_path):
    (tmp_path / "index.html").write_text("<h1>x</h1>")
    plan = RunPlan(serve_cmd=f"python3 -m http.server {_free_port()}",
                   port=_free_port(), cwd=str(tmp_path),
                   build_cmd="python3 -c \"import sys; sys.exit(3)\"")
    with pytest.raises(ServeError, match="build failed"):
        with ServedApp(plan, do_build=True, startup_timeout=5):
            pass
