"""Metadata-only Canvas lifecycle event.

A UI lifecycle notification, not a workflow trigger. No node type registers a
canary consumer for ``com.opencompany.canvas.*``, so routing it through
``services.events.dispatch.emit`` would run a Temporal Visibility
``ListWorkflowExecutions`` query that is guaranteed to match nothing — once
per ``canvas`` tool call. It is broadcast straight to connected WebSocket
clients instead, the canonical plugin pattern (see ``nodes/context/_events.py``).

The payload is identity + revision only. It carries no item content, because
the broadcast fans out to every connected socket; panels refetch through the
authorized ``canvas_list`` handler, which is where ownership is enforced.
"""

from __future__ import annotations

from typing import Optional

from services.events.envelope import WorkflowEvent

_WIRE_KEY = "canvas_updated"


def canvas_updated(
    *,
    workflow_id: Optional[str],
    node_id: str,
    revision: int,
) -> WorkflowEvent:
    return WorkflowEvent(
        source="opencompany://nodes/canvas",
        type="com.opencompany.canvas.updated",
        subject=node_id,
        workflow_id=workflow_id,
        data={
            "workflow_id": workflow_id,
            "node_id": node_id,
            "revision": revision,
        },
    )


async def dispatch_canvas_updated(
    *,
    workflow_id: Optional[str],
    node_id: str,
    revision: int,
) -> None:
    from services.status_broadcaster import get_status_broadcaster

    event = canvas_updated(
        workflow_id=workflow_id, node_id=node_id, revision=revision
    )
    await get_status_broadcaster().broadcast(
        {
            "type": _WIRE_KEY,
            "data": event.model_dump(mode="json", exclude_none=True),
        }
    )


__all__ = ["canvas_updated", "dispatch_canvas_updated"]
