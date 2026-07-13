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


def test_unrecognized_project_raises(tmp_path):
    with pytest.raises(ValueError, match="Could not detect how to serve"):
        detect_run_commands(str(tmp_path))


def test_no_serve_script_raises(tmp_path):
    _write_pkg(tmp_path, scripts={"build": "vite build"}, dev={"vite": "^5"})
    with pytest.raises(ValueError, match="no dev/start/serve/preview script"):
        detect_run_commands(str(tmp_path))


# ── non-Node detection ──────────────────────────────────────────────────────

def test_detect_django(tmp_path):
    (tmp_path / "manage.py").write_text("# django")
    (tmp_path / "requirements.txt").write_text("Django==5.0\n")
    plan = detect_run_commands(str(tmp_path))
    assert plan.framework == "django"
    assert plan.port == 8000
    assert "manage.py runserver 0.0.0.0:8000" in plan.serve_cmd
    assert plan.install_cmd == "pip install -r requirements.txt"


def test_detect_streamlit(tmp_path):
    (tmp_path / "requirements.txt").write_text("streamlit\npandas\n")
    (tmp_path / "app.py").write_text("import streamlit as st")
    plan = detect_run_commands(str(tmp_path))
    assert plan.framework == "streamlit"
    assert plan.port == 8501
    assert plan.serve_cmd.startswith("streamlit run app.py --server.port 8501")


def test_detect_fastapi(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "main.py").write_text("app = ...")
    plan = detect_run_commands(str(tmp_path))
    assert plan.framework == "fastapi"
    assert plan.serve_cmd == "uvicorn main:app --host 0.0.0.0 --port 8000"


def test_detect_flask(tmp_path):
    (tmp_path / "requirements.txt").write_text("Flask==3.0\n")
    (tmp_path / "app.py").write_text("from flask import Flask")
    plan = detect_run_commands(str(tmp_path))
    assert plan.framework == "flask"
    assert plan.port == 5000
    assert plan.serve_cmd == "flask --app app run --host 0.0.0.0 --port 5000"


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    plan = detect_run_commands(str(tmp_path))
    assert plan.framework == "go"
    assert plan.port == 8080
    assert plan.serve_cmd == "go run ."
    assert plan.build_cmd == "go build ./..."


def test_detect_static_site(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    plan = detect_run_commands(str(tmp_path))
    assert plan.framework == "static"
    assert plan.port == 8000
    assert plan.serve_cmd.startswith("python3 -m http.server 8000")


def test_non_node_port_override(tmp_path):
    (tmp_path / "manage.py").write_text("# django")
    plan = detect_run_commands(str(tmp_path), port=9001)
    assert plan.port == 9001
    assert "0.0.0.0:9001" in plan.serve_cmd


def test_node_wins_over_python_when_both_present(tmp_path):
    _write_pkg(tmp_path, scripts={"dev": "vite"}, dev={"vite": "^5"})
    (tmp_path / "requirements.txt").write_text("flask\n")
    plan = detect_run_commands(str(tmp_path))
    assert plan.framework == "vite"  # package.json takes precedence


def test_static_site_served_end_to_end_via_detection(tmp_path):
    # Auto-detected static command actually serves and health-checks.
    (tmp_path / "index.html").write_text("<h1>auto-static</h1>")
    plan = detect_run_commands(str(tmp_path), port=_free_port())
    with ServedApp(plan, do_build=False, startup_timeout=20) as app:
        import urllib.request
        body = urllib.request.urlopen(app.url, timeout=5).read().decode()
        assert "auto-static" in body


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
