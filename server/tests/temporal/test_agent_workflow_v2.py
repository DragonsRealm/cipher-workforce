"""Focused contracts for Context-backed Temporal agent orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import BaseModel
import pytest


def _ref(revision: int = 1) -> dict:
    return {
        "workflow_id": "workflow-1",
        "context_node_id": "context-1",
        "generation": 3,
        "thread_id": "execution:run-1",
        "epoch": 1,
        "revision": revision,
    }










def _settings(*, enabled: bool = True):
    return SimpleNamespace(
        temporal_agent_workflow_enabled=enabled,
        temporal_per_type_dispatch=True,
        temporal_worker_pool_enabled=False,
    )


def test_new_generation_routes_to_v2_but_missing_metadata_stays_v1():
    from services.temporal.workflow import MachinaWorkflow

    instance = MachinaWorkflow()
    with patch("core.config.Settings", side_effect=lambda: _settings()):
        v2 = instance._resolve_dispatch(
            "aiAgent",
            graph_version=2,
            generation=4,
            context_v2_enabled=True,
        )
        missing_generation = instance._resolve_dispatch(
            "aiAgent",
            graph_version=2,
            generation=0,
            context_v2_enabled=True,
        )
        missing_version = instance._resolve_dispatch(
            "aiAgent",
            graph_version=0,
            generation=4,
            context_v2_enabled=True,
        )

    assert v2 == {
        "kind": "child_workflow",
        "name": "AgentWorkflow",
    }
    assert missing_generation["name"] == "AgentWorkflow"
    assert missing_version["name"] == "AgentWorkflow"


def test_delegated_thread_identity_does_not_inherit_parent_session():
    from services.temporal.agent_activities import _thread_inputs

    value = _thread_inputs(
        {
            "session_id": "parent-chat",
            "team_task_id": "task-42",
            "execution_id": "run-1",
        }
    )
    assert value == {
        "session_id": None,
        "delegated_task_id": "task-42",
        "execution_id": "run-1",
    }


def test_control_data_scope_is_not_treated_as_explicit_chat_session():
    from services.temporal.agent_activities import _thread_inputs

    value = _thread_inputs(
        {
            "session_id": "scope-1",
            "data_scope_id": "scope-1",
            "execution_id": "generation-1",
            "context_execution_id": "firing-42",
        }
    )
    assert value == {
        "session_id": None,
        "delegated_task_id": None,
        "execution_id": "firing-42",
    }


def test_explicit_event_session_beats_execution_thread():
    from services.temporal.agent_activities import _thread_inputs

    value = _thread_inputs(
        {
            "session_id": "scope-1",
            "data_scope_id": "scope-1",
            "context_session_id": "chat-7",
            "context_execution_id": "firing-42",
        }
    )
    assert value == {
        "session_id": "chat-7",
        "delegated_task_id": None,
        "execution_id": "firing-42",
    }


def test_prepare_and_rebind_reject_duplicate_canonical_tool_names():
    from temporalio.exceptions import ApplicationError

    from services.temporal.agent_activities import (
        _validate_unique_tool_names,
    )

    tools = [
        {
            "name": "memory",
            "tool_node_id": "memory-a",
            "node_type": "simpleMemory",
        },
        {
            "name": "memory",
            "tool_node_id": "memory-b",
            "node_type": "simpleMemory",
        },
    ]
    with pytest.raises(ApplicationError) as exc_info:
        _validate_unique_tool_names(tools, phase="hot rebind")

    assert exc_info.value.type == "DuplicateAgentToolName"
    assert "memory-a" in str(exc_info.value)
    assert "memory-b" in str(exc_info.value)


@pytest.mark.asyncio
async def test_tool_input_preserves_supplied_only_semantics(monkeypatch):
    import services.node_registry as registry
    from services.temporal.agent_activities import _validate_tool_args

    class MemoryInput(BaseModel):
        operation: str
        limit: int = 20

    class MemoryNode:
        @classmethod
        def tool_input_model(cls):
            return MemoryInput

    monkeypatch.setattr(
        registry,
        "get_node_class",
        lambda node_type: MemoryNode if node_type == "simpleMemory" else None,
    )

    validated, error = await _validate_tool_args(
        {
            "node_type": "simpleMemory",
            "definition": {
                "parameters": MemoryInput.model_json_schema(),
            },
        },
        {
            "args": {"operation": "list"},
        },
    )

    assert error is None
    assert validated == {"operation": "list"}


@pytest.mark.asyncio
async def test_plugin_tool_activity_loads_arguments_by_reference(monkeypatch):
    import services.temporal.agent_activities as module

    store = object()
    monkeypatch.setattr(module, "_context_store", lambda: store)
    monkeypatch.setattr(
        module,
        "_runtime_config",
        AsyncMock(
            return_value={
                "protocol": "agent-context-v2",
                "prepared": {
                    "tools": [
                        {
                            "name": "memory",
                            "tool_node_id": "memory-1",
                            "node_type": "simpleMemory",
                            "version": 2,
                        }
                    ]
                },
            }
        ),
    )
    execute = AsyncMock(return_value={"result_ref": "sha256:result"})
    monkeypatch.setattr(module, "_execute_tool_and_append", execute)
    pending = {
        "call_index": 1,
        "call_id": "call-1",
        "name": "memory",
        "node_id": "memory-1",
        "node_type": "simpleMemory",
        "version": 2,
        "task_queue": "memory-heavy",
        "known": True,
    }

    result = await module.execute_tool_activity(
        {
            "protocol": "agent-context-tool-v2",
            "context_ref": _ref(2),
            "runtime_config_ref": "sha256:runtime",
            "response_ref": "sha256:response",
            "pending_tool": pending,
            "operation_id": "tool-operation",
            "iteration": 1,
        },
        expected_node_type="simpleMemory",
        expected_version=2,
    )

    assert result == {"result_ref": "sha256:result"}
    kwargs = execute.await_args.kwargs
    assert kwargs["store"] is store
    assert kwargs["response_ref"] == "sha256:response"
    assert kwargs["pending"] == pending
    assert "args" not in kwargs


def _event(sequence, event_type, *, wire=None, payload_ref=None):
    from models.agent_context import AgentContextEvent

    return AgentContextEvent(
        sequence=sequence,
        event_type=event_type,
        message_wire_v2=wire,
        payload_ref=payload_ref,
        operation_id=f"operation:{sequence}",
        provider="openai",
        previous_hash="0" * 64,
        payload_hash=str(sequence) * 64,
    )






@pytest.mark.asyncio
async def test_lifetime_usage_includes_compaction_calls():
    from services.temporal.agent_activities import (
        _context_ref,
        _lifetime_usage,
    )

    class Store:
        async def load_journal_page(self, *args, **kwargs):
            return (
                [
                    SimpleNamespace(
                        event_type="message.assistant",
                        payload_ref="sha256:assistant",
                        operation_id="temporal:run:iteration:1:assistant",
                    ),
                    SimpleNamespace(
                        event_type="context.compacted",
                        payload_ref="sha256:compaction",
                        operation_id="temporal:run:iteration:1:compact:event",
                    ),
                ],
                None,
            )

        async def get_blob(self, payload_ref):
            return {
                "usage": (
                    {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_tokens": 12,
                    }
                    if payload_ref == "sha256:assistant"
                    else {
                        "input_tokens": 4,
                        "output_tokens": 1,
                        "total_tokens": 5,
                    }
                )
            }

    usage = await _lifetime_usage(
        Store(),
        _context_ref(_ref()),
        "temporal:run",
    )

    assert usage == {
        "input_tokens": 14,
        "output_tokens": 3,
        "total_tokens": 17,
    }
