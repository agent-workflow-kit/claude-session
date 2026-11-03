"""Shared fixtures.

``no_leftover_processes`` (autouse) records every process a test starts in this process
through ``subprocess.Popen`` (the gateway's included) at launch: its PID, its start ticks, a
pidfd (Linux 5.3+) and whether it leads its own process group. At teardown, after the test has
returned, each one still running is SIGKILLed while it is still the process started here:

- a plain child through its pidfd, which names that exact process and never a reused PID;
- a group leader through its pidfd too (should it have left its group), and its whole group
  only while the leader is still our unreaped child with the same start ticks (an unreaped
  leader pins the group ID; Linux cannot signal a group through a pidfd).

Pidfds come from ``gateway.pidfd_open`` and ``gateway.pidfd_send_signal``: ``os.pidfd_open`` and
``signal.pidfd_send_signal`` where Python has them, and otherwise (Pythons built without them)
straight from the syscalls through ``ctypes`` on x86_64 and aarch64 Linux. Other machines and
macOS have no pidfd.

The check before ``killpg``, and before ``kill`` where there is no pidfd, leaves a window: if
the leader is reaped in it and its PID reused, the signal reaches the new process. Only a
thread the test left running past teardown can reap it then, so the window is open only for
such a test. Nothing else is ever signalled: the members of a group whose leader was already
reaped are left to the tests that started them (see ``grandchildren`` in test_gateway.py), which
record them by exact PID.

A fake ``claude`` (``tests/fakebin``) started by a process the test started, such as
``claude-session exec``, is a grandchild: ``Popen`` never sees it, and one that ignores
``exec``'s die-with-parent SIGTERM outlives ``exec``. So the fixture also points
``FAKE_CLAUDE_STARTED_DIR`` at a directory of its own, where every fake records its PID and
start ticks as it starts (``fakeclaude.fake_env`` bakes the variable into ``claude_bin``
wrappers). At teardown, after the processes above are ended, a recorded fake still running
with those start ticks after ``LEFTOVER_FAKE_GRACE`` (time to act on the SIGTERM) is
SIGKILLed through a pidfd opened before its start ticks are checked again, so the pidfd names
that exact fake; and the test fails, so a leak is never silently cleaned up. Without a pidfd
it is only reported.

Should a test run past ``WATCHDOG_SECONDS``, the watchdog writes the live tracked PIDs to
stderr, SIGKILLs every tracked process that has a pidfd (exact: a pidfd never names a reused
PID, so this cannot race the test's own wait), and the test fails at teardown. It sends no
other signal: without pidfds it only reports, and a test hung on something else stays hung.
This module is test-only; nothing under ``src/`` imports it (it imports ``gateway``).

The other autouse fixtures keep every test off the user's real directories: its own
``XDG_STATE_HOME`` (``_private_state_dir``), ``HOME`` and ``XDG_CONFIG_HOME``
(``_private_home``; not for ``live`` tests, which need the real Claude Code login) and
temporary-directory root (``private_tmp_root``: ``pathrules.TMP_ROOT``, so no run creates
anything under the real ``/tmp/csession-<uid>``).
"""

from __future__ import annotations

import contextlib
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from claude_session import gateway, pathrules

pytest_plugins = ["pytester"]  # test_leftover_guard.py runs the guard in a pytest of its own

LINUX = sys.platform.startswith("linux")
# A test still running after this long is reported as hung and its pidfd processes killed.
WATCHDOG_SECONDS = 60.0
# The production base of the per-uid temporary-directory root, read before any test patches it.
REAL_TMP_ROOT = pathrules.TMP_ROOT
# Where the fake claude writes its start records (tests/fakebin/fake_claude.py).
STARTED_ENV = "FAKE_CLAUDE_STARTED_DIR"
# How long a recorded fake may take to exit at teardown before it counts as left over.
LEFTOVER_FAKE_GRACE = 5.0


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


# Exact pidfds: ``gateway``'s (the os and signal functions, else the syscalls through ctypes
# on x86_64 and aarch64 Linux). Whether they are available here:
PIDFD = gateway.pidfd_support() is not None
_pidfd_open = gateway.pidfd_open
_pidfd_send_signal = gateway.pidfd_send_signal


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


def _running_ticks(pid: int) -> int | None:
    """Start ticks of a running process (not a zombie), or ``None``."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rpartition(b")")[2].split()
    except OSError:
        return None
    return None if fields[0] in (b"Z", b"X") else int(fields[19])


def _end_leftover_fakes(directory: str) -> list[str]:
    """End the fakes recorded in ``directory`` that still run (see the module docstring);
    returns a description of each one left over."""
    records: list[tuple[int, int]] = []
    for name in os.listdir(directory):
        pid, _, ticks = name.partition(".")
        records.append((int(pid), int(ticks)))
    deadline = time.monotonic() + LEFTOVER_FAKE_GRACE
    while True:
        records = [(pid, ticks) for pid, ticks in records if _running_ticks(pid) == ticks]
        if not records or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    leftovers: list[str] = []
    for pid, ticks in records:
        fd = _pidfd(pid)
        if fd is None:
            if _running_ticks(pid) == ticks:
                leftovers.append(f"{pid} (no pidfd: not signalled)")
            continue
        try:
            # Checked after the pidfd was opened: it names the recorded fake, never a reuse.
            if _running_ticks(pid) != ticks:
                continue
            with contextlib.suppress(ProcessLookupError):
                _pidfd_send_signal(fd, signal.SIGKILL)
            select.select([fd], [], [], 5.0)  # readable once the process has exited
            leftovers.append(f"{pid} (SIGKILLed)")
        finally:
            os.close(fd)
    return leftovers


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
    fakes_dir: str | None = None
    previous_env = os.environ.get(STARTED_ENV)
    leftover_fakes: list[str] = []
    try:
        fakes_dir = tempfile.mkdtemp(prefix="cs-fakes-")
        os.environ[STARTED_ENV] = fakes_dir
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
        try:
            if fakes_dir is not None:
                leftover_fakes = _end_leftover_fakes(fakes_dir)
                shutil.rmtree(fakes_dir, ignore_errors=True)
        finally:
            if previous_env is None:
                os.environ.pop(STARTED_ENV, None)
            else:
                os.environ[STARTED_ENV] = previous_env
    if fired.is_set():
        pytest.fail(f"watchdog fired: the test was still running after {WATCHDOG_SECONDS} s")
    if leftover_fakes:
        pytest.fail(
            "fake claude processes outlived the test (started by a process the test started;"
            f" ended here): {', '.join(leftover_fakes)}"
        )


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


@pytest.fixture(autouse=True)
def _private_home(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test (and every subprocess it starts) gets its own empty ``HOME`` and
    ``XDG_CONFIG_HOME``, so no test reads or writes the user's real configuration. ``live``
    tests keep the real ones: real Claude Code needs its login."""
    if request.node.get_closest_marker("live") is not None:
        return
    home = tmp_path_factory.mktemp("home")
    (home / ".config").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))


def _remove_tree(path: str) -> None:
    """Remove a test's tree even where it left a directory without write or search
    permission (the tests own every entry in it)."""

    def retry(func: Any, failed: str, exc: BaseException) -> None:
        os.chmod(os.path.dirname(failed), 0o700)
        with contextlib.suppress(OSError):
            os.chmod(failed, 0o700)
        func(failed)

    shutil.rmtree(path, onexc=retry)


@pytest.fixture(autouse=True)
def private_tmp_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every test gets its own base for the per-uid temporary-directory root
    (``pathrules.TMP_ROOT``), so no run creates anything under the real
    ``/tmp/csession-<uid>``. Subprocesses do not inherit the patch: tests that start the
    ``claude-session`` executable pass the path on (see ``test_exec_headless.run_exec``).

    The base is 0700 and owned by the test user, which passes tmpdir's base checks (owner root
    or the user; group- or other-writable only with the sticky bit). It is a short directory
    directly in the system temporary directory rather than under ``tmp_path``: the private
    tmpdir under it must leave room for Claude Code's socket names (``selfcheck``'s socket
    path limit), and ``tmp_path`` is too long for that."""
    base = tempfile.mkdtemp(prefix="cs-t-", dir="/tmp")
    os.chmod(base, 0o700)
    monkeypatch.setattr(pathrules, "TMP_ROOT", base)
    try:
        yield Path(base)
    finally:
        _remove_tree(base)
