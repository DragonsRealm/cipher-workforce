"""WebSocket commands for the Gallery panel.

Listing lives on the WebSocket rather than HTTP because the consumer is the
parameter panel, which already holds an authenticated socket with request
correlation — a second HTTP surface would mean a second auth path and a
second error envelope for no gain. ``GET /api/workspace/{id}/files/{path}``
stays the *content* channel (preview, download, Range).

``@ws_response`` rather than ``@ws_handler``: the latter logs every exception
at ERROR with a traceback, which breaks the NodeUserError contract that a
user-correctable failure is one WARN line.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from fastapi import WebSocket

from core.logging import get_logger
from services.plugin.ws import ws_response

from ._service import (
    WORKSPACE_LIST_DEFAULT,
    list_directory,
    list_matching,
    search_to_pattern,
)

logger = get_logger(__name__)


async def _root_for(workflow_id: str, *, mutating: bool = False):
    """Resolve the workspace directory for a request from the editor.

    Mutating callers refuse the anonymous ``default`` workspace: an id that
    does not resolve — a stale tab, a workflow deleted in another window —
    would otherwise silently modify files belonging to a different context.
    """
    from core.container import container
    from services.workspace_locator import resolve_workspace_root

    return await resolve_workspace_root(
        workflow_id, container.database(), allow_default=not mutating
    )


@ws_response
async def handle_list_workspace_files(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    """List one directory of a workflow's workspace, or search across it.

    ``pattern`` is a literal glob. ``search`` is what a person typed into a
    find box; translating that into a glob is this side's job, so the panel
    stays a text field and the meaning of "search" is defined once.

    A search covers the whole workspace, not the folder in view — a find
    box that only finds what is already on screen is not one.
    """
    workflow_id = str(data.get("workflow_id") or "")
    root = await _root_for(workflow_id)

    limit = int(data.get("limit") or WORKSPACE_LIST_DEFAULT)
    path = str(data.get("path") or "")

    pattern = str(data.get("pattern") or "").strip()
    search = str(data.get("search") or "")
    if not pattern and search:
        pattern = search_to_pattern(search)
        path = ""

    if pattern:
        result = await list_matching(
            str(root),
            pattern=pattern,
            path=path,
            workflow_id=workflow_id or None,
            limit=limit,
        )
    else:
        result = await list_directory(
            str(root), path=path, workflow_id=workflow_id or None, limit=limit
        )

    return {"success": True, "workflow_id": workflow_id, **result}


WS_HANDLERS: Dict[str, Callable[[Dict[str, Any], WebSocket], Awaitable[Dict[str, Any]]]] = {
    "list_workspace_files": handle_list_workspace_files,
}


__all__ = ["WS_HANDLERS", "handle_list_workspace_files"]
