"""Best-effort cleanup for provider resources owned by a Context epoch."""

from __future__ import annotations

from core.logging import get_logger


logger = get_logger(__name__)


async def fence_context_provider_resources(
    *,
    context_node_id: str,
    thread_id: str,
    keep_epoch: int,
) -> None:
    """Terminate warm provider state that belongs to an older epoch.

    The durable epoch in :class:`AgentContextStore` is authoritative.  Pool
    cleanup is intentionally best-effort because the provider bridge also
    rejects stale epoch keys when they are acquired.
    """

    try:
        from services.cli_agent.factory import get_session_pool

        pool = get_session_pool("claude")
        terminate_context = getattr(pool, "terminate_context", None)
        if terminate_context is not None:
            await terminate_context(
                context_node_id,
                thread_id=thread_id,
                keep_epoch=keep_epoch,
            )
    except Exception as exc:
        logger.warning(
            "[Context] provider resource cleanup failed for %s/%s: %s",
            context_node_id,
            thread_id,
            exc,
        )


__all__ = ["fence_context_provider_resources"]
