"""Interactive ``exec`` end to end: the ``needs-input`` states, sticky escalation and display
priority (interface.md, States and State transitions), and every row of the interactive end
mapping (interface.md, Outcome mapping: Interactive). ``exec`` runs on a pseudo-terminal and
launches the fake ``claude`` (``tests/fakebin``), which plays a scenario through the injected
hooks; the harness is ``test_exec_interactive``'s.

Process discipline as in ``test_exec_interactive``: every test synchronises on a marker file
before it signals, and signals only the exact ``exec`` process it started.
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

import pytest

from .test_exec_headless import START, run_row_of
from .test_exec_interactive import (
    HANG,
    STOP,
    check_outcome,
    events,
    report,
    start_terminal,
    touch,
    wait_marker,
)


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def user_exit() -> list[dict[str, Any]]:
    """``/exit``: SessionEnd ``prompt_input_exit``, then claude exits 0 (design.md, Spike
    results)."""
    return [{"do": "session_end", "reason": "prompt_input_exit"}, {"do": "exit", "code": 0}]


def prompt(text: str = "go") -> dict[str, Any]:
    return {"do": "user_prompt_submit", "prompt": text}


def permission(tool: str = "Bash") -> dict[str, Any]:
    return {"do": "permission_request", "tool_name": tool, "tool_input": {"command": "ls"}}


def question() -> dict[str, Any]:
    return {
        "do": "permission_request",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": "which one?"}]},
    }


def notification(kind: str) -> dict[str, Any]:
    return {"do": "notification", "notification_type": kind}


def tool_ran(tool: str = "Bash") -> dict[str, Any]:
    return {"do": "post_tool_use", "tool_name": tool, "tool_input": {"command": "ls"}}


def states(state: Path) -> list[tuple[Any, ...]]:
    """The run's ``(cause, state, activity, needs_input_reason)`` changes, in order."""
    return [row[:4] for row in events(state)]


def run(
    tmp_path: Path, state: Path, work: Path, steps: list[dict[str, Any]], **kwargs: Any
) -> tuple[int, dict[str, Any]]:
    term = start_terminal(tmp_path, state, work, steps, **kwargs)
    code, outcome, err = term.finish()
    return code, check_outcome(code, outcome, err)


# The acceptance flow


def test_working_permission_working_reported_then_auto_exit_with_exit_0(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """The fake session goes ``working`` -> ``needs-input(permission)`` -> ``working`` ->
    ``reported`` -> auto-exit and closes with exit 0, each change in that order."""
    code, outcome = run(
        tmp_path,
        state,
        work,
        [START, prompt(), permission(), tool_ran(), report("done"), STOP, HANG],
    )
    assert code == 0
    assert (outcome["outcome"], outcome["end_reason"], outcome["state"]) == (
        "done",
        "auto_exit",
        "exited",
    )
    assert states(state) == [
        ("launch", "starting", None, None),
        ("SessionStart", "idle", "idle", None),
        ("UserPromptSubmit", "working", "working", None),
        ("PermissionRequest", "needs-input", "working", "permission"),
        ("PostToolUse", "working", "working", None),
        ("report", "reported", "working", None),
        ("Stop", "reported", "idle", None),
        ("auto_exit", "exited", None, None),
    ]


# needs-input states, sticky escalation and display priority


def test_a_denied_permission_prompt_stays_until_user_prompt_submit(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """Denying fires no hook: the prompt survives Stop and idle_prompt (activity goes idle)
    and is cleared only by the next UserPromptSubmit."""
    code, outcome = run(
        tmp_path,
        state,
        work,
        [
            START,
            prompt(),
            permission(),
            notification("permission_prompt"),
            STOP,
            notification("idle_prompt"),
            prompt("try something else"),
            *user_exit(),
        ],
    )
    assert states(state)[3:] == [
        ("PermissionRequest", "needs-input", "working", "permission"),
        ("Stop", "needs-input", "idle", "permission"),
        ("UserPromptSubmit", "working", "working", None),
        ("exit", "exited-unreported", None, None),
    ]
    assert (code, outcome["outcome"], outcome["end_reason"]) == (
        14,
        "exited-unreported",
        "user_exit",
    )


def test_a_question_is_kept_by_the_permission_notification_and_survives_esc(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """AskUserQuestion is ``question``; the ``permission_prompt`` notification that follows it
    keeps it; Esc fires nothing, so it stays across Stop until the next prompt."""
    run(
        tmp_path,
        state,
        work,
        [
            START,
            prompt(),
            question(),
            notification("permission_prompt"),
            STOP,
            prompt("never mind"),
            *user_exit(),
        ],
    )
    assert states(state)[3:-1] == [
        ("PermissionRequest", "needs-input", "working", "question"),
        ("Stop", "needs-input", "idle", "question"),
        ("UserPromptSubmit", "working", "working", None),
    ]


def test_an_answered_question_clears_on_post_tool_use(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run(
        tmp_path,
        state,
        work,
        [START, prompt(), question(), tool_ran("AskUserQuestion"), STOP, *user_exit()],
    )
    assert states(state)[3:-1] == [
        ("PermissionRequest", "needs-input", "working", "question"),
        ("PostToolUse", "working", "working", None),
        ("Stop", "idle", "idle", None),
    ]


def test_an_elicitation_dialog_is_needs_input_elicitation(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run(
        tmp_path,
        state,
        work,
        [START, prompt(), notification("elicitation_dialog"), STOP, prompt(), *user_exit()],
    )
    assert states(state)[3:-1] == [
        ("Notification", "needs-input", "working", "elicitation"),
        ("Stop", "needs-input", "idle", "elicitation"),
        ("UserPromptSubmit", "working", "working", None),
    ]


def test_escalation_is_sticky_and_a_prompt_takes_display_priority(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """A ``needs-input`` report opens an escalation: a permission prompt shows over it, the
    approval (PostToolUse) reverts to it, Stop keeps it, and only UserPromptSubmit closes it
    (``reported``, the report standing). The run ends ``needs-input`` (13)."""
    code, outcome = run(
        tmp_path,
        state,
        work,
        [
            START,
            prompt(),
            report("needs-input"),
            permission(),
            tool_ran(),
            STOP,
            notification("idle_prompt"),
            prompt("here is the answer"),
            STOP,
            *user_exit(),
        ],
    )
    assert states(state)[3:] == [
        ("report", "needs-input", "working", "escalation"),
        ("PermissionRequest", "needs-input", "working", "permission"),
        ("PostToolUse", "needs-input", "working", "escalation"),
        ("Stop", "needs-input", "idle", "escalation"),
        ("UserPromptSubmit", "reported", "working", None),
        ("Stop", "reported", "idle", None),
        ("exit", "exited", None, None),
    ]
    assert (code, outcome["outcome"], outcome["end_reason"]) == (13, "needs-input", "user_exit")
    assert outcome["report"]["status"] == "needs-input"


def test_a_prompt_waiting_when_a_done_report_lands_keeps_needs_input(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """A report never hides a waiting prompt: the run stays ``needs-input(permission)`` and
    is not auto-exited; quitting gives the report's outcome."""
    marker = tmp_path / "idle"
    code, outcome = run(
        tmp_path,
        state,
        work,
        [
            START,
            prompt(),
            report("done"),
            permission(),
            STOP,
            touch(marker),
            {"do": "sleep", "seconds": 1.0},  # three auto-exit delays: nothing happens
            *user_exit(),
        ],
    )
    assert states(state)[-3:] == [
        ("PermissionRequest", "needs-input", "working", "permission"),
        ("Stop", "needs-input", "idle", "permission"),
        ("exit", "exited", None, None),
    ]
    assert (code, outcome["outcome"], outcome["end_reason"]) == (0, "done", "user_exit")
    assert run_row_of(state)["pending_cause"] is None


def test_hooks_before_session_start_change_nothing(tmp_path: Path, state: Path, work: Path) -> None:
    """The permission, question and elicitation rows apply only after SessionStart (the trust
    dialog fires no hooks; anything earlier is ignored)."""
    code, outcome = run(
        tmp_path,
        state,
        work,
        [permission(), question(), notification("elicitation_dialog"), tool_ran(), STOP],
    )
    assert states(state) == [
        ("launch", "starting", None, None),
        ("exit", "exited-unreported", None, None),
    ]
    assert (code, outcome["end_reason"]) == (14, "no_session_start")


# The interactive end mapping, every row


@pytest.mark.parametrize(
    ("steps", "code", "outcome_", "end_reason"),
    [
        # claude gone before SessionStart (the trust dialog: Esc exits 0, "No, exit" 1)
        ([{"do": "exit", "code": 0}], 14, "exited-unreported", "no_session_start"),
        ([{"do": "exit", "code": 1}], 14, "exited-unreported", "no_session_start"),
        # /exit, also with a non-zero exit
        ([START, *user_exit()], 14, "exited-unreported", "user_exit"),
        (
            [
                START,
                {"do": "session_end", "reason": "prompt_input_exit"},
                {"do": "exit", "code": 2},
            ],
            14,
            "exited-unreported",
            "user_exit",
        ),
        # with a report: the report's outcome, the end reason from the table
        ([START, report("blocked"), *user_exit()], 13, "blocked", "user_exit"),
        ([START, report("failed"), *user_exit()], 18, "failed", "user_exit"),
        # exit 0 otherwise; /clear's SessionEnd does not end the process
        ([START, {"do": "exit", "code": 0}], 14, "exited-unreported", "normal"),
        (
            [
                START,
                {"do": "session_end", "reason": "clear"},
                {"do": "session_start", "source": "clear"},
                {"do": "exit", "code": 0},
            ],
            14,
            "exited-unreported",
            "normal",
        ),
        ([START, {"do": "session_end", "reason": "other"}], 14, "exited-unreported", "normal"),
        ([START, report("done"), {"do": "exit", "code": 0}], 0, "done", "normal"),
        # non-zero exit
        ([START, {"do": "exit", "code": 3}], 14, "exited-unreported", "exit_3"),
        ([START, {"do": "exit", "code": 255}], 14, "exited-unreported", "exit_255"),
        # killed by a signal exec did not send (128+n): before user_exit
        (
            [
                START,
                {"do": "session_end", "reason": "prompt_input_exit"},
                {"do": "exit", "code": 137},
            ],
            15,
            "killed",
            "signal_kill",
        ),
        # ... and an actual signal death (the fake signals itself: its exact PID)
        ([START, {"do": "bash", "command": "kill -USR1 $PPID"}, HANG], 15, "killed", "signal_usr1"),
    ],
)
def test_interactive_end_mapping_rows(
    tmp_path: Path,
    state: Path,
    work: Path,
    steps: list[dict[str, Any]],
    code: int,
    outcome_: str,
    end_reason: str,
) -> None:
    got, outcome = run(tmp_path, state, work, steps)
    assert (got, outcome["outcome"], outcome["end_reason"]) == (code, outcome_, end_reason)
    assert outcome["state"] == ("exited" if outcome_ in ("done", "blocked", "failed") else outcome_)


def test_no_session_start_leaves_no_claude_session_id(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """interface.md, exec: quitting at the trust dialog leaves no Claude session id."""
    _, outcome = run(tmp_path, state, work, [{"do": "exit", "code": 1}])
    assert outcome["claude_session_id"] is None
    assert outcome["session_started_at"] is None


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_forwarded_signal_after_user_exit_keeps_its_cause(
    tmp_path: Path, state: Path, work: Path, sig: signal.Signals
) -> None:
    """A recorded ``forwarded_*`` cause wins over a SessionEnd ``prompt_input_exit`` (the
    cause rows come first). The fake exits on either signal without firing SessionEnd (its
    default SIGTERM handler would fire ``other``), so the recorded reason is still
    ``prompt_input_exit`` and only the row order decides."""
    marker = tmp_path / "ended"
    term = start_terminal(
        tmp_path,
        state,
        work,
        [START, {"do": "session_end", "reason": "prompt_input_exit"}, touch(marker), HANG],
        signals={"TERM": {"exit": 128 + signal.SIGTERM}},
    )
    try:
        wait_marker(term, marker)
        term.proc.send_signal(sig)  # the exact exec started here
    finally:
        got, outcome, err = term.finish()
    outcome = check_outcome(got, outcome, err)
    assert run_row_of(state)["session_end_reason"] == "prompt_input_exit"
    cause = "forwarded_term" if sig == signal.SIGTERM else "forwarded_hup"
    assert (got, outcome["outcome"], outcome["end_reason"]) == (15, "killed", cause)
