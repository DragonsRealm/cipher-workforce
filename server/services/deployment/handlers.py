"""Deployment domain WebSocket handlers.

Extracted from ``routers/websocket.py`` (Wave 13.2). The 5 handlers
below cover the deployment lifecycle:

  - ``deploy_workflow`` — start a continuously-running workflow with
    triggers + per-workflow locking.
  - ``cancel_deployment`` — cancel a running deployment, drain its
    listeners, unlock the workflow.
  - ``get_deployment_status`` — snapshot of in-flight deployments.
  - ``get_workflow_lock`` — current lock state.
  - ``update_deployment_settings`` — mutate runtime settings without
    re-deploying.

All handlers preserve their pre-Wave-13 wire shape. The module-level
``_deployment_tasks`` dict (workflow_id -> asyncio.Task) moves here
too; it was process-local in ``routers/websocket.py`` and stays the
same shape — only the import path changes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import WebSocket

from core.logging import get_logger
from services.ws_handler_registry import ws_handler
from services.deployment.control import (
    ACTIVE_STATES,
    WorkflowControlService,
    serialize_control,
)

# ``core.container`` and ``services.status_broadcaster`` are lazy-imported
# inside each handler body. This module is imported transitively via
# ``services.workflow`` during ``core.container`` initialization (the
# container wires ``WorkflowService`` which imports ``services.workflow``
# which imports ``services.deployment``). Eager imports at module scope
# would deadlock the partially-initialized container module.

logger = get_logger(__name__)


class TemporalControlUnavailable(RuntimeError):
    """A lifecycle Update could not start because no Temporal client exists."""


class TemporalControlAckMismatch(RuntimeError):
    """Temporal completed an Update without returning the requested state."""


class ControllerExecutionMissing(RuntimeError):
    """The controller execution this generation names no longer exists.

    Distinct from :class:`TemporalControlUnavailable`, and the distinction is
    the whole point: "unavailable" is transient and worth waiting out, whereas
    a deleted execution never comes back. Treating the second as the first is
    what let a generation sit in ``pausing`` forever, which in turn blocked
    every future Start on that workflow.
    """


# Per-workflow deployment tasks for proper cancellation (Temporal/n8n pattern).
# Maps workflow_id -> asyncio.Task for parallel workflow deployments.
_deployment_tasks: Dict[str, asyncio.Task] = {}


@ws_handler()
async def handle_deploy_workflow(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    """Deploy workflow to run continuously until cancelled.

    Expects:
        workflow_id: Workflow identifier (required for locking)
        nodes: List of workflow nodes with {id, type, data}
        edges: List of edges with {id, source, target}
        session_id: Optional session identifier
        delay_between_runs: Optional delay in seconds between iterations (default: 1.0)

    Returns:
        Deployment start confirmation (deployment runs in background)
    """
    global _deployment_tasks
    from core.container import container
    from services.status_broadcaster import get_status_broadcaster

    workflow_service = container.workflow_service()
    broadcaster = get_status_broadcaster()

    workflow_id = data.get("workflow_id")
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    from services.workflow_migrations import normalize_legacy_android_toolkit

    nodes, edges, normalized_parameters, migration_warnings = normalize_legacy_android_toolkit(
        nodes, edges, data.get("parameters_by_id")
    )
    if migration_warnings:
        logger.warning("[Deploy] %s", "; ".join(migration_warnings))
    session_id = data.get("session_id", "default")

    logger.debug(f"[Deploy] Received {len(edges)} edges for workflow {workflow_id}")
    for e in edges:
        target_handle = e.get("targetHandle")
        if target_handle and target_handle.startswith("input-") and target_handle != "input-main":
            logger.debug(f"[Deploy] Config edge: {e.get('source')} -> {e.get('target')} (handle={target_handle})")

    tool_edges = [e for e in edges if e.get("targetHandle") == "input-tools"]
    if tool_edges:
        logger.debug(f"[Deploy] Tool edges found: {len(tool_edges)}")
        for te in tool_edges:
            logger.debug(f"[Deploy] Tool edge: source={te.get('source')} -> target={te.get('target')}")
    else:
        logger.debug("[Deploy] No input-tools edges found")

    if not nodes:
        return {"success": False, "error": "No nodes provided"}

    if not workflow_id:
        return {"success": False, "error": "workflow_id is required for deployment"}

    # Pre-deploy validation gate. Deploy never honors a force-override —
    # a broken workflow running on a schedule is far worse than a failed
    # one-shot manual run.
    from services.workflow_validator import validate_workflow

    deploy_report = await validate_workflow(
        nodes=nodes,
        edges=edges,
        parameters_by_id=normalized_parameters,
    )
    if deploy_report["errors"]:
        return {
            "success": False,
            "error": "validation_failed",
            "report": deploy_report,
        }

    if workflow_service.is_workflow_deployed(workflow_id):
        status = workflow_service.get_deployment_status(workflow_id)
        return {
            "success": False,
            "error": f"Workflow {workflow_id} is already deployed. Cancel it first.",
            "workflow_id": workflow_id,
            "is_running": True,
            "run_counter": status.get("run_counter", 0),
        }

    lock_acquired = await broadcaster.lock_workflow(workflow_id, reason="deployment")
    if not lock_acquired:
        lock_info = broadcaster.get_workflow_lock(workflow_id)
        return {
            "success": False,
            "error": f"Workflow {workflow_id} is already locked for {lock_info.get('reason', 'deployment')}",
            "locked_by": lock_info.get("workflow_id"),
            "locked_at": lock_info.get("locked_at"),
        }

    await broadcaster.update_workflow_status(
        executing=True,
        current_node=None,
        progress=0,
        workflow_id=workflow_id,
    )
    await broadcaster.update_deployment_status(
        is_running=True,
        status="starting",
        active_runs=0,
        workflow_id=workflow_id,
    )

    async def status_callback(node_id: str, status: str, node_data: Optional[Dict] = None):
        if node_id == "__deployment__":
            active_runs = node_data.get("active_runs", 0) if node_data else 0
            await broadcaster.update_deployment_status(
                is_running=True,
                status=status,
                active_runs=active_runs,
                workflow_id=workflow_id,
                data=node_data,
            )
        else:
            await broadcaster.update_node_status(node_id, status, node_data, workflow_id=workflow_id)
            if status == "executing":
                position = node_data.get("position", 0) if node_data else 0
                total = node_data.get("total", 1) if node_data else 1
                progress = int((position / total) * 100) if total > 0 else 0
                await broadcaster.update_workflow_status(
                    executing=True,
                    current_node=node_id,
                    progress=progress,
                    workflow_id=workflow_id,
                )

    async def run_deployment():
        try:
            result = await workflow_service.deploy_workflow(
                nodes=nodes,
                edges=edges,
                session_id=session_id,
                status_callback=status_callback,
                workflow_id=workflow_id,
            )

            if not result.get("success"):
                logger.error("Deployment setup failed", error=result.get("error"), workflow_id=workflow_id)
                await broadcaster.update_deployment_status(
                    is_running=False,
                    status="error",
                    active_runs=0,
                    workflow_id=workflow_id,
                    error=result.get("error"),
                )
                await broadcaster.unlock_workflow(workflow_id)
                _deployment_tasks.pop(workflow_id, None)
                return result
            else:
                await broadcaster.update_deployment_status(
                    is_running=True,
                    status="running",
                    active_runs=0,
                    workflow_id=workflow_id,
                    data={
                        "triggers_setup": result.get("triggers_setup", []),
                        "deployment_id": result.get("deployment_id"),
                    },
                )
                logger.info(
                    "[Deployment] Event-driven deployment active",
                    deployment_id=result.get("deployment_id"),
                    workflow_id=workflow_id,
                    triggers=len(result.get("triggers_setup", [])),
                )
                return result

        except Exception as e:
            logger.error("Deployment task error", workflow_id=workflow_id, error=str(e))
            await broadcaster.update_deployment_status(
                is_running=False,
                status="error",
                active_runs=0,
                workflow_id=workflow_id,
                error=str(e),
            )
            await broadcaster.unlock_workflow(workflow_id)
            _deployment_tasks.pop(workflow_id, None)
            return {"success": False, "error": str(e), "workflow_id": workflow_id}

    _deployment_tasks[workflow_id] = asyncio.create_task(run_deployment())

    return {
        "success": True,
        "message": "Deployment started",
        "workflow_id": workflow_id,
        "is_running": True,
        "locked": True,
        "timestamp": time.time(),
    }


@ws_handler()
async def handle_cancel_deployment(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    """Cancel running deployment for a specific workflow (Temporal/n8n pattern).

    Expects:
        workflow_id: Workflow to cancel (required).

    Also cancels any active event waiters (trigger nodes) and unlocks the workflow.

    Returns:
        Cancellation result with iterations completed
    """
    global _deployment_tasks
    from core.container import container
    from services.status_broadcaster import get_status_broadcaster

    workflow_service = container.workflow_service()
    broadcaster = get_status_broadcaster()

    workflow_id = data.get("workflow_id")

    if not workflow_id:
        return {"success": False, "error": "workflow_id is required for cancellation"}

    result = await workflow_service.cancel_deployment(workflow_id)

    cancelled_waiters = 0
    if result.get("success"):
        cancelled_waiters = result.get("waiters_cancelled", 0)

    task = _deployment_tasks.pop(workflow_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.info("[Deployment] Deployment task cancelled", workflow_id=workflow_id)

    if workflow_id:
        await broadcaster.unlock_workflow(workflow_id)

    if result.get("success"):
        for node_id in result.get("cancelled_listener_node_ids", []):
            await broadcaster.clear_node_status(node_id)

        await broadcaster.update_workflow_status(
            executing=False,
            current_node=None,
            progress=0,
            workflow_id=workflow_id,
        )
        await broadcaster.update_deployment_status(
            is_running=False,
            status="cancelled",
            active_runs=0,
            workflow_id=workflow_id,
            data={
                "iterations_completed": result.get("iterations_completed", 0),
            },
        )

    return {
        "success": result.get("success", False),
        "message": result.get("message", result.get("error")),
        "workflow_id": workflow_id,
        "was_running": result.get("was_running", False),
        "iterations_completed": result.get("iterations_completed", 0),
        "cancelled_waiters": cancelled_waiters,
        "unlocked": workflow_id is not None,
        "timestamp": time.time(),
    }


@ws_handler()
async def handle_get_deployment_status(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    """Get current deployment status including workflow lock info."""
    from core.container import container
    from services.status_broadcaster import get_status_broadcaster

    workflow_service = container.workflow_service()
    broadcaster = get_status_broadcaster()

    workflow_id = data.get("workflow_id")
    status = workflow_service.get_deployment_status(workflow_id)

    return {
        "is_running": workflow_service.is_deployment_running(workflow_id),
        "run_counter": status.get("run_counter", 0),
        "active_runs": status.get("active_runs", 0),
        "settings": workflow_service.get_deployment_settings(),
        "workflow_id": workflow_id or status.get("workflow_id"),
        "deployed_workflows": status.get("deployed_workflows", []),
        "lock": broadcaster.get_workflow_lock(),
        "timestamp": time.time(),
    }


@ws_handler()
async def handle_get_workflow_lock(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    """Get current workflow lock status."""
    from services.status_broadcaster import get_status_broadcaster

    broadcaster = get_status_broadcaster()

    return {
        "lock": broadcaster.get_workflow_lock(),
        "timestamp": time.time(),
    }


@ws_handler()
async def handle_update_deployment_settings(
    data: Dict[str, Any],
    websocket: WebSocket,
) -> Dict[str, Any]:
    """Update deployment settings (can be called during active deployment)."""
    from core.container import container
    from services.status_broadcaster import get_status_broadcaster

    workflow_service = container.workflow_service()
    broadcaster = get_status_broadcaster()

    settings_to_update = {}
    if "delay_between_runs" in data:
        settings_to_update["delay_between_runs"] = data["delay_between_runs"]
    if "stop_on_error" in data:
        settings_to_update["stop_on_error"] = data["stop_on_error"]
    if "max_iterations" in data:
        settings_to_update["max_iterations"] = data["max_iterations"]

    updated_settings = await workflow_service.update_deployment_settings(settings_to_update)

    status = workflow_service.get_deployment_status()
    await broadcaster.broadcast(
        {
            "type": "deployment_settings_updated",
            "settings": updated_settings,
            "is_running": workflow_service.is_deployment_running(),
            "run_counter": status.get("run_counter", 0),
        }
    )

    return {
        "success": True,
        "settings": updated_settings,
        "is_running": workflow_service.is_deployment_running(),
        "run_counter": status.get("run_counter", 0),
        "active_runs": status.get("active_runs", 0),
        "timestamp": time.time(),
    }


def _control_service():
    from core.container import container

    return WorkflowControlService(container.database())


async def _start_controller(control) -> Optional[str]:
    """Start the durable controller, or use local mode when Temporal is disabled."""
    from core.container import container
    from temporalio.common import SearchAttributeKey, SearchAttributePair, TypedSearchAttributes

    wrapper = container.temporal_client()
    if wrapper is None or wrapper.client is None:
        if container.settings().temporal_enabled:
            raise RuntimeError("temporal_control_unavailable")
        return None
    handle = await wrapper.client.start_workflow(
        "WorkflowControlWorkflow",
        args=[{
            "workflow_id": control.workflow_id,
            "generation": control.generation,
            "execution_id": control.execution_id,
            "root_execution_id": control.root_execution_id,
            "data_scope_id": control.data_scope_id or control.execution_id,
            "state": "running",
        }],
        id=control.controller_workflow_id,
        task_queue=container.settings().temporal_task_queue,
        search_attributes=TypedSearchAttributes([
            SearchAttributePair(
                SearchAttributeKey.for_keyword("EventWorkflowId"), control.workflow_id,
            )
        ]),
    )
    return getattr(handle, "result_run_id", None) or getattr(handle, "first_execution_run_id", None)


def _controller_handle(control):
    from core.container import container

    wrapper = container.temporal_client()
    if wrapper is None or wrapper.client is None or not control.controller_workflow_id:
        return None
    return wrapper.client.get_workflow_handle(
        control.controller_workflow_id,
        run_id=control.controller_run_id,
    )


async def _signal_controller(control, signal_name: str) -> None:
    handle = _controller_handle(control)
    if handle is not None:
        await handle.signal(signal_name)


async def _update_controller_state(
    control,
    requested_state: str,
    *,
    update_id: str,
) -> Optional[Dict[str, Any]]:
    """Apply and await a durable controller state change.

    Temporal-disabled installations retain the local deployment path. When
    Temporal is enabled, losing its client is a failed mutation rather than a
    false-positive pause/resume acknowledgement.
    """
    from core.container import container

    handle = _controller_handle(control)
    if handle is None:
        if container.settings().temporal_enabled:
            raise TemporalControlUnavailable("temporal_control_unavailable")
        return None
    result = await handle.execute_update(
        "set_control_state",
        requested_state,
        id=update_id,
    )
    expected_state = "paused" if requested_state in {"pause", "paused"} else "running"
    if not isinstance(result, dict) or result.get("state") != expected_state:
        raise TemporalControlAckMismatch(
            f"temporal_control_ack_mismatch:{result.get('state') if isinstance(result, dict) else 'missing'}"
        )
    return result


async def _query_controller_state(control) -> Optional[Dict[str, Any]]:
    """Return controller state when reachable; status reads remain resilient.

    Raises :class:`ControllerExecutionMissing` when Temporal reports the
    execution does not exist, rather than folding that into the ``None``
    that means "could not reach it". Callers need to tell the two apart:
    one resolves itself, the other never will.
    """
    handle = _controller_handle(control)
    if handle is None:
        return None
    try:
        result = await handle.query("status")
        return result if isinstance(result, dict) else None
    except Exception as exc:
        if _temporal_target_already_gone(exc):
            # Expected and unremarkable: a controller closes when its
            # generation ends, and Temporal deletes closed executions once
            # the namespace retention window passes. Not a warning.
            logger.debug(
                "Workflow controller execution no longer exists",
                workflow_id=control.workflow_id,
                controller_workflow_id=control.controller_workflow_id,
                status=control.status,
            )
            raise ControllerExecutionMissing(
                str(control.controller_workflow_id)
            ) from exc
        logger.warning(
            "Workflow controller status query failed",
            workflow_id=control.workflow_id,
            controller_workflow_id=control.controller_workflow_id,
            error=str(exc),
        )
        return None


# A missing controller is only actionable for a generation the database still
# believes is alive. ``resetting`` is excluded deliberately: it already has an
# explicit retry path through Reset, and auto-failing it here could race a
# reset running concurrently in another request.
_MISSING_CONTROLLER_FAILS = frozenset(
    {"starting", "running", "pausing", "paused", "resuming"}
)


async def _fail_missing_controller(service: WorkflowControlService, control):
    """Converge a generation whose controller has vanished to ``failed``.

    Without this the row keeps whatever live status it had, and because
    ``begin_generation`` refuses to open a new generation unless the latest
    one is ``reset``, the workflow could never be started again. ``failed`` is
    both honest and recoverable -- Reset accepts it, and Reset is what returns
    the workflow to ``ready``.
    """
    if control.status not in _MISSING_CONTROLLER_FAILS:
        return control
    logger.warning(
        "Workflow controller execution is gone; failing the generation so it "
        "can be reset",
        workflow_id=control.workflow_id,
        controller_workflow_id=control.controller_workflow_id,
        status=control.status,
        generation=control.generation,
    )
    try:
        failed = await service.fail(control, "controller_execution_missing")
    except ValueError:
        # Lost the CAS to a concurrent writer, which means someone else has
        # already moved this row. Their transition wins; report what we have.
        return control
    await _broadcast_control(failed)
    return failed


def _generation_visibility_query(control) -> str:
    """Visibility query for controller descendants and standalone trigger roots."""
    if control.controller_workflow_id:
        root_id = str(control.controller_workflow_id).replace("'", "''")
        workflow_id = str(control.workflow_id).replace("'", "''")
        return (
            f"(RootWorkflowId='{root_id}' OR "
            f"EventWorkflowId='{workflow_id}') "
            "AND ExecutionStatus='Running'"
        )
    workflow_id = str(control.workflow_id).replace("'", "''")
    return f"EventWorkflowId='{workflow_id}' AND ExecutionStatus='Running'"


def _visibility_literal(value: Any) -> str:
    return str(value).replace("'", "''")


async def _list_generation_workflows(client, control) -> list[Any]:
    """Resolve tagged roots and every running descendant in those trees.

    Temporal Search Attributes are not inherited by child workflows. The
    first query therefore discovers the controller tree plus tagged standalone
    trigger/graph roots; a second batched RootWorkflowId query expands those
    roots to active Agent/DelegatedTask descendants.
    """
    targets: Dict[tuple[str, str], Any] = {}
    root_ids: set[str] = set()

    async for execution in client.list_workflows(
        query=_generation_visibility_query(control)
    ):
        execution_id = str(execution.id)
        run_id = str(getattr(execution, "run_id", "") or "")
        targets[(execution_id, run_id)] = execution
        root_ids.add(
            str(getattr(execution, "root_id", None) or execution_id)
        )

    ordered_roots = sorted(root_ids)
    batch_size = 40
    for offset in range(0, len(ordered_roots), batch_size):
        batch = ordered_roots[offset : offset + batch_size]
        root_values = ", ".join(
            f"'{_visibility_literal(root_id)}'"
            for root_id in batch
        )
        query = (
            f"RootWorkflowId IN ({root_values}) "
            "AND ExecutionStatus='Running'"
        )
        async for execution in client.list_workflows(query=query):
            execution_id = str(execution.id)
            run_id = str(getattr(execution, "run_id", "") or "")
            targets[(execution_id, run_id)] = execution

    return list(targets.values())


def _temporal_target_already_gone(exc: Exception) -> bool:
    try:
        from temporalio.service import RPCError, RPCStatusCode

        if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
            return True
    except Exception:
        pass
    message = str(exc).lower()
    return "not found" in message or "already completed" in message


async def _signal_generation_workflows(
    control,
    signal_name: str,
    *,
    strict: bool = False,
) -> int:
    """Best-effort cooperative fan-out to this deployment's live executions.

    Visibility is discovery only; durable control state remains authoritative.
    Every matching workflow receives an idempotent pause/resume flag mutation.
    """
    from core.container import container

    wrapper = container.temporal_client()
    if wrapper is None or wrapper.client is None:
        if strict and container.settings().temporal_enabled:
            raise TemporalControlUnavailable("temporal_control_unavailable")
        return 0
    signalled = 0
    failures: list[Exception] = []
    try:
        executions = await _list_generation_workflows(
            wrapper.client,
            control,
        )
        for execution in executions:
            try:
                await wrapper.client.get_workflow_handle(
                    execution.id, run_id=execution.run_id
                ).signal(signal_name)
                signalled += 1
            except Exception as exc:
                if not _temporal_target_already_gone(exc):
                    failures.append(exc)
                logger.warning(
                    "Workflow control signal failed",
                    workflow_id=control.workflow_id,
                    temporal_workflow_id=execution.id,
                    signal=signal_name,
                    error=str(exc),
                )
    except Exception as exc:
        logger.warning(
            "Workflow control visibility fan-out failed",
            workflow_id=control.workflow_id,
            signal=signal_name,
            error=str(exc),
        )
        if strict:
            raise RuntimeError(
                "workflow_signal_visibility_failed"
            ) from exc
    if strict and failures:
        raise RuntimeError(
            f"workflow_signal_failed:{len(failures)}"
        ) from failures[0]
    return signalled


async def _terminate_generation_workflows(control, *, strict: bool = False) -> int:
    """Immediately terminate every visible execution in one application tree."""
    from core.container import container

    wrapper = container.temporal_client()
    if wrapper is None or wrapper.client is None:
        if strict and container.settings().temporal_enabled:
            raise TemporalControlUnavailable("temporal_control_unavailable")
        return 0
    terminated = 0
    failures: list[Exception] = []
    try:
        executions = await _list_generation_workflows(
            wrapper.client,
            control,
        )
        for execution in executions:
            try:
                await wrapper.client.get_workflow_handle(
                    execution.id, run_id=execution.run_id
                ).terminate(reason="workflow_reset")
                terminated += 1
            except Exception as exc:
                if not _temporal_target_already_gone(exc):
                    failures.append(exc)
                logger.warning(
                    "Workflow reset termination failed",
                    workflow_id=control.workflow_id,
                    temporal_workflow_id=execution.id,
                    error=str(exc),
                )
    except Exception as exc:
        logger.warning(
            "Workflow reset visibility scan failed",
            workflow_id=control.workflow_id,
            error=str(exc),
        )
        if strict:
            raise RuntimeError("workflow_visibility_cleanup_failed") from exc
    if strict and failures:
        raise RuntimeError(
            f"workflow_termination_failed:{len(failures)}"
        ) from failures[0]
    return terminated


def _expected_revision(data: Dict[str, Any], control) -> int:
    supplied = data.get("expected_revision")
    if supplied is None:
        raise ValueError("expected_revision_required")
    return int(supplied)


async def _set_cron_pause(
    workflow_id: str,
    *,
    paused: bool,
    strict: bool = False,
) -> int:
    from core.container import container
    from services.temporal.schedules import set_cron_schedules_paused

    wrapper = container.temporal_client()
    if wrapper is None or wrapper.client is None:
        if strict and container.settings().temporal_enabled:
            raise TemporalControlUnavailable("temporal_control_unavailable")
        return 0
    return await set_cron_schedules_paused(
        wrapper.client,
        workflow_id,
        paused=paused,
        strict=strict,
    )


async def _delete_cron_schedules(
    workflow_id: str,
    *,
    strict: bool = False,
) -> int:
    from core.container import container
    from services.temporal.schedules import (
        delete_cron_schedules_for_deployment,
    )

    wrapper = container.temporal_client()
    if wrapper is None or wrapper.client is None:
        if strict and container.settings().temporal_enabled:
            raise TemporalControlUnavailable("temporal_control_unavailable")
        return 0
    return await delete_cron_schedules_for_deployment(
        wrapper.client,
        workflow_id,
        strict=strict,
    )


async def _with_runtime_counts(payload: Dict[str, Any], workflow_id: str) -> Dict[str, Any]:
    from core.container import container

    status = container.workflow_service().get_deployment_status(workflow_id)
    return {
        **payload,
        "active_count": status.get("active_runs", 0),
        "in_flight_count": status.get("active_runs", 0),
        "queued_count": (
            int(payload.get("queued_count", 0) or 0)
            + int(status.get("queued_events", 0) or 0)
        ),
    }


def _close_local_admission(workflow_id: str) -> None:
    """Synchronously gate legacy trigger callbacks before durable cleanup."""
    from core.container import container

    container.workflow_service().pause_deployment(workflow_id)


async def _control_payload(
    control,
    *,
    extra: Optional[Dict[str, Any]] = None,
    controller_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = serialize_control(control)
    if controller_status is not None:
        payload.update({
            "temporal_state": controller_status.get("state"),
            "temporal_revision": controller_status.get("revision"),
            "queued_count": controller_status.get("queued_events", 0),
            "temporal_available": True,
        })
    payload.update(extra or {})
    return await _with_runtime_counts(payload, control.workflow_id)


async def _broadcast_control(
    control,
    *,
    extra: Optional[Dict[str, Any]] = None,
    controller_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from services.status_broadcaster import get_status_broadcaster

    payload = await _control_payload(
        control,
        extra=extra,
        controller_status=controller_status,
    )
    await get_status_broadcaster().broadcast({
        "type": "workflow_control_status",
        "workflow_id": control.workflow_id,
        "data": payload,
    })
    return payload


async def _reconcile_control(service: WorkflowControlService, control):
    """Finish an interrupted DB transition from acknowledged Temporal state."""
    from core.container import container

    # A generation that has been reset (or failed) closed its controller on
    # purpose, and Temporal deletes closed executions once the namespace
    # retention window passes. Probing one is therefore a guaranteed-failing
    # RPC on every single status read, forever. Terminal generations are
    # answered from the database, which is authoritative for them anyway.
    if control.status not in ACTIVE_STATES:
        return control, None

    try:
        controller_status = await _query_controller_state(control)
    except ControllerExecutionMissing:
        return await _fail_missing_controller(service, control), None

    transition_target = {
        "pausing": ("paused", "paused"),
        "resuming": ("running", "running"),
    }.get(control.status)
    if transition_target is None:
        return control, controller_status
    requested_state, stable_state = transition_target

    if controller_status is None:
        if container.settings().temporal_enabled:
            return control, None
    if control.status == "pausing":
        container.workflow_service().pause_deployment(control.workflow_id)
    if controller_status is not None and controller_status.get("state") != stable_state:
        controller_status = await _update_controller_state(
            control,
            requested_state,
            update_id=(
                f"reconcile:{control.id}:{control.revision}:{stable_state}"
            ),
        )

    if control.status == "pausing":
        paused_schedules = await _set_cron_pause(
            control.workflow_id,
            paused=True,
            strict=True,
        )
        paused_triggers = await container.workflow_service().update_trigger_pause_status(
            control.workflow_id,
            paused=True,
        )
        signalled = await _signal_generation_workflows(
            control,
            "pause",
            strict=True,
        )
        transition_details = {
            "signalled_executions": signalled,
            "paused_schedules": paused_schedules,
            "paused_triggers": paused_triggers,
        }
    else:
        resumed_schedules = await _set_cron_pause(
            control.workflow_id,
            paused=False,
            strict=True,
        )
        signalled = await _signal_generation_workflows(
            control,
            "resume",
            strict=True,
        )
        queued = await container.workflow_service().resume_deployment(
            control.workflow_id,
        )
        resumed_triggers = await container.workflow_service().update_trigger_pause_status(
            control.workflow_id,
            paused=False,
        )
        transition_details = {
            "resumed_queued_events": queued,
            "signalled_executions": signalled,
            "resumed_schedules": resumed_schedules,
            "resumed_triggers": resumed_triggers,
        }
    try:
        control = await service.transition(
            control,
            expected_revision=control.revision,
            from_statuses={control.status},
            status=stable_state,
        )
        await _broadcast_control(
            control,
            controller_status=controller_status,
            extra=transition_details,
        )
    except ValueError as exc:
        if str(exc) != "control_revision_conflict":
            raise
        latest = await service.database.get_latest_workflow_control(control.workflow_id)
        if latest is not None and latest.generation == control.generation:
            control = latest
    return control, controller_status


async def _await_deployment_setup(workflow_id: str) -> Dict[str, Any]:
    """Wait for trigger/listener setup started by the legacy deploy handler."""
    task = _deployment_tasks.get(workflow_id)
    if task is None:
        return {
            "success": False,
            "error": "deployment_setup_task_missing",
            "workflow_id": workflow_id,
        }
    result = await asyncio.shield(task)
    if isinstance(result, dict):
        return result
    return {
        "success": False,
        "error": "deployment_setup_did_not_return_status",
        "workflow_id": workflow_id,
    }


async def _restore_control_after_failed_update(
    service: WorkflowControlService,
    control,
    *,
    transitional_state: str,
    stable_state: str,
):
    """Undo the DB projection when Temporal rejected a lifecycle update."""
    try:
        restored = await service.transition(
            control,
            expected_revision=control.revision,
            from_statuses={transitional_state},
            status=stable_state,
        )
    except ValueError:
        latest = await service.database.get_latest_workflow_control(control.workflow_id)
        restored = latest if latest is not None else control
    await _broadcast_control(restored)
    return restored


async def _duplicate_start_response(
    control,
    *,
    controller_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Report the durable outcome of a retried Start idempotency key."""
    payload = await _control_payload(
        control,
        controller_status=controller_status,
    )
    if control.status == "starting":
        return {
            "success": False,
            "error": "workflow_start_pending",
            "idempotent": True,
            **payload,
        }
    if control.status == "failed":
        return {
            "success": False,
            "error": control.terminal_reason or "workflow_start_failed",
            "idempotent": True,
            **payload,
        }
    return {"success": True, "idempotent": True, **payload}


@ws_handler("workflow_id")
async def handle_get_workflow_control_status(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    service = _control_service()
    control = await service.database.get_latest_workflow_control(data["workflow_id"])
    if control is None:
        return await _with_runtime_counts(
            serialize_control(None),
            data["workflow_id"],
        )
    control, controller_status = await _reconcile_control(service, control)
    return await _control_payload(
        control,
        controller_status=controller_status,
        extra={
            "temporal_available": (
                controller_status is not None
                if control.controller_run_id
                else False
            ),
        },
    )


@ws_handler("workflow_id")
async def handle_start_workflow(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    """Create generation one and retain deploy_workflow wire compatibility."""
    workflow_id = data["workflow_id"]
    key = data.get("idempotency_key") or f"start:{workflow_id}:{uuid.uuid4().hex}"
    service = _control_service()
    existing = await service.database.get_workflow_control_by_idempotency_key(
        workflow_id,
        key,
    )
    if existing is not None:
        existing, controller_status = await _reconcile_control(service, existing)
        return await _duplicate_start_response(
            existing,
            controller_status=controller_status,
        )

    latest = await service.database.get_latest_workflow_control(workflow_id)
    if data.get("expected_revision") is None:
        raise ValueError("expected_revision_required")
    expected_revision = int(data["expected_revision"])
    if expected_revision != (latest.revision if latest else 0):
        raise ValueError("control_revision_conflict")

    control, created = await service.begin_generation(
        workflow_id=workflow_id, nodes=data.get("nodes", []), edges=data.get("edges", []),
        session_id=data.get("session_id", "default"), idempotency_key=key,
    )
    if not created:
        control, controller_status = await _reconcile_control(service, control)
        return await _duplicate_start_response(
            control,
            controller_status=controller_status,
        )
    await _broadcast_control(control)
    try:
        run_id = await _start_controller(control)
        if run_id:
            control = await service.transition(
                control, expected_revision=control.revision, from_statuses={"starting"}, status="starting",
                values={"controller_run_id": run_id},
            )
            await service.database.update_workflow_run_data_scope(
                control.data_scope_id or control.execution_id, temporal_run_id=run_id,
            )
            await _broadcast_control(control)
        # Runtime persistence is generation-scoped. The caller's session is
        # retained on the scope for provenance, but must never namespace node
        # outputs for a controlled run.
        deploy_data = {
            **data,
            "session_id": control.data_scope_id or control.execution_id,
            "execution_id": control.execution_id,
            "root_execution_id": control.root_execution_id,
        }
        deployed = await handle_deploy_workflow(deploy_data, websocket)
        if not deployed.get("success"):
            raise RuntimeError(str(deployed.get("error", "deployment_failed")))
        deployed = await _await_deployment_setup(workflow_id)
        if not deployed.get("success"):
            raise RuntimeError(str(deployed.get("error", "deployment_failed")))
        control = await service.transition(control, expected_revision=control.revision, from_statuses={"starting"}, status="running")
    except Exception as exc:
        latest = await service.database.get_latest_workflow_control(workflow_id)
        if latest is not None and latest.generation == control.generation:
            control = latest
        if control.status == "starting":
            control = await service.fail(control, str(exc))
        try:
            await _signal_controller(control, "reset")
        except Exception as reset_exc:
            logger.warning(
                "Failed to reset controller after deployment setup error",
                workflow_id=workflow_id,
                error=str(reset_exc),
            )
        await _terminate_generation_workflows(control)
        await _broadcast_control(control)
        raise

    # Reaching ``running`` commits successful deployment setup. Projection
    # failures after that point must not tear down a live durable generation;
    # the client can recover the committed state through its status resync.
    try:
        controller_status = await _query_controller_state(control)
    except ControllerExecutionMissing:
        # Same rule as the comment above: this is a projection read taken
        # after the generation committed. Reporting no controller state is
        # acceptable; propagating and unwinding a live generation is not.
        controller_status = None
    payload = await _broadcast_control(
        control,
        controller_status=controller_status,
    )
    return {"success": True, **payload}


@ws_handler("workflow_id")
async def handle_pause_workflow(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    from core.container import container

    workflow_id = data["workflow_id"]
    service = _control_service()
    control = await service.database.get_latest_workflow_control(workflow_id)
    if control is None:
        return {"success": False, "error": "workflow_never_started"}
    control, controller_status = await _reconcile_control(service, control)
    if control.status == "paused":
        return {
            "success": True,
            "idempotent": True,
            **await _control_payload(control, controller_status=controller_status),
        }
    if control.status == "pausing":
        return {
            "success": False,
            "error": "workflow_control_transition_pending",
            **await _control_payload(control, controller_status=controller_status),
        }
    control = await service.transition(
        control, expected_revision=_expected_revision(data, control), from_statuses={"running"}, status="pausing"
    )
    await _broadcast_control(control)
    container.workflow_service().pause_deployment(workflow_id)
    try:
        controller_status = await _update_controller_state(
            control,
            "paused",
            update_id=(
                f"{control.id}:{control.revision}:"
                f"{data.get('idempotency_key') or uuid.uuid4().hex}:paused"
            ),
        )
    except (TemporalControlUnavailable, TemporalControlAckMismatch):
        await container.workflow_service().resume_deployment(workflow_id)
        await _restore_control_after_failed_update(
            service,
            control,
            transitional_state="pausing",
            stable_state="running",
        )
        raise
    paused_schedules = await _set_cron_pause(
        workflow_id,
        paused=True,
        strict=True,
    )
    paused_triggers = await container.workflow_service().update_trigger_pause_status(workflow_id, paused=True)
    signalled = await _signal_generation_workflows(
        control,
        "pause",
        strict=True,
    )
    control = await service.transition(control, expected_revision=control.revision, from_statuses={"pausing"}, status="paused")
    payload = await _broadcast_control(control, controller_status=controller_status, extra={
        "signalled_executions": signalled,
        "paused_schedules": paused_schedules,
        "paused_triggers": paused_triggers,
    })
    return {
        "success": True, "signalled_executions": signalled,
        "paused_schedules": paused_schedules, "paused_triggers": paused_triggers,
        **payload,
    }


@ws_handler("workflow_id")
async def handle_resume_workflow(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    from core.container import container

    workflow_id = data["workflow_id"]
    service = _control_service()
    control = await service.database.get_latest_workflow_control(workflow_id)
    if control is None:
        return {"success": False, "error": "workflow_never_started"}
    control, controller_status = await _reconcile_control(service, control)
    if control.status == "running":
        return {
            "success": True,
            "idempotent": True,
            **await _control_payload(control, controller_status=controller_status),
        }
    if control.status == "resuming":
        return {
            "success": False,
            "error": "workflow_control_transition_pending",
            **await _control_payload(control, controller_status=controller_status),
        }
    control = await service.transition(
        control, expected_revision=_expected_revision(data, control), from_statuses={"paused"}, status="resuming"
    )
    await _broadcast_control(control)
    try:
        controller_status = await _update_controller_state(
            control,
            "running",
            update_id=(
                f"{control.id}:{control.revision}:"
                f"{data.get('idempotency_key') or uuid.uuid4().hex}:running"
            ),
        )
    except (TemporalControlUnavailable, TemporalControlAckMismatch):
        await _restore_control_after_failed_update(
            service,
            control,
            transitional_state="resuming",
            stable_state="paused",
        )
        raise
    resumed_schedules = await _set_cron_pause(
        workflow_id,
        paused=False,
        strict=True,
    )
    signalled = await _signal_generation_workflows(
        control,
        "resume",
        strict=True,
    )
    queued = await container.workflow_service().resume_deployment(workflow_id)
    resumed_triggers = await container.workflow_service().update_trigger_pause_status(workflow_id, paused=False)
    control = await service.transition(control, expected_revision=control.revision, from_statuses={"resuming"}, status="running")
    payload = await _broadcast_control(control, controller_status=controller_status, extra={
        "resumed_queued_events": queued,
        "signalled_executions": signalled,
        "resumed_schedules": resumed_schedules,
        "resumed_triggers": resumed_triggers,
    })
    return {
        "success": True, "resumed_queued_events": queued, "signalled_executions": signalled,
        "resumed_schedules": resumed_schedules, "resumed_triggers": resumed_triggers,
        **payload,
    }


@ws_handler("workflow_id")
async def handle_reset_workflow(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
    workflow_id = data["workflow_id"]
    service = _control_service()
    current = await service.database.get_latest_workflow_control(workflow_id)
    if current is None:
        return await _with_runtime_counts(
            await service.get_status(workflow_id),
            workflow_id,
        )

    # ``reset`` is a completed cleanup barrier. Re-running generation-wide
    # sweeps here would race a concurrent Start and could terminate resources
    # from the next generation because standalone triggers use the stable
    # application workflow id.
    if current.status == "reset":
        return {
            "success": True,
            "idempotent": True,
            **await _control_payload(current),
        }

    if current.status != "resetting":
        current = await service.transition(
            current, expected_revision=_expected_revision(data, current),
            from_statuses={"starting", "running", "pausing", "paused", "resuming", "failed"}, status="resetting",
            values={"terminal_reason": "workflow_reset", "completed_at": datetime.now(timezone.utc)},
        )

    # Quiesce every producer before the final execution sweep. The local gate
    # is synchronous, so no callback can be admitted while broadcasts or
    # Temporal cleanup yield control.
    _close_local_admission(workflow_id)
    await _broadcast_control(current)

    try:
        await _signal_controller(current, "reset")
    except Exception as exc:
        logger.warning(
            "Workflow reset signal failed; continuing with termination",
            workflow_id=workflow_id,
            error=str(exc),
        )

    # Remove cron producers before terminating executions; otherwise a firing
    # between the execution scan and schedule deletion could survive Reset.
    deleted_schedules = await _delete_cron_schedules(
        workflow_id,
        strict=True,
    )
    cancelled = await handle_cancel_deployment(
        {"workflow_id": workflow_id},
        websocket,
    )
    # Durable Temporal cleanup above is authoritative. The process-local
    # deployment may legitimately be absent after an API-server restart.
    # Any other local teardown failure can leave a listener/admission path
    # alive, so keep the durable control in ``resetting`` for a safe retry.
    local_cleanup_completed = bool(cancelled.get("success"))
    if not local_cleanup_completed:
        # ``handle_cancel_deployment`` retains its historical envelope and
        # exposes manager errors as ``message``. Accept ``error`` as well for
        # direct/internal callers and future wire compatibility.
        local_error = str(
            cancelled.get("error")
            or cancelled.get("message")
            or "unknown"
        )
        expected_absent_error = f"Workflow {workflow_id} is not deployed"
        if local_error != expected_absent_error:
            raise RuntimeError(
                f"workflow_local_cleanup_failed:{local_error}"
            )

    # Controller, cron, and legacy local admission paths are now closed. This
    # final strict sweep therefore observes a fixed set of generation runs.
    terminated = await _terminate_generation_workflows(current, strict=True)

    archived = await service.database.update_workflow_run_data_scope(
        current.data_scope_id or current.execution_id,
        status="archived", archived_at=datetime.now(timezone.utc),
    )
    if not archived:
        raise RuntimeError("workflow_data_scope_archive_failed")

    from services.status_broadcaster import get_status_broadcaster
    from services.deployment.runtime_state import archive_and_reset_node_state
    broadcaster = get_status_broadcaster()
    node_state = await archive_and_reset_node_state(
        current, service.database, broadcaster,
    )

    current = await service.transition(
        current, expected_revision=current.revision, from_statuses={"resetting"}, status="reset",
        values={"terminal_reason": "workflow_reset", "completed_at": datetime.now(timezone.utc)},
    )
    await broadcaster.broadcast({
        "type": "workflow_runtime_reset",
        "workflow_id": workflow_id,
        "generation": current.generation,
        "data_scope_id": current.data_scope_id or current.execution_id,
        "archived_nodes": node_state["archived_nodes"],
        "reset_nodes": node_state["reset_nodes"],
    })
    payload = await _broadcast_control(current, extra={
        "terminated_executions": terminated,
        "deleted_schedules": deleted_schedules,
        "local_cleanup_completed": local_cleanup_completed,
        "archived_nodes": node_state["archived_nodes"],
        "reset_nodes": node_state["reset_nodes"],
    })
    return {
        "success": True,
        "idempotent": False,
        "terminated_executions": terminated,
        "deleted_schedules": deleted_schedules,
        "local_cleanup_completed": local_cleanup_completed,
        "archived_nodes": node_state["archived_nodes"],
        "reset_nodes": node_state["reset_nodes"],
        **payload,
    }


WS_HANDLERS: Dict[str, Any] = {
    "deploy_workflow": handle_deploy_workflow,
    "cancel_deployment": handle_cancel_deployment,
    "get_deployment_status": handle_get_deployment_status,
    "get_workflow_lock": handle_get_workflow_lock,
    "update_deployment_settings": handle_update_deployment_settings,
    "start_workflow": handle_start_workflow,
    "pause_workflow": handle_pause_workflow,
    "resume_workflow": handle_resume_workflow,
    "reset_workflow": handle_reset_workflow,
    "get_workflow_control_status": handle_get_workflow_control_status,
}


__all__ = [
    "WS_HANDLERS",
    "handle_cancel_deployment",
    "handle_deploy_workflow",
    "handle_get_deployment_status",
    "handle_get_workflow_lock",
    "handle_update_deployment_settings",
]
