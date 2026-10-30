"""Shared fixtures.

``no_leftover_processes`` (autouse) records every process a test starts in this process
through ``subprocess.Popen`` (the gateway's included) at launch: its PID, its start ticks, a
pidfd (Linux) and whether it leads its own process group. At teardown, and from a watchdog thread
should the test hang, each one still running is SIGKILLed, and only while it is verifiably the
process started here: a plain child through its pidfd (a signal through it can never reach a
reused PID), a group leader's whole group only while the leader is still our unreaped child with
the same start ticks (the unreaped leader pins the group ID). Nothing else is ever signalled: the
members of a group whose leader was already reaped are left to the tests that started them (see
``grandchildren`` in test_gateway.py), which record them by exact PID. A test the watchdog had
to unblock fails at teardown.
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
# A test still running after this long is hung: its processes are killed to unblock it.
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

    def _leader_is_ours(self) -> bool:
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
            if self.own_group:
                # A small window remains: should another thread reap the leader between this
                # check and killpg (the test's own wait racing the watchdog), and the PID be
                # reused as a new group leader within those microseconds, killpg would reach
                # that group. Linux 5.15 cannot signal a group through a pidfd.
                if self._leader_is_ours():
                    os.killpg(pid, signal.SIGKILL)
            elif self.pidfd is not None:
                signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)  # ESRCH once reaped
            elif self._leader_is_ours():
                os.kill(pid, signal.SIGKILL)

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
        if not done.wait(WATCHDOG_SECONDS):
            fired.set()
            for proc in list(started):
                proc.kill()  # no reaping here: the test's own wait returns

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
