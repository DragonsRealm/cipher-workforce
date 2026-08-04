"""Durable second phase for Context V2 workflow normalization.

The pure graph migration returns state-import receipts after canonical IDs are
known.  This module commits those receipts before the normalized graph replaces
its legacy topology, so a failed import never silently discards the only copy
of legacy conversation state.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable, Mapping

from services.agent_context import AgentContextStore


async def load_node_parameters(
    database: Any,
    nodes: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Hydrate parameters without serial reads at graph boundaries."""

    node_ids = [
        str(node["id"])
        for node in nodes
        if node.get("id") is not None
    ]
    getter = getattr(database, "get_node_parameters", None)
    if not callable(getter):
        return {}
    values = await asyncio.gather(
        *(getter(node_id) for node_id in node_ids)
    )
    return {
        node_id: dict(value or {})
        for node_id, value in zip(node_ids, values)
        if value
    }


async def import_legacy_context_receipts(
    database: Any,
    receipts: Iterable[Mapping[str, Any]],
) -> int:
    """Idempotently preserve Markdown and provider bindings as artifacts."""

    store = AgentContextStore(database)
    imported = 0
    for receipt in receipts:
        workflow_id = str(receipt.get("workflow_id") or "")
        context_node_id = str(receipt.get("context_node_id") or "")
        operation_id = str(receipt.get("operation_id") or "")
        if not workflow_id or not context_node_id or not operation_id:
            raise ValueError("invalid_context_import_receipt")

        # Generation zero is the immutable migration epoch. A new execution
        # imports it as a portable handoff into its own generation/thread.
        ref = await store.resolve_thread(
            workflow_id=workflow_id,
            context_node_id=context_node_id,
            generation=0,
            session_id=str(
                receipt.get("legacy_session_id")
                or receipt.get("agent_node_id")
                or "legacy"
            ),
        )
        markdown = receipt.get("markdown")
        payload_ref = None
        if markdown:
            payload_ref = await store.put_blob(
                {
                    "format": "legacy_markdown",
                    "fidelity": "legacy_partial",
                    "content": str(markdown),
                    "source_memory_node_id": receipt.get(
                        "legacy_memory_node_id"
                    ),
                }
            )
        await store.append_transition(
            ref,
            event_type="legacy_partial",
            operation_id=operation_id,
            payload_ref=payload_ref,
            provider="legacy",
        )

        bindings = dict(receipt.get("provider_bindings") or {})
        claude_session = bindings.get("last_session_id")
        if claude_session:
            await store.bind_provider(
                ref,
                provider="claude_code",
                binding_type="session_uuid",
                binding={"session_uuid": str(claude_session)},
                operation_id=f"{operation_id}:claude-session",
                fidelity="provider_bound",
            )
        vertex_interaction = bindings.get("vertex_interaction_id")
        vertex_environment = bindings.get("vertex_environment_id")
        if vertex_interaction or vertex_environment:
            await store.bind_provider(
                ref,
                provider="vertex",
                binding_type="interaction_environment",
                binding={
                    "interaction_id": vertex_interaction,
                    "environment_id": vertex_environment,
                },
                operation_id=f"{operation_id}:vertex-binding",
                fidelity="provider_bound",
            )
        imported += 1
    return imported


async def persist_parameter_aliases(
    database: Any,
    *,
    aliases: Mapping[str, str],
    parameters: Mapping[str, Mapping[str, Any]],
) -> None:
    """Rekey node configuration onto canonical node ids.

    This only moves rows; it never edits their contents. It previously also
    stripped a set of "legacy runtime fields" from every node, which destroyed
    real configuration: ``session_id`` is in that set and is a declared,
    load-bearing parameter on ``chatTrigger`` / ``chatSend`` / ``chatHistory``,
    so an ordinary read silently widened a chat trigger to match every session.

    Retiring them is unnecessary as well as unsafe. ``SimpleMemoryParams``
    declares ``extra="ignore"``, so a leftover ``memory_content`` on a migrated
    node is already inert — it reaches neither runtime behaviour nor the
    NodeSpec.
    """

    reverse_aliases = {new: old for old, new in aliases.items()}
    save = getattr(database, "save_node_parameters", None)
    remove = getattr(database, "delete_node_parameters", None)
    if not callable(save):
        return
    for node_id, raw_params in parameters.items():
        await save(node_id, dict(raw_params or {}))
        old_id = reverse_aliases.get(node_id)
        if old_id and old_id != node_id and callable(remove):
            await remove(old_id)


async def archive_removed_contexts(
    database: Any,
    *,
    workflow_id: str,
    previous_nodes: Iterable[Mapping[str, Any]],
    normalized_nodes: Iterable[Mapping[str, Any]],
    aliases: Mapping[str, str],
) -> int:
    """Archive journals whose system Context node left the saved graph."""

    previous = {
        str(node.get("id") or "")
        for node in previous_nodes
        if node.get("type") == "context" and node.get("id")
    }
    current = {
        str(node.get("id") or "")
        for node in normalized_nodes
        if node.get("type") == "context" and node.get("id")
    }
    # Canonicalization is a rename, not deletion.
    removed = {
        context_id
        for context_id in previous
        if context_id not in current
        and aliases.get(context_id) not in current
    }
    if not removed:
        return 0
    store = AgentContextStore(database)
    for context_id in sorted(removed):
        await store.archive_context(
            workflow_id=workflow_id,
            context_node_id=context_id,
            generation=None,
            operation_id=(
                f"context-node-removed:{workflow_id}:{context_id}"
            ),
        )
    return len(removed)


__all__ = [
    "archive_removed_contexts",
    "import_legacy_context_receipts",
    "load_node_parameters",
    "persist_parameter_aliases",
]
