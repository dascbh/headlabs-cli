"""Auto-fix loop for `headlabs local` — the edit→test→fix cycle.

After the model edits a file, this module:
1. Detects the project's test/lint command (from config, pyproject.toml, package.json, Makefile).
2. Runs it automatically.
3. If it fails, feeds the error output back to the model for correction.
4. Repeats up to MAX_AUTOFIX_RETRIES times.

Design: this is NOT integrated into the engine loop itself (which would
couple tool execution with testing policy). Instead, it's a post-turn hook
called by the CLI after engine.run() completes when edits were made. The
CLI can then call engine.run() again with the error context if auto-fix is
needed. This keeps the engine pure (tool loop only) and the fix policy in
the CLI layer where it belongs.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_AUTOFIX_RETRIES = 3
TEST_TIMEOUT_S = 60
MAX_ERROR_CHARS = 3000  # truncate test output fed back to the model


@dataclass
class AutofixTestCommand:
    """A detected test/lint command for the current project."""
    command: str
    source: str  # where it was detected from (e.g. "pyproject.toml", "config")


def detect_test_command(cwd: str, configured_command: str | None = None) -> AutofixTestCommand | None:
    """Detect the project's test command, in priority order:
    1. Explicitly configured (user set via config or .headlabs/autofix.json)
    2. pyproject.toml with pytest
    3. package.json with "test" script
    4. Makefile with "test" target
    Returns None if no test command can be detected.
    """
    if configured_command:
        return AutofixTestCommand(command=configured_command, source="config")

    root = Path(cwd)

    # Python: pyproject.toml or setup.cfg with pytest
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text()
        if "pytest" in content or "testpaths" in content:
            return AutofixTestCommand(command="python -m pytest -q --tb=short", source="pyproject.toml")

    # Node: package.json with test script
    package_json = root / "package.json"
    if package_json.exists():
        import json
        try:
            pkg = json.loads(package_json.read_text())
            if pkg.get("scripts", {}).get("test"):
                return AutofixTestCommand(command="npm test", source="package.json")
            if pkg.get("scripts", {}).get("lint"):
                return AutofixTestCommand(command="npm run lint", source="package.json")
        except (json.JSONDecodeError, OSError):
            pass

    # Makefile with test target
    makefile = root / "Makefile"
    if makefile.exists():
        content = makefile.read_text()
        if "\ntest:" in content or content.startswith("test:"):
            return AutofixTestCommand(command="make test", source="Makefile")

    return None


@dataclass
class AutofixResult:
    """Result of running the test command."""
    success: bool
    output: str  # stdout + stderr combined, truncated
    command: str


def run_test_command(test_cmd: AutofixTestCommand, cwd: str) -> AutofixResult:
    """Execute the test command and return the result."""
    try:
        proc = subprocess.run(
            test_cmd.command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return AutofixResult(
            success=False,
            output=f"Test command timed out after {TEST_TIMEOUT_S}s: {test_cmd.command}",
            command=test_cmd.command,
        )
    except OSError as exc:
        return AutofixResult(
            success=False, output=f"Failed to run test command: {exc}", command=test_cmd.command
        )

    combined = ""
    if proc.stdout:
        combined += proc.stdout
    if proc.stderr:
        combined += ("\n" if combined else "") + proc.stderr

    # Truncate but keep the tail (most useful part — error messages are usually at the end)
    if len(combined) > MAX_ERROR_CHARS:
        combined = "... (truncated)\n" + combined[-MAX_ERROR_CHARS:]

    return AutofixResult(
        success=proc.returncode == 0,
        output=combined,
        command=test_cmd.command,
    )


def build_fix_prompt(test_result: AutofixResult, edited_files: list[str]) -> str:
    """Build the prompt to send back to the model when tests fail after an edit."""
    files_str = ", ".join(edited_files) if edited_files else "(unknown files)"
    return (
        f"After your edits to {files_str}, the test command `{test_result.command}` failed.\n\n"
        f"Test output:\n```\n{test_result.output}\n```\n\n"
        "Please fix the code to make the tests pass. Read the error carefully and "
        "apply the minimal correction needed."
    )
