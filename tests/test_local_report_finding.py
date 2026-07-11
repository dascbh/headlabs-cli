"""Unit tests for the ReportFindingTool — the inspector's structured-finding
tool. Hermetic: writes only under tmp_path."""
from __future__ import annotations

import pytest

from headlabs.local import backlog
from headlabs.local.tools import ALL_TOOLS, ReportFindingTool


def test_not_registered_in_all_tools():
    # It must only be available to `local inspect`, never to run/chat.
    assert ReportFindingTool not in ALL_TOOLS


def test_read_only_and_no_permission():
    assert ReportFindingTool.requires_permission({}) is False
    assert ReportFindingTool.is_read_only() is True


def test_execute_records_finding(tmp_path):
    d = str(tmp_path)
    res = ReportFindingTool().execute(
        {"severity": "high", "title": "XSS", "detail": "unescaped output",
         "fix": "escape", "file": "views.py", "line": 5, "role": "security"},
        cwd=d,
    )
    assert res.is_error is False
    assert "XSS" in res.output
    items = backlog.load_backlog(d)
    assert len(items) == 1 and items[0]["resource"] == "views.py:5"


def test_schema_exposes_required_fields():
    schema = ReportFindingTool.to_api_schema()
    props = schema["input_schema"]["properties"]
    for field in ("severity", "title", "detail", "fix", "file", "line", "role"):
        assert field in props


def test_invalid_input_raises_validation_error():
    # Engine catches this; here we assert validate_input enforces the schema.
    with pytest.raises(Exception):
        ReportFindingTool.validate_input({"title": "no severity/detail"})
