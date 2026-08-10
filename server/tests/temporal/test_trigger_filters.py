"""Deployed triggers must honour the filter configured on the node.

Before this landed, ``filter_params`` was carried in ``listener_data`` and
never read. The ``EventType`` Search Attribute was the only narrowing on the
deployed path, so every event of the right type spawned a run:

  * ``webhookTrigger`` bound to ``/a`` also fired on a POST to ``/b`` --
    all webhooks share one CloudEvents type and are unscoped.
  * ``taskTrigger`` watching one agent fired on every task completion.
  * ``whatsappReceive`` scoped to one group fired on every message.

The canvas-Run path applied these filters, so Run and deploy disagreed
about what the same node does.

Harness note: ``temporal_workflow.patched`` and ``execute_activity`` are
monkeypatched and the workflow methods called directly, same approach as
test_conditional_edges.py.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

import nodes  # noqa: F401 -- populate the plugin registry

from services.temporal.activities import evaluate_trigger_filter_activity
from services.temporal.trigger_listener_workflow import (
    NODE_FILTER_PATCH,
    TriggerListenerWorkflow,
)


pytestmark = pytest.mark.unit


class _SpawnReached(BaseException):
    """Raised by the stubbed activity so a test can prove the gate opened.

    Deliberately a ``BaseException``: the spawn body wraps its graph lookup
    and status broadcasts in ``except Exception`` fallbacks, which would
    swallow an ordinary sentinel and let the test pass for the wrong reason.
    """


def _listener_data(**overrides):
    data = {
        "workflow_id": "wf-1",
        "workflow_slug": "Demo_1",
        "trigger_node_id": "trigger-1",
        "trigger_label": "webhookTrigger",
        "node_type": "webhookTrigger",
        "event_type": "com.opencompany.webhook.received",
        "filter_params": {"path": "hooks/a"},
        "nodes": [{"id": "trigger-1", "type": "webhookTrigger"}],
        "edges": [],
        "session_id": "default",
    }
    data.update(overrides)
    return data


def _event(path="hooks/a"):
    return {
        "id": "evt-1",
        "type": "com.opencompany.webhook.received",
        "data": {"path": path, "body": {}},
    }


class TestFilterActivity:
    """The activity is where the predicate actually runs.

    It lives outside the workflow sandbox because filter builders are in
    plugin folders and reach imports the sandbox forbids.
    """

    async def test_matching_event_is_admitted(self):
        assert await evaluate_trigger_filter_activity(
            {
                "node_type": "webhookTrigger",
                "filter_params": {"path": "hooks/a"},
                "event_data": {"path": "hooks/a"},
            }
        )

    async def test_non_matching_event_is_rejected(self):
        """The whole point: a webhook on another path must not spawn a run."""
        assert not await evaluate_trigger_filter_activity(
            {
                "node_type": "webhookTrigger",
                "filter_params": {"path": "hooks/a"},
                "event_data": {"path": "hooks/b"},
            }
        )

    async def test_predicate_receives_the_data_member_not_the_envelope(self):
        """``event_waiter.dispatch`` unpacks to ``(event_type, data)`` and
        hands filters the inner payload. Passing the whole CloudEvents
        envelope would make every filter silently reject, which is worse
        than the bug being fixed."""
        envelope = _event(path="hooks/a")
        # The envelope has no top-level "path"; only envelope["data"] does.
        assert "path" not in envelope
        assert not await evaluate_trigger_filter_activity(
            {
                "node_type": "webhookTrigger",
                "filter_params": {"path": "hooks/a"},
                "event_data": envelope,
            }
        )
        assert await evaluate_trigger_filter_activity(
            {
                "node_type": "webhookTrigger",
                "filter_params": {"path": "hooks/a"},
                "event_data": envelope["data"],
            }
        )

    async def test_task_trigger_filter_is_applied(self):
        """Not webhook-specific -- any registered builder is honoured."""
        payload = {
            "node_type": "taskTrigger",
            "filter_params": {"agent_name": "alpha"},
        }
        assert await evaluate_trigger_filter_activity(
            {**payload, "event_data": {"agent_name": "alpha", "status": "completed"}}
        )
        assert not await evaluate_trigger_filter_activity(
            {**payload, "event_data": {"agent_name": "beta", "status": "completed"}}
        )

    async def test_unknown_node_type_admits(self):
        """``build_filter`` defaults to accept-all for unregistered types."""
        assert await evaluate_trigger_filter_activity(
            {"node_type": "somethingElse", "filter_params": {"x": 1}, "event_data": {}}
        )

    async def test_non_dict_event_data_admits(self):
        assert await evaluate_trigger_filter_activity(
            {"node_type": "webhookTrigger", "filter_params": {"path": "a"}, "event_data": None}
        )

    async def test_a_raising_builder_fails_open(self, monkeypatch):
        """Over-firing is visible and recoverable; a silently dropped
        trigger event looks like the product is broken."""
        from temporalio import activity as temporal_activity

        monkeypatch.setattr(temporal_activity, "logger", MagicMock())

        def _boom(_node_type, _params):
            raise RuntimeError("builder exploded")

        monkeypatch.setattr("services.event_waiter.build_filter", _boom)
        assert await evaluate_trigger_filter_activity(
            {
                "node_type": "webhookTrigger",
                "filter_params": {"path": "hooks/a"},
                "event_data": {"path": "hooks/zzz"},
            }
        )


class TestSpawnIsGatedAtTheChokePoint:
    """``_spawn_child_run`` is where the gate belongs.

    ``WorkflowControlWorkflow._spawn_push_run`` constructs a
    ``TriggerListenerWorkflow`` purely to call this method, so gating in
    either workflow's own run loop would leave the other path unfiltered.
    """

    def _patch_workflow(self, monkeypatch, *, patched: bool, admits: bool):
        from temporalio import workflow as temporal_workflow

        monkeypatch.setattr(temporal_workflow, "patched", lambda _id: patched)
        monkeypatch.setattr(temporal_workflow, "logger", MagicMock())

        async def _execute_activity(name, *_args, **_kwargs):
            if name == "evaluate_trigger_filter_activity":
                return admits
            # Any other activity means the gate let the event through and
            # the spawn body started.
            raise _SpawnReached(name)

        monkeypatch.setattr(temporal_workflow, "execute_activity", _execute_activity)

    async def test_rejected_event_spawns_nothing(self, monkeypatch):
        self._patch_workflow(monkeypatch, patched=True, admits=False)
        # Returns cleanly: no graph lookup, no status broadcast, no child.
        await TriggerListenerWorkflow()._spawn_child_run(
            _event(path="hooks/b"), _listener_data()
        )

    async def test_admitted_event_proceeds_to_the_spawn_body(self, monkeypatch):
        self._patch_workflow(monkeypatch, patched=True, admits=True)
        with pytest.raises(_SpawnReached):
            await TriggerListenerWorkflow()._spawn_child_run(
                _event(path="hooks/a"), _listener_data()
            )

    async def test_unpatched_history_still_spawns_everything(self, monkeypatch):
        """Replay safety: skipping a spawn changes the recorded command
        sequence, so pre-patch histories must keep the old behaviour even
        for an event the filter would now reject."""
        self._patch_workflow(monkeypatch, patched=False, admits=False)
        with pytest.raises(_SpawnReached):
            await TriggerListenerWorkflow()._spawn_child_run(
                _event(path="hooks/b"), _listener_data()
            )

    async def test_empty_filter_params_skips_the_activity(self, monkeypatch):
        """An unconfigured trigger admits everything without paying for an
        activity round trip per event."""
        from temporalio import workflow as temporal_workflow

        monkeypatch.setattr(temporal_workflow, "patched", lambda _id: True)
        monkeypatch.setattr(temporal_workflow, "logger", MagicMock())

        async def _execute_activity(name, *_args, **_kwargs):
            if name == "evaluate_trigger_filter_activity":
                pytest.fail("filter activity ran for an unconfigured trigger")
            raise _SpawnReached(name)

        monkeypatch.setattr(temporal_workflow, "execute_activity", _execute_activity)
        with pytest.raises(_SpawnReached):
            await TriggerListenerWorkflow()._spawn_child_run(
                _event(), _listener_data(filter_params={})
            )


class TestBothDeployedPathsShareTheGate:
    def test_controller_push_spawn_delegates_to_the_listener(self):
        """Structural: if the controller ever grows its own spawn body, the
        filter stops applying to the modern path and this fails."""
        from services.temporal.workflow_control_workflow import WorkflowControlWorkflow

        source = inspect.getsource(WorkflowControlWorkflow._spawn_push_run)
        assert "TriggerListenerWorkflow" in source
        assert "_spawn_child_run" in source

    def test_the_gate_is_inside_spawn_child_run(self):
        source = inspect.getsource(TriggerListenerWorkflow._spawn_child_run)
        assert NODE_FILTER_PATCH in source or "NODE_FILTER_PATCH" in source
        assert "_event_passes_node_filter" in source

    def test_patch_marker_follows_the_established_naming(self):
        """One standard, matching CONDITIONAL_EDGES_PATCH."""
        from services.temporal.workflow import CONDITIONAL_EDGES_PATCH

        assert CONDITIONAL_EDGES_PATCH.startswith("machina-")
        assert NODE_FILTER_PATCH.startswith("machina-")


class TestFilterActivityIsRegisteredOnWorkers:
    def test_registered_everywhere_the_status_activity_is(self):
        """A workflow calling an unregistered activity fails at run time,
        not import time, so this is asserted structurally."""
        import services.temporal.worker as worker_module

        source = inspect.getsource(worker_module)
        assert source.count("evaluate_trigger_filter_activity") == source.count(
            "broadcast_trigger_status_activity"
        )
