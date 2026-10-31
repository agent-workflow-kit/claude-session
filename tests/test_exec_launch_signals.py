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


def test_a_signal_during_allocation_acts_once_the_run_exists(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    real = launch.allocate

    def allocate(*args: Any, **kwargs: Any) -> Any:
        own(signal.SIGHUP)  # deferred: the allocation completes
        return real(*args, **kwargs)

    monkeypatch.setattr(launch, "allocate", allocate)
    code, outcome = exec_in_process(monkeypatch, capsys, tmp_path, work, STEPS)
    assert_interrupted(code, outcome, tmp_path)
    assert outcome["message"] == "launch interrupted by SIGHUP"


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


# Before allocation exec dies by the signal: a subprocess, patched to signal itself in step 2.
ENTRY = """\
import os, signal, sys
from claude_session import launch, pathrules
pathrules.TMP_ROOT = {tmp_root!r}
sys.argv[0] = {exe!r}
real = launch.resolve_kind
def resolve_kind(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGTERM)
    return real(*args, **kwargs)
launch.resolve_kind = resolve_kind
from claude_session.__main__ import main
sys.exit(main())
"""


def test_a_signal_before_allocation_ends_exec_by_that_signal_and_allocates_nothing(
    tmp_path: Path, state: Path, work: Path
) -> None:
    from claude_session import pathrules

    scenario = write_json(tmp_path / "scenario.json", {"steps": STEPS})
    fake = claude_bin(tmp_path / "fakebin", scenario, tmp_path / "fake.jsonl")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {fake}\n{FAST}", encoding="utf-8")
    (tmp_path / "p.md").write_text("x")
    entry = ENTRY.format(tmp_root=pathrules.TMP_ROOT, exe=str(CLAUDE_SESSION))
    proc = subprocess.run(
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
    assert proc.returncode == -signal.SIGTERM, proc.stderr
    assert "Traceback" not in proc.stderr
    db = sqlite3.connect(f"file:{state / 'state.db'}?mode=ro", uri=True)
    try:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    finally:
        db.close()
    assert not (tmp_path / "fake.jsonl").exists()
