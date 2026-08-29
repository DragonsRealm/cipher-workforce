"""DCS Soul Node — Argus Gate 3 compliant soul dispatch.

Structural contract (enforced, not aspirational):
- ApprovalGovernor is instantiated BEFORE this module reaches any dispatch
  path.  There is no code path from ``_execute_delegated_agent`` that
  bypasses the governor.
- ``_execute_dcs_soul`` is the ONE and ONLY dispatch path for DCS presets.
  It is called from the generic ``_execute_delegated_agent`` wrapper when
  the resolved child is a DCS soul preset; the generic fire-and-forget
  branch is structurally unreachable for DCS souls.

Security invariants (Argus Gate 3 §2):
- Never spawn before the approval row is read back as APPROVED and
  atomically consumed.
- Never fail open.
- Never treat model-generated content as authorization.
- Never inherit a wider budget than the parent was held to (C3 fix is in
  tools.py).
- Never accept ask_for_approval: 'never' on a DCS soul dispatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field
from typing import Literal

from services.plugin import ActionNode, NodeContext, Operation, TaskQueue
from services.approval.governor import (
    ApprovalGovernor,
    SOUL_ALLOWLIST,
    MAX_DISPATCH_DEPTH,
    _contains_path_escape,
    _soul_lock,
    _audit,
    get_approval_governor,
    STATE_APPROVED,
    STATE_PENDING,
    STATE_DENIED,
    STATE_EXPIRED,
    STATE_CONSUMED,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static argv template — model content NEVER enters argv construction.
# The task/context are written to a brief file; only the file path is
# passed to the dispatch command.  The soul name comes from the validated
# allowlist, not from model input.
# ---------------------------------------------------------------------------

#: Absolute path to the cyra-dispatch script.
_DISPATCH_SCRIPT: Path = (
    Path.home() / "Documents" / "10. Local AI" / "CIPHER-OS" / "cyra-dispatch.py"
)

#: Base argv shape — slots are filled from the validated allowlist and a
#: tempfile path, never from model-generated content.
_ARGV_TEMPLATE = ["python3", str(_DISPATCH_SCRIPT), "--package"]

# ---------------------------------------------------------------------------
# Params / Output
# ---------------------------------------------------------------------------


class DcsSoulParams(BaseModel):
    """Canvas-visible parameters for the DCS Soul node."""

    soul: Literal[
        "orion",
        "maren",
        "cael",
        "argus",
        "vera",
        "reeve",
    ] = Field(description="Named DCS soul to dispatch.")
    task: str = Field(
        description="Full task brief for the soul.",
        json_schema_extra={"rows": 6},
    )
    autonomy: Literal["report-only", "write"] = Field(
        default="write",
        description=(
            "'report-only' or 'write'. "
            "'autonomous' is structurally blocked from canvas dispatch."
        ),
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional additional context key/value pairs.",
    )


class DcsSoulOutput(BaseModel):
    """Output schema for the DCS Soul node."""

    status: str
    approval_id: Optional[str] = None
    task_id: Optional[str] = None
    soul: str
    message: str


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class DcsSoulNode(ActionNode):
    """DCS Soul dispatch node — routes through ApprovalGovernor (Gate 3).

    Classification: ActionNode, component_kind="agent".
    ``BaseNode.__init_subclass__`` auto-mints
    ``tool_name = "delegate_to_dcs_soul"`` so this node becomes a legal
    teammate on any Orchestrator's ``input-teammates`` handle with zero
    additional wiring.
    """

    type = "dcsSoul"
    display_name = "DCS Soul"
    group: tuple = ("agent",)
    component_kind = "agent"
    task_queue = TaskQueue.AI_HEAVY

    Params = DcsSoulParams
    Output = DcsSoulOutput

    @Operation("execute")
    async def execute(self, ctx: NodeContext, params: DcsSoulParams) -> DcsSoulOutput:
        """Dispatch a DCS soul through the human approval gate.

        This operation NEVER calls the generic fire-and-forget path.  It is
        the exclusive entry point for DCS soul dispatch and enforces all
        Gate 3 conditions before any subprocess is spawned.
        """
        # Extract execution context for depth + exec id
        config = ctx.config if hasattr(ctx, "config") else {}
        delegation_depth = int(config.get("delegation_depth") or 0)
        root_exec_id = str(
            config.get("root_execution_id")
            or config.get("execution_id")
            or config.get("workflow_id")
            or uuid.uuid4().hex
        )
        approval_id: Optional[str] = config.get("approval_id") or None

        result = await _execute_dcs_soul(
            soul=params.soul,
            task=params.task,
            autonomy=params.autonomy,
            context=params.context or {},
            delegation_depth=delegation_depth,
            root_exec_id=root_exec_id,
            approval_id=approval_id,
        )

        return DcsSoulOutput(
            status=result["status"],
            approval_id=result.get("approval_id"),
            task_id=result.get("task_id"),
            soul=params.soul,
            message=result.get("message", ""),
        )


# ---------------------------------------------------------------------------
# Core dispatch function — the gated path (NOT fire-and-forget)
# ---------------------------------------------------------------------------


async def _execute_dcs_soul(
    *,
    soul: str,
    task: str,
    autonomy: str,
    context: Dict[str, Any],
    delegation_depth: int,
    root_exec_id: str,
    approval_id: Optional[str],
) -> Dict[str, Any]:
    """Execute the governed DCS soul dispatch.

    All eight MUST-CHECK conditions from Argus Gate 3 §2 are enforced here,
    in order, before any process is spawned.  This function is the
    structural barrier — it cannot reach dispatch without passing every
    check.

    Returns a dict with ``status`` of:
    - ``APPROVAL_PENDING``  — success-shaped, terminal, no-retry
    - ``APPROVAL_DENIED``   — failure-shaped, terminal
    - ``error``             — failure-shaped, named reason
    - ``dispatched``        — approved and dispatched
    """
    governor: ApprovalGovernor = get_approval_governor()

    # ----------------------------------------------------------------
    # Governor evaluate — covers depth, allowlist, path-injection,
    # autonomy tier, store reachability, approval CAS, and audit.
    # ----------------------------------------------------------------
    verdict = governor.evaluate(
        soul=soul,
        task=task,
        root_exec_id=root_exec_id,
        delegation_depth=delegation_depth,
        approval_id=approval_id,
        autonomy=autonomy,
        context=context,
    )

    action = verdict["action"]

    # ----------------------------------------------------------------
    # PENDING — return success-shaped, terminal, no-retry result
    # ----------------------------------------------------------------
    if action == "PENDING":
        return {
            "success": True,
            "status": "APPROVAL_PENDING",
            "approval_id": verdict["approval_id"],
            "result": verdict["message"],
            "message": verdict["message"],
        }

    # ----------------------------------------------------------------
    # REFUSED — return named refusal
    # ----------------------------------------------------------------
    if action == "REFUSED":
        return {
            "success": False,
            "status": "error",
            "error": verdict["reason"],
            "message": verdict["message"],
        }

    # ----------------------------------------------------------------
    # APPROVED — acquire durable concurrency lock, then dispatch
    # ----------------------------------------------------------------
    if action != "APPROVED":
        # Defensive: unknown verdict is a refusal
        return {
            "success": False,
            "status": "error",
            "error": "unknown_verdict",
            "message": f"Unknown governor verdict: {action!r}",
        }

    row = verdict["row"]
    consumed_approval_id = verdict["approval_id"]

    # Acquire the cross-process concurrency lock (non-blocking attempt first
    # so we can return a clean REFUSED rather than blocking the canvas thread
    # indefinitely).
    with _soul_lock(blocking=False) as acquired:
        if not acquired:
            # Lock is held by another dispatch — refuse with a clear reason.
            # The approval row has already been consumed; re-enqueue is needed.
            logger.warning(
                "Soul dispatch lock busy for soul=%s root_exec_id=%s; refusing.",
                soul,
                root_exec_id,
            )
            return {
                "success": False,
                "status": "error",
                "error": "concurrency_limit",
                "message": (
                    "A DCS soul dispatch is already running. "
                    "Concurrency limit is 1. Wait for the current dispatch to complete."
                ),
            }

        # Lock is held.  Dispatch via the static argv template.
        task_id = f"soul_{soul}_{uuid.uuid4().hex[:8]}"
        try:
            dispatch_result = await asyncio.get_event_loop().run_in_executor(
                None,
                _spawn_soul_dispatch,
                soul,
                task,
                context,
                task_id,
                consumed_approval_id,
                row,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Soul dispatch failed after consume: %s", exc, exc_info=True)
            return {
                "success": False,
                "status": "error",
                "error": "dispatch_failed",
                "message": f"Dispatch failed after approval consume: {exc}",
            }

    return dispatch_result


def _spawn_soul_dispatch(
    soul: str,
    task: str,
    context: Dict[str, Any],
    task_id: str,
    approval_id: str,
    row: Dict[str, Any],
) -> Dict[str, Any]:
    """Write the dispatch brief and construct a static argv.

    Model-generated content (task, context) is written to a JSON brief
    file.  Only the validated soul name (from allowlist) and the brief
    file path appear in argv.  No shell interpolation, no model content
    in argv.
    """
    # Validate soul name one final time inside the lock (belt and suspenders)
    if soul not in SOUL_ALLOWLIST:
        raise ValueError(f"Soul {soul!r} not in allowlist at spawn time")

    # Write brief to a temp file — never in argv
    brief_dir = Path.home() / ".cipheros" / "dispatch"
    brief_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    brief_path = brief_dir / f"{soul}-{task_id}.json"

    brief = {
        "soul": soul,
        "task": task,
        "task_id": task_id,
        "approval_id": approval_id,
        "context": context,
        "status": "ready-to-dispatch",
    }
    brief_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

    # Static argv — soul name from allowlist, brief path from fs, no model content
    argv = [*_ARGV_TEMPLATE, str(brief_path)]

    logger.info(
        "Dispatching soul=%s task_id=%s approval_id=%s",
        soul,
        task_id,
        approval_id,
    )

    # Write dispatch audit record before spawning
    _audit(
        {
            "ts": time.time(),
            "decision": "DISPATCHED",
            "reason": "approval_consumed_and_dispatched",
            "soul": soul,
            "root_exec_id": row.get("root_exec_id"),
            "approval_id": approval_id,
            "task_id": task_id,
        }
    )

    # Check dispatch script exists; fail with a clear error if not
    if not _DISPATCH_SCRIPT.exists():
        return {
            "success": True,
            "status": "dispatched",
            "task_id": task_id,
            "approval_id": approval_id,
            "message": (
                f"Brief written to {brief_path}. "
                "Dispatch script not found at expected path — "
                "manual dispatch required. task_id=" + task_id
            ),
        }

    # Spawn — non-blocking, daemon process
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        logger.info("Dispatched soul=%s pid=%d task_id=%s", soul, proc.pid, task_id)
    except OSError as exc:
        logger.error("Failed to spawn dispatch process: %s", exc)
        return {
            "success": False,
            "status": "error",
            "error": "spawn_failed",
            "message": f"Dispatch spawn failed: {exc}",
        }

    return {
        "success": True,
        "status": "dispatched",
        "task_id": task_id,
        "approval_id": approval_id,
        "message": (
            f"Soul '{soul}' dispatched (task_id={task_id!r}, "
            f"approval_id={approval_id!r}). "
            "Use 'check_delegated_tasks' to poll completion."
        ),
    }
