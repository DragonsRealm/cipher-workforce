from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.workflow_sanitizer import (
    sanitize_runtime_payload,
    sanitize_workflow_graph,
)


def test_server_sanitizer_removes_legacy_and_context_runtime_payloads():
    graph = {
        "graphVersion": 2,
        "nodes": [
            {
                "id": "ctx",
                "type": "context",
                "data": {
                    "label": "Context",
                    "systemManaged": True,
                    "agentNodeId": "agent",
                    "contextJournal": [{"role": "user", "content": "secret"}],
                    "providerBindings": {"session": "uuid"},
                },
            },
            {
                "id": "memory",
                "type": "simpleMemory",
                "data": {
                    "label": "Memory",
                    "parameters": {
                        "reset_policy": "preserve",
                        "memory_content": "secret transcript",
                        "last_session_id": "uuid",
                    },
                },
            },
        ],
        "edges": [],
    }

    cleaned = sanitize_workflow_graph(graph)
    context_data = cleaned["nodes"][0]["data"]
    assert context_data == {
        "label": "Context",
        "systemManaged": True,
        "agentNodeId": "agent",
    }
    assert cleaned["nodes"][1]["data"] == {"label": "Memory"}


def test_sanitizer_preserves_unrelated_provider_and_file_references():
    value = {
        "provider": "openai",
        "payload_ref": {"kind": "file", "path": "uploads/report.pdf"},
        "nested": {"thought_signature": "secret", "title": "safe"},
    }
    assert sanitize_runtime_payload(value) == {
        "provider": "openai",
        "payload_ref": {"kind": "file", "path": "uploads/report.pdf"},
        "nested": {"title": "safe"},
    }


def test_workflow_graph_uses_ui_schema_allowlists():
    graph = {
        "graphVersion": 2,
        "owner_id": "authenticated-user",
        "contextJournal": [{"content": "root secret"}],
        "nodes": [
            {
                "id": "agent",
                "type": "aiAgent",
                "position": {"x": 10, "y": 20, "runtime": "drop"},
                "selected": True,
                "data": {
                    "label": "Agent",
                    "disabled": False,
                    "condition": {"field": "ok", "operator": "equals", "value": True},
                    "parameters": {"api_key": "secret", "model": "gpt"},
                    "activeReplay": {"messages": ["secret"]},
                    "output": {"content": "secret"},
                },
            }
        ],
        "edges": [
            {
                "id": "edge",
                "source": "agent",
                "target": "agent",
                "sourceHandle": "output-main",
                "targetHandle": "input-main",
                "selected": True,
                "data": {
                    "label": "when ready",
                    "condition": {
                        "field": "status",
                        "operator": "equals",
                        "value": "ready",
                    },
                    "providerBindings": {"session": "secret"},
                },
            }
        ],
        "runtime_data": {"journal": "secret"},
    }

    cleaned = sanitize_workflow_graph(graph)

    assert set(cleaned) == {"graphVersion", "owner_id", "nodes", "edges"}
    assert cleaned["owner_id"] == "authenticated-user"
    assert cleaned["nodes"] == [
        {
            "id": "agent",
            "type": "aiAgent",
            "position": {"x": 10, "y": 20},
            "data": {
                "label": "Agent",
                "disabled": False,
                "condition": {
                    "field": "ok",
                    "operator": "equals",
                    "value": True,
                },
            },
        }
    ]
    assert cleaned["edges"] == [
        {
            "id": "edge",
            "source": "agent",
            "target": "agent",
            "sourceHandle": "output-main",
            "targetHandle": "input-main",
            "data": {
                "label": "when ready",
                "condition": {
                    "field": "status",
                    "operator": "equals",
                    "value": "ready",
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_export_parameter_read_is_authorized_and_server_redacted(
    monkeypatch,
):
    from routers import websocket as websocket_router

    database = SimpleNamespace(
        get_workflow=AsyncMock(
            return_value=SimpleNamespace(
                data={
                    "owner_id": "user-1",
                    "nodes": [{"id": "memory", "type": "simpleMemory"}],
                    "edges": [],
                }
            )
        ),
        get_node_parameters=AsyncMock(
            return_value={
                "reset_policy": "preserve",
                "memory_content": "private transcript",
                "provider_binding": {"session": "secret"},
            }
        ),
    )
    monkeypatch.setattr(
        websocket_router.container,
        "database",
        lambda: database,
    )
    socket = SimpleNamespace(state=SimpleNamespace(user_id="user-1"))
    result = await websocket_router.handle_get_all_node_parameters(
        {
            "workflow_id": "workflow-1",
            "node_ids": ["memory"],
            "purpose": "export",
        },
        socket,
    )
    assert result["parameters"]["memory"]["parameters"] == {
        "reset_policy": "preserve"
    }

    denied = await websocket_router.handle_get_all_node_parameters(
        {
            "workflow_id": "workflow-1",
            "node_ids": ["memory"],
            "purpose": "export",
        },
        SimpleNamespace(state=SimpleNamespace(user_id="user-2")),
    )
    assert denied == {"success": False, "error": "Workflow access denied"}
