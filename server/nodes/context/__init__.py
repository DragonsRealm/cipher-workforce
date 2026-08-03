"""Visible, system-managed Agent Context companion node.

Only replay policy is workflow configuration.  Journals, checkpoints,
provider bindings, epochs, and payload references live exclusively in
``AgentContextStore`` and never enter node parameters or normal outputs.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from services.plugin import ActionNode, NodeContext, Operation, TaskQueue


class AgentContextParams(BaseModel):
    compaction_mode: Literal[
        "auto",
        "native",
        "portable",
        "disabled",
    ] = Field(
        default="auto",
        title="Compaction Mode",
        description=(
            "Prefer verified provider-native compaction in auto mode, "
            "falling back to a portable structured checkpoint."
        ),
    )
    trigger_ratio: float = Field(
        default=0.8,
        gt=0,
        lt=1,
        title="Context Pressure Trigger",
        description=(
            "Compact when retained next-request input plus output headroom "
            "reaches this fraction of the context window."
        ),
    )
    context_window_override: Optional[int] = Field(
        default=None,
        ge=1024,
        title="Context Window Override",
        description=(
            "Optional model context-window override. Empty uses provider "
            "capability discovery."
        ),
    )
    exact_tail_retention_count: int = Field(
        default=8,
        ge=1,
        le=1000,
        title="Exact Tail Retention",
        description=(
            "Minimum number of recent exact transitions retained after a "
            "checkpoint."
        ),
    )

    model_config = ConfigDict(extra="forbid")


class AgentContextOutput(BaseModel):
    """Metadata-only execution result; no transcript or provider state."""

    configured: bool = True
    compaction_mode: str
    trigger_ratio: float
    exact_tail_retention_count: int

    model_config = ConfigDict(extra="forbid")


class AgentContextNode(ActionNode):
    type = "context"
    version = 2
    display_name = "Context"
    subtitle = "Exact Agent State"
    group = ("memory",)
    description = (
        "System-managed exact agent journal, replay checkpoints, and "
        "context-pressure policy"
    )
    component_kind = "model"
    handles = (
        {
            "name": "output-context",
            "kind": "output",
            "position": "top",
            "label": "Context",
            "role": "context",
        },
    )
    hide_input_handle = True
    hide_output_handle = False
    ui_hints = {
        "isContextPanel": True,
        "systemManaged": True,
        "hideInputSection": True,
        "hideOutputSection": True,
        "hideRunButton": True,
    }
    annotations = {
        "destructive": False,
        "readonly": True,
        "open_world": False,
    }
    task_queue = TaskQueue.DEFAULT

    Params = AgentContextParams
    Output = AgentContextOutput

    @Operation("policy")
    async def policy(
        self,
        ctx: NodeContext,
        params: AgentContextParams,
    ) -> AgentContextOutput:
        """Return public policy only; durable state stays in the service."""

        del ctx
        return AgentContextOutput(
            compaction_mode=params.compaction_mode,
            trigger_ratio=params.trigger_ratio,
            exact_tail_retention_count=params.exact_tail_retention_count,
        )

    @classmethod
    async def reset_execution_state(
        cls,
        *,
        node_id: str,
        workflow_id: str,
        execution_id: str,
        generation: int,
        graph: dict,
        database,
    ) -> dict:
        """Rotate every active thread and fence late generation writes."""

        del graph
        if generation <= 0:
            return {
                "reset": False,
                "rotated_threads": 0,
            }
        from services.agent_context import AgentContextStore
        from services.agent_context.lifecycle import (
            fence_context_provider_resources,
        )

        from ._events import dispatch_context_epoch_started

        store = AgentContextStore(database)
        threads = store.iter_threads(
            workflow_id=str(workflow_id),
            context_node_id=str(node_id),
            generation=generation,
            include_archived=False,
        )
        rotated = 0
        async for thread in threads:
            new_ref = await store.start_epoch(
                thread.ref,
                operation_id=(
                    f"workflow-reset:{execution_id}:{node_id}:"
                    f"{thread.ref.thread_id}:epoch-{thread.ref.epoch}"
                ),
                provider=thread.provider,
            )
            await fence_context_provider_resources(
                context_node_id=new_ref.context_node_id,
                thread_id=new_ref.thread_id,
                keep_epoch=new_ref.epoch,
            )
            rotated += 1
            try:
                await dispatch_context_epoch_started(
                    workflow_id=new_ref.workflow_id,
                    context_node_id=new_ref.context_node_id,
                    thread_id=new_ref.thread_id,
                    epoch=new_ref.epoch,
                    revision=new_ref.revision,
                    provider=thread.provider,
                    reason="workflow_reset",
                )
            except Exception:
                # Lifecycle state is authoritative; event delivery is
                # metadata-only and may be retried by observers.
                pass
        return {
            "reset": True,
            "rotated_threads": rotated,
        }


__all__ = [
    "AgentContextNode",
    "AgentContextOutput",
    "AgentContextParams",
]


# Context panel commands are plugin-owned side channels.  The node class above
# stays passive; all durable logic lives in AgentContextStore.
from services.plugin.edge_walker import (  # noqa: E402
    register_agent_context_builder,
)
from services.ws_handler_registry import register_ws_handlers  # noqa: E402

from ._descriptor import build_agent_context_descriptor  # noqa: E402
from ._handlers import WS_HANDLERS  # noqa: E402

register_ws_handlers(WS_HANDLERS)
# The framework walks `input-context` edges but knows nothing about this
# node's parameters or thread-selection rules; it calls whatever is
# registered here.
register_agent_context_builder(build_agent_context_descriptor)
