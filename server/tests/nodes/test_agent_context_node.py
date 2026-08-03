from __future__ import annotations

import pytest
from pydantic import ValidationError

from nodes.context import (
    AgentContextNode,
    AgentContextParams,
)
from nodes._visuals import get_plugin_icon_path, get_plugin_meta
from services.plugin.context import NodeContext
from services.plugin.edge_walker import collect_agent_connections
from services.ws_handler_registry import get_ws_handlers


def test_context_node_declares_only_policy_and_dedicated_handle():
    schema = AgentContextParams.model_json_schema()
    assert set(schema["properties"]) == {
        "compaction_mode",
        "trigger_ratio",
        "context_window_override",
        "exact_tail_retention_count",
    }
    forbidden = {
        "journal",
        "messages",
        "checkpoint",
        "provider_binding",
        "payload_ref",
        "epoch",
        "revision",
    }
    assert forbidden.isdisjoint(schema["properties"])
    assert AgentContextNode.handles == (
        {
            "name": "output-context",
            "kind": "output",
            "position": "top",
            "label": "Context",
            "role": "context",
        },
    )
    assert AgentContextNode.ui_hints["systemManaged"] is True
    assert AgentContextNode.ui_hints["isContextPanel"] is True
    assert AgentContextNode.ui_hints["hideInputSection"] is True
    assert AgentContextNode.ui_hints["hideOutputSection"] is True


def test_context_node_ships_backend_visual_assets():
    icon_path = get_plugin_icon_path("context")
    assert icon_path is not None
    assert icon_path.name == "icon.svg"
    assert icon_path.parent.name == "context"
    assert icon_path.is_file()
    assert (
        AgentContextNode._metadata_dict()["icon"]
        == "/api/schemas/nodes/context/icon"
    )
    assert get_plugin_meta("context", "color") == "#6272a4"


def test_context_policy_rejects_runtime_payloads():
    with pytest.raises(ValidationError):
        AgentContextParams.model_validate(
            {
                "compaction_mode": "auto",
                "journal": [{"role": "user", "content": "secret"}],
            }
        )


def test_context_panel_handlers_self_register():
    handlers = get_ws_handlers()
    assert {
        "get_agent_context",
        "clear_agent_context",
        "fork_agent_context",
        "export_agent_context",
    }.issubset(handlers)


@pytest.mark.asyncio
async def test_context_node_operation_is_policy_only_and_metadata_only():
    assert set(AgentContextNode._operations) == {"policy"}
    result = await AgentContextNode().policy(
        NodeContext(node_id="context-1", node_type="context"),
        AgentContextParams(),
    )
    assert result.model_dump() == {
        "configured": True,
        "compaction_mode": "auto",
        "trigger_ratio": 0.8,
        "exact_tail_retention_count": 8,
    }


@pytest.mark.asyncio
async def test_context_connection_activates_only_for_admitted_generation():
    class Database:
        async def get_node_parameters(self, node_id):
            assert node_id == "context-1"
            return {"compaction_mode": "auto"}

    graph = {
        "workflow_id": "workflow-1",
        "execution_id": "execution-1",
        "nodes": [
            {"id": "agent-1", "type": "aiAgent"},
            {"id": "context-1", "type": "context"},
        ],
        "edges": [
            {
                "source": "context-1",
                "target": "agent-1",
                "sourceHandle": "output-context",
                "targetHandle": "input-context",
            }
        ],
    }
    generation_zero = await collect_agent_connections(
        "agent-1",
        graph,
        Database(),
    )
    assert generation_zero[0] is None

    live = await collect_agent_connections(
        "agent-1",
        {**graph, "generation": 1},
        Database(),
    )
    assert live[0]["kind"] == "context"
    assert live[0]["generation"] == 1


class TestContextPluginOwnsItsDescriptor:
    """The framework must not carry Context-plugin knowledge.

    ``context`` is a declarative UI/policy surface onto an agent's context
    scope (RFC-0002 section 3). The edge walker knows only that something
    may be registered for ``input-context``; the descriptor's shape, the
    node's parameters and the thread-selection rules belong to the plugin.
    """

    def test_edge_walker_has_no_context_plugin_knowledge(self):
        import inspect

        from services.plugin import edge_walker

        source = inspect.getsource(edge_walker)
        # The framework may reference the handle name; what it must NOT do
        # is know the descriptor's shape or branch on the node type.
        assert '"kind": "context"' not in source, (
            "edge_walker hardcodes the Context descriptor shape; it should "
            "call the registered builder instead"
        )
        assert 'source_node.get("type") == "context"' not in source
        assert '"policy"' not in source, "node parameters belong to the plugin"

    def test_plugin_registers_the_builder(self):
        import nodes  # noqa: F401 - triggers plugin discovery
        from services.plugin.edge_walker import get_agent_context_builder

        builder = get_agent_context_builder()
        assert builder is not None
        assert builder.__module__.startswith("nodes.context")

    @pytest.mark.asyncio
    async def test_generation_zero_contributes_no_context(self):
        from nodes.context._descriptor import build_agent_context_descriptor

        class _Db:
            async def get_node_parameters(self, _node_id):
                return {}

        # Generation zero is a migration/import artifact, not a live scope.
        assert await build_agent_context_descriptor("ctx-1", {"generation": 0}, _Db()) is None

    @pytest.mark.asyncio
    async def test_delegated_task_does_not_inherit_parent_session(self):
        from nodes.context._descriptor import build_agent_context_descriptor

        class _Db:
            async def get_node_parameters(self, _node_id):
                return {"compaction_mode": "auto"}

        got = await build_agent_context_descriptor(
            "ctx-1",
            {"generation": 2, "session_id": "parent-session", "delegated_task_id": "task-9"},
            _Db(),
        )
        # Sharing the parent's session would collapse every subagent onto
        # one thread; the delegated task is the isolation boundary.
        assert got["session_id"] is None
        assert got["delegated_task_id"] == "task-9"
        assert got["policy"] == {"compaction_mode": "auto"}
