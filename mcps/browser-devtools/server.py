"""browser-devtools MCP — headless Chromium inspection for running front-ends.

Gives a Claude-backed agent (e.g. the platform `loop-inspector`) real browser
capability — load a URL, see console errors, failed network requests, rendered
DOM/text, screenshots, and evaluate JS — instead of the plain HTTP GET the
inspector uses today (which can't see a JS-rendered SPA).

Design notes:
- FastMCP's streamable-HTTP runtime is asyncio-based, so tools use Playwright's
  ASYNC API (`playwright.async_api`) with `async def` tools — no thread worker
  and no "asyncio.run() from a running loop" conflict (that trick is only needed
  in the local CLI tool, which shares a loop with prompt_toolkit).
- STATELESS: one fresh browser per tool call, always closed in `finally`. This
  matches `stateless_http=True` and the inspector's *fragmented* per-unit design
  (a fresh sub-invocation per resource), and mirrors the platform render-service
  pattern (one browser per request). No global browser to leak across calls.
- Playwright is imported lazily inside the tools so the module imports (and the
  server boots for `tools/list` verification) even where the browser binary is
  absent.
- Chromium launch args (`--no-sandbox --disable-dev-shm-usage --single-process
  --no-zygote`) are the same ones the platform render-service uses to run
  Chromium inside a container/Lambda.
"""
import os
import re

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("browser-devtools", host="0.0.0.0", stateless_http=True)

DEFAULT_WAIT_MS = int(os.environ.get("BROWSER_DEFAULT_WAIT_MS", "1500"))
NAV_TIMEOUT_MS = int(os.environ.get("BROWSER_NAV_TIMEOUT_MS", "15000"))
MAX_ITEMS = 100
MAX_TEXT = 4000
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--single-process", "--no-zygote"]

# Accessibility/DOM summary computed in the SAME page load as inspect_page — one
# browser launch per call. Doing this here (rather than a second evaluate_js
# call) avoids a back-to-back browser relaunch that can trip the gateway's
# request timeout.
_A11Y_JS = """() => ({
  imgs_without_alt: document.querySelectorAll('img:not([alt])').length,
  inputs_without_label: Array.from(document.querySelectorAll('input,select,textarea'))
      .filter(e => e.type !== 'hidden' && (!e.labels || !e.labels.length) && !e.getAttribute('aria-label')).length,
  links_without_text: Array.from(document.querySelectorAll('a'))
      .filter(a => !a.textContent.trim() && !a.getAttribute('aria-label')).length,
  buttons_without_text: Array.from(document.querySelectorAll('button'))
      .filter(b => !b.textContent.trim() && !b.getAttribute('aria-label')).length,
  has_lang: !!document.documentElement.lang,
  has_viewport: !!document.querySelector('meta[name=viewport]')
})"""


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url or ""))


async def _with_page(url: str, wait_ms: int, fn):
    """Launch a fresh headless Chromium, wire console/network sinks, navigate to
    ``url``, run ``fn(page, sinks)``, and always close the browser."""
    from playwright.async_api import async_playwright

    console: list[dict] = []
    requests: list[dict] = []
    failed: list[dict] = []
    page_errors: list[str] = []

    def on_console(msg):
        if len(console) < MAX_ITEMS:
            console.append({"type": msg.type, "text": msg.text})

    def on_pageerror(exc):
        # Uncaught JS exceptions (e.g. a TypeError that halts the script) fire
        # `pageerror`, NOT `console` — capture them explicitly or they're missed.
        if len(page_errors) < MAX_ITEMS:
            page_errors.append(str(exc))

    def on_request(req):
        if len(requests) < MAX_ITEMS:
            requests.append({"method": req.method, "url": req.url})

    def on_requestfailed(req):
        if len(failed) < MAX_ITEMS:
            failed.append({"url": req.url, "reason": (req.failure or "request failed")})

    def on_response(resp):
        if resp.status >= 400 and len(failed) < MAX_ITEMS:
            failed.append({"url": resp.url, "status": resp.status})

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        try:
            page = await browser.new_page()
            page.on("console", on_console)
            page.on("pageerror", on_pageerror)
            page.on("request", on_request)
            page.on("requestfailed", on_requestfailed)
            page.on("response", on_response)
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            status = "unknown"
            # `domcontentloaded` (not `load`) returns as soon as the DOM is
            # parsed — a slow/hanging subresource on a heavy page must not block
            # the whole inspection (and trip an upstream request timeout). We
            # still capture late console/network activity during wait_ms below.
            try:
                resp = await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                if resp is not None:
                    status = resp.status
            except PlaywrightTimeoutError:
                status = "load_timeout"  # proceed with whatever loaded so far
            if wait_ms > 0:
                await page.wait_for_timeout(min(wait_ms, 8_000))
            return await fn(page, {"console": console, "requests": requests,
                                   "failed": failed, "status": status,
                                   "page_errors": page_errors})
        finally:
            await browser.close()


@mcp.tool()
async def inspect_page(url: str, wait_ms: int = DEFAULT_WAIT_MS, screenshot: bool = False) -> dict:
    """Load a URL in a real headless Chromium and report runtime issues that a
    plain HTTP GET cannot see: HTTP status, console errors/warnings, uncaught JS
    exceptions (page_errors), failed (4xx/5xx or network-failed) requests, page
    title, an accessibility/DOM summary (imgs without alt, inputs without label,
    has lang/viewport, ...), and a rendered-text excerpt. Optionally returns a
    base64 PNG screenshot. Read-only; one fresh browser per call — this single
    call covers runtime errors, network health, and a11y together.

    Args:
        url: absolute http(s) URL of the running front-end to inspect.
        wait_ms: extra settle time after load for late console/network activity.
        screenshot: when true, include a base64-encoded PNG of the viewport.
    """
    if not _valid_url(url):
        return _err("INVALID_URL", "url must start with http:// or https://")

    async def collect(page, sinks):
        title = await page.title()
        try:
            text = await page.inner_text("body")
        except Exception:
            text = ""
        try:
            a11y = await page.evaluate(_A11Y_JS)
        except Exception:
            a11y = {}
        console = sinks["console"]
        out = {
            "url": url,
            "http_status": sinks["status"],
            "title": title,
            "console_errors": [c["text"] for c in console if c["type"] == "error"],
            "console_warnings": [c["text"] for c in console if c["type"] == "warning"],
            "page_errors": sinks["page_errors"],
            "failed_requests": sinks["failed"],
            "request_count": len(sinks["requests"]),
            "accessibility": a11y,
            "rendered_text_excerpt": text[:MAX_TEXT],
        }
        if screenshot:
            import base64
            png = await page.screenshot(type="png")
            out["screenshot_base64"] = base64.b64encode(png).decode()
        return out

    try:
        return await _with_page(url, wait_ms, collect)
    except Exception as exc:  # noqa: BLE001 — surface as a structured error, never raise
        return _err("BROWSER_ERROR", str(exc)[:500])


@mcp.tool()
async def evaluate_js(url: str, script: str, wait_ms: int = DEFAULT_WAIT_MS) -> dict:
    """Load a URL and evaluate a JavaScript expression in the page context,
    returning the JSON-serializable result. Useful for accessibility/DOM probes,
    e.g. counting images without alt text:
    `Array.from(document.querySelectorAll('img:not([alt])')).length`.
    Read-only; one fresh browser per call.
    """
    if not _valid_url(url):
        return _err("INVALID_URL", "url must start with http:// or https://")

    async def run(page, sinks):
        try:
            result = await page.evaluate(script)
        except Exception as exc:  # noqa: BLE001
            return _err("EVAL_ERROR", str(exc)[:500])
        return {"url": url, "result": result}

    try:
        return await _with_page(url, wait_ms, run)
    except Exception as exc:  # noqa: BLE001
        return _err("BROWSER_ERROR", str(exc)[:500])


app = mcp.streamable_http_app()

if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
