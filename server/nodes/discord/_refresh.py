"""Status refresh and trigger pre-check.

The refresh is deliberately passive: it reports what is connected and does
not connect anything. Telegram auto-reconnects here, but that plugin holds at
most one bot. Firing up every stored Discord account on each WebSocket client
connect would open sessions nobody asked for and spend the account's
concurrent-session budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from services.status_broadcaster import StatusBroadcaster

logger = get_logger(__name__)


async def refresh_discord_status(broadcaster: "StatusBroadcaster") -> None:
    """Broadcast per-account gateway state on WebSocket connect."""
    try:
        from ._accounts import list_accounts
        from ._events import broadcast_discord_status
        from ._gateway import known_gateways

        gateways = known_gateways()
        accounts = []
        for account in await list_accounts():
            gateway = gateways.get(account.account_id)
            accounts.append(
                {
                    "account_id": account.account_id,
                    "label": account.label,
                    "connected": bool(gateway and gateway.is_running()),
                }
            )

        await broadcast_discord_status(
            connected=any(a["connected"] for a in accounts),
            accounts=accounts,
        )
    except Exception as exc:
        # A failed refresh must never break a client connecting.
        logger.debug("discord status refresh failed", error=str(exc))


async def precheck_discord_trigger(parameters: Dict[str, Any]) -> Optional[str]:
    """Refuse to arm a trigger with no connection behind it.

    Returning a string short-circuits the wait with that message instead of
    registering a waiter that can never resolve.
    """
    from ._accounts import DEFAULT_ACCOUNT
    from ._gateway import known_gateways

    account_id = (parameters or {}).get("account_id") or DEFAULT_ACCOUNT
    gateway = known_gateways().get(account_id)
    if gateway is None or not gateway.is_running():
        return (
            "Discord bot is not connected. Connect it from the Credentials modal "
            "before deploying this trigger."
        )
    return None


__all__ = ["precheck_discord_trigger", "refresh_discord_status"]
