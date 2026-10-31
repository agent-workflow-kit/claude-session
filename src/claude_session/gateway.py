"""Gateway: the only module that starts subprocesses (design.md, Modules).

Standard library only (the ``hook-event`` fast path imports it). It runs ``claude``, ``git``, the
notes command, lifecycle hooks, ``crontab``, the sandbox prerequisite checks and ``ps`` (procinfo
on macOS). Callers never import ``subprocess``; they receive a ``Gateway`` object, so tests inject
``tests/fake_gateway.FakeGateway`` without patching any call site.

Rules every call follows:

- ``argv`` is a sequence of strings (a bare ``str`` or ``bytes`` is refused with ``TypeError``)
  whose first entry is an absolute path, resolved by the caller outside every session-writable
  path (interface.md, Path rules); a bare name is refused with ``ValueError``. With
  ``executable`` (an absolute path: the realpath of a tool resolved by
  ``pathrules.resolve_tool``) the program run is ``executable`` and ``argv[0]`` is only the
  name the program sees (the bare tool name, so launchers that dispatch on ``argv[0]``, such
  as volta, mise or snap shims, keep working); it must then be a non-empty string. Never
  ``shell=True``: lifecycle hooks are run as an explicit absolute ``/bin/sh -c <cmd>`` argv.
- Environment: with no explicit ``env`` the child gets this process's environment, plus
  ``extra_env``, with empty and relative ``PATH`` entries dropped (``sanitize_path``; if none
  remain, ``PATH`` is ``/usr/bin:/bin``). That
  default carries *every* inherited variable, secrets and ``CSESSION_*`` included, so the
  builders for hooks, the notes command and the session pass a complete ``env`` instead; it is
  used unchanged, because those builders apply the PATH rules themselves.
- ``run`` gives the child ``/dev/null`` as stdin unless ``stdin`` text is given, and ``spawn``
  does too unless ``INHERIT`` is passed (interactive ``claude``; ``None`` is refused), so a
  child never consumes the hook payload on ``hook-event``'s stdin.
- Lifecycle hooks use ``run(..., new_process_group=True, kill_grace=...)``: on timeout the
  group gets SIGTERM, then SIGKILL after ``kill_grace`` (interface.md, Hooks). The notes command
  uses ``run(..., new_process_group=True)`` with no grace (interface.md specifies none): on
  timeout its group gets SIGKILL. Either way a timeout ends everything still in the child's
  process group; a descendant that calls ``setsid``/``setpgid`` escapes (see
  ``GatewayTimeout``).
- The ``claude --version`` probe adds ``kill_group_on_exit=True``: its group also gets SIGKILL
  when ``claude`` exits normally (Linux: after the exit, before the leader is reaped, so the
  group ID still names that group), so no descendant outlives the probe.

``die_with_parent`` (``spawn``, Linux only, ignored elsewhere) sets ``PR_SET_PDEATHSIG(SIGTERM)``
in the child between fork and exec (through ``ctypes``, resolved before the fork, in
``preexec_fn``), so a headless ``claude`` never outlives a dead ``exec``. The kernel sends the
signal when the *thread* that forked the child exits, so spawn such children from a thread that
lives as long as the process (the main thread). If the parent already died before the child set
the flag, the child exits with ``PARENT_GONE_EXIT`` without running the program. Because
``preexec_fn`` is Python code, the forking thread blocks every signal around the fork; the child
resets each signal that has a Python handler here (and SIGTERM) to its default action before
restoring the mask, so a signal arriving between fork and exec gets the default action and never
runs this process's handlers in the child. Only the forking thread blocks signals: other threads
still take them, and Python runs every handler in the main thread of this process, which is
harmless to the child. Such a handler can run in the forking thread at any point between its
Python statements, even with every signal blocked there, so ``spawn`` changes the mask through the
C function behind ``signal.pthread_sigmask``, not through its Python wrapper: a handler that
raises then does so only after the mask is restored, never before (Python 3.13 runs pending
handlers on entry to a Python function). Residuals: signals to the forking thread wait until the
child has exec'd (or failed to); a handler (for a signal another thread took) that raises inside
``Popen`` after the fork, before ``spawn`` holds the ``Popen``, leaves that child running. ``run``
(Linux) starts its child the same way, without the death signal, so a handler that raises in
``exec``'s launch phase does so only once ``run`` holds the ``Popen`` and ends its child.

``run`` reads the child's output with its own loop rather than ``Popen.communicate``, which
reaps the child when a ``KeyboardInterrupt`` interrupts it: on Linux the child stays unreaped
until it has exited, so its process group can still be ended safely when an exception escapes.
Best effort without ``os.waitid`` (macOS): the final wait is ``Popen.wait``, which can reap the
leader when interrupted, after which its group is no longer signaled.
"""

from __future__ import annotations

import enum
import os
import select
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
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

# PATH when sanitizing leaves no entry: an empty PATH makes some programs search the cwd.
DEFAULT_PATH: Final = "/usr/bin:/bin"

_PR_SET_PDEATHSIG = 1
_SIG_DFL = 0
# Group scans during the kill grace: first interval, doubling up to the maximum.
_GRACE_POLL_FIRST = 0.05
_GRACE_POLL_MAX = 0.5
# Exit polls once the output is closed (the child usually exits with it).
_EXIT_POLL_FIRST = 0.001
_EXIT_POLL_MAX = 0.05
_READ_SIZE = 32768
_PIPE_BUF = getattr(select, "PIPE_BUF", 512)  # a write this small never blocks once writable


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
    - with ``new_process_group``: the child's process group got SIGTERM, then SIGKILL once
      ``kill_grace`` seconds passed or no live process was left in the group (with
      ``kill_grace=None``, SIGKILL straight away), and the leader was reaped. On Linux the
      leader stays unreaped until then, so the group ID cannot have been reused, and the group
      is not signaled at all once the leader was reaped elsewhere (best effort without
      ``os.waitid``, on macOS: an interrupted wait can reap the leader early, and the group is
      then not signaled). "Empty group" is detected through
      ``/proc`` on Linux (two scans in a row finding no live member); elsewhere the full grace
      is waited. A descendant that left the group (``setsid``/``setpgid``) is not signaled.
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
        executable: str | None = None,
        kill_group_on_exit: bool = False,
    ) -> Result:
        """Run to completion, capturing text output. ``new_process_group`` puts the child in
        its own process group, which a timeout ends as a whole; ``kill_grace`` (only with a
        group) is the longest SIGTERM-to-SIGKILL delay (see ``GatewayTimeout``).
        ``kill_group_on_exit`` (only with a group) also SIGKILLs the group when the leader
        exits normally, so no descendant outlives the run: on Linux it is sent once the
        leader has exited but before it is reaped (the unreaped leader keeps the group ID
        from being reused), and only to that group; without ``os.waitid`` (macOS) the
        leader is reaped first and the group is not signaled (best effort). An exception
        while waiting (a signal handler raising, ``KeyboardInterrupt`` included) ends the child
        the same way, then propagates (on Linux; best effort without ``os.waitid``, see
        ``GatewayTimeout``); one raised during the grace still gets the group SIGKILL and the
        leader reaped first. Raises ``GatewayTimeout``, and ``OSError`` when the
        program cannot be started."""
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
        executable: str | None = None,
    ) -> Child:
        """Start without waiting. Streams are file descriptors, ``DEVNULL`` or ``INHERIT``;
        ``stdin`` defaults to ``DEVNULL`` (``None`` raises ``TypeError``: pass ``INHERIT``),
        ``stdout``/``stderr`` to inheriting (``None``).
        ``new_process_group`` puts the child in its own process group (headless ``claude``);
        otherwise it stays in ours (interactive ``claude`` in the terminal's foreground group).

        ``die_with_parent`` uses ``preexec_fn``, which is not safe while other threads run:
        spawn such children before starting worker threads where possible. The forking thread
        blocks every signal around the fork and restores its mask afterwards, even when that
        raises; if a pending handler raises as the mask is restored, the started child is
        killed and reaped before the exception propagates. The child resets
        SIGTERM and every signal with a Python handler to its default action and unblocks
        SIGTERM, so the death signal works even when this process ignores or blocks SIGTERM;
        the rest of the signal mask, and any other ignored (``SIG_IGN``) signal, is inherited
        across exec, so callers install no-op handlers instead of ``SIG_IGN``. Raises
        ``OSError`` when the program cannot be started (including a failed ``prctl``)."""
        ...


def sanitize_path(path: str) -> str:
    """Drop empty and relative entries from a ``PATH`` value (interface.md, Path rules). If
    none remain, ``DEFAULT_PATH``: an empty ``PATH`` would make some programs search the cwd."""
    kept = os.pathsep.join(p for p in path.split(os.pathsep) if os.path.isabs(p))
    return kept or DEFAULT_PATH


def check_argv(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None,
    extra_env: Mapping[str, str] | None,
    executable: str | None = None,
) -> None:
    """The call contract shared by the real gateway and its fakes. Error messages name only
    ``argv[0]`` (and ``executable``): later arguments may carry secrets."""
    if isinstance(argv, str | bytes | bytearray):
        raise TypeError("gateway argv must be a sequence of strings, not a single string")
    if not argv:
        raise ValueError("gateway needs an argv, got an empty one")
    if executable is not None:
        if not isinstance(executable, str) or not os.path.isabs(executable):
            raise ValueError(f"gateway needs an absolute executable path, got {executable!r}")
        if not isinstance(argv[0], str) or not argv[0] or "\x00" in argv[0]:
            raise ValueError(f"gateway needs a non-empty argv[0], got {argv[0]!r}")
    elif not isinstance(argv[0], str) or not os.path.isabs(argv[0]):
        raise ValueError(f"gateway needs an absolute executable path, got {argv[0]!r}")
    if env is not None and extra_env is not None:
        raise ValueError("pass either env or extra_env, not both")


def check_spawn_stdin(stdin: object) -> None:
    """``spawn``'s ``stdin`` contract, shared with the fakes: ``None`` would silently inherit
    this process's stdin (the hook payload), so inheriting must be asked for with ``INHERIT``."""
    if stdin is None:
        raise TypeError("spawn stdin=None is refused: pass INHERIT to share this process's stdin")


def check_kill_grace(
    kill_grace: float | None, *, new_process_group: bool, kill_group_on_exit: bool = False
) -> None:
    """``run``'s ``kill_grace`` and ``kill_group_on_exit`` contract, shared with the fakes."""
    if kill_group_on_exit and not new_process_group:
        raise ValueError("kill_group_on_exit needs new_process_group (it applies to the group)")
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
    base["PATH"] = sanitize_path(base.get("PATH", ""))  # DEFAULT_PATH when unset
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


def _c_pthread_sigmask() -> Callable[[int, Iterable[int]], set[int]]:
    """``signal.pthread_sigmask`` without its Python wrapper: no handler can run before the C
    call changes the mask (Python 3.13 runs pending handlers on entry to the wrapper)."""
    import importlib  # lazy: keeps the hook-event import path light

    return importlib.import_module("_signal").pthread_sigmask


def _libc() -> _Libc:
    import ctypes

    libc_signal = ctypes.CDLL(None).signal
    libc_signal.restype = ctypes.c_void_p
    libc_signal.argtypes = (ctypes.c_int, ctypes.c_void_p)
    return _Libc(_prctl(), libc_signal, ctypes.get_errno)


def _pdeathsig_preexec(
    child_mask: set[int], parent: int | None = None, *, death_signal: bool = True
) -> Callable[[], None] | None:
    """A ``preexec_fn`` setting ``PR_SET_PDEATHSIG(SIGTERM)``, or ``None`` off Linux.

    The fork happens with every signal blocked (``spawn``, ``run``); the child resets
    the signals that have a Python handler here, and SIGTERM, to their default action, then
    sets ``child_mask`` (the caller's mask) without SIGTERM. ``parent`` is the PID the child
    must see as its parent (default: this process). Without ``death_signal`` (``run``) only
    the handlers and the mask are reset."""
    if not sys.platform.startswith("linux"):
        return None
    libc = _libc()  # resolved before the fork
    expected = os.getpid() if parent is None else parent
    handled = [s for s in signal.valid_signals() if callable(signal.getsignal(s))]
    reset = sorted({*handled, signal.SIGTERM})
    mask = child_mask - {signal.SIGTERM}

    def preexec() -> None:
        # A Python handler would run here, in the forked child, if its signal arrived; an
        # inherited SIG_IGN or blocked SIGTERM would make the death signal a no-op.
        for sig in reset:
            libc.signal(sig, _SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        if not death_signal:
            return
        if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(libc.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
        if os.getppid() != expected:  # the parent died before the flag was set
            os._exit(PARENT_GONE_EXIT)

    return preexec


def _reaped_elsewhere(pid: int) -> bool:
    """Whether ``pid`` is no longer our unreaped child (someone else reaped it), so its PID,
    the process group ID, may name another group now. Without ``os.waitid`` (macOS): never."""
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        return False
    try:
        waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    except ChildProcessError:
        return True
    return False


def _live_in_group(stat: bytes, pgid: int) -> bool:
    """Whether a ``/proc/<pid>/stat`` line is a live process in group ``pgid``: dead (``X``)
    and zombie (``Z``) processes are not, except a zombie whose other threads still run (its
    main thread exited)."""
    # After the comm (which may hold ")"): state, ppid, pgrp, ..., num_threads (index 17).
    fields = stat.rpartition(b")")[2].split()
    if len(fields) < 3 or fields[2] != str(pgid).encode():
        return False
    if fields[0] == b"X":
        return False
    if fields[0] == b"Z":
        return len(fields) > 17 and fields[17].isdigit() and int(fields[17]) > 1
    return True


def _group_has_live_member(pgid: int) -> bool:
    """Whether a live process (see ``_live_in_group``) is in group ``pgid``, by a ``/proc``
    scan (a zombie leader keeps ``killpg(pgid, 0)`` succeeding, so that cannot tell). True when
    unknown."""
    if not sys.platform.startswith("linux"):
        return True
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                stat = fh.read()
        except OSError:  # gone meanwhile
            continue
        if _live_in_group(stat, pgid):
            return True
    return False


def _wait_group_empty(
    pgid: int,
    seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait up to ``seconds`` until group ``pgid`` has no live member, leaving its leader
    unreaped. False if the leader was reaped elsewhere (stop signaling the group). The poll
    interval doubles from 0.05 s up to 0.5 s and never runs past the deadline."""
    deadline = monotonic() + seconds
    delay = _GRACE_POLL_FIRST
    while True:
        if _reaped_elsewhere(pgid):
            return False
        remaining = deadline - monotonic()
        if remaining <= 0:
            return True
        # Two empty scans in a row: a process forked while /proc was listed can be missed once.
        if not _group_has_live_member(pgid) and not _group_has_live_member(pgid):
            return True
        sleep(min(delay, remaining))
        delay = min(delay * 2, _GRACE_POLL_MAX)


def _signal_group(popen: subprocess.Popen[bytes], sig: int) -> bool:
    """Signal the child's group while its leader is still ours; False once it is not."""
    if popen.returncode is not None or _reaped_elsewhere(popen.pid):
        return False  # the leader's PID may name another group now
    try:
        os.killpg(popen.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    return True


def _end(popen: subprocess.Popen[bytes], *, group: bool, kill_grace: float | None) -> None:
    """End a ``run`` child (see ``GatewayTimeout``) and reap it. An exception during the grace
    (a signal handler raising) still gets the group SIGKILL and the leader reaped before it
    propagates; SIGKILL is skipped only once the leader was reaped elsewhere."""
    try:
        if not group:
            popen.kill()
        elif kill_grace is not None and _signal_group(popen, signal.SIGTERM):
            _wait_group_empty(popen.pid, kill_grace)
    finally:
        try:
            if group:
                _signal_group(popen, signal.SIGKILL)
        finally:
            try:
                # Again (both are idempotent): an exception may have interrupted the step above
                # before the kill was sent, and waiting for a leader that ignores SIGTERM would
                # then block forever.
                if group:
                    _signal_group(popen, signal.SIGKILL)
                else:
                    popen.kill()
            finally:
                popen.wait()


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


def _wait_exited(
    popen: subprocess.Popen[bytes],
    deadline: float | None,
    timeout: float | None,
    *,
    kill_group: bool = False,
) -> None:
    """Wait until the child exited, leaving it unreaped (``os.waitid`` with ``WNOWAIT``), then
    reap it; with ``kill_group``, SIGKILL its process group in between (the unreaped leader
    keeps the group ID from naming another group). Without ``os.waitid`` (macOS),
    ``Popen.wait``, which reaps when interrupted, and the group is not signaled."""
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        popen.wait(_remaining(deadline))
        return
    delay = _EXIT_POLL_FIRST
    while True:
        try:
            if waitid(os.P_PID, popen.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None:
                break
        except ChildProcessError:  # reaped elsewhere; Popen.wait copes
            break
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            assert timeout is not None  # a deadline comes from a timeout
            raise subprocess.TimeoutExpired(popen.args, timeout)
        time.sleep(delay if remaining is None else min(delay, remaining))
        delay = min(delay * 2, _EXIT_POLL_MAX)
    if kill_group:
        _signal_group(popen, signal.SIGKILL)  # skipped once the leader was reaped elsewhere
    popen.wait()  # it has exited: returns at once


def _exchange(
    popen: subprocess.Popen[bytes],
    data: bytes | None,
    timeout: float | None,
    *,
    kill_group: bool = False,
) -> tuple[bytes, bytes]:
    """Like ``Popen.communicate``: write ``data`` to stdin, read stdout and stderr to EOF,
    wait for the exit; ``subprocess.TimeoutExpired`` after ``timeout`` seconds. Unlike it, the
    child is never reaped before it has exited, even when interrupted (see the module docs)."""
    deadline = None if timeout is None else time.monotonic() + timeout
    assert popen.stdout is not None and popen.stderr is not None
    out_fd, err_fd = popen.stdout.fileno(), popen.stderr.fileno()
    readers = {out_fd: popen.stdout, err_fd: popen.stderr}
    output: dict[int, list[bytes]] = {fd: [] for fd in readers}
    pending = memoryview(data or b"")
    with selectors.DefaultSelector() as selector:
        for fd in readers:
            selector.register(fd, selectors.EVENT_READ)
        stdin = popen.stdin
        in_fd = -1 if stdin is None else stdin.fileno()
        if stdin is not None:
            if pending:
                selector.register(in_fd, selectors.EVENT_WRITE)
            else:
                stdin.close()
        while selector.get_map():
            remaining = _remaining(deadline)
            if remaining is not None and remaining <= 0:
                assert timeout is not None  # a deadline comes from a timeout
                raise subprocess.TimeoutExpired(popen.args, timeout)
            for key, _ in selector.select(remaining):
                if key.fd == in_fd:
                    try:
                        pending = pending[os.write(key.fd, pending[:_PIPE_BUF]) :]
                    except BrokenPipeError:  # the child stopped reading
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(key.fd)
                        assert stdin is not None
                        stdin.close()
                    continue
                chunk = os.read(key.fd, _READ_SIZE)
                if chunk:
                    output[key.fd].append(chunk)
                else:
                    selector.unregister(key.fd)
                    readers[key.fd].close()
    _wait_exited(popen, deadline, timeout, kill_group=kill_group)
    return b"".join(output[out_fd]), b"".join(output[err_fd])


def _text(data: bytes) -> str:
    """Decode as text mode would: invalid UTF-8 replaced, newlines translated to ``\\n``."""
    return data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def _kill_and_reap(popen: subprocess.Popen[bytes]) -> None:
    try:
        popen.kill()
    finally:
        popen.wait()


def _start_blocked(
    start: Callable[[Callable[[], None] | None], subprocess.Popen[bytes]],
    preexec: Callable[[set[int]], Callable[[], None] | None],
) -> subprocess.Popen[bytes]:
    """``start(preexec(caller_mask))`` with every signal blocked in this thread (see the
    module docstring): a handler that raises does so only once the mask is restored, and then
    the child it started is killed and reaped here, or the caller holds it."""
    # Read the mask without changing it; block inside the try, so the finally restores the
    # mask even when a pending handler raises from the blocking call itself.
    sigmask = _c_pthread_sigmask()
    saved_mask = {int(sig) for sig in sigmask(signal.SIG_BLOCK, [])}
    popen = None
    try:
        sigmask(signal.SIG_BLOCK, signal.valid_signals())
        popen = start(preexec(saved_mask))
    finally:
        try:
            sigmask(signal.SIG_SETMASK, saved_mask)
        except BaseException:
            if popen is not None:  # started, but the caller never gets it
                _kill_and_reap(popen)
            raise
    assert popen is not None  # start() returned or raised
    return popen


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
        executable: str | None = None,
        kill_group_on_exit: bool = False,
    ) -> Result:
        check_argv(argv, env=env, extra_env=extra_env, executable=executable)
        check_kill_grace(
            kill_grace, new_process_group=new_process_group, kill_group_on_exit=kill_group_on_exit
        )
        args = tuple(argv)
        data = None if stdin is None else stdin.encode("utf-8", "replace")

        def start(preexec: Callable[[], None] | None) -> subprocess.Popen[bytes]:
            try:
                return subprocess.Popen(
                    args,
                    executable=executable,
                    stdin=DEVNULL if stdin is None else subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=_child_env(env, extra_env),
                    cwd=cwd,
                    process_group=0 if new_process_group else None,
                    preexec_fn=preexec,
                )
            except subprocess.SubprocessError as exc:  # preexec_fn failed in the child
                raise OSError(f"could not start {argv[0]!r}: {exc}") from exc

        if sys.platform.startswith("linux"):
            # A signal handler that raises (exec's launch phase) does so only once run()
            # holds the Popen, so the child is ended (see the module docstring).
            popen = _start_blocked(start, lambda mask: _pdeathsig_preexec(mask, death_signal=False))
        else:
            popen = start(None)
        try:
            with popen:
                try:
                    out, err = _exchange(popen, data, timeout, kill_group=kill_group_on_exit)
                except subprocess.TimeoutExpired:
                    _end(popen, group=new_process_group, kill_grace=kill_grace)
                    raise GatewayTimeout(args, timeout) from None
                except BaseException:
                    _end(popen, group=new_process_group, kill_grace=kill_grace)
                    raise
        except BaseException:
            if popen.returncode is None:  # raised before the inner handler (entering with)
                _end(popen, group=new_process_group, kill_grace=kill_grace)
            raise
        return Result(args, popen.returncode, _text(out), _text(err))

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
        executable: str | None = None,
    ) -> Child:
        check_argv(argv, env=env, extra_env=extra_env, executable=executable)
        check_spawn_stdin(stdin)

        def start(preexec: Callable[[], None] | None) -> subprocess.Popen[bytes]:
            try:
                return subprocess.Popen(
                    tuple(argv),
                    executable=executable,
                    stdin=_stream(stdin),
                    stdout=_stream(stdout),
                    stderr=_stream(stderr),
                    env=_child_env(env, extra_env),
                    cwd=cwd,
                    process_group=0 if new_process_group else None,
                    preexec_fn=preexec,
                )
            except subprocess.SubprocessError as exc:  # preexec_fn failed in the child
                raise OSError(f"could not start {argv[0]!r}: {exc}") from exc

        if not (die_with_parent and sys.platform.startswith("linux")):
            return _PopenChild(start(None))
        return _PopenChild(_start_blocked(start, _pdeathsig_preexec))
