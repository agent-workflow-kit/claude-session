"""``supervisor.Supervision`` against a real store and a scripted child (no process is
started): the order of ``pending_cause`` and the forward, the one forward, the SIGKILL after
``kill_grace`` to the same child, and the auto-exit wait and re-check (design.md, Supervision;
interface.md, Signals)."""

from __future__ import annotations

import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from claude_session import ids, launch, logs, supervisor
from claude_session.signals import ExecSignals
from claude_session.store import Holder, NewRun, Store

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
    assert [(x.get("pid"), x["signal"]) for x in records] == [
        (4321, "SIGTERM"),
        (None, "SIGHUP"),
        (None, "SIGTERM"),
        (4321, "SIGKILL"),
    ]


def test_no_sigkill_to_a_child_that_already_exited(st: Store, tmp_path: Path) -> None:
    r = rig(st, tmp_path)
    receive(r, signal.SIGTERM)
    r.child.returncode = 143
    r.sup.kill()
    assert [s for s, _ in r.child.sent] == [signal.SIGTERM]


def logs_of(r: Rig) -> list[dict[str, Any]]:
    from .fakeclaude import read_jsonl

    return [x for x in read_jsonl(r.log) if x["event"] == "signal_sent"]


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
    real = st.latest_event

    def racing(sid: str, run_no: int) -> Any:
        event = real(sid, run_no)
        add_event(st, sid, "UserPromptSubmit", "reported", "working")
        return event

    st.latest_event = racing  # type: ignore[method-assign]
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
        (("report", "reported", "idle"), "done"),  # the report came after the Stop
    ],
)
def test_no_auto_exit_without_a_done_report_and_a_qualifying_stop(
    st: Store, tmp_path: Path, event: tuple[str, str, str], report: str | None
) -> None:
    r = rig(st, tmp_path, auto_exit_delay=0.0)
    if report is not None:
        done(st, r.sid)
    add_event(st, r.sid, *event)
    for now in (100.0, 200.0, 300.0):
        r.sup.check_auto_exit(now)
    assert r.child.sent == []


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
