"""Long-lived deployment controller and trigger hub for one generation."""

import asyncio
from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from services.temporal._retry_policies import DEFAULT_ACTIVITY_RETRY


COOPERATIVE_PAUSE_SCHEDULING_PATCH = (
    "workflow-control-cooperative-pause-scheduling-v1"
)


@workflow.defn(name="WorkflowControlWorkflow", sandboxed=False)
class WorkflowControlWorkflow:
    """Own control state and trigger scheduling without listener workflows.

    Trigger definitions and inbound events are recorded as signals in this
    workflow's own history. Only an actual triggered graph run becomes a child
    workflow, so Temporal's workflow list has no per-trigger listener rows.
    """

    def __init__(self) -> None:
        self._state = "running"
        self._revision = 0
        self._closed = False
        self._triggers: dict[str, Dict[str, Any]] = {}
        self._events: list[tuple[str, Dict[str, Any]]] = []
        self._seen_event_ids: set[str] = set()
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._use_cooperative_pause_schedule_gate = False

    @workflow.signal
    async def pause(self) -> None:
        self._apply_control_state("paused")

    @workflow.signal
    async def resume(self) -> None:
        self._apply_control_state("running")

    @workflow.update
    async def set_control_state(self, requested_state: str) -> Dict[str, Any]:
        """Idempotently apply and acknowledge a pause/resume transition.

        Updates give callers a durable acknowledgement that the controller
        processed the request. The legacy signals above remain registered for
        older callers and histories.
        """
        normalized = str(requested_state).strip().lower()
        aliases = {
            "pause": "paused",
            "paused": "paused",
            "resume": "running",
            "running": "running",
        }
        target_state = aliases.get(normalized)
        if target_state is None:
            raise ApplicationError(
                "Control state must be one of: pause, paused, resume, running",
                type="InvalidWorkflowControlState",
                non_retryable=True,
            )
        self._apply_control_state(target_state)
        return self.status()

    def _apply_control_state(self, target_state: str) -> None:
        """Apply one valid live-state transition without double revision."""
        if self._state not in {"running", "paused"}:
            return
        if self._state != target_state:
            self._state = target_state
            self._revision += 1

    @workflow.signal
    async def reset(self) -> None:
        self._state = "resetting"
        self._revision += 1
        self._closed = True
        for task in self._poll_tasks.values():
            task.cancel()

    @workflow.signal
    async def register_trigger(self, spec: Dict[str, Any]) -> None:
        listener_id = str(spec["listener_id"])
        if listener_id in self._triggers:
            return
        self._triggers[listener_id] = spec
        if spec["workflow_type"] == "PollingTriggerWorkflow":
            self._poll_tasks[listener_id] = asyncio.create_task(self._poll_trigger(listener_id, spec))

    @workflow.signal
    async def on_event(self, event: Dict[str, Any]) -> None:
        event_id = str(event.get("id") or "")
        event_type = str(event.get("type") or "")
        if not event_id or not event_type:
            return
        for listener_id, spec in self._triggers.items():
            if spec["workflow_type"] == "PollingTriggerWorkflow":
                continue
            if event_type not in set(spec.get("event_types") or [spec.get("event_type")]):
                continue
            dedup_key = f"{listener_id}:{event_id}"
            if dedup_key not in self._seen_event_ids:
                self._seen_event_ids.add(dedup_key)
                self._events.append((listener_id, event))

    @workflow.query
    def status(self) -> Dict[str, Any]:
        return {
            "state": self._state, "revision": self._revision,
            "triggers": {key: value["trigger_node_id"] for key, value in self._triggers.items()},
            "queued_events": len(self._events),
        }

    @workflow.run
    async def run(self, control_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self._use_cooperative_pause_schedule_gate = workflow.patched(
                COOPERATIVE_PAUSE_SCHEDULING_PATCH
            )
        except Exception:  # direct unit invocation outside Temporal runtime
            if workflow.in_workflow():
                raise
            self._use_cooperative_pause_schedule_gate = False
        self._state = control_data.get("state", "running")
        while not self._closed:
            await workflow.wait_condition(
                lambda: self._closed or (self._state == "running" and bool(self._events))
            )
            if self._closed:
                break
            listener_id, event = self._events.pop(0)
            spec = self._triggers.get(listener_id)
            if spec is not None:
                await self._spawn_push_run(event, spec)
        return {"state": self._state, "generation": control_data.get("generation")}

    async def _spawn_push_run(self, event: Dict[str, Any], spec: Dict[str, Any]) -> None:
        from services.temporal.trigger_listener_workflow import (
            TriggerListenerWorkflow,
            event_workflow_search_attributes,
        )

        listener = TriggerListenerWorkflow()
        listener_args = spec["listener_args"]
        await listener._spawn_child_run(
            event,
            listener_args,
            # This admission check predates the patch marker and must remain
            # unconditional for replay compatibility with existing controller
            # histories. The marker gates only the new child metadata.
            admission_check=self._wait_until_running,
            search_attributes=(
                event_workflow_search_attributes(
                    listener_args.get("workflow_id")
                )
                if self._use_cooperative_pause_schedule_gate
                else None
            ),
        )

    async def _wait_until_running(self) -> None:
        if self._state != "running":
            await workflow.wait_condition(lambda: self._closed or self._state == "running")
        if self._closed:
            raise asyncio.CancelledError

    async def _poll_trigger(self, listener_id: str, spec: Dict[str, Any]) -> None:
        from services.temporal.polling_trigger_workflow import (
            PollingTriggerWorkflow,
            _ACTIVITY_TIMEOUT_MULT,
            _DEFAULT_POLL_INTERVAL_S,
        )
        from services.temporal.trigger_listener_workflow import (
            event_workflow_search_attributes,
        )

        listener_data = spec["listener_args"]
        node_type = listener_data["node_type"]
        activity_name = f"poll.{node_type}.v{listener_data.get('version', 1)}"
        params = listener_data.get("filter_params", {}) or {}
        poll_interval = int(params.get("poll_interval") or _DEFAULT_POLL_INTERVAL_S)
        activity_timeout_s = max(30, poll_interval * _ACTIVITY_TIMEOUT_MULT)
        seen_ids: set[str] = set(listener_data.get("seen_ids") or [])
        baseline = not seen_ids
        runner = PollingTriggerWorkflow()

        while not self._closed and listener_id in self._triggers:
            if self._state != "running":
                await workflow.wait_condition(lambda: self._closed or self._state == "running")
            if self._closed:
                return
            if baseline:
                baseline = False
                baseline_only = True
            else:
                await workflow.sleep(timedelta(seconds=poll_interval))
                if self._state != "running":
                    continue
                baseline_only = False
            payload = {
                "node_id": listener_data["trigger_node_id"], "params": params,
                "seen_ids": list(seen_ids), "baseline_only": baseline_only,
            }
            try:
                result = await workflow.execute_activity(
                    activity_name, payload, activity_id=listener_data["trigger_node_id"],
                    start_to_close_timeout=timedelta(seconds=activity_timeout_s),
                    retry_policy=DEFAULT_ACTIVITY_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                workflow.logger.error(f"Controlled polling trigger failed: {exc}")
                continue
            seen_ids = set(result.get("seen_ids") or [])
            for event in result.get("events") or []:
                event_id = str(event.get("id") or "")
                dedup_key = f"{listener_id}:{event_id}"
                if not event_id or dedup_key in self._seen_event_ids:
                    continue
                self._seen_event_ids.add(dedup_key)
                if self._state != "running":
                    await workflow.wait_condition(lambda: self._closed or self._state == "running")
                if self._closed:
                    return
                await runner._spawn_child_run(
                    event,
                    listener_data,
                    admission_check=(
                        self._wait_until_running
                        if self._use_cooperative_pause_schedule_gate
                        else None
                    ),
                    search_attributes=(
                        event_workflow_search_attributes(
                            listener_data.get("workflow_id")
                        )
                        if self._use_cooperative_pause_schedule_gate
                        else None
                    ),
                )


__all__ = [
    "COOPERATIVE_PAUSE_SCHEDULING_PATCH",
    "WorkflowControlWorkflow",
]
