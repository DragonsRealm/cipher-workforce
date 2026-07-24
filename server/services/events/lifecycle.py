"""Generic lifecycle wiring helpers.

:func:`make_lifecycle_handlers` returns the standard 4 WebSocket
handlers (``connect`` / ``disconnect`` / ``reconnect`` / ``status``) for
any :class:`EventSource`. Plugins call it once and register the dict —
no per-handler boilerplate.

:func:`make_status_refresh` returns a ``register_service_refresh``
callback that passively mirrors the source's status into the
broadcaster cache (it never starts the source; see its docstring).

Together these collapse ~50 LOC of identical boilerplate per plugin
into two function calls.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, TYPE_CHECKING

from fastapi import WebSocket

from core.logging import get_logger

from .source import EventSource

if TYPE_CHECKING:
    from services.status_broadcaster import StatusBroadcaster

logger = get_logger(__name__)


def make_lifecycle_handlers(
    prefix: str,
    source: EventSource,
    *,
    extra: Dict[str, Callable[[Dict[str, Any], WebSocket], Awaitable[Dict[str, Any]]]] | None = None,
) -> Dict[str, Callable[[Dict[str, Any], WebSocket], Awaitable[Dict[str, Any]]]]:
    """Return ``{prefix}_connect / _disconnect / _reconnect / _status`` handlers.

    Pass ``extra`` to merge additional plugin-specific handlers (e.g.
    ``{"stripe_trigger": handle_stripe_trigger}``).
    """

    async def _connect(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
        return await source.start()

    async def _disconnect(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
        return await source.stop()

    async def _reconnect(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
        restart = getattr(source, "restart", None)
        if restart is not None:
            return await restart()
        await source.stop()
        return await source.start()

    async def _status(data: Dict[str, Any], websocket: WebSocket) -> Dict[str, Any]:
        status = await source.status()
        if hasattr(source, "has_credential"):
            status["has_stored_key"] = await source.has_credential()  # type: ignore[attr-defined]
        return {"success": True, "status": status}

    handlers: Dict[str, Callable[[Dict[str, Any], WebSocket], Awaitable[Dict[str, Any]]]] = {
        f"{prefix}_connect": _connect,
        f"{prefix}_disconnect": _disconnect,
        f"{prefix}_reconnect": _reconnect,
        f"{prefix}_status": _status,
    }
    if extra:
        handlers.update(extra)
    return handlers


def make_status_refresh(
    source: EventSource,
    *,
    status_key: str,
    broadcast_type: str,
) -> Callable[["StatusBroadcaster"], Awaitable[None]]:
    """Return a ``register_service_refresh`` callback.

    The callback is a passive probe: it mirrors the source's status into
    ``broadcaster._status[status_key]`` and emits a ``broadcast_type``
    broadcast. It never starts the source — a stored credential alone is
    not a reason to run an optional daemon. Sources start on demand:
    user-initiated ``{prefix}_connect`` / login handlers, and trigger-node
    deploy prechecks (e.g. ``StripeReceiveNode._check_precondition``).
    """

    async def refresh(broadcaster: "StatusBroadcaster") -> None:
        try:
            status = await source.status()
            broadcaster._status[status_key] = status
            await broadcaster.broadcast({"type": broadcast_type, "data": status})
        except Exception as e:
            logger.debug("[StatusBroadcaster] %s refresh failed: %s", status_key, e)

    return refresh
