"""Injectable clock (docs/plan.md, Conventions). Standard library only.

Code that reads time takes a ``Clock`` instead of calling ``datetime.now``/``time`` directly, so
tests drive time with ``tests/fake_clock.FakeClock``. Stored timestamps are UTC (design.md, Data
model); durations inside a process use ``monotonic``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """The current time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards; only differences are meaningful."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
