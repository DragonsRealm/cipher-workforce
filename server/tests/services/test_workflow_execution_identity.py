"""Execution-correlation contracts shared by workflow entry points."""

from __future__ import annotations

import asyncio
import time
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.workflow import WorkflowService


@pytest.mark.asyncio
async def test_sequential_workflow_uses_one_generated_id_for_every_node():
    service = WorkflowService.__new__(WorkflowService)
    service._settings = {"stop_on_error": False}

    observed: list[tuple[str, str]] = []

    async def fake_execute_node(self, **kwargs):
        observed.append((kwargs["execution_id"], kwargs["user_id"]))
        return {"success": True, "result": {"node_id": kwargs["node_id"]}}

    service.execute_node = MethodType(fake_execute_node, service)
    nodes = [
        {"id": "start-1", "type": "start", "data": {}},
        {"id": "console-1", "type": "console", "data": {}},
    ]
    edges = [{"source": "start-1", "target": "console-1"}]

    result = await service._execute_sequential(
        nodes,
        edges,
        "session-1",
        None,
        time.time(),
        "workflow-1",
    )

    assert result["execution_id"]
    assert observed == [
        (result["execution_id"], "owner"),
        (result["execution_id"], "owner"),
    ]


@pytest.mark.asyncio
async def test_sequential_workflow_preserves_authenticated_user():
    service = WorkflowService.__new__(WorkflowService)
    service._settings = {"stop_on_error": False}
    observed: list[str] = []

    async def fake_execute_node(self, **kwargs):
        observed.append(kwargs["user_id"])
        return {"success": True, "result": {}}

    service.execute_node = MethodType(fake_execute_node, service)
    await service._execute_sequential(
        [{"id": "start-1", "type": "start", "data": {}}],
        [],
        "session-1",
        None,
        time.time(),
        "workflow-1",
        "account-7",
    )

    assert observed == ["account-7"]


@pytest.mark.asyncio
async def test_parallel_workflows_isolate_authenticated_users():
    service = WorkflowService.__new__(WorkflowService)
    observed: dict[str, str] = {}

    async def fake_execute_node(self, **kwargs):
        observed[kwargs["workflow_id"]] = kwargs["user_id"]
        return {"success": True, "result": {}}

    service.execute_node = MethodType(fake_execute_node, service)

    class _Executor:
        async def execute_workflow(
            self,
            *,
            workflow_id,
            nodes,
            edges,
            session_id,
            enable_caching,
        ):
            await asyncio.sleep(0)
            await service._execute_node_adapter(
                "node-1",
                "console",
                {},
                {
                    "workflow_id": workflow_id,
                    "session_id": session_id,
                    "nodes": nodes,
                    "edges": edges,
                },
            )
            return {
                "success": True,
                "execution_id": workflow_id,
                "nodes_executed": ["node-1"],
                "outputs": {},
                "errors": [],
            }

    executor = _Executor()
    service._get_workflow_executor = lambda _callback=None: executor

    await asyncio.gather(
        service._execute_parallel(
            [],
            [],
            "session-a",
            None,
            time.time(),
            "workflow-a",
            "account-a",
        ),
        service._execute_parallel(
            [],
            [],
            "session-b",
            None,
            time.time(),
            "workflow-b",
            "account-b",
        ),
    )

    assert observed == {
        "workflow-a": "account-a",
        "workflow-b": "account-b",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supplied", "expected"),
    [("account-9", "account-9"), ("", "owner")],
)
async def test_execute_node_builds_user_scoped_context(
    supplied,
    expected,
):
    service = WorkflowService.__new__(WorkflowService)
    service._get_workspace_dir = lambda _slug: "workspace"
    service._node_executor = SimpleNamespace(
        execute=AsyncMock(return_value={"success": True}),
    )
    service._param_resolver = SimpleNamespace(resolve=AsyncMock())

    await service.execute_node(
        node_id="node-1",
        node_type="console",
        parameters={},
        workflow_slug="workflow",
        user_id=supplied,
    )

    context = service._node_executor.execute.await_args.kwargs["context"]
    assert context["user_id"] == expected


@pytest.mark.asyncio
async def test_temporal_workflow_preserves_authenticated_user():
    service = WorkflowService.__new__(WorkflowService)
    service._resolve_workflow_slug = AsyncMock(return_value="workflow")
    service._temporal_executor = SimpleNamespace(
        execute_workflow=AsyncMock(
            return_value={
                "success": True,
                "execution_id": "run-1",
                "nodes_executed": [],
                "outputs": {},
                "errors": [],
            }
        )
    )

    await service._execute_temporal(
        [],
        [],
        "session",
        None,
        time.time(),
        "workflow-1",
        2,
        1,
        "account-11",
    )

    kwargs = service._temporal_executor.execute_workflow.await_args.kwargs
    assert kwargs["user_id"] == "account-11"


@pytest.mark.asyncio
async def test_deploy_workflow_preserves_authenticated_user():
    service = WorkflowService.__new__(WorkflowService)
    manager = SimpleNamespace(
        deploy=AsyncMock(return_value={"success": True}),
    )
    service._get_deployment_manager = lambda: manager

    await service.deploy_workflow(
        [],
        [],
        workflow_id="workflow-1",
        graph_version=2,
        generation=1,
        user_id="account-12",
    )

    assert manager.deploy.await_args.kwargs["user_id"] == "account-12"
