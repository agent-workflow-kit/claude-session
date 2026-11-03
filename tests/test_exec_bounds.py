"""The run's bound and the ``session_start_timeout`` warning end to end (interface.md, Launch
flags ``--timeout``, exec; design.md, Supervision): ``exec`` runs the fake ``claude``
(``tests/fakebin``), which hangs or sleeps, and ends it at the bound.

No assertion depends on a short real deadline being met: bounds are a few seconds (the fake
reaches its steps long before), the fake ignores SIGTERM from its first instruction where that
matters, and it waits for exec's own log line where order matters. The precise timing is
covered with the injectable clock in ``test_supervision``."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from .fakeclaude import claude_bin, read_jsonl, write_json
from .test_exec_headless import START, gone, run_exec, script
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
    run = run_exec(tmp_path, state, work, [START, HANG], args=["--timeout", "3s"], config=FAST)
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
    assert took >= 3.0


def test_the_kinds_timeout_applies_without_the_flag(
    tmp_path: Path, state: Path, work: Path
) -> None:
    config = FAST + "kinds:\n  implement:\n    timeout: 3s\n"
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
    """The fake ignores SIGTERM from its first instruction (a shell wrapper ignores it before
    it execs the fake, and the fake's scenario keeps it ignored), so the bound's SIGTERM can
    never arrive before the ignore is in place."""
    steps = [START, HANG]
    signals = {"signals": {"TERM": "ignore"}}
    scenario = write_json(tmp_path / "scenario.json", {"steps": steps, **signals})
    fake = claude_bin(tmp_path / "fakebin", scenario, tmp_path / "fake.jsonl")
    wrapper = script(
        tmp_path / "wrapbin" / "claude", f'#!/bin/sh\ntrap \'\' TERM\nexec "{fake}" "$@"\n'
    )
    config = "poll_interval: 0.05s\nkill_grace: 0.5s\n"
    run = run_exec(
        tmp_path,
        state,
        work,
        steps,
        args=["--timeout", "2s"],
        config=config,
        scenario_extra=signals,
        claude=wrapper,
    )
    assert run.code == 16, run.proc.stderr
    row = run.run_row()
    assert (row["pending_cause"], row["end_reason"]) == ("timeout", "timeout")
    sent = [r for r in read_jsonl(state / "log.jsonl") if r["event"] == "signal_sent"]
    assert [(r["signal"], r["message"]) for r in sent] == [
        ("SIGTERM", "timeout"),
        ("SIGKILL", "kill_grace"),
    ]
    assert gone(row["child_pid"])


def test_a_report_keeps_its_outcome_under_a_bound(tmp_path: Path, state: Path, work: Path) -> None:
    """A bounded run that reports and then ends on its own keeps the report's outcome and
    never reaches the bound (30s, far beyond the run). The bound ending a reported run is
    covered with the injectable clock in ``test_supervision``."""
    report = {"do": "report", "args": ["done", "--summary", "finished"]}
    steps = [START, report, {"do": "stop"}, {"do": "result"}]
    run = run_exec(tmp_path, state, work, steps, args=["--timeout", "30s"], config=FAST)
    assert run.code == 0, run.proc.stderr
    assert (run.outcome["outcome"], run.outcome["state"]) == ("done", "exited")
    assert run.outcome["report"]["summary"] == "finished"
    assert run.run_row()["pending_cause"] is None
    assert final_cause(run) == "exit"


def test_no_session_start_within_the_timeout_warns_and_the_run_goes_on(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """The fake sends its SessionStart only once exec logged the warning (order, not time)."""
    config = FAST + "session_start_timeout: 1s\n"
    log = state / "log.jsonl"
    await_warning = (
        "i=0; while ! /bin/grep -q session_start_missing "
        f'"{log}" 2>/dev/null; do i=$((i+1)); [ "$i" -gt 1200 ] && exit 1; /bin/sleep 0.05; done'
    )
    steps = [
        {"do": "bash", "command": await_warning, "post_tool_use": "never"},
        START,
        {"do": "stop"},
        {"do": "result"},
    ]
    run = run_exec(tmp_path, state, work, steps, config=config)
    assert run.code == 0, run.proc.stderr  # a warning only: the run still ends normally
    warnings = [w for w in run.outcome["warnings"] if "SessionStart" in w]
    assert len(warnings) == 1 and "1s" in warnings[0]
    missing = [r for r in read_jsonl(state / "log.jsonl") if r["event"] == "session_start_missing"]
    assert len(missing) == 1
    assert (missing[0]["level"], missing[0]["csession_id"]) == ("warn", run.sid)


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
        args=["--timeout", "3s"],
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
    config = FAST + "kinds:\n  implement:\n    timeout: 0.2s\n    interactive_timeout: 3s\n"
    marker = tmp_path / "running"
    began = time.monotonic()
    term = start_terminal(tmp_path, state, work, [START, touch(marker), HANG], config=config)
    try:
        wait_marker(term, marker)
    finally:
        code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["end_reason"]) == (16, "timeout")
    assert time.monotonic() - began >= 3.0
