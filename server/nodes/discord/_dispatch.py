"""Turn a discord.py message object into a flat event payload.

Every snowflake is stringified here. discord.py exposes them as int, while
Discord's own JSON uses strings, and an id past 2^53 loses precision the
moment anything treats it as a number -- edge conditions comparing an id are
the most natural thing a user writes, and two distinct 18-digit ids comparing
equal is a bug with no visible cause.

Attachments travel as metadata, never bytes. A node result is persisted,
broadcast, and replayed into LLM context, so the payload is a reference and
discordAction fetches it on demand.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.logging import get_logger

logger = get_logger(__name__)

# Enough to fetch the file later and to decide whether it is worth fetching.
# Deliberately scalar-only: no nested media objects, no thumbnails.
_ATTACHMENT_FIELDS = ("id", "filename", "size", "url", "content_type", "width", "height")


def _sid(value: Any) -> Optional[str]:
    """Snowflake to string, preserving None."""
    return None if value is None else str(value)


def shape_attachment(attachment: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for field in _ATTACHMENT_FIELDS:
        value = getattr(attachment, field, None)
        if value is not None:
            payload[field] = _sid(value) if field == "id" else value
    return payload


def shape_message(message: Any, *, account_id: str) -> Dict[str, Any]:
    """Flatten one message into the trigger's output shape."""
    author = getattr(message, "author", None)
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    reference = getattr(message, "reference", None)

    attachments = [shape_attachment(a) for a in (getattr(message, "attachments", None) or [])]

    return {
        "account_id": account_id,
        "message_id": _sid(getattr(message, "id", None)),
        "channel_id": _sid(getattr(channel, "id", None)),
        "channel_name": getattr(channel, "name", None),
        "guild_id": _sid(getattr(guild, "id", None)),
        "guild_name": getattr(guild, "name", None),
        # None for a DM, which is also how a consumer tells the two apart.
        "is_dm": guild is None,
        "author_id": _sid(getattr(author, "id", None)),
        "author_name": getattr(author, "name", None),
        "author_display_name": getattr(author, "display_name", None),
        "author_is_bot": bool(getattr(author, "bot", False)),
        "content": getattr(message, "content", "") or "",
        "timestamp": (
            message.created_at.isoformat() if getattr(message, "created_at", None) else None
        ),
        "attachments": attachments,
        "has_attachments": bool(attachments),
        "mentions_me": _mentions_me(message),
        "reply_to_message_id": _sid(getattr(reference, "message_id", None)),
    }


def _mentions_me(message: Any) -> bool:
    """Whether the bot was mentioned.

    Useful as a filter because without the message-content intent this is one
    of the few signals that still arrives populated.
    """
    try:
        state = getattr(message, "_state", None)
        me = getattr(state, "user", None) if state else None
        if me is None:
            return False
        return any(getattr(u, "id", None) == me.id for u in (message.mentions or []))
    except Exception:
        return False


async def dispatch_message(message: Any, *, account_id: str) -> None:
    """Shape and emit one inbound message."""
    from ._events import dispatch_discord_message_received

    payload = shape_message(message, account_id=account_id)
    await dispatch_discord_message_received(payload)


__all__ = ["dispatch_message", "shape_attachment", "shape_message"]
