"""CloudEvents factory for Simple Memory mutations.

One event: ``memory.updated``, broadcast after a durable remember / update /
forget / clear. Broadcast directly via the status broadcaster (not
``services.events.dispatch.emit`` — no canary consumer exists for
``com.opencompany.memory.*``, so routing through ``emit`` would run a
Temporal Visibility query guaranteed to match nothing per mutation).

The payload is identity-only — never item content: the broadcast fans out
to every connected socket, and the panel refetches through the authorized
``list_memory_items`` handler.
"""

from __future__ import annotations

from core.logging import get_logger
from services.events.envelope import WorkflowEvent

logger = get_logger(__name__)

_SOURCE = "opencompany://nodes/simple_memory"


def memory_updated(
    *,
    workflow_id: str,
    memory_node_id: str,
    operation: str,
) -> WorkflowEvent:
    return WorkflowEvent(
        source=_SOURCE,
        type="com.opencompany.memory.updated",
        subject=str(memory_node_id),
        data={
            "workflow_id": str(workflow_id),
            "memory_node_id": str(memory_node_id),
            "operation": str(operation),
        },
    )


async def dispatch_memory_updated(
    *,
    workflow_id: str,
    memory_node_id: str,
    operation: str,
) -> None:
    """Best-effort panel notification; a broadcast failure never fails
    the mutation that triggered it."""

    try:
        from services.status_broadcaster import get_status_broadcaster

        await get_status_broadcaster().broadcast(
            {
                "type": "memory.updated",
                "data": memory_updated(
                    workflow_id=workflow_id,
                    memory_node_id=memory_node_id,
                    operation=operation,
                ).model_dump(mode="json", exclude_none=True),
            }
        )
    except Exception as exc:  # noqa: BLE001 — notification only
        # WARNING, not debug: a silently failing broadcast is exactly how
        # "the panel never updates live" becomes undiagnosable.
        logger.warning("[Memory] update broadcast failed: %s", exc)


__all__ = ["dispatch_memory_updated", "memory_updated"]
