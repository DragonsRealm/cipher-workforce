"""Filter builder for discordReceive.

Reads every parameter once at build time and closes over the result, so the
per-event path is plain comparisons. The fields it inspects are exactly those
``_dispatch.shape_message`` produces -- the two move together.
"""

from __future__ import annotations

from typing import Any, Callable, Dict


def build_discord_filter(params: Dict[str, Any]) -> Callable[[Dict[str, Any]], bool]:
    """Return a predicate deciding whether one event should fire this node."""
    params = params or {}

    account_id = (params.get("account_id") or "").strip()
    scope = params.get("scope") or "all"
    guild_id = (params.get("guild_id") or "").strip()
    channel_id = (params.get("channel_id") or "").strip()
    author_id = (params.get("author_id") or "").strip()
    keywords = [k.strip().lower() for k in (params.get("keywords") or "").split(",") if k.strip()]
    ignore_bots = params.get("ignore_bots", True)
    require_mention = params.get("require_mention", False)
    require_attachment = params.get("require_attachment", False)

    def matches(event: Dict[str, Any]) -> bool:
        # An account filter is not a convenience: without it, connecting a
        # second bot would make every trigger fire for both.
        if account_id and event.get("account_id") != account_id:
            return False

        if ignore_bots and event.get("author_is_bot"):
            return False

        if scope == "dm" and not event.get("is_dm"):
            return False
        if scope == "guild" and event.get("is_dm"):
            return False

        if guild_id and event.get("guild_id") != guild_id:
            return False
        if channel_id and event.get("channel_id") != channel_id:
            return False
        if author_id and event.get("author_id") != author_id:
            return False

        if require_mention and not event.get("mentions_me"):
            return False
        if require_attachment and not event.get("has_attachments"):
            return False

        if keywords:
            content = (event.get("content") or "").lower()
            if not any(word in content for word in keywords):
                return False

        return True

    return matches


_INTERACTION_KINDS = {"command": 2, "component": 3, "modal": 5}


def build_interaction_filter(params: Dict[str, Any]) -> Callable[[Dict[str, Any]], bool]:
    """Predicate for discordInteraction, over _interactions.shape_interaction."""
    params = params or {}

    account_id = (params.get("account_id") or "").strip()
    kind = params.get("interaction_kind") or "all"
    command_name = (params.get("command_name") or "").strip().lstrip("/")
    custom_id = (params.get("custom_id") or "").strip()
    guild_id = (params.get("guild_id") or "").strip()

    expected_type = _INTERACTION_KINDS.get(kind)

    def matches(event: Dict[str, Any]) -> bool:
        if account_id and event.get("account_id") != account_id:
            return False
        if expected_type is not None and event.get("interaction_type") != expected_type:
            return False
        if command_name and event.get("command_name") != command_name:
            return False
        if custom_id and event.get("custom_id") != custom_id:
            return False
        if guild_id and event.get("guild_id") != guild_id:
            return False
        return True

    return matches


__all__ = ["build_discord_filter", "build_interaction_filter"]
