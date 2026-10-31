"""``wait`` (interface.md, wait; design.md, Launch, adoption and pending runs): binding to the
current run, ``--run``, ``--timeout`` (exit 22, ``timed_out``, ``pending``), a session
forgotten mid-wait (10), and the ``--json`` output against ``schemas/cli/wait.json``.

``waiter.wait_for_run`` runs against a real store with a fake clock and sleep (no real
waiting); the ``claude-session wait`` tests start the real entry point."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from claude_session import ids, store, waiter
from claude_session.errors import AmbiguousError, NotFoundError
from claude_session.store import Final, Holder, Store

from .fake_clock import FakeClock
from .schemas import validate
from .test_exec_headless import claude_session_cmd, private_home
from .test_store_runs import add_event, new_run

WAIT_SCHEMA = "claude-session/cli/wait.json"


class Clock(FakeClock):
    """A fake clock whose sleep runs ``on_sleep`` (with the number of sleeps so far) after
    advancing the time."""

    def __init__(self) -> None:
        super().__init__()
        self.on_sleep: Callable[[int], object] = lambda _n: None

    @property
    def slept(self) -> list[float]:
        return self.sleeps

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        super().sleep(seconds)
        self.on_sleep(len(self.sleeps))


@pytest.fixture
def st(tmp_path: Path) -> Any:
    with Store.open(str(tmp_path / "state.db"), holder=Holder(pid=1, start="1")) as s:
        yield s


@pytest.fixture
def clock() -> Clock:
    return Clock()


def wait(
    st: Store,
    clock: Clock,
    text: str,
    *,
    run_no: int | None = None,
    timeout: float | None = None,
    poll_interval: float = 1.0,
) -> waiter.Waited:
    waited = waiter.wait_for_run(
        st,
        text,
        run_no=run_no,
        timeout=timeout,
        poll_interval=poll_interval,
        clock=clock,
    )
    validate(waited.output, WAIT_SCHEMA)
    assert waited.output["exit_code"] == waited.exit_code
    return waited


KILLED = Final(
    phase="session",
    outcome="killed",
    end_reason="forwarded_term",
    exit_code=15,
    state="killed",
    cause="exit",
)
DONE = Final(
    phase="session",
    outcome="done",
    end_reason="normal",
    exit_code=0,
    state="exited",
    cause="exit",
)


def session(st: Store, **fields: Any) -> str:
    sid = st.allocate(new_run(**fields), new_id=ids.new_session_id)
    st.update_run(sid, 1, phase="session")
    return sid


def spawned(st: Store, sid: str, run_no: int = 1) -> None:
    st.update_run(sid, run_no, phase="session")


def report(st: Store, sid: str, status: str = "done", run_no: int = 1) -> None:
    st.conn.execute(
        "UPDATE runs SET report_status = ?, report_implicit = 0, report_summary = 'ok', "
        "report_reason = CASE WHEN ? = 'done' THEN NULL ELSE 'why' END, "
        "reported_at = '2026-01-01T00:00:00Z' WHERE csession_id = ? AND run_no = ?",
        (status, status, sid, run_no),
    )
    st.conn.execute("UPDATE sessions SET state = 'reported' WHERE csession_id = ?", (sid,))


def second_run(st: Store, sid: str, *, pending: bool = False) -> None:
    """Run 2 of ``sid`` (a resume), copied from run 1 and live; the session's current run."""
    columns = [row[1] for row in st.conn.execute("PRAGMA table_info(runs)")]
    copied = [c for c in columns if c not in ("run_no",)]
    names = ", ".join(copied)
    with st.transaction() as conn:
        conn.execute(
            f"INSERT INTO runs (run_no, {names}) SELECT 2, {names} FROM runs "
            "WHERE csession_id = ? AND run_no = 1",
            (sid,),
        )
        conn.execute(
            "UPDATE runs SET ended_at = NULL, outcome = NULL, end_reason = NULL, "
            "exit_code = NULL, report_status = NULL, pending_cause = NULL, phase = 'launch', "
            "wrapper_pid = CASE WHEN ? THEN NULL ELSE wrapper_pid END "
            "WHERE csession_id = ? AND run_no = 2",
            (pending, sid),
        )
        conn.execute(
            "UPDATE sessions SET current_run = 2, state = 'starting', activity = NULL "
            "WHERE csession_id = ?",
            (sid,),
        )


def finish_done(st: Store, sid: str) -> bool:
    report(st, sid, "done")
    return st.finalize_run(sid, 1, DONE)


def forget(st: Store, sid: str) -> None:
    st.conn.execute(
        "UPDATE sessions SET forgotten_at = '2026-01-01T00:00:00Z' WHERE csession_id = ?",
        (sid,),
    )


# A final run


def test_a_final_run_returns_at_once_with_its_outcome_code(st: Store, clock: Clock) -> None:
    sid = session(st)
    assert st.finalize_run(sid, 1, KILLED)
    waited = wait(st, clock, sid, timeout=5.0)
    assert clock.slept == []
    assert waited.exit_code == 15
    out = waited.output
    assert (out["timed_out"], out["run_exit_code"], out["pending"]) == (False, 15, False)
    assert (out["outcome"], out["end_reason"], out["state"]) == (
        "killed",
        "forwarded_term",
        "killed",
    )


def test_a_reported_run_exits_with_the_reports_code(st: Store, clock: Clock) -> None:
    sid = session(st)
    report(st, sid, "blocked")
    blocked = Final(**{**DONE.__dict__, "outcome": "blocked", "exit_code": 13})
    assert st.finalize_run(sid, 1, blocked)
    waited = wait(st, clock, sid[:7])  # a prefix resolves through ids.lookup_session_id
    assert waited.exit_code == 13
    assert waited.output["report"]["status"] == "blocked"
    assert waited.output["state"] == "exited"


def test_wait_polls_until_the_run_is_final(st: Store, clock: Clock) -> None:
    sid = session(st)
    clock.on_sleep = lambda n: finish_done(st, sid) if n == 3 else None
    waited = wait(st, clock, sid, poll_interval=0.5)
    assert waited.exit_code == 0
    assert clock.slept == [0.5, 0.5, 0.5]
    assert waited.output["outcome"] == "done"


# Binding and --run


def test_wait_binds_to_the_current_run_at_invocation(st: Store, clock: Clock) -> None:
    """A resume that starts run 2 while wait polls does not move the wait to run 2."""
    sid = session(st)

    def finish_and_resume(n: int) -> None:
        if n == 1:
            assert st.finalize_run(sid, 1, KILLED)
            second_run(st, sid)

    clock.on_sleep = finish_and_resume
    waited = wait(st, clock, sid)
    assert (waited.output["run"], waited.exit_code) == (1, 15)
    # Run 1's own final state, not the session's (now run 2's starting).
    assert waited.output["state"] == "killed"


def test_wait_without_run_binds_the_newest_run(st: Store, clock: Clock) -> None:
    sid = session(st)
    assert st.finalize_run(sid, 1, KILLED)
    second_run(st, sid)
    waited = wait(st, clock, sid, timeout=1.0)
    assert waited.output["run"] == 2
    assert waited.exit_code == 22


def test_run_selects_another_run(st: Store, clock: Clock) -> None:
    sid = session(st)
    assert st.finalize_run(sid, 1, KILLED)
    second_run(st, sid)
    waited = wait(st, clock, sid, run_no=1, timeout=1.0)
    assert (waited.output["run"], waited.exit_code, waited.output["state"]) == (1, 15, "killed")
    assert clock.slept == []


@pytest.mark.parametrize(
    "run_no",
    [0, -1, 2, 9007199254740991, 2**63, 99999999999999999999, -(2**63) - 1, -99999999999999999999],
)
def test_a_run_that_does_not_exist_is_not_found(st: Store, clock: Clock, run_no: int) -> None:
    sid = session(st)
    with pytest.raises(NotFoundError, match=f"has no run {run_no}"):
        wait(st, clock, sid, run_no=run_no)


def test_an_unknown_session_is_not_found(st: Store, clock: Clock) -> None:
    session(st)
    with pytest.raises(NotFoundError):
        wait(st, clock, "cs-zzzzzzzz")


def test_an_ambiguous_prefix_lists_the_matches(st: Store, clock: Clock) -> None:
    ids_ = iter(["cs-aaaaaaaa", "cs-aaaabbbb"])
    for _ in range(2):
        st.allocate(new_run(), new_id=lambda _used: next(ids_))
    with pytest.raises(AmbiguousError) as caught:
        wait(st, clock, "cs-aaaa")
    assert caught.value.matches == ["cs-aaaaaaaa", "cs-aaaabbbb"]


def test_a_forgotten_session_is_not_found(st: Store, clock: Clock) -> None:
    sid = session(st)
    forget(st, sid)
    with pytest.raises(NotFoundError):
        wait(st, clock, sid)


# Forgotten mid-wait


def test_a_session_forgotten_mid_wait_is_not_found(st: Store, clock: Clock) -> None:
    sid = session(st)
    clock.on_sleep = lambda n: forget(st, sid) if n == 2 else None
    with pytest.raises(NotFoundError, match="forgotten"):
        wait(st, clock, sid, timeout=60.0)
    assert len(clock.slept) == 2


def test_a_session_deleted_mid_wait_is_not_found(st: Store, clock: Clock) -> None:
    sid = session(st)

    def delete(n: int) -> None:
        with st.transaction() as conn:
            for table in ("events", "labels", "runs", "sessions"):
                conn.execute(f"DELETE FROM {table} WHERE csession_id = ?", (sid,))

    clock.on_sleep = delete
    with pytest.raises(NotFoundError):
        wait(st, clock, sid)


# --timeout


def test_the_timeout_exits_22_with_the_live_run(st: Store, clock: Clock) -> None:
    sid = session(st)
    report(st, sid, "done")
    add_event(st, sid, "report", "reported", "working")
    waited = wait(st, clock, sid, timeout=2.5, poll_interval=1.0)
    assert waited.exit_code == 22
    assert clock.slept == [1.0, 1.0, 0.5]  # never past the deadline
    out = waited.output
    assert (out["timed_out"], out["exit_code"], out["run_exit_code"]) == (True, 22, None)
    assert (out["outcome"], out["end_reason"]) == (None, None)
    assert (out["pending"], out["phase"], out["state"]) == (False, "session", "reported")
    # The run's current report, though the run has no outcome yet.
    assert out["report"]["status"] == "done" and out["report"]["summary"] == "ok"


def test_the_timeout_without_a_report_has_report_null(st: Store, clock: Clock) -> None:
    sid = session(st)
    waited = wait(st, clock, sid, timeout=0.2)
    assert waited.exit_code == 22
    assert waited.output["report"] is None
    assert waited.output["state"] == "starting"


def test_the_timeout_of_a_pending_run_says_pending_and_launch(st: Store, clock: Clock) -> None:
    sid = session(st)
    assert st.finalize_run(sid, 1, KILLED)
    second_run(st, sid, pending=True)
    waited = wait(st, clock, sid, timeout=0.5)
    out = waited.output
    assert (waited.exit_code, out["pending"], out["phase"], out["state"]) == (
        22,
        True,
        "launch",
        "starting",
    )


def test_the_timeout_before_the_spawn_says_launch(st: Store, clock: Clock) -> None:
    """Adopted (a wrapper) but claude not yet spawned: not pending, phase launch
    (schemas/cli/outcome.json: phase)."""
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    waited = wait(st, clock, sid, timeout=0.5)
    assert (waited.output["pending"], waited.output["phase"]) == (False, "launch")


def test_a_run_final_at_the_deadline_is_reported_final(st: Store, clock: Clock) -> None:
    sid = session(st)
    clock.on_sleep = lambda n: finish_done(st, sid) if n == 2 else None
    waited = wait(st, clock, sid, timeout=2.0, poll_interval=1.0)
    assert (waited.exit_code, waited.output["timed_out"]) == (0, False)


def test_a_run_finalized_between_the_reads_is_reported_final(
    st: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout's output is read after the run was seen live: a run finalized in between
    is reported final, never as a timeout with an outcome."""
    sid = session(st)
    real = st.waited_run

    def stale(csession_id: str, run_no: int | None) -> dict[str, Any] | None:
        row = real(csession_id, run_no)
        assert row is not None
        st.finalize_run(sid, 1, KILLED)
        return {**row, "ended_at": None}

    monkeypatch.setattr(st, "waited_run", stale)
    waited = wait(st, clock, sid, timeout=0.1)
    assert (waited.exit_code, waited.output["timed_out"]) == (15, False)


def test_the_timeout_output_comes_from_one_read(
    st: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-5: a report that lands after the last poll's read shows in the timeout output
    together with the state it set, never a stale report next to a fresh state."""
    sid = session(st)
    real = st.waited_run
    reads: list[int] = []

    def then_report(csession_id: str, run_no: int | None) -> dict[str, Any] | None:
        row = real(csession_id, run_no)
        reads.append(1)
        if len(reads) == 2:  # the last poll's read, right before the deadline check
            report(st, sid, "done")
        return row

    monkeypatch.setattr(st, "waited_run", then_report)
    out = wait(st, clock, sid, timeout=0.1).output
    assert len(reads) == 2
    assert out["state"] == "reported"
    assert out["report"] is not None and out["report"]["status"] == "done"


def test_the_timeout_report_has_the_final_reports_shape(st: Store, clock: Clock) -> None:
    """One report builder for the outcome object and the timeout output."""
    sid = session(st)
    report(st, sid, "done")
    st.conn.execute(
        "UPDATE runs SET artifact_name = 'a.md', artifact_path = '/p/a.md', "
        "artifact_sha256 = ?, artifact_size = 3 WHERE csession_id = ?",
        ("0" * 64, sid),
    )
    live = wait(st, clock, sid, timeout=0.1).output["report"]
    assert st.finalize_run(sid, 1, DONE)
    final = wait(st, clock, sid).output["report"]
    assert live == final


def test_no_timeout_waits_without_a_deadline(st: Store, clock: Clock) -> None:
    sid = session(st)
    clock.on_sleep = lambda n: finish_done(st, sid) if n == 50 else None
    waited = wait(st, clock, sid, timeout=None, poll_interval=60.0)
    assert waited.exit_code == 0
    assert len(clock.slept) == 50


def test_the_liveness_check_runs_at_every_poll(
    st: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = session(st)
    checked: list[str] = []
    monkeypatch.setattr(waiter, "check_liveness", lambda _st, s: checked.append(s))
    wait(st, clock, sid, timeout=3.0)
    assert checked == [sid] * 4  # at invocation and after each of the 3 sleeps


def test_wait_holds_no_write_lock_between_polls(st: Store, clock: Clock, tmp_path: Path) -> None:
    """Another process can write while wait polls (wait never opens a write transaction)."""
    sid = session(st)
    writes: list[int] = []

    def write_elsewhere(n: int) -> None:
        other = Store.open(st.path, busy_timeout=0.05, holder=Holder(pid=2, start="2"))
        try:
            other.add_run_warnings(sid, 1, [f"write {n}"])
            writes.append(n)
        finally:
            other.close()

    clock.on_sleep = write_elsewhere
    wait(st, clock, sid, timeout=3.0)
    assert writes == [1, 2, 3]
    assert not st.conn.in_transaction


# The output by code (schemas/cli/wait.json)


@pytest.mark.parametrize(
    ("outcome", "end_reason", "code", "state", "report_status"),
    [
        ("done", "normal", 0, "exited", "done"),
        ("needs-input", "normal", 13, "exited", "needs-input"),
        ("failed", "normal", 18, "exited", "failed"),
        ("exited-unreported", "no_report", 14, "exited-unreported", None),
        ("timeout", "timeout", 16, "timeout", None),
        ("lost", "lost", 17, "lost", None),
    ],
)
def test_every_outcome_validates_against_the_wait_schema(
    st: Store,
    clock: Clock,
    outcome: str,
    end_reason: str,
    code: int,
    state: str,
    report_status: str | None,
) -> None:
    sid = session(st)
    if report_status is not None:
        report(st, sid, report_status)
    final = Final("session", outcome, end_reason, code, state, "exit")
    assert st.finalize_run(sid, 1, final)
    waited = wait(st, clock, sid)
    assert (waited.exit_code, waited.output["run_exit_code"], waited.output["state"]) == (
        code,
        code,
        state,
    )


def test_a_not_started_run_exits_12(st: Store, clock: Clock) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    final = Final("launch", "not-started", "never_adopted", 12, "not-started", "liveness")
    assert st.finalize_run(sid, 1, final)
    waited = wait(st, clock, sid)
    assert (waited.exit_code, waited.output["phase"], waited.output["cost_usd"]) == (
        12,
        "launch",
        None,
    )


# claude-session wait (the entry point)


def state_db(state: Path) -> Store:
    return Store.open(str(state / "state.db"))


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


def run_wait(
    tmp_path: Path, *args: str, config: str = "poll_interval: 0.05s\n", timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config, encoding="utf-8")
    return subprocess.run(
        [*claude_session_cmd(), "wait", "--config", str(cfg), *args],
        cwd=tmp_path,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# A trusted run: wait's outcome file is checked against the baseline only.
TRUSTED = {"kind": "implement", "profile": {"sandbox": "off", "reads_third_party": False}}


def stored_session(final: Final | None = None) -> str:
    with store.open_state() as st:
        sid = session(st, resolved=TRUSTED)
        if final is not None:
            assert st.finalize_run(sid, 1, final)
    return sid


def test_the_command_prints_the_outcome_and_exits_with_its_code(tmp_path: Path) -> None:
    sid = stored_session(KILLED)
    proc = run_wait(tmp_path, sid, "--json")
    assert proc.returncode == 15, proc.stderr
    out = json.loads(proc.stdout)
    validate(out, WAIT_SCHEMA)
    assert (out["csession_id"], out["run"], out["timed_out"]) == (sid, 1, False)


def test_the_command_times_out_with_22(tmp_path: Path) -> None:
    sid = stored_session()
    proc = run_wait(tmp_path, sid, "--json", "--timeout", "0.3s")
    assert proc.returncode == 22, proc.stderr
    out = json.loads(proc.stdout)
    validate(out, WAIT_SCHEMA)
    assert out["timed_out"] is True and out["state"] == "starting"


def test_the_command_without_json_prints_a_summary_on_stderr(tmp_path: Path) -> None:
    sid = stored_session()
    proc = run_wait(tmp_path, sid, "--timeout", "0.1s")
    assert proc.returncode == 22
    assert proc.stdout == ""
    assert sid in proc.stderr and "timed out" in proc.stderr


def test_the_command_on_an_unknown_session_exits_10(tmp_path: Path) -> None:
    stored_session()
    proc = run_wait(tmp_path, "cs-zzzzzzzz", "--json")
    assert proc.returncode == 10
    assert json.loads(proc.stdout)["error"]["code"] == "not_found"


def test_the_command_writes_its_output_to_the_outcome_file(tmp_path: Path) -> None:
    sid = stored_session(KILLED)
    out_file = tmp_path / "out" / "wait.json"
    out_file.parent.mkdir()
    proc = run_wait(tmp_path, sid, "--json", "--outcome-file", "out/wait.json")
    assert proc.returncode == 15, proc.stderr
    assert json.loads(out_file.read_text()) == json.loads(proc.stdout)
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600


def test_the_command_writes_a_not_found_envelope_to_the_outcome_file(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """R1-7: wait's own output on exit 10 is the error envelope; so is its outcome file (not
    under wait's cwd: before a run is bound, that is refused, see test_outcome_file.py)."""
    stored_session()
    out_file = tmp_path_factory.mktemp("out") / "wait.json"
    proc = run_wait(tmp_path, "cs-zzzzzzzz", "--json", "--outcome-file", str(out_file))
    assert proc.returncode == 10
    written = json.loads(out_file.read_text())
    assert written == json.loads(proc.stdout)
    assert written["error"]["code"] == "not_found"


def test_a_session_forgotten_mid_wait_writes_the_not_found_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from claude_session import cli

    sid = stored_session()

    class Forgetting(Clock):
        def sleep(self, seconds: float) -> None:
            super().sleep(seconds)
            with store.open_state() as other:
                forget(other, sid)

    monkeypatch.setattr(waiter, "SystemClock", Forgetting)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("poll_interval: 0.05s\n", encoding="utf-8")
    out_file = tmp_path / "wait.json"
    code = cli.main(
        ["wait", sid, "--config", str(cfg), "--timeout", "30s", "--outcome-file", str(out_file)]
    )
    assert code == 10
    assert "forgotten" in capsys.readouterr().err
    written = json.loads(out_file.read_text())
    assert written["error"]["code"] == "not_found"
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600


def test_an_internal_error_in_wait_writes_the_internal_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-2: the generic `internal` envelope wait prints also goes to its outcome file."""
    from claude_session import cli

    sid = stored_session()

    def broken(*_args: Any, **_kwargs: Any) -> waiter.Waited:
        raise RuntimeError("boom")

    monkeypatch.setattr(waiter, "wait_for_run", broken)
    out_file = tmp_path / "wait.json"
    code = cli.main(["wait", sid, "--json", "--outcome-file", str(out_file)])
    assert code == 1
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(out_file.read_text())
    assert written == printed
    assert written["error"]["code"] == "internal"
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600


def test_an_internal_error_with_an_unwritable_outcome_file_still_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from claude_session import cli

    sid = stored_session()

    def broken(*_args: Any, **_kwargs: Any) -> waiter.Waited:
        raise RuntimeError("boom")

    monkeypatch.setattr(waiter, "wait_for_run", broken)
    code = cli.main(["wait", sid, "--json", "--outcome-file", str(tmp_path / "no" / "x.json")])
    assert code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == "internal"
    assert "--outcome-file" in captured.err


def test_an_outcome_file_failure_is_a_warning(tmp_path: Path, state: Path) -> None:
    sid = stored_session(KILLED)
    proc = run_wait(tmp_path, sid, "--json", "--outcome-file", str(tmp_path / "no" / "x.json"))
    assert proc.returncode == 15, proc.stderr  # the exit code is unchanged
    out = json.loads(proc.stdout)
    validate(out, WAIT_SCHEMA)
    assert any("--outcome-file" in w for w in out["warnings"])
    records = [json.loads(line) for line in (state / "log.jsonl").read_text().splitlines()]
    failed = [r for r in records if r["event"] == "outcome_file_failed"]
    assert len(failed) == 1 and failed[0]["csession_id"] == sid


def test_the_command_returns_when_an_exec_run_ends_at_its_bound(
    tmp_path: Path, state: Path
) -> None:
    """``wait`` in its own process on a running headless ``exec``: it returns the run's
    outcome once ``exec`` ended it at the kind's timeout."""
    from .test_exec_headless import START, start_hanging

    work = tmp_path / "work"
    work.mkdir()
    marker = tmp_path / "hanging"
    steps = [START, {"do": "touch", "path": str(marker)}, {"do": "hang"}]
    proc = start_hanging(
        tmp_path, work, steps, marker, config="kinds:\n  implement:\n    timeout: 4s\n"
    )
    try:
        db = sqlite3.connect(f"file:{state / 'state.db'}?mode=ro", uri=True)
        try:
            (sid,) = db.execute("SELECT csession_id FROM sessions").fetchone()
        finally:
            db.close()
        waited = run_wait(tmp_path / "work", sid, "--json", "--timeout", "30s")
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 16, err
    assert waited.returncode == 16, waited.stderr
    result = json.loads(waited.stdout)
    validate(result, WAIT_SCHEMA)
    assert (result["outcome"], result["end_reason"]) == ("timeout", "timeout")
    exec_outcome = json.loads(out)
    assert {k: v for k, v in result.items() if k not in waiter.WAIT_FIELDS} == exec_outcome


# Golden outputs (tests/golden/wait-*.json)


def normalized(output: dict[str, Any]) -> dict[str, Any]:
    return {**output, "csession_id": "cs-aaaaaaaa"}


def test_golden_wait_outputs(st: Store, clock: Clock) -> None:
    from .golden import assert_golden

    sid = session(st)
    report(st, sid, "done")
    timed_out = wait(st, clock, sid, timeout=0.5).output
    assert_golden(timed_out, "wait-timed-out", normalize=normalized)
    assert st.finalize_run(sid, 1, DONE)
    final = wait(st, clock, sid).output
    assert_golden(final, "wait-done", normalize=normalized)
