"""Single source of truth for what ``telegramSend`` can send.

Both entry points — the workflow node (``telegram_send.py``) and the direct
WebSocket command (``_handlers.py:handle_telegram_send``) — route through
``perform_send`` here. They used to carry two independent copies of the
message-type dispatch, which had already drifted: the WebSocket copy never
forwarded ``silent`` or ``reply_to_message_id`` (neither name appeared in that
file), so a caller could ask for a silent threaded reply over the socket and
get a loud top-level message instead. Its per-type checks were otherwise
equivalent to the node's; validating through ``TelegramSendParams`` now keeps
the two from diverging again as message types are added.

Dispatch is a table keyed by message type, so adding a type is one entry plus
one ``Literal`` member rather than a new branch in two files.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from services.plugin import NodeUserError

from .telegram_send import TelegramSendParams


async def resolve_chat_id(service: Any, params: TelegramSendParams) -> str | int:
    """Resolve the destination chat, restoring a stored owner if needed.

    ``recipient_type="self"`` targets the bot owner, who is normally captured
    on the first private message. After a process restart the in-memory value
    is gone but the credential row survives, so fall back to it before giving
    up.
    """
    from core.logging import get_logger

    log = get_logger(__name__)

    if params.recipient_type != "self":
        if not params.chat_id:
            raise NodeUserError("chat_id is required")
        return params.chat_id

    chat_id = service.owner_chat_id
    if chat_id:
        return chat_id

    try:
        from services.plugin.deps import get_auth_service

        saved = await get_auth_service().get_api_key("telegram_owner_chat_id")
        if saved:
            owner_id = int(saved)
            await service.set_owner(owner_id)
            log.info(f"[Telegram] Owner restored from credentials: {owner_id}")
            return owner_id
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[Telegram] Failed to restore owner: {exc}")

    raise NodeUserError(
        "Bot owner not detected. Send any private message to your bot "
        "on Telegram to auto-detect, or set TELEGRAM_OWNER_CHAT_ID in .env",
    )


async def _send_text(service: Any, params: TelegramSendParams, common: Dict[str, Any]) -> Dict[str, Any]:
    if not params.text:
        raise NodeUserError("text is required for text message")
    return await service.send_message(
        text=params.text,
        parse_mode=params.parse_mode or None,
        **common,
    )


async def _send_photo(service: Any, params: TelegramSendParams, common: Dict[str, Any]) -> Dict[str, Any]:
    if not params.media_url:
        raise NodeUserError("media_url is required for photo message")
    return await service.send_photo(
        photo=params.media_url,
        caption=params.caption or None,
        parse_mode=params.parse_mode or None,
        **common,
    )


async def _send_document(service: Any, params: TelegramSendParams, common: Dict[str, Any]) -> Dict[str, Any]:
    if not params.media_url:
        raise NodeUserError("media_url is required for document message")
    return await service.send_document(
        document=params.media_url,
        caption=params.caption or None,
        parse_mode=params.parse_mode or None,
        **common,
    )


async def _send_location(service: Any, params: TelegramSendParams, common: Dict[str, Any]) -> Dict[str, Any]:
    if params.latitude is None or params.longitude is None:
        raise NodeUserError("latitude and longitude are required for location message")
    return await service.send_location(
        latitude=float(params.latitude),
        longitude=float(params.longitude),
        **common,
    )


async def _send_contact(service: Any, params: TelegramSendParams, common: Dict[str, Any]) -> Dict[str, Any]:
    if not params.phone_number or not params.first_name:
        raise NodeUserError("phone_number and first_name are required for contact message")
    return await service.send_contact(
        phone_number=params.phone_number,
        first_name=params.first_name,
        last_name=params.last_name or None,
        **common,
    )


_DISPATCH: Dict[str, Callable[[Any, TelegramSendParams, Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {
    "text": _send_text,
    "photo": _send_photo,
    "document": _send_document,
    "location": _send_location,
    "contact": _send_contact,
}


async def perform_send(
    service: Any,
    chat_id: str | int,
    params: TelegramSendParams,
) -> Dict[str, Any]:
    """Validate the type-specific params and dispatch to the service.

    Returns the raw service result; callers shape their own envelope.
    """
    handler = _DISPATCH.get(params.message_type)
    if handler is None:
        raise NodeUserError(f"Unsupported message type: {params.message_type}")

    reply_to: Optional[int] = (
        int(params.reply_to_message_id) if params.reply_to_message_id else None
    )
    common = dict(
        chat_id=chat_id,
        disable_notification=params.silent,
        reply_to_message_id=reply_to,
    )
    return await handler(service, params, common)


__all__ = ["perform_send", "resolve_chat_id"]
