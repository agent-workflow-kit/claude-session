"""Golden outputs for every documented run state, needs-input reason, outcome and end reason
(issue #127; interface.md, States and Outcome mapping): ``status --json`` of a session in each
state, and the ``wait --json`` outcome object of a run that ended with each end reason.

The runs are built in a state database (the way ``tests/test_status_list.py`` and
``tests/test_wait.py`` do), offline: no ``claude`` runs. A session-phase run's outcome and end
reason come from the real mapping (``endmap``) applied to the situation interface.md names for
it; the launch-phase and ``result_rejected`` ends, which are decided by ``exec`` around the
mapping, are written down from the interface. ``tests/test_interface_audit.py`` fails when a
documented state, outcome or end reason has no golden, so a new one needs a case here.

Goldens: ``tests/golden/status-<state>[-<reason>].json`` and
``tests/golden/wait-reason-<end_reason>.json`` / ``wait-outcome-<outcome>.json``; the sessions'
ids and times are fixed (fake clock), so only the state directory path is normalized.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from claude_session import endmap, store
from claude_session.store import Final, Store

from . import test_status_list as sl
from .golden import GOLDEN_ROOT, assert_golden
from .schemas import SCHEMA_ROOT
from .test_status_list import Clock, allocate, finalize, report_done, run_json, to_working
from .test_wait import Clock as WaitClock
from .test_wait import report as file_report

# The seeding fixtures of tests/test_status_list.py (a fake clock, the liveness table and a
# state database that reads that clock), registered under their names here.
clock = sl.clock
alive = sl.alive
st = sl.st

SID = "cs-aaaaaaaa"
LIVE_PIDS = {(4242, "boot:1"), (5151, "boot:2")}  # the seeded wrapper and child


# status --json in every state


def working(st: Store, clock: Clock, alive: set[tuple[int, str]]) -> None:
    allocate(st, SID, clock)
    to_working(st, SID, clock)
    alive.update(LIVE_PIDS)


def set_state(
    st: Store, clock: Clock, state: str, activity: str | None, reason: str | None, cause: str
) -> None:
    clock.tick()
    with st.transaction():
        run = st.hook_run(SID)
        assert run is not None
        st.apply_state(run, state=state, activity=activity, needs_input_reason=reason, cause=cause)


def final(outcome: str, end_reason: str, **fields: Any) -> Final:
    return Final(
        phase="session",
        outcome=outcome,
        end_reason=end_reason,
        exit_code=endmap.EXIT_CODES[outcome],
        state=endmap.final_state(outcome),
        cause="exit",
        **fields,
    )


Advance = Callable[[Store, Clock, set[tuple[int, str]]], None]


def live_idle(st: Store, clock: Clock, alive: set[tuple[int, str]]) -> None:
    set_state(st, clock, "idle", "idle", None, "Stop")


def live_needs_input(reason: str, cause: str) -> Advance:
    return lambda st, clock, alive: set_state(st, clock, "needs-input", "working", reason, cause)


def live_escalation(st: Store, clock: Clock, alive: set[tuple[int, str]]) -> None:
    """A `needs-input` report was ingested (interface.md, State transitions): the run has the
    report and the state is `needs-input(escalation)`."""
    clock.tick()
    with st.transaction():
        st.set_report(
            SID, 1, status="needs-input", summary=None, reason="Which branch?", artifact=None
        )
    set_state(st, clock, "needs-input", "working", "escalation", "report")


def live_reported(st: Store, clock: Clock, alive: set[tuple[int, str]]) -> None:
    report_done(st, SID, clock)


def ended(outcome: str, end_reason: str, *, stop_requested: bool = False) -> Advance:
    """The run ended: its wrapper and child are gone (as in the `status-done` golden), and a
    stopped one has the request `stop` recorded."""

    def advance(st: Store, clock: Clock, alive: set[tuple[int, str]]) -> None:
        if stop_requested:
            clock.tick()
            assert st.request_stop(SID, 1)
        alive.clear()
        finalize(st, SID, clock, final(outcome, end_reason))

    return advance


# (golden case, how the working session gets there). `starting`, `working`, `exited` (done)
# and `not-started` are the goldens of tests/test_status_list.py (STATUS_LIST_GOLDENS).
STATUS_CASES: list[tuple[str, Advance]] = [
    ("idle", live_idle),
    ("needs-input-permission", live_needs_input("permission", "PermissionRequest")),
    ("needs-input-question", live_needs_input("question", "PermissionRequest")),
    ("needs-input-elicitation", live_needs_input("elicitation", "Notification")),
    ("needs-input-escalation", live_escalation),
    ("reported", live_reported),
    ("exited-unreported", ended("exited-unreported", "no_report")),
    ("killed", ended("killed", "stopped", stop_requested=True)),
    ("timeout", ended("timeout", "timeout")),
    ("lost", ended("lost", "lost")),
]


@pytest.mark.parametrize(("case", "advance"), STATUS_CASES, ids=[c for c, _ in STATUS_CASES])
def test_status_json_of_a_session_in_each_state(
    case: str,
    advance: Advance,
    st: Store,
    clock: Clock,
    alive: set[tuple[int, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    working(st, clock, alive)
    advance(st, clock, alive)
    code, obj = run_json(["status", SID], capsys)
    assert code == 0
    assert obj["state"] == case.removesuffix(f"-{obj['needs_input_reason']}")
    run = obj["run"]
    if case == "needs-input-escalation":
        assert run["report"]["status"] == "needs-input"
    if obj["live"] is False:
        assert (run["wrapper_alive"], run["child_alive"]) == (False, False)
    if case == "killed":
        assert run["stop_requested_at"] is not None
    assert_golden(obj, f"status-{case}", normalize=sl._normalize_status)


# wait --json for every end reason

NO_RESULT = endmap.HeadlessResult()


def result(**final: Any) -> endmap.HeadlessResult:
    return endmap.HeadlessResult(complete=True, final={"type": "result", **final})


def headless(**kw: Any) -> endmap.Mapped:
    base: dict[str, Any] = {
        "pending_cause": None,
        "report_status": None,
        "result": result(),
        "returncode": 0,
        "session_started": True,
        "implicit_done": False,
    }
    return endmap.map_headless(**{**base, **kw})


def interactive(**kw: Any) -> endmap.Mapped:
    base: dict[str, Any] = {
        "pending_cause": None,
        "report_status": None,
        "returncode": 0,
        "session_started": True,
        "session_end_reason": None,
    }
    return endmap.map_interactive(**{**base, **kw})


def lost(**kw: Any) -> endmap.Mapped:
    base: dict[str, Any] = {
        "pending_cause": None,
        "report_status": None,
        "headless_result": None,
        "session_started": True,
        "implicit_done": False,
    }
    return endmap.map_wrapper_lost(**{**base, **kw})


# (end reason, the mapped end that produces it, the report filed in the run (if any)).
SESSION_ENDS: list[tuple[str, endmap.Mapped, str | None]] = [
    ("normal", interactive(), None),
    ("auto_exit", headless(pending_cause="auto_exit", report_status="done"), "done"),
    ("user_exit", interactive(session_end_reason="prompt_input_exit"), None),
    ("forwarded_term", headless(pending_cause="forwarded_term"), None),
    ("forwarded_hup", headless(pending_cause="forwarded_hup"), None),
    ("forwarded_int", headless(pending_cause="forwarded_int"), None),
    ("signal_kill", interactive(returncode=-9), None),
    ("stopped", headless(pending_cause="stopped"), None),
    ("internal_error", headless(pending_cause="internal_error"), None),
    ("timeout", headless(pending_cause="timeout"), None),
    ("max_turns", headless(result=result(subtype="error_max_turns")), None),
    ("api_error", headless(result=result(is_error=True)), None),
    ("no_result", headless(result=NO_RESULT), None),
    ("no_report", headless(), None),
    # `result_rejected` is decided by exec when it accepts an implicit done (a rejected
    # `result.md`, or report requests still in the outbox): the mapping itself says done.
    # Where it is produced: supervisor.py (the implicit-done check after the end) and
    # liveness.py (the same check for a lost wrapper); tests/test_exec_headless.py
    # (test of `result_rejected`, around line 394) runs it through `exec`.
    ("result_rejected", endmap.Mapped("exited-unreported", "result_rejected"), None),
    ("no_session_start", interactive(session_started=False), None),
    ("wrapper_lost", lost(report_status="blocked"), "blocked"),
    ("lost", lost(), None),
    ("exit_3", interactive(returncode=3), None),
]

# What a result object's totals add to the run (the final's `cost_usd` and `num_turns`).
RESULT_TOTALS = {
    "max_turns": {"cost_usd": 0.25, "num_turns": 30, "is_error": False},
    "api_error": {"cost_usd": 0.0625, "num_turns": 2, "is_error": True},
}

# The launch phase (interface.md, Outcome mapping: Launch phase).
LAUNCH_OUTCOMES = {
    "hook_veto": "vetoed",
    "hook_timeout": "vetoed",
    "hook_escalate": "escalated",
}
LAUNCH_ENDS = [
    (reason, endmap.Mapped(LAUNCH_OUTCOMES.get(reason, "not-started"), reason))
    for reason in json.loads((SCHEMA_ROOT / "cli" / "outcome.json").read_text())["$defs"][
        "launch_end_reason"
    ]["enum"]
]


@pytest.fixture
def wait_st(tmp_path: Path) -> Any:
    with Store.open(
        str(tmp_path / "state.db"),
        holder=store.Holder(pid=1, start="1"),
        liveness=lambda _p, _s: True,
        clock=sl.Clock(),  # fixed times: the goldens hold session_started_at
    ) as s:
        yield s


def wait_golden(
    wait_st: Store, name: str, ended: Final, report: str | None, *, started: bool = False
) -> None:
    """``started``: Claude Code's SessionStart was seen, so the run has ``session_started_at``
    and the session its Claude id (every session-phase end but ``no_session_start``)."""
    from .test_wait import session, wait

    sid = session(wait_st)
    if started:
        with wait_st.transaction():
            run = wait_st.hook_run(sid)
            assert run is not None
            wait_st.record_session_start(
                run,
                claude_session_id="c0ffee00-0000-4000-8000-000000000001",
                source="startup",
                transcript_path=None,
            )
    if report is not None:
        file_report(wait_st, sid, report)
    assert wait_st.finalize_run(sid, 1, ended)
    out = wait(wait_st, WaitClock(), sid).output
    assert (out["outcome"], out["end_reason"]) == (ended.outcome, ended.end_reason)
    assert out["exit_code"] == out["run_exit_code"] == endmap.EXIT_CODES[ended.outcome]
    assert (out["session_started_at"] is not None) is started
    assert (out["claude_session_id"] is not None) is started
    assert_golden(out, name, normalize=lambda o: {**o, "csession_id": SID})


@pytest.mark.parametrize(
    ("reason", "mapped", "report"), SESSION_ENDS, ids=[reason for reason, _, _ in SESSION_ENDS]
)
def test_wait_json_of_a_run_that_ended_in_the_session(
    reason: str, mapped: endmap.Mapped, report: str | None, wait_st: Store
) -> None:
    assert mapped.end_reason == reason
    ended = final(mapped.outcome, reason, **RESULT_TOTALS.get(reason, {}))
    wait_golden(
        wait_st, f"wait-reason-{reason}", ended, report, started=reason != "no_session_start"
    )


@pytest.mark.parametrize(("reason", "mapped"), LAUNCH_ENDS, ids=[r for r, _ in LAUNCH_ENDS])
def test_wait_json_of_a_run_that_ended_in_the_launch_phase(
    reason: str, mapped: endmap.Mapped, wait_st: Store
) -> None:
    ended = Final(
        phase="launch",
        outcome=mapped.outcome,
        end_reason=reason,
        exit_code=endmap.EXIT_CODES[mapped.outcome],
        state="not-started",
        cause="exit",
        message=f"the launch ended: {reason}",
    )
    wait_golden(wait_st, f"wait-reason-{reason}", ended, None)


@pytest.mark.parametrize("status", ["needs-input", "failed"])
def test_wait_json_of_a_run_that_reported_each_other_outcome(status: str, wait_st: Store) -> None:
    mapped = headless(report_status=status)
    assert (mapped.outcome, mapped.end_reason) == (status, "normal")
    wait_golden(wait_st, f"wait-outcome-{status}", final(status, "normal"), status, started=True)


def test_every_end_reason_of_the_schema_has_a_case_here() -> None:
    defs = json.loads((SCHEMA_ROOT / "cli" / "outcome.json").read_text())["$defs"]
    enum = {value for b in defs["end_reason"]["anyOf"] if "enum" in b for value in b["enum"]}
    cases = {reason for reason, _, _ in SESSION_ENDS} | {reason for reason, _ in LAUNCH_ENDS}
    assert enum <= cases
    assert {"exit_3", "signal_kill"} <= cases  # one value of each family


# Every golden of these commands is named by a case: deleting a case must delete its file.

# The goldens of tests/test_status_list.py, tests/test_wait.py and tests/test_report.py.
STATUS_LIST_GOLDENS = {"working", "done", "not-started", "pending"}
OTHER_GOLDENS = {"wait-done", "wait-timed-out", "report-artifact"}
OUTCOME_FAMILIES = ("status-", "wait-", "report-")


def named_goldens() -> set[str]:
    names = {f"status-{case}" for case, _ in STATUS_CASES}
    names |= {f"status-{case}" for case in STATUS_LIST_GOLDENS}
    names |= {f"wait-reason-{reason}" for reason, _, _ in SESSION_ENDS}
    names |= {f"wait-reason-{reason}" for reason, _ in LAUNCH_ENDS}
    names |= {f"wait-outcome-{status}" for status in ("needs-input", "failed")}
    names |= {f"report-{status}" for status in ("blocked", "failed", "needs-input")}
    return names | OTHER_GOLDENS


def unnamed_goldens(root: Path = GOLDEN_ROOT) -> set[str]:
    """The status, wait and report goldens under ``root`` that no case names."""
    files = {p.stem for p in root.glob("*.json") if p.name.startswith(OUTCOME_FAMILIES)}
    return files - named_goldens()


def test_every_status_wait_and_report_golden_is_named_by_a_case() -> None:
    assert unnamed_goldens() == set()


def test_a_golden_no_case_names_is_found(tmp_path: Path) -> None:
    for name in (*named_goldens(), "status-paused", "wait-reason-disk_full"):
        (tmp_path / f"{name}.json").write_text("{}", encoding="utf-8")
    (tmp_path / "list-sessions.json").write_text("{}", encoding="utf-8")
    assert unnamed_goldens(tmp_path) == {"status-paused", "wait-reason-disk_full"}
