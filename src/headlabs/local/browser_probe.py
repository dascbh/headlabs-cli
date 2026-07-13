"""Local deterministic usability probe — the axe-core + runtime checks that the
remote ``browser-devtools`` MCP runs, but driven by a LOCAL Playwright so it can
reach ``http://localhost`` (a served/local app) and carry authentication.

The remote MCP (``mcps/browser-devtools/server.py``) cannot reach the user's
localhost, so for a local/served target we reproduce its two deterministic tools
here — ``a11y_audit`` (axe-core WCAG) and a mobile ``inspect_page`` (a11y/DOM
summary, runtime errors, failed requests, performance) — and return dicts in the
EXACT shape ``inspector.deterministic_usability_findings(axe, mobile)`` already
consumes. That keeps the objective-findings logic single-sourced: only the
transport (local browser vs remote MCP) differs.

Runs Playwright's async API under ``asyncio.run`` — safe here because the
``inspect`` command path has no other running event loop (unlike the ``chat``
REPL, which is why the LLM-facing ``browser_devtools`` tool uses a thread
worker instead). If Playwright (or Chromium) is unavailable, both probes return
``{"error": ...}`` so the caller degrades to "no deterministic findings" rather
than crashing.
"""
from __future__ import annotations

import asyncio
import os

# axe-core bundle shipped alongside this module (see pyproject package-data),
# mirroring the copy baked into the browser-devtools MCP image.
_AXE_PATH = os.path.join(os.path.dirname(__file__), "axe.min.js")
try:
    _AXE_SOURCE = open(_AXE_PATH, encoding="utf-8").read()
except OSError:
    _AXE_SOURCE = ""

_VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 390, "height": 844},
}
_NAV_TIMEOUT_MS = 20_000
_MAX_ITEMS = 100
_MAX_TEXT = 4000

# Ported verbatim from mcps/browser-devtools/server.py so local and remote
# produce identical signal shapes.
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

_AXE_RUN_JS = """async () => {
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

_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def probe_available() -> bool:
    """True if Playwright is importable (a real browser is still needed at run
    time, but this gates the obvious 'not installed' case for callers/tests)."""
    try:
        import playwright.async_api  # noqa: F401
        return True
    except Exception:
        return False


def run_local_usability_probe(url: str, auth=None, *, wait_ms: int = 1200) -> tuple[dict, dict]:
    """Run the axe audit (desktop) and mobile inspect against ``url`` locally.

    Returns ``(axe_dict, mobile_dict)`` in the same shape as the remote MCP's
    ``a11y_audit`` / ``inspect_page`` tools, ready for
    ``deterministic_usability_findings``. Never raises: transport/browser
    failures surface as ``{"error": ...}`` in the respective dict.
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        msg = f"playwright unavailable: {exc}"
        return {"error": msg}, {"error": msg}

    context_kwargs = {}
    if auth is not None and not auth.is_empty():
        try:
            context_kwargs = auth.context_kwargs()
        except ValueError as exc:
            msg = f"auth error: {exc}"
            return {"error": msg}, {"error": msg}

    try:
        return asyncio.run(_run(url, context_kwargs, wait_ms))
    except Exception as exc:  # noqa: BLE001
        msg = f"browser error: {str(exc)[:300]}"
        return {"error": msg}, {"error": msg}


async def _run(url: str, context_kwargs: dict, wait_ms: int) -> tuple[dict, dict]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        try:
            axe = await _audit(browser, url, "desktop", context_kwargs, wait_ms)
            mobile = await _inspect(browser, url, "mobile", context_kwargs, wait_ms)
            return axe, mobile
        finally:
            await browser.close()


async def _new_page(browser, viewport: str, context_kwargs: dict):
    ctx = await browser.new_context(
        viewport=_VIEWPORTS.get(viewport, _VIEWPORTS["desktop"]),
        ignore_https_errors=True,   # local dev servers often use self-signed TLS
        **context_kwargs,
    )
    return ctx, await ctx.new_page()


async def _goto(page, url: str) -> str:
    from playwright.async_api import TimeoutError as PWTimeout
    try:
        resp = await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        return resp.status if resp is not None else "unknown"
    except PWTimeout:
        return "load_timeout"


async def _audit(browser, url: str, viewport: str, context_kwargs: dict, wait_ms: int) -> dict:
    if not _AXE_SOURCE:
        return {"error": "axe-core bundle not found"}
    ctx, page = await _new_page(browser, viewport, context_kwargs)
    try:
        status = await _goto(page, url)
        if wait_ms > 0:
            await page.wait_for_timeout(min(wait_ms, 8000))
        await page.add_script_tag(content=_AXE_SOURCE)
        violations = await page.evaluate(_AXE_RUN_JS)
        counts = {"critical": 0, "serious": 0, "moderate": 0, "minor": 0}
        for v in violations:
            imp = v.get("impact")
            if imp in counts:
                counts[imp] += 1
        return {
            "url": url, "viewport": viewport, "http_status": status,
            "violation_count": len(violations),
            "counts_by_impact": counts,
            "violations": violations[:_MAX_ITEMS],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"axe error: {str(exc)[:300]}"}
    finally:
        await ctx.close()


async def _inspect(browser, url: str, viewport: str, context_kwargs: dict, wait_ms: int) -> dict:
    ctx, page = await _new_page(browser, viewport, context_kwargs)
    console: list[dict] = []
    failed: list[dict] = []
    page_errors: list[str] = []

    def on_console(msg):
        if len(console) < _MAX_ITEMS:
            console.append({"type": msg.type, "text": msg.text})

    def on_pageerror(exc):
        if len(page_errors) < _MAX_ITEMS:
            page_errors.append(str(exc))

    def on_requestfailed(req):
        if len(failed) < _MAX_ITEMS:
            failed.append({"url": req.url, "reason": (req.failure or "request failed")})

    def on_response(resp):
        if resp.status >= 400 and len(failed) < _MAX_ITEMS:
            failed.append({"url": resp.url, "status": resp.status})

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("requestfailed", on_requestfailed)
    page.on("response", on_response)
    try:
        status = await _goto(page, url)
        if wait_ms > 0:
            await page.wait_for_timeout(min(wait_ms, 8000))
        try:
            title = await page.title()
        except Exception:
            title = ""
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
        return {
            "url": url, "viewport": viewport, "http_status": status,
            "title": title,
            "console_errors": [c["text"] for c in console if c["type"] == "error"],
            "console_warnings": [c["text"] for c in console if c["type"] == "warning"],
            "page_errors": page_errors,
            "failed_requests": failed,
            "accessibility": a11y,
            "performance": perf,
            "rendered_text_excerpt": text[:_MAX_TEXT],
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"inspect error: {str(exc)[:300]}"}
    finally:
        await ctx.close()
