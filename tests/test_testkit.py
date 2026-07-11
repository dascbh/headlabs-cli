"""Tests for the closed-loop test toolkit (headlabs.testkit)."""

import pytest

import headlabs.trace_store as ts
from headlabs import testkit


@pytest.fixture
def store(tmp_path, monkeypatch):
    d = tmp_path / "traces"
    monkeypatch.setattr(ts, "TRACES_DIR", d)
    monkeypatch.setattr(ts, "_INDEX", d / "index.jsonl")
    return ts


_RAW = {
    "score": 72,
    "verdict": "NEEDS_WORK",
    "dimensions": {
        "task_completion": {"score": 80, "evidence": "did it"},
        "tool_correctness": {"score": 60, "evidence": "mostly"},
    },
    "top_issues": ["slow"],
    "fix_instructions": ["use fewer tools"],
}


def test_parse_evaluation_handles_fenced_json():
    raw = '```json\n{"score": 80, "verdict": "PASS"}\n```'
    assert testkit.parse_evaluation(raw)["score"] == 80


def test_parse_evaluation_handles_surrounding_prose():
    raw = 'Here is my evaluation:\n{"score": 50, "dimensions": {}}\nThanks!'
    assert testkit.parse_evaluation(raw)["score"] == 50


def test_parse_evaluation_rejects_objects_without_score():
    assert testkit.parse_evaluation('{"foo": 1}') is None
    assert testkit.parse_evaluation("no json here") is None


def test_verdict_thresholds():
    assert testkit.verdict_for(85) == "PASS"
    assert testkit.verdict_for(70) == "NEEDS_WORK"
    assert testkit.verdict_for(40) == "FAIL"


def test_normalize_coerces_and_fills_verdict():
    ev = testkit.normalize({"score": "88", "dimensions": {
        "accuracy": {"score": "90", "evidence": "x"}}},
        agent_id="finops", scenario="s", exec_time_s=1.5, tool_calls=3)
    assert ev.score == 88
    assert ev.verdict == "PASS"  # filled from threshold
    assert ev.dimensions["accuracy"]["score"] == 90
    assert ev.tool_calls == 3
    assert ev.exec_time_s == 1.5


def test_normalize_clamps_scores():
    ev = testkit.normalize({"score": 150}, agent_id="x")
    assert ev.score == 100


def test_persist_and_baseline_round_trip(store):
    ev = testkit.normalize(_RAW, agent_id="finops", scenario="s")
    trace = testkit.persist(ev)
    assert trace.workflow == "test"
    base = testkit.baseline("finops")
    assert base is not None
    assert base.score == 72
    assert base.verdict == "NEEDS_WORK"
    assert base.dimensions["task_completion"]["score"] == 80


def test_baseline_none_for_unknown_agent(store):
    assert testkit.baseline("nope") is None


def test_baseline_excludes_specified_trace(store):
    e1 = testkit.normalize({"score": 60}, agent_id="finops")
    t1 = testkit.persist(e1)
    t1.started_at = 100
    store.save_trace(t1)
    e2 = testkit.normalize({"score": 90}, agent_id="finops")
    t2 = testkit.persist(e2)
    t2.started_at = 200
    store.save_trace(t2)
    # Excluding the newest yields the older one as the baseline.
    base = testkit.baseline("finops", before_trace_id=t2.trace_id)
    assert base.score == 60


def test_compare_baseline_verdict_when_no_prior():
    after = testkit.normalize(_RAW, agent_id="finops")
    cmp = testkit.compare(None, after)
    assert cmp["verdict"] == "BASELINE"
    assert cmp["score"]["after"] == 72
    assert cmp["score"]["delta"] is None


def test_compare_improved():
    before = testkit.normalize({"score": 60}, agent_id="x")
    after = testkit.normalize({"score": 85}, agent_id="x")
    cmp = testkit.compare(before, after)
    assert cmp["verdict"] == "IMPROVED"
    assert cmp["score"]["delta"] == 25


def test_compare_regressed():
    before = testkit.normalize({"score": 85}, agent_id="x")
    after = testkit.normalize({"score": 60}, agent_id="x")
    assert testkit.compare(before, after)["verdict"] == "REGRESSED"


def test_compare_unchanged_within_noise():
    before = testkit.normalize({"score": 80}, agent_id="x")
    after = testkit.normalize({"score": 82}, agent_id="x")  # +2 < noise band
    assert testkit.compare(before, after)["verdict"] == "UNCHANGED"


def test_compare_dimension_deltas():
    before = testkit.normalize({"score": 70, "dimensions": {
        "accuracy": {"score": 50}}}, agent_id="x")
    after = testkit.normalize({"score": 90, "dimensions": {
        "accuracy": {"score": 80}}}, agent_id="x")
    cmp = testkit.compare(before, after)
    assert cmp["dimensions"]["accuracy"]["delta"] == 30


def test_report_json_shape():
    before = testkit.normalize({"score": 60}, agent_id="x")
    after = testkit.normalize({"score": 90}, agent_id="x")
    report = testkit.report_json(after, before=before)
    assert report["schema"] == "headlabs.test/v1"
    assert report["evaluation"]["score"] == 90
    assert report["baseline"]["score"] == 60
    assert report["comparison"]["verdict"] == "IMPROVED"
