"""Standalone standard-JSONL conversation normalization helpers.

Lines are Anthropic Messages API objects: ``{"role": "user"|"assistant",
"content": str | List[ContentBlock], ...}``. :func:`append_message` can
serialize extra metadata (``timestamp``, ``session_id``, ``model``, ...),
but :func:`parse_jsonl` intentionally drops it when normalizing rows to
native :class:`services.llm.protocol.Message` values.

No production agent currently calls these helpers. Claude Code owns and
reads its native session JSONL independently; this module remains a tested
primitive for normalized import/export.
"""

import json
from typing import Any, List, Tuple

from services.llm.protocol import Message


def parse_jsonl(text: str) -> List[Message]:
    """Standard JSONL -> native :class:`Message` list.

    Text content blocks are joined with spaces; tool-call blocks and extra
    metadata are discarded. Rows with unknown roles, non-text content, or
    unparseable JSON are skipped for forward compatibility.
    """
    if not text:
        return []
    out: List[Message] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = obj.get("role")
        content = obj.get("content")
        if isinstance(content, list):
            content = " ".join(
                blk.get("text", "") if isinstance(blk, dict) else str(blk)
                for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            )
        if not isinstance(content, str):
            continue
        if role == "user":
            out.append(Message(role="user", content=content))
        elif role == "assistant":
            out.append(Message(role="assistant", content=content))
    return out


def append_message(
    text: str,
    role: str,
    content: str,
    **metadata: Any,
) -> str:
    """Append one Anthropic Messages-format line to a JSONL string.

    Metadata fields ride alongside ``role`` / ``content`` on the serialized
    line, but :func:`parse_jsonl` does not surface them on ``Message``.
    Always emits a trailing newline so successive appends concatenate
    cleanly.
    """
    line = json.dumps(
        {"role": role, "content": content, **metadata},
        ensure_ascii=False,
    )
    if text and not text.endswith("\n"):
        text = text + "\n"
    return (text or "") + line + "\n"


def trim_window(text: str, window_size: int) -> Tuple[str, List[str]]:
    """Keep the last ``window_size * 2`` lines (~ N user/assistant
    pairs). Returns ``(trimmed, removed)``. Removed lines are returned
    verbatim so callers can hand them to the long-term vector store.
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    keep = window_size * 2
    if len(lines) <= keep:
        return text, []
    removed = lines[:-keep]
    trimmed = "\n".join(lines[-keep:]) + "\n"
    return trimmed, removed
