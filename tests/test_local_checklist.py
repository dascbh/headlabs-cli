"""Unit tests for headlabs.local.checklist — parsing and verdict mapping.
No browser/platform involved."""
from headlabs.local.checklist import (
    ChecklistItem, parse_checklist, build_checklist_instruction, evaluate_results,
)


class _Result:
    def __init__(self, raw_output=None, summary=""):
        self.raw_output = raw_output
        self.summary = summary


# ── parse_checklist ─────────────────────────────────────────────────────────

def test_parse_mixed_markers():
    text = """# Formulários

    - [ ] Todo campo tem label visível
    * Botão primário destacado
    1. Estados de loading/erro presentes
    - [x] Contraste AA no texto
    """
    items = parse_checklist(text)
    assert [i.text for i in items] == [
        "Todo campo tem label visível",
        "Botão primário destacado",
        "Estados de loading/erro presentes",
        "Contraste AA no texto",
    ]
    assert all(i.severity is None for i in items)


def test_parse_severity_prefix():
    items = parse_checklist("- (high) Botão destacado\n- (crítico ignora) x\n- (LOW) rodapé")
    assert items[0].text == "Botão destacado" and items[0].severity == "high"
    # "(crítico ignora)" is not a valid severity token → kept as text, no severity
    assert items[1].severity is None
    assert items[2].severity == "low"


def test_parse_skips_headers_and_blanks():
    assert parse_checklist("# só header\n\n   \n## outro") == []


def test_parse_caps_items():
    items = parse_checklist("\n".join(f"- item {i}" for i in range(100)))
    assert len(items) == 50


# ── build_checklist_instruction ─────────────────────────────────────────────

def test_instruction_numbers_items_and_has_schema():
    items = [ChecklistItem("primeiro"), ChecklistItem("segundo")]
    instr = build_checklist_instruction(items, "http://x", "{}", context="foco extra")
    assert "1. primeiro" in instr and "2. segundo" in instr
    assert '"verdict": "pass"|"fail"|"na"' in instr
    assert "foco extra" in instr


# ── evaluate_results ────────────────────────────────────────────────────────

def test_evaluate_maps_verdicts_and_builds_findings():
    items = [ChecklistItem("A"), ChecklistItem("B", "high"), ChecklistItem("C")]
    result = _Result(raw_output=[
        {"n": 1, "verdict": "pass"},
        {"n": 2, "verdict": "fail", "evidence": "sem label", "severity": "low", "fix": "add label"},
        {"n": 3, "verdict": "na"},
    ])
    report, findings = evaluate_results(result, items)
    assert [r["verdict"] for r in report] == ["pass", "fail", "na"]
    assert len(findings) == 1
    f = findings[0]
    assert f["title"] == "Checklist: B"
    assert f["severity"] == "high"          # item severity overrides agent's "low"
    assert f["detail"] == "sem label"
    assert f["fix"] == "add label"
    assert f["file"] == "checklist:2"


def test_evaluate_uses_agent_severity_when_item_has_none():
    items = [ChecklistItem("A")]
    result = _Result(raw_output=[{"n": 1, "verdict": "fail", "severity": "critical"}])
    _, findings = evaluate_results(result, items)
    assert findings[0]["severity"] == "critical"


def test_evaluate_missing_or_unknown_verdict_defaults_na():
    items = [ChecklistItem("A"), ChecklistItem("B")]
    result = _Result(raw_output=[{"n": 1, "verdict": "banana"}])  # B absent entirely
    report, findings = evaluate_results(result, items)
    assert [r["verdict"] for r in report] == ["na", "na"]
    assert findings == []


def test_evaluate_reads_checklist_results_key():
    # The agent wraps the array under 'checklist_results' (observed in practice).
    items = [ChecklistItem("A"), ChecklistItem("B")]
    result = _Result(raw_output={"answer": "", "checklist_results": [
        {"n": 1, "verdict": "pass", "evidence": "ok"},
        {"n": 2, "verdict": "fail", "evidence": "faltou", "severity": "high"},
    ]})
    report, findings = evaluate_results(result, items)
    assert [r["verdict"] for r in report] == ["pass", "fail"]
    assert findings[0]["file"] == "checklist:2" and findings[0]["severity"] == "high"


def test_evaluate_reads_arbitrary_verdict_shaped_key():
    items = [ChecklistItem("A")]
    result = _Result(raw_output={"whatever_key": [{"n": 1, "verdict": "fail", "evidence": "x"}]})
    report, findings = evaluate_results(result, items)
    assert report[0]["verdict"] == "fail" and len(findings) == 1


def test_evaluate_parses_from_summary_when_no_raw_output():
    items = [ChecklistItem("A")]
    result = _Result(raw_output=None,
                     summary='blah [{"n":1,"verdict":"fail","evidence":"x"}] trailing')
    report, findings = evaluate_results(result, items)
    assert report[0]["verdict"] == "fail"
    assert findings[0]["severity"] == "medium"   # no item/agent severity → default
