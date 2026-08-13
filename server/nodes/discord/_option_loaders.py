"""Dropdown loaders for the parameter panel."""

from __future__ import annotations

from typing import Any, Dict, List

from core.logging import get_logger

logger = get_logger(__name__)


async def load_accounts(params: Dict[str, Any] | None = None) -> List[Dict[str, str]]:
    """Bot accounts with a stored token.

    The blank entry is first and is not a placeholder: it selects the default
    credential, which is what a single-bot install has.
    """
    from ._accounts import DEFAULT_ACCOUNT, list_accounts

    options = [{"name": "Default credential", "value": ""}]
    for account in await list_accounts():
        if account.account_id == DEFAULT_ACCOUNT:
            continue
        options.append({"name": account.label, "value": account.account_id})
    return options


async def load_guilds(params: Dict[str, Any] | None = None) -> List[Dict[str, str]]:
    """Servers the bot is a member of."""
    from . import _base
    from ._accounts import DEFAULT_ACCOUNT

    account_id = (params or {}).get("account_id") or DEFAULT_ACCOUNT
    try:
        guilds = await _base.get("users/@me/guilds", account_id=account_id)
    except Exception as exc:
        # A dropdown that cannot load must not break the panel.
        logger.debug("discord guild list unavailable", error=str(exc))
        return []
    return [
        {"name": g.get("name") or g.get("id", ""), "value": str(g.get("id", ""))}
        for g in (guilds if isinstance(guilds, list) else [])
    ]


async def load_channels(params: Dict[str, Any] | None = None) -> List[Dict[str, str]]:
    """Text channels in the selected server."""
    from . import _base
    from ._accounts import DEFAULT_ACCOUNT

    params = params or {}
    guild_id = params.get("guild_id")
    if not guild_id:
        return []
    account_id = params.get("account_id") or DEFAULT_ACCOUNT
    try:
        channels = await _base.get(f"guilds/{guild_id}/channels", account_id=account_id)
    except Exception as exc:
        logger.debug("discord channel list unavailable", error=str(exc))
        return []
    # Types 0 and 5 are text and announcement channels; the rest cannot take
    # a plain message.
    return [
        {"name": f"#{c.get('name', '')}", "value": str(c.get("id", ""))}
        for c in (channels if isinstance(channels, list) else [])
        if c.get("type") in (0, 5)
    ]


__all__ = ["load_accounts", "load_channels", "load_guilds"]
