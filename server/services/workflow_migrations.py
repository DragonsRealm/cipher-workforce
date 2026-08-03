"""Versioned, idempotent compatibility migrations for workflow graphs.

Graph normalization is deliberately pure.  Durable Context import happens
after canonical node IDs have been assigned and is driven by the returned
``state_imports`` receipts.  This keeps topology normalization safe for
editor previews while allowing persistence boundaries to commit topology
and imported runtime state transactionally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from constants import AI_AGENT_TYPES, ANDROID_SERVICE_NODE_TYPES


WORKFLOW_GRAPH_VERSION = 2


@dataclass(frozen=True)
class WorkflowGraphNormalization:
    """Result of the V2 graph migration pipeline."""

    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]
    node_parameters: Dict[str, Dict[str, Any]]
    warnings: List[str] = field(default_factory=list)
    aliases: Dict[str, str] = field(default_factory=dict)
    state_imports: List[Dict[str, Any]] = field(default_factory=list)
    graph_version: int = WORKFLOW_GRAPH_VERSION

    def graph_data(self, original: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return {
            **dict(original or {}),
            "graphVersion": self.graph_version,
            "nodes": self.nodes,
            "edges": self.edges,
        }


def _target_handle(edge: Mapping[str, Any]) -> Optional[str]:
    value = edge.get("targetHandle") or edge.get("target_handle")
    return str(value) if value else None


def _source_handle(edge: Mapping[str, Any]) -> Optional[str]:
    value = edge.get("sourceHandle") or edge.get("source_handle")
    return str(value) if value else None


def _requires_context(node_type: str) -> bool:
    """Read the declared plugin capability without type-name heuristics."""
    from services.node_registry import get_node_class

    node_cls = get_node_class(node_type)
    if node_cls is None:
        return False
    if bool(getattr(node_cls, "requires_context", False)):
        return True
    # Transitional support for plugins which shipped the capability as a
    # NodeSpec hint before gaining the BaseNode ClassVar.
    return bool((getattr(node_cls, "ui_hints", {}) or {}).get("requiresContext"))


def _context_node_for(agent: Mapping[str, Any], node_id: str) -> Dict[str, Any]:
    position = dict(agent.get("position") or {})
    x = position.get("x")
    y = position.get("y")
    context_position: Dict[str, Any] = {}
    if isinstance(x, (int, float)):
        context_position["x"] = x - 360
    if isinstance(y, (int, float)):
        context_position["y"] = y
    return {
        "id": node_id,
        "type": "context",
        "position": context_position,
        "data": {
            "label": "Context",
            "systemManaged": True,
            "agentNodeId": str(agent.get("id") or ""),
        },
    }


def _edge_id(prefix: str, source: str, target: str) -> str:
    safe_source = source.replace(":", "-")
    safe_target = target.replace(":", "-")
    return f"{prefix}-{safe_source}-{safe_target}"


def normalize_edge_handles(edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Canonicalize legacy ReactFlow handle keys without changing topology."""
    normalized: List[Dict[str, Any]] = []
    for edge in edges:
        item = dict(edge)
        target_handle = item.pop("target_handle", None)
        source_handle = item.pop("source_handle", None)
        if not item.get("targetHandle") and target_handle:
            item["targetHandle"] = target_handle
        if not item.get("sourceHandle") and source_handle:
            item["sourceHandle"] = source_handle
        normalized.append(item)
    return normalized


def normalize_legacy_android_toolkit(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    node_parameters: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Dict[str, Any]], List[str]]:
    """Replace legacy ``service -> androidTool -> agent`` graphs.

    The migration is pure and idempotent.  A service is connected directly
    to every valid agent formerly targeted by its toolkit. Existing direct
    edges win, and orphaned toolkits are removed with a warning.
    """
    params = dict(node_parameters or {})
    node_by_id = {node.get("id"): node for node in nodes if node.get("id")}
    toolkit_ids = {node_id for node_id, node in node_by_id.items() if node.get("type") == "androidTool"}
    if not toolkit_ids:
        return list(nodes), list(edges), params, []

    incoming: Dict[str, List[str]] = {node_id: [] for node_id in toolkit_ids}
    outgoing: Dict[str, List[str]] = {node_id: [] for node_id in toolkit_ids}
    for edge in edges:
        source, target = edge.get("source"), edge.get("target")
        if target in toolkit_ids:
            source_node = node_by_id.get(source, {})
            if source_node.get("type") in ANDROID_SERVICE_NODE_TYPES:
                incoming[target].append(source)
        if source in toolkit_ids:
            target_node = node_by_id.get(target, {})
            if target_node.get("type") in AI_AGENT_TYPES:
                outgoing[source].append(target)

    migrated_edges = [dict(edge) for edge in edges if edge.get("source") not in toolkit_ids and edge.get("target") not in toolkit_ids]
    direct_pairs = {
        (edge.get("source"), edge.get("target"))
        for edge in migrated_edges
        if (edge.get("targetHandle") or edge.get("target_handle")) == "input-tools"
    }
    warnings: List[str] = []
    for toolkit_id in sorted(toolkit_ids):
        agents = list(dict.fromkeys(outgoing[toolkit_id]))
        services = list(dict.fromkeys(incoming[toolkit_id]))
        if not agents:
            warnings.append(f"Removed legacy androidTool '{toolkit_id}' without a valid destination agent")
            continue
        for service_id in services:
            for agent_id in agents:
                if (service_id, agent_id) in direct_pairs:
                    continue
                migrated_edges.append(
                    {
                        "id": f"migrated-{service_id}-{agent_id}",
                        "source": service_id,
                        "target": agent_id,
                        "sourceHandle": "output-main",
                        "targetHandle": "input-tools",
                    }
                )
                direct_pairs.add((service_id, agent_id))

    migrated_nodes = [dict(node) for node in nodes if node.get("id") not in toolkit_ids]
    for toolkit_id in toolkit_ids:
        params.pop(toolkit_id, None)
    return migrated_nodes, migrated_edges, params, warnings


def normalize_workflow_graph(
    workflow_id: str,
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    node_parameters: Optional[Mapping[str, Dict[str, Any]]] = None,
    *,
    canonicalize_ids: bool = True,
) -> WorkflowGraphNormalization:
    """Normalize a graph to Context V2.

    The transform is idempotent and preserves unknown edges for validation.
    It performs the following ordered stages:

    1. canonicalize legacy handle field names;
    2. migrate the retired Android toolkit;
    3. convert every legacy Memory continuity edge into a Context edge plus
       an ordinary Memory tool edge;
    4. pair every plugin declaring ``requires_context`` with a fresh Context;
    5. assign canonical node IDs and return aliases/import receipts.

    Raw legacy Markdown is returned only in ``state_imports``.  It is never
    copied into the Context node or workflow graph.
    """
    normalized_edges = normalize_edge_handles(edges or [])
    normalized_nodes, normalized_edges, params, warnings = normalize_legacy_android_toolkit(
        nodes or [],
        normalized_edges,
        node_parameters,
    )
    normalized_nodes = [dict(node) for node in normalized_nodes]
    normalized_edges = [dict(edge) for edge in normalized_edges]
    params = {str(node_id): dict(value or {}) for node_id, value in params.items()}

    node_by_id = {str(node.get("id")): node for node in normalized_nodes if node.get("id") is not None}
    context_ids = {node_id for node_id, node in node_by_id.items() if node.get("type") == "context"}

    legacy_by_agent: Dict[str, List[str]] = {}
    retained_edges: List[Dict[str, Any]] = []
    for edge in normalized_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        source_node = node_by_id.get(source) or {}
        if source_node.get("type") == "simpleMemory" and _target_handle(edge) == "input-memory" and target in node_by_id:
            legacy_by_agent.setdefault(target, []).append(source)
            continue
        retained_edges.append(edge)
    normalized_edges = retained_edges

    # Reconnect legacy Simple Memory nodes as normal tools. A shared Memory
    # remains shared, while each destination agent receives isolated Context.
    tool_pairs = {
        (str(edge.get("source") or ""), str(edge.get("target") or "")) for edge in normalized_edges if _target_handle(edge) == "input-tools"
    }
    for agent_id, memory_ids in sorted(legacy_by_agent.items()):
        for memory_id in dict.fromkeys(memory_ids):
            if (memory_id, agent_id) in tool_pairs:
                continue
            normalized_edges.append(
                {
                    "id": _edge_id("memory-tool", memory_id, agent_id),
                    "source": memory_id,
                    "target": agent_id,
                    "sourceHandle": "output-tool",
                    "targetHandle": "input-tools",
                }
            )
            tool_pairs.add((memory_id, agent_id))

    required_agent_ids = {node_id for node_id, node in node_by_id.items() if _requires_context(str(node.get("type") or ""))}
    # A legacy edge is itself an explicit continuity declaration. It remains
    # migratable even if an optional plugin is not installed on this host.
    required_agent_ids.update(legacy_by_agent)

    # Resolve system companion ownership before repairing edges. This prevents
    # a stale Context whose owner was deleted from being silently adopted by a
    # different agent merely because an unrelated edge still points at it.
    context_targets: Dict[str, List[str]] = {}
    for edge in normalized_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if (
            source in context_ids
            and _source_handle(edge) == "output-context"
            and _target_handle(edge) == "input-context"
            and target in required_agent_ids
        ):
            context_targets.setdefault(source, []).append(target)

    declared_owners: Dict[str, str] = {}
    resolved_system_owners: Dict[str, str] = {}
    orphaned_system_contexts: set[str] = set()
    for context_id in sorted(context_ids):
        context_node = node_by_id[context_id]
        data = dict(context_node.get("data") or {})
        if data.get("systemManaged") is not True:
            continue
        declared_owner = str(data.get("agentNodeId") or "")
        if declared_owner:
            if declared_owner not in required_agent_ids:
                orphaned_system_contexts.add(context_id)
                warnings.append(f"Removed orphaned system Context {context_id!r}")
                continue
            declared_owners[context_id] = declared_owner
            resolved_system_owners[context_id] = declared_owner
            continue

        inferred = sorted(set(context_targets.get(context_id, [])))
        if len(inferred) == 1:
            resolved_system_owners[context_id] = inferred[0]
        elif not inferred:
            orphaned_system_contexts.add(context_id)
            warnings.append(f"Removed orphaned system Context {context_id!r}")
        # A shared system Context without ownership metadata is ambiguous.
        # Preserve it so the validation boundary rejects the graph instead of
        # guessing which agent owns provider state.

    # If retries or an older client produced two system companions for one
    # agent, retain one deterministically and archive the rest after save.
    contexts_by_owner: Dict[str, List[str]] = {}
    for context_id, owner_id in resolved_system_owners.items():
        if context_id not in orphaned_system_contexts:
            contexts_by_owner.setdefault(owner_id, []).append(context_id)
    for owner_id, candidates in sorted(contexts_by_owner.items()):
        if len(candidates) < 2:
            continue
        keeper = min(
            candidates,
            key=lambda context_id: (
                context_id not in declared_owners,
                owner_id not in context_targets.get(context_id, []),
                context_id,
            ),
        )
        for context_id in sorted(candidates):
            if context_id == keeper:
                continue
            orphaned_system_contexts.add(context_id)
            resolved_system_owners.pop(context_id, None)
            warnings.append(f"Removed duplicate system Context {context_id!r} for agent {owner_id!r}; retained {keeper!r}")

    if orphaned_system_contexts:
        normalized_nodes = [node for node in normalized_nodes if str(node.get("id") or "") not in orphaned_system_contexts]
        normalized_edges = [
            edge
            for edge in normalized_edges
            if str(edge.get("source") or "") not in orphaned_system_contexts
            and str(edge.get("target") or "") not in orphaned_system_contexts
        ]
        for context_id in orphaned_system_contexts:
            params.pop(context_id, None)
            node_by_id.pop(context_id, None)
            context_ids.discard(context_id)

    # A system-owned Context may only point at its recorded owner. Repair
    # stale/shared system edges, but leave ambiguous user-authored topology
    # untouched so validation can reject it.
    repaired_edges: List[Dict[str, Any]] = []
    owner_pairs: set[tuple[str, str]] = set()
    for edge in normalized_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        owner_id = resolved_system_owners.get(source)
        is_context_edge = _source_handle(edge) == "output-context" and _target_handle(edge) == "input-context"
        if owner_id and is_context_edge:
            if target != owner_id:
                warnings.append(f"Removed stale system Context edge {source!r} -> {target!r}; owner is {owner_id!r}")
                continue
            owner_pairs.add((source, target))
        repaired_edges.append(edge)
    normalized_edges = repaired_edges
    for context_id, owner_id in sorted(resolved_system_owners.items()):
        if (context_id, owner_id) in owner_pairs:
            continue
        normalized_edges.append(
            {
                "id": _edge_id("context", context_id, owner_id),
                "source": context_id,
                "target": owner_id,
                "sourceHandle": "output-context",
                "targetHandle": "input-context",
                "data": {"systemManaged": True},
            }
        )

    existing_contexts: Dict[str, List[str]] = {}
    for edge in normalized_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in context_ids and _source_handle(edge) == "output-context" and _target_handle(edge) == "input-context":
            existing_contexts.setdefault(target, []).append(source)

    # A required system edge removed in the editor is repaired by the
    # backend. The companion records its owner as non-runtime UI metadata so
    # repair does not rely on geometry or agent type-name heuristics.
    existing_pairs = {(source, target) for target, sources in existing_contexts.items() for source in sources}
    for context_id in sorted(context_ids):
        context_node = node_by_id[context_id]
        data = context_node.get("data") or {}
        owner_id = str(data.get("agentNodeId") or "")
        if (
            data.get("systemManaged") is True
            and owner_id in node_by_id
            and _requires_context(str((node_by_id.get(owner_id) or {}).get("type") or ""))
            and (context_id, owner_id) not in existing_pairs
        ):
            normalized_edges.append(
                {
                    "id": _edge_id("context", context_id, owner_id),
                    "source": context_id,
                    "target": owner_id,
                    "sourceHandle": "output-context",
                    "targetHandle": "input-context",
                    "data": {"systemManaged": True},
                }
            )
            existing_contexts.setdefault(owner_id, []).append(context_id)
            existing_pairs.add((context_id, owner_id))

    occupied_ids = set(node_by_id)
    for ordinal, agent_id in enumerate(sorted(required_agent_ids), start=1):
        if existing_contexts.get(agent_id):
            continue
        temporary_id = f"__context__:{ordinal}:{agent_id}"
        suffix = 1
        while temporary_id in occupied_ids:
            suffix += 1
            temporary_id = f"__context__:{ordinal}:{agent_id}:{suffix}"
        occupied_ids.add(temporary_id)
        context_node = _context_node_for(node_by_id[agent_id], temporary_id)
        normalized_nodes.append(context_node)
        node_by_id[temporary_id] = context_node
        context_ids.add(temporary_id)
        existing_contexts[agent_id] = [temporary_id]
        normalized_edges.append(
            {
                "id": _edge_id("context", temporary_id, agent_id),
                "source": temporary_id,
                "target": agent_id,
                "sourceHandle": "output-context",
                "targetHandle": "input-context",
                "data": {"systemManaged": True},
            }
        )

    # Persist the ownership marker for backend edge repair/cascade semantics.
    # It is UI metadata only; runtime state remains in AgentContextStore.
    agents_for_context: Dict[str, List[str]] = {}
    for agent_id, sources in existing_contexts.items():
        for source_id in dict.fromkeys(sources):
            agents_for_context.setdefault(source_id, []).append(agent_id)
    for agent_id, sources in existing_contexts.items():
        unique_sources = list(dict.fromkeys(sources))
        if len(unique_sources) != 1:
            continue
        source_id = unique_sources[0]
        if len(set(agents_for_context.get(source_id, []))) != 1:
            continue
        context_node = node_by_id.get(source_id)
        if context_node is None:
            continue
        context_node["data"] = {
            **dict(context_node.get("data") or {}),
            "systemManaged": True,
            "agentNodeId": agent_id,
        }

    # Cascade-delete an orphaned system companion when its owning agent is
    # absent. User-authored/unowned Context nodes are left for validation.
    claimed_context_ids = {source_id for sources in existing_contexts.values() for source_id in sources}
    unclaimed_system_contexts = {
        node_id
        for node_id, node in node_by_id.items()
        if node.get("type") == "context" and (node.get("data") or {}).get("systemManaged") is True and node_id not in claimed_context_ids
    }
    if unclaimed_system_contexts:
        normalized_nodes = [node for node in normalized_nodes if str(node.get("id") or "") not in unclaimed_system_contexts]
        normalized_edges = [
            edge
            for edge in normalized_edges
            if str(edge.get("source") or "") not in unclaimed_system_contexts
            and str(edge.get("target") or "") not in unclaimed_system_contexts
        ]
        for context_id in unclaimed_system_contexts:
            params.pop(context_id, None)
            warnings.append(f"Removed orphaned system Context {context_id!r}")

    aliases: Dict[str, str] = {}
    if canonicalize_ids and workflow_id:
        from services.workflow_naming import canonicalize_node_ids

        normalized_nodes, normalized_edges, aliases = canonicalize_node_ids(
            str(workflow_id),
            normalized_nodes,
            normalized_edges,
        )
        params = {aliases.get(node_id, node_id): value for node_id, value in params.items()}

    # Resolve agent -> Context after canonicalization so persistence can use
    # stable IDs.  Receipts are operation-id friendly and safe to replay.
    context_for_agent: Dict[str, str] = {}
    canonical_node_by_id = {str(node.get("id")): node for node in normalized_nodes if node.get("id") is not None}
    for edge in normalized_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if (
            (canonical_node_by_id.get(source) or {}).get("type") == "context"
            and _source_handle(edge) == "output-context"
            and _target_handle(edge) == "input-context"
        ):
            context_for_agent[target] = source

    state_imports: List[Dict[str, Any]] = []
    for legacy_agent_id, memory_ids in sorted(legacy_by_agent.items()):
        agent_id = aliases.get(legacy_agent_id, legacy_agent_id)
        context_id = context_for_agent.get(agent_id)
        if not context_id:
            continue
        for legacy_memory_id in dict.fromkeys(memory_ids):
            memory_id = aliases.get(legacy_memory_id, legacy_memory_id)
            legacy = params.get(memory_id, {})
            markdown = legacy.get("memory_content")
            bindings = {
                key: legacy[key]
                for key in (
                    "last_session_id",
                    "vertex_interaction_id",
                    "vertex_environment_id",
                )
                if legacy.get(key)
            }
            if markdown or bindings:
                state_imports.append(
                    {
                        "operation_id": (f"legacy-context-import:{workflow_id}:{context_id}:{memory_id}"),
                        "workflow_id": str(workflow_id),
                        "context_node_id": context_id,
                        "agent_node_id": agent_id,
                        "legacy_memory_node_id": memory_id,
                        "event_type": "legacy_partial",
                        "markdown": str(markdown) if markdown else None,
                        "legacy_session_id": str(legacy.get("session_id") or agent_id),
                        "provider_bindings": bindings,
                    }
                )
            warnings.append(
                f"Migrated legacy Simple Memory edge {memory_id!r} -> {agent_id!r}; process-local vector entries could not be imported"
            )

    return WorkflowGraphNormalization(
        nodes=normalized_nodes,
        edges=normalized_edges,
        node_parameters=params,
        warnings=warnings,
        aliases=aliases,
        state_imports=state_imports,
    )


__all__ = [
    "WORKFLOW_GRAPH_VERSION",
    "WorkflowGraphNormalization",
    "normalize_edge_handles",
    "normalize_legacy_android_toolkit",
    "normalize_workflow_graph",
]
