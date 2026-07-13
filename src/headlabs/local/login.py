"""Form-login capture for authenticated inspection.

Lets `headlabs local inspect` authenticate ANY form-login site from just a URL
plus credentials — no separate/manual session-capture step. It drives a real
browser through the login form (fill user + password, submit, wait for the
authenticated session) and returns a Playwright ``storage_state`` dict (cookies
+ localStorage of the logged-in session) that the deterministic probe and the
``browser_devtools`` tool then reuse.

Selectors are tolerant by default (email/username + password + submit), with
explicit overrides for non-standard forms. Runs Playwright's async API under
``asyncio.run`` — safe in the ``inspect`` command path (no other event loop).
"""
from __future__ import annotations

import asyncio

_USER_SELECTORS = [
    "input[type=email]", "input[autocomplete=username]", "input[name*=email i]",
    "input[id*=email i]", "input[name*=user i]", "input[id*=user i]",
    "input[placeholder*=mail i]", "input[placeholder*=usuário i]", "input[type=text]",
]
_PASS_SELECTORS = [
    "input[type=password]", "input[autocomplete=current-password]",
    "input[name*=pass i]", "input[id*=pass i]", "input[placeholder*=senha i]",
]
_SUBMIT_SELECTORS = [
    "button[type=submit]", "input[type=submit]",
    "button:has-text('Entrar')", "button:has-text('Login')", "button:has-text('Log in')",
    "button:has-text('Sign in')", "button:has-text('Acessar')", "button:has-text('Continuar')",
    "button",
]

_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


class LoginError(RuntimeError):
    """Raised when the login form can't be driven or produces no session."""


def capture_login(login_url: str, user: str, password: str, *,
                  user_selector: str | None = None, password_selector: str | None = None,
                  submit_selector: str | None = None, wait_ms: int = 4000,
                  timeout_ms: int = 30000) -> tuple[dict, str]:
    """Log in at ``login_url`` and return ``(storage_state_dict, landing_url)``.

    Raises :class:`LoginError` if Playwright is unavailable, the fields/submit
    can't be located, or the submit produced no session (still on /login).
    """
    try:
        import playwright.async_api  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raise LoginError(f"playwright unavailable: {exc}") from exc
    return asyncio.run(_run(login_url, user, password, user_selector,
                            password_selector, submit_selector, wait_ms, timeout_ms))


async def _first(page, selectors):
    for s in selectors:
        if not s:
            continue
        loc = page.locator(s)
        try:
            if await loc.count() > 0:
                return loc.first
        except Exception:  # noqa: BLE001 — a bad selector must not abort the search
            continue
    return None


async def _run(login_url, user, password, user_sel, pass_sel, submit_sel, wait_ms, timeout_ms):
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        try:
            ctx = await browser.new_context(ignore_https_errors=True)
            page = await ctx.new_page()
            try:
                await page.goto(login_url, wait_until="networkidle", timeout=timeout_ms)
            except PWTimeout:
                raise LoginError(f"login page did not load within {timeout_ms}ms: {login_url}")

            u = await _first(page, [user_sel] if user_sel else _USER_SELECTORS)
            pw = await _first(page, [pass_sel] if pass_sel else _PASS_SELECTORS)
            if u is None or pw is None:
                raise LoginError(
                    "could not locate the username/password fields on the login page — "
                    "pass --login-user-field / --login-pass-field with CSS selectors.")
            await u.fill(user)
            await pw.fill(password)

            submit = await _first(page, [submit_sel] if submit_sel else _SUBMIT_SELECTORS)
            if submit is None:
                raise LoginError("could not locate a submit button — pass --login-submit.")

            url_before = page.url
            try:
                async with page.expect_navigation(timeout=15000, wait_until="networkidle"):
                    await submit.click()
            except PWTimeout:
                # SPA login often updates in place without a full navigation.
                await page.wait_for_timeout(wait_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PWTimeout:
                pass

            landing = page.url
            state = await ctx.storage_state()
            has_session = bool(state.get("cookies")) or any(
                o.get("localStorage") for o in state.get("origins", []))
            still_on_login = "login" in landing.lower() and landing == url_before
            if not has_session and still_on_login:
                raise LoginError(
                    f"login produced no session (still at {landing}) — check the "
                    "credentials, or pass explicit --login-*-field selectors.")
            return state, landing
        finally:
            await browser.close()
