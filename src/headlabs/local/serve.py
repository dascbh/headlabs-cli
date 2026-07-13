"""Local app lifecycle runner for `headlabs local inspect --serve`.

`headlabs local` inspects a *running* front-end via ``--url``, but until now the
user had to build and start that server themselves. This module closes scenario
3 (build → run → access → test): detect how a project builds and serves itself,
start the dev server as a background process, wait until it actually answers,
hand the URL to the inspector, and — always — tear the server (and its child
processes) down afterward.

Two independent concerns live here:

- :func:`detect_run_commands` — a pure, no-side-effect reader of ``package.json``
  (and lockfiles) that returns a :class:`RunPlan`. Fully unit-testable.
- :class:`ServedApp` — a context manager that executes a :class:`RunPlan`:
  optional ``install`` and ``build`` (blocking), then the dev server via
  ``Popen`` in its OWN process group so teardown can kill the whole tree
  (npm → node → esbuild/vite children), with a real HTTP health-check before
  yielding.

Why a new module instead of reusing ``bash``/``run_test_command``: both of those
use blocking ``subprocess.run`` — a dev server would hang them until timeout.
Serving needs a long-lived background process with explicit teardown, which is a
different lifecycle.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# Framework → conventional dev-server port. Used when we can't read a port off
# the server's own stdout (which, when present, always wins).
_FRAMEWORK_PORTS = {
    "next": 3000,
    "vite": 5173,
    "react-scripts": 3000,
    "@angular/cli": 4200,
    "@vue/cli-service": 8080,
    "vue-cli-service": 8080,
    "nuxt": 3000,
    "astro": 4321,
    "svelte": 5173,
    "@sveltejs/kit": 5173,
    "gatsby": 8000,
    "remix": 3000,
}
_DEFAULT_PORT = 3000

# Preference order for the "run the dev server" script in package.json.
_SERVE_SCRIPT_ORDER = ("dev", "start", "serve", "preview")

STARTUP_TIMEOUT_S = 90          # a cold `next dev` / first `vite` build can be slow
BUILD_TIMEOUT_S = 600
INSTALL_TIMEOUT_S = 600
_HEALTH_POLL_INTERVAL_S = 0.5
_MAX_LOG_LINES = 400            # ring buffer of server stdout kept for diagnostics
_URL_RE = None  # compiled lazily in _scan_url


def is_local_url(url: str | None) -> bool:
    """True if ``url`` points at the local machine (localhost / 127.0.0.1 / ::1
    / 0.0.0.0). Such URLs are unreachable from the remote browser MCP, so the
    inspector must drive a local browser for them."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


@dataclass
class RunPlan:
    """How to build and serve a project. Produced by :func:`detect_run_commands`."""

    serve_cmd: str
    port: int
    cwd: str
    build_cmd: str | None = None
    install_cmd: str | None = None
    framework: str = "unknown"
    manager: str = "npm"

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}"


def _detect_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists():
        return "bun"
    return "npm"


def _install_cmd(manager: str) -> str:
    # Prefer reproducible/frozen installs where the manager supports them.
    return {
        "pnpm": "pnpm install",
        "yarn": "yarn install",
        "bun": "bun install",
    }.get(manager, "npm install")


def _run_script_cmd(manager: str, script: str) -> str:
    # `npm run <s>`, `pnpm <s>`, `yarn <s>`, `bun run <s>`.
    if manager == "npm":
        return f"npm run {script}"
    if manager == "bun":
        return f"bun run {script}"
    return f"{manager} {script}"


def _detect_framework_port(pkg: dict) -> tuple[str, int]:
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    for name, port in _FRAMEWORK_PORTS.items():
        if name in deps:
            return name, port
    return "unknown", _DEFAULT_PORT


def detect_run_commands(cwd: str, *, serve_cmd: str | None = None,
                        port: int | None = None,
                        build: bool = True) -> RunPlan:
    """Read a project directory and decide how to install, build and serve it.

    ``serve_cmd`` / ``port`` are explicit overrides (from CLI flags) that skip
    detection for that field. Raises ``ValueError`` when the project can't be
    served automatically and no ``serve_cmd`` override was given.
    """
    root = Path(cwd)
    manager = _detect_manager(root)
    pkg_path = root / "package.json"

    # Explicit override: trust the user's command; still detect a port default.
    if serve_cmd:
        framework, default_port = "custom", _DEFAULT_PORT
        if pkg_path.exists():
            try:
                framework, default_port = _detect_framework_port(json.loads(pkg_path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        return RunPlan(serve_cmd=serve_cmd, port=port or default_port, cwd=str(root),
                       framework=framework, manager=manager)

    if not pkg_path.exists():
        raise ValueError(
            f"No package.json in {root} — cannot auto-serve. "
            f"Pass --serve-cmd '<command>' and --port <n> to serve a non-Node project."
        )
    try:
        pkg = json.loads(pkg_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Could not read package.json: {exc}") from exc

    scripts = pkg.get("scripts", {}) or {}
    serve_script = next((s for s in _SERVE_SCRIPT_ORDER if scripts.get(s)), None)
    if not serve_script:
        raise ValueError(
            f"package.json has no {'/'.join(_SERVE_SCRIPT_ORDER)} script — "
            f"pass --serve-cmd '<command>' to say how to start the server."
        )

    framework, default_port = _detect_framework_port(pkg)
    build_cmd = _run_script_cmd(manager, "build") if (build and scripts.get("build")) else None

    return RunPlan(
        serve_cmd=_run_script_cmd(manager, serve_script),
        port=port or default_port,
        cwd=str(root),
        build_cmd=build_cmd,
        install_cmd=_install_cmd(manager),
        framework=framework,
        manager=manager,
    )


def _scan_url(line: str) -> str | None:
    """Extract the first ``http://…:port`` URL a dev server prints (Vite prints
    ``Local:   http://localhost:5173/``; Next prints ``- Local: http://…``)."""
    global _URL_RE
    if _URL_RE is None:
        import re
        _URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?[^\s]*")
    m = _URL_RE.search(line)
    return m.group(0) if m else None


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    """True if the URL answers with any non-5xx HTTP status (a 200/301/404 all
    mean 'the server is up and serving')."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500          # 4xx == server is up
    except (urllib.error.URLError, OSError, ValueError):
        return False


class ServeError(RuntimeError):
    """Raised when the app cannot be installed/built/started."""


class ServedApp:
    """Context manager that builds and serves a project, exposing ``.url``.

    On ``__enter__``: optional install, optional build, then the dev server as a
    background process group, health-checked until it answers. On ``__exit__``:
    the whole process group is terminated (SIGTERM → SIGKILL fallback), always.
    """

    def __init__(self, plan: RunPlan, *, do_install: bool = False, do_build: bool = True,
                 startup_timeout: float = STARTUP_TIMEOUT_S, env: dict | None = None,
                 log_cb=None):
        self.plan = plan
        self.do_install = do_install
        self.do_build = do_build
        self.startup_timeout = startup_timeout
        self.env = env
        self.log_cb = log_cb or (lambda _m: None)
        self.url: str = plan.url
        self._proc: subprocess.Popen | None = None
        self._log: deque[str] = deque(maxlen=_MAX_LOG_LINES)
        self._reader: threading.Thread | None = None
        self._discovered_url: str | None = None
        self._lock = threading.Lock()

    # -- blocking phases --------------------------------------------------

    def _run_blocking(self, cmd: str, phase: str, timeout: float) -> None:
        self.log_cb(f"{phase}: {cmd}")
        try:
            proc = subprocess.run(cmd, shell=True, cwd=self.plan.cwd, env=self._child_env(),
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ServeError(f"{phase} timed out after {int(timeout)}s: {cmd}")
        except OSError as exc:
            raise ServeError(f"{phase} failed to start: {exc}")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
            raise ServeError(f"{phase} failed (exit {proc.returncode}):\n{tail}")

    def _child_env(self) -> dict:
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        # Pin the port for frameworks that honor $PORT (CRA, Next, Angular),
        # so our health-check target and the server agree.
        env.setdefault("PORT", str(self.plan.port))
        env.setdefault("BROWSER", "none")   # stop CRA from opening a real browser
        env.setdefault("CI", "true")        # non-interactive; no prompts
        return env

    # -- background reader ------------------------------------------------

    def _pump_output(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            with self._lock:
                self._log.append(line)
                if self._discovered_url is None:
                    found = _scan_url(line)
                    if found:
                        self._discovered_url = found.rstrip("/")

    def logs(self) -> str:
        with self._lock:
            return "\n".join(self._log)

    # -- lifecycle --------------------------------------------------------

    def __enter__(self) -> "ServedApp":
        if self.do_install and self.plan.install_cmd:
            self._run_blocking(self.plan.install_cmd, "install", INSTALL_TIMEOUT_S)
        if self.do_build and self.plan.build_cmd:
            self._run_blocking(self.plan.build_cmd, "build", BUILD_TIMEOUT_S)

        self.log_cb(f"serve: {self.plan.serve_cmd} (porta esperada {self.plan.port})")
        popen_kwargs = dict(shell=True, cwd=self.plan.cwd, env=self._child_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1)
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True   # own process group → killpg the tree
        try:
            self._proc = subprocess.Popen(self.plan.serve_cmd, **popen_kwargs)
        except OSError as exc:
            raise ServeError(f"could not start dev server: {exc}")

        self._reader = threading.Thread(target=self._pump_output, daemon=True)
        self._reader.start()

        try:
            self._wait_until_ready()
        except Exception:
            self._teardown()
            raise
        return self

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise ServeError(
                    f"dev server exited early (code {self._proc.returncode}). Last output:\n"
                    + self.logs()[-1500:]
                )
            # A URL printed by the server is authoritative — it reflects the port
            # the server actually bound (Vite auto-increments if the port is busy).
            with self._lock:
                discovered = self._discovered_url
            candidate = discovered or self.plan.url
            if _http_ok(candidate):
                self.url = candidate
                self.log_cb(f"pronto: {self.url}")
                return
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        self._teardown()
        raise ServeError(
            f"dev server did not answer on {self.plan.url} within {int(self.startup_timeout)}s. "
            f"Last output:\n{self.logs()[-1500:]}"
        )

    def __exit__(self, exc_type, exc, tb) -> None:
        self._teardown()

    def _teardown(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
