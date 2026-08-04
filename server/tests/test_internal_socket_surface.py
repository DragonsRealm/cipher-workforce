"""`/ws/internal` is unauthenticated — it must reach almost nothing.

It performs no handshake and sets no principal, yet it dispatched through
the same registry as the authenticated socket, so `save_workflow`,
`delete_workflow` and all six Memory handlers were reachable without
credentials.
"""

from __future__ import annotations

import inspect


def test_allowlist_is_exactly_what_the_worker_needs():
    from services.authz import INTERNAL_SOCKET_HANDLERS

    assert INTERNAL_SOCKET_HANDLERS == {
        "execute_node",
        "execute_ai_node",
        "ping",
    }, "widening this set grants unauthenticated access — justify it in review"


def test_every_other_registered_handler_is_refused():
    """Generated from the LIVE registry, so it cannot go stale.

    This is the check that would have caught the six Memory handlers.
    """
    import nodes  # noqa: F401 - populates the plugin handler registry
    from routers.websocket import MESSAGE_HANDLERS, _resolve_handler
    from services.authz import INTERNAL_SOCKET_HANDLERS, resolve_internal_handler
    from services.ws_handler_registry import get_ws_handlers

    every = set(MESSAGE_HANDLERS) | set(get_ws_handlers())
    assert len(every) > 50, "registry looks unpopulated; the guard would be vacuous"

    reachable = {
        name for name in every
        if resolve_internal_handler(name, _resolve_handler) is not None
    }
    assert reachable <= INTERNAL_SOCKET_HANDLERS

    # Spot-check the ones that actually matter.
    for dangerous in (
        "save_workflow",
        "delete_workflow",
        "get_workflow",
        "list_memory_items",
        "clear_memory_items",
        "remember_memory",
        "get_agent_context",
    ):
        if dangerous in every:
            assert resolve_internal_handler(dangerous, _resolve_handler) is None, (
                f"{dangerous} is reachable on the unauthenticated socket"
            )


def test_refusal_is_indistinguishable_from_unknown_type():
    """A distinct error would confirm to a prober that a handler exists."""
    from routers.websocket import _resolve_handler
    from services.authz import resolve_internal_handler

    assert resolve_internal_handler("save_workflow", _resolve_handler) is None
    assert resolve_internal_handler("no_such_handler_at_all", _resolve_handler) is None


def test_internal_loop_actually_uses_the_gate():
    """Guards against the router being refactored back to the raw resolver."""
    import routers.websocket as ws

    source = inspect.getsource(ws)
    internal = source[source.index("WebSocket Internal") - 4000 :]
    assert "resolve_internal_handler(msg_type, _resolve_handler)" in internal
