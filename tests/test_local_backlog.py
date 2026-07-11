"""Unit tests for headlabs.local.backlog — per-project inspection backlog
persistence. Hermetic: writes only under a pytest tmp_path."""
from __future__ import annotations

import json

from headlabs.local import backlog


def test_load_empty_when_absent(tmp_path):
    assert backlog.load_backlog(str(tmp_path)) == []


def test_add_finding_persists_expected_shape(tmp_path):
    d = str(tmp_path)
    item = backlog.add_finding(
        d, role="security", severity="high", title="Hardcoded secret",
        detail="AWS key in source", fix="Move to Secrets Manager",
        file="app.py", line=10,
    )
    assert item["severity"] == "high"
    assert item["resource"] == "app.py:10"
    assert item["source"] == "inspector/security (local)"
    assert item["status"] == "open"

    on_disk = json.loads((tmp_path / ".headlabs" / "local_backlog.json").read_text())
    assert len(on_disk) == 1
    assert on_disk[0]["title"] == "Hardcoded secret"


def test_add_finding_dedupes_same_resource_and_title(tmp_path):
    d = str(tmp_path)
    backlog.add_finding(d, role="qa", severity="high", title="Bug", detail="a", file="x.py", line=1)
    backlog.add_finding(d, role="qa", severity="low", title="Bug", detail="b", file="x.py", line=1)
    assert len(backlog.load_backlog(d)) == 1


def test_add_finding_without_file_uses_title_as_resource(tmp_path):
    d = str(tmp_path)
    item = backlog.add_finding(d, role="qa", severity="medium", title="No tests", detail="")
    assert item["resource"] == "No tests"


def test_invalid_severity_defaults_to_medium(tmp_path):
    item = backlog.add_finding(str(tmp_path), role="qa", severity="wat", title="T", detail="")
    assert item["severity"] == "medium"


def test_set_status_marks_done(tmp_path):
    d = str(tmp_path)
    item = backlog.add_finding(d, role="qa", severity="high", title="T", detail="", file="a.py")
    assert backlog.set_status(d, item["id"], "done") is True
    assert backlog.load_backlog(d)[0]["status"] == "done"
    assert backlog.set_status(d, "nonexistent", "done") is False


def test_load_tolerates_corrupt_file(tmp_path):
    path = tmp_path / ".headlabs" / "local_backlog.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    assert backlog.load_backlog(str(tmp_path)) == []
