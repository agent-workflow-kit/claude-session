"""The conftest leftover-process guard signals only processes this session started."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests import conftest
from tests.conftest import _Started, _stat  # pyright: ignore[reportPrivateUsage]

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")


def _alive(pid: int) -> bool:
    stat = _stat(pid)
    if stat is None:
        return False
    with open(f"/proc/{pid}/stat", "rb") as fh:
        return fh.read().rpartition(b")")[2].split()[0] not in (b"Z", b"X")


def _stub(pid: int, returncode: int | None) -> subprocess.Popen[Any]:
    """A record's ``Popen`` as seen after its PID was reused: same PID, another process."""
    return cast(subprocess.Popen[Any], SimpleNamespace(pid=pid, returncode=returncode))


def _kill_exact(pid: int, ticks: int) -> None:
    stat = _stat(pid)
    if stat is not None and stat[2] == ticks:
        os.kill(pid, signal.SIGKILL)


@linux_only
def test_a_reaped_leader_whose_pid_now_leads_another_group_signals_nothing() -> None:
    # The recorded leader was reaped and its PID reused by a new group leader in this session
    # (the target, started here as the unrelated process): the record must not touch it.
    target = subprocess.Popen(["/bin/sleep", "30"], process_group=0)
    stat = _stat(target.pid)
    assert stat is not None
    try:
        for returncode, ticks in ((0, stat[2] - 1), (None, stat[2] - 1), (0, stat[2])):
            _Started(_stub(target.pid, returncode), ticks, True).kill()
            time.sleep(0.1)
            assert _alive(target.pid), (returncode, ticks)
    finally:
        _kill_exact(target.pid, stat[2])
        target.wait(timeout=5)


@linux_only
def test_a_group_leader_that_is_not_our_child_is_not_signalled() -> None:
    # Same PID and start ticks, yet not a child of this process (it is someone else's, or was
    # already reaped and its PID reused): neither its group nor the PID is signalled.
    starter = subprocess.run(
        [
            sys.executable,
            "-c",
            "import subprocess; print(subprocess.Popen(['/bin/sleep', '30'], process_group=0,"
            " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    pid = int(starter.stdout)
    stat = _stat(pid)
    assert stat is not None
    try:
        for own_group in (True, False):
            _Started(_stub(pid, None), stat[2], own_group).kill()
            time.sleep(0.1)
            assert _alive(pid), own_group
    finally:
        _kill_exact(pid, stat[2])


def test_stat_of_an_unreadable_process_is_not_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> object:
        raise PermissionError("hidepid")

    monkeypatch.setattr("builtins.open", denied)
    assert _stat(1) is None


def test_the_tracking_popen_is_restored_when_the_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = subprocess.Popen

    def failing(*args: object, **kwargs: object) -> object:
        raise RuntimeError("no thread")

    monkeypatch.setattr(conftest.threading, "Thread", failing)
    guard = conftest.no_leftover_processes.__wrapped__()  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(RuntimeError):
        next(guard)
    assert subprocess.Popen is real


_HUNG_TEST = (
    "import subprocess, time\n\n"
    "def test_slow():\n"
    "    proc = subprocess.Popen(['/bin/sleep', '30'])\n"
    "    time.sleep(1)\n"
    "    assert proc.poll() {}\n"
)


def test_the_watchdog_only_reports_a_hung_test_without_a_pidfd(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without a pidfd the process the inner test started outlives the watchdog firing;
    # teardown then ends it and fails the test.
    monkeypatch.setattr(conftest, "WATCHDOG_SECONDS", 0.2)
    monkeypatch.setattr(conftest, "_pidfd", lambda pid: None)
    pytester.makeconftest("from tests.conftest import no_leftover_processes  # noqa: F401\n")
    pytester.makepyfile(_HUNG_TEST.format("is None"))
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*watchdog fired*"])


@linux_only
@pytest.mark.skipif(not conftest.PIDFD, reason="needs a pidfd")
def test_the_watchdog_kills_a_hung_test_s_processes_through_their_pidfds(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conftest, "WATCHDOG_SECONDS", 0.2)
    pytester.makeconftest("from tests.conftest import no_leftover_processes  # noqa: F401\n")
    pytester.makepyfile(_HUNG_TEST.format(f"== -{int(signal.SIGKILL)}"))
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*watchdog fired*"])


def test_a_pidfd_is_available_on_x86_64_and_aarch64_linux() -> None:
    expected = sys.platform.startswith("linux") and platform.machine() in ("x86_64", "aarch64")
    assert conftest.PIDFD == expected


@linux_only
@pytest.mark.skipif(not conftest.PIDFD, reason="needs a pidfd")
def test_signalling_a_reaped_process_through_its_pidfd_raises_process_lookup_error() -> None:
    proc = subprocess.Popen(["/bin/true"])
    fd = conftest._pidfd_open(proc.pid)  # pyright: ignore[reportPrivateUsage]
    try:
        proc.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            conftest._pidfd_send_signal(fd, signal.SIGKILL)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(fd)


@pytest.mark.skipif(not conftest.PIDFD, reason="needs a pidfd")
def test_opening_a_pidfd_for_no_process_raises() -> None:
    with pytest.raises(OSError):
        conftest._pidfd_open(-1)  # pyright: ignore[reportPrivateUsage]


@linux_only
@pytest.mark.skipif(not conftest.PIDFD, reason="needs a pidfd")
def test_a_group_leader_that_left_its_group_is_killed_through_its_pidfd() -> None:
    # The leader moves itself into this process's group: killpg on its PID reaches nobody,
    # its pidfd still ends it.
    pgrp = os.getpgrp()
    leader = subprocess.Popen(
        [sys.executable, "-c", f"import os, time; os.setpgid(0, {pgrp}); time.sleep(30)"],
        process_group=0,
    )
    try:
        deadline = time.monotonic() + 10
        while (stat := _stat(leader.pid)) is not None and stat[0] != pgrp:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert stat is not None
        pidfd = conftest._pidfd_open(leader.pid)  # pyright: ignore[reportPrivateUsage]
        record = _Started(leader, stat[2], True, pidfd)
        try:
            record.kill()
            assert leader.wait(timeout=5) == -signal.SIGKILL
        finally:
            record.close()
    finally:
        if leader.returncode is None:
            leader.kill()
            leader.wait(timeout=5)
