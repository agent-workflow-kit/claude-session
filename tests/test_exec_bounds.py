"""The run's bound and the ``session_start_timeout`` warning end to end (interface.md, Launch
flags ``--timeout``, exec; design.md, Supervision): ``exec`` runs the fake ``claude``
(``tests/fakebin``), which hangs or sleeps, and ends it at the bound.

Real-time waits stay short: bounds of about a second, a fast poll."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from .fakeclaude import read_jsonl
from .test_exec_headless import START, gone, run_exec
from .test_exec_interactive import check_outcome, events, start_terminal, touch, wait_marker

FAST = "poll_interval: 0.05s\nkill_grace: 2s\n"
HANG = {"do": "hang"}


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def final_cause(run: Any) -> str:
    rows = run.rows("SELECT cause, final FROM events ORDER BY id DESC LIMIT 1")
    assert rows[0][1] == 1
    return str(rows[0][0])


def test_a_headless_run_past_its_timeout_ends_timeout_16(
    tmp_path: Path, state: Path, work: Path
) -> None:
    began = time.monotonic()
    run = run_exec(tmp_path, state, work, [START, HANG], args=["--timeout", "1s"], config=FAST)
    took = time.monotonic() - began
    assert run.code == 16, run.proc.stderr
    assert (run.outcome["outcome"], run.outcome["end_reason"], run.outcome["state"]) == (
        "timeout",
        "timeout",
        "timeout",
    )
    assert run.outcome["phase"] == "session"
    row = run.run_row()
    assert row["pending_cause"] == "timeout"
    assert final_cause(run) == "timeout"
    assert [r["signal"] for r in run.fake_log() if r["event"] == "signal"] == ["TERM"]
    sent = [r for r in read_jsonl(state / "log.jsonl") if r["event"] == "signal_sent"]
    assert [(r["pid"], r["signal"], r["message"]) for r in sent] == [
        (row["child_pid"], "SIGTERM", "timeout")
    ]
    assert gone(row["child_pid"])
    assert took >= 1.0


def test_the_kinds_timeout_applies_without_the_flag(
    tmp_path: Path, state: Path, work: Path
) -> None:
    config = FAST + "kinds:\n  implement:\n    timeout: 1s\n"
    run = run_exec(tmp_path, state, work, [START, HANG], config=config)
    assert run.code == 16, run.proc.stderr
    assert run.outcome["end_reason"] == "timeout"


def test_timeout_none_disables_the_kinds_timeout(tmp_path: Path, state: Path, work: Path) -> None:
    config = FAST + "kinds:\n  implement:\n    timeout: 0.3s\n"
    steps = [START, {"do": "sleep", "seconds": 1.2}, {"do": "stop"}, {"do": "result"}]
    run = run_exec(tmp_path, state, work, steps, args=["--timeout", "none"], config=config)
    assert run.code == 0, run.proc.stderr
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == ("done", "normal")
    assert run.run_row()["pending_cause"] is None
    assert final_cause(run) == "exit"


def test_a_child_ignoring_sigterm_at_the_bound_is_killed_after_kill_grace(
    tmp_path: Path, state: Path, work: Path
) -> None:
    steps = [START, {"do": "ignore_signals", "signals": ["TERM"]}, HANG]
    config = "poll_interval: 0.05s\nkill_grace: 0.5s\n"
    run = run_exec(tmp_path, state, work, steps, args=["--timeout", "0.5s"], config=config)
    assert run.code == 16, run.proc.stderr
    row = run.run_row()
    assert (row["pending_cause"], row["end_reason"]) == ("timeout", "timeout")
    sent = [r for r in read_jsonl(state / "log.jsonl") if r["event"] == "signal_sent"]
    assert [(r["signal"], r["message"]) for r in sent] == [
        ("SIGTERM", "timeout"),
        ("SIGKILL", "kill_grace"),
    ]
    assert gone(row["child_pid"])


def test_a_report_keeps_its_outcome_at_the_bound(tmp_path: Path, state: Path, work: Path) -> None:
    """With a report the outcome is the report's; the end reason and the final event's cause
    are still timeout (interface.md, Outcome mapping)."""
    steps = [START, {"do": "report", "args": ["done", "--summary", "finished"]}, HANG]
    run = run_exec(tmp_path, state, work, steps, args=["--timeout", "1s"], config=FAST)
    assert run.code == 0, run.proc.stderr
    assert (run.outcome["outcome"], run.outcome["end_reason"], run.outcome["state"]) == (
        "done",
        "timeout",
        "exited",
    )
    assert final_cause(run) == "timeout"


def test_no_session_start_within_the_timeout_warns_and_the_run_goes_on(
    tmp_path: Path, state: Path, work: Path
) -> None:
    config = FAST + "session_start_timeout: 0.3s\n"
    steps = [{"do": "sleep", "seconds": 0.8}, START, {"do": "stop"}, {"do": "result"}]
    run = run_exec(tmp_path, state, work, steps, config=config)
    assert run.code == 0, run.proc.stderr  # a warning only: the run still ends normally
    warnings = [w for w in run.outcome["warnings"] if "SessionStart" in w]
    assert len(warnings) == 1 and "0.3s" in warnings[0]
    missing = [r for r in read_jsonl(state / "log.jsonl") if r["event"] == "session_start_missing"]
    assert len(missing) == 1
    assert (missing[0]["level"], missing[0]["csession_id"]) == ("warn", run.sid)


def test_a_session_start_in_time_gives_no_warning(tmp_path: Path, state: Path, work: Path) -> None:
    config = FAST + "session_start_timeout: 0.5s\n"
    steps = [START, {"do": "sleep", "seconds": 0.8}, {"do": "stop"}, {"do": "result"}]
    run = run_exec(tmp_path, state, work, steps, config=config)
    assert run.code == 0, run.proc.stderr
    assert not [w for w in run.outcome["warnings"] if "SessionStart" in w]
    assert not [r for r in read_jsonl(state / "log.jsonl") if r["event"] == "session_start_missing"]


# Interactive: --timeout is the interactive_timeout


def test_an_interactive_run_past_its_timeout_ends_timeout(
    tmp_path: Path, state: Path, work: Path
) -> None:
    marker = tmp_path / "running"
    term = start_terminal(
        tmp_path,
        state,
        work,
        [START, touch(marker), HANG],
        args=["--timeout", "1s"],
        config=FAST + "session_start_timeout: 0.1s\n",
    )
    try:
        wait_marker(term, marker)
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["outcome"], outcome["end_reason"], outcome["state"]) == (
        16,
        "timeout",
        "timeout",
        "timeout",
    )
    assert events(state)[-1] == ("timeout", "timeout", None, None, 1)
    # Interactive runs are exempt from the session_start_timeout warning.
    assert not [r for r in term.exec_log() if r["event"] == "session_start_missing"]


def test_the_kinds_interactive_timeout_bounds_an_interactive_run(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """The kind's headless ``timeout`` does not apply in interactive mode; its
    ``interactive_timeout`` does."""
    config = FAST + "kinds:\n  implement:\n    timeout: 0.2s\n    interactive_timeout: 1s\n"
    marker = tmp_path / "running"
    began = time.monotonic()
    term = start_terminal(tmp_path, state, work, [START, touch(marker), HANG], config=config)
    try:
        wait_marker(term, marker)
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["end_reason"]) == (16, "timeout")
    assert time.monotonic() - began >= 1.0
