from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claude_session.clock import Clock, SystemClock

from .fake_clock import FakeClock


def test_system_clock_now_is_timezone_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=5)


def test_system_clock_monotonic_does_not_go_backwards() -> None:
    clock = SystemClock()
    first = clock.monotonic()
    clock.sleep(0.01)
    assert clock.monotonic() >= first + 0.005


def test_both_clocks_satisfy_the_protocol() -> None:
    clocks: list[Clock] = [SystemClock(), FakeClock()]
    assert all(isinstance(c, Clock) for c in clocks)


def test_fake_clock_sleep_advances_wall_and_monotonic_time() -> None:
    clock = FakeClock()
    start, mono = clock.now(), clock.monotonic()
    clock.sleep(90)
    assert clock.now() - start == timedelta(seconds=90)
    assert clock.monotonic() - mono == 90
    assert clock.sleeps == [90]


def test_fake_clock_refuses_naive_start_and_negative_advance() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError):
        FakeClock().advance(-1)


def test_fake_clock_rejects_a_negative_sleep_without_recording_it() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.sleep(-1)
    assert clock.sleeps == []
