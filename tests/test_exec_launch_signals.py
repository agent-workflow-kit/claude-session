"""The launch-phase column of the signal table (interface.md, Signals and Launch order:
Interrupted launch). ``exec`` runs in this process (``exec_in_process``) with its handlers
installed; a patched launch step signals this process itself (its exact PID) at a chosen point.
Headless runs, because the launch phase is the same in both modes and needs no terminal."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from claude_session import launch, signals
from claude_session.errors import LaunchValidationError

from .fakeclaude import claude_bin, write_json
from .test_exec_headless import (
    CLAUDE_SESSION,
    FAST,
    START,
    exec_in_process,
    private_home,
    result_step,
    run_row_of,
    validate_exec,
)

STEPS = [START, {"do": "stop"}, result_step()]


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def handlers_restored() -> Any:
    before = {sig: signal.getsignal(sig) for sig in signals.HANDLED}
    yield
    assert {sig: signal.getsignal(sig) for sig in signals.HANDLED} == before


def own(sig: int) -> None:
    os.kill(os.getpid(), sig)


def request_stop(state: Path) -> None:
    """Record a stop request the way ``stop`` (#32) does: ``stop_requested_at``."""
    db = sqlite3.connect(str(state / "state.db"))
    try:
        with db:
            db.execute("UPDATE runs SET stop_requested_at = '2026-10-31T00:00:00Z'")
    finally:
        db.close()


def assert_interrupted(
    code: int, outcome: dict[str, Any], tmp_path: Path, reason: str = "launch_interrupted"
) -> None:
    validate_exec(outcome)
    assert (code, outcome["exit_code"]) == (12, 12)
    assert (outcome["phase"], outcome["outcome"], outcome["end_reason"], outcome["state"]) == (
        "launch",
        "not-started",
        reason,
        "not-started",
    )
    assert not (tmp_path / "fake.jsonl").exists()  # claude was never spawned


@pytest.mark.parametrize("sig", signals.HANDLED)
def test_a_signal_during_the_launch_steps_ends_the_run_launch_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
    sig: int,
) -> None:
    real = launch.validate

    def validate(*args: Any, **kwargs: Any) -> Any:
        own(sig)
        return real(*args, **kwargs)

    monkeypatch.setattr(launch, "validate", validate)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    assert_interrupted(code, outcome, tmp_path)
    assert outcome["message"] == f"launch interrupted by {signal.Signals(sig).name}"
    row = run_row_of(state)
    assert row["child_pid"] is None and row["pending_cause"] is None


@pytest.mark.parametrize(
    ("sig", "reason"),
    [
        (signal.SIGTERM, "canceled"),
        (signal.SIGHUP, "canceled"),
        (signal.SIGINT, "launch_interrupted"),
    ],
)
def test_after_a_stop_request_sigterm_and_sighup_cancel_the_launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
    sig: int,
    reason: str,
) -> None:
    real = launch.validate

    def validate(*args: Any, **kwargs: Any) -> Any:
        request_stop(state)
        own(sig)
        return real(*args, **kwargs)

    monkeypatch.setattr(launch, "validate", validate)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    assert_interrupted(code, outcome, tmp_path, reason)


@pytest.mark.parametrize("sig", signals.HANDLED)
def test_a_signal_during_allocation_acts_once_the_run_exists(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
    sig: int,
) -> None:
    real = launch.allocate

    def allocate(*args: Any, **kwargs: Any) -> Any:
        own(sig)  # deferred: the allocation completes
        return real(*args, **kwargs)

    monkeypatch.setattr(launch, "allocate", allocate)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    assert_interrupted(code, outcome, tmp_path)
    assert outcome["message"] == f"launch interrupted by {signal.Signals(sig).name}"


def test_a_second_signal_during_the_launch_cleanup_is_only_recorded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    real_validate, real_end = launch.validate, launch.end_launch
    ends: list[str] = []

    def validate(*args: Any, **kwargs: Any) -> Any:
        own(signal.SIGINT)
        return real_validate(*args, **kwargs)

    def end_launch(*args: Any, **kwargs: Any) -> Any:
        own(signal.SIGTERM)  # a second signal while the run is being ended
        ends.append("end")
        return real_end(*args, **kwargs)

    monkeypatch.setattr(launch, "validate", validate)
    monkeypatch.setattr(launch, "end_launch", end_launch)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    assert ends == ["end"]
    assert_interrupted(code, outcome, tmp_path)
    assert outcome["message"] == "launch interrupted by SIGINT"
    assert run_row_of(state)["ended_at"] is not None


def test_a_signal_between_the_launch_steps_and_the_spawn_interrupts_the_launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    real = signals.ExecSignals.raise_pending

    def raise_pending(self: signals.ExecSignals) -> None:
        if sys._getframe(1).f_code.co_name == "spawn":
            own(signal.SIGTERM)  # deferred: recorded, raised by the check before the spawn
        real(self)

    monkeypatch.setattr(signals.ExecSignals, "raise_pending", raise_pending)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    assert_interrupted(code, outcome, tmp_path)
    sid = outcome["csession_id"]
    # The output files were closed and the run's secret index deleted.
    assert not (state / "private" / sid / "runs" / "1" / "secret-index").exists()


# Before allocation exec dies by the signal: a subprocess, patched to signal itself.
ENTRY = """\
import os, signal, sys
from claude_session import launch, pathrules
pathrules.TMP_ROOT = {tmp_root!r}
sys.argv[0] = {exe!r}
SIG = {sig}
{patch}
from claude_session.__main__ import main
sys.exit(main())
"""
# In launch step 2.
IN_STEP_2 = """\
real = launch.resolve_kind
def resolve_kind(*args, **kwargs):
    os.kill(os.getpid(), SIG)
    return real(*args, **kwargs)
launch.resolve_kind = resolve_kind
"""
# Right after exec's first handler is set, the others not yet (R1-11).
IN_INSTALL = """\
real = signal.signal
def setting(sig, handler):
    previous = real(sig, handler)
    if type(getattr(handler, "__self__", None)).__name__ == "ExecSignals" and sig == SIG:
        os.kill(os.getpid(), SIG)
    return previous
signal.signal = setting
"""


def exec_patched(
    tmp_path: Path, work: Path, sig: int, patch: str
) -> subprocess.CompletedProcess[str]:
    from claude_session import pathrules

    scenario = write_json(tmp_path / "scenario.json", {"steps": STEPS})
    fake = claude_bin(tmp_path / "fakebin", scenario, tmp_path / "fake.jsonl")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {fake}\n{FAST}", encoding="utf-8")
    (tmp_path / "p.md").write_text("x")
    entry = ENTRY.format(
        tmp_root=pathrules.TMP_ROOT, exe=str(CLAUDE_SESSION), sig=int(sig), patch=patch
    )
    return subprocess.run(
        [sys.executable, "-c", entry, "exec", "--mode", "headless", "--json"]
        + ["--config", str(cfg), "--prompt-file", str(tmp_path / "p.md")],
        cwd=work,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def assert_died_unallocated(
    proc: subprocess.CompletedProcess[str], sig: int, tmp_path: Path, state: Path
) -> None:
    assert proc.returncode == -sig, proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.stdout == ""  # nothing printed
    if (state / "state.db").exists():
        db = sqlite3.connect(f"file:{state / 'state.db'}?mode=ro", uri=True)
        try:
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
        finally:
            db.close()
    assert not (tmp_path / "fake.jsonl").exists()


@pytest.mark.parametrize("sig", signals.HANDLED)
def test_a_signal_before_allocation_ends_exec_by_that_signal_and_allocates_nothing(
    tmp_path: Path, state: Path, work: Path, sig: int
) -> None:
    proc = exec_patched(tmp_path, work, sig, IN_STEP_2)
    assert (state / "state.db").exists()
    assert_died_unallocated(proc, sig, tmp_path, state)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGHUP])
def test_a_signal_while_the_handlers_are_installed_ends_exec_by_it_without_a_traceback(
    tmp_path: Path, state: Path, work: Path, sig: int
) -> None:
    """R1-11: recorded while install() runs, then the before-allocation rule."""
    proc = exec_patched(tmp_path, work, sig, IN_INSTALL)
    assert_died_unallocated(proc, sig, tmp_path, state)


# A launch failure being handled, interrupted by a signal (R1-07)


@pytest.mark.parametrize(
    "failure",
    [
        LaunchValidationError("the cwd is not usable", end_reason="unsafe_cwd"),
        RuntimeError("an internal error before the spawn"),
    ],
    ids=["validation", "internal"],
)
def test_a_signal_while_a_launch_failure_is_handled_still_ends_the_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
    failure: Exception,
) -> None:
    """The signal lands in the except clause before its defer() (on entry to defer, as
    Python 3.13 runs pending handlers): the run still ends not-started and exec exits 12."""

    def validate(*_args: Any, **_kwargs: Any) -> Any:
        raise failure

    real_defer = signals.ExecSignals.defer
    fired: list[bool] = []

    def defer(self: signals.ExecSignals) -> None:
        if not fired and sys.exc_info()[0] is not None and self.phase is signals.Phase.RAISE:
            fired.append(True)
            own(signal.SIGTERM)  # raises LaunchInterrupted here, in the except clause
        real_defer(self)

    monkeypatch.setattr(launch, "validate", validate)
    monkeypatch.setattr(signals.ExecSignals, "defer", defer)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    assert fired
    assert_interrupted(code, outcome, tmp_path)
    assert outcome["message"] == "launch interrupted by SIGTERM"
    assert run_row_of(state)["ended_at"] is not None


def test_a_launch_interrupted_that_reaches_exec_unhandled_still_ends_the_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    """The backstop in exec: a LaunchInterrupted after allocation with the run not final."""

    def run(*_args: Any, **_kwargs: Any) -> Any:
        raise signals.LaunchInterrupted(signal.SIGHUP)

    monkeypatch.setattr(launch, "run", run)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    assert_interrupted(code, outcome, tmp_path)
    assert outcome["message"] == "launch interrupted by SIGHUP"
    assert run_row_of(state)["ended_at"] is not None


# After the run (R1-13, R1-16)


def test_a_signal_while_the_outcome_is_printed_is_only_recorded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    """exec's handlers stay installed until the outcome is printed: a late SIGTERM there is
    recorded and dropped (the check guards this test process: it signals itself only while
    exec's handler is in place)."""
    from claude_session import cli

    before = signal.getsignal(signal.SIGTERM)
    real_print = cli._print_outcome
    installed: list[bool] = []

    def printing(*args: Any, **kwargs: Any) -> None:
        installed.append(signal.getsignal(signal.SIGTERM) != before)
        if installed[-1]:
            own(signal.SIGTERM)
        real_print(*args, **kwargs)

    monkeypatch.setattr(cli, "_print_outcome", printing)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    assert installed == [True]
    assert (code, outcome["outcome"]) == (0, "done")


def test_signals_during_the_session_end_cleanup_keep_the_runs_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    from claude_session import supervisor

    real_finish = supervisor.finish

    def finish(*args: Any, **kwargs: Any) -> Any:
        for sig in signals.HANDLED:
            own(sig)  # the child is gone: recorded and dropped
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(supervisor, "finish", finish)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    validate_exec(outcome)
    assert (code, outcome["outcome"], outcome["end_reason"]) == (0, "done", "normal")
    assert run_row_of(state)["pending_cause"] is None
