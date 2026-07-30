"""In-process sliding-window rate limiter.

Exists because `POST /api/auth/login` is public, unauthenticated, and was
completely unthrottled -- an unlimited online brute-force surface.

Deliberately dependency-free rather than pulling in `slowapi`: the primary
runtime (`cli/commands/serve.py`) is a single uvicorn process, so a per-process
counter is exact. Under the legacy gunicorn path (`server/gunicorn.conf.py`)
each worker keeps its own counter, making the effective limit
`workers * max_events` -- still a hard ceiling, just a looser one. If this ever
needs to be exact across processes, back it with the existing `CacheService`
rather than adding a dependency.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Deque, Dict

# Upper bound on tracked keys. An attacker rotating source addresses would
# otherwise grow the dict without limit; at the cap the coldest keys are
# dropped, which fails open for those keys rather than exhausting memory.
_MAX_TRACKED_KEYS = 10_000


class SlidingWindowLimiter:
    """Allow at most ``max_events`` hits per ``window_seconds`` per key."""

    def __init__(
        self,
        *,
        max_events: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max_events = max_events
        self._window = float(window_seconds)
        self._clock = clock
        self._events: Dict[str, Deque[float]] = {}

    def hit(self, key: str) -> bool:
        """Record an attempt. Returns True if allowed, False if over the limit.

        A rejected attempt is NOT recorded -- otherwise a client that keeps
        retrying while blocked would extend its own lockout indefinitely,
        which turns a rate limit into a denial of service against the
        legitimate owner of that key.
        """
        now = self._clock()
        cutoff = now - self._window

        bucket = self._events.get(key)
        if bucket is None:
            if len(self._events) >= _MAX_TRACKED_KEYS:
                self._evict_stale(cutoff)
            bucket = self._events.setdefault(key, deque())

        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self._max_events:
            return False

        bucket.append(now)
        return True

    def reset(self, key: str) -> None:
        """Forget a key. Called after a successful login so a user who
        finally gets their password right is not still throttled."""
        self._events.pop(key, None)

    def retry_after(self, key: str) -> int:
        """Whole seconds until the oldest recorded hit for ``key`` expires."""
        bucket = self._events.get(key)
        if not bucket:
            return 0
        remaining = (bucket[0] + self._window) - self._clock()
        return max(0, int(remaining) + 1)

    def _evict_stale(self, cutoff: float) -> None:
        """Drop keys whose events have all aged out; if that frees nothing,
        drop the single oldest key so insertion can always proceed."""
        stale = [k for k, v in self._events.items() if not v or v[-1] <= cutoff]
        for key in stale:
            self._events.pop(key, None)
        if not stale and self._events:
            oldest = min(self._events, key=lambda k: self._events[k][-1])
            self._events.pop(oldest, None)
