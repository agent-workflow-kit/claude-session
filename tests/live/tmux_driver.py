"""A private tmux server for interactive live tests (and its unit tests, run with ``sh``/``cat``
instead of ``claude`` at no cost).

The server is isolated three ways: ``-f /dev/null`` (no tmux configuration), ``-S <socket>``
(a socket inside the test's private root, never the user's default one) and the private
environment the caller passes (``HOME``/``XDG_*``; ``TMUX`` is not in it). ``remain-on-exit``
keeps the pane after its command ends, so the exit status is readable. ``kill`` stops only
this server, by its socket, also after a failed ``start`` (the server may already be up).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from claude_session import gateway

SESSION = "live"


@dataclass(frozen=True)
class PaneState:
    pid: int
    dead: bool
    exit_status: int | None
    exit_signal: int | None


def parse_pane_state(line: str) -> PaneState:
    """Parse ``#{pane_pid} #{pane_dead} #{pane_dead_status} #{pane_dead_signal}``; the last two
    are empty for a pane still running (and the last one for tmux builds without it)."""
    parts = line.strip().split(" ")
    pid = int(parts[0])
    dead = len(parts) > 1 and parts[1] == "1"

    def number(index: int) -> int | None:
        if index < len(parts) and parts[index].lstrip("-").isdigit():
            return int(parts[index])
        return None

    return PaneState(pid, dead, number(2) if dead else None, number(3) if dead else None)


class Tmux:
    def __init__(self, env: Mapping[str, str], socket: Path, *, tmux: str | None = None) -> None:
        self._env = dict(env)
        self._env.pop("TMUX", None)
        self._socket = socket
        found = tmux or shutil.which("tmux", path=self._env.get("PATH"))
        if found is None:
            raise FileNotFoundError("tmux is not installed")
        self._tmux = os.path.abspath(found)  # the gateway wants an absolute path
        self._gw = gateway.SystemGateway()
        self.started = False

    def _run(self, *args: str, timeout: float = 15) -> gateway.Result:
        argv = [self._tmux, "-f", "/dev/null", "-S", str(self._socket), *args]
        return self._gw.run(argv, env=self._env, timeout=timeout)

    def start(self, argv: Sequence[str], *, cwd: Path, width: int = 200, height: int = 50) -> None:
        """Run ``argv`` (executed directly, not through a shell) in a detached pane."""
        result = self._run(
            "start-server", ";",
            "set-option", "-g", "exit-empty", "off", ";",
            "set-option", "-g", "remain-on-exit", "on", ";",
            "new-session", "-d", "-s", SESSION, "-x", str(width), "-y", str(height),
            "-c", str(cwd), "--", *argv,
        )  # fmt: skip
        if result.returncode != 0:
            raise RuntimeError(f"tmux could not start: {result.stderr.strip()}")
        self.started = True

    def state(self) -> PaneState:
        fmt = "#{pane_pid} #{pane_dead} #{pane_dead_status} #{pane_dead_signal}"
        result = self._run("list-panes", "-t", SESSION, "-F", fmt)
        if result.returncode != 0:
            raise RuntimeError(f"tmux list-panes failed: {result.stderr.strip()}")
        return parse_pane_state(result.stdout.splitlines()[0])

    def capture(self) -> str:
        return self._run("capture-pane", "-p", "-t", SESSION).stdout

    def send_keys(self, *keys: str) -> None:
        """Send tmux key names (``Enter``, ``C-c``, ``Escape``, ``Down``...)."""
        self._run("send-keys", "-t", SESSION, *keys)

    def send_text(self, text: str) -> None:
        """Send literal text (no key-name interpretation)."""
        self._run("send-keys", "-t", SESSION, "-l", "--", text)

    def wait_for(self, needle: str, timeout: float, interval: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if needle in self.capture():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def wait_dead(self, timeout: float, interval: float = 0.25) -> PaneState | None:
        deadline = time.monotonic() + timeout
        while True:
            state = self.state()
            if state.dead:
                return state
            if time.monotonic() >= deadline:
                return None
            time.sleep(interval)

    def kill(self) -> None:
        """Stop this tmux server (identified by its private socket) and what it runs. Always
        sent, errors ignored: a ``start`` that failed after ``start-server`` leaves a server."""
        with contextlib.suppress(gateway.GatewayTimeout, OSError):
            self._run("kill-server")
        self.started = False
