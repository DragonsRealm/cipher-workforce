"""discordAction — the rest of the Discord REST surface.

Includes ``download_attachments``, which is where inbound media is fetched.
It is an operation here rather than a flag on the trigger because a deployed
trigger never runs its node body -- the engine marks it pre-executed and
passes its output through verbatim -- so a download flag there would work on
the canvas and silently do nothing once deployed. Wiring
``discordReceive -> discordAction`` puts the fetch on the workflow's own time.
This mirrors whatsappBusinessMedia, which exists for the same reason.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.plugin import NodeContext, NodeUserError, Operation, TaskQueue

from . import _base
from ._accounts import DEFAULT_ACCOUNT
from ._base import AccountScopedNode
from ._credentials import DiscordBotCredential


def _show(**conditions: Any) -> Dict[str, Any]:
    """displayOptions helper. The frontend ANDs the conditions."""
    return {"displayOptions": {"show": conditions}}


class DiscordActionParams(BaseModel):
    operation: Literal[
        "list_guilds",
        "list_channels",
        "get_channel",
        "create_thread",
        "list_messages",
        "get_message",
        "edit_message",
        "delete_message",
        "add_reaction",
        "pin_message",
        "download_attachments",
        "interaction_respond",
        "execute_webhook",
        "custom",
    ] = Field(default="list_channels", description="What to do.")

    account_id: str = Field(
        default="",
        description="Which Discord bot to act as. Blank uses the default credential.",
        json_schema_extra={"loadOptionsMethod": "discordAccounts"},
    )

    guild_id: str = Field(
        default="",
        description="Server ID.",
        json_schema_extra=_show(operation=["list_channels"]),
    )
    channel_id: str = Field(
        default="",
        description="Channel ID.",
        json_schema_extra=_show(
            operation=[
                "get_channel",
                "create_thread",
                "list_messages",
                "get_message",
                "edit_message",
                "delete_message",
                "add_reaction",
                "pin_message",
            ]
        ),
    )
    message_id: str = Field(
        default="",
        description="Message ID.",
        json_schema_extra=_show(
            operation=["get_message", "edit_message", "delete_message", "add_reaction", "pin_message"]
        ),
    )
    content: str = Field(
        default="",
        description="Replacement message text.",
        json_schema_extra={"rows": 3, **_show(operation=["edit_message"])},
    )
    emoji: str = Field(
        default="",
        description="Emoji to react with, e.g. a unicode emoji or name:id for a custom one.",
        json_schema_extra=_show(operation=["add_reaction"]),
    )
    thread_name: str = Field(
        default="",
        description="Name for the new thread.",
        json_schema_extra=_show(operation=["create_thread"]),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=100,
        description="How many messages to return.",
        json_schema_extra=_show(operation=["list_messages"]),
    )

    attachments: List[dict] = Field(
        default_factory=list,
        description=(
            "Attachment objects from a discordReceive message. Each needs url "
            "and filename."
        ),
        json_schema_extra=_show(operation=["download_attachments"]),
    )

    interaction_ref: str = Field(
        default="",
        description="The interaction_ref from a discordInteraction trigger.",
        json_schema_extra=_show(operation=["interaction_respond"]),
    )
    interaction_message: str = Field(
        default="",
        description="Message to send as the interaction response.",
        json_schema_extra={"rows": 3, **_show(operation=["interaction_respond"])},
    )

    webhook_url: str = Field(
        default="",
        description="Discord webhook URL. Posts without a bot token.",
        json_schema_extra={"password": True, **_show(operation=["execute_webhook"])},
    )
    webhook_content: str = Field(
        default="",
        description="Message to post through the webhook.",
        json_schema_extra={"rows": 3, **_show(operation=["execute_webhook"])},
    )

    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"] = Field(
        default="GET",
        description="HTTP method for the custom call.",
        json_schema_extra=_show(operation=["custom"]),
    )
    path: str = Field(
        default="",
        description="API path relative to the Discord API root, e.g. 'users/@me'.",
        json_schema_extra=_show(operation=["custom"]),
    )
    body: dict = Field(
        default_factory=dict,
        description="JSON body for the custom call.",
        json_schema_extra=_show(operation=["custom"]),
    )

    model_config = ConfigDict(extra="ignore")


class DiscordActionOutput(BaseModel):
    # Parsed API response. A list result lands in `items` so the declared
    # shape stays an object.
    result: Optional[dict] = None
    items: List[Any] = Field(default_factory=list)
    count: Optional[int] = None
    # download_attachments returns references, never bytes.
    files: List[dict] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class DiscordActionNode(AccountScopedNode):
    type = "discordAction"
    display_name = "Discord"
    subtitle = "Discord API"
    group = ("discord",)
    description = "Read and manage Discord servers, channels, messages and attachments"
    component_kind = "square"
    tool_name = "discord"
    tool_description = (
        "Work with Discord: list servers and channels, read and manage messages, "
        "add reactions, create threads, and download attachments from an inbound "
        "message into the workspace."
    )
    handles = (
        {"name": "input-main", "kind": "input", "position": "left", "label": "Input", "role": "main"},
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (DiscordBotCredential,)
    annotations = {"destructive": True, "readonly": False, "open_world": True}
    task_queue = TaskQueue.REST_API
    usable_as_tool = True
    # See DiscordSendNode: which bot acts is operator configuration.
    server_controlled_fields = frozenset({"account_id"})
    hide_input_handle = False
    hide_output_handle = False

    Params = DiscordActionParams
    Output = DiscordActionOutput

    # ---- reads ----------------------------------------------------------

    @Operation("list_guilds", cost={"service": "discord", "action": "list_guilds", "count": 1})
    async def list_guilds(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        result = await _base.get("users/@me/guilds", account_id=self._account(params))
        return _as_list(result)

    @Operation("list_channels", cost={"service": "discord", "action": "list_channels", "count": 1})
    async def list_channels(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        guild_id = _require(params.guild_id, "guild ID")
        result = await _base.get(f"guilds/{guild_id}/channels", account_id=self._account(params))
        return _as_list(result)

    @Operation("get_channel")
    async def get_channel(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        channel_id = _require(params.channel_id, "channel ID")
        result = await _base.get(f"channels/{channel_id}", account_id=self._account(params))
        return DiscordActionOutput(result=result)

    @Operation("list_messages", cost={"service": "discord", "action": "list_messages", "count": 1})
    async def list_messages(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        channel_id = _require(params.channel_id, "channel ID")
        result = await _base.get(
            f"channels/{channel_id}/messages",
            account_id=self._account(params),
            params={"limit": params.limit},
        )
        return _as_list(result)

    @Operation("get_message")
    async def get_message(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        channel_id = _require(params.channel_id, "channel ID")
        message_id = _require(params.message_id, "message ID")
        result = await _base.get(
            f"channels/{channel_id}/messages/{message_id}", account_id=self._account(params)
        )
        return DiscordActionOutput(result=result)

    # ---- writes ---------------------------------------------------------

    @Operation("edit_message", cost={"service": "discord", "action": "edit_message", "count": 1})
    async def edit_message(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        channel_id = _require(params.channel_id, "channel ID")
        message_id = _require(params.message_id, "message ID")
        result = await _base.patch(
            f"channels/{channel_id}/messages/{message_id}",
            {"content": params.content},
            account_id=self._account(params),
        )
        return DiscordActionOutput(result=result)

    @Operation("delete_message", cost={"service": "discord", "action": "delete_message", "count": 1})
    async def delete_message(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        channel_id = _require(params.channel_id, "channel ID")
        message_id = _require(params.message_id, "message ID")
        await _base.delete(
            f"channels/{channel_id}/messages/{message_id}", account_id=self._account(params)
        )
        return DiscordActionOutput(result={"deleted": True, "message_id": message_id})

    @Operation("add_reaction")
    async def add_reaction(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        from urllib.parse import quote

        channel_id = _require(params.channel_id, "channel ID")
        message_id = _require(params.message_id, "message ID")
        emoji = _require(params.emoji, "emoji")
        await _base.request(
            "PUT",
            f"channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji, safe='')}/@me",
            account_id=self._account(params),
        )
        return DiscordActionOutput(result={"reacted": True, "emoji": emoji})

    @Operation("pin_message")
    async def pin_message(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        channel_id = _require(params.channel_id, "channel ID")
        message_id = _require(params.message_id, "message ID")
        await _base.request(
            "PUT", f"channels/{channel_id}/pins/{message_id}", account_id=self._account(params)
        )
        return DiscordActionOutput(result={"pinned": True, "message_id": message_id})

    @Operation("create_thread", cost={"service": "discord", "action": "create_thread", "count": 1})
    async def create_thread(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        channel_id = _require(params.channel_id, "channel ID")
        name = _require(params.thread_name, "thread name")
        # 11 is GUILD_PUBLIC_THREAD.
        result = await _base.post(
            f"channels/{channel_id}/threads",
            {"name": name, "type": 11},
            account_id=self._account(params),
        )
        return DiscordActionOutput(result=result)

    # ---- media ----------------------------------------------------------

    @Operation("download_attachments", cost={"service": "discord", "action": "download", "count": 1})
    async def download_attachments(
        self, ctx: NodeContext, params: DiscordActionParams
    ) -> DiscordActionOutput:
        """Fetch inbound attachments into the workspace as references.

        Discord CDN URLs are signed and expire, so they are fetched here and
        not persisted. ``fetch_to_workspace`` performs the SSRF check, streams
        with a size cap and writes inside the workspace root.
        """
        from services.media import fetch_to_workspace
        from services.media.preview import preview_kind

        if not params.attachments:
            raise NodeUserError(
                "No attachments supplied. Connect a discordReceive node and map "
                "its `attachments` output into this field."
            )

        files: List[dict] = []
        for attachment in params.attachments:
            url = (attachment or {}).get("url")
            if not url:
                continue
            filename = (attachment or {}).get("filename") or "attachment"
            stem = filename.rsplit(".", 1)[0] or "attachment"
            mime_type = (attachment or {}).get("content_type") or ""
            ref = await fetch_to_workspace(
                url,
                ctx=ctx,
                stem=stem,
                # Never "audio": that kind asserts a real container probe, and
                # a download measures nothing.
                kind=_ref_kind(preview_kind(mime_type)),
            )
            files.append(ref.model_dump(mode="json"))

        return DiscordActionOutput(files=files, count=len(files))

    @Operation("interaction_respond", cost={"service": "discord", "action": "interaction", "count": 1})
    async def interaction_respond(
        self, ctx: NodeContext, params: DiscordActionParams
    ) -> DiscordActionOutput:
        """Finish an interaction the trigger deferred.

        The router already acknowledged within Discord's three-second window,
        so this edits that deferred response rather than creating one. The
        token is resolved from the opaque ref here; it never travels through
        node output.
        """
        from ._interactions import resolve_token

        ref = _require(params.interaction_ref, "interaction reference")
        message = _require(params.interaction_message, "message")

        resolved = resolve_token(ref)
        if resolved is None:
            raise NodeUserError(
                "That interaction reference is unknown or has expired. Discord "
                "interaction tokens last 15 minutes, and references do not survive "
                "a server restart."
            )
        application_id, token = resolved

        # Webhook-style route: the token authenticates it, so no bot token is
        # attached and this deliberately bypasses the account path.
        import httpx

        url = (
            f"{_base.API_BASE_URL}/{_base.API_VERSION}/webhooks/"
            f"{application_id}/{token}/messages/@original"
        )
        async with httpx.AsyncClient(timeout=_base.REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.patch(
                url,
                json={"content": message},
                headers={"User-Agent": _base.USER_AGENT},
            )
        if response.status_code >= 400:
            # Never echo the URL: it embeds the interaction token.
            raise NodeUserError(
                f"Discord rejected the interaction response with HTTP "
                f"{response.status_code}. The 15-minute window may have closed."
            )
        return DiscordActionOutput(result=response.json() if response.content else {"sent": True})

    # ---- escape hatches -------------------------------------------------

    @Operation("execute_webhook", cost={"service": "discord", "action": "execute_webhook", "count": 1})
    async def execute_webhook(
        self, ctx: NodeContext, params: DiscordActionParams
    ) -> DiscordActionOutput:
        """Post through a webhook URL, which carries its own auth.

        No bot token is involved, so this reaches channels the bot is not in.
        """
        import httpx

        url = _require(params.webhook_url, "webhook URL")
        _base.assert_discord_host(url)
        content = _require(params.webhook_content, "message")

        async with httpx.AsyncClient(timeout=_base.REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                json={"content": content},
                headers={"User-Agent": _base.USER_AGENT},
                params={"wait": "true"},
            )
        if response.status_code >= 400:
            # Never echo the URL: it is the credential.
            raise NodeUserError(
                f"Discord rejected the webhook post with HTTP {response.status_code}. "
                "Check that the webhook still exists."
            )
        return DiscordActionOutput(result=response.json() if response.content else {"sent": True})

    @Operation("custom", cost={"service": "discord", "action": "custom", "count": 1})
    async def custom(self, ctx: NodeContext, params: DiscordActionParams) -> DiscordActionOutput:
        """Call any Discord API route. The path is validated in _base.build_url."""
        path = _require(params.path, "API path")
        result = await _base.request(
            params.method,
            path,
            account_id=self._account(params),
            json=params.body or None,
        )
        if isinstance(result, dict) and "data" in result and isinstance(result["data"], list):
            return _as_list(result["data"])
        return DiscordActionOutput(result=result if isinstance(result, dict) else None)

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _account(params: DiscordActionParams) -> str:
        return params.account_id or DEFAULT_ACCOUNT


def _require(value: str, label: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise NodeUserError(f"A {label} is required for this operation.")
    return cleaned


def _as_list(result: Any) -> DiscordActionOutput:
    """Wrap a list response so the declared Output stays an object."""
    items = result.get("data") if isinstance(result, dict) else result
    items = items if isinstance(items, list) else []
    return DiscordActionOutput(items=items, count=len(items))


def _ref_kind(preview: str) -> str:
    return preview if preview in {"image", "video"} else "file"


__all__ = ["DiscordActionNode", "DiscordActionOutput", "DiscordActionParams"]
