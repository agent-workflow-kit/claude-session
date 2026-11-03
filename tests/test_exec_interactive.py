"""``exec`` in interactive mode end to end, and the signal table (interface.md, exec and
Signals; design.md, Supervision): the ``claude-session`` entry point runs on a pseudo-terminal
(stdin and stdout are its slave side) and launches the fake ``claude`` (``tests/fakebin``),
which plays a scenario through the injected hooks.

Process discipline (PR #57 notes): every test synchronises on a marker file or a fake log
record before it signals, and signals only the exact ``exec`` process it started (or, through
the terminal, the foreground process group of the session it created for it). ``exec`` runs
in a session of its own (``start_new_session``), so the ``no_leftover_processes`` guard ends
its whole group, the fake included, if a test fails.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import select
import signal
import sqlite3
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from .fakeclaude import claude_bin, read_jsonl, wait_for, write_json
from .test_exec_headless import (
    START,
    claude_session_cmd,
    gone,
    ignored_signals,
    private_home,
    run_row_of,
    script,
    validate_exec,
)

FAST = "poll_interval: 0.05s\nauto_exit_delay: 0.3s\nkill_grace: 2s\n"
HANG = {"do": "hang"}
STOP = {"do": "stop"}


def report(status: str, **extra: Any) -> dict[str, Any]:
    if status in ("done",):
        return {"do": "report", "args": [status, "--summary", "finished"], **extra}
    return {"do": "report", "args": [status, "--reason-file", "-"], "heredoc": "why", **extra}


def touch(path: Path) -> dict[str, Any]:
    return {"do": "touch", "path": str(path)}


# The session leader for tests that need a controlling terminal: it takes the terminal, runs
# exec in its own (foreground) process group, ignores SIGINT with a handler (so exec does not
# inherit an ignored SIGINT), keeps the terminal open until the test has read exec's output
# (a hangup would discard it) and exits with exec's exit code.
LEADER = """\
import fcntl, signal, subprocess, sys, termios
signal.signal(signal.SIGINT, lambda *_: None)
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
code = subprocess.call(sys.argv[1:])
sys.stdin.readline()
sys.exit(code if code >= 0 else 128 - code)
"""


@dataclass
class Terminal:
    """``exec`` on a pseudo-terminal; the master side is read in a thread until ``stop``."""

    proc: subprocess.Popen[bytes]
    master: int
    tmp_path: Path
    state: Path
    leader: bool = False
    data: bytearray = field(default_factory=bytearray)
    reader: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)

    def read_all(self) -> None:
        while not self.stop.is_set():
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:  # EIO: every slave descriptor is closed
                return
            if not chunk:
                return
            self.data += chunk

    def type(self, data: bytes) -> None:
        os.write(self.master, data)

    def hang_up(self) -> None:
        """Close the master side (the reader stopped first, so nothing holds it open)."""
        self.stop.set()
        assert self.reader is not None
        self.reader.join(timeout=10)
        os.close(self.master)
        self.master = -1

    def outcome_lines(self) -> list[str]:
        text = self.data.decode("utf-8", "replace").replace("\r\n", "\n")
        # A Ctrl-C typed at the terminal is echoed as "^C" without a newline.
        lines = (line.removeprefix("^C") for line in text.split("\n"))
        return [line for line in lines if line.startswith("{")]

    def finish(self, timeout: float = 30) -> tuple[int, dict[str, Any] | None, str]:
        """Wait for ``exec``; its exit code, the outcome it printed on the terminal (None if
        none) and its stderr."""
        try:
            if self.leader:  # release the leader once exec's outcome is on the terminal
                deadline = time.monotonic() + timeout
                while not self.outcome_lines() and self.proc.poll() is None:
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                self.type(b"\n")
            _, err = self.proc.communicate(timeout=timeout)
        finally:
            if self.proc.returncode is None:
                # The process group this test created (start_new_session), the fake included,
                # SIGKILLed while its leader is still unreaped (its PID, the group ID, cannot
                # have been reused); then the leader is reaped.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
        if self.master >= 0:
            self.hang_up()
        lines = self.outcome_lines()
        outcome = json.loads(lines[-1]) if lines else None
        code = self.proc.returncode
        assert code is not None
        return code, outcome, err.decode("utf-8", "replace")

    def fake_log(self) -> list[dict[str, Any]]:
        return read_jsonl(self.tmp_path / "fake.jsonl")

    def exec_log(self) -> list[dict[str, Any]]:
        return read_jsonl(self.state / "log.jsonl")


def _child_setup(controlling: bool, ignore: Sequence[int]) -> Any:
    """The ``preexec_fn`` of exec: take the terminal as the controlling one, and ignore
    ``ignore`` (``nohup``)."""
    if not controlling and not ignore:
        return None

    def setup() -> None:  # pragma: no cover - runs in the child
        if controlling:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        for sig in ignore:
            signal.signal(sig, signal.SIG_IGN)

    return setup


def start_terminal(
    tmp_path: Path,
    state: Path,
    cwd: Path,
    steps: list[dict[str, Any]],
    *,
    args: Sequence[str] = (),
    config: str = FAST,
    controlling: str | None = None,
    env: Mapping[str, str] | None = None,
    patch: str = "",
    ignore: Sequence[int] = (),
    status_to: Path | None = None,
    signals: Mapping[str, Any] | None = None,
) -> Terminal:
    """``controlling``: None (the terminal is only exec's stdin and stdout), ``"exec"`` (it
    is exec's controlling terminal, exec the session leader) or ``"leader"`` (``LEADER`` is
    the session leader, exec in its foreground process group). ``patch``: Python run in
    exec's process before its entry point (a fault injected into exec). ``ignore``: signals
    exec inherits as ignored. ``status_to``: claude first copies its ``/proc`` status there
    (a shell wrapper that then execs the fake, same PID). ``signals``: the fake's signal
    overrides (its scenario ``signals``)."""
    body: dict[str, Any] = {"steps": steps}
    if signals is not None:
        body["signals"] = dict(signals)
    scenario = write_json(tmp_path / "scenario.json", body)
    fake = claude_bin(tmp_path / "fakebin", scenario, tmp_path / "fake.jsonl")
    if status_to is not None:
        fake = script(
            tmp_path / "wrapbin" / "claude",
            f'#!/bin/sh\n/bin/cat /proc/self/status > "{status_to}"\nexec "{fake}" "$@"\n',
        )
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {fake}\n{config}", encoding="utf-8")
    command = claude_session_cmd()
    if patch:
        command = [*command[:-1], patch + "\n" + command[-1]]
    argv = [*command, "exec", "--json", "--config", str(cfg), *args]
    if controlling == "leader":
        argv = [sys.executable, "-I", "-c", LEADER, *argv]
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env={**os.environ, **private_home(tmp_path), **(env or {})},
            stdin=slave,
            stdout=slave,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_child_setup(controlling == "exec", ignore),
        )
    finally:
        os.close(slave)
    term = Terminal(proc, master, tmp_path, state, leader=controlling == "leader")
    term.reader = threading.Thread(target=term.read_all, daemon=True)
    term.reader.start()
    return term


def wait_marker(term: Terminal, marker: Path, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not marker.exists():
        assert term.proc.poll() is None, term.proc.stderr.read() if term.proc.stderr else ""
        assert time.monotonic() < deadline
        time.sleep(0.02)


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def events(state: Path) -> list[tuple[Any, ...]]:
    db = sqlite3.connect(f"file:{state / 'state.db'}?mode=ro", uri=True)
    try:
        return db.execute(
            "SELECT cause, to_state, to_activity, needs_input_reason, final FROM events ORDER BY id"
        ).fetchall()
    finally:
        db.close()


def signals_sent(term: Terminal) -> list[tuple[Any, ...]]:
    return [
        (r.get("pid"), r["signal"], r.get("message"))
        for r in term.exec_log()
        if r["event"] == "signal_sent"
    ]


def signals_not_sent(term: Terminal) -> list[tuple[Any, ...]]:
    return [
        (r.get("pid"), r["signal"], r.get("message"))
        for r in term.exec_log()
        if r["event"] == "signal_not_sent"
    ]


def fake_signals(term: Terminal) -> list[tuple[str, str]]:
    return [
        (r["event"], r["signal"])
        for r in term.fake_log()
        if r["event"] in ("signal", "signal_ignored")
    ]


def check_outcome(code: int, outcome: dict[str, Any] | None, err: str) -> dict[str, Any]:
    assert outcome is not None, err
    validate_exec(outcome)
    assert outcome["exit_code"] == code, err
    assert "Traceback" not in err
    return outcome


# A whole interactive run, and auto-exit


def test_an_interactive_run_goes_to_permission_and_reported_then_auto_exits(
    tmp_path: Path, state: Path, work: Path
) -> None:
    term = start_terminal(
        tmp_path,
        state,
        work,
        [
            START,
            {"do": "user_prompt_submit", "prompt": "go"},
            {"do": "permission_request", "tool_name": "Bash", "tool_input": {"command": "ls"}},
            {"do": "post_tool_use", "tool_name": "Bash", "tool_input": {"command": "ls"}},
            report("done"),
            STOP,
            HANG,
        ],
    )
    code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert code == 0
    assert (outcome["outcome"], outcome["end_reason"], outcome["state"]) == (
        "done",
        "auto_exit",
        "exited",
    )
    assert outcome["report"]["summary"] == "finished"
    assert outcome["cost_usd"] is None  # interactive
    row = run_row_of(state)
    assert row["mode"] == "interactive" and row["pending_cause"] == "auto_exit"
    changes = events(state)
    assert ("SessionStart", "idle", "idle", None, 0) in changes  # interactive: idle
    assert ("PermissionRequest", "needs-input", "working", "permission", 0) in changes
    assert ("Stop", "reported", "idle", None, 0) in changes
    assert changes[-1] == ("auto_exit", "exited", None, None, 1)
    started = next(r for r in term.fake_log() if r["event"] == "start")
    assert started["pid"] == row["child_pid"]
    assert fake_signals(term) == [("signal", "TERM")]
    assert signals_sent(term) == [(row["child_pid"], "SIGTERM", "auto_exit")]
    assert gone(row["child_pid"])


def test_the_interactive_child_shares_the_terminal_and_the_foreground_group(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """design.md, Supervision: interactive claude is started in exec's (the terminal's
    foreground) process group, no setsid, with the terminal inherited."""
    marker = tmp_path / "ready"
    term = start_terminal(tmp_path, state, work, [START, touch(marker), HANG])
    try:
        wait_marker(term, marker)
        child = run_row_of(state)["child_pid"]

        def stat(pid: int) -> list[str]:
            return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()

        groups = [stat(child)[2:4], stat(term.proc.pid)[2:4]]
        ttys = [os.readlink(f"/proc/{term.proc.pid}/fd/{fd}") for fd in (0, 1, 2)]
        ttys += [os.readlink(f"/proc/{child}/fd/{fd}") for fd in (0, 1, 2)]
    finally:
        term.proc.send_signal(signal.SIGTERM)
        code, outcome, err = term.finish()
    assert groups[0] == groups[1]  # same process group and session
    assert ttys[0].startswith("/dev/pts/")
    assert ttys[3:] == ttys[:3], ttys  # inherited: the terminal, and exec's stderr
    assert check_outcome(code, outcome, err)["end_reason"] == "forwarded_term"


@pytest.mark.parametrize(
    ("steps", "args", "status", "code"),
    [
        # no done report
        ([report("blocked"), STOP], (), "blocked", 13),
        # a Stop with background tasks
        ([report("done"), {"do": "stop", "background_tasks": [{"id": "t1"}]}], (), "done", 0),
        # auto-exit off for this run
        ([report("done"), STOP], ("--no-auto-exit",), "done", 0),
        # a waiting (denied) prompt: needs-input, not reported
        (
            [
                report("done"),
                {"do": "permission_request", "tool_name": "Bash", "tool_input": {}},
                STOP,
            ],
            (),
            "done",
            0,
        ),
    ],
)
def test_no_auto_exit_without_a_done_report_and_a_qualifying_stop(
    tmp_path: Path,
    state: Path,
    work: Path,
    steps: list[dict[str, Any]],
    args: tuple[str, ...],
    status: str,
    code: int,
) -> None:
    marker = tmp_path / "idle"
    term = start_terminal(tmp_path, state, work, [START, *steps, touch(marker), HANG], args=args)
    try:
        wait_marker(term, marker)
        time.sleep(1.2)  # four auto-exit delays: nothing may happen meanwhile
        assert term.proc.poll() is None
        assert fake_signals(term) == []
        term.proc.send_signal(signal.SIGTERM)  # the exact exec started here
    finally:
        got, outcome, err = term.finish()
    outcome = check_outcome(got, outcome, err)
    assert got == code
    assert (outcome["outcome"], outcome["end_reason"]) == (status, "forwarded_term")
    assert run_row_of(state)["pending_cause"] == "forwarded_term"


def test_auto_exit_rechecks_the_latest_event_before_sigterm(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """A UserPromptSubmit after the qualifying Stop resets the wait: claude is ended only
    after the next qualifying Stop, and the delay counts from that Stop."""
    marker = tmp_path / "second-stop"
    term = start_terminal(
        tmp_path,
        state,
        work,
        [
            START,
            report("done"),
            STOP,
            {"do": "user_prompt_submit", "prompt": "one more thing"},
            {"do": "sleep", "seconds": 1.0},
            touch(marker),
            STOP,
            HANG,
        ],
    )
    code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["outcome"], outcome["end_reason"]) == (0, "done", "auto_exit")
    log = term.fake_log()
    steps = [i for i, r in enumerate(log) if r["event"] == "step" and r["step"]["do"] == "stop"]
    term_at = next(i for i, r in enumerate(log) if r["event"] == "signal")
    assert len(steps) == 2 and term_at > steps[1]  # only after the second Stop


def test_auto_exit_counts_the_delay(tmp_path: Path, state: Path, work: Path) -> None:
    marker = tmp_path / "stopped"
    config = "poll_interval: 0.05s\nauto_exit_delay: 1.5s\nkill_grace: 2s\n"
    term = start_terminal(
        tmp_path, state, work, [START, report("done"), STOP, touch(marker), HANG], config=config
    )
    wait_marker(term, marker)
    stopped = time.monotonic()
    code, outcome, err = term.finish()
    took = time.monotonic() - stopped
    assert check_outcome(code, outcome, err)["end_reason"] == "auto_exit"
    assert took >= 1.3, took


# The signal table, interactive session phase


def test_sigint_to_interactive_exec_is_ignored_and_never_forwarded(
    tmp_path: Path, state: Path, work: Path
) -> None:
    marker = tmp_path / "running"
    term = start_terminal(
        tmp_path,
        state,
        work,
        [START, touch(marker), {"do": "sleep", "seconds": 1.5}, {"do": "exit", "code": 0}],
    )
    try:
        wait_marker(term, marker)
        term.proc.send_signal(signal.SIGINT)
        term.proc.send_signal(signal.SIGINT)
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    # Ctrl-C is not a kill: claude ended the run itself.
    assert (code, outcome["outcome"], outcome["end_reason"]) == (14, "exited-unreported", "normal")
    assert run_row_of(state)["pending_cause"] is None
    assert fake_signals(term) == []
    assert signals_sent(term) == []


def test_ctrl_c_at_the_terminal_reaches_claude_and_not_the_end_mapping(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """With the pseudo-terminal as exec's controlling terminal, Ctrl-C sends SIGINT to the
    foreground process group: exec and claude. claude handles it (one Ctrl-C does not exit,
    spike), exec ignores it."""
    marker = tmp_path / "running"
    term = start_terminal(
        tmp_path,
        state,
        work,
        [START, touch(marker), {"do": "sleep", "seconds": 2.0}, {"do": "exit", "code": 0}],
        controlling="leader",
    )
    try:
        wait_marker(term, marker)
        term.type(b"\x03")
        wait_for(tmp_path / "fake.jsonl", lambda r: r["event"] == "signal_ignored")
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["end_reason"]) == (14, "normal")
    assert fake_signals(term) == [("signal_ignored", "INT")]
    assert run_row_of(state)["pending_cause"] is None
    assert signals_sent(term) == []


@pytest.mark.parametrize(
    ("sig", "cause", "name"),
    [(signal.SIGTERM, "forwarded_term", "TERM"), (signal.SIGHUP, "forwarded_hup", "HUP")],
)
def test_sigterm_and_sighup_are_forwarded_once_and_end_the_run_killed(
    tmp_path: Path, state: Path, work: Path, sig: int, cause: str, name: str
) -> None:
    marker = tmp_path / "running"
    term = start_terminal(tmp_path, state, work, [START, touch(marker), HANG])
    try:
        wait_marker(term, marker)
        term.proc.send_signal(sig)
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["outcome"], outcome["end_reason"], outcome["state"]) == (
        15,
        "killed",
        cause,
        "killed",
    )
    row = run_row_of(state)
    assert row["pending_cause"] == cause
    assert fake_signals(term) == [("signal", name)]
    assert signals_sent(term) == [(row["child_pid"], f"SIG{name}", cause)]
    assert gone(row["child_pid"])


def test_sigterm_with_a_report_keeps_the_reports_outcome(
    tmp_path: Path, state: Path, work: Path
) -> None:
    marker = tmp_path / "reported"
    term = start_terminal(
        tmp_path,
        state,
        work,
        [START, report("needs-input"), touch(marker), HANG],
    )
    try:
        wait_marker(term, marker)
        term.proc.send_signal(signal.SIGTERM)
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["outcome"], outcome["end_reason"]) == (
        13,
        "needs-input",
        "forwarded_term",
    )


def test_sigterm_after_a_stop_request_is_stopped(tmp_path: Path, state: Path, work: Path) -> None:
    """interface.md, Signals: a SIGTERM from stop is pending_cause stopped. The stop request
    is recorded here the way stop (#32) records it: stop_requested_at on the run."""
    marker = tmp_path / "running"
    term = start_terminal(tmp_path, state, work, [START, touch(marker), HANG])
    try:
        wait_marker(term, marker)
        db = sqlite3.connect(str(state / "state.db"))
        try:
            with db:
                db.execute("UPDATE runs SET stop_requested_at = '2026-10-31T00:00:00Z'")
        finally:
            db.close()
        term.proc.send_signal(signal.SIGTERM)
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["outcome"], outcome["end_reason"]) == (15, "killed", "stopped")
    assert run_row_of(state)["pending_cause"] == "stopped"


def test_a_child_ignoring_sigterm_is_sigkilled_after_kill_grace_and_signaled_once(
    tmp_path: Path, state: Path, work: Path
) -> None:
    marker = tmp_path / "running"
    term = start_terminal(
        tmp_path,
        state,
        work,
        [START, {"do": "ignore_signals", "signals": ["TERM", "HUP"]}, touch(marker), HANG],
        config="poll_interval: 0.05s\nkill_grace: 1.5s\n",
    )
    try:
        wait_marker(term, marker)
        sent = time.monotonic()
        term.proc.send_signal(signal.SIGTERM)
        wait_for(tmp_path / "fake.jsonl", lambda r: r["event"] == "signal_ignored")
        term.proc.send_signal(signal.SIGHUP)  # during the grace: not forwarded
        term.proc.send_signal(signal.SIGTERM)
    finally:
        code, outcome, err = term.finish()
    took = time.monotonic() - sent
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["end_reason"]) == (15, "forwarded_term")
    assert took >= 1.4, took
    row = run_row_of(state)
    assert fake_signals(term) == [("signal_ignored", "TERM")]
    pid = row["child_pid"]
    sent_records = signals_sent(term)
    assert sent_records[0] == (pid, "SIGTERM", "forwarded_term")
    assert sent_records[-1] == (pid, "SIGKILL", "kill_grace")
    assert len(sent_records) == 2  # the others were received, not forwarded
    assert [r[1] for r in signals_not_sent(term)] == ["SIGHUP", "SIGTERM"]
    assert gone(pid)


def test_a_terminal_hangup_ends_the_run_and_exec_still_exits_with_its_code(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """Closing the terminal: exec (the session leader) gets SIGHUP and forwards it; the
    outcome cannot be printed on the dead terminal, the exit code still carries it."""
    marker = tmp_path / "running"
    term = start_terminal(tmp_path, state, work, [START, touch(marker), HANG], controlling="exec")
    wait_marker(term, marker)
    term.hang_up()
    code, outcome, err = term.finish()
    assert outcome is None  # the terminal was gone
    assert code == 15, err
    assert "Traceback" not in err
    row = run_row_of(state)
    assert (row["outcome"], row["end_reason"], row["exit_code"]) == (
        "killed",
        "forwarded_hup",
        15,
    )
    assert gone(row["child_pid"])


def test_prompt_from_stdin_on_a_terminal_is_a_usage_error(
    tmp_path: Path, state: Path, work: Path
) -> None:
    term = start_terminal(tmp_path, state, work, [START], args=["--prompt-file", "-"])
    code, outcome, err = term.finish()
    assert code == 2
    assert outcome is not None and outcome["error"]["code"] == "usage"
    assert not (state / "sessions").exists() or not any((state / "sessions").iterdir())


# Review round 1 (R1-02, R1-06, R1-09, R1-16)


def test_a_done_report_drained_by_the_stops_own_hook_auto_exits(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """R1-02: no PostToolUse for the report call, so the Stop hook drains it: the latest
    event is the report (reported, idle) right after the idle Stop, and auto-exit acts."""
    term = start_terminal(
        tmp_path,
        state,
        work,
        [
            START,
            {"do": "user_prompt_submit", "prompt": "go"},
            report("done", post_tool_use="never"),
            STOP,
            HANG,
        ],
    )
    code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["outcome"], outcome["end_reason"]) == (0, "done", "auto_exit")
    changes = events(state)
    assert changes[-3:-1] == [
        ("Stop", "idle", "idle", None, 0),
        ("report", "reported", "idle", None, 0),
    ]
    assert fake_signals(term) == [("signal", "TERM")]


def test_an_inherited_ignored_sighup_stays_ignored_for_exec_and_claude(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """R1-06: nohup: exec sends nothing on SIGHUP, and claude inherits the ignore."""
    marker = tmp_path / "running"
    status = tmp_path / "claude-status"
    term = start_terminal(
        tmp_path,
        state,
        work,
        [START, touch(marker), HANG],
        ignore=[signal.SIGHUP],
        status_to=status,
    )
    try:
        wait_marker(term, marker)
        assert ignored_signals(status) & (1 << (signal.SIGHUP - 1))
        term.proc.send_signal(signal.SIGHUP)  # the exact exec: ignored
        term.proc.send_signal(signal.SIGTERM)
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["end_reason"]) == (15, "forwarded_term")
    assert fake_signals(term) == [("signal", "TERM")]
    assert signals_not_sent(term) == []


def test_sighup_to_the_whole_group_ends_the_run_forwarded_hup(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """R1-09: a hangup reaches exec and claude together (the process group this test created
    for exec, while its leader is unreaped); claude may be reaped before exec takes its
    SIGHUP: the run still ends forwarded_hup, with nothing sent."""
    marker = tmp_path / "running"
    term = start_terminal(tmp_path, state, work, [START, touch(marker), HANG])
    try:
        wait_marker(term, marker)
        os.killpg(term.proc.pid, signal.SIGHUP)
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["outcome"], outcome["end_reason"]) == (15, "killed", "forwarded_hup")
    assert run_row_of(state)["pending_cause"] == "forwarded_hup"
    received = fake_signals(term)  # from the group signal, and exec's forward if it was first
    assert received and set(received) == {("signal", "HUP")}
    assert len(signals_sent(term)) + len(signals_not_sent(term)) == 1


# Faults injected into the interactive exec (its process, before the entry point)
SIGNAL_IN_VALIDATE = """\
import os, signal
from claude_session import launch
_real_validate = launch.validate
def _validate(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGTERM)
    return _real_validate(*args, **kwargs)
launch.validate = _validate
"""
SIGINT_IN_SPAWN = """\
import os, signal
from claude_session import gateway
_real_spawn = gateway.SystemGateway.spawn
def _spawn(self, *args, **kwargs):
    os.kill(os.getpid(), signal.SIGINT)
    return _real_spawn(self, *args, **kwargs)
gateway.SystemGateway.spawn = _spawn
"""


def test_a_signal_in_the_interactive_launch_phase_ends_the_run_launch_interrupted(
    tmp_path: Path, state: Path, work: Path
) -> None:
    term = start_terminal(tmp_path, state, work, [START, HANG], patch=SIGNAL_IN_VALIDATE)
    code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["phase"], outcome["outcome"], outcome["end_reason"]) == (
        12,
        "launch",
        "not-started",
        "launch_interrupted",
    )
    assert not (tmp_path / "fake.jsonl").exists()  # claude was never spawned


def test_a_sigint_during_the_interactive_spawn_is_dropped(
    tmp_path: Path, state: Path, work: Path
) -> None:
    term = start_terminal(
        tmp_path, state, work, [START, {"do": "exit", "code": 0}], patch=SIGINT_IN_SPAWN
    )
    code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["outcome"], outcome["end_reason"]) == (14, "exited-unreported", "normal")
    assert run_row_of(state)["pending_cause"] is None
    assert fake_signals(term) == []
    assert signals_sent(term) == [] and signals_not_sent(term) == []
