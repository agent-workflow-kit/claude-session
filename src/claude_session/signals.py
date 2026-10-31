"""``exec``'s signal handling by phase (interface.md, Signals; design.md, Supervision).

``exec`` installs one handler for SIGINT, SIGTERM and SIGHUP before it allocates a run
(``ExecSignals.install``) and moves it through these phases:

- ``RAISE`` (from the install until allocation, and the launch phase): the handler raises
  ``LaunchInterrupted``. Before allocation nothing is recorded and ``exec`` dies by the signal;
  in the launch phase the run ends ``not-started`` (``launch_interrupted``, or ``canceled`` for a
  SIGTERM or SIGHUP after a ``stop`` request). The handler switches to ``DEFER`` before it
  raises, so a second signal during that cleanup is only recorded.
- ``DEFER`` (allocation, the spawn, and every cleanup): the handler only records the signal.
  ``enter_launch`` raises a signal recorded during allocation once the run exists, and
  ``raise_pending`` one recorded between the launch steps and the spawn.
- ``SESSION`` (from the spawn of ``claude``): the handler only records the signal; the
  supervision loop takes it (``take``) and forwards it to the exact child. In interactive mode
  SIGINT is dropped here: ``claude`` gets it from the terminal and ``exec`` never re-forwards it,
  so Ctrl-C never counts as a kill.

- ``DELIVER`` (after the child was reaped: finalization and the last ``post-state-change``
  delivery, ``enter_delivery``): the handler records the signal and sets ``delivery_canceled``,
  so the deliverer ends the running hooks' process groups, starts no further hook and leaves
  the remaining events for the next delivering command (interface.md, Hooks: Delivery).

SIGTSTP and SIGCONT are never touched (default action: job control applies to the foreground
process group). The handlers are Python callables, never ``SIG_IGN``: an ignored signal would be
inherited by ``claude`` across ``exec``, a caught one is reset to its default action there. A
signal already ignored when ``exec`` starts (``nohup``, a shell's background job) stays ignored:
no handler is installed for it, and ``claude`` inherits an ignored SIGHUP or SIGINT; a headless
``claude`` always starts with SIGTERM at its default action, because its parent-death signal
needs it (``gateway``; interface.md, Signals).

``install`` sets the handlers in a record-only phase and applies the before-allocation rule
(``RAISE``) to a signal recorded meanwhile only once every handler is in place.

The handler runs in the main thread between bytecodes; it only appends to a deque, so it never
interrupts a store write in the ``DEFER`` and ``SESSION`` phases. Standard library only.
"""

from __future__ import annotations

import enum
import signal
import threading
from collections import deque
from types import FrameType
from typing import Any

HANDLED: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
# The pending cause of a signal forwarded in the session phase (interface.md, Signals).
FORWARDED_CAUSES: dict[int, str] = {
    signal.SIGTERM: "forwarded_term",
    signal.SIGHUP: "forwarded_hup",
    signal.SIGINT: "forwarded_int",
}
# Signals that read as the ``stop`` command's when the run has ``stop_requested_at``
# (``stopped`` in the session phase, ``canceled`` in the launch phase). ``stop`` sends SIGTERM;
# SIGHUP is included as the launch-phase table of interface.md does; SIGINT never is.
STOP_SIGNALS = frozenset({signal.SIGTERM, signal.SIGHUP})


class Phase(enum.Enum):
    RAISE = "raise"
    DEFER = "defer"
    SESSION = "session"
    DELIVER = "deliver"


class LaunchInterrupted(BaseException):  # noqa: N818 (like KeyboardInterrupt)
    """A signal ``exec`` received before ``claude`` was spawned. A ``BaseException``, so the
    ``except Exception`` of best-effort code never swallows it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum

    @property
    def name(self) -> str:
        return signal_name(self.signum)


def signal_name(signum: int) -> str:
    """``SIGTERM`` for 15, ``SIG<n>`` for a number without a name."""
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"SIG{signum}"


def stop_cause(signum: int, stop_requested: bool) -> str:
    """The session-phase pending cause of a signal ``exec`` received."""
    if stop_requested and signum in STOP_SIGNALS:
        return "stopped"
    return FORWARDED_CAUSES[signum]


class ExecSignals:
    """The handler state of one ``exec`` (see the module docs). Starts in ``RAISE``."""

    def __init__(self) -> None:
        self.phase = Phase.RAISE
        self.interactive = False
        self._pending: deque[int] = deque()
        self._previous: dict[int, Any] = {}
        # Set by a signal in the ``DELIVER`` phase: post-state-change delivery stops.
        self.delivery_canceled = threading.Event()

    # Installation

    def install(self) -> bool:
        """Install the handlers, except for a signal inherited as ignored (it stays ignored);
        False (nothing installed) off the main thread, where Python cannot set handlers.
        Signals are only recorded until every handler is installed; then a recorded one
        raises ``LaunchInterrupted`` (the before-allocation rule)."""
        self.phase = Phase.DEFER
        try:
            for sig in HANDLED:
                if signal.getsignal(sig) == signal.SIG_IGN:
                    continue  # inherited ignored: it stays ignored (see the module docstring)
                self._previous[sig] = signal.signal(sig, self._handle)
        except ValueError:
            self.restore()
            self.phase = Phase.RAISE
            return False
        self.phase = Phase.RAISE
        self.raise_pending()
        return True

    def restore(self) -> None:
        """Put the previous handlers back (those this object replaced)."""
        while self._previous:
            sig, previous = self._previous.popitem()
            signal.signal(sig, previous)

    # The handler

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self.phase is Phase.RAISE:
            self.phase = Phase.DEFER  # a second signal during the cleanup is only recorded
            raise LaunchInterrupted(signum)
        self._pending.append(signum)  # an interactive SIGINT is dropped by take()
        if self.phase is Phase.DELIVER:
            self.delivery_canceled.set()

    # Phase changes

    def defer(self) -> None:
        self.phase = Phase.DEFER

    def enter_delivery(self) -> None:
        """The child is reaped (or never started): from now on a signal only records itself
        and cancels the remaining ``post-state-change`` delivery (``delivery_canceled``)."""
        self.phase = Phase.DELIVER

    def enter_launch(self) -> None:
        """Enter the launch phase (the run is allocated): a signal recorded meanwhile raises
        now."""
        self.raise_pending()
        self.phase = Phase.RAISE

    def raise_pending(self) -> None:
        """Raise ``LaunchInterrupted`` for the first signal recorded so far (the others are
        dropped), switching to ``DEFER``; nothing when none was."""
        if self._pending:
            signum = self._pending.popleft()
            self._pending.clear()
            self.phase = Phase.DEFER
            raise LaunchInterrupted(signum)

    def enter_session(self, *, interactive: bool) -> None:
        """``claude`` is spawned: record signals for the supervision loop. A SIGINT recorded
        while an interactive ``claude`` was being spawned is dropped (the terminal sent it to
        ``claude`` too). Only flags change here: the recorded signals are filtered by
        ``take``, never by iterating the deque the handler appends to."""
        self.interactive = interactive
        self.phase = Phase.SESSION

    def take(self) -> list[int]:
        """The signals recorded since the last call, in order; in an interactive session
        without SIGINT (``claude`` gets it from the terminal, during the spawn too)."""
        taken = []
        while self._pending:
            signum = self._pending.popleft()
            if self.interactive and signum == signal.SIGINT:
                continue
            taken.append(signum)
        return taken
