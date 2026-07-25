"""Telegram Receive — Wave 11.C migration (event-based trigger).

Long-polling Telegram bot dispatches events into ``event_waiter``
under ``telegram_message_received``. The plugin's filter narrows by
sender/content type; legacy ``build_telegram_filter`` stays wired
through ``FILTER_BUILDERS`` until 11.F unifies dispatch.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.plugin import NodeContext, Operation, TaskQueue, TriggerNode

from ._credentials import TelegramCredential


class TelegramReceiveParams(BaseModel):
    """7-field schema. Snake_case field names throughout; JSON Schema
    keys match field names exactly (no aliases).
    """

    content_type_filter: Literal[
        "all",
        "media",
        "text",
        "photo",
        "video",
        "audio",
        "voice",
        "animation",
        "video_note",
        "document",
        "sticker",
        "location",
        "contact",
        "poll",
    ] = Field(
        default="all",
        description=(
            "Filter by message content type. 'media' matches any message "
            "carrying a file (photo, video, audio, voice, animation, video "
            "note, sticker or document)."
        ),
    )
    sender_filter: Literal[
        "all",
        "self",
        "private",
        "group",
        "supergroup",
        "channel",
        "specific_chat",
        "specific_user",
        "keywords",
    ] = Field(
        default="all",
        description="Filter which messages trigger the workflow",
    )
    chat_id: str = Field(
        default="",
        description="Only trigger for messages from this specific chat ID",
        json_schema_extra={
            "displayOptions": {"show": {"sender_filter": ["specific_chat"]}},
        },
    )
    from_user: str = Field(
        default="",
        description="Only trigger for messages from this specific user ID",
        json_schema_extra={
            "displayOptions": {"show": {"sender_filter": ["specific_user"]}},
        },
    )
    keywords: str = Field(
        default="",
        description="Comma-separated keywords to trigger on (case-insensitive)",
        json_schema_extra={
            "displayOptions": {"show": {"sender_filter": ["keywords"]}},
        },
    )
    ignore_bots: bool = Field(
        default=True,
        description="Do not trigger on messages from other bots",
        json_schema_extra={
            "displayOptions": {
                "show": {
                    "sender_filter": [
                        "all",
                        "private",
                        "group",
                        "supergroup",
                        "channel",
                        "specific_chat",
                        "specific_user",
                        "keywords",
                    ]
                }
            },
        },
    )

    model_config = ConfigDict(extra="ignore")


class TelegramMedia(BaseModel):
    """Type-agnostic view of whatever file the message carried.

    ``None`` for text / location / contact / poll messages. This is the field
    downstream nodes and agents should read — the per-type blocks below are
    Telegram's own shapes and require per-kind branching, this one does not.

    It never carries bytes or base64: node outputs are persisted, broadcast,
    and serialized into LLM conversations, so media travels as an id plus (once
    downloaded) a workspace-relative path.
    """

    kind: Optional[str] = None  # photo|video|audio|voice|animation|video_note|sticker|document
    file_id: Optional[str] = None  # re-send via telegramSend without re-uploading
    file_unique_id: Optional[str] = None  # stable across bots — use as a dedup key
    mime_type: Optional[str] = None  # synthesised when Telegram omits it
    file_name: Optional[str] = None  # synthesised from kind + extension when absent
    file_size: Optional[int] = None
    duration: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    caption: Optional[str] = None
    file_path: Optional[str] = None  # set once downloaded; never bytes
    downloaded: Optional[bool] = None
    download_error: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class TelegramReceiveOutput(BaseModel):
    message_id: Optional[int] = None
    chat_id: Optional[int] = None
    chat_type: Optional[str] = None
    chat_title: Optional[str] = None
    from_id: Optional[int] = None
    from_username: Optional[str] = None
    from_first_name: Optional[str] = None
    from_last_name: Optional[str] = None
    is_bot: Optional[bool] = None
    text: Optional[str] = None
    caption: Optional[str] = None
    content_type: Optional[str] = None
    date: Optional[str] = None
    reply_to_message_id: Optional[int] = None
    # Normalized media view — read this instead of the per-type blocks.
    media: Optional[TelegramMedia] = None
    has_media: Optional[bool] = None
    media_group_id: Optional[str] = None  # shared by messages sent as one album
    reply_to: Optional[dict] = None  # bounded summary of the quoted message
    # Telegram-native per-type detail blocks.
    photo: Optional[dict] = None
    video: Optional[dict] = None
    audio: Optional[dict] = None
    voice: Optional[dict] = None
    animation: Optional[dict] = None
    video_note: Optional[dict] = None
    sticker: Optional[dict] = None
    document: Optional[dict] = None
    location: Optional[dict] = None
    contact: Optional[dict] = None
    poll: Optional[dict] = None

    model_config = ConfigDict(extra="allow")


class TelegramReceiveNode(TriggerNode):
    type = "telegramReceive"
    display_name = "Telegram Receive"
    subtitle = "Inbound Message"
    group = ("social", "trigger")
    description = "Trigger workflow when Telegram message is received"
    component_kind = "trigger"
    handles = ({"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},)
    credentials = (TelegramCredential,)
    task_queue = TaskQueue.TRIGGERS_EVENT
    mode = "event"
    event_type = "telegram_message_received"

    Params = TelegramReceiveParams
    Output = TelegramReceiveOutput

    def build_filter(self, params: TelegramReceiveParams) -> Callable[[Dict[str, Any]], bool]:
        # The filter body lives in this plugin folder (Wave 11.F moved it out
        # of services/event_waiter.py). This path is normally unreachable —
        # __init__.py pre-registers the builder and event_waiter only binds
        # ``build_filter`` for types absent from FILTER_BUILDERS — but it must
        # still import from the real location.
        from ._filters import build_telegram_filter

        return build_telegram_filter(params.model_dump())

    async def execute(
        self,
        node_id: str,
        parameters: Dict[str, Any],
        context,
    ) -> Dict[str, Any]:
        # Pre-flight: refuse to register a waiter if the bot isn't
        # connected. Matches the pre-refactor handler contract where a
        # disconnected bot short-circuits before event_waiter.register
        # and returns an error envelope.
        import time

        from ._service import get_telegram_service

        svc = get_telegram_service()
        if not getattr(svc, "connected", False):
            return self._wrap_error(
                start_time=time.time(),
                error=("Telegram bot not connected. Add bot token in Credentials."),
            )
        return await super().execute(node_id, parameters, context)

    @Operation("wait")
    async def wait(self, ctx: NodeContext, params: TelegramReceiveParams) -> TelegramReceiveOutput:
        raise NotImplementedError("Event triggers return via TriggerNode.execute, not the op body")
