"""Context descriptor construction.

The ``context`` node is a declarative UI and policy surface onto an agent's
context scope — it never owns the journal itself (RFC-0002 section 3). What
it *does* own is the shape of the descriptor handed to the agent runtime:
which thread the agent resolves, and the policy the operator configured on
this node.

That shape lives here rather than in ``services/plugin/edge_walker.py`` so
the framework carries no knowledge of this plugin's parameters or its
thread-selection rules. Registered from the package ``__init__`` via
``services.plugin.edge_walker.register_agent_context_builder``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Session ids that carry no durable identity — an agent addressed through
# any of these is not resuming a named conversation.
_ANONYMOUS_SESSION_IDS = {"", "default", "internal"}


def _resolve_thread_session_id(
    context: Dict[str, Any],
    delegated_task_id: Optional[str],
) -> Optional[str]:
    """Pick the session that identifies this agent's context thread.

    An explicit chat/session id always wins. Otherwise a delegated task is
    the durable isolation boundary — delegation helpers carry an internal
    parent session that must NOT be inherited, or every subagent would
    share its parent's thread.
    """
    explicit_session_id = context.get("explicit_session_id")
    if explicit_session_id:
        return explicit_session_id
    if delegated_task_id:
        return None
    raw_session_id = context.get("session_id")
    if str(raw_session_id or "").strip().lower() in _ANONYMOUS_SESSION_IDS:
        return None
    return raw_session_id


async def build_agent_context_descriptor(
    source_node_id: str,
    context: Dict[str, Any],
    database: Any,
) -> Optional[Dict[str, Any]]:
    """Describe the Context scope an agent should run against.

    Returns ``None`` when this connection contributes no context, which the
    edge walker treats as "skip this edge".
    """
    generation = int(
        context.get("generation") or context.get("workflow_generation") or 0
    )
    # Generation zero is an immutable migration/import artifact, not a live
    # Context namespace. One-off and manual executions stay stateless until
    # Start admits a real workflow generation.
    if generation <= 0:
        return None

    # Only this node's DECLARED policy travels in the descriptor. Reading the
    # stored row verbatim shipped whatever else happened to be saved against
    # the node — migrated graphs still carry the legacy ``memory_content``
    # markdown here — which then landed in the runtime snapshot, was persisted
    # into the journal, and was rendered to the operator as though it were
    # part of the model's context.
    from . import AgentContextParams

    stored = await database.get_node_parameters(source_node_id) or {}
    policy = {
        name: stored[name]
        for name in AgentContextParams.model_fields
        if name in stored
    }
    delegated_task_id = (
        context.get("delegated_task_id")
        or context.get("parent_task_id")
        or context.get("team_task_id")
        or context.get("task_id")
    )

    return {
        "kind": "context",
        "node_id": source_node_id,
        "context_node_id": source_node_id,
        "workflow_id": str(context.get("workflow_id") or ""),
        "generation": generation,
        "user_id": context.get("user_id", "owner"),
        "execution_id": context.get("execution_id"),
        "session_id": _resolve_thread_session_id(context, delegated_task_id),
        "delegated_task_id": delegated_task_id,
        "policy": policy,
    }


__all__ = ["build_agent_context_descriptor"]
