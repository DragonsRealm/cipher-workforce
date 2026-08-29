"""Human Approval Surface — Argus Gate 3 (auth-gated, fail-closed).

This router exposes three endpoints for a human approver to list, approve,
or reject pending DCS soul dispatch requests.

Security contract (non-negotiable):
- All endpoints require Authorization: Bearer <token>
- Token is read from env var APPROVAL_SURFACE_SECRET at request time
- If APPROVAL_SURFACE_SECRET is not set: 503 on all three endpoints
- If header is missing or wrong token: 401 on all three endpoints
- NEVER approve or reject on auth failure — fail closed

Smoke-test curl examples (set TOKEN and SERVER as appropriate):

  TOKEN="$CIPHER_APPROVAL_TOKEN"
  SERVER="http://localhost:5678"

  # List pending approvals
  curl -s -H "Authorization: Bearer $TOKEN" $SERVER/api/approvals | python3 -m json.tool

  # Approve a specific approval
  curl -s -X POST -H "Authorization: Bearer $TOKEN" \
    $SERVER/api/approvals/APPROVAL_ID_HERE/approve | python3 -m json.tool

  # Reject a specific approval
  curl -s -X POST -H "Authorization: Bearer $TOKEN" \
    $SERVER/api/approvals/APPROVAL_ID_HERE/reject | python3 -m json.tool
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from services.approval.governor import (
    HumanApprovalQueue,
    STATE_PENDING,
    _open_db,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

_UNCONFIGURED_DETAIL = "Approval surface not configured — set CIPHER_APPROVAL_TOKEN"


def _check_auth(authorization: Optional[str]) -> None:
    """Validate the Bearer token.  Raises HTTPException on any failure.

    Fail-closed contract (Argus Gate 3):
    - env var not set → 503 (surface not configured)
    - header missing or wrong token → 401
    Never returns without raising when auth fails.
    """
    secret = os.environ.get("CIPHER_APPROVAL_TOKEN")
    if not secret:
        raise HTTPException(status_code=503, detail=_UNCONFIGURED_DETAIL)

    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not hmac.compare_digest(secret, parts[1]):
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_pending_approvals(
    authorization: Optional[str] = Header(default=None),
) -> List[Dict[str, Any]]:
    """Return all PENDING, non-expired approval rows.

    Requires: Authorization: Bearer <APPROVAL_SURFACE_SECRET>
    """
    _check_auth(authorization)

    now = time.time()
    try:
        conn = _open_db()
        try:
            rows = conn.execute(
                """
                SELECT approval_id, soul, created_at, expires_at, root_exec_id, notes
                FROM soul_approvals
                WHERE state = ? AND expires_at > ?
                ORDER BY created_at ASC
                """,
                (STATE_PENDING, now),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("Approval store unavailable in list endpoint: %s", exc)
        raise HTTPException(status_code=503, detail="Approval store unavailable")

    result = []
    for row in rows:
        approval_id, soul, created_at, expires_at, root_exec_id, notes_raw = row

        # Parse notes safely; never fail the whole list on one bad row
        autonomy = "unknown"
        context: Dict[str, Any] = {}
        if notes_raw:
            try:
                notes = json.loads(notes_raw)
                autonomy = notes.get("autonomy", "unknown")
                context = notes.get("context", {}) or {}
            except (json.JSONDecodeError, AttributeError):
                pass

        result.append(
            {
                "approval_id": approval_id,
                "soul": soul,
                "autonomy": autonomy,
                "context": context,
                "created_at": created_at,
                "expires_at": expires_at,
                "root_exec_id": root_exec_id,
            }
        )

    return result


@router.post("/{approval_id}/approve")
async def approve_dispatch(
    approval_id: str,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Grant approval for a pending soul dispatch.

    Requires: Authorization: Bearer <APPROVAL_SURFACE_SECRET>
    Returns 200 on success, 409 if not PENDING, 503 on store error.
    """
    _check_auth(authorization)

    try:
        approved = HumanApprovalQueue().approve(approval_id, approver="dragon")
    except RuntimeError as exc:
        logger.error("Approval store error on approve: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "reason": "store_unavailable"},
        )

    if approved:
        logger.info("Human approval granted for approval_id=%s", approval_id)
        return JSONResponse(
            status_code=200,
            content={"ok": True, "approval_id": approval_id},
        )
    else:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "reason": "not_pending"},
        )


@router.post("/{approval_id}/reject")
async def reject_dispatch(
    approval_id: str,
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    """Deny a pending soul dispatch.

    Requires: Authorization: Bearer <APPROVAL_SURFACE_SECRET>
    Returns 200 on success, 409 if not PENDING, 503 on store error.
    """
    _check_auth(authorization)

    try:
        denied = HumanApprovalQueue().deny(approval_id, approver="dragon")
    except RuntimeError as exc:
        logger.error("Approval store error on reject: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "reason": "store_unavailable"},
        )

    if denied:
        logger.info("Human approval denied for approval_id=%s", approval_id)
        return JSONResponse(
            status_code=200,
            content={"ok": True, "approval_id": approval_id},
        )
    else:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "reason": "not_pending"},
        )
