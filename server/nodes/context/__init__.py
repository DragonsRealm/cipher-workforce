"""Visible, system-managed Agent Context companion node.

Connecting this node to an agent's ``input-context`` handle opts the agent
into the plain conversation store: one ``agent_conversations`` row per
``(workflow_id, generation, agent_node_id)``, loaded at run start and saved
per turn. The node itself carries no runtime state — the conversation lives
in the store and is viewed through the panel handlers below.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from services.plugin import ActionNode, NodeContext, Operation, TaskQueue


class AgentContextParams(BaseModel):
    """The Context node is configuration-free; the connection is the opt-in."""

    model_config = ConfigDict(extra="forbid")


class AgentContextOutput(BaseModel):
    """Metadata-only execution result; no transcript."""

    configured: bool = True

    model_config = ConfigDict(extra="forbid")


class AgentContextNode(ActionNode):
    type = "context"
    version = 2
    display_name = "Context"
    subtitle = "Agent Conversation"
    group = ("memory",)
    description = (
        "System-managed durable conversation for connected agents; "
        "one conversation per agent per workflow generation"
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
        """Report configuration only; conversations stay in the store."""

        del ctx, params
        return AgentContextOutput()

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
        """Workflow Reset wipes this workflow's stored conversations.

        The next Start admits a new generation (a new key) anyway, but the
        panel shows the newest STORED generation — so surviving rows would
        keep rendering the pre-Reset conversation as the live context and
        Reset would look like it did nothing. Warm claude subprocesses
        still holding the wiped conversation are terminated (the
        generation fence in ``acquire`` covers the next Start
        independently), and a ``context.updated`` broadcast refreshes any
        open panel.
        """

        del graph
        if generation <= 0:
            return {"reset": False, "cleared_conversations": 0}
        from services.agent_context import clear_conversation

        cleared = await clear_conversation(
            database, workflow_id=str(workflow_id)
        )
        try:
            from services.cli_agent.factory import get_session_pool

            pool = get_session_pool("claude")
            terminate = getattr(pool, "terminate_conversations", None)
            if callable(terminate):
                await terminate(str(workflow_id))
        except Exception:
            # Best-effort: the acquire-time generation fence still protects
            # the next Start from reusing a stale warm process.
            pass
        try:
            from ._events import dispatch_context_updated

            await dispatch_context_updated(
                workflow_id=str(workflow_id),
                generation=generation,
                agent_node_id=str(node_id),
                message_count=0,
            )
        except Exception:
            # Lifecycle state is authoritative; the broadcast is a UI
            # notification and may be retried by a manual panel refresh.
            pass
        return {"reset": True, "cleared_conversations": cleared}


__all__ = [
    "AgentContextNode",
    "AgentContextOutput",
    "AgentContextParams",
]


# Context panel commands are plugin-owned side channels.  The node class above
# stays passive; all durable state lives in the conversation store.
from services.agent_context.listeners import (  # noqa: E402
    register_conversation_listener,
)
from services.plugin.edge_walker import (  # noqa: E402
    register_agent_context_builder,
)
from services.ws_handler_registry import register_ws_handlers  # noqa: E402

from ._descriptor import build_agent_context_descriptor  # noqa: E402
from ._events import on_conversation_saved  # noqa: E402
from ._handlers import WS_HANDLERS  # noqa: E402

register_ws_handlers(WS_HANDLERS)
# The framework walks `input-context` edges but knows nothing about this
# node's parameters; it calls whatever is registered here.
register_agent_context_builder(build_agent_context_descriptor)
# The store announces durable saves; this plugin decides they are worth a
# `context.updated` broadcast.  Registering here keeps the store free of any
# knowledge that a UI exists.
register_conversation_listener(on_conversation_saved)
