"""Discord rate-limit bookkeeping. Pure state, no IO.

Reactive rather than predictive: this reads 429 responses and holds requests
that follow, but does not track ``X-RateLimit-Remaining`` to sleep before
hitting zero. Preemptive per-bucket accounting is a real optimisation and a
real pile of state; it can be added when 429s actually show up.

Three scopes, and conflating any two of them is a bug:

* per account -- the global 50 req/s ceiling is per bot token.
* per account -- a global 429 pauses that token's traffic entirely.
* **per process** -- the invalid-request ban is enforced by Cloudflare on the
  source IP, not the token. Three misconfigured bots on one host share one
  budget, and blowing it bans the host including the healthy accounts. This is
  the piece most likely to get "simplified" into the per-account limiter later.
  It must not be.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

# Discord's documented global ceiling for a bot token.
GLOBAL_REQUESTS_PER_SECOND = 50

# Cloudflare bans an IP for 10 minutes past 10,000 invalid responses (401,
# 403, 429) in a 10-minute window. Stopping early is the whole point: the ban
# takes out every account on the host, so local failure is strictly cheaper.
INVALID_REQUEST_WINDOW_SECONDS = 600
INVALID_REQUEST_LIMIT = 10_000
INVALID_REQUEST_SAFETY_MARGIN = 9_000
_INVALID_STATUSES = frozenset({401, 403, 429})

# Past this, holding an activity slot open costs more than failing and letting
# Temporal's own backoff re-dispatch. Well under the 10-minute activity budget.
MAX_INLINE_WAIT_SECONDS = 30.0


class RateLimitExceeded(RuntimeError):
    """A wait too long to sit through. Retryable: Temporal re-dispatches.

    Deliberately not NodeUserError -- that type is non-retryable, so raising
    one here would turn a transient throttle into a permanent failure.
    """

    def __init__(self, retry_after: float, scope: str = "") -> None:
        super().__init__(
            f"Discord rate limit: retry after {retry_after:.1f}s"
            + (f" (scope={scope})" if scope else "")
        )
        self.retry_after = retry_after
        self.scope = scope


class InvalidRequestBudgetExhausted(RuntimeError):
    """Local stop before Cloudflare bans the host IP."""


class _InvalidRequestGuard:
    """Process-wide, shared across every account. See the module docstring."""

    def __init__(self) -> None:
        self._events: list[float] = []

    def record(self, status_code: int, now: Optional[float] = None) -> None:
        if status_code not in _INVALID_STATUSES:
            return
        moment = now if now is not None else time.monotonic()
        self._events.append(moment)
        self._prune(moment)

    def _prune(self, now: float) -> None:
        cutoff = now - INVALID_REQUEST_WINDOW_SECONDS
        self._events = [t for t in self._events if t >= cutoff]

    def count(self, now: Optional[float] = None) -> int:
        moment = now if now is not None else time.monotonic()
        self._prune(moment)
        return len(self._events)

    def check(self, now: Optional[float] = None) -> None:
        if self.count(now) >= INVALID_REQUEST_SAFETY_MARGIN:
            raise InvalidRequestBudgetExhausted(
                "Too many rejected Discord requests from this host in the last "
                "10 minutes. Continuing would trigger a Cloudflare IP ban "
                "affecting every Discord account on this server. Check that "
                "stored bot tokens are valid and that the bot has access to "
                "the channels being used."
            )


_INVALID_REQUEST_GUARD = _InvalidRequestGuard()


def invalid_request_guard() -> _InvalidRequestGuard:
    return _INVALID_REQUEST_GUARD


class AccountLimiter:
    """Per-token pacing and global-429 hold."""

    def __init__(self, requests_per_second: int = GLOBAL_REQUESTS_PER_SECOND) -> None:
        self._min_interval = 1.0 / max(requests_per_second, 1)
        self._last_request = 0.0
        self._hold_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Pace one request, waiting out any active global hold."""
        async with self._lock:
            now = time.monotonic()
            wait = max(self._hold_until - now, self._last_request + self._min_interval - now)
            if wait > MAX_INLINE_WAIT_SECONDS:
                raise RateLimitExceeded(wait, "global")
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def hold(self, seconds: float) -> None:
        self._hold_until = max(self._hold_until, time.monotonic() + seconds)


def parse_retry_after(payload: Optional[dict], headers: Optional[dict]) -> float:
    """Seconds to wait after a 429.

    The JSON body carries sub-second precision; the ``Retry-After`` header is
    rounded to whole seconds, so it is only the fallback. Both are attacker-
    adjacent input, so a malformed value degrades to 1s rather than raising.
    """
    if payload:
        value = payload.get("retry_after")
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    if headers:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        try:
            parsed = float(raw)  # type: ignore[arg-type]
            if parsed >= 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return 1.0


def is_global_limit(payload: Optional[dict], headers: Optional[dict]) -> bool:
    if payload and payload.get("global") is True:
        return True
    headers = headers or {}
    scope = headers.get("X-RateLimit-Scope") or headers.get("x-ratelimit-scope")
    return scope == "global"


def is_cloudflare_ban(headers: Optional[dict]) -> bool:
    """A 429 that is not Discord's.

    Discord's own 429 is always JSON. An HTML one is the edge rejecting the
    host IP, which is a different problem with a different remedy -- and one
    that looks like an hour of unexplained rate limiting if reported as a
    normal throttle.
    """
    content_type = (headers or {}).get("Content-Type") or (headers or {}).get("content-type") or ""
    return "application/json" not in content_type.lower()


_LIMITERS: Dict[str, AccountLimiter] = {}


def limiter_for(account_id: str) -> AccountLimiter:
    """Per-account limiter, created on first use and kept for the process."""
    limiter = _LIMITERS.get(account_id)
    if limiter is None:
        limiter = AccountLimiter()
        _LIMITERS[account_id] = limiter
    return limiter


__all__ = [
    "AccountLimiter",
    "InvalidRequestBudgetExhausted",
    "MAX_INLINE_WAIT_SECONDS",
    "RateLimitExceeded",
    "invalid_request_guard",
    "is_cloudflare_ban",
    "is_global_limit",
    "limiter_for",
    "parse_retry_after",
]
