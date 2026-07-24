"""Client / in-process-task supervisor.

For services that don't own a subprocess but still have a lifecycle:
HTTP clients pointed at an externally-spawned daemon,
or in-process tasks like the python-telegram-bot polling loop.
"""

from __future__ import annotations

from typing import Any, Optional

import aiohttp

from .base import BaseSupervisor


class BaseClientSupervisor(BaseSupervisor):
    """A :class:`BaseSupervisor` that owns an aiohttp session.

    Subclasses with non-HTTP lifecycles (e.g. polling tasks) override
    ``_do_start`` / ``_do_stop`` / ``is_running`` directly and don't have
    to use ``_session`` at all.
    """

    # Subclasses with an HTTP backend set this; pure-task subclasses
    # leave it empty and override ``health_check``.
    base_url: str = ""

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None

    def is_running(self) -> bool:
        return self._session is not None and not self._session.closed

    async def _do_start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _do_stop(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def health_check(self) -> bool:
        if not self.base_url or not self.is_running():
            return self.is_running()
        try:
            async with self._session.get(f"{self.base_url.rstrip('/')}/health") as resp:
                return 200 <= resp.status < 300
        except aiohttp.ClientError:
            return False

    def _extra_status(self) -> dict[str, Any]:
        return {"base_url": self.base_url or None}
