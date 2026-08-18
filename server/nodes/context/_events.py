"""CloudEvents factory for the Context conversation lifecycle.

One event: ``context.updated``, broadcast after every durable conversation
save. It is broadcast directly via the status broadcaster rather than
through ``services.events.dispatch.emit`` — no node type registers a canary
consumer for ``com.opencompany.context.*``, so routing through ``emit``
would run a Temporal Visibility query guaranteed to match nothing once per
save.

The payload is identity + revision data only (never transcript content):
the broadcast fans out to every connected socket, and the panel refetches
through the authorized ``get_agent_context`` handler.
"""

from __future__ import annotations

from typing import Any

from core.logging import get_logger
from services.events.envelope import WorkflowEvent

logger = get_logger(__name__)

_SOURCE = "opencompany://nodes/context"


def context_updated(
    *,
    workflow_id: str,
    generation: int,
    agent_node_id: str,
    message_count: int,
) -> WorkflowEvent:
    """One durable conversation save committed."""

    return WorkflowEvent(
        source=_SOURCE,
        type="com.opencompany.context.updated",
        subject=str(agent_node_id),
        data={
            "workflow_id": str(workflow_id),
            "generation": int(generation),
            "agent_node_id": str(agent_node_id),
            "message_count": max(0, int(message_count)),
        },
    )


async def _broadcast(event: WorkflowEvent, *, wire_routing_key: str) -> None:
    from services.status_broadcaster import get_status_broadcaster

    await get_status_broadcaster().broadcast(
        {
            "type": wire_routing_key,
            "data": event.model_dump(mode="json", exclude_none=True),
        }
    )


async def dispatch_context_updated(**metadata: Any) -> None:
    await _broadcast(
        context_updated(**metadata),
        wire_routing_key="context.updated",
    )


async def on_conversation_saved(
    *,
    workflow_id: str,
    generation: int,
    agent_node_id: str,
    message_count: int,
) -> None:
    """Conversation-save listener registered by the plugin package.

    ``services.agent_context`` must never import ``nodes/``; the plugin
    registers this adapter via ``register_conversation_listener`` instead.
    The listener registry already swallows listener failures, so a
    broadcast problem can never fail a save.
    """

    await dispatch_context_updated(
        workflow_id=workflow_id,
        generation=generation,
        agent_node_id=agent_node_id,
        message_count=message_count,
    )


__all__ = [
    "context_updated",
    "dispatch_context_updated",
    "on_conversation_saved",
]
