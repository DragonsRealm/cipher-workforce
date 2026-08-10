"""Commit notifications for durable Context state changes.

The store is the one place that knows a Context thread advanced, and it is
the only place every writer passes through -- the in-process agent loop, the
Temporal LLM activity, and the CLI-agent bridge all reach durable state via
:class:`~services.agent_context.store.AgentContextStore`. Emitting the
"thread advanced" notification here rather than at each call site means a new
writer gets live UI updates for free and no caller carries broadcast code.

Layering: the store must never import ``nodes/`` (plugin self-containment).
So this module owns a fanout registry and the Context plugin registers its
broadcaster from ``nodes/context/__init__.py``, exactly like the other
plugin-owned registries (``register_service_refresh``,
``register_output_schema``, ...).

Contract, and it is load-bearing: :func:`notify_context_commit` is
best-effort. It never raises, never rolls back the commit that triggered it,
and never blocks on anything slower than an in-process broadcast. These
commits happen inside the Temporal LLM activity's post-send window, which is
heartbeat-silent under a 60s ``heartbeat_timeout`` on a ``maximum_attempts=1``
retry policy -- an expensive or throwing listener there would fail a run over
a UI notification.
"""

from __future__ import annotations

from typing import Awaitable, Callable, List, Optional

from core.logging import get_logger
from models.agent_context import AgentContextRef
from services.plugin.registry import IdempotentList

logger = get_logger(__name__)

ContextCommitListener = Callable[..., Awaitable[None]]

_LISTENERS: List[ContextCommitListener] = []
_FANOUT: IdempotentList[ContextCommitListener] = IdempotentList(
    "agent_context_commit",
    items=_LISTENERS,
)


def register_context_commit_listener(listener: ContextCommitListener) -> None:
    """Register a callback fired after a Context thread durably advances.

    Idempotent on re-import (registering the same callable twice is a no-op).
    Listeners are invoked with keyword arguments only, so adding a field later
    does not break an existing listener.
    """

    _FANOUT.register(listener)


async def notify_context_commit(
    ref: AgentContextRef,
    *,
    provider: Optional[str],
    active_token_count: int,
    sequence: Optional[int] = None,
) -> None:
    """Announce that ``ref`` now points at newly committed durable state.

    Call this only *after* a successful commit and *after* reloading the ref,
    so the notification can never be observed ahead of the state it describes
    and always carries the post-commit revision.

    Never call it on the idempotent-replay path: a replay is not a state
    change, and waking the UI for one produces a phantom update.
    """

    if not _LISTENERS:
        return

    for listener in list(_LISTENERS):
        try:
            await listener(
                ref=ref,
                provider=provider,
                active_token_count=active_token_count,
                sequence=sequence,
            )
        except Exception:  # noqa: BLE001 -- a notification may never fail a commit
            logger.debug(
                "Context commit listener failed",
                listener=getattr(listener, "__qualname__", repr(listener)),
                exc_info=True,
            )


__all__ = [
    "ContextCommitListener",
    "notify_context_commit",
    "register_context_commit_listener",
]
