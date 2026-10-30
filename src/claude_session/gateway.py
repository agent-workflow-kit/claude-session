"""Gateway: the only module that starts subprocesses (design.md, Modules).

Standard library only (the ``hook-event`` fast path imports it). It runs ``claude``, ``git``, the
notes command, ``crontab``, the sandbox prerequisite checks and ``ps`` (procinfo on macOS).
Callers never import ``subprocess``; they receive a ``Gateway`` object, so tests inject
``tests/fake_gateway.FakeGateway`` without patching any call site.

Rules every call follows:

- ``argv[0]`` is an absolute path (resolved by the caller outside every session-writable path,
  interface.md, Path rules); a bare name is refused with ``ValueError``. There is never a shell.
- Environment: by default the child gets this process's environment with ``PATH`` sanitized
  (empty and relative entries dropped, see ``sanitize_path``), plus ``extra_env``. A caller that
  built a complete environment itself (the session environment, a hook's environment) passes it
  as ``env``; it is used unchanged, because those builders already apply the PATH rules.
- ``run`` gives the child ``/dev/null`` as stdin unless ``stdin`` text is given, so a child never
  consumes the hook payload on ``hook-event``'s stdin.

``die_with_parent`` (``spawn``, Linux only, ignored elsewhere) sets ``PR_SET_PDEATHSIG(SIGTERM)``
in the child between fork and exec (through ``ctypes``, resolved before the fork, in
``preexec_fn``), so a headless ``claude`` never outlives a dead ``exec``. The kernel sends the
signal when the *thread* that forked the child exits, so spawn such children from a thread that
lives as long as the process (the main thread). If the parent already died before the child set
the flag, the child sends itself SIGTERM.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Pass as a ``spawn`` stdin/stdout/stderr to connect it to /dev/null; ``None`` inherits.
DEVNULL = subprocess.DEVNULL

_PR_SET_PDEATHSIG = 1


@dataclass(frozen=True)
class Result:
    """A finished ``run``: exit code and decoded output (invalid UTF-8 replaced)."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class GatewayTimeout(Exception):
    """``run`` exceeded its timeout; the child was killed and reaped."""

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
    ) -> Result:
        """Run to completion, capturing text output. Raises ``GatewayTimeout``, and ``OSError``
        when the program cannot be started."""
        ...

    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        new_process_group: bool = False,
        die_with_parent: bool = False,
    ) -> Child:
        """Start without waiting. ``stdin``/``stdout``/``stderr`` are file descriptors,
        ``DEVNULL`` or ``None`` (inherit). ``new_process_group`` puts the child in its own
        process group (headless ``claude``, hooks); otherwise it stays in ours (interactive
        ``claude`` in the terminal's foreground group)."""
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
    """The call contract shared by the real gateway and its fakes."""
    if not argv or not os.path.isabs(argv[0]):
        raise ValueError(f"gateway needs an absolute executable path, got {list(argv)!r}")
    if env is not None and extra_env is not None:
        raise ValueError("pass either env or extra_env, not both")


def _child_env(
    env: Mapping[str, str] | None, extra_env: Mapping[str, str] | None
) -> dict[str, str]:
    if env is not None:
        return dict(env)
    base = dict(os.environ)
    if "PATH" in base:
        base["PATH"] = sanitize_path(base["PATH"])
    base.update(extra_env or {})
    return base


def _pdeathsig_preexec() -> Callable[[], None] | None:
    """A ``preexec_fn`` setting ``PR_SET_PDEATHSIG(SIGTERM)``, or ``None`` off Linux."""
    if not sys.platform.startswith("linux"):
        return None
    import ctypes  # lazy: keeps the hook-event import path light

    prctl = ctypes.CDLL(None, use_errno=True).prctl  # resolved before the fork
    parent = os.getpid()

    def preexec() -> None:
        if prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
        if os.getppid() != parent:  # the parent died before the flag was set
            os.kill(os.getpid(), signal.SIGTERM)

    return preexec


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
    ) -> Result:
        check_argv(argv, env=env, extra_env=extra_env)
        args = tuple(argv)
        try:
            proc = subprocess.run(
                args,
                input=stdin,
                stdin=DEVNULL if stdin is None else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(env, extra_env),
                cwd=cwd,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GatewayTimeout(args, timeout) from None
        return Result(args, proc.returncode, proc.stdout, proc.stderr)

    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        new_process_group: bool = False,
        die_with_parent: bool = False,
    ) -> Child:
        check_argv(argv, env=env, extra_env=extra_env)
        popen = subprocess.Popen(
            tuple(argv),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=_child_env(env, extra_env),
            cwd=cwd,
            process_group=0 if new_process_group else None,
            preexec_fn=_pdeathsig_preexec() if die_with_parent else None,
        )
        return _PopenChild(popen)
