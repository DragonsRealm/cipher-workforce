"""Save notifications for durable conversation changes.

``save_conversation`` is the one place every writer passes through — the
in-process agent loop, the Temporal LLM activity, and the CLI-agent bridge.
Emitting the "conversation advanced" notification there means a new writer
gets live panel updates for free and no caller carries broadcast code.

Layering: this service must never import ``nodes/`` (plugin
self-containment), so it owns a fanout registry and the Context plugin
registers its broadcaster from ``nodes/context/__init__.py`` — the same
pattern as the other plugin-owned registries.

Contract, and it is load-bearing: :func:`notify_conversation_saved` is
best-effort. It never raises and never blocks on anything slower than an
in-process broadcast — saves happen inside the Temporal LLM activity's
post-send window, where a throwing or slow listener would fail a run over
a UI notification.
"""

from __future__ import annotations

from typing import Awaitable, Callable, List

from core.logging import get_logger
from services.plugin.registry import IdempotentList

logger = get_logger(__name__)

ConversationListener = Callable[..., Awaitable[None]]

_LISTENERS: List[ConversationListener] = []
_FANOUT: IdempotentList[ConversationListener] = IdempotentList(
    "agent_conversation_saved",
    items=_LISTENERS,
)


def register_conversation_listener(listener: ConversationListener) -> None:
    """Register a callback fired after a conversation durably saves.

    Idempotent on re-import. Listeners are invoked with keyword arguments
    only, so adding a field later does not break an existing listener.
    """

    _FANOUT.register(listener)


async def notify_conversation_saved(
    *,
    workflow_id: str,
    generation: int,
    agent_node_id: str,
    message_count: int,
) -> None:
    """Announce a durable save. Called only after a successful commit."""

    if not _LISTENERS:
        return

    for listener in list(_LISTENERS):
        try:
            await listener(
                workflow_id=workflow_id,
                generation=generation,
                agent_node_id=agent_node_id,
                message_count=message_count,
            )
        except Exception:  # noqa: BLE001 — a notification may never fail a save
            # WARNING, not debug: a silently failing listener is exactly how
            # "the panel never updates live" becomes undiagnosable — the save
            # succeeds, the run continues, and nothing in the operator log
            # hints that the broadcast chain is broken.
            logger.warning(
                "Conversation listener failed",
                listener=getattr(listener, "__qualname__", repr(listener)),
                exc_info=True,
            )


__all__ = [
    "ConversationListener",
    "notify_conversation_saved",
    "register_conversation_listener",
]
