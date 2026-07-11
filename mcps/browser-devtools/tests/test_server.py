"""Smoke tests: the MCP server imports cleanly and registers its tools.

Deliberately does NOT launch a browser (Playwright import is lazy inside the
tools), so this runs anywhere — including the platform's `_verify_mcp_behavior`
subprocess, which boots the server and lists tools without a Chromium binary."""
import server


def test_import():
    assert hasattr(server, "mcp")


def test_helpers():
    assert server._valid_url("http://localhost:5173") is True
    assert server._valid_url("ftp://x") is False
    assert "error" in server._err("X", "y")
