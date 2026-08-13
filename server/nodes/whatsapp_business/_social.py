"""Adapter mapping ``socialSend``'s generic shape onto Cloud API params.

Registered as the ``"whatsapp_business"`` platform from this package's
``__init__.py``. Distinct from ``nodes/whatsapp/`` (the personal-account
bridge), which registers ``"whatsapp"`` — the two share no credential and no
API, so socialSend exposes them as separate channels.

The operations are called on the node itself rather than reimplemented here,
so the Cloud API request shaping, the per-type limits and the error mapping
all stay in one place.
"""

from __future__ import annotations

from typing import Any, Dict

from services.plugin import NodeUserError

# socialSend's media_source vocabulary -> the Cloud API's. socialSend has no
# notion of a pre-uploaded Meta media id, so "id" is unreachable from here;
# base64 arrives as an upload envelope that coerce_file_param understands, so
# it takes the same path as a workspace file.
_MEDIA_SOURCES = {"url": "link", "file": "file", "base64": "file"}


async def social_send_adapter(payload: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """Translate a ``socialSend`` payload into a ``whatsappBusinessSend`` call."""
    from .whatsapp_business_send import (
        WhatsAppBusinessSendNode,
        WhatsAppBusinessSendParams,
    )

    recipient = payload.get("recipient", "")
    message_type = payload.get("message_type", "text")
    node = WhatsAppBusinessSendNode()

    if message_type == "text":
        params = WhatsAppBusinessSendParams(
            operation="send_text",
            to=recipient,
            text=payload.get("message", ""),
        )
        result = await node.send_text(ctx, params)

    elif message_type in {"image", "video", "audio", "document", "sticker"}:
        source = payload.get("media_source", "url")
        params = WhatsAppBusinessSendParams(
            operation="send_media",
            to=recipient,
            media_type=message_type,
            media_source=_MEDIA_SOURCES.get(source, "link"),
            media_url=payload.get("media_url", ""),
            media=payload.get("media_data") or payload.get("file_path"),
            caption=payload.get("caption", ""),
            media_filename=payload.get("filename", ""),
        )
        result = await node.send_media(ctx, params)

    elif message_type == "location":
        params = WhatsAppBusinessSendParams(
            operation="send_location",
            to=recipient,
            latitude=payload.get("latitude", 0),
            longitude=payload.get("longitude", 0),
            location_name=payload.get("location_name", ""),
            location_address=payload.get("address", ""),
        )
        result = await node.send_location(ctx, params)

    else:
        raise NodeUserError(
            f"socialSend cannot send '{message_type}' through WhatsApp Business. "
            "Use text, image, video, audio, document, sticker or location, or "
            "use the whatsappBusinessSend node directly for templates and "
            "interactive messages."
        )

    # The operations raise NodeUserError on failure, so reaching here is
    # success. socialSend keys on this field.
    return {"success": True, **result.model_dump(mode="json", exclude_none=True)}


__all__ = ["social_send_adapter"]
