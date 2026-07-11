"""Context compaction for `headlabs local` — automatic summarization when
conversation history approaches the model's context window limit.

Strategy (derived from OpenCode/Claude Code patterns):
- Estimate token count heuristically (chars / 4 is a rough but fast approximation
  that avoids pulling in a tokenizer dependency).
- When estimated tokens > COMPACTION_THRESHOLD_RATIO × context_limit, trigger
  compaction: ask the model itself to summarize the conversation so far, then
  replace the history with [system_prompt, summary_message, last_N_messages].
- The summary is tagged as a special "system" or "user" message so the model
  knows it's reading a summary, not the original conversation.
- Preserve the last N messages raw (not summarized) so the model has immediate
  local context to work from — summarizing EVERYTHING including the very last
  exchange would lose critical detail.

This module provides the logic; the CLI layer calls it between turns.
"""
from __future__ import annotations

COMPACTION_THRESHOLD_RATIO = 0.80  # trigger at 80% of context limit
PRESERVE_LAST_N = 6  # keep this many recent messages unsummarized
CHARS_PER_TOKEN = 4  # rough heuristic, no tokenizer needed

SUMMARY_PROMPT = (
    "The conversation so far has grown long. Summarize the key points, decisions, "
    "files edited, tools used, and current state of the task in a compact paragraph. "
    "Include file paths and specific details the assistant will need to continue working. "
    "Do NOT include greetings or meta-commentary about summarizing — just the facts."
)


def estimate_tokens(history: list[dict]) -> int:
    """Cheap heuristic token count — no external tokenizer dependency."""
    total_chars = sum(len(str(m.get("content", ""))) for m in history)
    # tool_calls and tool messages add overhead
    total_chars += sum(
        len(str(m.get("tool_calls", ""))) for m in history if m.get("tool_calls")
    )
    return total_chars // CHARS_PER_TOKEN


def needs_compaction(history: list[dict], context_limit: int) -> bool:
    """Returns True if the history should be compacted."""
    if len(history) <= PRESERVE_LAST_N + 2:  # system + a few messages — too short to compact
        return False
    estimated = estimate_tokens(history)
    threshold = int(context_limit * COMPACTION_THRESHOLD_RATIO)
    return estimated > threshold


def build_compaction_messages(history: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split history into (messages_to_summarize, messages_to_preserve).
    Returns the two halves — caller asks the model to summarize the first part."""
    # Always preserve: [0] = system prompt, last PRESERVE_LAST_N messages
    system_msg = history[0] if history and history[0].get("role") == "system" else None
    start_idx = 1 if system_msg else 0

    # Messages to keep raw at the end
    preserve_start = max(start_idx, len(history) - PRESERVE_LAST_N)
    to_summarize = history[start_idx:preserve_start]
    to_preserve = history[preserve_start:]

    return to_summarize, to_preserve


def format_for_summarization(messages: list[dict]) -> str:
    """Flatten a list of messages into a readable text block for the model to summarize."""
    parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if role == "tool":
            # Truncate long tool outputs in the summary input
            content = content[:500] + ("..." if len(content) > 500 else "")
        if content:
            parts.append(f"[{role}] {content}")
        # Include tool call names (without full args, to save space)
        if m.get("tool_calls"):
            names = [tc.get("function", {}).get("name", "?") for tc in m["tool_calls"]]
            parts.append(f"[{role}] called tools: {', '.join(names)}")
    return "\n".join(parts)


def apply_compaction(
    history: list[dict], summary_text: str
) -> list[dict]:
    """Replace the history with [system, summary_as_user_msg, preserved_recent_msgs].
    Called after the model produces the summary."""
    system_msg = history[0] if history and history[0].get("role") == "system" else None
    _, to_preserve = build_compaction_messages(history)

    new_history = []
    if system_msg:
        new_history.append(system_msg)

    # Insert the summary as a user message (the model reads it as context briefing)
    new_history.append({
        "role": "user",
        "content": (
            "[Context summary from earlier in this conversation — "
            "the original messages have been compacted to save context space]\n\n"
            + summary_text
        ),
    })
    # Add a synthetic assistant ack so the conversation flow is valid
    new_history.append({
        "role": "assistant",
        "content": "Understood. I have the context from the summary above and will continue from here.",
    })
    # Preserve recent messages
    new_history.extend(to_preserve)

    return new_history
