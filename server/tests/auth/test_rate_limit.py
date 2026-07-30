"""Unit tests for SlidingWindowLimiter, driven by an injected clock."""

from __future__ import annotations

import pytest

from core.rate_limit import _MAX_TRACKED_KEYS, SlidingWindowLimiter

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _limiter(clock, *, max_events=3, window_seconds=60.0):
    return SlidingWindowLimiter(max_events=max_events, window_seconds=window_seconds, clock=clock)


class TestConstruction:
    @pytest.mark.parametrize("max_events", [0, -1])
    def test_rejects_non_positive_max_events(self, clock, max_events):
        with pytest.raises(ValueError):
            _limiter(clock, max_events=max_events)

    @pytest.mark.parametrize("window", [0, -5])
    def test_rejects_non_positive_window(self, clock, window):
        with pytest.raises(ValueError):
            _limiter(clock, window_seconds=window)


class TestHit:
    def test_allows_up_to_the_limit(self, clock):
        limiter = _limiter(clock)
        assert [limiter.hit("k") for _ in range(3)] == [True, True, True]

    def test_blocks_past_the_limit(self, clock):
        limiter = _limiter(clock)
        for _ in range(3):
            limiter.hit("k")
        assert limiter.hit("k") is False

    def test_keys_are_independent(self, clock):
        limiter = _limiter(clock, max_events=1)
        assert limiter.hit("a") is True
        assert limiter.hit("b") is True
        assert limiter.hit("a") is False

    def test_recovers_after_the_window(self, clock):
        limiter = _limiter(clock, max_events=1, window_seconds=60.0)
        assert limiter.hit("k") is True
        assert limiter.hit("k") is False
        clock.advance(61)
        assert limiter.hit("k") is True

    def test_window_slides_rather_than_resetting(self, clock):
        limiter = _limiter(clock, max_events=2, window_seconds=60.0)
        limiter.hit("k")          # t=0
        clock.advance(30)
        limiter.hit("k")          # t=30
        assert limiter.hit("k") is False
        clock.advance(31)         # t=61 -- the t=0 hit has aged out
        assert limiter.hit("k") is True
        assert limiter.hit("k") is False

    def test_blocked_attempts_are_not_recorded(self, clock):
        """Otherwise a client retrying while blocked extends its own lockout
        forever, turning the limit into a self-inflicted denial of service."""
        limiter = _limiter(clock, max_events=1, window_seconds=60.0)
        limiter.hit("k")                  # t=0, recorded
        clock.advance(30)
        assert limiter.hit("k") is False  # must NOT record at t=30
        clock.advance(31)                 # t=61, the only real hit has expired
        assert limiter.hit("k") is True


class TestReset:
    def test_reset_clears_a_key(self, clock):
        limiter = _limiter(clock, max_events=1)
        limiter.hit("k")
        assert limiter.hit("k") is False
        limiter.reset("k")
        assert limiter.hit("k") is True

    def test_reset_of_unknown_key_is_safe(self, clock):
        _limiter(clock).reset("never-seen")


class TestRetryAfter:
    def test_zero_when_untracked(self, clock):
        assert _limiter(clock).retry_after("k") == 0

    def test_counts_down(self, clock):
        limiter = _limiter(clock, max_events=1, window_seconds=60.0)
        limiter.hit("k")
        first = limiter.retry_after("k")
        clock.advance(30)
        assert limiter.retry_after("k") < first

    def test_always_positive_while_blocked(self, clock):
        limiter = _limiter(clock, max_events=1, window_seconds=60.0)
        limiter.hit("k")
        assert limiter.retry_after("k") > 0


class TestEviction:
    def test_key_count_stays_bounded(self, clock):
        """An attacker rotating source addresses must not grow the dict
        without limit."""
        limiter = _limiter(clock, max_events=1, window_seconds=1.0)
        for i in range(_MAX_TRACKED_KEYS + 50):
            limiter.hit(f"key-{i}")
            # Age keys out as we go so the stale sweep has work to do.
            if i % 100 == 0:
                clock.advance(2)
        assert len(limiter._events) <= _MAX_TRACKED_KEYS

    def test_insertion_still_succeeds_when_nothing_is_stale(self, clock):
        """With every key fresh the stale sweep frees nothing, so the oldest
        key is dropped instead -- a new key must never be refused outright."""
        limiter = _limiter(clock, max_events=5, window_seconds=10_000.0)
        for i in range(_MAX_TRACKED_KEYS):
            limiter.hit(f"key-{i}")
            clock.advance(0.001)
        assert limiter.hit("brand-new") is True
        assert len(limiter._events) <= _MAX_TRACKED_KEYS
