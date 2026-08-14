"""WebSocket commands for the credentials modal.

The message-type strings are the frontend contract. Renaming one is a
breaking change regardless of what happens on this side.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from fastapi import WebSocket

from core.logging import get_logger
from services.events.oauth_lifecycle import make_oauth_lifecycle_handlers
from services.plugin.ws import ws_response

from ._accounts import DEFAULT_ACCOUNT, list_accounts
from ._oauth import build_oauth

logger = get_logger(__name__)


def _account_id(data: Dict[str, Any]) -> str:
    return (data or {}).get("account_id") or DEFAULT_ACCOUNT


async def _snapshot() -> Dict[str, Any]:
    """Every known account plus its live connection state.

    Accounts come from stored credentials rather than from the gateway
    registry, so one that has never been connected still appears.
    """
    from ._gateway import known_gateways

    gateways = known_gateways()
    accounts = []
    for account in await list_accounts():
        gateway = gateways.get(account.account_id)
        entry = {
            "account_id": account.account_id,
            "label": account.label,
            "application_id": account.application_id,
            "connected": bool(gateway and gateway.is_running()),
        }
        if gateway is not None:
            entry.update(gateway.status_snapshot())
        accounts.append(entry)
    return {
        "connected": any(a["connected"] for a in accounts),
        "accounts": accounts,
    }


@ws_response
async def handle_discord_connect(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    from ._gateway import get_gateway

    gateway = await get_gateway(_account_id(data))
    await gateway.start()
    return {"success": True, **await _snapshot()}


@ws_response
async def handle_discord_disconnect(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    from ._gateway import get_gateway

    gateway = await get_gateway(_account_id(data))
    await gateway.stop()
    return {"success": True, **await _snapshot()}


@ws_response
async def handle_discord_reconnect(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    from ._gateway import get_gateway

    gateway = await get_gateway(_account_id(data))
    # No RestartPolicy: a failed reconnect is usually a bad token or a
    # missing intent, and retrying those spends the daily IDENTIFY budget to
    # reach the same answer.
    await gateway.restart()
    return {"success": True, **await _snapshot()}


@ws_response
async def handle_discord_status(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    return {"success": True, **await _snapshot()}


@ws_response
async def handle_discord_accounts(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    snapshot = await _snapshot()
    return {"success": True, "accounts": snapshot["accounts"]}


def _user_info_to_subject(info: Dict[str, Any]) -> str:
    username = info.get("username") or ""
    return f"@{username}" if username else "Unknown"


# discord_oauth_login / discord_oauth_status / discord_logout. Without these
# the callback route exists but nothing can start the flow.
_OAUTH_HANDLERS = make_oauth_lifecycle_handlers(
    provider="discord",
    oauth_factory=build_oauth,
    user_info_to_subject=_user_info_to_subject,
)


WS_HANDLERS: Dict[str, Callable[[Dict[str, Any], WebSocket], Awaitable[Dict[str, Any]]]] = {
    "discord_connect": handle_discord_connect,
    "discord_disconnect": handle_discord_disconnect,
    "discord_reconnect": handle_discord_reconnect,
    "discord_status": handle_discord_status,
    "discord_accounts": handle_discord_accounts,
    **_OAUTH_HANDLERS,
}


__all__ = ["WS_HANDLERS"]
