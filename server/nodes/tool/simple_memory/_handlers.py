"""Human-facing WebSocket API for the Simple Memory panel.

Every request resolves its namespace from the persisted workflow graph and
the authenticated WebSocket. Neither the client nor the model can provide a
namespace identifier. Contents are returned only on these authorized,
paginated calls; change broadcasts elsewhere remain metadata-only.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import WebSocket
from pydantic import ValidationError

from services.memory.tool_store import (
    MemoryScope,
    MemoryStoreError,
    MemoryToolStore,
)
from services.plugin import NodeUserError
from services.plugin.deps import get_database
from services.plugin.ws import ws_response

from . import SimpleMemoryToolInput


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
    # The current deployment is single-owner when no auth principal is
    # attached; this mirrors NodeContext.user_id's trusted default.
    return "owner"


def _require_external_socket(websocket: WebSocket) -> None:
    """The internal unauthenticated worker socket may not touch Memory.

    Defence in depth behind the allowlist in ``services.authz.ws_surface``:
    the Context handlers have carried this guard since they were written,
    and its absence here is exactly why these six handlers were reachable
    without credentials.
    """
    scope = getattr(websocket, "scope", {}) or {}
    if scope.get("path") == "/ws/internal":
        raise NodeUserError("Memory access requires an authenticated client")


async def _resolve_store_and_scope(
    data: Dict[str, Any], websocket: WebSocket
) -> tuple[MemoryToolStore, MemoryScope]:
    _require_external_socket(websocket)
    workflow_id = str(data.get("workflow_id") or "").strip()
    node_id = str(
        data.get("memory_node_id") or data.get("node_id") or ""
    ).strip()
    if not workflow_id:
        raise NodeUserError("workflow_id required")
    if not node_id:
        raise NodeUserError("memory_node_id required")

    database = get_database()
    saved = await database.get_workflow(workflow_id)
    if saved is None:
        raise NodeUserError("Workflow not found")
    graph = saved.data if hasattr(saved, "data") else saved.get("data", saved)
    owner_id = _authenticated_owner(websocket)
    stored_owner = (
        str(graph.get("owner_id") or "")
        if isinstance(graph, dict)
        else ""
    )
    if stored_owner and stored_owner != owner_id:
        raise NodeUserError("Workflow access denied")
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    matches = [
        node
        for node in nodes
        if str(node.get("id") or "") == node_id
        and str(node.get("type") or node.get("data", {}).get("type") or "")
        == "simpleMemory"
    ]
    if len(matches) != 1:
        raise NodeUserError(
            "Memory node does not belong to the requested workflow"
        )
    return MemoryToolStore(database), MemoryScope(
        owner_id=owner_id,
        workflow_id=workflow_id,
        memory_node_id=node_id,
    )


def _validate(operation: str, data: Dict[str, Any]) -> SimpleMemoryToolInput:
    allowed = {
        "content",
        "title",
        "category",
        "tags",
        "expires_at",
        "query",
        "categories",
        "limit",
        "cursor",
        "memory_id",
        "expected_version",
        "patch",
    }
    try:
        return SimpleMemoryToolInput.model_validate(
            {
                "operation": operation,
                **{key: data[key] for key in allowed if key in data},
            }
        )
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        raise NodeUserError(
            f"Invalid Memory request: {first.get('msg', str(exc))}"
        ) from exc


def _operation_id(data: Dict[str, Any], operation: str) -> Optional[str]:
    value = data.get("request_id") or data.get("operation_id")
    return f"memory-ui:{operation}:{str(value)[:450]}" if value else None


@ws_response
async def handle_list_memory_items(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    store, scope = await _resolve_store_and_scope(data, websocket)
    operation = "recall" if str(data.get("query") or "").strip() else "list"
    args = _validate(operation, data)
    try:
        result = (
            await store.recall(
                scope,
                query=args.query or "",
                categories=args.categories,
                tags=args.tags,
                limit=args.limit,
                cursor=args.cursor,
            )
            if operation == "recall"
            else await store.list(
                scope,
                categories=args.categories,
                tags=args.tags,
                limit=args.limit,
                cursor=args.cursor,
            )
        )
    except MemoryStoreError as exc:
        raise NodeUserError(str(exc)) from exc
    return {"success": True, **result}


@ws_response
async def handle_get_memory_item(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    store, scope = await _resolve_store_and_scope(data, websocket)
    args = _validate("get", data)
    try:
        result = await store.get(scope, args.memory_id or "")
    except MemoryStoreError as exc:
        raise NodeUserError(str(exc)) from exc
    return {"success": True, **result}


@ws_response
async def handle_remember_memory(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    store, scope = await _resolve_store_and_scope(data, websocket)
    args = _validate("remember", data)
    result = await store.remember(
        scope,
        content=args.content or "",
        title=args.title,
        category=args.category,
        tags=args.tags,
        expires_at=args.expires_at,
        operation_id=_operation_id(data, "remember"),
    )
    return {"success": True, **result}


@ws_response
async def handle_update_memory_item(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    store, scope = await _resolve_store_and_scope(data, websocket)
    args = _validate("update", data)
    assert args.patch is not None
    try:
        result = await store.update(
            scope,
            memory_id=args.memory_id or "",
            expected_version=args.expected_version or 0,
            patch=args.patch.model_dump(exclude_unset=True),
            operation_id=_operation_id(data, "update"),
        )
    except MemoryStoreError as exc:
        raise NodeUserError(str(exc)) from exc
    return {"success": True, **result}


@ws_response
async def handle_forget_memory_item(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    store, scope = await _resolve_store_and_scope(data, websocket)
    args = _validate("forget", data)
    try:
        result = await store.forget(
            scope,
            memory_id=args.memory_id or "",
            expected_version=args.expected_version or 0,
            operation_id=_operation_id(data, "forget"),
        )
    except MemoryStoreError as exc:
        raise NodeUserError(str(exc)) from exc
    return {"success": True, **result}


@ws_response
async def handle_clear_memory_items(
    data: Dict[str, Any], websocket: WebSocket
) -> Dict[str, Any]:
    """Human-only bulk clear. The LLM ToolInput has no clear operation."""
    store, scope = await _resolve_store_and_scope(data, websocket)
    result = await store.clear_namespace(
        scope,
        operation_id=_operation_id(data, "clear"),
    )
    return {"success": True, **result}


WSHandler = Callable[
    [Dict[str, Any], WebSocket], Awaitable[Dict[str, Any]]
]
WS_HANDLERS: Dict[str, WSHandler] = {
    "list_memory_items": handle_list_memory_items,
    "get_memory_item": handle_get_memory_item,
    "remember_memory": handle_remember_memory,
    "update_memory_item": handle_update_memory_item,
    "forget_memory_item": handle_forget_memory_item,
    "clear_memory_items": handle_clear_memory_items,
}


__all__ = [
    "WS_HANDLERS",
    "handle_clear_memory_items",
    "handle_forget_memory_item",
    "handle_get_memory_item",
    "handle_list_memory_items",
    "handle_remember_memory",
    "handle_update_memory_item",
]
