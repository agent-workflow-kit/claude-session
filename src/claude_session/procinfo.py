"""Process start time and liveness (design.md, Modules; interface.md, Liveness).

A process is identified by its PID plus a **start-time token**: an opaque string, compared only
for equality, that changes when the PID is reused. ``is_alive(pid, token)`` is true only when
that exact process still runs and is not a zombie ("wrapper dead" means gone or zombie).

- Linux: ``<boot_id>:<starttime>``, where ``starttime`` is field 22 of ``/proc/<pid>/stat``
  (clock ticks since boot) and ``boot_id`` comes from ``/proc/sys/kernel/random/boot_id``, so a
  PID and tick count recorded before a reboot never match a process after it. Ticks are used
  rather than a wall-clock conversion because ``btime`` can shift by a second between reads.
- macOS: the ``lstart`` column of ``/bin/ps -o stat= -o lstart= -p <pid>`` with ``LC_ALL=C``
  and ``TZ=UTC`` (stable across callers' locales and zones), whitespace-normalized. Its
  resolution is one second; macOS allocates PIDs sequentially, so reuse within the same second
  is not a practical concern.

Standard library only; on macOS ``ps`` runs through the injected ``Gateway``.
"""

from __future__ import annotations

import os
import sys

from .gateway import Gateway

PS = "/bin/ps"
PS_TIMEOUT = 10.0
_DEAD_STATES = ("Z", "X")  # zombie, dead
_STARTTIME_INDEX = 22 - 3  # field 22, counted after the comm field (2) and state (3)


class ProcInfoError(Exception):
    """The platform reported something this module cannot interpret."""


def parse_proc_stat(text: str) -> tuple[str, int]:
    """``(state, starttime)`` from a ``/proc/<pid>/stat`` line.

    The comm field (2) is in parentheses and may itself contain spaces and parentheses, so the
    fields after it are found from the *last* ``)``.
    """
    head, sep, tail = text.rpartition(")")
    fields = tail.split()
    if not sep or "(" not in head or len(fields) <= _STARTTIME_INDEX:
        raise ProcInfoError(f"unparsable /proc stat line: {text[:200]!r}")
    try:
        return fields[0], int(fields[_STARTTIME_INDEX])
    except ValueError:
        raise ProcInfoError(f"unparsable /proc stat line: {text[:200]!r}") from None


def _check_pid(pid: int) -> None:
    # kill(2) treats 0 and negative PIDs as process groups; they never name one process.
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError(f"invalid pid: {pid!r}")


class ProcInfo:
    def __init__(
        self, gateway: Gateway, *, platform: str = sys.platform, proc_root: str = "/proc"
    ) -> None:
        self._gateway = gateway
        self._linux = platform.startswith("linux")
        self._proc_root = proc_root

    def start_time(self, pid: int) -> str | None:
        """The start-time token of a running process, or ``None`` if it is gone or a zombie."""
        _check_pid(pid)
        return self._linux_start(pid) if self._linux else self._ps_start(pid)

    def is_alive(self, pid: int, start: str) -> bool:
        """True only if ``pid`` is running, not a zombie, and its start-time token is ``start``.

        This is the ``(pid, start) -> bool`` liveness checker ``store`` takes."""
        return self.start_time(pid) == start

    def _linux_start(self, pid: int) -> str | None:
        try:
            with open(os.path.join(self._proc_root, str(pid), "stat"), encoding="utf-8") as fh:
                text = fh.read()
        except (FileNotFoundError, ProcessLookupError):
            return None
        state, ticks = parse_proc_stat(text)
        if state in _DEAD_STATES:
            return None
        return f"{self._boot_id()}:{ticks}"

    def _boot_id(self) -> str:
        path = os.path.join(self._proc_root, "sys", "kernel", "random", "boot_id")
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def _ps_start(self, pid: int) -> str | None:
        proc = self._gateway.run(
            [PS, "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
            extra_env={"LC_ALL": "C", "TZ": "UTC"},
            timeout=PS_TIMEOUT,
        )
        out = proc.stdout.split()
        if proc.returncode != 0:
            if not out and not proc.stderr.strip():
                return None  # no such process
            raise ProcInfoError(f"ps failed ({proc.returncode}): {proc.stderr.strip()[:200]}")
        # stat, then lstart: weekday, month, day, time, year.
        if len(out) != 6:
            raise ProcInfoError(f"unparsable ps output: {proc.stdout[:200]!r}")
        if out[0][0] in _DEAD_STATES:
            return None
        return " ".join(out[1:])
