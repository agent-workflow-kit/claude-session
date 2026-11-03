"""``gateway.pidfd_open`` and ``gateway.pidfd_send_signal``: the os and signal functions where
Python has them, else the syscalls through ``ctypes`` on x86_64 and aarch64 Linux. Only
processes these tests started are signaled."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys

import pytest

from claude_session import gateway

LINUX = sys.platform.startswith("linux")
PIDFD_MACHINE = LINUX and os.uname().machine in ("x86_64", "aarch64")
needs_pidfd = pytest.mark.skipif(not PIDFD_MACHINE, reason="x86_64 and aarch64 Linux only")


@pytest.fixture
def syscall_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the ctypes syscall path (as on a Python built without the wrappers)."""
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)


def test_pidfds_are_available_on_x86_64_and_aarch64_linux() -> None:
    support = gateway.pidfd_support()
    if not PIDFD_MACHINE:
        assert support in (None, gateway.PIDFD_NATIVE)
        return
    native = hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
    assert support == (gateway.PIDFD_NATIVE if native else gateway.PIDFD_SYSCALL)


@needs_pidfd
@pytest.mark.usefixtures("syscall_only")
def test_the_syscall_path_signals_the_exact_process() -> None:
    assert gateway.pidfd_support() == gateway.PIDFD_SYSCALL
    proc = subprocess.Popen(["/bin/sleep", "60"])
    fd = gateway.pidfd_open(proc.pid)
    try:
        gateway.pidfd_send_signal(fd, signal.SIGTERM)
        assert proc.wait(timeout=30) == -signal.SIGTERM
        with pytest.raises(ProcessLookupError):  # reaped: the pidfd never reaches a reuse
            gateway.pidfd_send_signal(fd, signal.SIGTERM)
    finally:
        os.close(fd)


@needs_pidfd
@pytest.mark.usefixtures("syscall_only")
def test_the_syscall_path_refuses_a_pid_with_no_process() -> None:
    proc = subprocess.Popen(["/bin/true"])
    proc.wait(timeout=30)
    with pytest.raises(OSError):
        gateway.pidfd_open(-1)
    with pytest.raises(OSError):
        gateway.pidfd_open(2**31)
    try:
        fd = gateway.pidfd_open(proc.pid)
    except ProcessLookupError:
        return
    os.close(fd)  # the PID was reused meanwhile: then it names that other process


def test_without_any_pidfd_the_calls_raise_enosys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(gateway, "_pidfd_syscall", lambda: None)
    assert gateway.pidfd_support() is None
    with pytest.raises(OSError) as raised:
        gateway.pidfd_open(os.getpid())
    assert raised.value.errno == errno.ENOSYS
    with pytest.raises(OSError) as raised:
        gateway.pidfd_send_signal(0, 0)
    assert raised.value.errno == errno.ENOSYS
