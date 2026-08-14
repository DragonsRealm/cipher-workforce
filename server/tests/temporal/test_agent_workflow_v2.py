"""Focused contracts for Context-backed Temporal agent orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


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
