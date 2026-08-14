"""Small runtime adapters bound to one Context thread."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from models.agent_context import (
    AgentContextAppendResult,
    AgentContextRef,
)
from services.agent_context.store import AgentContextStore
from services.llm.protocol import (
    Message,
    message_from_wire,
    message_to_wire,
)


class OpaqueCheckpointError(RuntimeError):
    """Raised when a native replay payload cannot be rendered portably."""


class AgentContextTransitionWriter:
    """Epoch-bound implementation of ``AgentContextTransitionSink``.

    The writer owns only the latest bounded ref.  Payload bodies are moved to
    hash-addressed blob storage before the journal entry is committed.
    """

    def __init__(
        self,
        store: AgentContextStore,
        ref: AgentContextRef,
    ) -> None:
        self.store = store
        self.ref = ref
        self._append_lock = asyncio.Lock()

    async def append_transition(
        self,
        *,
        event_type: str,
        operation_id: str,
        provider: str,
        message_wire: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> AgentContextAppendResult:
        payload_ref = (
            await self.store.put_blob(payload)
            if payload is not None
            else None
        )
        async with self._append_lock:
            result = await self.store.append_transition(
                self.ref,
                event_type=event_type,
                operation_id=operation_id,
                provider=provider,
                message_wire=message_wire,
                payload_ref=payload_ref,
            )
            self.ref = result.ref
            return result


async def reconstruct_transcript(
    store: AgentContextStore,
    ref: AgentContextRef,
) -> tuple[AgentContextRef, list[dict[str, Any]]]:
    """Reconstruct portable checkpoint messages plus the exact live tail.

    Provider-native checkpoints are intentionally opaque and must be replayed
    by that provider's context adapter.  Portable checkpoints use a JSON
    object containing ``messages`` (or ``message_wire`` /
    ``replay_messages``) and can be reconstructed here.
    """

    state = await store.load_active(ref)
    checkpoint_wires: list[dict[str, Any]] = []
    if state.checkpoint is not None:
        payload = await store.get_blob(state.checkpoint.replay_payload_ref)
        candidates: Any
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            candidates = (
                payload.get("messages")
                or payload.get("message_wire")
                or payload.get("replay_messages")
            )
        else:
            candidates = None
        if not isinstance(candidates, list):
            raise OpaqueCheckpointError(
                "checkpoint replay payload is provider-native"
            )
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise OpaqueCheckpointError(
                    "portable checkpoint contains a non-message item"
                )
            checkpoint_wires.append(
                dict(message_to_wire(message_from_wire(candidate)))
            )
    # A provider fork starts a fresh epoch from an explicit portable handoff.
    # It is a replay boundary even before the first request snapshot exists.
    handoff_wires: Optional[list[dict[str, Any]]] = None
    handoff_sequence = 0
    for event in reversed(state.tail):
        if event.event_type != "provider_handoff" or not event.payload_ref:
            continue
        payload = await store.get_blob(event.payload_ref)
        candidates = (
            payload.get("messages") if isinstance(payload, dict) else None
        )
        if not isinstance(candidates, list):
            continue
        handoff_wires = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise OpaqueCheckpointError(
                    "provider handoff contains a non-message item"
                )
            handoff_wires.append(
                dict(message_to_wire(message_from_wire(candidate)))
            )
        handoff_sequence = event.sequence
        break

    # Request snapshots are exact render boundaries.  They include system and
    # user messages, which do not otherwise have a standalone transition in
    # the journal.  Start from the latest committed snapshot and apply only
    # the assistant/tool transitions that followed it.
    snapshot_wires: Optional[list[dict[str, Any]]] = None
    snapshot_sequence = handoff_sequence
    for event in reversed(state.tail):
        if event.event_type != "request.snapshot" or not event.payload_ref:
            continue
        payload = await store.get_blob(event.payload_ref)
        candidates = (
            payload.get("messages") if isinstance(payload, dict) else None
        )
        if not isinstance(candidates, list):
            continue
        validated: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise OpaqueCheckpointError(
                    "request snapshot contains a non-message item"
                )
            validated.append(
                dict(message_to_wire(message_from_wire(candidate)))
            )
        snapshot_wires = validated
        snapshot_sequence = event.sequence
        break

    wires = (
        snapshot_wires
        if snapshot_wires is not None
        else (
            handoff_wires
            if handoff_wires is not None
            else checkpoint_wires
        )
    )
    wires.extend(
        dict(event.message_wire)
        for event in state.tail
        if event.sequence > snapshot_sequence
        and event.message_wire is not None
    )
    return state.ref, wires


async def reconstruct_messages(
    store: AgentContextStore,
    ref: AgentContextRef,
) -> tuple[AgentContextRef, list[Message]]:
    """Typed convenience wrapper over :func:`reconstruct_transcript`."""

    current_ref, wires = await reconstruct_transcript(store, ref)
    return current_ref, [message_from_wire(wire) for wire in wires]


__all__ = [
    "AgentContextTransitionWriter",
    "OpaqueCheckpointError",
    "reconstruct_transcript",
    "reconstruct_messages",
]
