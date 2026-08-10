"""MachinaWorkflow must honour conditional edges.

Before this, ``server/services/temporal/`` contained no edge-condition
evaluation at all -- every reference to "condition" in that package was
``workflow.wait_condition`` for cooperative pause or trigger waiting. A user
could set a condition in the editor, see it render as an edge label, and have
it do nothing the moment execution routed through Temporal. There was no
error and no warning; both branches simply ran.

The in-process ``WorkflowExecutor`` has always evaluated them
(``_evaluate_incoming_conditions``), so this is parity, not a new feature.
Two semantics are deliberately mirrored rather than improved:

* **OR-any** across a node's conditional incoming edges.
* **Skipping is not transitive** -- a skipped node counts as "completed" for
  dependency purposes (``get_completed_nodes`` includes ``SKIPPED``), so an
  unconditional downstream node still runs.

Runs the real ``MachinaWorkflow.run`` body with ``start_activity`` /
``logger`` monkeypatched, same harness as test_machina_workflow_loop.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import nodes  # noqa: F401 -- populate plugin registry


@pytest.fixture(autouse=True)
def _patch_workflow_logger(monkeypatch):
    from temporalio import workflow as temporal_workflow

    monkeypatch.setattr(temporal_workflow, "logger", MagicMock())


def _branching_graph():
    """py-1 emits ``result.answer = 42``; two gated branches read it."""
    nodes_ = [
        {"id": "start-1", "type": "start", "data": {"label": "Start"}},
        {"id": "py-1", "type": "pythonExecutor", "data": {"label": "Source"}},
        {"id": "match-1", "type": "pythonExecutor", "data": {"label": "Matching"}},
        {"id": "miss-1", "type": "pythonExecutor", "data": {"label": "Non-matching"}},
    ]
    edges = [
        {"id": "e0", "source": "start-1", "target": "py-1", "targetHandle": "input-main"},
        {
            "id": "e1",
            "source": "py-1",
            "target": "match-1",
            "targetHandle": "input-main",
            "data": {"condition": {"field": "result.answer", "operator": "eq", "value": 42}},
        },
        {
            "id": "e2",
            "source": "py-1",
            "target": "miss-1",
            "targetHandle": "input-main",
            "data": {"condition": {"field": "result.answer", "operator": "eq", "value": 99}},
        },
    ]
    return nodes_, edges


def _install_fakes(monkeypatch):
    """Patch out activity dispatch; return the list scheduling appends to."""
    from temporalio import workflow as temporal_workflow

    from services.temporal.workflow import MachinaWorkflow

    scheduled: list[str] = []

    def fake_start_activity(name, **kwargs):
        ctx = kwargs["args"][0]
        scheduled.append(ctx["node_id"])
        fut = asyncio.get_event_loop().create_future()
        fut.set_result({"success": True, "node_id": ctx["node_id"], "result": {"answer": 42}})
        return fut

    async def fake_execute_activity(*args, **kwargs):
        return None

    monkeypatch.setattr(temporal_workflow, "start_activity", fake_start_activity)
    monkeypatch.setattr(temporal_workflow, "execute_activity", fake_execute_activity)
    # ``**_kwargs`` absorbs the routing/graph-version arguments the real
    # resolver takes; dispatch resolution is out of scope here (test_dispatch.py
    # owns it) and pinning the exact signature would make this test fail on
    # every unrelated change to it.
    monkeypatch.setattr(
        MachinaWorkflow,
        "_resolve_dispatch",
        lambda self, node_type, **_kwargs: {
            "kind": "activity",
            "name": f"node.{node_type}.v1",
            "queue": None,
        },
    )
    return scheduled


async def _run(graph, execution_id):
    from services.temporal.workflow import MachinaWorkflow

    nodes_, edges = graph
    return await asyncio.wait_for(
        MachinaWorkflow().run(
            {
                "nodes": nodes_,
                "edges": edges,
                "session_id": "test",
                "workflow_id": "wf-cond-test",
                "execution_id": execution_id,
            }
        ),
        timeout=5.0,
    )


class TestConditionalEdgesGateScheduling:
    @pytest.mark.asyncio
    async def test_only_the_matching_branch_is_scheduled(self, monkeypatch):
        from temporalio import workflow as temporal_workflow

        monkeypatch.setattr(temporal_workflow, "patched", lambda _patch_id: True)
        scheduled = _install_fakes(monkeypatch)

        result = await _run(_branching_graph(), "wf-cond-run1")

        assert "match-1" in scheduled, "the branch whose condition matched must run"
        assert "miss-1" not in scheduled, (
            "the branch whose condition did not match must be skipped. Pre-fix "
            "Temporal scheduled BOTH branches because it never looked at "
            "edge.data.condition at all."
        )
        assert result["success"] is True
        # Skipped nodes still resolve, so the graph drains rather than hanging.
        assert "miss-1" in result["execution_trace"]
        assert "miss-1" not in result["outputs"]

    @pytest.mark.asyncio
    async def test_unpatched_history_still_schedules_both(self, monkeypatch):
        """Determinism guard: a history recorded before the patch replays with
        every node scheduled unconditionally, exactly as it was written."""
        from temporalio import workflow as temporal_workflow

        monkeypatch.setattr(temporal_workflow, "patched", lambda _patch_id: False)
        scheduled = _install_fakes(monkeypatch)

        await _run(_branching_graph(), "wf-cond-run2")

        assert "match-1" in scheduled
        assert "miss-1" in scheduled, (
            "with the patch gate closed the old command sequence must be "
            "reproduced verbatim, or in-flight workflows fail replay"
        )


class TestSkipIsNotTransitive:
    @pytest.mark.asyncio
    async def test_unconditional_downstream_of_a_skipped_node_still_runs(self, monkeypatch):
        """Mirrors the in-process semantic, where SKIPPED counts as completed
        in ``ExecutionContext.get_completed_nodes``."""
        from temporalio import workflow as temporal_workflow

        monkeypatch.setattr(temporal_workflow, "patched", lambda _patch_id: True)
        scheduled = _install_fakes(monkeypatch)

        nodes_, edges = _branching_graph()
        nodes_.append({"id": "tail-1", "type": "pythonExecutor", "data": {"label": "Tail"}})
        edges.append({"id": "e3", "source": "miss-1", "target": "tail-1", "targetHandle": "input-main"})

        result = await _run((nodes_, edges), "wf-cond-run3")

        assert "miss-1" not in scheduled
        assert "tail-1" in scheduled, (
            "a skipped node must not wedge its unconditional downstream -- it "
            "resolves as completed so the graph drains"
        )
        assert result["success"] is True


class TestUnconditionalGraphsUnaffected:
    @pytest.mark.asyncio
    async def test_graph_without_conditions_schedules_everything(self, monkeypatch):
        from temporalio import workflow as temporal_workflow

        monkeypatch.setattr(temporal_workflow, "patched", lambda _patch_id: True)
        scheduled = _install_fakes(monkeypatch)

        nodes_, edges = _branching_graph()
        for edge in edges:
            edge.pop("data", None)

        await _run((nodes_, edges), "wf-cond-run4")

        assert "match-1" in scheduled
        assert "miss-1" in scheduled
