"""Metadata-only Context lifecycle events."""

from __future__ import annotations

from typing import Optional

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
) -> WorkflowEvent:
    return WorkflowEvent(
        source="opencompany://nodes/context",
        type="com.opencompany.context.updated",
        subject=context_node_id,
        workflow_id=workflow_id,
        data={
            "workflow_id": workflow_id,
            "context_node_id": context_node_id,
            "thread_id": thread_id,
            "epoch": epoch,
            "revision": revision,
            "provider": provider,
            "active_token_count": max(0, active_token_count),
        },
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


async def dispatch_context_epoch_started(**metadata) -> None:
    from services.events.dispatch import emit

    await emit(
        context_epoch_started(**metadata),
        wire_routing_key="context.epoch.started",
    )


async def dispatch_context_updated(**metadata) -> None:
    from services.events.dispatch import emit

    await emit(
        context_updated(**metadata),
        wire_routing_key="context.updated",
    )


async def dispatch_context_compacted(**metadata) -> None:
    from services.events.dispatch import emit

    await emit(
        context_compacted(**metadata),
        wire_routing_key="context.compacted",
    )


__all__ = [
    "context_compacted",
    "context_epoch_started",
    "context_updated",
    "dispatch_context_compacted",
    "dispatch_context_epoch_started",
    "dispatch_context_updated",
]
