"""Unit tests for headlabs.local.tools.ask_user_question — stdin mocked."""
from unittest.mock import patch

from headlabs.local.tools.ask_user_question import AskUserQuestionTool


def test_ask_user_question_returns_answer():
    with patch("builtins.input", return_value="my answer"):
        result = AskUserQuestionTool().execute({"question": "What should I do?"}, cwd=".")
    assert not result.is_error
    assert result.output == "my answer"


def test_ask_user_question_strips_whitespace():
    with patch("builtins.input", return_value="  spaced answer  "):
        result = AskUserQuestionTool().execute({"question": "Q?"}, cwd=".")
    assert result.output == "spaced answer"


def test_ask_user_question_handles_eof_gracefully():
    """Non-interactive runs (e.g. --yes / piped stdin) must not crash."""
    with patch("builtins.input", side_effect=EOFError):
        result = AskUserQuestionTool().execute({"question": "Q?"}, cwd=".")
    assert not result.is_error
    assert "did not answer" in result.output.lower()


def test_ask_user_question_handles_keyboard_interrupt_gracefully():
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        result = AskUserQuestionTool().execute({"question": "Q?"}, cwd=".")
    assert not result.is_error
    assert "did not answer" in result.output.lower()


def test_ask_user_question_empty_answer_handled():
    with patch("builtins.input", return_value=""):
        result = AskUserQuestionTool().execute({"question": "Q?"}, cwd=".")
    assert not result.is_error
    assert "empty answer" in result.output.lower()


def test_ask_user_question_never_requires_permission():
    assert AskUserQuestionTool.requires_permission({}) is False


def test_ask_user_question_prints_the_question(capsys):
    with patch("builtins.input", return_value="ok"):
        AskUserQuestionTool().execute({"question": "Should I proceed with X?"}, cwd=".")
    captured = capsys.readouterr()
    assert "Should I proceed with X?" in captured.out
