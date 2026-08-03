"""Message validation and filtering utilities.

Provider-agnostic -- works with any message format.
"""

from typing import Any, List, Sequence


def is_valid_message_content(content: Any) -> bool:
    """Check if message content is non-empty for API calls."""
    if content is None:
        return False
    if isinstance(content, list):
        return any((isinstance(b, dict) and b.get("text", "").strip()) or (isinstance(b, str) and b.strip()) for b in content)
    if isinstance(content, str):
        return bool(content.strip())
    return bool(content)


def filter_empty_messages(messages: Sequence) -> List:
    """Filter out messages with empty content.

    Works with native Message dataclasses and raw provider payloads.
    """
    filtered = []
    for m in messages:
        # Detect role across native messages and raw provider payloads
        role = getattr(m, "role", None) or getattr(m, "type", "")

        # Tool messages -- always keep
        if role == "tool":
            filtered.append(m)
            continue

        # AI/assistant replay state must survive even when it has no rendered
        # text. Provider compaction, reasoning signatures, and ordered output
        # blocks are inputs to the next request, not presentation content.
        if role in ("ai", "assistant"):
            tool_calls = getattr(m, "tool_calls", None)
            provider_state = getattr(m, "provider_state", None)
            blocks = getattr(m, "blocks", None)
            if tool_calls or provider_state or blocks:
                filtered.append(m)
                continue

        # Everything else -- keep only if content is non-empty
        content = getattr(m, "content", None)
        if is_valid_message_content(content):
            filtered.append(m)

    return filtered
