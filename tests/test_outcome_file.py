"""``--outcome-file`` (interface.md, Outcome file; design.md, Data model): the finalizer's write
right after the finalization compare-and-set, exactly once, the backfill, write failures as
warnings, rejected paths never written, mode 0600 and no symlink followed.

The writers run against a real store; the ``exec`` tests start the real entry point with the
fake ``claude``."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sqlite3
import stat
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_session import cli, ids, launch, outcomefile, pathrules, store, supervisor, waiter
from claude_session import liveness as liveness_module
from claude_session.errors import LaunchValidationError, OutcomeFileRejected
from claude_session.liveness import Checked
from claude_session.signals import LaunchInterrupted
from claude_session.store import Final, Holder, Store

from .schemas import validate
from .test_exec_headless import (
    ISOLATED as ISOLATED_FLAGS,
)
from .test_exec_headless import (
    START,
    STOP,
    claude_session_cmd,
    private_home,
    result_step,
    run_exec,
)
from .test_store_runs import new_run

EXEC_SCHEMA = "claude-session/cli/exec.json"
RACERS = 6  # processes racing one outcome-file write
WAIT_SCHEMA = "claude-session/cli/wait.json"
KILLED = Final(
    phase="session",
    outcome="killed",
    end_reason="signal_kill",
    exit_code=15,
    state="killed",
    cause="exit",
)
LAUNCH_FAILED = Final(
    phase="launch",
    outcome="not-started",
    end_reason="unsafe_cwd",
    exit_code=12,
    state="not-started",
    cause="launch",
    message="cwd refused",
)
TRUSTED = {"kind": "implement", "profile": {"sandbox": "off", "reads_third_party": False}}
ISOLATED = {"kind": "respond", "profile": {"sandbox": "required", "reads_third_party": True}}


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def st(state: Path) -> Any:
    with store.open_state() as s:
        yield s


def allocate(st: Store, out: Path | None, *, resolved: dict[str, Any] = TRUSTED, **kw: Any) -> str:
    fields = {"outcome_file": None if out is None else str(out), "resolved": resolved, **kw}
    return st.allocate(new_run(**fields), new_id=ids.new_session_id)


def final_run(st: Store, out: Path | None, final: Final = KILLED, **kw: Any) -> str:
    sid = allocate(st, out, **kw)
    assert st.finalize_run(sid, 1, final)
    return sid


def isolated_run(
    st: Store, out: Path | None, cwd: Path, writable: list[Path] | None = None, **kw: Any
) -> str:
    """A final isolated run with cwd ``cwd`` and the session-writable paths launch step 5
    stored (``writable``; None: none stored)."""
    sid = final_run(st, out, resolved=ISOLATED, **kw)
    st.update_run(sid, 1, cwd=str(cwd))
    if writable is not None:
        st.update_run(sid, 1, writable_json=json.dumps([str(p) for p in writable]))
    return sid


def victim_link(writable: Path, tmp_path: Path) -> tuple[Path, Path]:
    """``writable/link``, a symlink an agent planted to a directory outside every
    session-writable path, and that directory (the victim): a write through the link would
    land there."""
    victim = tmp_path / "victim"
    victim.mkdir(exist_ok=True)
    writable.mkdir(parents=True, exist_ok=True)
    (writable / "link").symlink_to(victim)
    return writable / "link", victim


def counting_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every outcome-file write (``fsutil.write_private_file``) this process makes."""
    writes: list[str] = []
    real = outcomefile.fsutil.write_private_file

    def counted(path: str, data: bytes) -> None:
        writes.append(path)
        real(path, data)

    monkeypatch.setattr(outcomefile.fsutil, "write_private_file", counted)
    return writes


def written_at(st: Store, sid: str, run_no: int = 1) -> str | None:
    return st.run_record(sid, run_no)["outcome_file_written_at"]


def log_records(state: Path) -> list[dict[str, Any]]:
    path = state / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def failures(state: Path) -> list[dict[str, Any]]:
    return [r for r in log_records(state) if r["event"] == "outcome_file_failed"]


# The run's write (finalizer and backfill)


def test_a_final_run_gets_its_outcome_object_once_mode_0600(st: Store, tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    sid = final_run(st, out)
    assert outcomefile.write_run(st, sid, 1) is None
    written = json.loads(out.read_text())
    validate(written, EXEC_SCHEMA)
    assert written == outcomefile.final_output(st.run_record(sid, 1))
    assert (written["csession_id"], written["outcome"], written["exit_code"]) == (sid, "killed", 15)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert written_at(st, sid) is not None
    # Exactly once: a second write (a racing backfill) changes nothing.
    out.write_text("caller's copy")
    assert outcomefile.write_run(st, sid, 1) is None
    assert outcomefile.backfill(st) == []
    assert out.read_text() == "caller's copy"


def test_a_launch_phase_end_validates_against_the_schema(st: Store, tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    sid = final_run(st, out, LAUNCH_FAILED)
    outcomefile.write_run(st, sid, 1)
    written = json.loads(out.read_text())
    validate(written, EXEC_SCHEMA)
    assert (written["phase"], written["end_reason"], written["message"]) == (
        "launch",
        "unsafe_cwd",
        "cwd refused",
    )


def test_a_run_that_is_not_final_is_never_written(st: Store, tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    sid = allocate(st, out)
    assert outcomefile.write_run(st, sid, 1) is None
    assert not out.exists() and written_at(st, sid) is None
    assert st.unwritten_outcome_files() == []


def test_a_run_without_an_outcome_file_writes_nothing(st: Store, tmp_path: Path) -> None:
    sid = final_run(st, None)
    assert outcomefile.write_run(st, sid, 1) is None
    assert written_at(st, sid) is None
    assert list(tmp_path.iterdir()) == []


def test_a_write_failure_is_a_warning_logged_and_not_retried(
    st: Store, state: Path, tmp_path: Path
) -> None:
    sid = final_run(st, tmp_path / "missing" / "out.json")
    warning = outcomefile.write_run(st, sid, 1)
    assert warning is not None and warning.startswith(outcomefile.NOT_WRITTEN)
    row = st.run_record(sid, 1)
    assert (row["outcome"], row["exit_code"]) == ("killed", 15)  # the outcome is unchanged
    assert warning in json.loads(row["warnings_json"])
    logged = failures(state)
    assert len(logged) == 1 and (logged[0]["csession_id"], logged[0]["run"]) == (sid, 1)
    # Recorded as handled: the backfill does not retry it (and log again).
    assert row["outcome_file_written_at"] is not None
    assert outcomefile.backfill(st) == []
    assert len(failures(state)) == 1


def test_a_symlink_at_the_name_is_replaced_never_followed(st: Store, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep")
    out = tmp_path / "out.json"
    out.symlink_to(target)
    sid = final_run(st, out)
    assert outcomefile.write_run(st, sid, 1) is None
    assert target.read_text() == "keep"
    assert not out.is_symlink() and json.loads(out.read_text())["csession_id"] == sid


def test_a_path_under_the_sessions_directory_is_never_written(
    st: Store, state: Path, tmp_path: Path
) -> None:
    other = state / "sessions" / "cs-aaaaaaaa" / "artifacts"
    other.mkdir(parents=True)
    out = other / "out.json"
    sid = final_run(st, out)  # even a trusted run: any isolated agent may write there
    assert outcomefile.write_run(st, sid, 1) == outcomefile.REJECTED
    assert not out.exists()
    assert failures(state)[0]["message"] == outcomefile.REJECTED


def test_a_path_under_the_tmp_root_is_never_written(st: Store, tmp_path: Path) -> None:
    root = Path(pathrules.tmp_parent())
    root.mkdir(parents=True, exist_ok=True)
    sid = final_run(st, root / "out.json")
    assert outcomefile.write_run(st, sid, 1) == outcomefile.REJECTED
    assert not (root / "out.json").exists()


def test_an_isolated_runs_cwd_is_refused_a_trusted_runs_is_not(st: Store, tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    isolated = isolated_run(st, cwd / "a.json", cwd, [])
    trusted = final_run(st, cwd / "b.json")
    st.update_run(trusted, 1, cwd=str(cwd))
    assert outcomefile.write_run(st, isolated, 1) == outcomefile.REJECTED
    assert outcomefile.write_run(st, trusted, 1) is None
    assert not (cwd / "a.json").exists() and (cwd / "b.json").exists()


def test_a_symlinked_parent_inside_a_writable_path_is_refused(st: Store, tmp_path: Path) -> None:
    """The agent could re-point a symlink in a directory it writes, even when the target is
    safe now: every hop of the resolution is checked."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    safe = tmp_path / "safe"
    safe.mkdir()
    (cwd / "link").symlink_to(safe)
    sid = isolated_run(st, cwd / "link" / "out.json", cwd, [])
    assert outcomefile.write_run(st, sid, 1) == outcomefile.REJECTED
    assert list(safe.iterdir()) == []


def test_the_stored_writable_set_is_refused_by_every_writer(st: Store, tmp_path: Path) -> None:
    """The finalizer and the backfill check the run's stored session-writable paths (its
    write_dirs here), not just its cwd."""
    link, victim = victim_link(tmp_path / "write-dir", tmp_path)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    first = isolated_run(st, link / "a.json", cwd, [tmp_path / "write-dir"])
    second = isolated_run(st, link / "b.json", cwd, [tmp_path / "write-dir"])
    assert outcomefile.write_run(st, first, 1) == outcomefile.REJECTED
    assert outcomefile.backfill(st) == [f"{second} run 1: {outcomefile.REJECTED}"]
    assert list(victim.iterdir()) == []


def test_an_isolated_run_without_stored_paths_is_never_written(
    st: Store, state: Path, tmp_path: Path
) -> None:
    """Fail closed: the paths its agent could write are not known."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    out = tmp_path / "safe" / "out.json"
    out.parent.mkdir()
    sid = isolated_run(st, out, cwd, None)
    assert outcomefile.write_run(st, sid, 1) == outcomefile.UNDETERMINED
    assert not out.exists()
    assert failures(state)[0]["message"] == outcomefile.UNDETERMINED
    assert outcomefile.backfill(st) == []  # handled: not retried


@pytest.mark.parametrize(
    ("resolved", "isolated"),
    [
        (TRUSTED, False),
        (ISOLATED, True),
        ({"profile": {"sandbox": "preferred", "reads_third_party": False}}, True),
        ({"kind": "x"}, True),  # unreadable: fail closed
        ({"profile": "nonsense"}, True),
    ],
)
def test_run_isolation_from_the_stored_resolution(resolved: dict[str, Any], isolated: bool) -> None:
    row = {"resolved_json": json.dumps(resolved), "reads_third_party": 0}
    assert outcomefile.run_isolated(row) is isolated
    assert outcomefile.run_isolated({"resolved_json": "{not json"}) is True


# Exactly once under concurrency, and the backfill


def test_a_writer_holding_the_lease_wins_the_other_skips(
    st: Store, state: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out.json"
    sid = final_run(st, out)
    lease = f"{outcomefile.LEASE_PREFIX}:{sid}:1"
    with Store.open(str(state / "state.db"), holder=Holder(pid=os.getpid(), start="x")) as other:
        other.liveness = lambda _pid, _start: True  # type: ignore[method-assign]
        assert other.take_lease(lease)
        st.liveness = lambda _pid, _start: True  # type: ignore[method-assign]
        assert outcomefile.write_run(st, sid, 1) is None
        assert not out.exists() and written_at(st, sid) is None
        other.release_lease(lease)
    assert outcomefile.backfill(st) == []
    assert json.loads(out.read_text())["csession_id"] == sid


def test_a_dead_writers_lease_is_taken_over(st: Store, state: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    sid = final_run(st, out)
    with Store.open(str(state / "state.db"), holder=Holder(pid=2**22 + 7, start="gone")) as dead:
        assert dead.take_lease(f"{outcomefile.LEASE_PREFIX}:{sid}:1")
    assert outcomefile.backfill(st) == []
    assert out.exists()


def test_backfill_writes_final_unwritten_runs_only(st: Store, tmp_path: Path) -> None:
    a, b, live = (tmp_path / name for name in ("a.json", "b.json", "live.json"))
    first = final_run(st, a)
    second = final_run(st, b)
    pending = allocate(st, live)
    assert st.unwritten_outcome_files() == sorted([(first, 1), (second, 1)])
    assert st.unwritten_outcome_files(first) == [(first, 1)]
    assert outcomefile.backfill(st, first) == []
    assert a.exists() and not b.exists()
    assert outcomefile.backfill(st) == []
    assert b.exists() and not live.exists()
    assert written_at(st, pending) is None


def test_backfill_shows_the_runs_own_final_state(st: Store, tmp_path: Path) -> None:
    """A run backfilled after the session moved on reports its own final state."""
    out = tmp_path / "out.json"
    sid = final_run(st, out)
    st.conn.execute("UPDATE sessions SET state = 'working' WHERE csession_id = ?", (sid,))
    outcomefile.backfill(st)
    written = json.loads(out.read_text())
    assert written["state"] == "killed"
    validate(written, EXEC_SCHEMA)


def test_backfill_returns_the_warnings(st: Store, tmp_path: Path) -> None:
    sid = final_run(st, tmp_path / "no" / "out.json")
    warnings = outcomefile.backfill(st)
    assert len(warnings) == 1 and warnings[0].startswith(f"{sid} run 1: {outcomefile.NOT_WRITTEN}")


def test_a_store_error_leaves_the_write_to_the_backfill(
    st: Store, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.json"
    sid = final_run(st, out)

    def busy(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(st, "run_record", busy)
        warning = outcomefile.write_run(st, sid, 1)
    assert warning is not None and "reconcile writes it later" in warning
    assert not out.exists() and written_at(st, sid) is None
    assert len(failures(state)) == 1
    assert outcomefile.backfill(st) == []
    assert out.exists()


def test_a_failed_claim_writes_nothing_and_the_backfill_writes_once(
    st: Store, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claim before write: a store error at the claim leaves no file and an honest warning;
    the backfill then writes the file exactly once."""
    writes = counting_writes(monkeypatch)
    out = tmp_path / "out.json"
    sid = final_run(st, out)

    def busy(*_args: Any, **_kwargs: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(st, "claim_outcome_file", busy)
        warning = outcomefile.write_run(st, sid, 1)
    assert warning is not None and "reconcile writes it later" in warning
    assert not out.exists() and writes == []
    assert outcomefile.backfill(st) == []
    assert outcomefile.backfill(st) == []
    assert writes == [str(out)] and json.loads(out.read_text())["csession_id"] == sid


def test_store_failures_after_the_write_never_write_again(
    st: Store, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes = counting_writes(monkeypatch)
    out = tmp_path / "out.json"
    sid = final_run(st, out)

    def busy(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(st, "release_lease", busy)
        m.setattr(st, "add_run_warnings", busy)
        assert outcomefile.write_run(st, sid, 1) is None
    assert writes == [str(out)]
    assert outcomefile.write_run(st, sid, 1) is None
    assert outcomefile.backfill(st) == []
    assert writes == [str(out)]


def test_a_rejection_keeps_its_warning_when_the_store_fails_after(
    st: Store, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = state / "sessions" / "cs-aaaaaaaa" / "out.json"
    out.parent.mkdir(parents=True)
    sid = final_run(st, out)

    def busy(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(st, "release_lease", busy)
    monkeypatch.setattr(st, "add_run_warnings", busy)
    assert outcomefile.write_run(st, sid, 1) == outcomefile.REJECTED
    assert [r["message"] for r in failures(state)] == [outcomefile.REJECTED]


@pytest.mark.parametrize("stall", ["before_claim", "after_claim"])
def test_a_stalled_writer_whose_lease_was_taken_over_never_writes_twice(
    stall: str, st: Store, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer stalled past the lease TTL: a second writer (61 s later) takes its lease over.
    Exactly one of them writes, whether the stall came before or after the claim."""
    writes = counting_writes(monkeypatch)
    out = tmp_path / "out.json"
    sid = final_run(st, out)
    st.liveness = lambda _pid, _start: True  # type: ignore[method-assign]
    later = st.clock() + store.LEASE_TTL + timedelta(seconds=1)
    taker_warnings: list[str | None] = []

    def taker() -> None:
        with Store.open(str(state / "state.db"), clock=lambda: later) as other:
            other.liveness = lambda _pid, _start: True  # type: ignore[method-assign]
            taker_warnings.append(outcomefile.write_run(other, sid, 1))

    if stall == "before_claim":  # stalls while encoding, after its read, before its claim
        real_output = outcomefile.final_output

        stalled: list[bool] = []

        def stalled_output(row: dict[str, Any]) -> dict[str, Any]:
            if not stalled:  # the first writer's call; not the taker's own
                stalled.append(True)
                taker()
            return real_output(row)

        monkeypatch.setattr(outcomefile, "final_output", stalled_output)
    else:  # stalls between its claim and its write
        real_claim = st.claim_outcome_file

        def stalled_claim(csession_id: str, run_no: int) -> bool:
            won = real_claim(csession_id, run_no)
            taker()
            return won

        monkeypatch.setattr(st, "claim_outcome_file", stalled_claim)
    assert outcomefile.write_run(st, sid, 1) is None
    assert taker_warnings == [None]
    assert writes == [str(out)]
    assert outcomefile.backfill(st) == []
    assert writes == [str(out)]


def _race_writer(db: str, sid: str, counter: str, barrier: Any, backfill: bool) -> None:
    """One process of the race below: count each write in ``counter`` (one ``O_APPEND`` line),
    wait for the others, then write the outcome file (or backfill) a few times on a store whose
    busy timeout is tiny, so store errors happen."""
    real = outcomefile.fsutil.write_private_file

    def counted(path: str, data: bytes) -> None:
        fd = os.open(counter, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
        real(path, data)

    outcomefile.fsutil.write_private_file = counted  # type: ignore[assignment]
    with Store.open(db, busy_timeout=0.001) as racer:
        barrier.wait(timeout=30)
        for _ in range(5):
            if backfill:
                outcomefile.backfill(racer)
            else:
                outcomefile.write_run(racer, sid, 1)


def test_racing_processes_write_the_outcome_file_once(
    st: Store, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several processes race the finalizer's write and the backfill on a busy store: at most
    one writes (store errors only leave the write to a later backfill), and the backfill
    after the race completes it, so the file is written exactly once."""
    out = tmp_path / "out.json"
    sid = final_run(st, out)
    writes = counting_writes(monkeypatch)  # this process's own (the backfill after the race)
    counter = tmp_path / "writes"
    counter.write_text("")
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(RACERS)
    procs = [
        ctx.Process(
            target=_race_writer,
            args=(str(state / "state.db"), sid, str(counter), barrier, index % 2 == 1),
        )
        for index in range(RACERS)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
    assert [proc.exitcode for proc in procs] == [0] * RACERS
    raced = len(counter.read_text().splitlines())
    assert raced <= 1
    outcomefile.backfill(st)  # completes a write the race left to it, if any
    assert raced + len(writes) == 1
    assert written_at(st, sid) is not None
    assert json.loads(out.read_text())["csession_id"] == sid


# Launch: step 2, step 5 and the launch-phase end


def _resolved(isolated: bool, caller_cwd: str | None) -> launch.Resolved:
    profile = SimpleNamespace(isolated=lambda: isolated)
    return launch.Resolved(resolution=profile, caller_cwd=caller_cwd)  # type: ignore[arg-type]


def _process(state_root: Path, cwd: Path) -> launch.Process:
    return launch.Process(
        environ={},
        caller_cwd=str(cwd),
        claude_session="/opt/cs/bin/claude-session",
        interpreter="/usr/bin/python3",
        state_root=str(state_root),
        gateway=None,  # type: ignore[arg-type]
    )


def _request(outcome_file: str | None) -> launch.Request:
    return SimpleNamespace(outcome_file=outcome_file)  # type: ignore[return-value]


def test_step_2_refuses_an_isolated_runs_cwd(tmp_path: Path) -> None:
    root, cwd = tmp_path / "state", tmp_path / "repo"
    cwd.mkdir()
    proc = _process(root, cwd)
    out = str(cwd / "out.json")
    with pytest.raises(OutcomeFileRejected) as info:
        launch.check_outcome_file(_request(out), _resolved(True, str(cwd)), proc)
    assert info.value.exit_code == 2
    launch.check_outcome_file(_request(out), _resolved(False, str(cwd)), proc)
    launch.check_outcome_file(_request(None), _resolved(True, str(cwd)), proc)
    launch.check_outcome_file(_request(str(tmp_path / "o.json")), _resolved(True, str(cwd)), proc)


def test_step_2_refuses_the_sessions_directory_for_every_run(tmp_path: Path) -> None:
    root = tmp_path / "state"
    out = str(root / "sessions" / "cs-aaaaaaaa" / "work" / "out.json")
    for isolated in (True, False):
        with pytest.raises(OutcomeFileRejected):
            launch.check_outcome_file(
                _request(out), _resolved(isolated, None), _process(root, tmp_path)
            )


def _alloc(state: Path, sid: str) -> launch.Allocated:
    return launch.Allocated(sid, 1, str(state / "sessions" / sid), str(state / "private" / sid), ())


def test_step_5_rejects_the_path_and_the_end_drops_it_with_the_finalization(
    st: Store, state: Path, tmp_path: Path
) -> None:
    write_dir = tmp_path / "write-dir"
    write_dir.mkdir()
    out = write_dir / "out.json"
    sid = allocate(st, out, resolved=ISOLATED)
    a = _alloc(state, sid)
    writable = pathrules.SessionWritable([str(write_dir)])
    launch.check_outcome_path(str(tmp_path / "ok.json"), writable)
    launch.check_outcome_path(str(out), None)  # trusted: nothing session-writable
    with pytest.raises(launch.UnsafeOutcomeFile) as info:
        launch.check_outcome_path(str(out), writable)
    assert info.value.end_reason == "unsafe_path"
    assert st.run_record(sid, 1)["outcome_file"] == str(out)  # not dropped separately
    launch.end_launch(st, a, None, None, _process(state, tmp_path), info.value)
    row = st.run_record(sid, 1)
    assert (row["outcome"], row["end_reason"]) == ("not-started", "unsafe_path")
    assert row["outcome_file"] is None
    assert not out.exists()


def test_a_rejected_path_stays_unwritten_when_the_launch_end_cannot_write(
    st: Store, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drop is part of the finalization: a lease failure at the end, then a backfill,
    never write the path step 5 rejected (here through an agent-planted symlink)."""
    link, victim = victim_link(tmp_path / "write-dir", tmp_path)
    out = link / "out.json"
    sid = allocate(st, out, resolved=ISOLATED)
    with pytest.raises(launch.UnsafeOutcomeFile) as info:
        launch.check_outcome_path(str(out), pathrules.SessionWritable([str(link.parent)]))

    def busy(*_args: Any, **_kwargs: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(st, "take_lease", busy)
        launch.end_launch(st, _alloc(state, sid), None, None, _process(state, tmp_path), info.value)
    assert st.run_record(sid, 1)["ended_at"] is not None
    assert outcomefile.backfill(st) == []
    assert list(victim.iterdir()) == []


class _Signals:
    def enter_launch(self) -> None:
        pass

    def defer(self) -> None:
        pass


@pytest.mark.parametrize("known", ["stored", "guard"])
def test_a_signal_during_step_5_never_writes_a_path_the_full_set_rejects(
    known: str, st: Store, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal right after step 5 knew the full set (stored on the run, or only in the
    launch's guard when the store write did not happen): the interrupted end checks it."""
    link, victim = victim_link(tmp_path / "write-dir", tmp_path)
    out = link / "out.json"
    sid = allocate(st, out, resolved=ISOLATED)
    full = (str(link.parent),)

    def validate(*args: Any, **kwargs: Any) -> None:
        guard = kwargs["guard"]
        guard.paths = full
        if known == "stored":
            st.update_run(sid, 1, writable_json=json.dumps(list(full)))
        raise LaunchInterrupted(signal.SIGINT)

    monkeypatch.setattr(launch, "validate", validate)
    resolved = launch.Resolved(resolution=SimpleNamespace(), caller_cwd=None)  # type: ignore[arg-type]
    with pytest.raises(LaunchInterrupted):
        launch.run(
            st,
            None,  # type: ignore[arg-type]
            _request(str(out)),
            resolved,
            _alloc(state, sid),
            _process(state, tmp_path),
            supervise=None,  # type: ignore[arg-type]
            signals=_Signals(),  # type: ignore[arg-type]
        )
    row = st.run_record(sid, 1)
    assert row["end_reason"] == "launch_interrupted"
    assert json.loads(row["writable_json"]) == list(full)
    assert outcomefile.REJECTED in json.loads(row["warnings_json"])
    assert outcomefile.backfill(st) == []
    assert list(victim.iterdir()) == []


def test_a_launch_phase_end_writes_the_outcome_file(st: Store, state: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    sid = allocate(st, out)
    a = launch.Allocated(sid, 1, str(state / "sessions" / sid), str(state / "private" / sid), ())
    error = LaunchValidationError("no claude", end_reason="claude_not_found")
    launch.end_launch(st, a, None, None, _process(state, tmp_path), error)
    written = json.loads(out.read_text())
    validate(written, EXEC_SCHEMA)
    assert (written["phase"], written["end_reason"], written["exit_code"]) == (
        "launch",
        "claude_not_found",
        12,
    )
    assert written == outcomefile.final_output(st.run_record(sid, 1))
    # A second launch-phase end of the same run lost the compare-and-set: nothing written.
    out.unlink()
    launch.end_launch(st, a, None, None, _process(state, tmp_path), error)
    assert not out.exists()


def test_the_internal_error_path_writes_the_outcome_file(st: Store, tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    sid = allocate(st, out)
    st.update_run(sid, 1, phase="session")

    class Dirs:
        def open_run_dir(self, _run: int) -> int:
            raise OSError("gone")

    v = SimpleNamespace(dirs=Dirs(), tmpdir=None, writable=None)
    a = launch.Allocated(sid, 1, "/s", "/p", ())
    cfg = SimpleNamespace(kill_grace=0.1)
    supervisor._internal_error(st, cfg, a, v, None)  # type: ignore[arg-type]
    written = json.loads(out.read_text())
    assert (written["outcome"], written["end_reason"]) == ("killed", "internal_error")
    validate(written, EXEC_SCHEMA)


# exec end to end


def test_exec_writes_its_outcome_object_right_after_finalizing(
    tmp_path: Path, state: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path_factory.mktemp("out") / "outcome.json"
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, {"do": "report", "args": ["done", "--summary", "ok"]}, STOP, result_step()],
        args=["--outcome-file", str(out)],
        config="poll_interval: 0.05s\n",
    )
    assert run.code == 0, run.proc.stderr
    written = json.loads(out.read_text())
    validate(written, EXEC_SCHEMA)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    # What --json prints; warnings the end adds after the write (cleanup) are stdout's only.
    printed = dict(run.outcome)
    assert {k: v for k, v in written.items() if k != "warnings"} == {
        k: v for k, v in printed.items() if k != "warnings"
    }
    assert set(written["warnings"]) <= set(printed["warnings"])
    row = run.run_row()
    assert row["outcome_file"] == str(out) and row["outcome_file_written_at"] is not None


def test_exec_with_an_unwritable_outcome_file_keeps_its_outcome(
    tmp_path: Path, state: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "missing-dir" / "outcome.json"
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, {"do": "report", "args": ["done", "--summary", "ok"]}, STOP, result_step()],
        args=["--outcome-file", str(out)],
        config="poll_interval: 0.05s\n",
    )
    assert run.code == 0, run.proc.stderr
    assert run.outcome["outcome"] == "done"
    assert any(w.startswith(outcomefile.NOT_WRITTEN) for w in run.outcome["warnings"])
    assert [r["csession_id"] for r in failures(state)] == [run.sid]
    assert not out.exists()


def test_exec_of_an_isolated_kind_refuses_an_outcome_file_in_its_cwd(
    tmp_path: Path, state: Path
) -> None:
    """Step 2: exit 2, nothing allocated, and the usage envelope is not written there."""
    work = tmp_path / "repo"
    work.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("claude_bin: /nonexistent/claude\n", encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Answer.\n", encoding="utf-8")
    proc = subprocess.run(
        [
            *claude_session_cmd(),
            *("exec", "--mode", "headless", "--kind", "respond", "--json"),
            *("--config", str(cfg), "--prompt-file", str(prompt), "--outcome-file", "out.json"),
        ],
        cwd=work,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout)["error"]["code"] == "usage"
    assert "session-writable" in proc.stderr
    assert not (work / "out.json").exists()
    with store.open_state() as st:
        assert st.list_sessions() == []


# Usage errors and wait


def test_a_usage_error_is_never_written_under_the_sessions_directory(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.open_state().close()
    target = state / "sessions" / "out.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    assert cli.main(["exec", "--timeout", "0", "--outcome-file", str(target)]) == 2
    assert not target.exists()
    assert outcomefile.REJECTED in capsys.readouterr().err
    assert failures(state)[0]["message"] == outcomefile.REJECTED


def test_the_rejection_itself_is_never_written_to_the_rejected_path(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    cli._write_outcome_file(str(out), OutcomeFileRejected("rejected"), lambda _path: False)
    assert not out.exists()


def test_wait_never_writes_under_the_sessions_directory(st: Store, state: Path) -> None:
    sid = final_run(st, None)
    target = state / "sessions" / sid / "work" / "wait.json"
    target.parent.mkdir(parents=True)
    proc = subprocess.run(
        [*claude_session_cmd(), "wait", sid, "--json", "--outcome-file", str(target)],
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 15, proc.stderr  # the exit code is unchanged
    out = json.loads(proc.stdout)
    validate(out, WAIT_SCHEMA)
    assert outcomefile.REJECTED in out["warnings"]
    assert not target.exists()


def test_wait_refuses_an_isolated_runs_cwd(st: Store, tmp_path: Path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    sid = isolated_run(st, None, cwd, [])
    proc = subprocess.run(
        [*claude_session_cmd(), "wait", sid, "--json", "--outcome-file", "wait.json"],
        cwd=cwd,
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 15, proc.stderr
    assert outcomefile.REJECTED in json.loads(proc.stdout)["warnings"]
    assert not (cwd / "wait.json").exists()


def test_wait_refuses_the_waited_runs_write_dirs(
    st: Store, state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    link, victim = victim_link(tmp_path / "write-dir", tmp_path)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    sid = isolated_run(st, None, cwd, [tmp_path / "write-dir"])
    assert cli.main(["wait", sid, "--json", "--outcome-file", str(link / "w.json")]) == 15
    out = json.loads(capsys.readouterr().out)
    assert outcomefile.REJECTED in out["warnings"]
    assert list(victim.iterdir()) == []


def test_wait_on_an_isolated_run_without_stored_paths_writes_nothing(
    st: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that ended before launch step 5 knew its paths: fail closed."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    out = tmp_path / "safe" / "w.json"
    out.parent.mkdir()
    sid = isolated_run(st, None, cwd, None)
    assert cli.main(["wait", sid, "--json", "--outcome-file", str(out)]) == 15
    assert outcomefile.UNDETERMINED in json.loads(capsys.readouterr().out)["warnings"]
    assert not out.exists()


def test_a_session_forgotten_while_waiting_never_writes_through_its_paths(
    st: Store,
    state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The not_found envelope of a session forgotten mid-wait is checked against the bound
    run's paths, not just the baseline."""
    link, victim = victim_link(tmp_path / "write-dir", tmp_path)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    sid = allocate(st, None, resolved=ISOLATED)
    st.update_run(sid, 1, cwd=str(cwd), writable_json=json.dumps([str(link.parent)]))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("CSESSION_POLL_INTERVAL", "0.01s")
    calls: list[str] = []

    def liveness(_st: Store, _cfg: Any, csession_id: str, **_kw: Any) -> Checked:
        calls.append(csession_id)
        if len(calls) == 2:  # after the first poll: the run is bound
            with store.open_state() as other:
                other.conn.execute(
                    "UPDATE sessions SET forgotten_at = '2026-01-01T00:00:00Z' "
                    "WHERE csession_id = ?",
                    (csession_id,),
                )
        return Checked()

    # The check wait runs at every poll (cli passes its own, with the config).
    monkeypatch.setattr(liveness_module, "check_session", liveness)
    assert cli.main(["wait", sid, "--json", "--outcome-file", str(link / "w.json")]) == 10
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == "not_found"
    assert outcomefile.REJECTED in captured.err
    assert list(victim.iterdir()) == []


def test_a_session_deleted_right_after_wait_returned_keeps_the_exit_code(
    st: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """wait checks its outcome file against the row it bound, never a second read that can
    fail once the session is gone."""
    sid = final_run(st, None)
    out = tmp_path / "w.json"
    real = waiter.wait_for_run

    def then_deleted(*args: Any, **kwargs: Any) -> waiter.Waited:
        waited = real(*args, **kwargs)
        with store.open_state() as other:
            other.conn.execute("DELETE FROM runs WHERE csession_id = ?", (sid,))
            other.conn.execute("DELETE FROM sessions WHERE csession_id = ?", (sid,))
        return waited

    monkeypatch.setattr(waiter, "wait_for_run", then_deleted)
    assert cli.main(["wait", sid, "--json", "--outcome-file", str(out)]) == 15
    assert json.loads(out.read_text())["exit_code"] == 15


def test_wait_before_binding_refuses_its_own_cwd(
    st: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown id: the run (and so its isolation) is unknown, so the envelope is not
    written under this process's cwd (here through a symlink planted there)."""
    cwd = tmp_path / "cwd"
    link, victim = victim_link(cwd, tmp_path)
    monkeypatch.chdir(cwd)
    assert cli.main(["wait", "cs-zzzzzzzz", "--outcome-file", "link/w.json"]) == 10
    assert outcomefile.REJECTED in capsys.readouterr().err
    assert list(victim.iterdir()) == []


# Error envelopes before the kind is known (exec, start, resume)


@pytest.mark.parametrize(
    "bad", [["--bogus-flag"], ["--timeout", "0"]], ids=["parse-error", "flag-rule"]
)
def test_an_early_usage_error_never_writes_through_the_cwd(
    bad: list[str],
    st: Store,
    state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The kind (respond: isolated, its agent writes the caller's cwd) is not resolved yet:
    the envelope is not written through the cwd, where the agent could have planted a
    symlink; a warning on stderr, exit 2."""
    cwd = tmp_path / "repo"
    link, victim = victim_link(cwd, tmp_path)
    monkeypatch.chdir(cwd)
    argv = ["exec", "--kind", "respond", "--mode", "headless", "--outcome-file", "link/out.json"]
    assert cli.main([*argv, *bad]) == 2
    assert outcomefile.REJECTED in capsys.readouterr().err
    assert list(victim.iterdir()) == []


@pytest.mark.parametrize("bad", [["--bogus-flag"], ["--timeout", "0"]])
def test_an_early_usage_error_never_writes_through_the_given_cwd(
    bad: list[str],
    st: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    link, victim = victim_link(repo, tmp_path)
    monkeypatch.chdir(tmp_path)
    argv = ["exec", "--kind", "respond", "--cwd", "repo", "--outcome-file", str(link / "o.json")]
    assert cli.main([*argv, *bad]) == 2
    assert outcomefile.REJECTED in capsys.readouterr().err
    assert list(victim.iterdir()) == []


def test_the_outcome_file_path_is_joined_to_the_cwd_not_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``a/link/../x`` names the file next to the link's target, as the write resolves it,
    never ``a/x``: the path stored with the run is the one written."""
    from .test_launch import args, load

    (tmp_path / "a").mkdir()
    (tmp_path / "elsewhere" / "deep").mkdir(parents=True)
    (tmp_path / "a" / "link").symlink_to(tmp_path / "elsewhere" / "deep")
    monkeypatch.chdir(tmp_path)
    req = launch.read_request(args(outcome_file="a/link/../x", mode="headless"), load(tmp_path))
    assert req.outcome_file == os.path.join(str(tmp_path), "a/link/../x")
    assert os.path.realpath(str(req.outcome_file)) == str(tmp_path / "elsewhere" / "x")


# A launch that fails before step 5 knows every session-writable path (isolated runs)


def _early_failure(tmp_path: Path, failure: str) -> tuple[Path, dict[str, str]]:
    """A cwd whose isolated launch fails at the cwd check (inside a hidden home directory:
    before the profile is read) or at git (a broken ``.git``: after it), and the env."""
    home = tmp_path / "home"
    if failure == "cwd":
        cwd = home / ".config" / "proj"
        cwd.mkdir(parents=True)
    else:
        cwd = tmp_path / "work"
        cwd.mkdir()
        (cwd / ".git").write_text("not a gitdir line\n", encoding="utf-8")
    return cwd, {"HOME": str(home)}


@pytest.mark.parametrize("failure", ["cwd", "git"])
def test_an_early_launch_failure_never_writes_through_a_write_dir(
    failure: str, tmp_path: Path, state: Path
) -> None:
    """The outcome file is under a literal ``write_dirs`` entry, through a symlink planted
    there: the launch fails before step 5's outcome-file check, and the end still refuses the
    write_dirs (the launch's guard), so the victim is untouched."""
    link, victim = victim_link(tmp_path / "write-dir", tmp_path)
    cwd, env = _early_failure(tmp_path, failure)
    config = f"poll_interval: 0.05s\nkinds:\n  implement:\n    write_dirs: ['{link.parent}']\n"
    run = run_exec(
        tmp_path,
        state,
        cwd,
        [START, result_step()],
        args=[*ISOLATED_FLAGS, "--outcome-file", str(link / "out.json")],
        config=config,
        env=env,
    )
    assert run.code == 12, run.proc.stderr
    assert run.outcome["end_reason"] == "unsafe_cwd"
    assert outcomefile.REJECTED in run.outcome["warnings"]
    assert list(victim.iterdir()) == []


def test_an_early_launch_failure_with_an_unexpandable_entry_writes_nothing(
    tmp_path: Path, state: Path
) -> None:
    """A write_dirs entry that needs ${GIT_DIR}, unknown when git fails: the session-writable
    paths cannot be determined, so the file is not written (fail closed), with a warning."""
    cwd, env = _early_failure(tmp_path, "git")
    out = tmp_path / "safe" / "out.json"
    out.parent.mkdir()
    config = "poll_interval: 0.05s\nkinds:\n  implement:\n    write_dirs: ['${GIT_DIR}/x']\n"
    run = run_exec(
        tmp_path,
        state,
        cwd,
        [START, result_step()],
        args=[*ISOLATED_FLAGS, "--outcome-file", str(out)],
        config=config,
        env=env,
    )
    assert run.code == 12, run.proc.stderr
    assert outcomefile.UNDETERMINED in run.outcome["warnings"]
    assert [r["message"] for r in failures(state)] == [outcomefile.UNDETERMINED]
    assert not out.exists()
