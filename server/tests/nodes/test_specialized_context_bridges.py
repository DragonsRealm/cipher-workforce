"""Thin-node wiring tests for specialized Context V2 backends."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.cli_agent.protocol import BatchResult, SessionResult
from tests.nodes._mocks import patched_broadcaster, patched_container

pytestmark = pytest.mark.node_contract


def _node(node_id: str, node_type: str) -> dict:
    return {"id": node_id, "type": node_type, "data": {"label": node_type}}


def _context_graph(agent_id: str, agent_type: str) -> tuple[list, list]:
    context_id = "wf:context:1"
    return (
        [_node(agent_id, agent_type), _node(context_id, "context")],
        [
            {
                "source": context_id,
                "target": agent_id,
                "sourceHandle": "output-context",
                "targetHandle": "input-context",
            }
        ],
    )


@pytest.mark.asyncio
async def test_rlm_node_passes_context_descriptor_without_legacy_memory(
    harness,
):
    nodes, edges = _context_graph("rlm-1", "rlm_agent")
    rlm = MagicMock()
    rlm.execute = AsyncMock(
        return_value={"success": True, "result": {"response": "done"}}
    )
    harness.ai_service.rlm_service = rlm

    with patched_container(auth_api_keys={}), patched_broadcaster():
        result = await harness.execute(
            "rlm_agent",
            {"prompt": "solve", "provider": "openai", "model": "gpt-4o"},
            node_id="rlm-1",
            context=harness.build_context(
                nodes=nodes,
                edges=edges,
                extra={"generation": 1},
            ),
        )

    harness.assert_envelope(result, success=True)
    kwargs = rlm.execute.await_args.kwargs
    assert kwargs["memory_data"] is None
    assert kwargs["context_data"]["kind"] == "context"
    assert kwargs["context_data"]["context_node_id"] == "wf:context:1"


@pytest.mark.asyncio
async def test_claude_node_routes_context_separately_from_v1_memory(harness):
    nodes, edges = _context_graph("claude-1", "claude_code_agent")
    service = MagicMock()
    service.run_batch = AsyncMock(
        return_value=BatchResult(
            tasks=[
                SessionResult(
                    task_id="t1",
                    provider="claude",
                    response="done",
                    session_id="uuid-1",
                    success=True,
                )
            ],
            n_tasks=1,
            n_succeeded=1,
            provider="claude",
        )
    )

    with (
        patched_container(auth_api_keys={}),
        patched_broadcaster(),
        patch(
            "services.cli_agent.service.get_ai_cli_service",
            return_value=service,
        ),
    ):
        result = await harness.execute(
            "claude_code_agent",
            {"prompt": "edit"},
            node_id="claude-1",
            context=harness.build_context(
                nodes=nodes,
                edges=edges,
                extra={"generation": 1},
            ),
        )

    harness.assert_envelope(result, success=True)
    kwargs = service.run_batch.await_args.kwargs
    assert kwargs["connected_memory"] is None
    assert kwargs["connected_context"]["kind"] == "context"
    task = list(kwargs["tasks"])[0]
    assert task.continue_session is False
    assert result["result"]["session_id"] is None
    assert result["result"]["tasks"][0]["session_id"] is None


@pytest.mark.asyncio
async def test_codex_node_routes_observable_context_without_resume(harness):
    nodes, edges = _context_graph("codex-1", "codex_agent")
    service = MagicMock()
    service.run_batch = AsyncMock(
        return_value=BatchResult(
            tasks=[
                SessionResult(
                    task_id="t1",
                    provider="codex",
                    response="done",
                    session_id=None,
                    success=True,
                )
            ],
            n_tasks=1,
            n_succeeded=1,
            provider="codex",
        )
    )

    with (
        patched_container(auth_api_keys={}),
        patched_broadcaster(),
        patch(
            "services.cli_agent.service.get_ai_cli_service",
            return_value=service,
        ),
    ):
        result = await harness.execute(
            "codex_agent",
            {"prompt": "inspect"},
            node_id="codex-1",
            context=harness.build_context(
                nodes=nodes,
                edges=edges,
                extra={"generation": 1},
            ),
        )

    harness.assert_envelope(result, success=True)
    kwargs = service.run_batch.await_args.kwargs
    assert kwargs["connected_context"]["kind"] == "context"
    assert kwargs.get("connected_memory") is None
    assert result["result"].get("session_id") is None
    assert result["result"]["tasks"][0]["session_id"] is None


@pytest.mark.asyncio
async def test_vertex_uses_context_binding_and_redacts_ids_from_output(harness):
    nodes, edges = _context_graph("vertex-1", "vertex_managed_agent")
    bridge = MagicMock()
    bridge.load_binding = AsyncMock(
        return_value={
            "interaction_id": "ix-prev",
            "environment_id": "env-prev",
        }
    )
    bridge.append_observable = AsyncMock()
    bridge.bind_provider = AsyncMock()
    bridge.augment_prompt = MagicMock(side_effect=lambda prompt: prompt)
    interaction = SimpleNamespace(
        id="ix-next",
        status="completed",
        environment_id="env-next",
        output_text="answer",
        steps=[],
        usage=None,
    )

    with (
        patched_container(auth_api_keys={}),
        patched_broadcaster() as broadcaster,
        patch(
            "services.cli_agent.context_bridge."
            "SpecializedAgentContextBridge.resolve",
            AsyncMock(return_value=bridge),
        ),
        patch(
            "nodes.agent.vertex_managed_agent.build_genai_client",
            return_value=MagicMock(),
        ),
        patch(
            "nodes.agent.vertex_managed_agent.stream_interaction",
            AsyncMock(return_value=interaction),
        ) as stream,
    ):
        broadcaster.broadcast_agent_progress = AsyncMock()
        broadcaster.broadcast = AsyncMock()
        result = await harness.execute(
            "vertex_managed_agent",
            {
                "prompt": "continue",
                "project_id": "project",
                "visualize_cloud_tools": False,
            },
            node_id="vertex-1",
            context=harness.build_context(
                nodes=nodes,
                edges=edges,
                extra={"generation": 1},
            ),
        )

    harness.assert_envelope(result, success=True)
    _, kwargs = stream.call_args
    assert kwargs["previous_interaction_id"] == "ix-prev"
    assert kwargs["environment"] == "env-prev"
    assert result["result"]["interaction_id"] is None
    assert result["result"]["environment_id"] is None
    bridge.bind_provider.assert_awaited_once()
    binding = bridge.bind_provider.await_args.args[1]
    assert binding["interaction_id"] == "ix-next"
    assert binding["environment_id"] == "env-next"
