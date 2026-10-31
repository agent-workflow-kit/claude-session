"""Shared fixtures.

``no_leftover_processes`` (autouse) records every process a test starts in this process
through ``subprocess.Popen`` (the gateway's included) at launch: its PID, its start ticks, a
pidfd (Linux 5.3+) and whether it leads its own process group. At teardown, after the test has
returned, each one still running is SIGKILLed while it is still the process started here:

- a plain child through its pidfd, which names that exact process and never a reused PID;
- a group leader through its pidfd too (should it have left its group), and its whole group
  only while the leader is still our unreaped child with the same start ticks (an unreaped
  leader pins the group ID; Linux cannot signal a group through a pidfd).

The check before ``killpg``, and before ``kill`` where there is no pidfd (macOS, older Linux),
leaves a window: if the leader is reaped in it and its PID reused, the signal reaches the new
process. Only a thread the test left running past teardown can reap it then, so the window is
open only for such a test. Nothing else is ever signalled: the members of a group whose leader
was already reaped are left to the tests that started them (see ``grandchildren`` in
test_gateway.py), which record them by exact PID.

The watchdog signals nothing. Should a test run past ``WATCHDOG_SECONDS`` it writes the live
tracked PIDs to stderr, and the test fails at teardown.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

pytest_plugins = ["pytester"]  # test_leftover_guard.py runs the guard in a pytest of its own

LINUX = sys.platform.startswith("linux")
# A test still running after this long is reported as hung (nothing is signalled).
WATCHDOG_SECONDS = 60.0


def _stat(pid: int) -> tuple[int, int, int] | None:
    """``(pgrp, session, start ticks)`` of a live process (zombies included), or ``None`` when
    it is gone or cannot be read (``hidepid``): such a process is never treated as ours."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rpartition(b")")[2].split()
    except OSError:
        return None
    return int(fields[2]), int(fields[3]), int(fields[19])


def _unreaped_child(pid: int) -> bool:
    """Whether ``pid`` is a child of this process not yet reaped (running or a zombie), so the
    PID, and the group ID it leads, cannot have been reused."""
    if not hasattr(os, "waitid"):
        return True  # macOS: only Popen's own bookkeeping (returncode) is available
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return False  # reaped, or never our child
    return True


def _pidfd(pid: int) -> int | None:
    if not hasattr(os, "pidfd_open"):
        return None
    try:
        return os.pidfd_open(pid)
    except OSError:
        return None


@dataclass(frozen=True)
class _Started:
    popen: subprocess.Popen[Any]
    ticks: int | None  # None off Linux
    own_group: bool
    pidfd: int | None = None  # None off Linux

    def is_ours(self) -> bool:
        """Whether the PID still names the process started here: not reaped (through Popen or
        otherwise), still our child and, on Linux, with the same start ticks."""
        if self.popen.returncode is not None or not _unreaped_child(self.popen.pid):
            return False
        if not LINUX:
            return True
        stat = _stat(self.popen.pid)
        return stat is not None and stat[2] == self.ticks

    def kill(self) -> None:
        pid = self.popen.pid
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if self.own_group and self.is_ours():
                os.killpg(pid, signal.SIGKILL)  # check-then-kill: see the module docstring
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if self.pidfd is not None:
                signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)  # ESRCH once reaped
            elif self.is_ours():
                os.kill(pid, signal.SIGKILL)  # no pidfd: check-then-kill

    def close(self) -> None:
        if self.pidfd is not None:
            os.close(self.pidfd)


def _tracking_popen(started: list[_Started]) -> type[subprocess.Popen[Any]]:
    real = subprocess.Popen

    class TrackedPopen(real):  # type: ignore[valid-type,misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            stat = _stat(self.pid) if LINUX else None
            own = kwargs.get("process_group") == 0 or bool(kwargs.get("start_new_session"))
            # Popen has not reaped the child yet (it is at worst a zombie): the pidfd names it.
            pidfd = _pidfd(self.pid) if LINUX else None
            started.append(_Started(self, None if stat is None else stat[2], own, pidfd))

    return TrackedPopen


@pytest.fixture(autouse=True)
def no_leftover_processes() -> Iterator[list[_Started]]:
    started: list[_Started] = []
    real = subprocess.Popen
    done, fired = threading.Event(), threading.Event()

    def watchdog() -> None:
        # Report only: a signal from here could race the test's own wait (see the docstring).
        if not done.wait(WATCHDOG_SECONDS):
            fired.set()
            live = [p.popen.pid for p in list(started) if p.is_ours()]  # never reaps
            print(f"watchdog: test hung; live tracked PIDs: {live}", file=sys.__stderr__)

    thread: threading.Thread | None = None
    try:
        subprocess.Popen = _tracking_popen(started)  # type: ignore[misc]
        thread = threading.Thread(target=watchdog, daemon=True)
        thread.start()
        yield started
    finally:
        subprocess.Popen = real  # type: ignore[misc]
        done.set()
        if thread is not None:
            thread.join()
        for proc in started:
            proc.kill()
            if proc.popen.returncode is None:
                with contextlib.suppress(subprocess.TimeoutExpired, ChildProcessError):
                    proc.popen.wait(timeout=5)
            proc.close()
    if fired.is_set():
        pytest.fail(f"watchdog fired: the test was still running after {WATCHDOG_SECONDS} s")
