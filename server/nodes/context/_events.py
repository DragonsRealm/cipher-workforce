"""Metadata-only Context lifecycle events.

These are UI lifecycle notifications, not workflow triggers. No node type
registers a canary consumer for ``com.opencompany.context.*``, so routing them
through ``services.events.dispatch.emit`` would run a Temporal Visibility
``ListWorkflowExecutions`` query that is guaranteed to match nothing -- once per
journal append. They are broadcast straight to connected WebSocket clients
instead, which is the canonical plugin pattern (see ``nodes/telegram/_events.py``).

The payload is identity + version only. It carries no message bodies and no
provider state, because the broadcast fans out to every connected socket; the
panel refetches through the authorized ``get_agent_context`` handler, which is
where ownership is enforced.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from models.agent_context import AgentContextRef
from services.events.envelope import WorkflowEvent


def context_updated(
    *,
    workflow_id: str,
    context_node_id: str,
    thread_id: str,
    epoch: int,
    revision: int,
    provider: Optional[str],
    active_token_count: int,
    sequence: Optional[int] = None,
) -> WorkflowEvent:
    data: Dict[str, Any] = {
        "workflow_id": workflow_id,
        "context_node_id": context_node_id,
        "thread_id": thread_id,
        "epoch": epoch,
        "revision": revision,
        "provider": provider,
        "active_token_count": max(0, active_token_count),
    }
    if sequence is not None:
        # Lets the panel show "N new events" on the paginated journal view
        # without refetching a page it is not currently looking at.
        data["sequence"] = sequence
    return WorkflowEvent(
        source="opencompany://nodes/context",
        type="com.opencompany.context.updated",
        subject=context_node_id,
        workflow_id=workflow_id,
        data=data,
    )


def context_compacted(
    *,
    workflow_id: str,
    context_node_id: str,
    thread_id: str,
    epoch: int,
    revision: int,
    provider: Optional[str],
    strategy: str,
    covers_through_sequence: int,
    active_token_count: int,
) -> WorkflowEvent:
    return WorkflowEvent(
        source="opencompany://nodes/context",
        type="com.opencompany.context.compacted",
        subject=context_node_id,
        workflow_id=workflow_id,
        data={
            "workflow_id": workflow_id,
            "context_node_id": context_node_id,
            "thread_id": thread_id,
            "epoch": epoch,
            "revision": revision,
            "provider": provider,
            "strategy": strategy,
            "covers_through_sequence": covers_through_sequence,
            "active_token_count": max(0, active_token_count),
        },
    )


def context_epoch_started(
    *,
    workflow_id: str,
    context_node_id: str,
    thread_id: str,
    epoch: int,
    revision: int,
    provider: Optional[str],
    reason: str,
) -> WorkflowEvent:
    return WorkflowEvent(
        source="opencompany://nodes/context",
        type="com.opencompany.context.epoch.started",
        subject=context_node_id,
        workflow_id=workflow_id,
        data={
            "workflow_id": workflow_id,
            "context_node_id": context_node_id,
            "thread_id": thread_id,
            "epoch": epoch,
            "revision": revision,
            "provider": provider,
            "reason": reason,
        },
    )


async def _broadcast(event: WorkflowEvent, *, wire_routing_key: str) -> None:
    """Send one CloudEvents envelope to connected clients.

    Deliberately not ``services.events.dispatch.emit`` -- see the module
    docstring. The envelope, ``source``, ``type``, ``subject`` and ``data`` are
    identical either way, so the wire contract the frontend sees is unchanged.
    """

    from services.status_broadcaster import get_status_broadcaster

    await get_status_broadcaster().broadcast(
        {
            "type": wire_routing_key,
            "data": event.model_dump(mode="json", exclude_none=True),
        }
    )


async def dispatch_context_epoch_started(**metadata) -> None:
    await _broadcast(
        context_epoch_started(**metadata),
        wire_routing_key="context.epoch.started",
    )


async def dispatch_context_updated(**metadata) -> None:
    await _broadcast(
        context_updated(**metadata),
        wire_routing_key="context.updated",
    )


async def dispatch_context_compacted(**metadata) -> None:
    await _broadcast(
        context_compacted(**metadata),
        wire_routing_key="context.compacted",
    )


async def on_context_commit(
    *,
    ref: AgentContextRef,
    provider: Optional[str],
    active_token_count: int,
    sequence: Optional[int] = None,
) -> None:
    """Turn a durable store commit into a ``context.updated`` broadcast.

    Registered with ``register_context_commit_listener`` from the package
    ``__init__``, so every writer that reaches durable state -- the in-process
    agent loop, the Temporal LLM activity, the CLI-agent bridge -- produces a
    live update with no code at the call site.

    Epoch rotation is deliberately not routed here. ``start_epoch``'s callers
    already emit ``context.epoch.started`` carrying a ``reason``
    (``clear`` / ``fork`` / ``workflow_reset``) that the store cannot know, so a
    store-level emit would both duplicate the broadcast and lose the reason.
    """

    await dispatch_context_updated(
        workflow_id=ref.workflow_id,
        context_node_id=ref.context_node_id,
        thread_id=ref.thread_id,
        epoch=ref.epoch,
        revision=ref.revision,
        provider=provider,
        active_token_count=active_token_count,
        sequence=sequence,
    )


__all__ = [
    "context_compacted",
    "context_epoch_started",
    "context_updated",
    "dispatch_context_compacted",
    "dispatch_context_epoch_started",
    "dispatch_context_updated",
    "on_context_commit",
]
