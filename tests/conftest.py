"""Shared fixtures.

``no_leftover_processes`` (autouse) records every process a test starts in this process
through ``subprocess.Popen`` (the gateway's included) at launch: its PID, its start ticks, a
pidfd (Linux 5.3+) and whether it leads its own process group. At teardown, after the test has
returned, each one still running is SIGKILLed while it is still the process started here:

- a plain child through its pidfd, which names that exact process and never a reused PID;
- a group leader through its pidfd too (should it have left its group), and its whole group
  only while the leader is still our unreaped child with the same start ticks (an unreaped
  leader pins the group ID; Linux cannot signal a group through a pidfd).

Pidfds come from ``os.pidfd_open`` and ``signal.pidfd_send_signal`` where Python has them, and
otherwise (Pythons built without them) straight from the syscalls through ``ctypes`` on x86_64
and aarch64 Linux. Other machines and macOS have no pidfd.

The check before ``killpg``, and before ``kill`` where there is no pidfd, leaves a window: if
the leader is reaped in it and its PID reused, the signal reaches the new process. Only a
thread the test left running past teardown can reap it then, so the window is open only for
such a test. Nothing else is ever signalled: the members of a group whose leader was already
reaped are left to the tests that started them (see ``grandchildren`` in test_gateway.py), which
record them by exact PID.

Should a test run past ``WATCHDOG_SECONDS``, the watchdog writes the live tracked PIDs to
stderr, SIGKILLs every tracked process that has a pidfd (exact: a pidfd never names a reused
PID, so this cannot race the test's own wait), and the test fails at teardown. It sends no
other signal: without pidfds it only reports, and a test hung on something else stays hung.
This module is test-only; nothing under ``src/`` imports it.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import platform
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest_plugins = ["pytester"]  # test_leftover_guard.py runs the guard in a pytest of its own

LINUX = sys.platform.startswith("linux")
# A test still running after this long is reported as hung and its pidfd processes killed.
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


# (SYS_pidfd_open, SYS_pidfd_send_signal) by platform.machine(), for Pythons without the
# os/signal wrappers. Any other machine gets no pidfd.
_PIDFD_SYSCALLS = {"x86_64": (434, 424), "aarch64": (434, 424)}


def _libc_syscall() -> Any:
    """libc's ``syscall``, or ``None`` when it is not needed or not available."""
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        return None
    if not LINUX or platform.machine() not in _PIDFD_SYSCALLS:
        return None
    try:
        fn = ctypes.CDLL(None, use_errno=True).syscall
    except (OSError, AttributeError):
        return None
    fn.restype = ctypes.c_long
    return fn


_SYSCALL = _libc_syscall()
# Whether exact pidfds are available here (natively or through the syscalls).
PIDFD = (hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")) or (
    _SYSCALL is not None
)


def _check(result: int) -> int:
    if result < 0:
        err = ctypes.get_errno() or errno.EIO  # never 0: set_errno(0) precedes every call
        raise OSError(err, os.strerror(err))  # ESRCH arrives as ProcessLookupError
    return result


def _pidfd_open(pid: int) -> int:
    if _SYSCALL is None:
        return os.pidfd_open(pid)
    ctypes.set_errno(0)
    return _check(
        _SYSCALL(
            ctypes.c_long(_PIDFD_SYSCALLS[platform.machine()][0]),
            ctypes.c_int(pid),
            ctypes.c_uint(0),
        )
    )


def _pidfd_send_signal(fd: int, sig: int) -> None:
    if _SYSCALL is None:
        signal.pidfd_send_signal(fd, sig)
        return
    ctypes.set_errno(0)
    _check(
        _SYSCALL(
            ctypes.c_long(_PIDFD_SYSCALLS[platform.machine()][1]),
            ctypes.c_int(fd),
            ctypes.c_int(sig),
            ctypes.c_void_p(None),
            ctypes.c_uint(0),
        )
    )


def _pidfd(pid: int) -> int | None:
    if not PIDFD:
        return None
    try:
        return _pidfd_open(pid)
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
                _pidfd_send_signal(self.pidfd, signal.SIGKILL)  # ESRCH once reaped
            elif self.is_ours():
                os.kill(pid, signal.SIGKILL)  # no pidfd: check-then-kill

    def kill_exact(self) -> None:
        """SIGKILL through the pidfd only; without one, nothing is signalled."""
        if self.pidfd is not None:
            with contextlib.suppress(OSError):  # ESRCH once reaped
                _pidfd_send_signal(self.pidfd, signal.SIGKILL)

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
        # Signals only through pidfds: a check-then-kill from here could race the test's own
        # wait (see the docstring). The pidfds stay open until this thread is joined.
        if not done.wait(WATCHDOG_SECONDS):
            fired.set()
            records = list(started)
            live = [p.popen.pid for p in records if p.is_ours()]  # never reaps
            print(f"watchdog: test hung; live tracked PIDs: {live}", file=sys.__stderr__)
            for record in records:
                record.kill_exact()

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


@pytest.fixture(autouse=True)
def _private_state_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Every test (and every subprocess it starts) gets its own ``XDG_STATE_HOME``, so no
    test ever touches the real state directory. Kept outside ``tmp_path``, which tests may
    expect to stay empty; not created up front."""
    state_home = tmp_path_factory.mktemp("xdg") / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    return state_home
