"""BrowserDevtoolsTool — Chrome DevTools-style browser automation and
inspection, backed by Playwright (which drives Chromium via the Chrome
DevTools Protocol under the hood).

This is a native tool, NOT an MCP client -- `headlabs local` does not speak
the MCP protocol (see docs/local-runtime.md section 3 for why MCP support is
out of scope). This tool gives equivalent capability to the popular
"chrome-devtools-mcp" server (navigate, screenshot, console logs, network
requests, run JS, click/read DOM) without implementing the MCP wire protocol.

Threading model -- IMPORTANT, this is not incidental: Playwright's Sync API
internally manages its own asyncio event loop bound to whichever thread
calls `sync_playwright().start()`. `headlabs local chat` uses
`prompt_toolkit`'s `PromptSession.prompt()`, which ALSO drives an asyncio
event loop (via `asyncio.run()`) on the main thread for every input prompt.
Confirmed via a real crash in an interactive session: after using
browser_devtools once, the next `session.prompt()` call raised
"RuntimeError: asyncio.run() cannot be called from a running event loop" --
a known conflict (see microsoft/playwright-python#462) between Playwright
Sync API and any other asyncio loop sharing the same thread.

Fix: the entire Playwright session (start, every page action, and stop)
runs inside one dedicated worker thread created lazily on first use. The
main thread (where prompt_toolkit's REPL loop lives) never touches asyncio
via Playwright directly -- it only sends callables to the worker thread via
a queue and blocks on a per-call result. This keeps the fix local to this
tool instead of rewriting `local_cli.py`'s REPL to asyncio (which would be a
much larger, riskier change for the same problem).

Session model: a single Chromium instance + page is kept alive across tool
calls within the same `headlabs local` process (on the worker thread),
since a realistic task is "navigate, then screenshot, then read console
logs" -- three separate tool calls that must share one browser session, not
three throwaway browsers. Call action="close" to shut it down explicitly;
otherwise it's closed automatically when the CLI process exits.

Always requires permission, like BashTool and EditFileTool: this tool can
navigate to arbitrary URLs and execute arbitrary JavaScript in a real
browser context, which is a real-world side effect surface.
"""
from __future__ import annotations

import queue
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from headlabs.local.tools.base import BaseTool, ToolResult

DEFAULT_TIMEOUT_MS = 15_000
MAX_OUTPUT_CHARS = 10_000
MAX_LOG_ENTRIES = 100
MAX_NETWORK_ENTRIES = 100
WORKER_JOIN_TIMEOUT_S = 10


class _BrowserWorker:
    """Owns the Playwright Sync API session on a single dedicated thread.

    All Playwright calls (start, page actions, stop) happen inside `_run()`,
    executing on `self._thread` -- never on the caller's thread. This is
    what keeps Playwright's internal asyncio loop from colliding with
    prompt_toolkit's asyncio loop on the main thread (see module docstring).
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._auth = None  # BrowserAuth | None — applied to the browser context
        self.console_logs: list[str] = []
        self.network_requests: list[str] = []

    def set_auth(self, auth) -> None:
        """Set authentication for the browser context. Must be called BEFORE the
        first navigate (the context is created once, lazily); a later change has
        no effect until the session is closed and reopened."""
        self._auth = auth

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Worker thread loop: pull (func, args, kwargs, result_queue) jobs
        and execute them, always on this same thread."""
        while True:
            job = self._jobs.get()
            if job is None:  # sentinel -- shut down the thread
                return
            func, args, kwargs, result_q = job
            try:
                result_q.put(("ok", func(*args, **kwargs)))
            except Exception as exc:  # noqa: BLE001 -- forward any exception to the caller thread
                result_q.put(("error", exc))

    def _call(self, func, *args, **kwargs):
        """Run `func(*args, **kwargs)` on the worker thread and block until
        it completes, re-raising any exception on the calling thread."""
        self._ensure_thread()
        result_q: queue.Queue = queue.Queue()
        self._jobs.put((func, args, kwargs, result_q))
        status, value = result_q.get()
        if status == "error":
            raise value
        return value

    # --- actions below all run via self._call(), so they execute on the worker thread ---

    def _do_ensure_started(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        # A context (not a bare page) so authentication — storage_state, HTTP
        # basic credentials, extra headers — can be applied for login-gated or
        # served-behind-auth targets. With no auth this is an empty context,
        # equivalent to the previous new_page().
        ctx_kwargs = {"ignore_https_errors": True}
        if self._auth is not None and not self._auth.is_empty():
            ctx_kwargs.update(self._auth.context_kwargs())
        self._context = self._browser.new_context(**ctx_kwargs)
        self._page = self._context.new_page()
        self._page.on("console", lambda msg: self._record_console(msg))
        self._page.on("request", lambda req: self._record_request(req))

    def _record_console(self, msg) -> None:
        if len(self.console_logs) >= MAX_LOG_ENTRIES:
            return
        self.console_logs.append(f"[{msg.type}] {msg.text}")

    def _record_request(self, req) -> None:
        if len(self.network_requests) >= MAX_NETWORK_ENTRIES:
            return
        self.network_requests.append(f"{req.method} {req.url}")

    def _do_navigate(self, url: str, timeout_ms: int) -> tuple[str, int | str]:
        self._do_ensure_started()
        response = self._page.goto(url, timeout=timeout_ms, wait_until="load")
        return url, (response.status if response else "unknown")

    def _do_screenshot(self, out_path: str, timeout_ms: int) -> None:
        self._page.screenshot(path=out_path, timeout=timeout_ms)

    def _do_evaluate(self, script: str):
        return self._page.evaluate(script)

    def _do_click(self, selector: str, timeout_ms: int) -> None:
        self._page.click(selector, timeout=timeout_ms)

    def _do_get_text(self, selector: str, timeout_ms: int) -> str:
        return self._page.inner_text(selector, timeout=timeout_ms)

    def _do_close(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.console_logs = []
        self.network_requests = []

    # --- public API used by the tool, all dispatched through self._call() ---

    def navigate(self, url: str, timeout_ms: int) -> tuple[str, int | str]:
        return self._call(self._do_navigate, url, timeout_ms)

    def screenshot(self, out_path: str, timeout_ms: int) -> None:
        self._call(self._do_screenshot, out_path, timeout_ms)

    def evaluate(self, script: str):
        return self._call(self._do_evaluate, script)

    def click(self, selector: str, timeout_ms: int) -> None:
        self._call(self._do_click, selector, timeout_ms)

    def get_text(self, selector: str, timeout_ms: int) -> str:
        return self._call(self._do_get_text, selector, timeout_ms)

    def close(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            return
        self._call(self._do_close)
        self._jobs.put(None)  # sentinel to stop the worker loop
        self._thread.join(timeout=WORKER_JOIN_TIMEOUT_S)
        self._thread = None

    def is_started(self) -> bool:
        return self._page is not None


_worker = _BrowserWorker()


class BrowserDevtoolsInput(BaseModel):
    action: Literal[
        "navigate", "screenshot", "get_console_logs", "get_network_requests",
        "evaluate", "click", "get_text", "close",
    ] = Field(..., description="Which browser devtools action to perform")
    url: str = Field("", description="URL to navigate to (action=navigate)")
    script: str = Field("", description="JavaScript expression to evaluate (action=evaluate)")
    selector: str = Field("", description="CSS selector (action=click, action=get_text)")
    timeout_ms: int = Field(DEFAULT_TIMEOUT_MS, description="Timeout in milliseconds for navigation/waits")


class BrowserDevtoolsTool(BaseTool):
    name = "browser_devtools"
    description = (
        "Control a real headless Chrome browser for debugging and automation: navigate to a "
        "URL, take a screenshot, read console logs, inspect network requests, run JavaScript, "
        "click an element, or read an element's text. Equivalent in capability to the "
        "chrome-devtools-mcp server, but implemented as a native tool. The browser session "
        "persists across calls until action='close' is used."
    )
    input_schema = BrowserDevtoolsInput

    @staticmethod
    def requires_permission(input_data: dict) -> bool:
        return True

    def execute(self, input_data: dict, *, cwd: str) -> ToolResult:
        parsed = BrowserDevtoolsInput.model_validate(input_data)

        try:
            from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
        except ImportError:
            return ToolResult(
                output=(
                    "playwright is not installed. Install with: pip install playwright && "
                    "playwright install chromium"
                ),
                is_error=True,
            )

        handler = getattr(self, f"_action_{parsed.action}", None)
        if handler is None:
            return ToolResult(output=f"Unknown action: {parsed.action}", is_error=True)

        try:
            return handler(parsed)
        except PlaywrightTimeoutError as exc:
            return ToolResult(output=f"Timed out: {exc}", is_error=True)
        except PlaywrightError as exc:
            return ToolResult(output=f"Browser error: {exc}", is_error=True)

    def _action_navigate(self, parsed: BrowserDevtoolsInput) -> ToolResult:
        if not parsed.url:
            return ToolResult(output="action=navigate requires 'url'", is_error=True)
        url = parsed.url
        if not re.match(r"^https?://", url):
            url = f"https://{url}"
        final_url, status = _worker.navigate(url, parsed.timeout_ms)
        return ToolResult(output=f"Navigated to {final_url} (HTTP {status})")

    def _action_screenshot(self, parsed: BrowserDevtoolsInput) -> ToolResult:
        if not _worker.is_started():
            return ToolResult(output="No page loaded yet -- call action=navigate first", is_error=True)
        out_path = Path(tempfile.gettempdir()) / f"headlabs_browser_screenshot_{int(time.time() * 1000)}.png"
        _worker.screenshot(str(out_path), parsed.timeout_ms)
        return ToolResult(output=f"Screenshot saved to {out_path}")

    def _action_get_console_logs(self, parsed: BrowserDevtoolsInput) -> ToolResult:
        if not _worker.console_logs:
            return ToolResult(output="(no console logs captured)")
        text = "\n".join(_worker.console_logs)[:MAX_OUTPUT_CHARS]
        return ToolResult(output=text)

    def _action_get_network_requests(self, parsed: BrowserDevtoolsInput) -> ToolResult:
        if not _worker.network_requests:
            return ToolResult(output="(no network requests captured)")
        text = "\n".join(_worker.network_requests)[:MAX_OUTPUT_CHARS]
        return ToolResult(output=text)

    def _action_evaluate(self, parsed: BrowserDevtoolsInput) -> ToolResult:
        if not _worker.is_started():
            return ToolResult(output="No page loaded yet -- call action=navigate first", is_error=True)
        if not parsed.script:
            return ToolResult(output="action=evaluate requires 'script'", is_error=True)
        result = _worker.evaluate(parsed.script)
        return ToolResult(output=str(result)[:MAX_OUTPUT_CHARS])

    def _action_click(self, parsed: BrowserDevtoolsInput) -> ToolResult:
        if not _worker.is_started():
            return ToolResult(output="No page loaded yet -- call action=navigate first", is_error=True)
        if not parsed.selector:
            return ToolResult(output="action=click requires 'selector'", is_error=True)
        _worker.click(parsed.selector, parsed.timeout_ms)
        return ToolResult(output=f"Clicked element matching selector: {parsed.selector}")

    def _action_get_text(self, parsed: BrowserDevtoolsInput) -> ToolResult:
        if not _worker.is_started():
            return ToolResult(output="No page loaded yet -- call action=navigate first", is_error=True)
        if not parsed.selector:
            return ToolResult(output="action=get_text requires 'selector'", is_error=True)
        text = _worker.get_text(parsed.selector, parsed.timeout_ms)
        return ToolResult(output=text[:MAX_OUTPUT_CHARS] or "(empty text content)")

    def _action_close(self, parsed: BrowserDevtoolsInput) -> ToolResult:
        _worker.close()
        return ToolResult(output="Browser session closed")
