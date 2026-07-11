"""Unit tests for headlabs.local.tools.browser_devtools — Playwright mocked,
no real Chromium launched.

The module runs the actual Playwright Sync API session on a dedicated
worker thread (see the module docstring in browser_devtools.py for why: a
real crash was observed where Playwright's Sync API collided with
prompt_toolkit's asyncio event loop on the main thread when both ran on the
same thread). Tests patch the worker's internal `_do_*` methods (which is
where the real `playwright.sync_api` calls live) rather than mocking a page
object directly, since the worker thread + queue plumbing is itself part of
what's under test.
"""
from unittest.mock import MagicMock, patch

import pytest

from headlabs.local.tools import browser_devtools as bd_module
from headlabs.local.tools.browser_devtools import BrowserDevtoolsTool


@pytest.fixture(autouse=True)
def reset_worker():
    """Every test gets a clean, unstarted worker -- prevents state leaking
    between tests via the module-level singleton, and shuts down any worker
    thread left running by a previous test."""
    bd_module._worker.close()
    bd_module._worker._page = None
    bd_module._worker.console_logs = []
    bd_module._worker.network_requests = []
    yield
    bd_module._worker.close()


def test_navigate_prepends_https_and_reports_status():
    with patch.object(bd_module._worker, "navigate", return_value=("https://example.com", 200)) as mock_nav:
        result = BrowserDevtoolsTool().execute({"action": "navigate", "url": "example.com"}, cwd=".")
    assert not result.is_error
    assert "https://example.com" in result.output
    assert "200" in result.output
    called_url = mock_nav.call_args[0][0]
    assert called_url == "https://example.com"


def test_navigate_requires_url():
    result = BrowserDevtoolsTool().execute({"action": "navigate"}, cwd=".")
    assert result.is_error
    assert "requires 'url'" in result.output


def test_screenshot_without_navigate_first_is_an_error():
    result = BrowserDevtoolsTool().execute({"action": "screenshot"}, cwd=".")
    assert result.is_error
    assert "navigate first" in result.output


def test_screenshot_saves_to_tmp_and_reports_path():
    bd_module._worker._page = MagicMock()  # mark as started
    with patch.object(bd_module._worker, "screenshot") as mock_screenshot:
        result = BrowserDevtoolsTool().execute({"action": "screenshot"}, cwd=".")
    assert not result.is_error
    assert "Screenshot saved to" in result.output
    assert ".png" in result.output
    mock_screenshot.assert_called_once()


def test_get_console_logs_empty_by_default():
    result = BrowserDevtoolsTool().execute({"action": "get_console_logs"}, cwd=".")
    assert not result.is_error
    assert "no console logs" in result.output


def test_get_console_logs_returns_captured_entries():
    bd_module._worker.console_logs = ["[log] hello", "[error] boom"]
    result = BrowserDevtoolsTool().execute({"action": "get_console_logs"}, cwd=".")
    assert "hello" in result.output
    assert "boom" in result.output


def test_get_network_requests_empty_by_default():
    result = BrowserDevtoolsTool().execute({"action": "get_network_requests"}, cwd=".")
    assert not result.is_error
    assert "no network requests" in result.output


def test_get_network_requests_returns_captured_entries():
    bd_module._worker.network_requests = ["GET https://example.com/", "GET https://example.com/style.css"]
    result = BrowserDevtoolsTool().execute({"action": "get_network_requests"}, cwd=".")
    assert "example.com/" in result.output
    assert "style.css" in result.output


def test_evaluate_without_navigate_first_is_an_error():
    result = BrowserDevtoolsTool().execute({"action": "evaluate", "script": "1+1"}, cwd=".")
    assert result.is_error
    assert "navigate first" in result.output


def test_evaluate_requires_script():
    bd_module._worker._page = MagicMock()
    result = BrowserDevtoolsTool().execute({"action": "evaluate"}, cwd=".")
    assert result.is_error
    assert "requires 'script'" in result.output


def test_evaluate_returns_real_computed_result():
    """Ground-truth style check: the tool must report what the browser
    actually evaluated, not a hardcoded/fabricated string."""
    bd_module._worker._page = MagicMock()
    with patch.object(bd_module._worker, "evaluate", return_value=4) as mock_eval:
        result = BrowserDevtoolsTool().execute({"action": "evaluate", "script": "2+2"}, cwd=".")
    assert not result.is_error
    assert result.output == "4"
    mock_eval.assert_called_once_with("2+2")


def test_click_requires_selector():
    bd_module._worker._page = MagicMock()
    result = BrowserDevtoolsTool().execute({"action": "click"}, cwd=".")
    assert result.is_error
    assert "requires 'selector'" in result.output


def test_click_without_navigate_first_is_an_error():
    result = BrowserDevtoolsTool().execute({"action": "click", "selector": "#btn"}, cwd=".")
    assert result.is_error
    assert "navigate first" in result.output


def test_click_calls_worker_click_with_selector():
    bd_module._worker._page = MagicMock()
    with patch.object(bd_module._worker, "click") as mock_click:
        result = BrowserDevtoolsTool().execute({"action": "click", "selector": "#submit"}, cwd=".")
    assert not result.is_error
    mock_click.assert_called_once()
    assert mock_click.call_args[0][0] == "#submit"


def test_get_text_requires_selector():
    bd_module._worker._page = MagicMock()
    result = BrowserDevtoolsTool().execute({"action": "get_text"}, cwd=".")
    assert result.is_error
    assert "requires 'selector'" in result.output


def test_get_text_returns_real_inner_text():
    bd_module._worker._page = MagicMock()
    with patch.object(bd_module._worker, "get_text", return_value="Actual page heading"):
        result = BrowserDevtoolsTool().execute({"action": "get_text", "selector": "h1"}, cwd=".")
    assert result.output == "Actual page heading"


def test_get_text_empty_content_reports_explicitly():
    bd_module._worker._page = MagicMock()
    with patch.object(bd_module._worker, "get_text", return_value=""):
        result = BrowserDevtoolsTool().execute({"action": "get_text", "selector": "h1"}, cwd=".")
    assert "empty text content" in result.output.lower()


def test_close_resets_worker_state():
    bd_module._worker._page = MagicMock()
    bd_module._worker._browser = MagicMock()
    bd_module._worker._playwright = MagicMock()
    bd_module._worker.console_logs = ["[log] x"]
    # Give the worker a real (but no-op) thread so close() has something to join.
    import threading
    bd_module._worker._thread = threading.Thread(target=lambda: None)
    bd_module._worker._thread.start()
    bd_module._worker._thread.join()

    with patch.object(bd_module._worker, "_call", side_effect=lambda f, *a, **k: f(*a, **k)):
        result = BrowserDevtoolsTool().execute({"action": "close"}, cwd=".")

    assert not result.is_error
    assert "closed" in result.output.lower()


def test_always_requires_permission():
    assert BrowserDevtoolsTool.requires_permission({"action": "navigate", "url": "x"}) is True
    assert BrowserDevtoolsTool.requires_permission({"action": "get_console_logs"}) is True


def test_unknown_action_is_rejected_by_schema_validation():
    with pytest.raises(Exception):
        BrowserDevtoolsTool().execute({"action": "totally_fake_action"}, cwd=".")


def test_worker_call_dispatches_to_dedicated_thread_not_caller_thread():
    """Regression test for the real bug: Playwright's Sync API must never
    run on the thread that also drives prompt_toolkit's asyncio loop (the
    main/caller thread). Confirms _call() executes the function on the
    worker's own thread, not synchronously on the caller's thread."""
    import threading

    worker = bd_module._BrowserWorker()
    seen_thread_ids = []

    def probe():
        seen_thread_ids.append(threading.get_ident())
        return "ok"

    caller_thread_id = threading.get_ident()
    result = worker._call(probe)

    assert result == "ok"
    assert len(seen_thread_ids) == 1
    assert seen_thread_ids[0] != caller_thread_id  # ran on a different thread
    worker.close()


def test_worker_reuses_same_thread_across_multiple_calls():
    """The worker thread should be started once and reused, not spawned per
    call -- otherwise the browser session (bound to one thread) would break."""
    import threading

    worker = bd_module._BrowserWorker()
    thread_ids = []

    def probe():
        thread_ids.append(threading.get_ident())

    worker._call(probe)
    worker._call(probe)
    worker._call(probe)

    assert len(set(thread_ids)) == 1  # all three ran on the same worker thread
    worker.close()


def test_worker_propagates_exceptions_from_worker_thread_to_caller():
    worker = bd_module._BrowserWorker()

    def boom():
        raise ValueError("something broke in the browser")

    with pytest.raises(ValueError, match="something broke in the browser"):
        worker._call(boom)
    worker.close()
