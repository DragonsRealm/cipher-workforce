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


def _has_provider_payload(message: Any) -> bool:
    """True when provider_state carries actual replayable content.

    Providers attach provider_state to every assistant message, so its
    presence says nothing. Its ``payload.content`` is what distinguishes a
    compaction checkpoint (must survive, has no rendered text) from an
    empty turn (must be dropped, or it re-serializes to an empty content
    array the API rejects).
    """
    state = getattr(message, "provider_state", None)
    if not isinstance(state, dict):
        return bool(state)
    payload = state.get("payload")
    if not isinstance(payload, dict):
        return bool(payload)
    return any(bool(value) for value in payload.values())


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
            blocks = getattr(m, "blocks", None)
            # The bare PRESENCE of provider_state is not enough. Anthropic
            # attaches it to every assistant message, so keying on presence
            # retains genuinely empty turns (a refusal, a max_tokens stop);
            # those re-serialize as ``{"role":"assistant","content":[]}``,
            # which the API rejects with a 400 on every subsequent run
            # until memory is cleared — a wedge the conversation cannot
            # recover from. What matters is whether it carries replayable
            # payload: a compaction checkpoint lives here with no rendered
            # text and MUST survive.
            if tool_calls or blocks or _has_provider_payload(m):
                filtered.append(m)
                continue

        # Everything else -- keep only if content is non-empty
        content = getattr(m, "content", None)
        if is_valid_message_content(content):
            filtered.append(m)

    return filtered
