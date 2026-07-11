"""Unit tests for headlabs.local.compaction — context estimation and summarization logic."""
from __future__ import annotations

from headlabs.local.compaction import (
    estimate_tokens, needs_compaction, build_compaction_messages,
    format_for_summarization, apply_compaction, PRESERVE_LAST_N,
)


def _make_history(n_messages: int, content_size: int = 100) -> list[dict]:
    """Create a fake history with n messages of given content size."""
    history = [{"role": "system", "content": "You are helpful."}]
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": "x" * content_size})
    return history


def test_estimate_tokens_basic():
    history = [{"role": "user", "content": "a" * 400}]
    tokens = estimate_tokens(history)
    assert tokens == 100  # 400 chars / 4


def test_needs_compaction_false_when_short():
    history = _make_history(3, content_size=100)
    assert needs_compaction(history, context_limit=16384) is False


def test_needs_compaction_true_when_long():
    # 100 messages × 1000 chars each = 100,000 chars = ~25,000 tokens
    # context_limit=16384 → threshold=13107 tokens → 25000 > 13107 → should compact
    history = _make_history(100, content_size=1000)
    assert needs_compaction(history, context_limit=16384) is True


def test_build_compaction_messages_preserves_last_n():
    history = _make_history(20, content_size=50)
    to_summarize, to_preserve = build_compaction_messages(history)
    assert len(to_preserve) == PRESERVE_LAST_N
    assert to_preserve[-1] == history[-1]
    assert len(to_summarize) == 20 - PRESERVE_LAST_N  # excludes system prompt (idx 0)


def test_format_for_summarization_includes_roles():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "tool", "content": "result of tool call"},
    ]
    text = format_for_summarization(messages)
    assert "[user]" in text
    assert "[assistant]" in text
    assert "[tool]" in text
    assert "hello" in text


def test_format_for_summarization_truncates_long_tool_output():
    messages = [{"role": "tool", "content": "x" * 2000}]
    text = format_for_summarization(messages)
    assert len(text) < 2000
    assert "..." in text


def test_apply_compaction_produces_valid_history():
    history = _make_history(20, content_size=50)
    summary = "This is the conversation summary."
    new_history = apply_compaction(history, summary)

    # Should have: system, summary (user), ack (assistant), then preserved messages
    assert new_history[0]["role"] == "system"
    assert "[Context summary" in new_history[1]["content"]
    assert summary in new_history[1]["content"]
    assert new_history[2]["role"] == "assistant"  # synthetic ack
    assert len(new_history) == 3 + PRESERVE_LAST_N  # system + summary + ack + preserved


def test_apply_compaction_preserves_recent_messages():
    history = _make_history(20, content_size=50)
    original_last = history[-1]
    new_history = apply_compaction(history, "summary text")
    assert new_history[-1] == original_last
