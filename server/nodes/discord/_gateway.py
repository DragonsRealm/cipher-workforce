"""Gateway connections, one per Discord bot account.

discord.py owns the socket. What it handles is exactly the part that is
miserable to re-derive and fails silently when wrong: zlib-stream framing,
heartbeat/ACK tracking with jitter, RESUME versus IDENTIFY on the right close
codes, resume_gateway_url, and IDENTIFY concurrency. A hand-rolled client
stays "connected" and quietly stops receiving messages hours later.

REST is deliberately not borrowed from it -- see _base. The two never cross:
gateway traffic goes through the client here, every REST call goes through
_base, and each has its own rate limiting. Calling ``client.http`` from node
code would put requests behind a limiter that knows nothing about the
process-wide invalid-request budget.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from core.logging import get_logger
from services._supervisor import register_supervisor
from services._supervisor.base import BaseSupervisor, RestartPolicy
from services.plugin import NodeUserError

from ._accounts import DEFAULT_ACCOUNT, resolve_secrets

logger = get_logger(__name__)

# How long to wait for READY before treating the connection as failed.
READY_TIMEOUT_SECONDS = 60.0

# Close conditions that no amount of retrying will fix. Feeding these to the
# restart policy would hammer Discord and burn the 1000-IDENTIFY daily budget,
# which ends in a reset token and an email to the app owner.
_TERMINAL_ERRORS = ("PrivilegedIntentsRequired", "LoginFailure")


def _resolve_intents(discord_module: Any) -> Any:
    """The intent set every Discord node needs.

    Computed in one place because ``Intents`` must be final before the client
    is constructed -- adding one later means restarting every connection. A
    future capability (voice, presence) widens this function and nothing else.

    message_content is privileged. Without it approved in the Developer Portal
    the gateway still connects and simply delivers empty ``content``, which is
    why the credential probe reports on it rather than leaving it to surface
    as "the Discord node returns blank text".
    """
    intents = discord_module.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.dm_messages = True
    intents.message_content = True
    intents.reactions = True
    return intents


class DiscordGateway(BaseSupervisor):
    """One live gateway connection for one bot account."""

    def __init__(self, account_id: str = DEFAULT_ACCOUNT) -> None:
        super().__init__()
        self.account_id = account_id
        # Distinct per account: the supervisor registry keys on this, and a
        # shared label would mean the second account silently replaced the
        # first.
        self.name = f"discord-gateway:{account_id}"
        self._client: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._terminal_error: Optional[str] = None

    # ---- lifecycle ------------------------------------------------------

    def is_running(self) -> bool:
        return self._client is not None and not self._client.is_closed()

    async def _do_start(self) -> None:
        # Imported here, never at module scope: nodes/__init__.py swallows
        # import errors during discovery, so a failure at import time would
        # make the whole plugin vanish without a word.
        import discord

        secrets = await resolve_secrets(self.account_id)
        token = secrets["token"]

        client = discord.Client(intents=_resolve_intents(discord))
        self._register_handlers(client, discord)
        self._client = client

        # start(), never run(): run() installs signal handlers and would
        # fight uvicorn's own.
        self._task = asyncio.create_task(client.start(token), name=self.name)

        # Race readiness against the connection task rather than just
        # awaiting readiness. Login failures surface inside start(), so
        # waiting on wait_until_ready() alone would sit out the full timeout
        # and report "not ready" for what is really a rejected token.
        ready = asyncio.create_task(client.wait_until_ready())
        try:
            done, _ = await asyncio.wait(
                {ready, self._task},
                timeout=READY_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not ready.done():
                ready.cancel()

        if self._task in done:
            # start() returned or raised before the client became ready.
            failure = self._task.exception()
            await self._do_stop()
            if failure is not None:
                raise self._translate_start_error(failure) from failure
            raise NodeUserError("Discord gateway closed before it finished connecting.")

        if ready not in done:
            await self._do_stop()
            raise NodeUserError(
                f"Discord gateway did not become ready within {READY_TIMEOUT_SECONDS:.0f}s. "
                "Check that this host can reach discord.com."
            )

        logger.info(
            "discord gateway ready",
            account_id=self.account_id,
            user=str(getattr(client.user, "name", "")),
            guilds=len(getattr(client, "guilds", []) or []),
        )

    async def _do_stop(self) -> None:
        client, task = self._client, self._task
        self._client, self._task = None, None

        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.debug("discord gateway close failed", error=str(exc))

        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Ours, from the cancel above.
                pass
            except Exception as exc:
                # The task's own failure is already reported by whoever
                # awaited start(); losing it here would mask nothing.
                logger.debug("discord gateway task ended", error=str(exc))

    def _extra_status(self) -> Dict[str, Any]:
        client = self._client
        user = getattr(client, "user", None) if client else None
        latency = getattr(client, "latency", None) if client else None
        return {
            "account_id": self.account_id,
            "bot_username": str(getattr(user, "name", "")) if user else None,
            "bot_id": str(getattr(user, "id", "")) if user else None,
            "guild_count": len(getattr(client, "guilds", []) or []) if client else 0,
            # discord.py reports infinity before the first heartbeat.
            "latency_ms": round(latency * 1000) if latency and latency != float("inf") else None,
            "terminal_error": self._terminal_error,
        }

    # ---- errors ---------------------------------------------------------

    def _translate_start_error(self, exc: Exception) -> Exception:
        """Terminal failures must not be retried.

        A wrong token or an unapproved privileged intent fails identically on
        every attempt, so backing off just spends the daily IDENTIFY budget.
        """
        name = type(exc).__name__
        if name in _TERMINAL_ERRORS:
            self._terminal_error = name
            if name == "PrivilegedIntentsRequired":
                return NodeUserError(
                    "Discord refused the connection because a privileged intent is not "
                    "enabled. Turn on Message Content under Bot > Privileged Gateway "
                    "Intents in the Developer Portal."
                )
            return NodeUserError(
                "Discord rejected the bot token. Copy it again from the Developer "
                "Portal; resetting a token invalidates the old one."
            )
        return exc

    def can_retry(self) -> bool:
        """False once a terminal condition has been seen."""
        return self._terminal_error is None

    # ---- event wiring ---------------------------------------------------

    def _register_handlers(self, client: Any, discord_module: Any) -> None:
        account_id = self.account_id

        @client.event
        async def on_message(message: Any) -> None:  # noqa: ANN401 - discord.py type
            # The bot's own messages would otherwise loop straight back into
            # any workflow that replies.
            if client.user is not None and message.author.id == client.user.id:
                return
            from ._dispatch import dispatch_message

            try:
                await dispatch_message(message, account_id=account_id)
            except Exception as exc:
                # One malformed message must not kill the receive loop.
                logger.warning("discord message dispatch failed", error=str(exc))


# --------------------------------------------------------------------------
# Per-account registry
# --------------------------------------------------------------------------
#
# BaseSupervisor.get_instance() is a per-subclass singleton, so it cannot hold
# N connections. The registry below is the multi-account equivalent, and each
# gateway still registers with services._supervisor so shutdown_all_supervisors
# reaches it.

_GATEWAYS: Dict[str, DiscordGateway] = {}
_REGISTRY_LOCK = asyncio.Lock()


async def get_gateway(account_id: str = DEFAULT_ACCOUNT) -> DiscordGateway:
    """Return the gateway for one account, creating it on first use."""
    async with _REGISTRY_LOCK:
        gateway = _GATEWAYS.get(account_id)
        if gateway is None:
            gateway = DiscordGateway(account_id)
            _GATEWAYS[account_id] = gateway
            register_supervisor(gateway)
        return gateway


def running_gateways() -> Dict[str, DiscordGateway]:
    return {aid: gw for aid, gw in _GATEWAYS.items() if gw.is_running()}


def known_gateways() -> Dict[str, DiscordGateway]:
    return dict(_GATEWAYS)


async def stop_all_gateways() -> None:
    """Close every connection on shutdown.

    Sessions left open count against the account's concurrent-session limit
    until Discord times them out, which in a dev restart loop looks like a
    second instance that will not go away.
    """
    for gateway in list(_GATEWAYS.values()):
        try:
            await gateway.stop()
        except Exception as exc:
            logger.debug("discord gateway stop failed", label=gateway.label, error=str(exc))


__all__ = [
    "DiscordGateway",
    "READY_TIMEOUT_SECONDS",
    "RestartPolicy",
    "get_gateway",
    "known_gateways",
    "running_gateways",
    "stop_all_gateways",
]
