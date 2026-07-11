"""Unit tests for headlabs.local.tools.execute_python."""
from headlabs.local.tools.execute_python import ExecutePythonTool


def test_execute_python_runs_code_and_captures_stdout(tmp_path):
    result = ExecutePythonTool().execute({"code": "print('hello from python')"}, cwd=str(tmp_path))
    assert not result.is_error
    assert "hello from python" in result.output
    assert "exit_code: 0" in result.output


def test_execute_python_captures_computation_result(tmp_path):
    result = ExecutePythonTool().execute({"code": "print(2 + 2)"}, cwd=str(tmp_path))
    assert not result.is_error
    assert "4" in result.output


def test_execute_python_captures_traceback_on_error(tmp_path):
    result = ExecutePythonTool().execute({"code": "raise ValueError('boom')"}, cwd=str(tmp_path))
    assert result.is_error
    assert "ValueError" in result.output
    assert "boom" in result.output
    assert "exit_code: 1" in result.output


def test_execute_python_syntax_error_captured(tmp_path):
    result = ExecutePythonTool().execute({"code": "def broken(:"}, cwd=str(tmp_path))
    assert result.is_error
    assert "SyntaxError" in result.output


def test_execute_python_multiline_code(tmp_path):
    code = """
x = 10
y = 20
print(f"sum={x + y}")
"""
    result = ExecutePythonTool().execute({"code": code}, cwd=str(tmp_path))
    assert not result.is_error
    assert "sum=30" in result.output


def test_execute_python_runs_in_given_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("present")
    code = "import os; print('marker.txt' in os.listdir('.'))"
    result = ExecutePythonTool().execute({"code": code}, cwd=str(tmp_path))
    assert "True" in result.output


def test_execute_python_timeout(tmp_path):
    result = ExecutePythonTool().execute(
        {"code": "import time; time.sleep(5)", "timeout_s": 1}, cwd=str(tmp_path)
    )
    assert result.is_error
    assert "timed out" in result.output.lower()


def test_execute_python_does_not_affect_headlabs_process(tmp_path):
    """Runs in a subprocess -- must not be able to crash or mutate the
    calling process's own state (e.g. sys.modules)."""
    import sys

    before = "headlabs" in sys.modules
    result = ExecutePythonTool().execute(
        {"code": "import sys; del sys.modules  # would crash if run in-process"}, cwd=str(tmp_path)
    )
    assert not result.is_error
    assert ("headlabs" in sys.modules) == before  # our own process is untouched


def test_execute_python_always_requires_permission():
    assert ExecutePythonTool.requires_permission({}) is True


def test_execute_python_stderr_captured_separately(tmp_path):
    code = "import sys; sys.stderr.write('warning message\\n')"
    result = ExecutePythonTool().execute({"code": code}, cwd=str(tmp_path))
    assert "stderr:" in result.output
    assert "warning message" in result.output
