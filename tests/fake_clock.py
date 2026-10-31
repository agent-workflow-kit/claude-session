"""A manual clock for tests: time moves only when the test (or a ``sleep``) moves it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """Implements ``claude_session.clock.Clock``; ``sleep`` advances time instead of blocking."""

    def __init__(self, start: datetime = EPOCH) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs a timezone-aware start")
        self._now = start.astimezone(UTC)
        self._monotonic = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        self.sleeps.append(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time does not go backwards")
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds
