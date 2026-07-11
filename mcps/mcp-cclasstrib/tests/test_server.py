"""Smoke test: verify MCP server imports cleanly."""
def test_import():
    import server
    assert hasattr(server, "mcp")
