"""Helpers to run the fake ``claude`` (``tests/fakebin/claude``, format in
``tests/fakebin/fake_claude.py``) from tests.

Two ways in, both starting the real fake process:

- through a gateway: ``FakeGateway(spawn_via=SystemGateway()).spawn([str(FAKE_CLAUDE), ...],
  extra_env=fake_env(scenario, log))`` (or any ``Gateway``'s ``spawn``/``run``);
- as ``claude_bin``: ``claude_bin(dir, scenario, log)`` writes an executable wrapper that sets
  the fake's variables itself, so it works whatever environment the code under test builds.

Every test that starts a fake that hangs or ignores SIGTERM relies on the autouse
``no_leftover_processes`` fixture in ``conftest.py`` (the process is started through
``subprocess.Popen`` in the test process) and should still end it itself by exact PID.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

FAKEBIN = Path(__file__).resolve().parent / "fakebin"
FAKE_CLAUDE = FAKEBIN / "claude"
RECORD_HOOK = FAKEBIN / "record_hook.py"

# The injected hooks (design.md, Generated settings; interface.md, Internal commands):
# (event, matcher, timeout in seconds).
INJECTED_HOOKS: tuple[tuple[str, str | None, int], ...] = (
    ("SessionStart", None, 30),
    ("UserPromptSubmit", None, 30),
    ("PermissionRequest", None, 30),
    ("Notification", "permission_prompt", 30),
    ("Notification", "idle_prompt", 30),
    ("Notification", "elicitation_dialog", 30),
    ("PostToolUse", None, 60),
    ("Stop", None, 60),
    ("SessionEnd", None, 30),
)


def recorder(
    log: Path,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    sleep: float = 0.0,
    ignore_term: bool = False,
    args: Sequence[str] = (),
) -> str:
    """A shell command running ``record_hook.py``: appends its call to ``log`` and answers with
    ``stdout``/``stderr``/``exit_code``. ``args`` are recorded (and more may follow)."""
    argv = [sys.executable, "-I", str(RECORD_HOOK), "--log", str(log)]
    argv += ["--stdout", stdout, "--stderr", stderr, "--exit", str(exit_code)]
    argv += ["--sleep", str(sleep)]
    if ignore_term:
        argv.append("--ignore-term")
    argv += ["--", *args]
    return shlex.join(argv)


def hook_settings(
    command_for: Callable[[str], str],
    hooks: Iterable[tuple[str, str | None, int]] = INJECTED_HOOKS,
) -> dict[str, Any]:
    """Settings in Claude Code's hook layout with one command per injected hook entry;
    ``command_for(event)`` gives the command (claude-session's is ``<python> -I -m
    claude_session hook-event --state-dir <dir> <Event>``)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for event, matcher, timeout in hooks:
        group: dict[str, Any] = {
            "hooks": [{"type": "command", "command": command_for(event), "timeout": timeout}]
        }
        if matcher is not None:
            group["matcher"] = matcher
        out.setdefault(event, []).append(group)
    return {"hooks": out}


def recording_settings(log: Path, **answers: Any) -> dict[str, Any]:
    """The injected hooks, each a recorder that records its event name as its argument."""
    return hook_settings(lambda event: recorder(log, args=[event], **answers))


def write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def fake_env(scenario: Path, log: Path | None = None) -> dict[str, str]:
    """The variables that point the fake at its scenario and log (``extra_env``)."""
    env = {"FAKE_CLAUDE_SCENARIO": str(scenario)}
    if log is not None:
        env["FAKE_CLAUDE_LOG"] = str(log)
    return env


def claude_bin(directory: Path, scenario: Path, log: Path | None = None) -> Path:
    """An executable ``claude`` wrapper for ``claude_bin``: sets the fake's variables and
    ``exec``s the fake under this interpreter (same PID, so signals reach the fake)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude"
    assigns = " ".join(f"{k}={shlex.quote(v)}" for k, v in fake_env(scenario, log).items())
    command = shlex.join([sys.executable, str(FAKEBIN / "claude")])
    path.write_text(f'#!/bin/sh\n{assigns} exec {command} "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def report_stub(directory: Path, log: Path, **answers: Any) -> Path:
    """A stand-in ``claude-session`` in ``directory`` (put it first on ``PATH``): records its
    arguments, stdin and environment in ``log``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude-session"
    path.write_text(f'#!/bin/sh\nexec {recorder(log, **answers)} "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def wait_for(
    path: Path, predicate: Callable[[Mapping[str, Any]], bool], timeout: float = 15.0
) -> dict[str, Any]:
    """Poll the JSON-lines ``path`` until a record matches ``predicate``; fail after
    ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        for record in read_jsonl(path):
            if predicate(record):
                return record
        if time.monotonic() > deadline:
            raise AssertionError(f"no matching record in {path} after {timeout} s")
        time.sleep(0.02)
