"""Shared fixtures.

``no_leftover_processes`` (autouse) records every process a test starts in this process
through ``subprocess.Popen`` (the gateway's included) at launch: its PID, its start ticks and
whether it leads its own process group. At teardown, and from a watchdog thread should the test
hang, each one still running is SIGKILLed: the exact PID, or for a group leader its whole group,
and only while that PID still names the process started here (same start ticks). A group whose
leader was already reaped loses only the members that started after the leader. So a failing or
hanging test never leaves a TERM-ignoring or looping process behind.
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

LINUX = sys.platform.startswith("linux")
# A test still running after this long is hung: its processes are killed to unblock it.
WATCHDOG_SECONDS = 60.0


def _stat(pid: int) -> tuple[int, int, int] | None:
    """``(pgrp, session, start ticks)`` of a live process (zombies included), or ``None``."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rpartition(b")")[2].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return int(fields[2]), int(fields[3]), int(fields[19])


@dataclass(frozen=True)
class _Started:
    popen: subprocess.Popen[Any]
    ticks: int | None  # None off Linux
    own_group: bool

    def same_process(self) -> bool:
        """Whether the PID still names the process started here (gone once reaped)."""
        if self.popen.returncode is not None:
            return False  # reaped through Popen: the PID may be reused
        if not LINUX:
            return True  # unreaped (as far as Popen knows): the PID is still ours
        stat = _stat(self.popen.pid)
        return stat is not None and stat[2] == self.ticks

    def kill(self) -> None:
        pid = self.popen.pid
        if self.same_process():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                if self.own_group:
                    os.killpg(pid, signal.SIGKILL)  # the unreaped leader pins the group ID
                else:
                    os.kill(pid, signal.SIGKILL)
        elif self.own_group and LINUX and self.ticks is not None:
            self._kill_group_remnants()

    def _kill_group_remnants(self) -> None:
        """The leader was reaped: kill the members of its group that started after it (a group
        ID cannot be reused while any member is left) in our session, each by its exact PID."""
        pgid, session = self.popen.pid, os.getsid(0)
        assert self.ticks is not None
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            stat = _stat(int(name))
            if stat is not None and stat[:2] == (pgid, session) and stat[2] >= self.ticks:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(int(name), signal.SIGKILL)


def _tracking_popen(started: list[_Started]) -> type[subprocess.Popen[Any]]:
    real = subprocess.Popen

    class TrackedPopen(real):  # type: ignore[valid-type,misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            stat = _stat(self.pid) if LINUX else None
            own = kwargs.get("process_group") == 0 or bool(kwargs.get("start_new_session"))
            started.append(_Started(self, None if stat is None else stat[2], own))

    return TrackedPopen


@pytest.fixture(autouse=True)
def no_leftover_processes() -> Iterator[list[_Started]]:
    started: list[_Started] = []
    real = subprocess.Popen
    subprocess.Popen = _tracking_popen(started)  # type: ignore[misc]
    done = threading.Event()

    def watchdog() -> None:
        if not done.wait(WATCHDOG_SECONDS):
            for proc in list(started):
                proc.kill()  # no reaping here: the test's own wait returns

    thread = threading.Thread(target=watchdog, daemon=True)
    thread.start()
    try:
        yield started
    finally:
        subprocess.Popen = real  # type: ignore[misc]
        done.set()
        thread.join()
        for proc in started:
            proc.kill()
            if proc.popen.returncode is None:
                with contextlib.suppress(subprocess.TimeoutExpired, ChildProcessError):
                    proc.popen.wait(timeout=5)
