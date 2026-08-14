"""Interaction shaping and the response-token store.

Discord's interaction token is a 15-minute bearer credential that can post
messages as the app. A trigger's output is persisted three ways, broadcast
twice, retained in the status cache and replayed into LLM context on every
turn, so the token itself never leaves this module. The trigger emits an
opaque ``interaction_ref`` and discordAction trades it back here.

Refs do not survive a restart. That is deliberate rather than a gap: a
workflow that outlived a restart mid-interaction has already blown the
15-minute window, so persisting the token would only widen its exposure
without making anything work.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Dict, Optional, Tuple

from core.logging import get_logger

logger = get_logger(__name__)

# Discord interaction types.
PING = 1
APPLICATION_COMMAND = 2
MESSAGE_COMPONENT = 3
APPLICATION_COMMAND_AUTOCOMPLETE = 4
MODAL_SUBMIT = 5

# Interaction callback types.
PONG = 1
CHANNEL_MESSAGE_WITH_SOURCE = 4
DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE = 5
DEFERRED_UPDATE_MESSAGE = 6

# Discord invalidates the token at 15 minutes; expiring slightly earlier means
# a ref never resolves to a token that is already dead.
TOKEN_TTL_SECONDS = 14 * 60

_TOKENS: Dict[str, Tuple[str, str, float]] = {}


def store_token(application_id: str, token: str) -> str:
    """Stash a response token and return an opaque handle for it."""
    _expire()
    ref = secrets.token_urlsafe(16)
    _TOKENS[ref] = (application_id, token, time.monotonic() + TOKEN_TTL_SECONDS)
    return ref


def resolve_token(ref: str) -> Optional[Tuple[str, str]]:
    """Return ``(application_id, token)`` for a ref, or None if unknown."""
    _expire()
    entry = _TOKENS.get(ref)
    if entry is None:
        return None
    application_id, token, _ = entry
    return application_id, token


def _expire(now: Optional[float] = None) -> None:
    moment = now if now is not None else time.monotonic()
    for ref in [r for r, (_, _, expiry) in _TOKENS.items() if expiry <= moment]:
        _TOKENS.pop(ref, None)


def deferred_response_type(interaction_type: int) -> int:
    """How to acknowledge within the three-second deadline.

    A component click defers with UPDATE_MESSAGE so the existing message is
    edited; deferring it as a new message would post an empty one.
    """
    return (
        DEFERRED_UPDATE_MESSAGE
        if interaction_type == MESSAGE_COMPONENT
        else DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
    )


def shape_interaction(payload: Dict[str, Any], *, account_id: str) -> Dict[str, Any]:
    """Flatten an interaction into the trigger's output shape.

    Snowflakes are stringified for the same reason as in _dispatch: ids past
    2^53 lose precision the moment anything treats them as numbers.
    """
    data = payload.get("data") or {}
    member = payload.get("member") or {}
    user = member.get("user") or payload.get("user") or {}

    options = {
        option.get("name"): option.get("value")
        for option in (data.get("options") or [])
        if isinstance(option, dict) and option.get("name")
    }

    return {
        "account_id": account_id,
        "interaction_id": str(payload.get("id") or ""),
        "interaction_type": payload.get("type"),
        "application_id": str(payload.get("application_id") or ""),
        "command_name": data.get("name"),
        "custom_id": data.get("custom_id"),
        "component_type": data.get("component_type"),
        "options": options,
        "channel_id": str(payload.get("channel_id") or "") or None,
        "guild_id": str(payload.get("guild_id") or "") or None,
        "user_id": str(user.get("id") or "") or None,
        "user_name": user.get("username"),
        # The token itself is never emitted. discordAction resolves this.
        "interaction_ref": store_token(
            str(payload.get("application_id") or ""), str(payload.get("token") or "")
        ),
    }


__all__ = [
    "APPLICATION_COMMAND",
    "DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE",
    "DEFERRED_UPDATE_MESSAGE",
    "MESSAGE_COMPONENT",
    "PING",
    "PONG",
    "TOKEN_TTL_SECONDS",
    "deferred_response_type",
    "resolve_token",
    "shape_interaction",
    "store_token",
]
