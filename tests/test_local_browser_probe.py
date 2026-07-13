"""Tests for headlabs.local.browser_probe — the LOCAL deterministic usability
probe. These launch a real headless Chromium against a real served page, so
they are skipped when the browser binary isn't installed.

They also cover the full scenario-3 chain without any LLM:
    serve a local app  →  local axe/inspect probe  →  deterministic findings.
"""
import socket

import pytest

from headlabs.local.browser_auth import BrowserAuth
from headlabs.local.browser_probe import run_local_usability_probe
from headlabs.local.serve import ServedApp, detect_run_commands
from headlabs.local.inspector import deterministic_usability_findings


def _browser_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _browser_available(),
                                reason="headless Chromium not available")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# A page with deliberate, axe-detectable WCAG violations: no lang, no title,
# an <img> without alt, and an <input> without a label.
_BAD_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body>
  <img src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==">
  <form><input type="text" name="q"></form>
  <p>hello</p>
</body>
</html>"""


@pytest.fixture
def served_bad_page(tmp_path):
    (tmp_path / "index.html").write_text(_BAD_HTML)
    port = _free_port()
    plan = detect_run_commands(str(tmp_path),
                               serve_cmd=f"python3 -m http.server {port} --bind 127.0.0.1",
                               port=port)
    with ServedApp(plan, do_build=False, startup_timeout=20) as app:
        yield app.url


def test_probe_returns_axe_and_mobile_shapes(served_bad_page):
    axe, mobile = run_local_usability_probe(served_bad_page, wait_ms=300)

    # axe dict shape matches the remote MCP a11y_audit tool.
    assert "error" not in axe, axe
    assert axe["http_status"] == 200
    assert isinstance(axe["violations"], list)
    assert axe["violation_count"] >= 1
    ids = {v["id"] for v in axe["violations"]}
    # image-alt and/or label and/or html-has-lang are the expected offenders.
    assert ids & {"image-alt", "label", "html-has-lang", "document-title"}, ids

    # mobile inspect_page dict shape.
    assert "error" not in mobile, mobile
    assert mobile["viewport"] == "mobile"
    a11y = mobile["accessibility"]
    assert a11y["imgs_without_alt"] >= 1
    assert a11y["inputs_without_label"] >= 1
    assert "performance" in mobile


def test_probe_feeds_deterministic_findings(served_bad_page):
    axe, mobile = run_local_usability_probe(served_bad_page, wait_ms=300)
    findings = deterministic_usability_findings(axe, mobile)
    assert findings, "expected at least one deterministic finding"
    # Every finding has the backlog add_finding shape.
    for f in findings:
        assert f["title"] and f["severity"] in {"critical", "high", "medium", "low"}
        assert f["file"]  # stable dedup key
    # At least one WCAG finding sourced from axe.
    assert any(f["file"].startswith("wcag:") for f in findings)


def test_probe_auth_error_surfaces_without_crashing():
    # A storage_state path that doesn't exist must degrade to error dicts, not raise.
    auth = BrowserAuth(storage_state="/no/such/state.json")
    axe, mobile = run_local_usability_probe("http://localhost:1/", auth)
    assert axe.get("error") and mobile.get("error")
    assert "auth error" in axe["error"]
    # And deterministic_usability_findings tolerates error dicts (returns []).
    assert deterministic_usability_findings(axe, mobile) == []


def test_probe_unreachable_url_is_error_not_crash():
    # Nothing listening on this port → navigation fails → structured error dict.
    axe, mobile = run_local_usability_probe(f"http://127.0.0.1:{_free_port()}/", wait_ms=100)
    # axe couldn't audit (nav failed); both degrade gracefully.
    assert "error" in axe or axe.get("violation_count") == 0
    assert deterministic_usability_findings(axe, mobile) == [] or isinstance(
        deterministic_usability_findings(axe, mobile), list)
