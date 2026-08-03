"""ToolInput and locked-schema security contracts."""

from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from nodes.tool.current_time_tool import CurrentTimeToolNode
from nodes.tool.simple_memory import (
    SimpleMemoryNode,
    SimpleMemoryParams,
    SimpleMemoryToolInput,
)
from services.agent_runtime import AgentToolSpec, _validated_tool_args
from services.ai import AIService
from services.llm.protocol import ToolCall, ToolDef
from services.plugin import ActionNode, NodeContext, Operation, ToolNode
from services.ws_handler_registry import get_ws_handlers


class _ConfiguredToolParams(BaseModel):
    endpoint: str
    model_config = ConfigDict(extra="forbid")


class _ConfiguredToolOutput(BaseModel):
    endpoint: str
    query: str


class _DefaultedToolInput(BaseModel):
    query: str
    configured_limit: int = 25


class _ConfiguredToolNode(ToolNode, abstract=True):
    type = "_testConfiguredToolInput"
    display_name = "Configured ToolInput Test"
    group = ("tool",)
    description = "Test-only ToolInput boundary"
    handles = (
        {
            "name": "output-tool",
            "kind": "output",
            "position": "top",
            "label": "Tool",
            "role": "tools",
        },
    )
    ui_hints = {"isToolPanel": True}
    Params = _ConfiguredToolParams
    ToolInput = _ConfiguredToolParams
    Output = _ConfiguredToolOutput
    server_controlled_fields = frozenset({"endpoint"})

    @Operation("run")
    async def run(
        self, ctx: NodeContext, params: _ConfiguredToolParams
    ) -> _ConfiguredToolOutput:
        return _ConfiguredToolOutput(
            endpoint=params.endpoint,
            query=str(ctx.raw.get("query") or ""),
        )


class _DualPurposeActionNode(ActionNode, abstract=True):
    type = "_testDualPurposeAction"
    usable_as_tool = True
    Params = _ConfiguredToolParams
    Output = _ConfiguredToolOutput

    @Operation("run")
    async def run(
        self, ctx: NodeContext, params: _ConfiguredToolParams
    ) -> _ConfiguredToolOutput:
        return _ConfiguredToolOutput(
            endpoint=params.endpoint,
            query=str(ctx.raw.get("query") or ""),
        )


def test_tool_input_defaults_to_params():
    assert CurrentTimeToolNode.ToolInput is CurrentTimeToolNode.Params
    assert CurrentTimeToolNode.tool_input_model() is CurrentTimeToolNode.Params


def test_native_validation_does_not_materialize_omitted_defaults():
    spec = AgentToolSpec(
        definition=ToolDef(
            name="defaulted",
            description="test",
            parameters=_DefaultedToolInput.model_json_schema(),
        ),
        args_schema=_DefaultedToolInput,
    )
    args, error = _validated_tool_args(
        ToolCall(id="call-1", name="defaulted", args={"query": "hello"}),
        {"defaulted": spec},
    )
    assert error is None
    assert args == {"query": "hello"}


def test_simple_memory_has_split_locked_schema_and_canonical_name():
    assert SimpleMemoryNode.Params is SimpleMemoryParams
    assert SimpleMemoryNode.ToolInput is SimpleMemoryToolInput
    assert SimpleMemoryNode.tool_schema_locked is True
    assert SimpleMemoryNode.tool_name == "memory"
    schema = SimpleMemoryNode.as_tool_schema()["parameters"]
    assert "operation" in schema["properties"]
    assert "reset_policy" not in schema["properties"]


def test_stored_custom_schema_cannot_replace_memory_tool_input():
    malicious = {
        "db_schema_config": {
            "fields": {
                "workflow_id": {
                    "type": "string",
                    "required": True,
                }
            }
        }
    }
    resolved = AIService._get_tool_schema(
        object(), "simpleMemory", malicious
    )
    assert resolved is SimpleMemoryToolInput
    assert "workflow_id" not in resolved.model_fields


def test_memory_panel_handlers_are_plugin_registered():
    handlers = get_ws_handlers()
    assert {
        "list_memory_items",
        "get_memory_item",
        "remember_memory",
        "update_memory_item",
        "forget_memory_item",
        "clear_memory_items",
    }.issubset(handlers)


async def test_memory_builder_ignores_stored_name_and_schema():
    class _Database:
        async def get_tool_schema(self, _node_id):
            return {
                "tool_name": "steal_scope",
                "tool_description": "untrusted",
                "schema_config": {
                    "fields": {
                        "workflow_id": {
                            "type": "string",
                            "required": True,
                        }
                    }
                },
            }

    service = AIService.__new__(AIService)
    service.database = _Database()
    tool, _config = await service._build_tool_from_node(
        {
            "node_id": "memory-node",
            "node_type": "simpleMemory",
            "parameters": {"reset_policy": "preserve"},
            "label": "Memory",
        }
    )
    assert tool.definition.name == "memory"
    assert "operation" in tool.definition.parameters["properties"]
    assert "workflow_id" not in tool.definition.parameters["properties"]


async def test_model_arguments_cannot_override_server_controlled_config():
    node = _ConfiguredToolNode()
    context = NodeContext(
        node_id="configured-tool",
        node_type=_ConfiguredToolNode.type,
        raw={"query": "hello"},
    )
    result = await node.execute_as_tool(
        {"endpoint": "https://attacker.invalid"},
        {"endpoint": "https://trusted.internal"},
        context,
    )
    assert result["endpoint"] == "https://trusted.internal"


async def test_action_node_tools_keep_their_existing_merge_contract():
    node = _DualPurposeActionNode()
    result = await node.execute_as_tool(
        {"endpoint": "https://model.example"},
        {"endpoint": "https://configured.example"},
        NodeContext(
            node_id="dual-purpose",
            node_type=_DualPurposeActionNode.type,
            raw={"query": "hello"},
        ),
    )
    assert result == {
        "endpoint": "https://model.example",
        "query": "hello",
    }


async def test_memory_panel_scope_is_resolved_from_workflow_and_auth(
    monkeypatch,
):
    from nodes.tool.simple_memory import _handlers

    class _Database:
        async def get_workflow(self, workflow_id):
            assert workflow_id == "workflow-1"
            return SimpleNamespace(
                data={
                    "owner_id": "authenticated-user",
                    "nodes": [
                        {"id": "memory-1", "type": "simpleMemory"}
                    ]
                }
            )

    monkeypatch.setattr(_handlers, "get_database", lambda: _Database())
    websocket = SimpleNamespace(
        state=SimpleNamespace(user_id="authenticated-user"),
        scope={},
    )
    _store, scope = await _handlers._resolve_store_and_scope(
        {
            "workflow_id": "workflow-1",
            "memory_node_id": "memory-1",
            "namespace_id": "client-chosen-namespace",
            "user_id": "client-chosen-user",
        },
        websocket,
    )
    assert scope.workflow_id == "workflow-1"
    assert scope.memory_node_id == "memory-1"
    assert scope.owner_id == "authenticated-user"
    assert scope.namespace_id != "client-chosen-namespace"
