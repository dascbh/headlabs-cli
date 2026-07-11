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

# Viewport presets for responsive/mobile usability testing.
_VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 390, "height": 844},
}

# axe-core is bundled in the image (see Dockerfile) and injected into the page
# for a11y_audit — no runtime CDN dependency.
_AXE_PATH = os.path.join(os.path.dirname(__file__), "axe.min.js")
try:
    _AXE_SOURCE = open(_AXE_PATH, encoding="utf-8").read()
except OSError:
    _AXE_SOURCE = ""

# Lightweight performance snapshot (First Contentful Paint + navigation timing).
_PERF_JS = """() => {
  const n = performance.getEntriesByType('navigation')[0] || {};
  const p = performance.getEntriesByType('paint').find(e => e.name === 'first-contentful-paint');
  return {
    fcp_ms: p ? Math.round(p.startTime) : null,
    dom_content_loaded_ms: n.domContentLoadedEventEnd ? Math.round(n.domContentLoadedEventEnd) : null,
    load_ms: n.loadEventEnd ? Math.round(n.loadEventEnd) : null,
    transfer_size: n.transferSize || null
  };
}"""

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
  has_viewport: !!document.querySelector('meta[name=viewport]'),
  horizontal_overflow: document.documentElement.scrollWidth > (window.innerWidth + 2),
  small_tap_targets: Array.from(document.querySelectorAll('a,button,input,select,[role=button]'))
      .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (r.width < 40 || r.height < 40); }).length
})"""


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url or ""))


async def _with_page(url: str, wait_ms: int, fn, viewport: str = "desktop"):
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
            page = await browser.new_page(viewport=_VIEWPORTS.get(viewport, _VIEWPORTS["desktop"]))
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
async def inspect_page(url: str, wait_ms: int = DEFAULT_WAIT_MS, screenshot: bool = False,
                       viewport: str = "desktop") -> dict:
    """Load a URL in a real headless Chromium and report runtime issues that a
    plain HTTP GET cannot see: HTTP status, console errors/warnings, uncaught JS
    exceptions (page_errors), failed (4xx/5xx or network-failed) requests, page
    title, an accessibility/DOM summary (imgs without alt, inputs without label,
    has lang/viewport, horizontal_overflow, small_tap_targets), a performance
    snapshot (FCP + navigation timing), and a rendered-text excerpt. Optionally
    returns a base64 PNG screenshot. Read-only; one fresh browser per call — this
    single call covers runtime errors, network health, a11y, responsiveness, and
    perceived performance together.

    Args:
        url: absolute http(s) URL of the running front-end to inspect.
        wait_ms: extra settle time after load for late console/network activity.
        screenshot: when true, include a base64-encoded PNG of the viewport.
        viewport: one of 'desktop' (1280x800), 'tablet' (768x1024), 'mobile'
            (390x844). Use 'mobile' to catch responsive/overflow/tap-target issues.
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
        try:
            perf = await page.evaluate(_PERF_JS)
        except Exception:
            perf = {}
        console = sinks["console"]
        out = {
            "url": url,
            "viewport": viewport,
            "http_status": sinks["status"],
            "title": title,
            "console_errors": [c["text"] for c in console if c["type"] == "error"],
            "console_warnings": [c["text"] for c in console if c["type"] == "warning"],
            "page_errors": sinks["page_errors"],
            "failed_requests": sinks["failed"],
            "request_count": len(sinks["requests"]),
            "accessibility": a11y,
            "performance": perf,
            "rendered_text_excerpt": text[:MAX_TEXT],
        }
        if screenshot:
            import base64
            png = await page.screenshot(type="png")
            out["screenshot_base64"] = base64.b64encode(png).decode()
        return out

    try:
        return await _with_page(url, wait_ms, collect, viewport=viewport)
    except Exception as exc:  # noqa: BLE001 — surface as a structured error, never raise
        return _err("BROWSER_ERROR", str(exc)[:500])


@mcp.tool()
async def a11y_audit(url: str, viewport: str = "desktop", wait_ms: int = DEFAULT_WAIT_MS) -> dict:
    """Run a full WCAG 2.0/2.1 A & AA accessibility audit on a URL using axe-core
    (the industry-standard engine), in a real headless browser. Returns concrete,
    objective violations — not judgment — each with impact
    (critical/serious/moderate/minor), rule id, help text, a docs URL, and a few
    offending element selectors. Complements the heuristic/visual review a model
    does from a screenshot. Read-only; one fresh browser per call.

    Args:
        url: absolute http(s) URL to audit.
        viewport: 'desktop' | 'tablet' | 'mobile'.
        wait_ms: settle time after DOM load before auditing.
    """
    if not _valid_url(url):
        return _err("INVALID_URL", "url must start with http:// or https://")
    if not _AXE_SOURCE:
        return _err("AXE_MISSING", "axe-core bundle not found in the image")

    async def run(page, sinks):
        try:
            await page.add_script_tag(content=_AXE_SOURCE)
            violations = await page.evaluate(
                """async () => {
                    const r = await axe.run(document, {
                        resultTypes: ['violations'],
                        runOnly: { type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa'] }
                    });
                    return r.violations.map(v => ({
                        id: v.id, impact: v.impact, help: v.help, description: v.description,
                        helpUrl: v.helpUrl,
                        node_count: v.nodes.length,
                        sample_targets: v.nodes.slice(0, 5).map(n => (n.target || []).join(' '))
                    }));
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            return _err("AXE_ERROR", str(exc)[:500])
        counts = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}
        for v in violations:
            imp = v.get("impact")
            if imp in counts:
                counts[imp] += 1
        return {
            "url": url,
            "viewport": viewport,
            "http_status": sinks["status"],
            "violation_count": len(violations),
            "counts_by_impact": counts,
            "violations": violations[:MAX_ITEMS],
        }

    try:
        return await _with_page(url, wait_ms, run, viewport=viewport)
    except Exception as exc:  # noqa: BLE001
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
