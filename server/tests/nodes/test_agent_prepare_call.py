"""Contracts for the shared agent pre-dispatch flow (prepare_agent_call)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _connections(*, task_data=None, tool_data=None):
    return (
        None,  # memory_data
        None,  # skill_data
        tool_data,
        None,  # input_data
        task_data,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "error"])
async def test_completion_firing_keeps_tools(status):
    """The taskTrigger prompt tells a lead to list/accept/reassign via the
    Task Manager tool — stripping tools on exactly that firing made the
    lead unable to act on any completion (it could only answer in prose,
    which read as the lead forgetting its plan)."""
    import nodes.agent._inline as inline

    tools = [
        {"node_id": "tm-1", "node_type": "taskManager", "label": "Task Manager"},
        {"node_id": "search-1", "node_type": "braveSearch", "label": "Search"},
    ]
    task = {"task_id": "task-1", "status": status, "result": "done"}

    with patch.object(
        inline,
        "collect_agent_connections",
        AsyncMock(return_value=_connections(task_data=task, tool_data=tools)),
    ), patch.object(
        inline,
        "collect_teammate_connections",
        AsyncMock(return_value=[]),
    ), patch(
        "services.status_broadcaster.get_status_broadcaster",
        MagicMock(),
    ):
        prepared = await inline.prepare_agent_call(
            node_id="lead-1",
            node_type="orchestrator_agent",
            parameters={"prompt": "review"},
            context={},
            database=object(),
        )

    assert prepared["tool_data"] == tools
    # The task context is still injected ahead of the original prompt.
    assert "task-1" in prepared["parameters"]["prompt"]
    assert prepared["parameters"]["prompt"].endswith("review")
