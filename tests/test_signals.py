"""``exec``'s signal phases (interface.md, Signals): the handler is exercised by signalling this
process itself (``os.kill(os.getpid(), ...)``, the exact PID), with the handlers restored after
every test."""

from __future__ import annotations

import os
import signal
import threading
from collections import deque
from collections.abc import Iterator
from typing import Any

import pytest

from claude_session.signals import (
    HANDLED,
    ExecSignals,
    LaunchInterrupted,
    Phase,
    signal_name,
    stop_cause,
)


@pytest.fixture
def sigs() -> Iterator[ExecSignals]:
    handlers = ExecSignals()
    assert handlers.install()
    try:
        yield handlers
    finally:
        handlers.restore()


def own(sig: int) -> None:
    """Signal this process (its exact PID); the handler runs before ``os.kill`` returns."""
    os.kill(os.getpid(), sig)


def test_install_sets_callable_handlers_and_restore_puts_the_previous_back() -> None:
    before = {sig: signal.getsignal(sig) for sig in HANDLED}
    handlers = ExecSignals()
    assert handlers.install()
    try:
        for sig in HANDLED:
            # Never SIG_IGN: an ignored signal would be inherited by claude across exec.
            assert callable(signal.getsignal(sig))
            assert signal.getsignal(sig) not in (signal.SIG_IGN, signal.SIG_DFL)
        # Job-control signals keep their default action.
        assert signal.getsignal(signal.SIGTSTP) == signal.SIG_DFL
        assert signal.getsignal(signal.SIGCONT) == signal.SIG_DFL
    finally:
        handlers.restore()
    assert {sig: signal.getsignal(sig) for sig in HANDLED} == before


def test_install_off_the_main_thread_installs_nothing() -> None:
    before = {sig: signal.getsignal(sig) for sig in HANDLED}
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(ExecSignals().install()))
    thread.start()
    thread.join()
    assert result == [False]
    assert {sig: signal.getsignal(sig) for sig in HANDLED} == before


@pytest.mark.parametrize("sig", HANDLED)
def test_the_raise_phase_raises_and_switches_to_defer(sigs: ExecSignals, sig: int) -> None:
    with pytest.raises(LaunchInterrupted) as info:
        own(sig)
    assert info.value.signum == sig
    assert sigs.phase is Phase.DEFER
    own(sig)  # a second signal during the cleanup is only recorded
    assert sigs.take() == [sig]


def test_the_defer_phase_records_and_enter_launch_raises_the_first(sigs: ExecSignals) -> None:
    sigs.defer()
    own(signal.SIGHUP)
    own(signal.SIGTERM)
    with pytest.raises(LaunchInterrupted) as info:
        sigs.enter_launch()
    assert info.value.signum == signal.SIGHUP and info.value.name == "SIGHUP"
    assert sigs.phase is Phase.DEFER and sigs.take() == []


def test_enter_launch_without_a_recorded_signal_switches_to_raise(sigs: ExecSignals) -> None:
    sigs.defer()
    sigs.enter_launch()
    assert sigs.phase is Phase.RAISE
    with pytest.raises(LaunchInterrupted):
        own(signal.SIGTERM)


def test_raise_pending_is_a_no_op_without_signals(sigs: ExecSignals) -> None:
    sigs.defer()
    sigs.raise_pending()
    assert sigs.phase is Phase.DEFER


def test_interactive_session_drops_sigint_and_records_term_and_hup(sigs: ExecSignals) -> None:
    sigs.defer()
    sigs.enter_session(interactive=True)
    own(signal.SIGINT)
    own(signal.SIGTERM)
    own(signal.SIGINT)
    own(signal.SIGHUP)
    assert sigs.take() == [signal.SIGTERM, signal.SIGHUP]
    assert sigs.take() == []


def test_headless_session_records_sigint(sigs: ExecSignals) -> None:
    sigs.defer()
    sigs.enter_session(interactive=False)
    own(signal.SIGINT)
    assert sigs.take() == [signal.SIGINT]


def test_a_sigint_recorded_during_an_interactive_spawn_is_dropped(sigs: ExecSignals) -> None:
    sigs.defer()
    own(signal.SIGINT)
    own(signal.SIGTERM)
    sigs.enter_session(interactive=True)
    assert sigs.take() == [signal.SIGTERM]


def test_a_sigint_recorded_during_a_headless_spawn_is_kept(sigs: ExecSignals) -> None:
    sigs.defer()
    own(signal.SIGINT)
    sigs.enter_session(interactive=False)
    assert sigs.take() == [signal.SIGINT]


def test_a_signal_recorded_while_entering_the_session_is_neither_lost_nor_an_error(
    sigs: ExecSignals,
) -> None:
    """R1-01: the handler appends to the deque while a SIGINT recorded during the spawn is
    being dropped (simulated at the worst point: mid-iteration, if anything iterates it)."""

    class Racing(deque[int]):
        fired = False

        def __iter__(self) -> Iterator[int]:
            items = list(super().__iter__())
            for index, item in enumerate(items):
                yield item
                if index == 0 and not self.fired:
                    self.fired = True
                    sigs._handle(signal.SIGHUP, None)  # the handler, mid-iteration

        def clear(self) -> None:
            if not self.fired:
                self.fired = True
                sigs._handle(signal.SIGHUP, None)  # the handler, before the clear
            super().clear()

    sigs.defer()
    sigs._pending = Racing([signal.SIGINT, signal.SIGTERM])
    sigs.enter_session(interactive=True)
    if not sigs._pending.fired:  # nothing iterated or cleared: the handler fires now
        sigs._handle(signal.SIGHUP, None)
    assert sigs.take() == [signal.SIGTERM, signal.SIGHUP]


@pytest.mark.parametrize("sig", HANDLED)
def test_an_inherited_ignored_signal_stays_ignored(sig: int) -> None:
    """R1-06: nohup (SIGHUP) or a background job (SIGINT): no handler, claude inherits it."""
    previous = signal.signal(sig, signal.SIG_IGN)
    try:
        handlers = ExecSignals()
        assert handlers.install()
        try:
            assert signal.getsignal(sig) == signal.SIG_IGN
            others = [s for s in HANDLED if s != sig]
            assert all(signal.getsignal(s) == handlers._handle for s in others)
            own(sig)  # ignored: no LaunchInterrupted, nothing recorded
            assert handlers.take() == []
        finally:
            handlers.restore()
        assert signal.getsignal(sig) == signal.SIG_IGN
    finally:
        signal.signal(sig, previous)


def test_a_signal_during_the_install_raises_only_once_every_handler_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1-11: a signal right after the first handler is set is recorded; install raises it
    (the before-allocation rule) with all handlers in place, and restore puts them back."""
    before = {sig: signal.getsignal(sig) for sig in HANDLED}
    real = signal.signal
    handlers = ExecSignals()

    def setting(sig: int, handler: Any) -> Any:
        previous = real(sig, handler)
        if sig == HANDLED[0] and handler == handlers._handle:
            own(sig)  # its handler is set, the others not yet
        return previous

    monkeypatch.setattr(signal, "signal", setting)
    try:
        with pytest.raises(LaunchInterrupted) as info:
            handlers.install()
        assert info.value.signum == HANDLED[0]
        assert all(signal.getsignal(s) == handlers._handle for s in HANDLED)
    finally:
        handlers.restore()
    assert {sig: signal.getsignal(sig) for sig in HANDLED} == before


@pytest.mark.parametrize(
    ("sig", "stop", "cause"),
    [
        (signal.SIGTERM, False, "forwarded_term"),
        (signal.SIGHUP, False, "forwarded_hup"),
        (signal.SIGINT, False, "forwarded_int"),
        (signal.SIGTERM, True, "stopped"),
        (signal.SIGHUP, True, "stopped"),
        (signal.SIGINT, True, "forwarded_int"),
    ],
)
def test_stop_cause(sig: int, stop: bool, cause: str) -> None:
    assert stop_cause(sig, stop) == cause


def test_signal_name() -> None:
    assert signal_name(signal.SIGTERM) == "SIGTERM"
    assert signal_name(200) == "SIG200"
