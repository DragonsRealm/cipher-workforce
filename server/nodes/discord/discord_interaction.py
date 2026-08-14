"""discordInteraction — fire a workflow on a slash command or component click.

A second trigger node rather than a mode on discordReceive. A canary trigger
registers exactly one CloudEvents type, because that string becomes the
EventType Search Attribute its listener is discovered by, so one node cannot
subscribe to both. The payloads also share almost nothing: merging them would
mean a union output schema and weaker {{trigger.field}} resolution. This is
the same split, for the same reasons, as whatsappBusinessReceive vs
whatsappBusinessStatus.

Interactions arrive over HTTP, not the gateway. Setting an interactions
endpoint URL for an application stops INTERACTION_CREATE being delivered over
the socket, so the two paths are mutually exclusive per app.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.plugin import NodeContext, Operation, TaskQueue, TriggerNode

from ._credentials import DiscordBotCredential
from ._events import INTERACTION_CREATED_TYPE


class DiscordInteractionParams(BaseModel):
    account_id: str = Field(
        default="",
        description="Which Discord app to accept interactions for.",
        json_schema_extra={"loadOptionsMethod": "discordAccounts"},
    )
    interaction_kind: Literal["all", "command", "component", "modal"] = Field(
        default="all",
        description="Which kind of interaction fires this trigger.",
    )
    command_name: str = Field(
        default="",
        description="Only fire for this slash command.",
        json_schema_extra={"displayOptions": {"show": {"interaction_kind": ["all", "command"]}}},
    )
    custom_id: str = Field(
        default="",
        description="Only fire for a component or modal with this custom_id.",
        json_schema_extra={"displayOptions": {"show": {"interaction_kind": ["all", "component", "modal"]}}},
    )
    guild_id: str = Field(
        default="",
        description="Only fire for interactions in this server.",
    )

    model_config = ConfigDict(extra="ignore")


class DiscordInteractionOutput(BaseModel):
    account_id: Optional[str] = None
    interaction_id: Optional[str] = None
    interaction_type: Optional[int] = None
    application_id: Optional[str] = None
    command_name: Optional[str] = None
    custom_id: Optional[str] = None
    component_type: Optional[int] = None
    options: Dict[str, Any] = Field(default_factory=dict)
    channel_id: Optional[str] = None
    guild_id: Optional[str] = None
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    # An opaque handle, never the token. Pass it to
    # discordAction[interaction_respond] to answer within the 15-minute
    # window. The token itself is a bearer credential and node output is
    # persisted, broadcast and replayed into LLM context.
    interaction_ref: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class DiscordInteractionNode(TriggerNode):
    type = "discordInteraction"
    display_name = "Discord Interaction"
    subtitle = "Slash Command"
    group = ("discord", "trigger")
    description = "Trigger a workflow when a Discord slash command or component is used"
    component_kind = "trigger"
    handles = (
        {"name": "output-main", "kind": "output", "position": "right", "label": "Output", "role": "main"},
    )
    credentials = (DiscordBotCredential,)
    task_queue = TaskQueue.TRIGGERS_EVENT
    mode = "event"
    event_type = "discord_interaction_created"

    Params = DiscordInteractionParams
    Output = DiscordInteractionOutput

    def build_filter(self, params: BaseModel) -> Callable[[Dict[str, Any]], bool]:
        from ._filters import build_interaction_filter

        return build_interaction_filter(params.model_dump())

    @Operation("wait")
    async def wait(
        self, ctx: NodeContext, params: DiscordInteractionParams
    ) -> DiscordInteractionOutput:
        """Never called; interactions resolve through the event waiter."""
        raise NotImplementedError("discordInteraction resolves through the event waiter")


__all__ = [
    "INTERACTION_CREATED_TYPE",
    "DiscordInteractionNode",
    "DiscordInteractionOutput",
    "DiscordInteractionParams",
]
