"""Which handlers the unauthenticated internal socket may reach.

``/ws/internal`` exists so a Temporal activity worker can call back into
the running backend. It is in ``PUBLIC_PATHS`` and performs no handshake,
so it has no authenticated principal at all — yet it dispatched through
the same registry as the authenticated socket, which meant every handler
was reachable: ``save_workflow``, ``delete_workflow``, and all six Memory
handlers among them.

This is a deny-by-default allowlist rather than a per-handler opt-out
because per-handler opt-out has already failed here once: the Context
handler checked the socket path and its Memory sibling did not. An
allowlist inverts the default, so a newly added handler is closed until
someone edits this named constant.

Splitting the handler registry in two was considered and rejected: 40+
plugin ``__init__`` modules self-register into one registry, so a split
forces every plugin author to classify their handler, and whichever
registry is the default is wrong half the time — with the wrong default
being a silent privilege grant.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

#: Everything the activity worker legitimately needs. Both execute
#: handlers already carry their own ``/ws/internal`` identity branch.
INTERNAL_SOCKET_HANDLERS: frozenset[str] = frozenset(
    {
        "execute_node",
        "execute_ai_node",
        "ping",
    }
)


def resolve_internal_handler(
    msg_type: str,
    resolver: Callable[[str], Optional[Any]],
) -> Optional[Any]:
    """Resolve ``msg_type`` for the internal socket, or return ``None``.

    A refused type returns ``None`` so the caller emits its ordinary
    unknown-message-type response. Refusal and "no such handler" are
    deliberately indistinguishable — a distinct error would tell a prober
    that ``save_workflow`` exists here but is forbidden.
    """
    if msg_type not in INTERNAL_SOCKET_HANDLERS:
        return None
    return resolver(msg_type)


def execution_principal(data: Mapping[str, Any], websocket: Any) -> str:
    """Resolve the identity an execution runs as.

    One implementation for every execute handler. They previously had three
    slightly different copies: two honoured a payload-supplied ``user_id``
    on the internal worker socket and one did not, so the same request
    executed as a different principal depending on which handler received
    it.

    A payload-supplied ``user_id`` is trusted ONLY on ``/ws/internal``,
    where the sender is a Temporal activity relaying identity that was
    minted server-side at deploy time. On any authenticated socket the
    handshake identity wins and the payload is ignored.
    """
    from constants import OWNER_PRINCIPAL_ID

    scope = getattr(websocket, "scope", {}) or {}
    if str(scope.get("path") or "") == "/ws/internal":
        candidate = data.get("user_id")
    else:
        candidate = getattr(getattr(websocket, "state", None), "user_id", None)
    return str(candidate or OWNER_PRINCIPAL_ID)


__all__ = [
    "INTERNAL_SOCKET_HANDLERS",
    "execution_principal",
    "resolve_internal_handler",
]
