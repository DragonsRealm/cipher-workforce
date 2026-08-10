"""whatsappBusinessMedia — upload, resolve, download and delete Meta media.

Why this is a separate node rather than an ``auto_download`` flag on the
trigger: shaping happens inside the webhook request, where Meta expects a
prompt 200 and retries anything slower, and the deployed trigger path never
runs the node body at all. Downloading there would both delay the
acknowledgement and be unreachable once deployed. Wiring
``whatsappBusinessReceive -> whatsappBusinessMedia`` keeps the fetch on the
workflow's own time.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.plugin import ActionNode, NodeContext, NodeUserError, Operation, TaskQueue

from ._base import (
    CREDENTIAL_ID,
    GRAPH_API_VERSION,
    GRAPH_BASE_URL,
    graph_delete,
    graph_get,
    graph_post,
    resolve_phone_number_id,
)
from ._credentials import WhatsAppBusinessCredential

# Meta's own per-kind ceilings. Enforced before upload so an oversize file
# fails locally with a useful message instead of as a generic 131053.
_MAX_BYTES = {
    "audio": 16 * 1024 * 1024,
    "document": 100 * 1024 * 1024,
    "image": 5 * 1024 * 1024,
    "sticker": 500 * 1024,
    "video": 16 * 1024 * 1024,
}


class WhatsAppBusinessMediaParams(BaseModel):
    operation: Literal["upload", "get_url", "download", "delete"] = Field(
        default="download",
        description="What to do with the media.",
    )
    media_id: str = Field(
        default="",
        description="Meta media ID. Inbound messages carry one on `media.id`.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["get_url", "download", "delete"]}}},
    )
    file: Any = Field(
        default=None,
        description="File to upload.",
        json_schema_extra={
            "widget": "file",
            "accept": "*/*",
            "displayOptions": {"show": {"operation": ["upload"]}},
        },
    )
    mime_type: str = Field(
        default="",
        description="Override the detected MIME type. Meta rejects unsupported types.",
        json_schema_extra={"displayOptions": {"show": {"operation": ["upload"]}}},
    )

    model_config = ConfigDict(extra="ignore")


class WhatsAppBusinessMediaOutput(BaseModel):
    media_id: Optional[str] = None
    url: Optional[str] = None
    mime_type: Optional[str] = None
    sha256: Optional[str] = None
    file_size: Optional[int] = None
    # Populated by `download`. A reference, never the bytes -- a payload here
    # would be persisted, broadcast and replayed into LLM context.
    file: Optional[dict] = None
    files: List[dict] = Field(default_factory=list)
    success: Optional[bool] = None

    model_config = ConfigDict(extra="allow")


class WhatsAppBusinessMediaNode(ActionNode):
    type = "whatsappBusinessMedia"
    display_name = "WhatsApp Business Media"
    subtitle = "Upload / download"
    group = ("whatsapp_business", "tool")
    description = "Upload media to WhatsApp, or download media from an inbound message"
    component_kind = "square"
    tool_name = "whatsapp_business_media"
    tool_description = (
        "Download media from an inbound WhatsApp message into the workflow "
        "workspace, or upload a file to WhatsApp and get a media ID for sending. "
        "Inbound messages carry the media ID on media.id."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (WhatsAppBusinessCredential,)
    annotations = {"destructive": False, "readonly": False, "open_world": True}
    task_queue = TaskQueue.MESSAGING
    usable_as_tool = True

    Params = WhatsAppBusinessMediaParams
    Output = WhatsAppBusinessMediaOutput

    @Operation("upload", cost={"service": "whatsapp_business", "action": "media_upload", "count": 1})
    async def upload(
        self, ctx: NodeContext, params: WhatsAppBusinessMediaParams
    ) -> WhatsAppBusinessMediaOutput:
        from services.media import coerce_file_param

        if params.file in (None, ""):
            raise NodeUserError("Select a file to upload.")

        # One call handles all three shapes the UI can produce (workspace ref,
        # legacy base64 envelope, bare path) and enforces containment.
        filename, blob = coerce_file_param(params.file, ctx=ctx)

        mime_type = params.mime_type.strip() or _guess_mime(filename)
        kind = _kind_for(mime_type)
        cap = _MAX_BYTES.get(kind)
        if cap and len(blob) > cap:
            raise NodeUserError(
                f"{filename} is {len(blob) // (1024 * 1024)} MB; WhatsApp accepts at "
                f"most {cap // (1024 * 1024)} MB for {kind}."
            )

        phone_number_id = await resolve_phone_number_id(ctx)
        result = await graph_post(
            ctx,
            f"{phone_number_id}/media",
            files={"file": (filename, blob, mime_type)},
            data={"messaging_product": "whatsapp", "type": mime_type},
        )
        return WhatsAppBusinessMediaOutput(
            media_id=result.get("id"), mime_type=mime_type, file_size=len(blob)
        )

    @Operation("get_url")
    async def get_url(
        self, ctx: NodeContext, params: WhatsAppBusinessMediaParams
    ) -> WhatsAppBusinessMediaOutput:
        media_id = _require_media_id(params)
        phone_number_id = await resolve_phone_number_id(ctx)
        result = await graph_get(ctx, media_id, {"phone_number_id": phone_number_id})
        return WhatsAppBusinessMediaOutput(
            media_id=result.get("id") or media_id,
            url=result.get("url"),
            mime_type=result.get("mime_type"),
            sha256=result.get("sha256"),
            file_size=_as_int(result.get("file_size")),
        )

    @Operation("download", cost={"service": "whatsapp_business", "action": "media_download", "count": 1})
    async def download(
        self, ctx: NodeContext, params: WhatsAppBusinessMediaParams
    ) -> WhatsAppBusinessMediaOutput:
        """Resolve then fetch, in that order and without caching the URL.

        Meta's media URLs expire after five minutes, so resolving here rather
        than trusting one carried from an earlier node is what makes this work
        on a retry. The URL is also authenticated despite being signed, so the
        bearer token still has to be sent.
        """
        from services.media import fetch_to_workspace
        from services.media.preview import preview_kind

        media_id = _require_media_id(params)
        phone_number_id = await resolve_phone_number_id(ctx)

        resolved = await graph_get(ctx, media_id, {"phone_number_id": phone_number_id})
        url = resolved.get("url")
        if not url:
            raise NodeUserError(
                f"WhatsApp returned no download URL for media {media_id}. "
                "Media older than 30 days is deleted by Meta."
            )

        mime_type = resolved.get("mime_type") or ""
        async with ctx.connection(CREDENTIAL_ID) as conn:
            secrets = await conn.credentials()
        token = secrets.get("api_key")

        ref = await fetch_to_workspace(
            url,
            ctx=ctx,
            stem=f"whatsapp-{media_id[:12]}",
            # preview_kind maps a MIME to the renderer the UI can actually
            # show; "audio" is never claimed here because that kind asserts a
            # real container probe, which no download performs.
            kind=_ref_kind(preview_kind(mime_type)),
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = ref.model_dump(mode="json")
        return WhatsAppBusinessMediaOutput(
            media_id=media_id,
            mime_type=mime_type or ref.mime_type,
            sha256=resolved.get("sha256") or ref.sha256,
            file_size=ref.size_bytes,
            file=payload,
            files=[payload],
        )

    @Operation("delete")
    async def delete(
        self, ctx: NodeContext, params: WhatsAppBusinessMediaParams
    ) -> WhatsAppBusinessMediaOutput:
        media_id = _require_media_id(params)
        phone_number_id = await resolve_phone_number_id(ctx)
        result = await graph_delete(ctx, media_id, {"phone_number_id": phone_number_id})
        return WhatsAppBusinessMediaOutput(
            media_id=media_id, success=bool(result.get("success", True))
        )


def _require_media_id(params: WhatsAppBusinessMediaParams) -> str:
    media_id = (params.media_id or "").strip()
    if not media_id:
        raise NodeUserError(
            "No media ID supplied. An inbound WhatsApp message carries one on "
            "media.id."
        )
    return media_id


def _guess_mime(filename: str) -> str:
    import mimetypes

    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _kind_for(mime_type: str) -> str:
    base = (mime_type or "").split("/")[0].lower()
    if mime_type == "image/webp":
        return "sticker"
    if base in {"image", "audio", "video"}:
        return base
    return "document"


def _ref_kind(preview: str) -> str:
    """Map a preview verdict onto a FileRef kind.

    Never returns "audio": that kind asserts the duration came from a real
    probe, and a download measures nothing.
    """
    return preview if preview in {"image", "video"} else "file"


def _as_int(value: Any) -> Optional[int]:
    # Meta documents file_size as a number but serialises it as a string in
    # some responses.
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "WhatsAppBusinessMediaNode",
    "WhatsAppBusinessMediaOutput",
    "WhatsAppBusinessMediaParams",
]
