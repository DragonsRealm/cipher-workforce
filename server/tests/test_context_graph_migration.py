from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from services.workflow_migrations import normalize_workflow_graph


def _node(node_id: str, node_type: str, x: int = 0):
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": x, "y": 20},
        "data": {"label": node_id},
    }


def test_legacy_memory_becomes_isolated_context_and_shared_tool(monkeypatch):
    monkeypatch.setattr(
        "services.workflow_migrations._requires_context",
        lambda node_type: node_type == "agent",
    )
    nodes = [
        _node("memory", "simpleMemory"),
        _node("first", "agent", 400),
        _node("second", "agent", 800),
    ]
    edges = [
        {
            "source": "memory",
            "target": "first",
            "sourceHandle": "output-memory",
            "targetHandle": "input-memory",
        },
        {
            "source": "memory",
            "target": "second",
            "source_handle": "output-memory",
            "target_handle": "input-memory",
        },
    ]
    result = normalize_workflow_graph(
        "42",
        nodes,
        edges,
        {"memory": {"memory_content": "## Human\nhello"}},
    )

    contexts = [node for node in result.nodes if node["type"] == "context"]
    assert [node["id"] for node in contexts] == ["42:context:1", "42:context:2"]
    context_edges = [edge for edge in result.edges if edge.get("targetHandle") == "input-context"]
    assert len(context_edges) == 2
    assert len({edge["source"] for edge in context_edges}) == 2
    tool_edges = [edge for edge in result.edges if edge.get("targetHandle") == "input-tools"]
    assert len(tool_edges) == 2
    assert {edge["source"] for edge in tool_edges} == {"42:simpleMemory:1"}
    assert len(result.state_imports) == 2
    assert all(item["event_type"] == "legacy_partial" for item in result.state_imports)
    assert all("memory_content" not in node.get("data", {}) for node in contexts)


def test_context_graph_normalization_is_idempotent(monkeypatch):
    monkeypatch.setattr(
        "services.workflow_migrations._requires_context",
        lambda node_type: node_type == "agent",
    )
    first = normalize_workflow_graph(
        "7",
        [_node("agent", "agent")],
        [],
    )
    second = normalize_workflow_graph(
        "7",
        first.nodes,
        first.edges,
        first.node_parameters,
    )
    assert second.nodes == first.nodes
    assert second.edges == first.edges
    assert second.aliases == {}


def test_backend_repairs_required_edge_and_removes_deleted_agent_context(
    monkeypatch,
):
    monkeypatch.setattr(
        "services.workflow_migrations._requires_context",
        lambda node_type: node_type == "agent",
    )
    nodes = [
        {
            **_node("agent", "agent"),
            "data": {"label": "Agent"},
        },
        {
            **_node("ctx", "context"),
            "data": {
                "label": "Context",
                "systemManaged": True,
                "agentNodeId": "agent",
            },
        },
    ]
    repaired = normalize_workflow_graph("3", nodes, [])
    assert [edge for edge in repaired.edges if edge.get("targetHandle") == "input-context"]
    context = next(node for node in repaired.nodes if node["type"] == "context")
    assert context["data"]["agentNodeId"] == "3:agent:1"

    deleted = normalize_workflow_graph(
        "3",
        [node for node in repaired.nodes if node["type"] != "agent"],
        [],
    )
    assert not [node for node in deleted.nodes if node["type"] == "context"]


def test_normalization_removes_duplicate_system_context_deterministically(
    monkeypatch,
):
    monkeypatch.setattr(
        "services.workflow_migrations._requires_context",
        lambda node_type: node_type == "agent",
    )
    nodes = [
        _node("agent", "agent"),
        {
            **_node("ctx-a", "context"),
            "data": {
                "label": "ctx-a",
                "systemManaged": True,
                "agentNodeId": "agent",
            },
        },
        {
            **_node("ctx-b", "context"),
            "data": {
                "label": "ctx-b",
                "systemManaged": True,
                "agentNodeId": "agent",
            },
        },
    ]
    edges = [
        {
            "source": "ctx-b",
            "target": "agent",
            "sourceHandle": "output-context",
            "targetHandle": "input-context",
        }
    ]

    result = normalize_workflow_graph("5", nodes, edges)

    contexts = [node for node in result.nodes if node["type"] == "context"]
    assert len(contexts) == 1
    assert contexts[0]["data"]["label"] == "ctx-b"
    assert contexts[0]["data"]["agentNodeId"] == "5:agent:1"
    assert any("Removed duplicate system Context" in warning for warning in result.warnings)


def test_stale_system_context_owner_is_removed_instead_of_reassigned(
    monkeypatch,
):
    monkeypatch.setattr(
        "services.workflow_migrations._requires_context",
        lambda node_type: node_type == "agent",
    )
    nodes = [
        _node("agent", "agent"),
        {
            **_node("ctx", "context"),
            "data": {
                "label": "stale",
                "systemManaged": True,
                "agentNodeId": "deleted-agent",
            },
        },
    ]
    edges = [
        {
            "source": "ctx",
            "target": "agent",
            "sourceHandle": "output-context",
            "targetHandle": "input-context",
        }
    ]

    result = normalize_workflow_graph("6", nodes, edges)

    contexts = [node for node in result.nodes if node["type"] == "context"]
    assert len(contexts) == 1
    assert contexts[0]["data"]["label"] == "Context"
    assert contexts[0]["data"]["agentNodeId"] == "6:agent:1"
    assert any("Removed orphaned system Context 'ctx'" == warning for warning in result.warnings)


def test_system_context_shared_edge_is_repaired_to_recorded_owner(
    monkeypatch,
):
    monkeypatch.setattr(
        "services.workflow_migrations._requires_context",
        lambda node_type: node_type == "agent",
    )
    nodes = [
        _node("a", "agent"),
        _node("b", "agent"),
        {
            **_node("ctx", "context"),
            "data": {
                "label": "existing",
                "systemManaged": True,
                "agentNodeId": "a",
            },
        },
    ]
    edges = [
        {
            "source": "ctx",
            "target": target,
            "sourceHandle": "output-context",
            "targetHandle": "input-context",
        }
        for target in ("a", "b")
    ]

    result = normalize_workflow_graph("8", nodes, edges)

    context_edges = [edge for edge in result.edges if edge.get("targetHandle") == "input-context"]
    assert len(context_edges) == 2
    assert len({edge["source"] for edge in context_edges}) == 2
    existing = next(node for node in result.nodes if node["type"] == "context" and node["data"]["label"] == "existing")
    assert existing["data"]["agentNodeId"] == "8:agent:1"
    assert next(edge["target"] for edge in context_edges if edge["source"] == existing["id"]) == "8:agent:1"


class _Params(BaseModel):
    pass


class _ContextCapable:
    Params = _Params
    credentials = ()
    requires_context = True
    ui_hints = {}


class _ContextNode:
    Params = _Params
    credentials = ()
    requires_context = False
    ui_hints = {}


@pytest.mark.asyncio
async def test_validator_rejects_missing_multiple_and_shared_contexts(monkeypatch):
    from services.workflow_validator import validate_workflow

    monkeypatch.setattr(
        "services.workflow_validator.get_node_class",
        lambda node_type: {
            "agent": _ContextCapable,
            "context": _ContextNode,
        }.get(node_type),
    )
    nodes = [
        _node("a", "agent"),
        _node("b", "agent"),
        _node("c1", "context"),
        _node("c2", "context"),
    ]
    edges = [
        {
            "source": "c1",
            "target": "a",
            "sourceHandle": "output-context",
            "targetHandle": "input-context",
        },
        {
            "source": "c2",
            "target": "a",
            "sourceHandle": "output-context",
            "targetHandle": "input-context",
        },
        {
            "source": "c1",
            "target": "b",
            "sourceHandle": "output-context",
            "targetHandle": "input-context",
        },
    ]
    report = await validate_workflow(nodes, edges)
    codes = {issue["code"] for issue in report["errors"]}
    assert "MULTIPLE_CONTEXTS" in codes
    assert "SHARED_CONTEXT" in codes

    missing = await validate_workflow([_node("a", "agent")], [])
    assert "MISSING_CONTEXT" in {issue["code"] for issue in missing["errors"]}


@pytest.mark.asyncio
async def test_save_rejects_ambiguous_shared_context_before_persistence(
    monkeypatch,
):
    from services.workflow_storage import handlers

    monkeypatch.setattr(
        "services.workflow_migrations._requires_context",
        lambda node_type: node_type == "agent",
    )
    monkeypatch.setattr(
        "services.workflow_validator.get_node_class",
        lambda node_type: {
            "agent": _ContextCapable,
            "context": _ContextNode,
        }.get(node_type),
    )
    database = type("Database", (), {})()
    database.allocate_workflow_id = AsyncMock(return_value="1")
    database.get_workflow = AsyncMock(return_value=None)
    database.list_workflow_slugs = AsyncMock(return_value=[])
    database.get_node_parameters = AsyncMock(return_value={})
    database.save_workflow = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers.container, "database", lambda: database)

    result = await handlers.handle_save_workflow(
        {
            "workflow_id": "new",
            "name": "Invalid Context",
            "data": {
                "nodes": [
                    _node("a", "agent"),
                    _node("b", "agent"),
                    {
                        **_node("ctx", "context"),
                        "data": {"label": "Context", "systemManaged": True},
                    },
                ],
                "edges": [
                    {
                        "source": "ctx",
                        "target": target,
                        "sourceHandle": "output-context",
                        "targetHandle": "input-context",
                    }
                    for target in ("a", "b")
                ],
            },
        },
        websocket=None,
    )

    assert result["success"] is False
    assert result["error"] == "invalid_context_topology"
    assert "SHARED_CONTEXT" in {issue["code"] for issue in result["validation_errors"]}
    database.save_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_uses_authenticated_owner_and_ignores_client_owner(
    monkeypatch,
):
    from services.workflow_storage import handlers

    database = type("Database", (), {})()
    database.allocate_workflow_id = AsyncMock(return_value="1")
    database.get_workflow = AsyncMock(return_value=None)
    database.list_workflow_slugs = AsyncMock(return_value=[])
    database.get_node_parameters = AsyncMock(return_value={})
    database.save_workflow = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers.container, "database", lambda: database)

    result = await handlers.handle_save_workflow(
        {
            "workflow_id": "new",
            "name": "Owned",
            "data": {
                "nodes": [],
                "edges": [],
                "owner_id": "client-forgery",
            },
        },
        websocket=SimpleNamespace(state=SimpleNamespace(user_id="authenticated-user")),
    )

    assert result["success"] is True
    assert result["data"]["owner_id"] == "authenticated-user"
    assert database.save_workflow.await_args.kwargs["data"]["owner_id"] == ("authenticated-user")


@pytest.mark.asyncio
async def test_save_cannot_replace_backend_owned_context(
    monkeypatch,
):
    from services.workflow_storage import handlers

    monkeypatch.setattr(
        "services.workflow_migrations._requires_context",
        lambda node_type: node_type == "agent",
    )
    monkeypatch.setattr(
        "services.workflow_validator.get_node_class",
        lambda node_type: {
            "agent": _ContextCapable,
            "context": _ContextNode,
        }.get(node_type),
    )
    existing = SimpleNamespace(
        id="1",
        name="Owned",
        slug="owned",
        description=None,
        data={
            "graphVersion": 2,
            "owner_id": "owner",
            "nodes": [
                _node("1:agent:1", "agent"),
                {
                    **_node("1:context:1", "context"),
                    "data": {
                        "label": "Context",
                        "systemManaged": True,
                        "agentNodeId": "1:agent:1",
                    },
                },
            ],
            "edges": [
                {
                    "source": "1:context:1",
                    "target": "1:agent:1",
                    "sourceHandle": "output-context",
                    "targetHandle": "input-context",
                }
            ],
        },
    )
    database = type("Database", (), {})()
    database.get_workflow = AsyncMock(return_value=existing)
    database.get_node_parameters = AsyncMock(return_value={})
    database.save_workflow = AsyncMock(return_value=True)
    monkeypatch.setattr(handlers.container, "database", lambda: database)

    result = await handlers.handle_save_workflow(
        {
            "workflow_id": "1",
            "name": "Owned",
            "data": {
                "nodes": [
                    _node("1:agent:1", "agent"),
                    {
                        **_node("1:context:99", "context"),
                        "data": {
                            "label": "Forged",
                            "systemManaged": True,
                            "agentNodeId": "1:agent:1",
                        },
                    },
                ],
                "edges": [
                    {
                        "source": "1:context:99",
                        "target": "1:agent:1",
                        "sourceHandle": "output-context",
                        "targetHandle": "input-context",
                    }
                ],
                "owner_id": "forged-owner",
            },
        },
        websocket=None,
    )

    contexts = [
        node
        for node in result["data"]["nodes"]
        if node["type"] == "context"
    ]
    assert [node["id"] for node in contexts] == ["1:context:1"]
    assert contexts[0]["data"]["agentNodeId"] == "1:agent:1"
    assert result["data"]["owner_id"] == "owner"
    assert any(
        "Ignored untrusted Context companion '1:context:99'"
        == warning
        for warning in result["migration_warnings"]
    )


def test_save_owner_resolution_preserves_existing_backend_owner():
    from services.workflow_storage.handlers import _trusted_owner_id

    assert (
        _trusted_owner_id(
            websocket=None,
            existing=SimpleNamespace(data={"owner_id": "stored-owner"}),
        )
        == "stored-owner"
    )


@pytest.mark.asyncio
async def test_workflow_delete_archives_context_before_graph(
    monkeypatch,
):
    from services.workflow_storage import handlers

    database = type("Database", (), {})()
    database.get_workflow = AsyncMock(
        return_value=SimpleNamespace(
            data={
                "nodes": [
                    _node("agent", "agent"),
                    _node("ctx", "context"),
                ]
            }
        )
    )
    database.delete_workflow = AsyncMock(return_value=True)
    store = SimpleNamespace(archive_context=AsyncMock(return_value=[]))
    monkeypatch.setattr(handlers.container, "database", lambda: database)
    monkeypatch.setattr(
        "services.agent_context.AgentContextStore",
        lambda _: store,
    )

    result = await handlers.handle_delete_workflow(
        {"workflow_id": "12"},
        websocket=None,
    )

    assert result == {
        "success": True,
        "workflow_id": "12",
        "contexts_archived": 1,
        "context_archives_pending": 0,
    }
    store.archive_context.assert_awaited_once_with(
        workflow_id="12",
        context_node_id="ctx",
        generation=None,
        operation_id="workflow-deleted:12:ctx",
    )
    database.delete_workflow.assert_awaited_once_with("12")


@pytest.mark.asyncio
async def test_rest_delete_uses_shared_context_lifecycle_path(
    monkeypatch,
):
    from routers import database as database_router

    shared_delete = AsyncMock(
        return_value={
            "success": True,
            "workflow_id": "12",
            "contexts_archived": 1,
            "context_archives_pending": 0,
        }
    )
    monkeypatch.setattr(
        database_router,
        "delete_workflow_with_context_archival",
        shared_delete,
    )
    database = object()

    result = await database_router.delete_workflow(
        "12",
        database=database,
    )

    assert result["success"] is True
    shared_delete.assert_awaited_once_with(database, "12")
