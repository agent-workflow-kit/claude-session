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
from .golden import assert_golden
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


def live_idle(st: Store, clock: Clock) -> None:
    set_state(st, clock, "idle", "idle", None, "Stop")


def live_needs_input(reason: str, cause: str) -> Callable[[Store, Clock], None]:
    return lambda st, clock: set_state(st, clock, "needs-input", "working", reason, cause)


def live_reported(st: Store, clock: Clock) -> None:
    report_done(st, SID, clock)


def ended(outcome: str, end_reason: str) -> Callable[[Store, Clock], None]:
    return lambda st, clock: finalize(st, SID, clock, final(outcome, end_reason))


# (golden case, how the working session gets there). `starting`, `working`, `exited` (done)
# and `not-started` are the goldens of tests/test_status_list.py.
STATUS_CASES: list[tuple[str, Callable[[Store, Clock], None]]] = [
    ("idle", live_idle),
    ("needs-input-permission", live_needs_input("permission", "PermissionRequest")),
    ("needs-input-question", live_needs_input("question", "PermissionRequest")),
    ("needs-input-elicitation", live_needs_input("elicitation", "Notification")),
    ("needs-input-escalation", live_needs_input("escalation", "report")),
    ("reported", live_reported),
    ("exited-unreported", ended("exited-unreported", "no_report")),
    ("killed", ended("killed", "stopped")),
    ("timeout", ended("timeout", "timeout")),
    ("lost", ended("lost", "lost")),
]


@pytest.mark.parametrize(("case", "advance"), STATUS_CASES, ids=[c for c, _ in STATUS_CASES])
def test_status_json_of_a_session_in_each_state(
    case: str,
    advance: Callable[[Store, Clock], None],
    st: Store,
    clock: Clock,
    alive: set[tuple[int, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    working(st, clock, alive)
    advance(st, clock)
    code, obj = run_json(["status", SID], capsys)
    assert code == 0
    assert obj["state"] == case.removesuffix(f"-{obj['needs_input_reason']}")
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
    ("result_rejected", endmap.Mapped("exited-unreported", "result_rejected"), None),
    ("no_session_start", interactive(session_started=False), None),
    ("wrapper_lost", lost(report_status="blocked"), "blocked"),
    ("lost", lost(), None),
    ("exit_3", interactive(returncode=3), None),
]

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
    ) as s:
        yield s


def wait_golden(wait_st: Store, name: str, ended: Final, report: str | None) -> None:
    from .test_wait import session, wait

    sid = session(wait_st)
    if report is not None:
        file_report(wait_st, sid, report)
    assert wait_st.finalize_run(sid, 1, ended)
    out = wait(wait_st, WaitClock(), sid).output
    assert (out["outcome"], out["end_reason"]) == (ended.outcome, ended.end_reason)
    assert out["exit_code"] == out["run_exit_code"] == endmap.EXIT_CODES[ended.outcome]
    assert_golden(out, name, normalize=lambda o: {**o, "csession_id": SID})


@pytest.mark.parametrize(
    ("reason", "mapped", "report"), SESSION_ENDS, ids=[reason for reason, _, _ in SESSION_ENDS]
)
def test_wait_json_of_a_run_that_ended_in_the_session(
    reason: str, mapped: endmap.Mapped, report: str | None, wait_st: Store
) -> None:
    assert mapped.end_reason == reason
    wait_golden(wait_st, f"wait-reason-{reason}", final(mapped.outcome, reason), report)


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
    wait_golden(wait_st, f"wait-outcome-{status}", final(status, "normal"), status)


def test_every_end_reason_of_the_schema_has_a_case_here() -> None:
    defs = json.loads((SCHEMA_ROOT / "cli" / "outcome.json").read_text())["$defs"]
    (enum,) = [b["enum"] for b in defs["end_reason"]["anyOf"] if "enum" in b]
    cases = {reason for reason, _, _ in SESSION_ENDS} | {reason for reason, _ in LAUNCH_ENDS}
    assert set(enum) <= cases
    assert {"exit_3", "signal_kill"} <= cases  # one value of each family
