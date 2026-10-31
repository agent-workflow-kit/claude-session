"""``--outcome-file`` (interface.md, Outcome file; design.md, Data model): the finalizer's write
right after the finalization compare-and-set, exactly once, the backfill, write failures as
warnings, rejected paths never written, mode 0600 and no symlink followed.

The writers run against a real store; the ``exec`` tests start the real entry point with the
fake ``claude``."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_session import cli, ids, launch, outcomefile, pathrules, store, supervisor
from claude_session.errors import LaunchValidationError, OutcomeFileRejected
from claude_session.store import Final, Holder, Store

from .schemas import validate
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
    isolated = final_run(st, cwd / "a.json", resolved=ISOLATED)
    st.update_run(isolated, 1, cwd=str(cwd))
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
    sid = final_run(st, cwd / "link" / "out.json", resolved=ISOLATED)
    st.update_run(sid, 1, cwd=str(cwd))
    assert outcomefile.write_run(st, sid, 1) == outcomefile.REJECTED
    assert list(safe.iterdir()) == []


def test_the_full_writable_set_is_checked_when_given(st: Store, tmp_path: Path) -> None:
    write_dir = tmp_path / "write-dir"
    write_dir.mkdir()
    sid = final_run(st, write_dir / "out.json", resolved=ISOLATED)
    writable = pathrules.SessionWritable([str(write_dir)])
    assert outcomefile.write_run(st, sid, 1, extra=writable) == outcomefile.REJECTED
    assert not (write_dir / "out.json").exists()


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


def test_a_marking_failure_after_the_write_is_contained(
    st: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out.json"
    sid = final_run(st, out)

    def busy(*_args: Any, **_kwargs: Any) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(st, "mark_outcome_file_written", busy)
    assert outcomefile.write_run(st, sid, 1) is not None  # a warning, never an exception
    assert out.exists()


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


def test_step_5_drops_a_rejected_path_and_fails_the_launch(
    st: Store, state: Path, tmp_path: Path
) -> None:
    write_dir = tmp_path / "write-dir"
    write_dir.mkdir()
    out = write_dir / "out.json"
    sid = allocate(st, out, resolved=ISOLATED)
    a = launch.Allocated(sid, 1, str(state / "sessions" / sid), str(state / "private" / sid), ())
    writable = pathrules.SessionWritable([str(write_dir)])
    launch.check_outcome_path(st, a, str(tmp_path / "ok.json"), writable)
    launch.check_outcome_path(st, a, str(out), None)  # trusted: nothing session-writable
    with pytest.raises(launch.UnsafeOutcomeFile) as info:
        launch.check_outcome_path(st, a, str(out), writable)
    assert info.value.end_reason == "unsafe_path"
    assert st.run_record(sid, 1)["outcome_file"] is None
    proc = _process(state, tmp_path)
    launch.end_launch(st, a, None, None, proc, info.value)
    row = st.run_record(sid, 1)
    assert (row["outcome"], row["end_reason"]) == ("not-started", "unsafe_path")
    assert not out.exists()


def test_the_launch_end_never_writes_the_rejected_path_even_if_not_dropped(
    st: Store, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_dir = tmp_path / "write-dir"
    write_dir.mkdir()
    out = write_dir / "out.json"
    sid = allocate(st, out, resolved=ISOLATED)
    a = launch.Allocated(sid, 1, str(state / "sessions" / sid), str(state / "private" / sid), ())

    def busy(*_args: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(st, "drop_outcome_file", busy)
    writable = pathrules.SessionWritable([str(write_dir)])
    with pytest.raises(launch.UnsafeOutcomeFile) as info:
        launch.check_outcome_path(st, a, str(out), writable)
    launch.end_launch(st, a, None, None, _process(state, tmp_path), info.value)
    assert not out.exists()
    assert written_at(st, sid) is not None  # rejected: recorded, never retried


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
    cli._write_outcome_file(str(out), OutcomeFileRejected("rejected"))
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
    sid = final_run(st, None, resolved=ISOLATED)
    st.update_run(sid, 1, cwd=str(cwd))
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
