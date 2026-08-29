"""Dynamic webhook endpoint router for incoming HTTP requests.

Two dispatch paths:

1. **Plugin-owned WebhookSource** (Wave 12 framework). Plugins register
   a :class:`services.events.WebhookSource` for their path and shape a
   :class:`WorkflowEvent` envelope.  The router enforces HMAC
   verification at the router level before calling ``source.handle()``
   (Argus Addendum 8: default-deny).  A source may declare
   ``skip_signature_check = True`` to bypass verification (e.g. GET
   handshake sources, challenge-response sources).
2. **Legacy generic webhook** (pre-framework). Falls through to
   ``broadcast_webhook_received`` so existing ``webhookTrigger`` nodes
   keep working untouched.
   # TODO Phase 5: enforce per-path HMAC on legacy path too.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from typing import Dict
import asyncio
import logging

from services.events import WEBHOOK_SOURCES
import services.webhook_store as webhook_store
# Wave 12 B9: webhook event dispatch moved to
# ``nodes/trigger/webhook_trigger/_events.broadcast_webhook_received``.
# The router no longer reaches into the broadcaster directly.

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

# Pending responses: path -> asyncio.Future (for responseNode mode)
_pending_responses: Dict[str, asyncio.Future] = {}


def resolve_webhook_response(node_id: str, response_data: dict):
    """Resolve a pending webhook response.

    Called by webhookResponse node execution to send response back to caller.
    Uses path from response_data to find the pending Future.
    """
    # Find pending response by path (stored when we started waiting)
    for path, future in list(_pending_responses.items()):
        if not future.done():
            future.set_result(response_data)
            logger.info(f"[Webhook] Response resolved for path: {path}")
            return

    logger.warning(f"[Webhook] No pending response found for node: {node_id}")


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def handle_webhook(path: str, request: Request):
    """Handle incoming webhook requests.

    Path-handler dispatch (Wave 12) runs first; legacy generic
    dispatch is the fallback for paths nobody has claimed.
    """
    source = WEBHOOK_SOURCES.get(path)
    if source is not None:
        try:
            if request.method == "GET":
                # Subscription handshakes (Meta's hub.challenge echo, and
                # equivalents) need a provider-specific body that is not
                # this router's fixed JSON. A source that declines returns
                # None and falls through to the normal handle() path, so
                # sources without an override are unaffected.
                handshake = await source.handle_get(request)
                if handshake is not None:
                    logger.info("[Webhook] GET %s -> %s handshake", path, type(source).__name__)
                    return handshake

            # --- Argus Addendum 8: default-deny HMAC enforcement ---
            # Read body once here (FastAPI caches it; source.handle() can
            # still call await request.body() internally).
            # GET requests are subscription handshakes / challenge-response flows
            # and carry no signed body — skip HMAC for all GETs.
            skip_sig = (
                request.method == "GET"
                or getattr(source, "skip_signature_check", False)
            )
            if not skip_sig:
                body = await request.body()
                sig_header = (
                    request.headers.get("x-hub-signature-256")
                    or request.headers.get("x-signature-256")
                    or request.headers.get("x-webhook-signature")
                )
                if not webhook_store.verify_hmac(body, sig_header):
                    logger.warning(
                        "[Webhook] %s %s -> 401 signature_required (%s)",
                        request.method, path, type(source).__name__,
                    )
                    return JSONResponse(
                        {"error": "webhook_signature_required"},
                        status_code=401,
                    )

            await source.handle(request)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("[Webhook] %s handler crashed: %s", path, e)
            raise HTTPException(status_code=500, detail=str(e))
        logger.info("[Webhook] %s %s -> %s", request.method, path, type(source).__name__)
        return JSONResponse({"status": "received", "path": path}, status_code=200)

    body = await request.body()
    json_body = None
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type and body:
        try:
            json_body = await request.json()
        except Exception:
            pass

    webhook_data = {
        "method": request.method,
        "path": path,
        "headers": dict(request.headers),
        "query": dict(request.query_params),
        "body": body.decode("utf-8") if isinstance(body, bytes) else (body if body else ""),
        "json": json_body,
    }

    logger.info(f"[Webhook] Received: {request.method} /webhook/{path}")

    # Wave 12 B9: route through plugin _events.py wrapper.
    from nodes.trigger.webhook_trigger._events import broadcast_webhook_received

    await broadcast_webhook_received(webhook_data)

    return JSONResponse(
        content={"status": "received", "path": path, "message": "Webhook received and dispatched to workflow"}, status_code=200
    )


@router.get("/")
async def list_info():
    """Get webhook endpoint info."""
    return {
        "endpoint": "/webhook/{path}",
        "description": "Send HTTP requests to trigger webhookTrigger nodes",
        "usage": "Deploy a workflow with webhookTrigger node, then send requests to /webhook/{path}",
        "example": "POST /webhook/my-webhook with JSON body",
    }
