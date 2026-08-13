"""Adapter mapping ``socialSend``'s generic shape onto discordSend.

Registered as the ``"discord"`` platform from this package's ``__init__.py``.
The send operation is called on the node itself rather than reimplemented, so
the splitting, attachment handling and error mapping stay in one place.
"""

from __future__ import annotations

from typing import Any, Dict

from services.plugin import NodeUserError


async def social_send_adapter(payload: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """Translate a ``socialSend`` payload into a ``discordSend`` call."""
    from .discord_send import DiscordSendNode, DiscordSendParams

    recipient = payload.get("recipient", "")
    recipient_type = payload.get("recipient_type", "channel")
    message_type = payload.get("message_type", "text")

    if message_type != "text":
        raise NodeUserError(
            f"socialSend can only send text through Discord, not '{message_type}'. "
            "Use the discordSend node for embeds and attachments."
        )

    # socialSend addresses a person as "user" and everything else by id.
    target_type = "user" if recipient_type == "user" else "channel"
    params = DiscordSendParams(
        target_type=target_type,
        channel_id="" if target_type == "user" else recipient,
        user_id=recipient if target_type == "user" else "",
        message=payload.get("message", ""),
    )
    result = await DiscordSendNode().send(ctx, params)

    # The operation raises on failure, so reaching here is success.
    return {"success": True, **result.model_dump(mode="json", exclude_none=True)}


__all__ = ["social_send_adapter"]
