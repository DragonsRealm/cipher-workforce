"""Workflow execution routes."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Dict, Any

from constants import OWNER_PRINCIPAL_ID
from core.container import container
from services.workflow import WorkflowService
from services.status_broadcaster import get_status_broadcaster
from core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/workflow", tags=["workflow"])


@router.get("/node-output/{node_id}")
async def get_workflow_node_output(
    node_id: str,
    output_name: str = "output_0",
    session_id: str = "default",
    workflow_service: WorkflowService = Depends(lambda: container.workflow_service()),
):
    """Get stored output data for a node."""
    return await workflow_service.get_workflow_node_output(node_id, output_name, session_id)


@router.delete("/clear-outputs")
async def clear_workflow_outputs(
    session_id: str = "default", workflow_service: WorkflowService = Depends(lambda: container.workflow_service())
):
    """Clear all stored node outputs for a session."""
    try:
        await workflow_service.clear_all_outputs(session_id)
        return {"success": True, "message": f"Cleared all outputs for session: {session_id}"}
    except Exception as e:
        logger.error("Failed to clear outputs", session_id=session_id, error=str(e), exc_info=True)
        return {"success": False, "error": "Failed to clear outputs"}


@router.get("/health")
async def workflow_health_check():
    """Workflow service health check."""
    return {"status": "OK", "service": "workflow"}


class TemporalNodeExecuteRequest(BaseModel):
    """Request from Temporal service to execute a single node."""

    node_id: str
    node_type: str
    data: Dict[str, Any] = {}
    context: Dict[str, Any] = {}


class BroadcastStatusRequest(BaseModel):
    """Request to broadcast node status from Temporal activity."""

    node_id: str
    status: str
    data: Dict[str, Any] = {}
    workflow_id: str = None


@router.post("/broadcast-status")
async def broadcast_status_for_temporal(request: BroadcastStatusRequest):
    """Broadcast node status - called by Temporal activities for real-time updates.

    This endpoint allows Temporal activities to send status updates
    to connected WebSocket clients during workflow execution.
    """
    logger.debug(f"Broadcast: {request.node_id} -> {request.status} (workflow={request.workflow_id})")

    broadcaster = get_status_broadcaster()
    await broadcaster.update_node_status(
        node_id=request.node_id,
        status=request.status,
        data=request.data,
        workflow_id=request.workflow_id,
    )

    return {"success": True}


@router.post("/node/execute")
async def execute_node_for_temporal(
    request: TemporalNodeExecuteRequest, workflow_service: WorkflowService = Depends(lambda: container.workflow_service())
):
    """Execute a single node - called by Temporal service.

    This is a simplified endpoint for Temporal activities to call.
    It extracts context and delegates to the existing execute_node method.
    """
    logger.debug(f"Temporal execute_node: {request.node_id} (type={request.node_type})")

    context = request.context
    try:
        invocation_extras = {
            key: context[key]
            for key in (
                "auto_rebind_tools",
                "invoking_agent_node_id",
                "agent_iteration",
                "tool_call_index",
                "tool_call_id",
            )
            if key in context
        }
        result = await workflow_service.execute_node(
            node_id=request.node_id,
            node_type=request.node_type,
            parameters=request.data,
            nodes=context.get("nodes", []),
            edges=context.get("edges", []),
            session_id=context.get("session_id", "temporal"),
            execution_id=context.get("execution_id"),
            workflow_id=context.get("workflow_id"),
            # The caller is a Temporal activity relaying an identity minted
            # server-side at deploy time. Without this the node executes as
            # the anonymous owner regardless of who deployed the workflow.
            user_id=str(context.get("user_id") or OWNER_PRINCIPAL_ID),
            extras=invocation_extras or None,
        )
        logger.info(
            "Temporal endpoint node result",
            node_id=request.node_id,
            success=result.get("success"),
            error=result.get("error"),
        )
        return result
    except Exception:
        logger.exception(
            "Temporal endpoint node execution failed",
            node_id=request.node_id,
        )
        raise
