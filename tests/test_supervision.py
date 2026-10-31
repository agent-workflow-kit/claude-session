"""``supervisor.Supervision`` against a real store and a scripted child (no process is
started): the order of ``pending_cause`` and the forward, the one forward, the SIGKILL after
``kill_grace`` to the same child, and the auto-exit wait and re-check (design.md, Supervision;
interface.md, Signals)."""

from __future__ import annotations

import errno
import signal
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_session import ids, launch, logs, supervisor
from claude_session.gateway import GATE_EXEC_FAILED
from claude_session.signals import ExecSignals
from claude_session.store import Holder, NewRun, Store

from .fake_gateway import FakeChild, FakeGateway
from .test_store_runs import add_event, new_run


@dataclass
class Cfg:
    poll_interval: float = 0.5
    kill_grace: float = 10.0
    auto_exit_delay: float = 2.0


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@dataclass
class ScriptedChild:
    """A ``Child`` whose ``send_signal`` records the run's ``pending_cause`` at that moment."""

    st: Store
    sid: str
    pid: int = 4321
    sent: list[tuple[int, Any]] = field(default_factory=list)
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        if self.returncode is not None:
            return
        cause = self.st.run_record(self.sid, 1)["pending_cause"]
        self.sent.append((sig, cause))

    def release(self) -> None:
        pass


@pytest.fixture
def st(tmp_path: Path) -> Any:
    with Store.open(str(tmp_path / "state.db"), holder=Holder(pid=1, start="1")) as s:
        yield s


@dataclass
class Rig:
    st: Store
    sid: str
    child: ScriptedChild
    signals: ExecSignals
    clock: Clock
    sup: supervisor.Supervision
    log: Path


def rig(
    st: Store, tmp_path: Path, *, mode: str = "interactive", auto_exit: bool = True, **cfg: Any
) -> Rig:
    run: NewRun = new_run(mode=mode)
    sid = st.allocate(run, new_id=ids.new_session_id)
    alloc = launch.Allocated(
        csession_id=sid, run=1, session_dir="/s", private_dir="/p", warnings=()
    )
    child = ScriptedChild(st, sid)
    sigs = ExecSignals()
    sigs.enter_session(interactive=mode == "interactive")
    clock = Clock()
    log = tmp_path / "log.jsonl"
    sup = supervisor.Supervision(
        st,
        Cfg(**cfg),  # type: ignore[arg-type]
        alloc,
        child,
        sigs,
        logs.EventLog(str(log)),
        auto_exit=auto_exit,
        monotonic=clock,
    )
    return Rig(st, sid, child, sigs, clock, sup, log)


def receive(r: Rig, sig: int) -> None:
    r.signals._handle(sig, None)  # what the installed handler does for a received signal
    for signum in r.signals.take():
        r.sup.forward(signum)


def test_pending_cause_is_recorded_before_the_forward(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path)
    receive(r, signal.SIGTERM)
    assert r.child.sent == [(signal.SIGTERM, "forwarded_term")]


def test_a_stop_request_makes_the_cause_stopped(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path)
    st.conn.execute("UPDATE runs SET stop_requested_at = 'x' WHERE csession_id = ?", (r.sid,))
    receive(r, signal.SIGHUP)
    assert r.child.sent == [(signal.SIGHUP, "stopped")]


def test_an_earlier_cause_stands_and_the_signal_is_still_forwarded(
    st: Store, tmp_path: Path
) -> None:
    r = rig(st, tmp_path)
    assert st.set_pending_cause(r.sid, 1, "stopped")
    receive(r, signal.SIGTERM)
    assert r.child.sent == [(signal.SIGTERM, "stopped")]


def test_interactive_sigint_is_never_forwarded(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path)
    receive(r, signal.SIGINT)
    assert r.child.sent == []
    assert st.run_record(r.sid, 1)["pending_cause"] is None


def test_headless_sigint_is_forwarded(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path, mode="headless")
    receive(r, signal.SIGINT)
    assert r.child.sent == [(signal.SIGINT, "forwarded_int")]


def test_only_the_first_signal_is_forwarded_then_sigkill_after_kill_grace(
    st: Store, tmp_path: Path
) -> None:
    r = rig(st, tmp_path, kill_grace=10.0)
    receive(r, signal.SIGTERM)
    receive(r, signal.SIGHUP)
    receive(r, signal.SIGTERM)
    assert r.child.sent == [(signal.SIGTERM, "forwarded_term")]
    assert r.sup.kill_at == 110.0
    r.sup.kill()  # what the loop does once kill_at is reached
    assert r.child.sent[-1][0] == signal.SIGKILL
    records = logs_of(r)
    assert [(x["event"], x.get("pid"), x["signal"]) for x in records] == [
        ("signal_sent", 4321, "SIGTERM"),
        ("signal_not_sent", None, "SIGHUP"),
        ("signal_not_sent", None, "SIGTERM"),
        ("signal_sent", 4321, "SIGKILL"),
    ]
    assert all("pid" in x for x in records if x["event"] == "signal_sent")


def test_no_sigkill_to_a_child_that_already_exited(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path)
    receive(r, signal.SIGTERM)
    r.child.returncode = 143
    r.sup.kill()
    assert [s for s, _ in r.child.sent] == [signal.SIGTERM]


def logs_of(r: Rig) -> list[dict[str, Any]]:
    """The ``signal_sent`` and ``signal_not_sent`` records, each valid per the log schema."""
    from .fakeclaude import read_jsonl
    from .schemas import validate
    from .test_logs import LOG_SCHEMA

    records = [x for x in read_jsonl(r.log) if x["event"] in ("signal_sent", "signal_not_sent")]
    for record in records:
        validate(record, LOG_SCHEMA)
    return records


# Signals queued when the child exits (R1-09)


def exits_with(r: Rig, code: int, *queued: int) -> int:
    """The loop's wait for a child that exits with ``code`` while ``queued`` signals wait in
    the deque (received before the reap, not yet taken)."""

    def wait(timeout: float | None = None) -> int | None:
        for sig in queued:
            r.signals._handle(sig, None)
        r.child.returncode = code
        return code

    r.child.wait = wait  # type: ignore[method-assign]
    return r.sup.wait()


def test_a_signal_queued_when_the_child_exits_records_its_cause_and_sends_nothing(
    st: Store, tmp_path: Path
) -> None:
    """A terminal hangup reaches claude and exec together: claude may be reaped before the
    loop takes exec's SIGHUP. Its cause is still recorded, and nothing is sent."""
    r = rig(st, tmp_path)
    assert exits_with(r, -signal.SIGHUP, signal.SIGHUP, signal.SIGTERM) == -signal.SIGHUP
    assert st.run_record(r.sid, 1)["pending_cause"] == "forwarded_hup"
    assert r.child.sent == []
    assert [(x["event"], x.get("pid"), x["signal"]) for x in logs_of(r)] == [
        ("signal_not_sent", None, "SIGHUP"),
        ("signal_not_sent", None, "SIGTERM"),
    ]


def test_a_signal_queued_at_exit_after_a_stop_request_is_stopped(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path)
    st.conn.execute("UPDATE runs SET stop_requested_at = 'x' WHERE csession_id = ?", (r.sid,))
    exits_with(r, -signal.SIGTERM, signal.SIGTERM)
    assert st.run_record(r.sid, 1)["pending_cause"] == "stopped"


def test_an_interactive_sigint_queued_at_exit_records_nothing(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path)
    exits_with(r, 0, signal.SIGINT)
    assert st.run_record(r.sid, 1)["pending_cause"] is None


def test_a_signal_queued_at_exit_after_the_forward_keeps_the_first_cause(
    st: Store, tmp_path: Path
) -> None:
    r = rig(st, tmp_path)
    receive(r, signal.SIGTERM)
    exits_with(r, -signal.SIGTERM, signal.SIGHUP)
    assert st.run_record(r.sid, 1)["pending_cause"] == "forwarded_term"
    assert [s for s, _ in r.child.sent] == [signal.SIGTERM]


# Auto-exit


def done(st: Store, sid: str) -> None:
    st.conn.execute("UPDATE runs SET report_status = 'done' WHERE csession_id = ?", (sid,))


def test_auto_exit_waits_the_delay_then_records_the_cause_before_sigterm(
    st: Store, tmp_path: Path
) -> None:
    r = rig(st, tmp_path, auto_exit_delay=2.0)
    done(st, r.sid)
    add_event(st, r.sid, "Stop", "reported", "idle")
    r.sup.check_auto_exit(100.0)
    r.sup.check_auto_exit(101.9)
    assert r.child.sent == []
    r.sup.check_auto_exit(102.0)
    assert r.child.sent == [(signal.SIGTERM, "auto_exit")]
    assert r.sup.ended


def test_a_newer_event_restarts_the_wait_at_the_next_qualifying_stop(
    st: Store, tmp_path: Path
) -> None:
    r = rig(st, tmp_path, auto_exit_delay=2.0)
    done(st, r.sid)
    add_event(st, r.sid, "Stop", "reported", "idle")
    r.sup.check_auto_exit(100.0)
    add_event(st, r.sid, "UserPromptSubmit", "reported", "working")
    r.sup.check_auto_exit(103.0)
    assert r.child.sent == [] and r.sup.candidate is None
    add_event(st, r.sid, "Stop", "reported", "idle")
    r.sup.check_auto_exit(104.0)
    r.sup.check_auto_exit(105.9)
    assert r.child.sent == []
    r.sup.check_auto_exit(106.0)
    assert r.child.sent == [(signal.SIGTERM, "auto_exit")]


def test_the_recheck_fails_when_an_event_lands_after_the_delay(st: Store, tmp_path: Path) -> None:
    """The compare-and-set re-checks the latest event: an event committed between the poll's
    read and the compare-and-set wins, and nothing is sent."""
    r = rig(st, tmp_path, auto_exit_delay=0.0)
    done(st, r.sid)
    add_event(st, r.sid, "Stop", "reported", "idle")
    real = st.latest_events

    def racing(sid: str, run_no: int, count: int) -> Any:
        events = real(sid, run_no, count)
        add_event(st, sid, "UserPromptSubmit", "reported", "working")
        return events

    st.latest_events = racing  # type: ignore[method-assign]
    r.sup.check_auto_exit(100.0)
    assert r.child.sent == []
    assert st.run_record(r.sid, 1)["pending_cause"] is None


@pytest.mark.parametrize(
    ("event", "report"),
    [
        (("Stop", "reported", "idle"), None),  # no report
        (("Stop", "reported", "working"), "done"),  # background tasks: still working
        (("Stop", "needs-input", "idle"), "done"),  # a prompt waiting
        (("Notification", "reported", "idle"), "done"),  # idle_prompt, not a Stop
        (("report", "reported", "idle"), "done"),  # a report after a non-Stop event
    ],
)
def test_no_auto_exit_without_a_done_report_and_a_qualifying_stop(
    st: Store, tmp_path: Path, event: tuple[str, str, str], report: str | None
) -> None:
    r = rig(st, tmp_path, auto_exit_delay=0.0)
    if report is not None:
        done(st, r.sid)
    add_event(st, r.sid, "PostToolUse", "working", "working")
    add_event(st, r.sid, *event)
    for now in (100.0, 200.0, 300.0):
        r.sup.check_auto_exit(now)
    assert r.child.sent == []


def test_a_report_drained_by_the_stops_own_hook_auto_exits(st: Store, tmp_path: Path) -> None:
    """R1-02: the Stop hook drains the report: Stop(idle, idle), then report(reported, idle).
    The report event is the candidate, re-checked in the compare-and-set."""
    r = rig(st, tmp_path, auto_exit_delay=2.0)
    done(st, r.sid)
    add_event(st, r.sid, "Stop", "idle", "idle")
    report_event = add_event(st, r.sid, "report", "reported", "idle")
    r.sup.check_auto_exit(100.0)
    assert r.sup.candidate == report_event and r.child.sent == []
    r.sup.check_auto_exit(102.0)
    assert r.child.sent == [(signal.SIGTERM, "auto_exit")]


@pytest.mark.parametrize(
    "stop",
    [
        ("Stop", "idle", "working"),  # background tasks
        ("PostToolUse", "idle", "idle"),  # not a Stop
    ],
)
def test_a_report_after_anything_but_an_idle_stop_does_not_auto_exit(
    st: Store, tmp_path: Path, stop: tuple[str, str, str]
) -> None:
    r = rig(st, tmp_path, auto_exit_delay=0.0)
    done(st, r.sid)
    add_event(st, r.sid, *stop)
    add_event(st, r.sid, "report", "reported", "idle")
    for now in (100.0, 200.0):
        r.sup.check_auto_exit(now)
    assert r.child.sent == []


def test_auto_exit_sends_sigterm_with_no_further_store_write(st: Store, tmp_path: Path) -> None:
    """R1-03: after the compare-and-set, SIGTERM goes out even while another connection holds
    the write lock (no second write in between)."""
    import sqlite3

    r = rig(st, tmp_path, auto_exit_delay=0.0)
    done(st, r.sid)
    add_event(st, r.sid, "Stop", "reported", "idle")
    other = sqlite3.connect(str(tmp_path / "state.db"), isolation_level=None, timeout=0)
    real = st.set_auto_exit_cause

    def then_lock(sid: str, run_no: int, event_id: int) -> bool:
        won = real(sid, run_no, event_id)
        other.execute("BEGIN IMMEDIATE")  # a hook's write lands right after the re-check
        return won

    st.set_auto_exit_cause = then_lock  # type: ignore[method-assign]
    try:
        r.sup.check_auto_exit(100.0)
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert r.child.sent == [(signal.SIGTERM, "auto_exit")]


def test_a_busy_store_at_the_auto_exit_recheck_retries_at_the_next_poll(
    st: Store, tmp_path: Path
) -> None:
    """R1-05: a busy timeout in the compare-and-set is logged, the candidate kept."""
    import sqlite3

    r = rig(st, tmp_path, auto_exit_delay=0.0)
    done(st, r.sid)
    stop = add_event(st, r.sid, "Stop", "reported", "idle")
    real = st.set_auto_exit_cause
    calls: list[int] = []

    def busy_once(sid: str, run_no: int, event_id: int) -> bool:
        calls.append(event_id)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(sid, run_no, event_id)

    st.set_auto_exit_cause = busy_once  # type: ignore[method-assign]
    r.sup.check_auto_exit(100.0)
    assert r.child.sent == [] and r.sup.candidate == stop
    assert [(x["event"], x["level"]) for x in logs_of(r)] == [("signal_not_sent", "warn")]
    r.sup.check_auto_exit(100.5)
    assert calls == [stop, stop]
    assert r.child.sent == [(signal.SIGTERM, "auto_exit")]


def test_an_internal_error_keeps_a_recorded_cause(st: Store, tmp_path: Path) -> None:
    """R1-04: internal_error is recorded only when the run has no cause."""
    from types import SimpleNamespace

    r = rig(st, tmp_path)
    assert st.set_pending_cause(r.sid, 1, "auto_exit")

    def no_run_dir(_run: int) -> int:
        raise OSError("no run directory here")

    v = SimpleNamespace(dirs=SimpleNamespace(open_run_dir=no_run_dir), tmpdir=None)
    supervisor._internal_error(st, Cfg(), r.sup.alloc, v, None)  # type: ignore[arg-type]
    row = st.run_record(r.sid, 1)
    assert row["pending_cause"] == "auto_exit"
    assert (row["outcome"], row["end_reason"]) == ("killed", "internal_error")


def test_auto_exit_off_never_checks(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path, auto_exit=False, auto_exit_delay=0.0, poll_interval=0.0)
    done(st, r.sid)
    add_event(st, r.sid, "Stop", "reported", "idle")
    r.child.returncode = None
    calls = iter([None, None, 0])

    def wait(timeout: float | None = None) -> int | None:
        return next(calls)

    r.child.wait = wait  # type: ignore[method-assign]
    assert r.sup.wait() == 0
    assert r.child.sent == []


def test_the_loop_forwards_signals_and_kills_after_the_grace(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path, kill_grace=1.0, poll_interval=0.5)
    ticks = iter(range(10))

    def wait(timeout: float | None = None) -> int | None:
        step = next(ticks)
        if step == 0:
            r.signals._handle(signal.SIGTERM, None)
        r.clock.now += 0.6
        if any(s == signal.SIGKILL for s, _ in r.child.sent):
            return -9
        return None

    r.child.wait = wait  # type: ignore[method-assign]
    assert r.sup.wait() == -9
    assert [s for s, _ in r.child.sent] == [signal.SIGTERM, signal.SIGKILL]


# The run's bound and the session_start_timeout warning (#19)


def bounded(
    st: Store,
    tmp_path: Path,
    *,
    bound: float | None = None,
    session_start_timeout: float | None = None,
    mode: str = "headless",
    **cfg: Any,
) -> Rig:
    """A rig whose supervision has a bound and/or a session-start check, spawned at 100.0."""
    r = rig(st, tmp_path, mode=mode, auto_exit=False, **cfg)
    r.sup = supervisor.Supervision(
        st,
        Cfg(**cfg),  # type: ignore[arg-type]
        r.sup.alloc,
        r.child,
        r.signals,
        logs.EventLog(str(r.log)),
        auto_exit=False,
        monotonic=r.clock,
        bound=bound,
        session_start_timeout=session_start_timeout,
        spawned_at=100.0,
    )
    return r


def all_logs(r: Rig) -> list[dict[str, Any]]:
    from .fakeclaude import read_jsonl
    from .schemas import validate
    from .test_logs import LOG_SCHEMA

    records = [x for x in read_jsonl(r.log) if x["event"] != "migration"] if r.log.exists() else []
    for record in records:
        validate(record, LOG_SCHEMA)
    return records


def test_the_bound_records_timeout_before_sigterm_once_it_is_reached(
    st: Store, tmp_path: Path
) -> None:
    r = bounded(st, tmp_path, bound=5.0, kill_grace=3.0)
    r.sup.check_bound(104.999)
    assert r.child.sent == [] and not r.sup.ended
    r.sup.check_bound(105.0)
    assert r.child.sent == [(signal.SIGTERM, "timeout")]
    assert r.sup.ended
    assert r.sup.kill_at == 100.0 + 3.0  # armed from the clock at the send
    assert [(x["event"], x["signal"], x["message"]) for x in logs_of(r)] == [
        ("signal_sent", "SIGTERM", "timeout")
    ]


def test_the_bound_counts_from_the_spawn_not_from_supervision(st: Store, tmp_path: Path) -> None:
    r = bounded(st, tmp_path, bound=5.0)
    r.clock.now = 104.0  # supervision began late; the bound still ends at spawn + 5
    r.sup.check_bound(105.0)
    assert r.child.sent == [(signal.SIGTERM, "timeout")]


def test_no_bound_never_ends_the_run(st: Store, tmp_path: Path) -> None:
    r = bounded(st, tmp_path, bound=None)
    r.sup.check_bound(1e12)
    assert r.child.sent == [] and not r.sup.ended


def test_an_earlier_cause_stands_when_the_bound_is_reached(st: Store, tmp_path: Path) -> None:
    r = bounded(st, tmp_path, bound=1.0)
    assert st.set_pending_cause(r.sid, 1, "stopped")
    r.sup.check_bound(101.0)
    assert r.child.sent == [(signal.SIGTERM, "stopped")]
    # The log names the cause that stands, not the one the bound asked for.
    assert [(x["event"], x["signal"], x["message"]) for x in logs_of(r)] == [
        ("signal_sent", "SIGTERM", "stopped")
    ]


def test_a_report_keeps_its_outcome_when_the_bound_ends_the_run(st: Store, tmp_path: Path) -> None:
    """A done report recorded before the bound: the bound still ends the run (cause timeout),
    but the outcome is the report's, exit 0 (interface.md, Outcome mapping)."""
    from claude_session.endmap import HeadlessResult, map_headless

    r = bounded(st, tmp_path, bound=5.0)
    done(st, r.sid)
    r.sup.check_bound(104.0)
    assert r.child.sent == []
    r.sup.check_bound(105.0)
    assert r.child.sent == [(signal.SIGTERM, "timeout")]
    record = st.run_record(r.sid, 1)
    mapped = map_headless(
        pending_cause=record["pending_cause"],
        report_status=record["report_status"],
        result=HeadlessResult(),
        returncode=-signal.SIGTERM,
        session_started=True,
        implicit_done=False,
    )
    final = supervisor._final(mapped, HeadlessResult())
    assert (final.outcome, final.end_reason, final.exit_code, final.state, final.cause) == (
        "done",
        "timeout",
        0,
        "exited",
        "timeout",
    )


def busy_store(st: Store, monkeypatch: pytest.MonkeyPatch, name: str, message: str) -> list[int]:
    """Make ``st.<name>`` always raise ``OperationalError(message)``; returns its calls."""
    import sqlite3

    calls: list[int] = []

    def fail(*_args: Any) -> Any:
        calls.append(1)
        error = sqlite3.OperationalError(message)
        error.sqlite_errorcode = (  # type: ignore[attr-defined]
            sqlite3.SQLITE_BUSY if "locked" in message else sqlite3.SQLITE_FULL
        )
        raise error

    monkeypatch.setattr(st, name, fail)
    return calls


def test_a_store_busy_past_the_bound_and_kill_grace_takes_the_internal_error_path(
    st: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-3: the timeout cause is retried while the store is busy only until bound +
    kill_grace; then supervision fails and the internal-error path ends the child (SIGTERM,
    SIGKILL after kill_grace), so the child never outlives the bound without limit."""
    from types import SimpleNamespace

    from claude_session.errors import InternalError

    r = bounded(st, tmp_path, bound=1.0, kill_grace=3.0)
    calls = busy_store(st, monkeypatch, "set_pending_cause", "database is locked")
    r.sup.check_bound(101.0)
    r.sup.check_bound(103.999)
    assert r.child.sent == [] and len(calls) == 2  # retried within the deadline
    with pytest.raises(InternalError, match="busy"):
        r.sup.check_bound(104.0)
    assert r.child.sent == []
    assert [x["event"] for x in logs_of(r)] == ["signal_not_sent", "signal_not_sent"]

    def no_run_dir(_run: int) -> int:
        raise OSError("no run directory here")

    v = SimpleNamespace(dirs=SimpleNamespace(open_run_dir=no_run_dir), tmpdir=None)
    supervisor._internal_error(st, Cfg(kill_grace=3.0), r.sup.alloc, v, r.child)  # type: ignore[arg-type]
    assert [s for s, _ in r.child.sent] == [signal.SIGTERM, signal.SIGKILL]
    row = st.run_record(r.sid, 1)
    assert (row["outcome"], row["end_reason"]) == ("killed", "internal_error")


def test_the_loop_fails_once_the_store_stays_busy_past_the_deadline(
    st: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_session.errors import InternalError

    r = bounded(st, tmp_path, bound=2.0, kill_grace=1.0, poll_interval=0.5)
    busy_store(st, monkeypatch, "set_pending_cause", "database is locked")

    def wait(timeout: float | None = None) -> int | None:
        r.clock.now += 0.25
        return None

    r.child.wait = wait  # type: ignore[method-assign]
    with pytest.raises(InternalError):
        r.sup.wait()
    assert 103.0 <= r.clock.now <= 103.5
    assert r.child.sent == []


def test_a_store_error_other_than_busy_at_the_bound_fails_at_once(
    st: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_session.errors import InternalError

    r = bounded(st, tmp_path, bound=1.0, kill_grace=3.0)
    calls = busy_store(st, monkeypatch, "set_pending_cause", "database or disk is full")
    with pytest.raises(InternalError):
        r.sup.check_bound(101.0)
    assert len(calls) == 1 and r.child.sent == []
    assert logs_of(r) == []  # not retried: no signal_not_sent


def test_a_busy_store_at_the_bound_retries_at_the_next_poll(
    st: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    r = bounded(st, tmp_path, bound=1.0)
    real = st.set_pending_cause
    calls: list[str] = []

    def busy_once(sid: str, run_no: int, cause: str) -> bool:
        calls.append(cause)
        if len(calls) == 1:
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
            raise error
        return real(sid, run_no, cause)

    monkeypatch.setattr(st, "set_pending_cause", busy_once)
    r.sup.check_bound(101.0)
    assert r.child.sent == [] and not r.sup.ended  # nothing sent without the cause
    r.sup.check_bound(101.5)
    assert r.child.sent == [(signal.SIGTERM, "timeout")]
    records = logs_of(r)
    assert [(x["event"], x["level"]) for x in records] == [
        ("signal_not_sent", "warn"),
        ("signal_sent", "info"),
    ]
    assert "timeout" in records[0]["message"]


def test_the_loop_ends_the_child_at_the_bound_and_kills_it_after_kill_grace(
    st: Store, tmp_path: Path
) -> None:
    r = bounded(st, tmp_path, bound=2.0, kill_grace=1.0, poll_interval=0.5)

    def wait(timeout: float | None = None) -> int | None:
        r.clock.now += 0.25
        if any(s == signal.SIGKILL for s, _ in r.child.sent):
            return -9
        return None

    r.child.wait = wait  # type: ignore[method-assign]
    assert r.sup.wait() == -9
    assert r.child.sent == [(signal.SIGTERM, "timeout"), (signal.SIGKILL, "timeout")]
    assert 102.0 <= r.clock.now <= 104.0


def test_a_forwarded_signal_is_not_followed_by_a_bound_signal(st: Store, tmp_path: Path) -> None:
    r = bounded(st, tmp_path, bound=1.0)
    receive(r, signal.SIGTERM)
    r.sup.check_bound(200.0)  # the loop only checks the bound before the child was signaled
    assert [s for s, _ in r.child.sent] == [signal.SIGTERM]
    assert st.run_record(r.sid, 1)["pending_cause"] == "forwarded_term"


def started(st: Store, sid: str) -> None:
    st.conn.execute(
        "UPDATE runs SET session_started_at = '2026-01-01T00:00:00Z' WHERE csession_id = ?",
        (sid,),
    )


def test_no_session_start_within_the_timeout_warns_once(st: Store, tmp_path: Path) -> None:
    r = bounded(st, tmp_path, session_start_timeout=120.0)
    r.sup.check_session_start(219.9)
    assert st.run_record(r.sid, 1)["warnings_json"] in (None, "[]")
    r.sup.check_session_start(220.0)
    r.sup.check_session_start(221.0)
    import json

    warnings = json.loads(st.run_record(r.sid, 1)["warnings_json"])
    assert len(warnings) == 1
    assert "SessionStart" in warnings[0] and "120s" in warnings[0]
    missing = [x for x in all_logs(r) if x["event"] == "session_start_missing"]
    assert len(missing) == 1
    assert (missing[0]["level"], missing[0]["csession_id"], missing[0]["run"]) == (
        "warn",
        r.sid,
        1,
    )
    assert r.child.sent == []  # a warning only: the run goes on


def test_a_session_start_in_time_gives_no_warning(st: Store, tmp_path: Path) -> None:
    r = bounded(st, tmp_path, session_start_timeout=1.0)
    started(st, r.sid)
    r.sup.check_session_start(500.0)
    assert st.run_record(r.sid, 1)["warnings_json"] in (None, "[]")
    assert all_logs(r) == []


def test_no_session_start_check_without_a_timeout(st: Store, tmp_path: Path) -> None:
    """Interactive runs are exempt (they may wait at the trust dialog): run_session passes
    no session_start_timeout for them."""
    r = bounded(st, tmp_path, session_start_timeout=None, mode="interactive")
    r.sup.check_session_start(1e12)
    assert all_logs(r) == []


def test_a_busy_store_at_the_session_start_warning_retries_and_logs_once(
    st: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    r = bounded(st, tmp_path, session_start_timeout=1.0)
    real = st.add_run_warnings
    calls: list[int] = []

    def busy_once(sid: str, run_no: int, warnings: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(sid, run_no, warnings)

    monkeypatch.setattr(st, "add_run_warnings", busy_once)
    r.sup.check_session_start(101.0)
    r.sup.check_session_start(102.0)
    r.sup.check_session_start(103.0)
    assert len(calls) == 2
    assert st.run_record(r.sid, 1)["warnings_json"].count("SessionStart") == 1
    assert len([x for x in all_logs(r) if x["event"] == "session_start_missing"]) == 1


def test_a_session_start_after_the_logged_warning_still_adds_it(
    st: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-10: once ``session_start_missing`` is logged, a retry only retries the add: a
    SessionStart that lands meanwhile does not drop the warning the log announced."""
    import sqlite3

    r = bounded(st, tmp_path, session_start_timeout=1.0)
    real = st.add_run_warnings
    calls: list[int] = []

    def busy_once(sid: str, run_no: int, warnings: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        real(sid, run_no, warnings)

    monkeypatch.setattr(st, "add_run_warnings", busy_once)
    r.sup.check_session_start(101.0)
    started(st, r.sid)  # SessionStart lands between the busy add and its retry
    r.sup.check_session_start(102.0)
    assert len(calls) == 2
    assert st.run_record(r.sid, 1)["warnings_json"].count("SessionStart") == 1
    assert len([x for x in all_logs(r) if x["event"] == "session_start_missing"]) == 1


def test_the_loop_checks_the_session_start_and_keeps_supervising(st: Store, tmp_path: Path) -> None:
    r = bounded(st, tmp_path, session_start_timeout=1.0, poll_interval=0.5)
    ticks = iter(range(10))

    def wait(timeout: float | None = None) -> int | None:
        r.clock.now += 0.5
        return 0 if next(ticks) == 5 else None

    r.child.wait = wait  # type: ignore[method-assign]
    assert r.sup.wait() == 0
    assert r.child.sent == []
    assert "SessionStart" in st.run_record(r.sid, 1)["warnings_json"]


@pytest.mark.parametrize(
    ("end_reason", "cause"),
    [("timeout", "timeout"), ("auto_exit", "auto_exit"), ("max_turns", "exit"), ("normal", "exit")],
)
def test_the_final_events_cause_follows_the_end_reason(end_reason: str, cause: str) -> None:
    from claude_session.endmap import HeadlessResult, Mapped

    outcome = "timeout" if end_reason in ("timeout", "max_turns") else "done"
    final = supervisor._final(Mapped(outcome, end_reason), HeadlessResult())
    assert final.cause == cause


def test_a_failed_release_reverts_to_the_launch_phase_and_ends_the_run_not_started(
    st: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3: claude could not be executed at the gate's release (a bad interpreter): the child
    (which exited at once) is reaped, the recorded child is cleared back to a launch-phase run
    without a child, and the run ends not-started, launch_interrupted (exit 12)."""
    sid = st.allocate(new_run(mode="interactive"), new_id=ids.new_session_id)
    alloc = launch.Allocated(
        csession_id=sid, run=1, session_dir="/s", private_dir="/p", warnings=()
    )
    fake = FakeGateway()
    error = FileNotFoundError(errno.ENOENT, "No such file or directory", "/opt/claude")
    fake.on(["/opt/claude"], timeout=True, release_error=error)
    prepared = SimpleNamespace(
        argv=["/opt/claude"], executable=None, stdout_fd=None, stderr_fd=None, run_secret="s"
    )
    validated = SimpleNamespace(session_env=SimpleNamespace(env={"PATH": "/usr/bin"}), cwd="/")
    recorded: list[dict[str, Any]] = []
    ended: list[tuple[Any, ...]] = []
    real_update = st.update_run

    def recording_update(csession_id: str, run_no: int, **fields: Any) -> None:
        real_update(csession_id, run_no, **fields)
        recorded.append(dict(fields))

    def end_launch(
        st_: Store, alloc_: Any, v: Any, prep: Any, process: Any, err: Any, *, exc: Any = None
    ) -> None:
        ended.append((v, prep, err, exc, st_.run_record(sid, 1)))

    monkeypatch.setattr(st, "update_run", recording_update)
    monkeypatch.setattr(launch, "end_launch", end_launch)
    result = supervisor.run_session(
        st,
        None,  # type: ignore[arg-type]  # unused before the release
        alloc,
        validated,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        SimpleNamespace(gateway=fake),  # type: ignore[arg-type]
        mode="interactive",
        implicit_done=True,
        auto_exit=True,
        signals=ExecSignals(),
    )
    assert result.exit_code == 12
    (child,) = fake.children
    assert isinstance(child, FakeChild) and fake.calls[0].gated
    assert child.released and child.returncode == GATE_EXEC_FAILED
    assert [r.get("phase") for r in recorded] == ["session", "launch"]
    assert recorded[0]["child_pid"] == child.pid
    assert recorded[1] == {
        "phase": "launch",
        "child_pid": None,
        "child_start": None,
        "started_at": None,
    }
    ((v, prep, err, exc, row),) = ended
    assert (v, prep, exc) == (validated, None, error)
    assert err.end_reason == "launch_interrupted"
    assert str(err) == "claude could not be started: No such file or directory"
    assert (row["phase"], row["child_pid"], row["started_at"]) == ("launch", None, None)
