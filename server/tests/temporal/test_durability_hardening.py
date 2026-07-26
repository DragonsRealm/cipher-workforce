"""Months-long durability hardening: contract tests.

The product guarantee: a deployed workflow (running OR paused) survives
backend restarts, is never auto-terminated, and keeps executing for
months. These tests lock the mechanisms that deliver it:

- No lifetime caps on new child workflow starts (replay-patched).
- History-pressure continue-as-new on every long-lived loop (Temporal
  hard-terminates around ~51,200 history events).
- The controller carries its full state (triggers, queued events, dedup
  baseline, control state) across rollovers, so it is addressed by
  workflow id only — never a pinned run id.
- dispatch.emit skips controllers whose deployment has no matching push
  trigger (cross-deployment signal amplification burned every
  controller's history budget on other deployments' traffic).
- The startup terminate sweep's active-control guard shares the
  canonical ACTIVE_STATES set (the old copy omitted "resetting").
- Boot-time reconcile converges rows a crash left transitional and
  re-arms the process-local half of running/paused generations.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.temporal.polling_trigger_workflow import PollingTriggerWorkflow
from services.temporal.trigger_listener_workflow import TriggerListenerWorkflow
from services.temporal.workflow_control_workflow import WorkflowControlWorkflow

SERVER_DIR = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Replay-patch guarding — every new behaviour change that alters recorded
# commands must ride a workflow.patched marker.
# ---------------------------------------------------------------------------


def test_durability_changes_are_replay_patch_guarded():
    from nodes.scheduler.cron_scheduler import _workflow as cron_module
    from services.temporal import agent_workflow as agent_module
    from services.temporal import polling_trigger_workflow as polling_module
    from services.temporal import trigger_listener_workflow as trigger_module
    from services.temporal import workflow as machina_module
    from services.temporal import workflow_control_workflow as control_module
    from services.temporal.agent_workflow import AgentWorkflow, DelegatedTaskWorkflow
    from services.temporal.workflow import MachinaWorkflow
    from nodes.scheduler.cron_scheduler._workflow import CronTriggerWorkflow

    expected = {
        machina_module.UNBOUNDED_LIFETIMES_PATCH: inspect.getsource(MachinaWorkflow.run),
        agent_module.UNBOUNDED_LIFETIMES_PATCH: inspect.getsource(AgentWorkflow.run),
        trigger_module.UNBOUNDED_CHILD_RUNS_PATCH: inspect.getsource(
            TriggerListenerWorkflow._spawn_child_run
        ),
        polling_module.UNBOUNDED_CHILD_RUNS_PATCH: inspect.getsource(
            PollingTriggerWorkflow._spawn_child_run
        ),
        cron_module.UNBOUNDED_CHILD_RUNS_PATCH: inspect.getsource(CronTriggerWorkflow.run),
        trigger_module.HISTORY_BOUNDED_CAN_PATCH: inspect.getsource(
            TriggerListenerWorkflow.run
        ),
        polling_module.HISTORY_BOUNDED_CAN_PATCH: inspect.getsource(
            PollingTriggerWorkflow.run
        ),
        control_module.CONTINUE_AS_NEW_PATCH: inspect.getsource(
            WorkflowControlWorkflow.run
        ),
        trigger_module.BOUNDED_STATUS_BROADCASTS_PATCH: inspect.getsource(
            TriggerListenerWorkflow._spawn_child_run
        ),
    }

    assert set(expected) == {
        "machina-unbounded-lifetimes-v1",
        "agent-unbounded-lifetimes-v1",
        "trigger-unbounded-child-runs-v1",
        "polling-unbounded-child-runs-v1",
        "cron-unbounded-child-runs-v1",
        "trigger-history-bounded-can-v1",
        "polling-history-bounded-can-v1",
        "workflow-control-continue-as-new-v1",
        "trigger-bounded-status-broadcasts-v1",
    }
    # DelegatedTaskWorkflow shares agent_module's marker.
    assert "UNBOUNDED_LIFETIMES_PATCH" in inspect.getsource(DelegatedTaskWorkflow.run)
    for patch_id, source in expected.items():
        assert "workflow.patched(" in source
        assert patch_id.endswith("-v1")


# ---------------------------------------------------------------------------
# Unbounded child runs — new executions must not carry lifetime caps.
# ---------------------------------------------------------------------------


def _listener_data() -> dict:
    return {
        "workflow_id": "wf-1",
        "trigger_node_id": "trigger-1",
        "node_type": "webhookTrigger",
        "event_type": "com.opencompany.webhook.received",
        "filter_params": {},
        "nodes": [{"id": "trigger-1", "type": "webhookTrigger", "data": {}}],
        "edges": [],
        "session_id": "default",
        "tenant_id": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("patched_on", [True, False])
async def test_listener_child_lifetime_caps_are_patch_gated(monkeypatch, patched_on):
    from services.temporal import trigger_listener_workflow as trigger_module

    temporal_workflow = trigger_module.workflow
    monkeypatch.setattr(temporal_workflow, "logger", MagicMock())
    flags = {
        trigger_module.UNBOUNDED_CHILD_RUNS_PATCH: patched_on,
        trigger_module.BOUNDED_STATUS_BROADCASTS_PATCH: patched_on,
    }
    monkeypatch.setattr(
        temporal_workflow, "patched", lambda pid: flags.get(pid, False)
    )
    monkeypatch.setattr(temporal_workflow, "execute_activity", AsyncMock())

    captured: dict = {}

    async def start_child(name, args=None, **kwargs):
        captured.update(kwargs)
        captured["name"] = name

    monkeypatch.setattr(temporal_workflow, "start_child_workflow", start_child)

    listener = TriggerListenerWorkflow()
    await listener._spawn_child_run({"id": "evt-1", "data": {}}, _listener_data())

    assert captured["name"] == "MachinaWorkflow"
    if patched_on:
        assert "execution_timeout" not in captured
        assert "run_timeout" not in captured
    else:
        assert captured["execution_timeout"] == timedelta(hours=1)
        assert captured["run_timeout"] == timedelta(hours=1)


@pytest.mark.asyncio
async def test_listener_broadcast_failure_does_not_kill_the_spawn(monkeypatch):
    """Status broadcasts are cosmetic — a dead broadcaster must neither
    wedge (unlimited retries) nor fail the spawn."""
    from services.temporal import trigger_listener_workflow as trigger_module

    temporal_workflow = trigger_module.workflow
    monkeypatch.setattr(temporal_workflow, "logger", MagicMock())
    monkeypatch.setattr(temporal_workflow, "patched", lambda _pid: True)
    monkeypatch.setattr(
        temporal_workflow,
        "execute_activity",
        AsyncMock(side_effect=RuntimeError("broadcaster down")),
    )
    started = {}

    async def start_child(name, args=None, **kwargs):
        started["id"] = kwargs.get("id")

    monkeypatch.setattr(temporal_workflow, "start_child_workflow", start_child)

    listener = TriggerListenerWorkflow()
    await listener._spawn_child_run({"id": "evt-1", "data": {}}, _listener_data())
    assert started["id"]  # the child run still started


# ---------------------------------------------------------------------------
# History-pressure continue-as-new.
# ---------------------------------------------------------------------------


class _CanFired(Exception):
    pass


def _pressure_info() -> SimpleNamespace:
    return SimpleNamespace(
        is_continue_as_new_suggested=lambda: True,
        get_current_history_length=lambda: 0,
    )


@pytest.mark.asyncio
async def test_polling_rolls_over_under_history_pressure_with_zero_events(monkeypatch):
    """A quiet mailbox burns ~11 history events per cycle with the
    processed counter frozen at 0 — the rollover must key on history
    pressure, not on emitted events."""
    from services.temporal import polling_trigger_workflow as polling_module

    temporal_workflow = polling_module.workflow
    monkeypatch.setattr(temporal_workflow, "logger", MagicMock())
    monkeypatch.setattr(temporal_workflow, "patched", lambda _pid: True)
    monkeypatch.setattr(temporal_workflow, "info", _pressure_info)

    sleeps: list = []

    async def record_sleep(duration):
        sleeps.append(duration)

    monkeypatch.setattr(temporal_workflow, "sleep", record_sleep)
    monkeypatch.setattr(
        temporal_workflow,
        "execute_activity",
        AsyncMock(return_value={"seen_ids": ["prior", "new"], "events": []}),
    )
    captured: dict = {}

    def fake_can(listener_data):
        captured["carried"] = listener_data
        raise _CanFired

    monkeypatch.setattr(temporal_workflow, "continue_as_new", fake_can)

    listener_data = _listener_data()
    listener_data["node_type"] = "googleGmailReceive"
    listener_data["seen_ids"] = ["prior"]  # non-baseline start -> sleeps
    listener_data["filter_params"] = {"poll_interval": 5}  # absurdly hot

    instance = PollingTriggerWorkflow()
    with pytest.raises(_CanFired):
        await instance.run(listener_data)

    assert instance._processed_count == 0
    carried = captured["carried"]
    assert set(carried["seen_ids"]) == {"prior", "new"}
    assert carried["control_paused"] is False
    # The 5s user interval was clamped to the defensive floor.
    assert sleeps == [timedelta(seconds=polling_module._MIN_POLL_INTERVAL_S)]


async def _predicate_wait(predicate):
    while not predicate():
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_listener_rollover_carries_queued_events_and_pause_flag(monkeypatch):
    from services.temporal import trigger_listener_workflow as trigger_module

    temporal_workflow = trigger_module.workflow
    monkeypatch.setattr(temporal_workflow, "logger", MagicMock())
    monkeypatch.setattr(temporal_workflow, "patched", lambda _pid: True)
    monkeypatch.setattr(temporal_workflow, "info", _pressure_info)
    monkeypatch.setattr(temporal_workflow, "wait_condition", _predicate_wait)

    captured: dict = {}

    def fake_can(args=None):
        captured["carried"] = args[0]
        raise _CanFired

    monkeypatch.setattr(temporal_workflow, "continue_as_new", fake_can)

    instance = TriggerListenerWorkflow()
    monkeypatch.setattr(instance, "_spawn_child_run", AsyncMock())

    listener_data = _listener_data()
    # Carried queue from a previous rollover: two events pending.
    listener_data["pending_events"] = [
        {"id": "evt-1", "data": {}},
        {"id": "evt-2", "data": {}},
    ]

    with pytest.raises(_CanFired):
        await instance.run(listener_data)

    carried = captured["carried"]
    # evt-1 processed; the still-queued evt-2 carries instead of dropping.
    assert [e["id"] for e in carried["pending_events"]] == ["evt-2"]
    assert carried["control_paused"] is False
    # Seeded events joined the dedup set so producer retries stay deduped.
    assert "evt-1" in instance._seen_event_ids


# ---------------------------------------------------------------------------
# Controller rollover state machine.
# ---------------------------------------------------------------------------


def _push_spec(listener_id: str = "l-push") -> dict:
    return {
        "listener_id": listener_id,
        "workflow_type": "TriggerListenerWorkflow",
        "event_type": "t.x",
        "event_types": ["t.x"],
        "trigger_node_id": "n-push",
        "listener_args": _listener_data(),
    }


def _poll_spec(listener_id: str = "l-poll") -> dict:
    data = _listener_data()
    data["node_type"] = "googleGmailReceive"
    return {
        "listener_id": listener_id,
        "workflow_type": "PollingTriggerWorkflow",
        "event_type": "t.poll",
        "event_types": ["t.poll"],
        "trigger_node_id": "n-poll",
        "listener_args": data,
    }


@pytest.mark.asyncio
async def test_controller_on_event_requests_rollover_under_pressure():
    controller = WorkflowControlWorkflow()
    controller._can_enabled = True
    controller._triggers["l-push"] = _push_spec()
    controller._history_pressure = lambda: True  # type: ignore[method-assign]

    await controller.on_event({"id": "evt-1", "type": "t.x"})

    assert controller._events  # queued
    assert controller._can_requested is True


def test_controller_seen_event_ids_are_bounded():
    from services.temporal import workflow_control_workflow as control_module

    controller = WorkflowControlWorkflow()
    cap = control_module._MAX_CARRIED_SEEN_IDS
    for index in range(cap + 10):
        controller._remember_event_id(f"key-{index}")
    assert len(controller._seen_event_ids) == cap
    assert "key-0" not in controller._seen_event_ids
    assert f"key-{cap + 9}" in controller._seen_event_ids


@pytest.mark.asyncio
async def test_controller_rollover_carries_triggers_events_and_state(monkeypatch):
    from services.temporal import workflow_control_workflow as control_module

    temporal_workflow = control_module.workflow
    monkeypatch.setattr(temporal_workflow, "logger", MagicMock())
    captured: dict = {}

    def fake_can(args=None):
        captured["carried"] = args[0]
        raise _CanFired

    monkeypatch.setattr(temporal_workflow, "continue_as_new", fake_can)

    controller = WorkflowControlWorkflow()
    controller._state = "paused"  # rollover must work mid-pause
    controller._revision = 7
    controller._triggers["l-push"] = _push_spec()
    controller._events.append(("l-push", {"id": "evt-9", "type": "t.x"}))
    controller._remember_event_id("l-push:evt-9")
    # A live poll task the rollover must cancel cleanly.
    blocker = asyncio.Event()

    async def parked_poll():
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            raise

    controller._poll_tasks["l-poll"] = asyncio.create_task(parked_poll())

    with pytest.raises(_CanFired):
        await controller._continue_as_new({"generation": 3, "workflow_id": "wf-1"})

    carried = captured["carried"]
    assert carried["state"] == "paused"
    assert carried["revision"] == 7
    assert carried["generation"] == 3
    assert "l-push" in carried["triggers"]
    assert [event["id"] for _lid, event in carried["pending_events"]] == ["evt-9"]
    assert "l-push:evt-9" in carried["seen_event_ids"]
    assert controller._poll_tasks["l-poll"].cancelled()


@pytest.mark.asyncio
async def test_controller_seeds_carried_state_and_restarts_poll_loops(monkeypatch):
    controller = WorkflowControlWorkflow()
    started: list = []

    async def fake_poll(listener_id, spec):
        started.append(listener_id)
        await asyncio.Event().wait()

    monkeypatch.setattr(controller, "_poll_trigger", fake_poll)

    controller._seed_carried_state(
        {
            "revision": 4,
            "seen_event_ids": ["l-push:evt-1"],
            "pending_events": [["l-push", {"id": "evt-2", "type": "t.x"}]],
            "triggers": {"l-push": _push_spec(), "l-poll": _poll_spec()},
        }
    )
    await asyncio.sleep(0)  # let the created task start

    assert controller._revision == 4
    assert "l-push:evt-1" in controller._seen_event_ids
    assert controller._events == [("l-push", {"id": "evt-2", "type": "t.x"})]
    assert set(controller._triggers) == {"l-push", "l-poll"}
    assert started == ["l-poll"]  # only polling triggers get loops
    controller._poll_tasks["l-poll"].cancel()


@pytest.mark.asyncio
async def test_controller_poll_loop_writes_seen_ids_back_into_the_spec(monkeypatch):
    """The provider baseline must live in the carried spec so a rollover
    restarts the poll loop exactly where it left off."""
    from services.temporal import workflow_control_workflow as control_module

    temporal_workflow = control_module.workflow
    monkeypatch.setattr(temporal_workflow, "logger", MagicMock())
    monkeypatch.setattr(temporal_workflow, "wait_condition", _predicate_wait)
    monkeypatch.setattr(temporal_workflow, "sleep", AsyncMock())

    controller = WorkflowControlWorkflow()
    controller._can_enabled = False
    spec = _poll_spec()
    controller._triggers[spec["listener_id"]] = spec

    async def poll_activity(*_args, **_kwargs):
        controller._closed = True  # one cycle, then exit
        return {"seen_ids": ["provider-1"], "events": []}

    monkeypatch.setattr(temporal_workflow, "execute_activity", poll_activity)

    await controller._poll_trigger(spec["listener_id"], spec)
    assert spec["listener_args"]["seen_ids"] == ["provider-1"]


@pytest.mark.asyncio
async def test_controller_push_spawn_failure_does_not_kill_the_run_loop(monkeypatch):
    from services.temporal import workflow_control_workflow as control_module

    temporal_workflow = control_module.workflow
    monkeypatch.setattr(temporal_workflow, "logger", MagicMock())
    monkeypatch.setattr(temporal_workflow, "wait_condition", _predicate_wait)
    monkeypatch.setattr(temporal_workflow, "in_workflow", lambda: False)

    controller = WorkflowControlWorkflow()
    calls = {"n": 0}

    async def failing_spawn(_event, _spec):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("duplicate child id")
        controller._closed = True

    monkeypatch.setattr(controller, "_spawn_push_run", failing_spawn)
    controller._triggers["l-push"] = _push_spec()
    controller._events.append(("l-push", {"id": "evt-1", "type": "t.x"}))
    controller._events.append(("l-push", {"id": "evt-2", "type": "t.x"}))

    result = await controller.run({"generation": 1, "state": "running"})
    # First spawn failed, second still ran; the controller survived both.
    assert calls["n"] == 2
    assert result["generation"] == 1


# ---------------------------------------------------------------------------
# dispatch.emit controller narrowing.
# ---------------------------------------------------------------------------


def test_dispatch_controller_helpers_read_both_attribute_shapes():
    from services.events.dispatch import _controller_event_types, _is_controller

    controller = SimpleNamespace(workflow_type="WorkflowControlWorkflow")
    listener = SimpleNamespace(workflow_type="TriggerListenerWorkflow")
    assert _is_controller(controller) is True
    assert _is_controller(listener) is False

    typed = SimpleNamespace(
        typed_search_attributes=[
            SimpleNamespace(
                key=SimpleNamespace(name="ControlEventTypes"),
                value=["a.b", "c.d"],
            )
        ],
        search_attributes=None,
    )
    assert _controller_event_types(typed) == {"a.b", "c.d"}

    plain = SimpleNamespace(
        typed_search_attributes=None,
        search_attributes={"ControlEventTypes": ["x.y"]},
    )
    assert _controller_event_types(plain) == {"x.y"}

    absent = SimpleNamespace(typed_search_attributes=None, search_attributes=None)
    assert _controller_event_types(absent) is None  # pre-upgrade: match-all


def test_control_event_types_search_attribute_is_registered():
    from services.temporal.search_attributes import EVENT_SEARCH_ATTRIBUTES

    names = {spec.name for spec in EVENT_SEARCH_ATTRIBUTES}
    assert "ControlEventTypes" in names


# ---------------------------------------------------------------------------
# Terminate-sweep guard + controller addressing.
# ---------------------------------------------------------------------------


def test_active_states_are_shared_and_include_resetting():
    from models.database import WORKFLOW_CONTROL_ACTIVE_STATES
    from services.deployment.control import ACTIVE_STATES

    assert ACTIVE_STATES is WORKFLOW_CONTROL_ACTIVE_STATES
    assert "resetting" in ACTIVE_STATES
    # core.database is conftest-stubbed, so verify the real source text
    # references the shared constant instead of a hand-copied set.
    database_source = (SERVER_DIR / "core" / "database.py").read_text(encoding="utf-8")
    assert "WORKFLOW_CONTROL_ACTIVE_STATES" in database_source
    assert '"starting", "running", "pausing", "paused", "resuming"}' not in database_source


def test_controller_handles_are_never_run_id_pinned():
    """Continue-as-new mints new run ids under the same workflow id; a
    pinned handle would target a closed run after the first rollover."""
    from services.deployment import handlers, manager

    handle_source = inspect.getsource(handlers._controller_handle)
    assert "get_workflow_handle(control.controller_workflow_id)" in handle_source

    manager_source = inspect.getsource(manager)
    assert 'run_id=control.controller_run_id' not in manager_source


def test_terminate_flag_defaults_false_in_both_env_files():
    for env_name in (".env.template", ".env"):
        text = (SERVER_DIR.parent / env_name).read_text(encoding="utf-8")
        assert "TEMPORAL_TERMINATE_RUNNING_ON_STARTUP=false" in text
        assert "TEMPORAL_TERMINATE_RUNNING_ON_STARTUP=true" not in text


# ---------------------------------------------------------------------------
# Boot-time reconcile.
# ---------------------------------------------------------------------------


def _boot_control(status: str, *, graph_nodes=None):
    return SimpleNamespace(
        id="workflow-control:wf:1",
        workflow_id="wf",
        generation=1,
        execution_id="execution-1",
        root_execution_id="execution-1",
        data_scope_id="execution-1",
        controller_workflow_id="workflow-control-wf-g1",
        controller_run_id="controller-run-1",
        status=status,
        revision=3,
        graph_snapshot={
            "nodes": graph_nodes
            if graph_nodes is not None
            else [{"id": "t-1", "type": "webhookTrigger", "data": {}}],
            "edges": [],
        },
        created_at=None,
        updated_at=None,
        terminal_reason=None,
    )


@pytest.mark.asyncio
async def test_interrupted_start_with_registered_triggers_converges_to_running(
    monkeypatch,
):
    from services.deployment import handlers

    control = _boot_control("starting")
    running = _boot_control("running")
    service = SimpleNamespace(
        transition=AsyncMock(return_value=running),
        fail=AsyncMock(),
        database=SimpleNamespace(
            get_latest_workflow_control=AsyncMock(return_value=control)
        ),
    )
    monkeypatch.setattr(handlers, "_broadcast_control", AsyncMock(return_value={}))

    result = await handlers._converge_interrupted_start(
        service, control, {"state": "running", "triggers": {"l-1": "n-1"}}
    )

    assert result.status == "running"
    assert service.transition.await_args.kwargs["status"] == "running"
    service.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupted_start_with_empty_controller_fails_and_closes_it(
    monkeypatch,
):
    from services.deployment import handlers

    control = _boot_control("starting")
    failed = _boot_control("failed")
    service = SimpleNamespace(
        transition=AsyncMock(),
        fail=AsyncMock(return_value=failed),
        database=SimpleNamespace(
            get_latest_workflow_control=AsyncMock(return_value=control)
        ),
    )
    reset_signal = AsyncMock()
    monkeypatch.setattr(handlers, "_signal_controller", reset_signal)
    monkeypatch.setattr(handlers, "_broadcast_control", AsyncMock(return_value={}))

    result = await handlers._converge_interrupted_start(
        service, control, {"state": "running", "triggers": {}}
    )

    assert result.status == "failed"
    assert service.fail.await_args.args[1] == "interrupted_start"
    reset_signal.assert_awaited_once_with(control, "reset")
    service.transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupted_start_of_triggerless_graph_converges_to_running(
    monkeypatch,
):
    from services.deployment import handlers

    control = _boot_control(
        "starting",
        graph_nodes=[{"id": "a-1", "type": "aiAgent", "data": {}}],
    )
    running = _boot_control("running")
    service = SimpleNamespace(
        transition=AsyncMock(return_value=running),
        fail=AsyncMock(),
        database=SimpleNamespace(
            get_latest_workflow_control=AsyncMock(return_value=control)
        ),
    )
    monkeypatch.setattr(handlers, "_broadcast_control", AsyncMock(return_value={}))

    result = await handlers._converge_interrupted_start(
        service, control, {"state": "running", "triggers": {}}
    )
    assert result.status == "running"
    service.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreachable_controller_leaves_starting_row_untouched():
    from services.deployment import handlers

    control = _boot_control("starting")
    service = SimpleNamespace(transition=AsyncMock(), fail=AsyncMock())
    result = await handlers._converge_interrupted_start(service, control, None)
    assert result is control
    service.transition.assert_not_awaited()
    service.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_rearm_restores_paused_posture_from_snapshot(monkeypatch):
    from core.container import container
    from services.deployment import handlers

    control = _boot_control("paused")
    workflow_service = MagicMock()
    workflow_service.is_workflow_deployed.return_value = False
    workflow_service.pause_deployment = MagicMock()
    workflow_service.update_trigger_pause_status = AsyncMock(return_value=1)
    monkeypatch.setattr(
        container, "workflow_service", MagicMock(return_value=workflow_service)
    )
    deploy = AsyncMock(return_value={"success": True})
    setup = AsyncMock(return_value={"success": True})
    cron_pause = AsyncMock(return_value=0)
    monkeypatch.setattr(handlers, "handle_deploy_workflow", deploy)
    monkeypatch.setattr(handlers, "_await_deployment_setup", setup)
    monkeypatch.setattr(handlers, "_set_cron_pause", cron_pause)

    await handlers._rearm_generation(control)

    deploy_data = deploy.await_args.args[0]
    assert deploy_data["workflow_id"] == "wf"
    assert deploy_data["nodes"] == control.graph_snapshot["nodes"]
    # Generation-scoped persistence, same contract as handle_start_workflow.
    assert deploy_data["session_id"] == "execution-1"
    workflow_service.pause_deployment.assert_called_once_with("wf")
    workflow_service.update_trigger_pause_status.assert_awaited_once_with(
        "wf", paused=True
    )
    cron_pause.assert_awaited_once_with("wf", paused=True, strict=False)


@pytest.mark.asyncio
async def test_rearm_skips_already_deployed_workflows(monkeypatch):
    from core.container import container
    from services.deployment import handlers

    control = _boot_control("running")
    workflow_service = MagicMock()
    workflow_service.is_workflow_deployed.return_value = True
    monkeypatch.setattr(
        container, "workflow_service", MagicMock(return_value=workflow_service)
    )
    deploy = AsyncMock()
    monkeypatch.setattr(handlers, "handle_deploy_workflow", deploy)

    await handlers._rearm_generation(control)
    deploy.assert_not_awaited()


@pytest.mark.asyncio
async def test_boot_reconcile_processes_each_active_row_with_isolation(monkeypatch):
    from core.container import container
    from services.deployment import handlers

    healthy = _boot_control("running")
    broken = _boot_control("running")
    broken.workflow_id = "wf-broken"
    database = SimpleNamespace(
        list_active_workflow_controls=AsyncMock(return_value=[broken, healthy]),
    )
    monkeypatch.setattr(container, "database", MagicMock(return_value=database))

    async def reconcile(_service, control):
        if control.workflow_id == "wf-broken":
            raise RuntimeError("temporal hiccup")
        return control, {"state": "running"}

    rearm = AsyncMock()
    monkeypatch.setattr(handlers, "_reconcile_control", reconcile)
    monkeypatch.setattr(handlers, "_rearm_generation", rearm)

    processed = await handlers.reconcile_active_controls_on_boot()

    # The broken row was isolated; the healthy one was re-armed.
    assert processed == 1
    rearm.assert_awaited_once_with(healthy)
