"""Gateway: the only module that starts subprocesses (design.md, Modules).

Standard library only (the ``hook-event`` fast path imports it). It runs ``claude``, ``git``, the
notes command, lifecycle hooks, ``crontab``, the sandbox prerequisite checks and ``ps`` (procinfo
on macOS). Callers never import ``subprocess``; they receive a ``Gateway`` object, so tests inject
``tests/fake_gateway.FakeGateway`` without patching any call site.

Rules every call follows:

- ``argv`` is a sequence of strings (a bare ``str`` or ``bytes`` is refused with ``TypeError``)
  whose first entry is an absolute path, resolved by the caller outside every session-writable
  path (interface.md, Path rules); a bare name is refused with ``ValueError``. Never
  ``shell=True``: lifecycle hooks are run as an explicit absolute ``/bin/sh -c <cmd>`` argv.
- Environment: with no explicit ``env`` the child gets this process's environment, plus
  ``extra_env``, with empty and relative ``PATH`` entries dropped (``sanitize_path``). That
  default carries *every* inherited variable, secrets and ``CSESSION_*`` included, so the
  builders for hooks, the notes command and the session pass a complete ``env`` instead; it is
  used unchanged, because those builders apply the PATH rules themselves.
- ``run`` gives the child ``/dev/null`` as stdin unless ``stdin`` text is given, and ``spawn``
  does too unless ``INHERIT`` is passed (interactive ``claude``), so a child never consumes the
  hook payload on ``hook-event``'s stdin.
- Lifecycle hooks and the notes command use ``run(..., new_process_group=True, kill_grace=...)``
  so a timeout ends everything they started (see ``GatewayTimeout``).

``die_with_parent`` (``spawn``, Linux only, ignored elsewhere) sets ``PR_SET_PDEATHSIG(SIGTERM)``
in the child between fork and exec (through ``ctypes``, resolved before the fork, in
``preexec_fn``), so a headless ``claude`` never outlives a dead ``exec``. The kernel sends the
signal when the *thread* that forked the child exits, so spawn such children from a thread that
lives as long as the process (the main thread). If the parent already died before the child set
the flag, the child exits with ``PARENT_GONE_EXIT`` without running the program.
"""

from __future__ import annotations

import enum
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

# Pass as a ``spawn`` stream to connect it to /dev/null (``spawn``'s default stdin).
DEVNULL: Final = subprocess.DEVNULL


class Inherit(enum.Enum):
    """The type of ``INHERIT``."""

    INHERIT = "inherit"


# Pass as a ``spawn`` stream to share this process's descriptor (interactive ``claude``).
INHERIT: Final = Inherit.INHERIT

# Exit code of a ``die_with_parent`` child whose parent died before the death signal was set.
PARENT_GONE_EXIT: Final = 128 + signal.SIGTERM

_PR_SET_PDEATHSIG = 1
_SIG_DFL = 0
_GRACE_POLL = 0.02


@dataclass(frozen=True)
class Result:
    """A finished ``run``: exit code and decoded output (invalid UTF-8 replaced)."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class GatewayTimeout(Exception):
    """``run`` exceeded its timeout. Before this is raised:

    - without ``new_process_group``: the direct child got SIGKILL and was reaped. Only the
      direct child: processes it started may keep running.
    - with ``new_process_group``: the child's process group got SIGTERM, then, after
      ``kill_grace`` seconds or as soon as the leader exited, SIGKILL (with ``kill_grace=None``,
      SIGKILL straight away), and the leader was reaped. The group is signaled only while its
      leader is unreaped, so its ID cannot have been reused.
    """

    def __init__(self, argv: Sequence[str], timeout: float | None) -> None:
        super().__init__(f"{argv[0]} timed out after {timeout} s")
        self.argv = tuple(argv)
        self.timeout = timeout


@runtime_checkable
class Child(Protocol):
    """A started child process."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None:
        """The exit code (negative signal number if killed), or ``None`` while running."""
        ...

    def wait(self, timeout: float | None = None) -> int | None:
        """Wait for exit; ``None`` if still running after ``timeout`` seconds."""
        ...

    def send_signal(self, sig: int) -> None:
        """Signal this child; a no-op once it has been reaped (its PID may be reused)."""
        ...


@runtime_checkable
class Gateway(Protocol):
    """What callers depend on; ``SystemGateway`` is the real one."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        timeout: float | None = None,
        new_process_group: bool = False,
        kill_grace: float | None = None,
    ) -> Result:
        """Run to completion, capturing text output. ``new_process_group`` puts the child in
        its own process group, which a timeout ends as a whole; ``kill_grace`` (only with a
        group) is the SIGTERM-to-SIGKILL delay. Raises ``GatewayTimeout``, and ``OSError`` when
        the program cannot be started."""
        ...

    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: int | Inherit = DEVNULL,
        stdout: int | Inherit | None = None,
        stderr: int | Inherit | None = None,
        new_process_group: bool = False,
        die_with_parent: bool = False,
    ) -> Child:
        """Start without waiting. Streams are file descriptors, ``DEVNULL`` or ``INHERIT``;
        ``stdin`` defaults to ``DEVNULL``, ``stdout``/``stderr`` to inheriting (``None``).
        ``new_process_group`` puts the child in its own process group (headless ``claude``);
        otherwise it stays in ours (interactive ``claude`` in the terminal's foreground group).

        ``die_with_parent`` uses ``preexec_fn``, which is not safe while other threads run:
        spawn such children before starting worker threads where possible. The child resets
        SIGTERM to its default action and unblocks it, so the death signal works even when
        this process ignores SIGTERM; any other ignored (``SIG_IGN``) signal is inherited
        across exec, so callers install no-op handlers instead of ``SIG_IGN``. Raises
        ``OSError`` when the program cannot be started (including a failed ``prctl``)."""
        ...


def sanitize_path(path: str) -> str:
    """Drop empty and relative entries from a ``PATH`` value (interface.md, Path rules)."""
    return os.pathsep.join(p for p in path.split(os.pathsep) if os.path.isabs(p))


def check_argv(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None,
    extra_env: Mapping[str, str] | None,
) -> None:
    """The call contract shared by the real gateway and its fakes. Error messages name only
    ``argv[0]``: later arguments may carry secrets."""
    if isinstance(argv, str | bytes | bytearray):
        raise TypeError("gateway argv must be a sequence of strings, not a single string")
    if not argv:
        raise ValueError("gateway needs an argv, got an empty one")
    if not isinstance(argv[0], str) or not os.path.isabs(argv[0]):
        raise ValueError(f"gateway needs an absolute executable path, got {argv[0]!r}")
    if env is not None and extra_env is not None:
        raise ValueError("pass either env or extra_env, not both")


def check_kill_grace(kill_grace: float | None, *, new_process_group: bool) -> None:
    """``run``'s ``kill_grace`` contract, shared with the fakes."""
    if kill_grace is None:
        return
    if not new_process_group:
        raise ValueError("kill_grace needs new_process_group (it applies to the group)")
    if kill_grace < 0:
        raise ValueError(f"kill_grace must not be negative, got {kill_grace}")


def _child_env(
    env: Mapping[str, str] | None, extra_env: Mapping[str, str] | None
) -> dict[str, str]:
    if env is not None:
        return dict(env)
    base = dict(os.environ)
    base.update(extra_env or {})
    if "PATH" in base:
        base["PATH"] = sanitize_path(base["PATH"])
    return base


def _stream(value: int | Inherit | None) -> int | None:
    return None if value is Inherit.INHERIT else value


@dataclass(frozen=True)
class _Libc:
    prctl: Callable[..., int]
    signal: Callable[..., object]
    get_errno: Callable[[], int]


def _prctl() -> Callable[..., int]:
    import ctypes  # lazy: keeps the hook-event import path light

    return ctypes.CDLL(None, use_errno=True).prctl


def _libc() -> _Libc:
    import ctypes

    libc_signal = ctypes.CDLL(None).signal
    libc_signal.restype = ctypes.c_void_p
    libc_signal.argtypes = (ctypes.c_int, ctypes.c_void_p)
    return _Libc(_prctl(), libc_signal, ctypes.get_errno)


def _pdeathsig_preexec(parent: int | None = None) -> Callable[[], None] | None:
    """A ``preexec_fn`` setting ``PR_SET_PDEATHSIG(SIGTERM)``, or ``None`` off Linux.

    ``parent`` is the PID the child must see as its parent (default: this process)."""
    if not sys.platform.startswith("linux"):
        return None
    libc = _libc()  # resolved before the fork
    expected = os.getpid() if parent is None else parent

    def preexec() -> None:
        # An inherited SIG_IGN or blocked SIGTERM would make the death signal a no-op.
        libc.signal(signal.SIGTERM, _SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
        if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(libc.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
        if os.getppid() != expected:  # the parent died before the flag was set
            os._exit(PARENT_GONE_EXIT)

    return preexec


def _wait_exited_unreaped(pid: int, seconds: float) -> None:
    """Wait up to ``seconds`` for ``pid`` to exit, leaving it unreaped (so its PID, which is
    the process group ID, stays ours). Without ``os.waitid`` (macOS) it waits the full time."""
    waitid = getattr(os, "waitid", None)
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        if waitid is not None:
            try:
                if waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None:
                    return
            except ChildProcessError:
                return
        time.sleep(min(_GRACE_POLL, remaining))


def _signal_group(popen: subprocess.Popen[str], sig: int) -> None:
    if popen.returncode is not None:  # leader reaped: its PID may name another group now
        return
    try:
        os.killpg(popen.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _end(popen: subprocess.Popen[str], *, group: bool, kill_grace: float | None) -> None:
    """End a ``run`` child (see ``GatewayTimeout``) and reap it."""
    if group:
        if kill_grace is not None:
            _signal_group(popen, signal.SIGTERM)
            _wait_exited_unreaped(popen.pid, kill_grace)
        _signal_group(popen, signal.SIGKILL)
    else:
        popen.kill()
    popen.wait()


class _PopenChild:
    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    def poll(self) -> int | None:
        return self._popen.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self._popen.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def send_signal(self, sig: int) -> None:
        self._popen.send_signal(sig)  # Popen skips the signal once the child is reaped


class SystemGateway:
    """The real gateway."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        timeout: float | None = None,
        new_process_group: bool = False,
        kill_grace: float | None = None,
    ) -> Result:
        check_argv(argv, env=env, extra_env=extra_env)
        check_kill_grace(kill_grace, new_process_group=new_process_group)
        args = tuple(argv)
        with subprocess.Popen(
            args,
            stdin=DEVNULL if stdin is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(env, extra_env),
            cwd=cwd,
            process_group=0 if new_process_group else None,
        ) as popen:
            try:
                out, err = popen.communicate(stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                _end(popen, group=new_process_group, kill_grace=kill_grace)
                raise GatewayTimeout(args, timeout) from None
            except BaseException:
                _end(popen, group=new_process_group, kill_grace=None)
                raise
        return Result(args, popen.returncode, out, err)

    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: int | Inherit = DEVNULL,
        stdout: int | Inherit | None = None,
        stderr: int | Inherit | None = None,
        new_process_group: bool = False,
        die_with_parent: bool = False,
    ) -> Child:
        check_argv(argv, env=env, extra_env=extra_env)
        try:
            popen = subprocess.Popen(
                tuple(argv),
                stdin=_stream(stdin),
                stdout=_stream(stdout),
                stderr=_stream(stderr),
                env=_child_env(env, extra_env),
                cwd=cwd,
                process_group=0 if new_process_group else None,
                preexec_fn=_pdeathsig_preexec() if die_with_parent else None,
            )
        except subprocess.SubprocessError as exc:  # preexec_fn failed in the child
            raise OSError(f"could not start {argv[0]!r}: {exc}") from exc
        return _PopenChild(popen)
