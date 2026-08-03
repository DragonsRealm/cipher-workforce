"""Long-lived deployment controller and trigger hub for one generation."""

import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from temporalio import workflow
from temporalio.common import SearchAttributeKey, SearchAttributePair
from temporalio.exceptions import ApplicationError

from services.temporal._retry_policies import DEFAULT_ACTIVITY_RETRY


# The controller is the ONE workflow expected to live for months, and it
# multiplexes every trigger's signals, poll timers/activities, and child
# spawns into a single event history. Temporal terminates any workflow
# around ~51,200 history events, so without continue-as-new a single
# polling trigger at the default 60s interval killed the whole
# deployment's control plane in days. The run rolls over (carrying
# triggers, control state, queued events, and per-trigger seen-id
# baselines) whenever the server suggests it or the history crosses the
# soft cap below.

# Roll over before the server's hard ceiling. is_continue_as_new_suggested
# is the primary signal; this cap is the deterministic backstop.
_HISTORY_SOFT_CAP = 10_000
# Bounds on state carried across continue-as-new — the CAN argument blob
# is capped at 2MiB by Temporal, so unbounded carries would trade a
# history overflow for an argument overflow.
_MAX_CARRIED_EVENTS = 256
_MAX_CARRIED_SEEN_IDS = 4_096
# Defensive floor for user-supplied poll intervals: a few-second interval
# burns ~100K+ history events/day. Mirrors the plugin-side clamp
# (PollingTriggerNode.poll_interval_clamp) which the legacy asyncio path
# applies but workflow payloads historically did not.
_MIN_POLL_INTERVAL_S = 30


@workflow.defn(name="WorkflowControlWorkflow", sandboxed=False)
class WorkflowControlWorkflow:
    """Own control state and trigger scheduling without listener workflows.

    Trigger definitions and inbound events are recorded as signals in this
    workflow's own history. Only an actual triggered graph run becomes a child
    workflow, so Temporal's workflow list has no per-trigger listener rows.

    Longevity contract: the controller continue-as-news to keep its
    history bounded. All state a rollover must preserve lives in
    ``control_data`` — trigger specs (whose ``listener_args["seen_ids"]``
    the poll loops write back after every cycle), the pending push-event
    queue, the dedup baseline, and the control state/revision. Callers
    therefore address the controller by workflow id only, never a pinned
    run id.
    """

    def __init__(self) -> None:
        self._state = "running"
        self._revision = 0
        self._closed = False
        self._triggers: dict[str, Dict[str, Any]] = {}
        self._events: list[tuple[str, Dict[str, Any]]] = []
        # Insertion-ordered so the carry across continue-as-new can keep
        # the newest entries when trimming to _MAX_CARRIED_SEEN_IDS.
        self._seen_event_ids: dict[str, None] = {}
        self._poll_tasks: dict[str, asyncio.Task] = {}
        self._can_requested = False

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
        else:
            self._upsert_event_types_attribute()

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
                self._remember_event_id(dedup_key)
                self._events.append((listener_id, event))
        # A paused controller still receives matching signals; without this
        # check its history could overflow mid-pause with the run loop
        # parked. The flag wakes the run loop, which rolls over carrying
        # the queue (and the paused state) forward.
        self._maybe_request_rollover()

    @workflow.query
    def status(self) -> Dict[str, Any]:
        return {
            "state": self._state, "revision": self._revision,
            "triggers": {key: value["trigger_node_id"] for key, value in self._triggers.items()},
            "queued_events": len(self._events),
        }

    @workflow.run
    async def run(self, control_data: Dict[str, Any]) -> Dict[str, Any]:
        self._state = control_data.get("state", "running")
        self._seed_carried_state(control_data)
        while not self._closed:
            await workflow.wait_condition(
                lambda: (
                    self._closed
                    or self._can_requested
                    or (self._state == "running" and bool(self._events))
                )
            )
            if self._closed:
                break
            if self._can_requested:
                await self._continue_as_new(control_data)
                # Unreachable in a real run; direct unit invocation returns.
                break
            listener_id, event = self._events.pop(0)
            spec = self._triggers.get(listener_id)
            if spec is None:
                continue
            try:
                await self._spawn_push_run(event, spec)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — per-event isolation
                # One bad event (duplicate child id, exhausted broadcast
                # retries) must not fail the deployment's entire control
                # plane. Same isolation contract as the listener classes.
                workflow.logger.error(
                    f"Controller push spawn failed for event.id={event.get('id')}: {exc}"
                )
            self._maybe_request_rollover()
        return {"state": self._state, "generation": control_data.get("generation")}

    # ---- continue-as-new machinery ------------------------------------

    def _seed_carried_state(self, control_data: Dict[str, Any]) -> None:
        """Rehydrate rollover-carried state; no-op for first-run payloads.

        Everything here is command-free, so pre-patch histories (whose
        payloads lack the carried keys) replay identically.
        """
        self._revision = int(control_data.get("revision") or 0)
        for seen_id in control_data.get("seen_event_ids") or []:
            self._seen_event_ids[str(seen_id)] = None
        for pending in control_data.get("pending_events") or []:
            try:
                listener_id, event = pending[0], pending[1]
            except (IndexError, TypeError, KeyError):
                continue
            self._events.append((str(listener_id), event))
        for listener_id, spec in (control_data.get("triggers") or {}).items():
            listener_id = str(listener_id)
            if listener_id in self._triggers:
                continue
            self._triggers[listener_id] = spec
            if spec.get("workflow_type") == "PollingTriggerWorkflow":
                self._poll_tasks[listener_id] = asyncio.create_task(
                    self._poll_trigger(listener_id, spec)
                )
        if any(
            spec.get("workflow_type") != "PollingTriggerWorkflow"
            for spec in self._triggers.values()
        ):
            self._upsert_event_types_attribute()

    def _remember_event_id(self, dedup_key: str) -> None:
        self._seen_event_ids[dedup_key] = None
        if len(self._seen_event_ids) > _MAX_CARRIED_SEEN_IDS:
            oldest = next(iter(self._seen_event_ids))
            del self._seen_event_ids[oldest]

    def _history_pressure(self) -> bool:
        from services.temporal.trigger_listener_workflow import _history_pressure

        return _history_pressure(_HISTORY_SOFT_CAP)

    def _maybe_request_rollover(self) -> None:
        if not self._closed and not self._can_requested:
            if self._history_pressure():
                self._can_requested = True

    async def _continue_as_new(self, control_data: Dict[str, Any]) -> None:
        """Roll the run over, carrying everything a controller must keep.

        Poll tasks are cancelled first — their provider ``seen_ids``
        baselines were written back into each trigger spec's
        ``listener_args`` after every cycle, so the carried specs restart
        the loops in the new run without re-emitting old items. Works
        while paused too: the paused state (and queued events) carry.
        """
        for task in self._poll_tasks.values():
            task.cancel()
        if self._poll_tasks:
            await asyncio.gather(*self._poll_tasks.values(), return_exceptions=True)
        dropped = max(0, len(self._events) - _MAX_CARRIED_EVENTS)
        if dropped:
            workflow.logger.warning(
                f"Controller rollover dropping {dropped} oldest queued event(s) "
                f"beyond the {_MAX_CARRIED_EVENTS} carry cap"
            )
        carried: Dict[str, Any] = {
            **control_data,
            "state": self._state,
            "revision": self._revision,
            "triggers": self._triggers,
            "pending_events": [
                list(item) for item in self._events[-_MAX_CARRIED_EVENTS:]
            ],
            "seen_event_ids": list(self._seen_event_ids)[-_MAX_CARRIED_SEEN_IDS:],
        }
        workflow.logger.info(
            f"Controller continue_as_new: triggers={len(self._triggers)} "
            f"pending={len(carried['pending_events'])} state={self._state}"
        )
        workflow.continue_as_new(args=[carried])

    def _upsert_event_types_attribute(self) -> None:
        """Advertise push event types via the ControlEventTypes attribute.

        Lets ``dispatch.emit`` skip controllers with no matching trigger
        instead of signalling every running controller with every
        platform event. Controllers without the attribute are treated as
        match-all by dispatch.
        """
        # Upsert is a workflow command and the failure path logs through
        # ``workflow.logger``; both need the workflow event loop. Direct
        # unit invocation has no loop, so no-op there rather than raising
        # out of the exception handler.
        if not workflow.in_workflow():
            return
        event_types: set[str] = set()
        for spec in self._triggers.values():
            if spec.get("workflow_type") == "PollingTriggerWorkflow":
                continue
            for event_type in spec.get("event_types") or [spec.get("event_type")]:
                if event_type:
                    event_types.add(str(event_type))
        if not event_types:
            return
        try:
            workflow.upsert_search_attributes(
                [
                    SearchAttributePair(
                        SearchAttributeKey.for_keyword_list("ControlEventTypes"),
                        sorted(event_types),
                    )
                ]
            )
        except Exception as exc:  # noqa: BLE001 — attribute is an optimisation
            workflow.logger.warning(f"ControlEventTypes upsert failed (non-fatal): {exc}")

    # ---- spawning -------------------------------------------------------

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
            admission_check=self._wait_until_running,
            search_attributes=(
                event_workflow_search_attributes(
                    listener_args.get("workflow_id")
                )
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
        # A few-second user-supplied interval overflows history in hours.
        poll_interval = max(_MIN_POLL_INTERVAL_S, poll_interval)
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                workflow.logger.error(f"Controlled polling trigger failed: {exc}")
                self._maybe_request_rollover()
                continue
            seen_ids = set(result.get("seen_ids") or [])
            # Write the provider baseline back into the carried spec so a
            # continue-as-new (which cancels this task) restarts the loop
            # exactly where it left off instead of re-emitting old items.
            listener_data["seen_ids"] = list(seen_ids)
            for event in result.get("events") or []:
                event_id = str(event.get("id") or "")
                dedup_key = f"{listener_id}:{event_id}"
                if not event_id or dedup_key in self._seen_event_ids:
                    continue
                self._remember_event_id(dedup_key)
                if self._state != "running":
                    await workflow.wait_condition(lambda: self._closed or self._state == "running")
                if self._closed:
                    return
                try:
                    await runner._spawn_child_run(
                        event,
                        listener_data,
                        admission_check=self._wait_until_running,
                        search_attributes=event_workflow_search_attributes(
                            listener_data.get("workflow_id")
                        ),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as spawn_exc:  # noqa: BLE001 — per-event isolation
                    # A failed spawn must not silently kill this poll task
                    # (the trigger would stop firing forever while the
                    # controller still reported Running).
                    workflow.logger.error(
                        f"Controlled polling spawn failed for event.id={event_id}: {spawn_exc}"
                    )
            # Poll cycles burn history even with zero events — request
            # rollover from here too, so a quiet mailbox cannot ride the
            # controller into the server's hard termination ceiling.
            self._maybe_request_rollover()


__all__ = [
    "WorkflowControlWorkflow",
]
