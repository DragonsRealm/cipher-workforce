"""whatsappBusinessSend — send a message via Meta's WhatsApp Cloud API."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._base import graph_post, normalize_recipient, resolve_phone_number_id
from ._credentials import WhatsAppBusinessCredential

# Meta's documented ceiling for a text body. Longer bodies are rejected
# outright rather than truncated: silently dropping the tail of an outbound
# business message is worse than a clear failure.
_MAX_TEXT_BODY = 4096


class WhatsAppBusinessSendParams(BaseModel):
    """Operator configuration, persisted on the node."""

    # Required once a node declares more than one @Operation: _pick_operation
    # reads this off the raw parameters, and an absent value resolves to no
    # operation at all rather than a default.
    operation: Literal["send_text", "send_media"] = Field(
        default="send_text",
        description="What to send.",
    )
    to: str = Field(
        default="",
        description="Recipient phone number in international format (e.g. +14155551234).",
    )
    text: str = Field(
        default="",
        description="Message body.",
        json_schema_extra={"rows": 4},
    )
    preview_url: bool = Field(
        default=False,
        description="Render a link preview for the first URL in the body.",
    )
    reply_to_message_id: str = Field(
        default="",
        description="Optional message id to reply to, threading the message.",
    )
    format_markdown: bool = Field(
        default=True,
        description="Convert GFM markdown to WhatsApp's *bold* / _italic_ syntax.",
    )

    # --- send_media ---------------------------------------------------------
    # Named media_type, NOT message_type. ParameterRenderer.getFileAcceptType
    # hardcodes a switch on a sibling parameter literally called
    # `message_type` and, when present, overrides whatever `accept` the
    # backend declared. Sidestepping the name keeps this node's accept list
    # authoritative without a frontend change.
    media_type: Literal["image", "video", "audio", "document", "sticker"] = Field(
        default="image",
        description="Kind of media to send.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_media"]}}},
    )
    media_source: Literal["file", "id", "link"] = Field(
        default="file",
        description=(
            "file: upload from the workspace. id: a media ID already uploaded. "
            "link: a public URL Meta fetches itself."
        ),
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_media"]}}},
    )
    media: Any = Field(
        default=None,
        description="File to send.",
        json_schema_extra={
            "widget": "file",
            "accept": "*/*",
            "displayOptions": {"show": {"media_source": ["file"]}},
        },
    )
    media_id: str = Field(
        default="",
        description="Existing Meta media ID.",
        json_schema_extra={"displayOptions": {"show": {"media_source": ["id"]}}},
    )
    media_url: str = Field(
        default="",
        description="Publicly reachable URL. Meta downloads it directly.",
        json_schema_extra={"displayOptions": {"show": {"media_source": ["link"]}}},
    )
    caption: str = Field(
        default="",
        description="Caption. Not supported for audio or sticker.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["send_media"]}}},
    )
    media_filename: str = Field(
        default="",
        description="Filename shown to the recipient. Documents only.",
        json_schema_extra={"displayOptions": {"show": {"media_type": ["document"]}}},
    )

    # There is deliberately NO phone_number_id parameter.
    #
    # It selects which business identity a message is sent *from*, so it is
    # exactly the field a prompt injection in an inbound message would want to
    # set. On a dual-purpose ActionNode there is no way to protect it: the
    # split-schema machinery (ToolInput / server_controlled_fields) is a
    # ToolNode extension, and BaseNode.execute_as_tool sends
    # ``{**node_params, **tool_args}`` for everything else -- model arguments
    # win, and ctx.raw["_raw_parameters"] is that same merged dict, so reading
    # from it protects nothing.
    #
    # The sending number therefore comes only from the credential, where the
    # model cannot reach it. A per-node override belongs with multi-number
    # support, which is deferred (see the plan's 6.4).

    model_config = ConfigDict(extra="ignore")


class WhatsAppBusinessSendOutput(BaseModel):
    message_id: Optional[str] = None
    to: Optional[str] = None
    wa_id: Optional[str] = None
    phone_number_id: Optional[str] = None
    # Meta's reference schema always documents this, but no per-type example
    # includes it, so it is parsed as optional rather than assumed.
    message_status: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class WhatsAppBusinessSendNode(ActionNode):
    type = "whatsappBusinessSend"
    display_name = "WhatsApp Business Send"
    subtitle = "Cloud API"
    group = ("whatsapp_business", "tool")
    description = "Send a WhatsApp message through Meta's official Cloud API"
    component_kind = "square"
    tool_name = "whatsapp_business_send"
    tool_description = (
        "Send a WhatsApp text message to a phone number using the official "
        "WhatsApp Business Cloud API. Only works if the recipient messaged the "
        "business within the last 24 hours; outside that window an approved "
        "template is required instead."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (WhatsAppBusinessCredential,)
    annotations = {"destructive": False, "readonly": False, "open_world": True}
    task_queue = TaskQueue.MESSAGING
    usable_as_tool = True

    # ToolInput / server_controlled_fields are deliberately NOT declared.
    # They are ToolNode extensions; on a dual-purpose ActionNode they are
    # silently inert (BaseNode.execute_as_tool short-circuits before reading
    # them), so declaring them would advertise a protection that does not
    # exist. Fields the model must not control are kept out of Params instead.
    Params = WhatsAppBusinessSendParams
    Output = WhatsAppBusinessSendOutput

    @Operation("send_text", cost={"service": "whatsapp_business", "action": "send_text", "count": 1})
    async def send_text(
        self,
        ctx: NodeContext,
        params: WhatsAppBusinessSendParams,
    ) -> WhatsAppBusinessSendOutput:
        recipient = normalize_recipient(params.to)
        body = params.text or ""
        if not body.strip():
            raise NodeUserError("Cannot send an empty WhatsApp message.")

        if params.format_markdown:
            from services.markdown_formatter import to_whatsapp

            body = to_whatsapp(body)

        if len(body) > _MAX_TEXT_BODY:
            raise NodeUserError(
                f"Message body is {len(body)} characters; WhatsApp accepts at most "
                f"{_MAX_TEXT_BODY}. Split it before sending."
            )

        preview_url = params.preview_url
        reply_to = (params.reply_to_message_id or "").strip()
        phone_number_id = await resolve_phone_number_id(ctx)

        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": preview_url, "body": body},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}

        result = await graph_post(ctx, f"{phone_number_id}/messages", payload)
        return _shape_send_result(result, recipient, phone_number_id)

    @Operation("send_media", cost={"service": "whatsapp_business", "action": "send_media", "count": 1})
    async def send_media(
        self, ctx: NodeContext, params: WhatsAppBusinessSendParams
    ) -> WhatsAppBusinessSendOutput:
        recipient = normalize_recipient(params.to)
        phone_number_id = await resolve_phone_number_id(ctx)
        media_type = params.media_type

        # `id` and `link` are a strict oneOf in Meta's schema -- sending both
        # is rejected, so exactly one is built here.
        media_object: Dict[str, Any] = {}
        if params.media_source == "link":
            if not params.media_url.strip():
                raise NodeUserError("Provide a media URL, or switch the source to file/id.")
            media_object["link"] = params.media_url.strip()
        elif params.media_source == "id":
            if not params.media_id.strip():
                raise NodeUserError("Provide a media ID, or switch the source to file/link.")
            media_object["id"] = params.media_id.strip()
        else:
            media_object["id"] = await _upload_for_send(ctx, params, phone_number_id)

        # Meta rejects a caption on audio and sticker rather than ignoring it.
        if params.caption.strip() and media_type not in {"audio", "sticker"}:
            media_object["caption"] = params.caption.strip()
        if media_type == "document" and params.media_filename.strip():
            media_object["filename"] = params.media_filename.strip()

        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": media_type,
            media_type: media_object,
        }
        reply_to = (params.reply_to_message_id or "").strip()
        if reply_to:
            payload["context"] = {"message_id": reply_to}

        result = await graph_post(ctx, f"{phone_number_id}/messages", payload)
        return _shape_send_result(result, recipient, phone_number_id)


async def _upload_for_send(
    ctx: NodeContext, params: WhatsAppBusinessSendParams, phone_number_id: str
) -> str:
    """Upload a workspace file and return the media id Meta assigns."""
    from services.media import coerce_file_param

    if params.media in (None, ""):
        raise NodeUserError("Select a file to send, or switch the media source to id/link.")

    filename, blob = coerce_file_param(params.media, ctx=ctx)
    import mimetypes

    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    result = await graph_post(
        ctx,
        f"{phone_number_id}/media",
        # Bytes, not a file handle: Connection.request replays the same kwargs
        # on its one auth retry, and a consumed handle would replay empty.
        files={"file": (filename, blob, mime_type)},
        data={"messaging_product": "whatsapp", "type": mime_type},
    )
    media_id = result.get("id")
    if not media_id:
        raise NodeUserError("WhatsApp accepted the upload but returned no media ID.")
    return str(media_id)


def _shape_send_result(
    result: Dict[str, Any], recipient: str, phone_number_id: str
) -> WhatsAppBusinessSendOutput:
    messages: List[Dict[str, Any]] = result.get("messages") or []
    contacts: List[Dict[str, Any]] = result.get("contacts") or []
    first = messages[0] if messages else {}
    return WhatsAppBusinessSendOutput(
        message_id=first.get("id"),
        message_status=first.get("message_status"),
        to=recipient,
        wa_id=(contacts[0].get("wa_id") if contacts else None),
        phone_number_id=phone_number_id,
    )


__all__ = [
    "WhatsAppBusinessSendNode",
    "WhatsAppBusinessSendOutput",
    "WhatsAppBusinessSendParams",
]
