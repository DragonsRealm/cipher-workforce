"""Authorized WebSocket handlers for the Context inspector.

Raw journal/replay data is returned only from ``get_agent_context`` after the
saved workflow is verified to own the requested Context node.  Lifecycle
broadcasts contain metadata only.  Exports are deliberately redacted: replay
payloads, provider bindings, signatures, and message bodies never leave via
the export path.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import WebSocket

from models.agent_context import (
    AgentContextEvent,
    AgentContextThreadSummary,
)
from services.agent_context import (
    AgentContextStore,
    OpaqueCheckpointError,
    reconstruct_message_wire_v2,
)
from services.agent_context.lifecycle import fence_context_provider_resources
from services.plugin import NodeUserError
from services.plugin.ws import ws_response

from ._events import dispatch_context_epoch_started


_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


def _database():
    from core.container import container

    return container.database()


def _store() -> AgentContextStore:
    return AgentContextStore(_database())


def _require_external_socket(websocket: WebSocket) -> None:
    """The internal unauthenticated worker socket may not inspect journals."""

    scope = getattr(websocket, "scope", {}) or {}
    if scope.get("path") == "/ws/internal":
        raise NodeUserError("Context inspection requires an authenticated client")


def _authenticated_owner(websocket: WebSocket) -> str:
    state = getattr(websocket, "state", None)
    value = getattr(state, "user_id", None) if state is not None else None
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value)
    scope = getattr(websocket, "scope", {}) or {}
    value = scope.get("user_id") if isinstance(scope, dict) else None
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value)
    return "owner"


async def _authorize_context_node(
    *,
    websocket: WebSocket,
    workflow_id: str,
    context_node_id: str,
) -> Any:
    _require_external_socket(websocket)
    if not workflow_id or not context_node_id:
        raise NodeUserError("workflow_id and context_node_id are required")
    workflow = await _database().get_workflow(workflow_id)
    if workflow is None:
        raise NodeUserError("Workflow not found")
    graph = workflow.data if isinstance(workflow.data, dict) else {}
    stored_owner = str(graph.get("owner_id") or "")
    if stored_owner and stored_owner != _authenticated_owner(websocket):
        raise NodeUserError("Workflow access denied")
    owned = any(
        isinstance(node, dict)
        and str(node.get("id") or "") == context_node_id
        and str(node.get("type") or "") == "context"
        for node in graph.get("nodes", [])
    )
    if not owned:
        raise NodeUserError(
            "Context node does not belong to the requested workflow"
        )
    return workflow


def _optional_generation(data: Dict[str, Any]) -> Optional[int]:
    value = data.get("generation")
    if value in (None, ""):
        return None
    try:
        generation = int(value)
    except (TypeError, ValueError) as exc:
        raise NodeUserError("generation must be an integer") from exc
    if generation < 0:
        raise NodeUserError("generation must be non-negative")
    return generation


async def _select_thread(
    store: AgentContextStore,
    data: Dict[str, Any],
) -> Optional[AgentContextThreadSummary]:
    generation = _optional_generation(data)
    threads = await store.list_threads(
        workflow_id=str(data["workflow_id"]),
        context_node_id=str(data["context_node_id"]),
        generation=generation,
        include_archived=False,
    )
    requested_thread = str(data.get("thread_id") or "")
    selected = next(
        (
            thread
            for thread in threads
            if not requested_thread
            or thread.ref.thread_id == requested_thread
        ),
        None,
    )
    if requested_thread and selected is None:
        raise NodeUserError("Context thread not found")
    requested_epoch = data.get("epoch")
    if selected is not None and requested_epoch not in (None, ""):
        try:
            epoch = int(requested_epoch)
        except (TypeError, ValueError) as exc:
            raise NodeUserError("epoch must be an integer") from exc
        if epoch != selected.ref.epoch:
            raise NodeUserError("Requested Context epoch is no longer active")
    return selected


def _encode_cursor(sequence: Optional[int]) -> Optional[str]:
    if sequence is None:
        return None
    return base64.urlsafe_b64encode(str(sequence).encode()).decode().rstrip("=")


def _decode_cursor(value: Any) -> int:
    if value in (None, ""):
        return 0
    if not isinstance(value, str) or len(value) > 32:
        raise NodeUserError("Invalid Context cursor")
    try:
        padded = value + "=" * (-len(value) % 4)
        sequence = int(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise NodeUserError("Invalid Context cursor") from exc
    if sequence < 0:
        raise NodeUserError("Invalid Context cursor")
    return sequence


async def _event_view(
    store: AgentContextStore,
    event: AgentContextEvent,
    *,
    hydrate_payload: bool,
) -> dict[str, Any]:
    value = event.model_dump(mode="json", exclude_none=True)
    if (
        hydrate_payload
        and event.payload_ref
        and event.payload_ref.startswith("sha256:")
    ):
        try:
            value["payload"] = await store.get_blob(event.payload_ref)
        except Exception:
            # External/missing payload references stay visible as references;
            # a journal page must not fail because an attachment was retired.
            value["payload_unavailable"] = True
    return value


def _fidelity(
    provider: Optional[str],
    binding_fidelity: Optional[str],
) -> tuple[str, bool]:
    if binding_fidelity:
        return binding_fidelity, binding_fidelity != "observable_only"
    normalized = (provider or "").lower()
    if "codex" in normalized or normalized == "rlm":
        return "observable_only", False
    if "claude_code" in normalized or "vertex" in normalized:
        return "provider_bound", True
    if normalized:
        return "provider_replayable", True
    return "unknown", False


async def _snapshot(
    store: AgentContextStore,
    thread: AgentContextThreadSummary,
    *,
    threads: list[AgentContextThreadSummary],
    view: str,
    cursor: Any = None,
    limit: int = _DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    ref = thread.ref
    active = await store.load_active(ref)
    checkpoints = await store.list_checkpoints(ref)
    bindings = await store.load_provider_bindings(
        ref,
        provider=thread.provider,
    )
    fidelity, resumable = _fidelity(
        thread.provider,
        bindings[-1].fidelity if bindings else None,
    )
    params = await _database().get_node_parameters(ref.context_node_id) or {}
    context_window = params.get("context_window_override")
    try:
        context_window = int(context_window) if context_window else None
    except (TypeError, ValueError):
        context_window = None
    base: dict[str, Any] = {
        "threads": [
            {
                "thread_id": item.ref.thread_id,
                "generation": item.ref.generation,
                "epoch": item.ref.epoch,
                "revision": item.ref.revision,
                "thread_kind": item.thread_kind,
                "provider": item.provider,
                "status": item.status,
                "active_token_count": item.active_token_count,
                "updated_at": item.updated_at.isoformat(),
            }
            for item in threads
        ],
        "thread_id": ref.thread_id,
        "epoch": ref.epoch,
        "revision": active.ref.revision,
        "provider": thread.provider,
        "fidelity": fidelity,
        "resumable": resumable,
        "active_token_count": active.active_token_count,
        "context_window": context_window,
        "pressure_ratio": (
            active.active_token_count / context_window
            if context_window
            else None
        ),
        "provider_binding_status": "bound" if bindings else "unbound",
        "checkpoints": [
            checkpoint.model_dump(mode="json") for checkpoint in checkpoints
        ],
    }
    if view == "active":
        checkpoint_payload: Any = None
        if active.checkpoint is not None:
            try:
                checkpoint_payload = await store.get_blob(
                    active.checkpoint.replay_payload_ref
                )
            except Exception:
                checkpoint_payload = {"unavailable": True}
        base["active_replay"] = {
            "checkpoint": (
                {
                    "metadata": active.checkpoint.model_dump(mode="json"),
                    "payload": checkpoint_payload,
                }
                if active.checkpoint is not None
                else None
            ),
            "tail": [
                await _event_view(store, event, hydrate_payload=True)
                for event in active.tail
            ],
        }
        base["events"] = []
        base["next_cursor"] = None
        return base

    # Scope the journal to the live epoch. Workflow Reset (and Clear epoch)
    # rotate the thread to a fresh epoch and leave the prior events in place
    # as archived history; reading every epoch made a reset look like it had
    # done nothing, because the panel still listed every pre-reset turn.
    events, next_after = await store.load_journal_page(
        active.ref,
        after_sequence=_decode_cursor(cursor),
        limit=limit,
        epoch=active.ref.epoch,
    )
    base["events"] = [
        await _event_view(store, event, hydrate_payload=True)
        for event in events
    ]
    base["active_replay"] = None
    base["next_cursor"] = _encode_cursor(next_after)
    return base


def _empty_snapshot() -> dict[str, Any]:
    return {
        "threads": [],
        "thread_id": None,
        "epoch": None,
        "revision": None,
        "provider": None,
        "fidelity": "unknown",
        "resumable": False,
        "active_token_count": 0,
        "context_window": None,
        "pressure_ratio": None,
        "provider_binding_status": "unbound",
        "checkpoints": [],
        "events": [],
        "active_replay": None,
        "next_cursor": None,
    }


@ws_response
async def handle_get_agent_context(
    data: Dict[str, Any],
    websocket: WebSocket,
) -> Dict[str, Any]:
    await _authorize_context_node(
        websocket=websocket,
        workflow_id=str(data.get("workflow_id") or ""),
        context_node_id=str(data.get("context_node_id") or ""),
    )
    store = _store()
    generation = _optional_generation(data)
    threads = await store.list_threads(
        workflow_id=str(data["workflow_id"]),
        context_node_id=str(data["context_node_id"]),
        generation=generation,
        include_archived=False,
    )
    requested_thread = str(data.get("thread_id") or "")
    thread = next(
        (
            item
            for item in threads
            if not requested_thread or item.ref.thread_id == requested_thread
        ),
        None,
    )
    if requested_thread and thread is None:
        raise NodeUserError("Context thread not found")
    requested_epoch = data.get("epoch")
    if thread is not None and requested_epoch not in (None, ""):
        try:
            epoch = int(requested_epoch)
        except (TypeError, ValueError) as exc:
            raise NodeUserError("epoch must be an integer") from exc
        if epoch != thread.ref.epoch:
            raise NodeUserError("Requested Context epoch is no longer active")
    if thread is None:
        return {"success": True, "context": _empty_snapshot()}
    view = str(data.get("view") or "journal")
    if view not in {"journal", "active"}:
        raise NodeUserError("view must be journal or active")
    try:
        limit = int(data.get("limit") or _DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError) as exc:
        raise NodeUserError("limit must be an integer") from exc
    limit = max(1, min(limit, _MAX_PAGE_SIZE))
    return {
        "success": True,
        "context": await _snapshot(
            store,
            thread,
            threads=threads,
            view=view,
            cursor=data.get("cursor"),
            limit=limit,
        ),
    }


@ws_response
async def handle_clear_agent_context(
    data: Dict[str, Any],
    websocket: WebSocket,
) -> Dict[str, Any]:
    await _authorize_context_node(
        websocket=websocket,
        workflow_id=str(data.get("workflow_id") or ""),
        context_node_id=str(data.get("context_node_id") or ""),
    )
    store = _store()
    thread = await _select_thread(store, data)
    if thread is None:
        return {"success": True, "context": _empty_snapshot()}
    operation_id = str(
        data.get("operation_id")
        or f"context-ui-clear:{data.get('request_id') or uuid.uuid4()}"
    )
    new_ref = await store.start_epoch(
        thread.ref,
        operation_id=operation_id,
        provider=thread.provider,
    )
    await fence_context_provider_resources(
        context_node_id=new_ref.context_node_id,
        thread_id=new_ref.thread_id,
        keep_epoch=new_ref.epoch,
    )
    await dispatch_context_epoch_started(
        workflow_id=new_ref.workflow_id,
        context_node_id=new_ref.context_node_id,
        thread_id=new_ref.thread_id,
        epoch=new_ref.epoch,
        revision=new_ref.revision,
        provider=thread.provider,
        reason="clear",
    )
    return {
        "success": True,
        "context": {
            "thread_id": new_ref.thread_id,
            "epoch": new_ref.epoch,
            "revision": new_ref.revision,
        },
    }


async def _portable_handoff(
    store: AgentContextStore,
    thread: AgentContextThreadSummary,
) -> str:
    active = await store.load_active(thread.ref)
    portable = True
    try:
        _, messages = await reconstruct_message_wire_v2(
            store,
            active.ref,
        )
    except OpaqueCheckpointError:
        portable = False
        # Historical opaque checkpoints cannot be translated. Preserve the
        # exact observable tail and clearly mark the handoff as partial.
        messages = [
            event.message_wire_v2
            for event in active.tail
            if event.message_wire_v2 is not None
        ]
    return await store.put_blob(
        {
            "format": "agent-context-portable-handoff-v1",
            "fidelity": (
                "portable" if portable else "observable_only"
            ),
            "messages": messages,
            "source": {
                "workflow_id": thread.ref.workflow_id,
                "context_node_id": thread.ref.context_node_id,
                "generation": thread.ref.generation,
                "thread_id": thread.ref.thread_id,
                "epoch": thread.ref.epoch,
                "revision": active.ref.revision,
                "provider": thread.provider,
            },
            # Metadata is safe inside hash-addressed Context storage; it is
            # not returned by normal graph/status/export paths.
            "source_checkpoint": (
                {
                    "provider": active.checkpoint.provider,
                    "strategy": active.checkpoint.strategy,
                    "covers_through_sequence": (
                        active.checkpoint.covers_through_sequence
                    ),
                    "source_hash": active.checkpoint.source_hash,
                }
                if active.checkpoint is not None
                else None
            ),
        }
    )


@ws_response
async def handle_fork_agent_context(
    data: Dict[str, Any],
    websocket: WebSocket,
) -> Dict[str, Any]:
    await _authorize_context_node(
        websocket=websocket,
        workflow_id=str(data.get("workflow_id") or ""),
        context_node_id=str(data.get("context_node_id") or ""),
    )
    store = _store()
    thread = await _select_thread(store, data)
    if thread is None:
        return {"success": True, "context": _empty_snapshot()}
    # The provider binding is backend-owned (RFC-0002 section 3: the canvas
    # never owns provider bindings). Deriving it from the thread rather than
    # the request also stops a stale client snapshot from forking the epoch
    # onto a provider the thread already moved off.
    provider = str(thread.provider or "portable")
    handoff_ref = await _portable_handoff(store, thread)
    operation_id = str(
        data.get("operation_id")
        or f"context-ui-fork:{data.get('request_id') or uuid.uuid4()}"
    )
    new_ref = await store.fork_provider(
        thread.ref,
        provider=provider,
        operation_id=operation_id,
        portable_handoff_ref=handoff_ref,
    )
    await dispatch_context_epoch_started(
        workflow_id=new_ref.workflow_id,
        context_node_id=new_ref.context_node_id,
        thread_id=new_ref.thread_id,
        epoch=new_ref.epoch,
        revision=new_ref.revision,
        provider=provider,
        reason="fork",
    )
    return {
        "success": True,
        "context": {
            "thread_id": new_ref.thread_id,
            "epoch": new_ref.epoch,
            "revision": new_ref.revision,
            "provider": provider,
        },
    }


def _redacted_event(event: AgentContextEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "event_type": event.event_type,
        "provider": event.provider,
        "previous_hash": event.previous_hash,
        "payload_hash": event.payload_hash,
        "redacted": True,
    }


async def _redacted_export(
    store: AgentContextStore,
    thread: AgentContextThreadSummary,
) -> dict[str, Any]:
    events: list[AgentContextEvent] = []
    after = 0
    while True:
        page, next_after = await store.load_journal_page(
            thread.ref,
            after_sequence=after,
            limit=_MAX_PAGE_SIZE,
        )
        events.extend(page)
        if next_after is None:
            break
        after = next_after
    checkpoints = await store.list_checkpoints(thread.ref, limit=100)
    return {
        "schema": "opencompany.agent-context.redacted.v1",
        "redacted": True,
        "context": {
            "workflow_id": thread.ref.workflow_id,
            "context_node_id": thread.ref.context_node_id,
            "generation": thread.ref.generation,
            "thread_id": thread.ref.thread_id,
            "epoch": thread.ref.epoch,
            "revision": thread.ref.revision,
            "provider": thread.provider,
            "status": thread.status,
            "active_token_count": thread.active_token_count,
        },
        "checkpoints": [
            {
                "provider": checkpoint.provider,
                "strategy": checkpoint.strategy,
                "covers_through_sequence": (
                    checkpoint.covers_through_sequence
                ),
                "active_token_count": checkpoint.active_token_count,
                "source_revision": checkpoint.source_revision,
                "source_hash": checkpoint.source_hash,
                "replay_payload_redacted": True,
            }
            for checkpoint in checkpoints
        ],
        "events": [_redacted_event(event) for event in events],
        "provider_bindings_redacted": True,
    }


@ws_response
async def handle_export_agent_context(
    data: Dict[str, Any],
    websocket: WebSocket,
) -> Dict[str, Any]:
    await _authorize_context_node(
        websocket=websocket,
        workflow_id=str(data.get("workflow_id") or ""),
        context_node_id=str(data.get("context_node_id") or ""),
    )
    store = _store()
    thread = await _select_thread(store, data)
    export = (
        await _redacted_export(store, thread)
        if thread is not None
        else {
            "schema": "opencompany.agent-context.redacted.v1",
            "redacted": True,
            "context": None,
            "checkpoints": [],
            "events": [],
            "provider_bindings_redacted": True,
        }
    )
    safe_id = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        str(data["context_node_id"]),
    ).strip("-")
    return {
        "success": True,
        "filename": f"context-{safe_id or 'export'}-redacted.json",
        "content": json.dumps(
            export,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        "export": export,
    }


WSHandler = Callable[
    [Dict[str, Any], WebSocket],
    Awaitable[Dict[str, Any]],
]

WS_HANDLERS: Dict[str, WSHandler] = {
    "get_agent_context": handle_get_agent_context,
    "clear_agent_context": handle_clear_agent_context,
    "fork_agent_context": handle_fork_agent_context,
    "export_agent_context": handle_export_agent_context,
}


__all__ = [
    "WS_HANDLERS",
    "handle_clear_agent_context",
    "handle_export_agent_context",
    "handle_fork_agent_context",
    "handle_get_agent_context",
]
