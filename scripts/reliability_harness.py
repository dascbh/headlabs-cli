"""Exhaustive reliability harness for `headlabs local` against a real,
self-hosted 8B model. Not a pytest suite -- this is a research/tuning tool:
it runs a battery of known-failure-pattern cases against the REAL server,
captures objective signals (which tools were actually called, with which
arguments, in which order) rather than judging free-text output, and reports
pass/fail against explicit expectations.

Usage:
    AWS_PROFILE=enxstudios python3 scripts/reliability_harness.py [--verbose]

Each case declares:
  - prompt: what to ask
  - expect_tool_calls: list of tool names that MUST appear, in order (or
    "any_order=True" if sequence doesn't matter)
  - expect_no_fabrication_markers: substrings that would only appear if the
    model fabricated per-file details without reading them (derived from
    the REAL, wrong content it hallucinated in earlier live sessions)
  - min_tool_calls_per_target: e.g. multi-file cases require >=1 read/grep
    call per distinct file mentioned
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from headlabs.local.config import load_local_config
from headlabs.local.engine import EngineEvent, QueryEngine, SYSTEM_PROMPT
from headlabs.local.permission import PermissionManager
from headlabs.local.provider import OpenAICompatibleProvider, ProviderError
from headlabs.local.tools import ALL_TOOLS

REPO_ROOT = Path(__file__).resolve().parent.parent


def _extract_numbers_near_label(text: str, label: str, window: int = 60) -> list[int]:
    """Find integers appearing within `window` characters of `label` in
    `text`, case-insensitive. Deliberately crude (regex, not NLP) -- this is
    a cheap adversarial check, not a general-purpose fact extractor.

    Strips thousand separators (plain comma/dot/space, and the U+202F
    NARROW NO-BREAK SPACE some models use for readability, e.g. "42 925")
    before matching, so "42,925" / "42.925" / "42 925" all normalize to
    42925 instead of being missed or split into wrong numbers."""
    numbers = []
    for match in re.finditer(re.escape(label), text, re.IGNORECASE):
        start = max(0, match.start() - window)
        end = min(len(text), match.end() + window)
        snippet = text[start:end]
        # Normalize thousand-separated numbers like "42 925" or "42,925" to "42925"
        # before extracting, so formatting choices don't cause false negatives.
        normalized = re.sub(r"(?<=\d)[,.\u202f\u00a0 ](?=\d{3}\b)", "", snippet)
        numbers.extend(int(n) for n in re.findall(r"\b(\d+)\b", normalized))
    return numbers


@dataclass
class Case:
    name: str
    prompt: str
    expect_tools_called: set[str] = field(default_factory=set)
    min_calls_per_expected_tool: int = 1
    # Alternative valid tool(s) for tasks with more than one legitimate way to accomplish the
    # goal (e.g. "create and run a script" can go through edit_file+bash, or execute_python
    # writing+running the file itself). At least one of these must be called; unlike
    # expect_tools_called, this is an OR, not an AND-per-item.
    expect_any_of_tools: set[str] = field(default_factory=set)
    forbidden_output_substrings: list[str] = field(default_factory=list)
    require_nonempty_final_text: bool = True
    # Numeric ground-truth check: (label, expected_value, tolerance). The
    # harness looks for expected_value appearing in the final text within
    # +/- tolerance of any integer found near the label -- a cheap but real
    # check that catches "confidently wrong number" hallucinations, which
    # free-text substring checks cannot catch.
    numeric_ground_truths: list[tuple[str, int, int]] = field(default_factory=list)
    # Simpler alternative to numeric_ground_truths for cases where there's exactly one
    # unambiguous expected number in the whole answer and label-based proximity matching is
    # fragile (e.g. the model answers in English/Portuguese unpredictably, so a Portuguese
    # label like "resultado" may never appear near the number). Checks the ENTIRE final_text
    # for the number, not just a window near a label.
    expect_number_anywhere: tuple[int, int] | None = None  # (expected_value, tolerance)
    max_iterations: int = 15


@dataclass
class CaseResult:
    case: Case
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None
    duration_s: float = 0.0

    @property
    def tools_called(self) -> set[str]:
        return {name for name, _ in self.tool_calls}

    def passed(self) -> tuple[bool, list[str]]:
        reasons = []
        if self.error:
            return False, [f"engine raised: {self.error}"]

        missing = self.case.expect_tools_called - self.tools_called
        if missing:
            reasons.append(f"never called expected tool(s): {sorted(missing)}")

        if self.case.expect_any_of_tools and not (self.case.expect_any_of_tools & self.tools_called):
            reasons.append(
                f"expected at least one of {sorted(self.case.expect_any_of_tools)} to be called, "
                f"but only called: {sorted(self.tools_called)}"
            )

        if self.case.require_nonempty_final_text and not self.final_text.strip():
            reasons.append("final_text is empty -- model called tools but never produced a summary")

        for tool_name in self.case.expect_tools_called:
            count = sum(1 for n, _ in self.tool_calls if n == tool_name)
            if count < self.case.min_calls_per_expected_tool:
                reasons.append(
                    f"{tool_name!r} called {count}x, expected >= {self.case.min_calls_per_expected_tool}x"
                )

        for marker in self.case.forbidden_output_substrings:
            if marker.lower() in self.final_text.lower():
                reasons.append(f"final text contains forbidden/fabricated marker: {marker!r}")

        for label, expected, tolerance in self.case.numeric_ground_truths:
            found_numbers = _extract_numbers_near_label(self.final_text, label)
            if not found_numbers:
                reasons.append(f"no number found near label {label!r} to verify against ground truth {expected}")
            elif not any(abs(n - expected) <= tolerance for n in found_numbers):
                reasons.append(
                    f"numbers near {label!r} were {found_numbers}, none within {tolerance} of "
                    f"ground truth {expected} -- likely fabricated/guessed"
                )

        if self.case.expect_number_anywhere is not None:
            expected, tolerance = self.case.expect_number_anywhere
            normalized = re.sub(r"(?<=\d)[,.\u202f\u00a0 ](?=\d{3}\b)", "", self.final_text)
            found_numbers = [int(n) for n in re.findall(r"\b(\d+)\b", normalized)]
            if not any(abs(n - expected) <= tolerance for n in found_numbers):
                reasons.append(
                    f"expected number {expected} (tolerance {tolerance}) not found anywhere in "
                    f"final text -- numbers present: {found_numbers}"
                )

        return len(reasons) == 0, reasons


CASES: list[Case] = [
    Case(
        name="multi_file_analysis_no_fabrication",
        prompt=(
            "Analise como as tools read_file e grep funcionam de verdade no codigo, "
            "lendo os arquivos reais em src/headlabs/local/tools/."
        ),
        expect_tools_called={"read_file"},
        min_calls_per_expected_tool=2,  # must read BOTH files, not reuse one lookup for both
        forbidden_output_substrings=[
            "grep.py",  # real filename is grep_tool.py -- this exact wrong name was observed hallucinated
            "case_sensitive: bool = Field(True",  # real default is False -- observed wrong value hallucinated
        ],
    ),
    Case(
        name="specific_url_uses_fetch_not_search",
        prompt="Acesse o site https://example.com e me diga o que o texto da pagina contem.",
        expect_tools_called={"web_fetch"},
    ),
    Case(
        name="tool_failure_must_be_disclosed_not_fabricated",
        prompt=(
            "Use a tool bash para rodar o comando './this-script-does-not-exist.sh' "
            "e me diga o resultado."
        ),
        expect_tools_called={"bash"},
        forbidden_output_substrings=[
            "successfully",
            "concluído com sucesso",
        ],
    ),
    Case(
        name="nonexistent_file_must_report_not_invent_content",
        prompt="Leia o arquivo src/headlabs/local/tools/this_file_does_not_exist.py e resuma o conteudo.",
        expect_tools_called={"read_file"},
        forbidden_output_substrings=["class", "def "],  # any code-shaped content is fabricated
    ),
    Case(
        name="simple_single_tool_call_sanity_check",
        prompt="Use a tool bash para rodar 'echo hello_world_marker' e me diga o output exato.",
        expect_tools_called={"bash"},
    ),
    Case(
        name="three_distinct_files_all_verified",
        prompt=(
            "Leia os arquivos base.py, bash.py e read_file.py dentro de "
            "src/headlabs/local/tools/ e me diga o nome da classe definida em cada um."
        ),
        expect_tools_called={"read_file"},
        min_calls_per_expected_tool=3,
    ),
    Case(
        name="adversarial_five_files_higher_load",
        prompt=(
            "Leia os 5 arquivos em src/headlabs/local/tools/: config_tool.py, glob_tool.py, "
            "grep_tool.py, web_fetch.py e todo_write.py. Para cada um, me diga o nome exato "
            "da classe e se ele requer permissao (requires_permission) por padrao."
        ),
        expect_tools_called={"read_file"},
        min_calls_per_expected_tool=5,
        # 5 files means at least 5 read_file calls plus glob/discovery turns -- 15 was measured
        # too tight and cut the model off mid-synthesis (empty final_text), which is a harness
        # bug, not a model failure: with max_iterations=25 the same prompt completed correctly.
        max_iterations=25,
    ),
    Case(
        name="adversarial_similar_named_files_confusion",
        prompt=(
            "Compare o arquivo web_fetch.py com o arquivo web_search.py em "
            "src/headlabs/local/tools/. Quais bibliotecas HTTP cada um usa?"
        ),
        expect_tools_called={"read_file"},
        min_calls_per_expected_tool=2,
        forbidden_output_substrings=[
            "boto3",  # web_fetch.py does NOT use boto3 -- only web_search.py does (secrets manager)
        ],
    ),
    Case(
        name="adversarial_ambiguous_pronoun_reference",
        prompt=(
            "Leia o arquivo engine.py em src/headlabs/local/. Depois leia o arquivo permission.py "
            "no mesmo diretorio. Qual dos dois tem mais linhas de codigo? Diga o numero exato de "
            "linhas de cada arquivo."
        ),
        expect_tools_called={"read_file"},
        min_calls_per_expected_tool=2,
        # Ground truth computed dynamically at run time (below, in main()) via `wc -l` on the
        # real files -- NOT hardcoded, since these files are edited during development and a
        # stale hardcoded number would wrongly flag a correct answer as fabricated.
        numeric_ground_truths=[],  # populated in main() right before running this case
    ),
    Case(
        name="adversarial_partial_tool_failure_midway",
        prompt=(
            "Leia o arquivo base.py em src/headlabs/local/tools/, depois leia o arquivo "
            "totally_fake_nonexistent_module.py no mesmo diretorio, depois leia bash.py. "
            "Me diga o resultado de cada leitura."
        ),
        expect_tools_called={"read_file"},
        min_calls_per_expected_tool=3,
        forbidden_output_substrings=["class FakeModule", "def totally_fake"],
    ),
    Case(
        name="execute_python_arithmetic_ground_truth",
        prompt=(
            "Use a tool execute_python para calcular a soma dos quadrados dos numeros de 1 a 50 "
            "(ou seja, 1^2 + 2^2 + ... + 50^2). Me diga o resultado exato."
        ),
        expect_tools_called={"execute_python"},
        # sum(i**2 for i in range(1, 51)) == 42925 -- real, computable ground truth, not an
        # estimate. A model that "does math in its head" instead of running the code will
        # very likely miss this exact value. Uses expect_number_anywhere (not a label-based
        # check) because the model may answer in English or Portuguese unpredictably, making
        # a fixed label like "resultado" unreliable.
        expect_number_anywhere=(42925, 0),
    ),
    Case(
        name="bash_create_and_run_script_ground_truth",
        prompt=(
            "Crie um script bash chamado /tmp/headlabs_harness_check.sh que imprime a soma "
            "de 17 e 25 usando aritmetica do bash (nao escreva o numero direto, calcule). "
            "De permissao de execucao e execute o script. Me diga o output exato que ele "
            "imprimiu."
        ),
        expect_tools_called={"bash"},
        min_calls_per_expected_tool=1,
        # 17 + 25 == 42, computed by the script itself, then reported. Reduces the chance the
        # model just prints a fabricated number without actually running anything.
        expect_number_anywhere=(42, 0),
    ),
    Case(
        name="edit_file_then_execute_verifies_real_behavior",
        prompt=(
            "Crie um arquivo /tmp/headlabs_harness_fib.py com uma funcao fibonacci(n) que "
            "retorna o n-esimo numero de Fibonacci (fibonacci(0)=0, fibonacci(1)=1). Depois "
            "execute esse arquivo (ou use execute_python) para calcular fibonacci(15) e me "
            "diga o resultado exato."
        ),
        # Two legitimate paths observed: (a) edit_file to write the .py file, then bash/
        # execute_python to run it, or (b) execute_python writing and running the file itself
        # via open()/exec in one call. Both are valid "created and executed real code" --
        # requiring edit_file specifically was over-constraining the harness, not a model bug.
        expect_any_of_tools={"edit_file", "execute_python"},
        # fibonacci(15) == 610, a real computable value. Catches both "wrote wrong code" and
        # "never actually ran it, just guessed the answer".
        numeric_ground_truths=[("fibonacci", 610, 0)],
    ),
]


def run_case(case: Case, provider_factory) -> CaseResult:
    provider = provider_factory()
    permission_manager = PermissionManager(str(REPO_ROOT), mode="auto")  # no interactive prompts
    engine = QueryEngine(
        provider,
        ALL_TOOLS,
        permission_manager,
        cwd=str(REPO_ROOT),
        max_iterations=case.max_iterations,
        system_prompt=SYSTEM_PROMPT,
    )

    result = CaseResult(case=case)
    start = time.monotonic()

    def on_event(ev: EngineEvent) -> None:
        if ev.type == "tool_call":
            pass  # name-only event; full args come in the paired tool_result below via closure state
        if ev.type == "tool_result":
            result.tool_calls.append((ev.tool_name, dict(ev.tool_input)))

    try:
        result.final_text = engine.run(case.prompt, on_event=on_event)
    except ProviderError as exc:
        result.error = str(exc)
    finally:
        result.duration_s = time.monotonic() - start
        provider.close()

    return result


def main() -> int:
    verbose = "--verbose" in sys.argv
    repeat = 1
    for arg in sys.argv[1:]:
        if arg.startswith("--repeat="):
            repeat = int(arg.split("=", 1)[1])

    cfg = load_local_config()
    if not cfg.is_configured():
        print("headlabs local is not configured. Run: headlabs local config --base-url ... --model ...")
        return 2

    # Populate dynamic ground truth for the line-count case right before running --
    # avoids a hardcoded number going stale as engine.py/permission.py are edited.
    for case in CASES:
        if case.name == "adversarial_ambiguous_pronoun_reference":
            engine_lines = sum(1 for _ in (REPO_ROOT / "src/headlabs/local/engine.py").open())
            permission_lines = sum(1 for _ in (REPO_ROOT / "src/headlabs/local/permission.py").open())
            case.numeric_ground_truths = [
                ("engine.py", engine_lines, 3),
                ("permission.py", permission_lines, 3),
            ]

    print(f"Model under test: {cfg.model}")
    print(f"Server: {cfg.base_url}")
    print(f"Running {len(CASES)} cases x {repeat} repetition(s)...\n")

    case_pass_counts: dict[str, list[bool]] = {c.name: [] for c in CASES}
    all_results: list[CaseResult] = []

    for rep in range(repeat):
        if repeat > 1:
            print(f"### Repetition {rep + 1}/{repeat} ###\n")
        for case in CASES:
            print(f"--- {case.name} ---")
            result = run_case(case, lambda: OpenAICompatibleProvider(cfg))
            all_results.append(result)
            passed, reasons = result.passed()
            case_pass_counts[case.name].append(passed)
            status = "PASS" if passed else "FAIL"
            print(f"  [{status}] ({result.duration_s:.1f}s, {len(result.tool_calls)} tool calls: "
                  f"{[n for n, _ in result.tool_calls]})")
            if not passed:
                for r in reasons:
                    print(f"    - {r}")
            if verbose:
                print(f"    final_text: {result.final_text[:300]!r}")
            print()

    n_passed = sum(1 for r in all_results if r.passed()[0])
    print(f"\n{'=' * 60}")
    print(f"PER-CASE SUCCESS RATE ({repeat} repetition(s) each):")
    for name, outcomes in case_pass_counts.items():
        rate = sum(outcomes) / len(outcomes) * 100
        print(f"  {name}: {sum(outcomes)}/{len(outcomes)} ({rate:.0f}%)")
    print(f"{'=' * 60}")
    print(f"OVERALL: {n_passed}/{len(all_results)} passed ({n_passed / len(all_results) * 100:.0f}%)")
    print(f"{'=' * 60}")
    return 0 if n_passed == len(all_results) else 1


if __name__ == "__main__":
    sys.exit(main())
