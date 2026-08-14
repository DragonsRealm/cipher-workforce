"""One-time handoff from immutable generation-zero migration artifacts."""

from __future__ import annotations

import hashlib
from typing import Any

from models.agent_context import AgentContextRef, AgentContextThreadSummary
from services.agent_context.store import AgentContextStore
from services.llm.protocol import Message, message_to_wire


async def import_generation_zero_handoff(
    store: AgentContextStore,
    target_ref: AgentContextRef,
) -> AgentContextRef:
    """Copy legacy partial context into a live generation idempotently.

    Graph migration writes recognized legacy state to generation zero so the
    original artifact is immutable. A newly admitted generation receives a
    clearly labelled MessageWire boundary that can participate in normal
    replay. Provider bindings are copied only when their source thread is
    unambiguous; raw binding values remain in the blob store.
    """

    if target_ref.generation <= 0:
        return target_ref
    sources = await store.list_threads(
        workflow_id=target_ref.workflow_id,
        context_node_id=target_ref.context_node_id,
        generation=0,
        include_archived=True,
        limit=1000,
    )
    if not sources:
        return target_ref

    exact = [
        source
        for source in sources
        if source.ref.thread_id == target_ref.thread_id
    ]
    selected = exact or sorted(sources, key=lambda item: item.ref.thread_id)
    current = target_ref
    for source in selected:
        current = await _import_legacy_events(store, source, current)

    # A continuation UUID/interaction ID must not be guessed when multiple
    # legacy Memory sessions could have supplied it.
    if len(selected) == 1:
        await _copy_provider_bindings(store, selected[0], current)
    return current


async def _import_legacy_events(
    store: AgentContextStore,
    source: AgentContextThreadSummary,
    target_ref: AgentContextRef,
) -> AgentContextRef:
    after = 0
    current = target_ref
    while True:
        events, next_after = await store.load_journal_page(
            source.ref,
            after_sequence=after,
            limit=200,
        )
        for event in events:
            if event.event_type != "legacy_partial" or not event.payload_ref:
                continue
            payload = await store.get_blob(event.payload_ref)
            content = (
                payload.get("content")
                if isinstance(payload, dict)
                else None
            )
            if not content:
                continue
            message = Message(
                role="user",
                content=(
                    "Legacy conversation context imported with "
                    "legacy_partial fidelity. It may be incomplete; treat "
                    "the current request and current system instruction as "
                    "authoritative.\n\n"
                    f"{content}"
                ),
            )
            result = await store.append_transition(
                current,
                event_type="legacy_partial.handoff",
                operation_id=_operation_id(
                    "legacy-context-handoff",
                    target_ref=current,
                    source_identity=(
                        f"{source.ref.thread_id}:{event.payload_hash}"
                    ),
                ),
                message_wire=dict(message_to_wire(message)),
                payload_ref=event.payload_ref,
                provider="legacy",
            )
            current = result.ref
        if next_after is None:
            break
        after = next_after
    return current


async def _copy_provider_bindings(
    store: AgentContextStore,
    source: AgentContextThreadSummary,
    target_ref: AgentContextRef,
) -> None:
    bindings = await store.load_provider_bindings(source.ref)
    for binding in bindings:
        payload: Any = await store.get_blob(binding.binding_ref)
        await store.bind_provider(
            target_ref,
            provider=binding.provider,
            binding_type=binding.binding_type,
            binding=payload,
            operation_id=_operation_id(
                "legacy-context-binding",
                target_ref=target_ref,
                source_identity=(
                    f"{source.ref.thread_id}:{binding.provider}:"
                    f"{binding.binding_type}:"
                    f"{binding.binding_ref.removeprefix('sha256:')}"
                ),
            ),
            fidelity=binding.fidelity,
        )


def _operation_id(
    prefix: str,
    *,
    target_ref: AgentContextRef,
    source_identity: str,
) -> str:
    # RuntimeMutation IDs are globally unique, not merely unique within a
    # Context thread. Include the complete target fence while keeping the
    # persisted identifier bounded.
    material = (
        f"{target_ref.workflow_id}:{target_ref.context_node_id}:"
        f"{target_ref.generation}:{target_ref.thread_id}:"
        f"{source_identity}"
    )
    return f"{prefix}:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


__all__ = ["import_generation_zero_handoff"]
