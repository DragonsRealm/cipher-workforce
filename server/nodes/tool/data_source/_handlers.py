"""Human-facing WebSocket API for the Data panel.

Mount CRUD operates on the machine-wide allowlist (owner-scoped); browsing
resolves its scope from the persisted workflow graph and the authenticated
WebSocket, mirroring the Simple Memory handlers. Neither the client nor the
model can supply a mount root or namespace — only names of rows the
operator created.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict

from fastapi import WebSocket

from services.plugin import NodeUserError
from services.plugin.deps import get_database
from services.plugin.ws import ws_response

from ._paths import MOUNT_PREFIX, mount_entry, split_mount_path

_BROWSE_LIMIT_MAX = 200
_BROWSE_LIMIT_DEFAULT = 50


def _authenticated_owner(websocket: WebSocket) -> str:
    """Read server-authenticated identity without consulting request data."""
    state = getattr(websocket, "state", None)
    for attribute in ("user_id", "principal_id", "subject"):
        value = getattr(state, attribute, None) if state is not None else None
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    scope = getattr(websocket, "scope", None)
    if isinstance(scope, dict):
        for key in ("user_id", "principal_id", "subject"):
            value = scope.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value)
    return "owner"


def _require_external_socket(websocket: WebSocket) -> None:
    """The internal unauthenticated worker socket may not touch mounts."""
    scope = getattr(websocket, "scope", {}) or {}
    if scope.get("path") == "/ws/internal":
        raise NodeUserError("Data access requires an authenticated client")


def _store():
    from services.data.mount_store import DataMountStore

    return DataMountStore(get_database())


async def _resolve_node_scope(
    data: Dict[str, Any], websocket: WebSocket
) -> tuple[str, str, str]:
    """(owner_id, workflow_id, node_id) for a browse request, or raise."""
    _require_external_socket(websocket)
    workflow_id = str(data.get("workflow_id") or "").strip()
    node_id = str(data.get("data_node_id") or data.get("node_id") or "").strip()
    if not workflow_id:
        raise NodeUserError("workflow_id required")
    if not node_id:
        raise NodeUserError("data_node_id required")
    database = get_database()
    saved = await database.get_workflow(workflow_id)
    if saved is None:
        raise NodeUserError("Workflow not found")
    graph = saved.data if hasattr(saved, "data") else saved.get("data", saved)
    owner = _authenticated_owner(websocket)
    stored_owner = (
        str(graph.get("owner_id") or "") if isinstance(graph, dict) else ""
    )
    if stored_owner and stored_owner != owner:
        raise NodeUserError("Workflow access denied")
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    matches = [
        node
        for node in nodes
        if str(node.get("id") or "") == node_id
        and str(node.get("type") or node.get("data", {}).get("type") or "")
        == "dataSource"
    ]
    if len(matches) != 1:
        raise NodeUserError(
            "Data node does not belong to the requested workflow"
        )
    return owner, workflow_id, node_id


# ------------------------------------------------------------- mount CRUD


@ws_response
async def handle_data_list_mounts(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    _require_external_socket(websocket)
    mounts = await _store().list_mounts(_authenticated_owner(websocket))
    return {"success": True, "mounts": mounts}


@ws_response
async def handle_data_add_mount(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    from services.data.mount_store import MountStoreError

    _require_external_socket(websocket)
    try:
        mount = await _store().add_mount(
            _authenticated_owner(websocket),
            name=str(data.get("name") or ""),
            root_path=str(data.get("root_path") or ""),
            writable=bool(data.get("writable", False)),
        )
    except MountStoreError as exc:
        raise NodeUserError(str(exc)) from exc
    return {"success": True, "mount": mount}


@ws_response
async def handle_data_update_mount(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    from services.data.mount_store import MountStoreError

    _require_external_socket(websocket)
    try:
        mount = await _store().update_mount(
            _authenticated_owner(websocket),
            name=str(data.get("name") or ""),
            writable=bool(data.get("writable", False)),
        )
    except MountStoreError as exc:
        raise NodeUserError(str(exc)) from exc
    return {"success": True, "mount": mount}


@ws_response
async def handle_data_remove_mount(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    from services.data.mount_store import MountStoreError

    _require_external_socket(websocket)
    try:
        result = await _store().remove_mount(
            _authenticated_owner(websocket),
            name=str(data.get("name") or ""),
        )
    except MountStoreError as exc:
        raise NodeUserError(str(exc)) from exc
    return {"success": True, **result}


# ----------------------------------------------------------------- browse


def _crumbs(virtual: str) -> list[dict[str, str]]:
    parts = [part for part in virtual.split("/") if part]
    return [
        {"name": part, "path": "/".join(parts[: index + 1])}
        for index, part in enumerate(parts)
    ]


@ws_response
async def handle_data_browse(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    """Read-only listing over the node's workspace + enabled mounts."""
    from nodes.tool.data_source import DataToolParams

    owner, workflow_id, node_id = await _resolve_node_scope(data, websocket)
    database = get_database()
    persisted = await database.get_node_parameters(node_id) or {}
    params = DataToolParams.model_validate(persisted)
    path = str(data.get("path") or "").strip()
    try:
        limit = int(data.get("limit") or _BROWSE_LIMIT_DEFAULT)
    except (TypeError, ValueError):
        limit = _BROWSE_LIMIT_DEFAULT
    limit = max(1, min(limit, _BROWSE_LIMIT_MAX))

    split = split_mount_path(path)
    if split is not None:
        name, rest = split
        if name not in params.mounts:
            raise NodeUserError(f"Mount '{name}' is not enabled on this node")
        row = await _store().get_mount(owner, name)
        if row is None:
            raise NodeUserError(f"Mount '{name}' is no longer defined")
        from nodes.filesystem._backend import resolve_within

        root = Path(row["root_path"])
        target = resolve_within(root, rest) if rest else root.resolve()
        if not target.is_dir():
            raise NodeUserError("Not a directory")

        def scan() -> list[dict[str, Any]]:
            rows = []
            with os.scandir(target) as iterator:
                for entry in iterator:
                    child_rel = f"{rest}/{entry.name}" if rest else entry.name
                    rows.append(
                        mount_entry(
                            mount_name=name,
                            rel_path=child_rel,
                            abs_path=Path(entry.path),
                            writable=bool(row["writable"]),
                        )
                    )
            rows.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
            return rows

        rows = await asyncio.to_thread(scan)
        virtual = f"{MOUNT_PREFIX}/{name}/{rest}" if rest else f"{MOUNT_PREFIX}/{name}"
        return {
            "success": True,
            "source": "mount",
            "mount": name,
            "path": virtual,
            "crumbs": _crumbs(virtual),
            "entries": rows[:limit],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
        }

    from nodes.filesystem.gallery._service import list_directory
    from services.workspace_locator import resolve_workspace_root

    root = await resolve_workspace_root(workflow_id, database)
    listing = await list_directory(
        str(root), path=path, workflow_id=workflow_id, limit=limit
    )
    return {"success": True, "source": "workspace", **listing}


WSHandler = Callable[[Dict[str, Any], WebSocket], Awaitable[Dict[str, Any]]]
WS_HANDLERS: Dict[str, WSHandler] = {
    "data_list_mounts": handle_data_list_mounts,
    "data_add_mount": handle_data_add_mount,
    "data_update_mount": handle_data_update_mount,
    "data_remove_mount": handle_data_remove_mount,
    "data_browse": handle_data_browse,
}


__all__ = [
    "WS_HANDLERS",
    "handle_data_add_mount",
    "handle_data_browse",
    "handle_data_list_mounts",
    "handle_data_remove_mount",
    "handle_data_update_mount",
]
