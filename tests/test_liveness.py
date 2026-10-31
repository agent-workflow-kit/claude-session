"""The liveness check and ``reconcile`` (interface.md, Liveness and reconcile; design.md,
Reconcile, salvage and retention).

Wrappers and children are real processes: a live one is a ``sleep`` this test started (its
PID and procinfo start token), a dead one a process that already exited (its token recorded
while it ran). Launches that crash are real ``claude-session`` processes killed (SIGKILL by
themselves, at an injected point) at the step under test."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from claude_session import (
    cli,
    ids,
    lifecycle,
    liveness,
    reconcile,
    sessiondir,
    store,
    tmpdir,
    toolhooks,
)
from claude_session.gateway import SystemGateway
from claude_session.store import Final, Store

from .fakeclaude import claude_bin, write_json
from .schemas import validate
from .test_exec_headless import START, claude_session_cmd, private_home, result_step
from .test_store_runs import new_run

RECONCILE_SCHEMA = "claude-session/cli/reconcile.json"
# A trusted run (its outcome file is checked against the baseline only).
TRUSTED = {"kind": "implement", "profile": {"sandbox": "off", "reads_third_party": False}}
PROMPT = ("--prompt-file", "prompt.md")  # in ``work``, the cwd of every launch
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


# Real processes


class Procs:
    """Processes this test started; every one still running is ended (its exact PID) and
    reaped at teardown."""

    def __init__(self) -> None:
        self.started: list[subprocess.Popen[bytes]] = []

    def live(self, *, new_session: bool = False) -> tuple[int, str]:
        proc = subprocess.Popen(SLEEPER, start_new_session=new_session)
        self.started.append(proc)
        token = wait_token(proc.pid)
        return proc.pid, token

    def dead(self) -> tuple[int, str]:
        """A process that ran and exited (reaped): its PID and the token it had."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
        )
        token = wait_token(proc.pid)
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=30)
        return proc.pid, token

    def running(self, pid: int) -> bool:
        proc = next(p for p in self.started if p.pid == pid)
        return proc.poll() is None

    def close(self) -> None:
        for proc in self.started:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)


def wait_token(pid: int) -> str:
    deadline = time.monotonic() + 10
    while True:
        token = store.start_time(pid)
        if token:
            return token
        assert time.monotonic() < deadline
        time.sleep(0.01)


@pytest.fixture
def procs() -> Iterator[Procs]:
    p = Procs()
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def st() -> Iterator[Store]:
    with store.open_state() as s:
        yield s


@pytest.fixture
def root(st: Store) -> str:
    return os.path.dirname(st.path)


def session(
    st: Store,
    root: str,
    *,
    wrapper: tuple[int, str] | None,
    child: tuple[int, str] | None = None,
    mode: str = "headless",
    **fields: Any,
) -> str:
    """A session with run 1 as described: ``wrapper`` None makes it pending (``fields`` may
    set ``expires_at``), ``child`` a spawned one (phase ``session``)."""
    me = os.getpid()
    sid = st.allocate(
        new_run(state_root=root, mode=mode, wrapper_pid=me, wrapper_start="x", resolved=TRUSTED),
        new_id=ids.new_session_id,
    )
    values: dict[str, Any] = {"wrapper_pid": None, "wrapper_start": None}
    if wrapper is not None:
        values = {"wrapper_pid": wrapper[0], "wrapper_start": wrapper[1]}
    if child is not None:
        values.update(child_pid=child[0], child_start=child[1], phase="session")
    values.update(fields)
    columns = ", ".join(f"{name} = ?" for name in values)
    st.conn.execute(
        f"UPDATE runs SET {columns} WHERE csession_id = ? AND run_no = 1", (*values.values(), sid)
    )
    return sid


def run_row(st: Store, sid: str, run: int = 1) -> dict[str, Any]:
    return st.run_record(sid, run)


def final_events(st: Store, sid: str) -> list[tuple[Any, ...]]:
    return st.conn.execute(
        "SELECT run_no, cause, to_state FROM events WHERE csession_id = ? AND final = 1",
        (sid,),
    ).fetchall()


def write_private(root: str, sid: str, run: int, name: str, data: bytes = b"x") -> Path:
    with sessiondir.SessionDirs.open(root, sid) as dirs:
        dirs.prepare_run(run)
        dirs.write_private_run_file(run, name, data)
    return Path(sessiondir.private_dir(root, sid)) / "runs" / str(run) / name


def cfg_of(tmp_path: Path, text: str = "") -> Any:
    from claude_session import config

    path = tmp_path / "config.yaml"
    path.write_text("kill_grace: 2s\n" + text, encoding="utf-8")
    return config.load(str(path))


# classify: who is alive


def test_a_live_wrapper_is_never_finalized(st: Store, root: str, procs: Procs) -> None:
    sid = session(st, root, wrapper=procs.live())
    checked = liveness.check_session(st, None, sid)
    assert checked.changes == [] and run_row(st, sid)["ended_at"] is None


def test_a_reused_pid_with_another_token_is_dead(st: Store, root: str, procs: Procs) -> None:
    """PID reuse: the recorded PID runs, but it is another process (another start token)."""
    pid, _token = procs.live()
    sid = session(st, root, wrapper=(pid, "0:1"))
    assert liveness.classify(st, run_row(st, sid)) == liveness.LAUNCH_LOST
    liveness.check_session(st, None, sid)
    row = run_row(st, sid)
    assert (row["outcome"], row["end_reason"]) == ("not-started", "launch_interrupted")
    assert procs.running(pid)  # nothing was signaled


def test_an_empty_token_is_never_a_match_and_keeps_an_existing_pid_live(
    st: Store, root: str, procs: Procs
) -> None:
    pid, _token = procs.live()
    sid = session(st, root, wrapper=(pid, ""))
    assert liveness.classify(st, run_row(st, sid)) == liveness.LIVE
    assert liveness.check_session(st, None, sid).changes == []


def test_an_empty_token_of_a_gone_pid_is_dead(st: Store, root: str, procs: Procs) -> None:
    pid, _ = procs.dead()
    sid = session(st, root, wrapper=(pid, ""))
    if store.pid_exists(pid):  # reused already: the conservative reading keeps it live
        pytest.skip("the dead process's PID was reused")
    assert liveness.classify(st, run_row(st, sid)) == liveness.LAUNCH_LOST


@pytest.mark.parametrize("pid", [0, -1, 2**31, 2**63 - 1])
def test_impossible_pids_are_not_alive(st: Store, pid: int) -> None:
    assert not liveness.process_alive(st, pid, "boot:1")
    assert not liveness.process_alive(st, pid, "")


def test_an_unanswerable_check_counts_as_alive(root: str, procs: Procs) -> None:
    """``Store.liveness`` answers alive when the platform cannot tell: never finalized."""
    with Store.open(os.path.join(root, "state.db"), liveness=lambda _p, _s: True) as s:
        sid = session(s, root, wrapper=procs.dead())
        assert liveness.classify(s, run_row(s, sid)) == liveness.LIVE


# Pending runs


def test_an_expired_pending_run_ends_never_adopted_with_its_spec_and_outcome_file(
    st: Store, root: str, tmp_path: Path
) -> None:
    out = tmp_path / "outcome.json"
    sid = session(st, root, wrapper=None, expires_at="2000-01-01T00:00:00.000000Z")
    st.conn.execute("UPDATE runs SET outcome_file = ? WHERE csession_id = ?", (str(out), sid))
    spec = write_private(root, sid, 1, lifecycle.SPEC_NAME)
    checked = liveness.check_session(st, None, sid)
    assert [(c.action, c.end_reason) for c in checked.changes] == [
        ("marked_not_started", "never_adopted")
    ]
    assert final_events(st, sid) == [(1, "liveness", "not-started")]
    assert not spec.exists()
    assert json.loads(out.read_text())["end_reason"] == "never_adopted"


def test_a_pending_run_before_its_deadline_stays(st: Store, root: str) -> None:
    sid = session(st, root, wrapper=None, expires_at="2999-01-01T00:00:00.000000Z")
    assert liveness.check_session(st, None, sid).changes == []


@pytest.mark.parametrize("expires", [None, "", "garbage", "2000-01-01"])
def test_a_pending_run_with_an_unusable_deadline_is_expired(
    st: Store, root: str, expires: str | None
) -> None:
    sid = session(st, root, wrapper=None, expires_at=expires)
    liveness.check_session(st, None, sid)
    assert run_row(st, sid)["end_reason"] == "never_adopted"


# Wrapper dead, no child: the launch phase


def test_a_wrapper_dead_before_the_spawn_ends_launch_interrupted_and_cleans_up(
    st: Store, root: str, procs: Procs, tmp_path: Path
) -> None:
    out = tmp_path / "outcome.json"
    sid = session(st, root, wrapper=procs.dead(), outcome_file=str(out))
    spec = write_private(root, sid, 1, lifecycle.SPEC_NAME)
    index = write_private(root, sid, 1, "secret-index")
    tmp = Path(tmpdir.create(sid))
    checked = liveness.check_session(st, None, sid, cause="reconcile")
    assert [(c.action, c.outcome, c.end_reason) for c in checked.changes] == [
        ("marked_not_started", "not-started", "launch_interrupted")
    ]
    row = run_row(st, sid)
    assert row["phase"] == "launch" and row["message"] == liveness.LAUNCH_LOST_MESSAGE
    assert final_events(st, sid) == [(1, "reconcile", "not-started")]
    assert not spec.exists() and not index.exists() and not tmp.exists()
    assert json.loads(out.read_text())["end_reason"] == "launch_interrupted"


def test_a_check_finalizes_a_run_exactly_once(st: Store, root: str, procs: Procs) -> None:
    sid = session(st, root, wrapper=procs.dead())
    first = liveness.check_session(st, None, sid)
    second = liveness.check_session(st, None, sid)
    assert len(first.changes) == 1 and second.changes == []
    assert len(final_events(st, sid)) == 1


def test_a_run_that_changed_hands_after_the_check_is_never_finalized(
    st: Store, root: str, procs: Procs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check found the wrapper dead; meanwhile the row names another (live) wrapper: the
    compare-and-set requires the dead one and changes nothing."""
    sid = session(st, root, wrapper=procs.dead())
    live = procs.live()
    real = liveness.classify

    def classify_then_change(s: Store, row: dict[str, Any]) -> str:
        verdict = real(s, row)
        s.conn.execute(
            "UPDATE runs SET wrapper_pid = ?, wrapper_start = ? WHERE csession_id = ?",
            (*live, sid),
        )
        return verdict

    monkeypatch.setattr(liveness, "classify", classify_then_change)
    assert liveness.check_session(st, None, sid).changes == []
    assert run_row(st, sid)["ended_at"] is None


# Wrapper dead, child alive: an orphan


def test_a_dead_wrapper_with_a_live_child_is_an_orphan_and_never_signaled(
    st: Store, root: str, procs: Procs
) -> None:
    child = procs.live()
    sid = session(st, root, wrapper=procs.dead(), child=child)
    checked = liveness.check_session(st, None, sid)
    assert checked.changes == []
    assert checked.orphans == [{"csession_id": sid, "run": 1, "child_pid": child[0]}]
    assert run_row(st, sid)["ended_at"] is None and procs.running(child[0])


# Wrapper and child dead: salvage


def salvaged(
    st: Store, root: str, procs: Procs, *, report: str | None = None, **fields: Any
) -> str:
    sid = session(st, root, wrapper=procs.dead(), child=procs.dead(), **fields)
    if report is not None:
        st.conn.execute(
            "UPDATE runs SET report_status = ?, report_implicit = 0, report_summary = 's', "
            "reported_at = '2026-01-01T00:00:00Z' WHERE csession_id = ?",
            (report, sid),
        )
    return sid


def test_salvage_without_anything_is_lost(st: Store, root: str, procs: Procs) -> None:
    sid = salvaged(st, root, procs)
    checked = liveness.check_session(st, None, sid)
    assert [(c.action, c.outcome, c.end_reason, c.to_state) for c in checked.changes] == [
        ("marked_lost", "lost", "lost", "lost")
    ]
    assert final_events(st, sid) == [(1, "liveness", "lost")]


def test_salvage_with_a_report_is_the_reports_outcome_wrapper_lost(
    st: Store, root: str, procs: Procs
) -> None:
    sid = salvaged(st, root, procs, report="blocked")
    (change,) = liveness.check_session(st, None, sid).changes
    assert (change.action, change.outcome, change.end_reason, change.to_state) == (
        "marked_exited_wrapper_lost",
        "blocked",
        "wrapper_lost",
        "exited",
    )


@pytest.mark.parametrize(
    ("cause", "outcome", "event_cause"),
    [
        ("timeout", "timeout", "timeout"),
        ("stopped", "killed", "liveness"),
        ("internal_error", "killed", "liveness"),
        ("forwarded_term", "killed", "liveness"),
    ],
)
def test_salvage_with_a_pending_cause_uses_the_cause_table(
    st: Store, root: str, procs: Procs, cause: str, outcome: str, event_cause: str
) -> None:
    sid = salvaged(st, root, procs, pending_cause=cause)
    (change,) = liveness.check_session(st, None, sid).changes
    assert (change.action, change.outcome, change.end_reason) == (
        "finalized_from_cause",
        outcome,
        cause,
    )
    assert final_events(st, sid)[0][1] == event_cause


def test_salvage_maps_a_complete_headless_result(st: Store, root: str, procs: Procs) -> None:
    sid = salvaged(st, root, procs, resolved_json=json.dumps({"profile": {}}))
    result = {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "r"}
    write_private(root, sid, 1, "result.json", (json.dumps(result) + "\n").encode())
    (change,) = liveness.check_session(st, None, sid).changes
    assert (change.action, change.outcome, change.end_reason) == (
        "finalized_from_result",
        "timeout",
        "max_turns",
    )


def test_salvage_of_an_implicit_done_without_a_verified_secret_is_rejected(
    st: Store, root: str, procs: Procs, tmp_path: Path
) -> None:
    """The run secret cannot be checked (no secret file matching the stored digest): the
    implicit done is rejected, never accepted on a guess."""
    sid = salvaged(st, root, procs, resolved_json=json.dumps({"profile": {"implicit_done": True}}))
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
    write_private(root, sid, 1, "result.json", (json.dumps(result) + "\n").encode())
    (change,) = liveness.check_session(st, cfg_of(tmp_path), sid).changes
    assert (change.outcome, change.end_reason) == ("exited-unreported", "result_rejected")


def test_salvage_deletes_the_secret_index_and_writes_the_outcome_file(
    st: Store, root: str, procs: Procs, tmp_path: Path
) -> None:
    out = tmp_path / "o.json"
    sid = salvaged(st, root, procs, outcome_file=str(out))
    index = write_private(root, sid, 1, "secret-index")
    liveness.check_session(st, None, sid)
    assert not index.exists()
    assert json.loads(out.read_text())["outcome"] == "lost"


def test_a_salvage_another_process_holds_is_left_to_it(st: Store, root: str, procs: Procs) -> None:
    sid = salvaged(st, root, procs)
    holder = procs.live()
    with Store.open(st.path, holder=store.Holder(*holder)) as other:
        assert other.take_lease(f"salvage:{sid}:1")
    assert liveness.check_session(st, None, sid).changes == []
    assert run_row(st, sid)["ended_at"] is None


def test_concurrent_checks_finalize_once(root: str, procs: Procs) -> None:
    with store.open_state() as st:
        sids = [salvaged(st, root, procs) for _ in range(5)]
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def check() -> None:
        try:
            with store.open_state() as own:
                barrier.wait(10)
                for sid in sids:
                    liveness.check_session(own, None, sid, patience=5)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=check) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []
    with store.open_state() as st:
        for sid in sids:
            assert final_events(st, sid) == [(1, "liveness", "lost")]


def test_a_superseded_open_run_is_finalized_without_touching_the_current_run(
    st: Store, root: str, procs: Procs
) -> None:
    sid = salvaged(st, root, procs)
    live = procs.live()
    st.conn.execute(
        "INSERT INTO runs (csession_id, run_no, created_at, run_kind, wrapper_pid, wrapper_start)"
        " VALUES (?, 2, '2026-01-01T00:00:00Z', 'implement', ?, ?)",
        (sid, *live),
    )
    st.conn.execute(
        "UPDATE sessions SET current_run = 2, state = 'working' WHERE csession_id = ?", (sid,)
    )
    liveness.check_session(st, None, sid)
    assert run_row(st, sid, 1)["outcome"] == "lost"
    assert run_row(st, sid, 2)["ended_at"] is None
    state = st.conn.execute("SELECT state FROM sessions WHERE csession_id = ?", (sid,)).fetchone()
    assert state == ("working",)


def test_a_dry_run_changes_nothing(st: Store, root: str, procs: Procs) -> None:
    sid = salvaged(st, root, procs, report="done")
    (change,) = liveness.check_session(st, None, sid, dry_run=True).changes
    assert (change.outcome, change.end_reason, change.detail) == (
        "done",
        "wrapper_lost",
        "dry run: not applied",
    )
    assert run_row(st, sid)["ended_at"] is None


# Real crashes (an injected SIGKILL of claude-session itself)


def crashing_cmd(patch: str) -> list[str]:
    command = claude_session_cmd()
    return [*command[:-1], patch + "\n" + command[-1]]


DIE = "import os, signal\ndef _die(*a, **k):\n    os.kill(os.getpid(), signal.SIGKILL)\n"


def crash(tmp_path: Path, work: Path, argv: Sequence[str], patch: str) -> None:
    proc = subprocess.run(
        [*crashing_cmd(DIE + patch), *argv],
        cwd=work,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    (path / "prompt.md").write_text("Do the task.\n", encoding="utf-8")
    return path


def fake_config(tmp_path: Path, steps: list[dict[str, Any]], extra: str = "") -> Path:
    scenario = write_json(tmp_path / "scenario.json", {"steps": steps})
    fake = claude_bin(tmp_path / "fakebin", scenario, tmp_path / "fake.jsonl")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {fake}\npoll_interval: 0.05s\n{extra}", encoding="utf-8")
    return cfg


def reconcile_json(tmp_path: Path, *extra: str) -> dict[str, Any]:
    cfg = tmp_path / "config.yaml"
    if not cfg.exists():
        cfg.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [*claude_session_cmd(), "reconcile", "--json", "--config", str(cfg), *extra],
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    validate(out, RECONCILE_SCHEMA)
    return out


def only_session() -> str:
    with store.open_state() as st:
        (sid,) = st.session_ids()
    return sid


def test_an_exec_that_crashed_mid_launch_is_ended_by_reconcile(tmp_path: Path, work: Path) -> None:
    cfg = fake_config(tmp_path, [START, result_step()])
    patch = "from claude_session import launch\nlaunch.prepare = _die\n"
    crash(tmp_path, work, ["exec", "--mode", "headless", "--config", str(cfg), *PROMPT], patch)
    sid = only_session()
    out = reconcile_json(tmp_path)
    assert [(c["csession_id"], c["action"], c["end_reason"]) for c in out["changes"]][:1] == [
        (sid, "marked_not_started", "launch_interrupted")
    ]
    with store.open_state() as st:
        assert final_events(st, sid) == [(1, "reconcile", "not-started")]


def test_a_start_that_died_before_release_is_ended_and_its_spec_deleted(
    tmp_path: Path, work: Path
) -> None:
    cfg = fake_config(tmp_path, [START, result_step()])
    patch = "from claude_session import store\nstore.Store.release_pending = _die\n"
    crash(
        tmp_path,
        work,
        ["start", "--json", "--mode", "headless", "--config", str(cfg), *PROMPT],
        patch,
    )
    sid = only_session()
    with store.open_state() as st:
        root = os.path.dirname(st.path)
    spec = Path(sessiondir.private_dir(root, sid)) / "runs" / "1" / lifecycle.SPEC_NAME
    assert spec.exists()
    out = reconcile_json(tmp_path)
    assert out["changes"][0]["end_reason"] == "launch_interrupted"
    assert not spec.exists()


def test_an_adopter_that_crashed_after_the_compare_and_set_is_ended(
    tmp_path: Path, work: Path
) -> None:
    cfg = fake_config(tmp_path, [START, result_step()])
    started = subprocess.run(
        [
            *claude_session_cmd(),
            "start",
            "--json",
            "--mode",
            "headless",
            "--config",
            str(cfg),
            *PROMPT,
        ],
        cwd=work,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    sid = json.loads(started.stdout)["csession_id"]
    patch = "from claude_session import lifecycle\nlifecycle.load_adoption = _die\n"
    crash(tmp_path, work, ["exec", "--session-id", sid], patch)
    with store.open_state() as st:
        row = st.run_record(sid, 1)
        root = os.path.dirname(st.path)
        assert row["wrapper_pid"] is not None and row["child_pid"] is None
        spec = Path(sessiondir.private_dir(root, sid)) / "runs" / "1" / lifecycle.SPEC_NAME
        assert spec.exists()
        (change,) = liveness.check_session(st, None, sid).changes
    assert (change.outcome, change.end_reason) == ("not-started", "launch_interrupted")
    assert not spec.exists()


def test_a_killed_exec_whose_child_died_is_salvaged_and_its_report_ingested(
    tmp_path: Path, work: Path
) -> None:
    """exec SIGKILLed while claude runs: the headless child dies with it (parent-death
    signal); the report it filed was never drained (no PostToolUse): salvage ingests it."""
    marker = tmp_path / "reported"
    steps = [
        START,
        {"do": "report", "args": ["done", "--summary", "s"], "post_tool_use": "never"},
        {"do": "touch", "path": str(marker)},
        {"do": "hang"},
    ]
    cfg = fake_config(tmp_path, steps)
    proc = subprocess.Popen(
        [*claude_session_cmd(), "exec", "--mode", "headless", "--config", str(cfg), *PROMPT],
        cwd=work,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        proc.kill()  # exact PID: the exec this test started
        proc.wait(timeout=30)
    sid = only_session()
    with store.open_state() as st:
        child = st.run_record(sid, 1)["child_pid"]
    deadline = time.monotonic() + 30
    while store.pid_exists(child) and store.start_time(child):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    out = reconcile_json(tmp_path)
    actions = [(c["action"], c["outcome"], c["end_reason"]) for c in out["changes"]]
    assert ("outbox_ingested", None, None) in actions
    assert ("marked_exited_wrapper_lost", "done", "wrapper_lost") in actions
    with store.open_state() as st:
        assert st.run_record(sid, 1)["report_summary"] == "s"


# reconcile


def test_reconcile_json_output_validates_and_lists_orphans(tmp_path: Path, procs: Procs) -> None:
    with store.open_state() as st:
        root = os.path.dirname(st.path)
        child = procs.live()
        orphan = session(st, root, wrapper=procs.dead(), child=child)
        lost = salvaged(st, root, procs)
    out = reconcile_json(tmp_path)
    assert out["orphans"] == [{"csession_id": orphan, "run": 1, "child_pid": child[0]}]
    assert (lost, "marked_lost") in [(c["csession_id"], c["action"]) for c in out["changes"]]
    assert procs.running(child[0])


def test_reconcile_dry_run_changes_nothing(tmp_path: Path, procs: Procs) -> None:
    with store.open_state() as st:
        root = os.path.dirname(st.path)
        sid = salvaged(st, root, procs)
    out = reconcile_json(tmp_path, "--dry-run")
    assert out["dry_run"] is True and out["changes"][0]["detail"] == reconcile.DRY
    with store.open_state() as st:
        assert st.run_record(sid, 1)["ended_at"] is None


def test_a_second_reconcile_reports_already_running(tmp_path: Path, procs: Procs) -> None:
    holder = procs.live()
    with store.open_state() as st:
        with Store.open(st.path, holder=store.Holder(*holder)) as other:
            assert other.take_lease(reconcile.LEASE)
    out = reconcile_json(tmp_path)
    assert out["already_running"] is True and out["changes"] == []


def test_concurrent_reconciles_finalize_each_run_once(tmp_path: Path, procs: Procs) -> None:
    with store.open_state() as st:
        root = os.path.dirname(st.path)
        sids = [salvaged(st, root, procs) for _ in range(6)]
    cfg = tmp_path / "config.yaml"
    cfg.write_text("", encoding="utf-8")
    runners = [
        subprocess.Popen(
            [*claude_session_cmd(), "reconcile", "--json", "--config", str(cfg)],
            env={**os.environ, **private_home(tmp_path)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    outs = []
    for runner in runners:
        stdout, _ = runner.communicate(timeout=120)
        assert runner.returncode == 0
        outs.append(json.loads(stdout))
    finalized = [
        c["csession_id"] for out in outs for c in out["changes"] if c["action"] == "marked_lost"
    ]
    assert sorted(finalized) == sorted(sids)  # each exactly once, whoever did it
    with store.open_state() as st:
        for sid in sids:
            assert len(final_events(st, sid)) == 1


def test_reconcile_delivers_undelivered_events(tmp_path: Path, procs: Procs) -> None:
    hooked = tmp_path / "hooked.jsonl"
    cfg_text = f"hooks:\n  post-state-change:\n    - cmd: cat >> {hooked}; echo >> {hooked}\n"
    (tmp_path / "config.yaml").write_text(cfg_text, encoding="utf-8")
    with store.open_state() as st:
        root = os.path.dirname(st.path)
        sid = salvaged(st, root, procs)
        liveness.check_session(st, None, sid)  # as status would: finalized, not delivered
    out = reconcile_json(tmp_path)
    assert (sid, "events_delivered") in [(c["csession_id"], c["action"]) for c in out["changes"]]
    payloads = [json.loads(line) for line in hooked.read_text().splitlines() if line]
    assert [p["final"] for p in payloads] == [False, True]
    assert payloads[-1]["cause"] == "liveness"


def test_reconcile_backfills_an_unwritten_outcome_file(
    tmp_path: Path, st: Store, root: str, procs: Procs
) -> None:
    out = tmp_path / "o.json"
    sid = session(st, root, wrapper=procs.live(), outcome_file=str(out))
    st.finalize_run(sid, 1, Final("session", "lost", "lost", 17, "lost", "exit"))
    reconcile.run(st, cfg_of(tmp_path), gateway=SystemGateway(), environ=os.environ)
    assert json.loads(out.read_text())["outcome"] == "lost"


def test_reconcile_discards_the_outbox_of_a_final_run(
    tmp_path: Path, st: Store, root: str, procs: Procs
) -> None:
    sid = session(st, root, wrapper=procs.live())
    st.finalize_run(sid, 1, Final("session", "lost", "lost", 17, "lost", "exit"))
    with sessiondir.SessionDirs.open(root, sid) as dirs:
        dirs.prepare_run(1)
    request = "0190f5e2-0000-7000-8000-000000000001"
    path = Path(sessiondir.session_dir(root, sid)) / "outbox" / f"{request}.json"
    path.write_text("{}", encoding="utf-8")
    result = reconcile.run(st, cfg_of(tmp_path), gateway=SystemGateway(), environ=os.environ)
    assert (sid, "outbox_discarded") in [(c["csession_id"], c["action"]) for c in result["changes"]]
    assert not path.exists()
    assert st.conn.execute("SELECT result FROM requests").fetchall() == [("discarded",)]


def test_reconcile_keeps_the_outbox_of_a_live_run(
    tmp_path: Path, st: Store, root: str, procs: Procs
) -> None:
    sid = session(st, root, wrapper=procs.live())
    with sessiondir.SessionDirs.open(root, sid) as dirs:
        dirs.prepare_run(1)
    path = Path(sessiondir.session_dir(root, sid)) / "outbox"
    request = path / "0190f5e2-0000-7000-8000-000000000001.json"
    request.write_text("{}", encoding="utf-8")
    reconcile.run(st, cfg_of(tmp_path), gateway=SystemGateway(), environ=os.environ)
    assert request.exists()


def test_reconcile_removes_tmpdirs_of_final_and_deleted_sessions_only(
    tmp_path: Path, st: Store, root: str, procs: Procs
) -> None:
    final = session(st, root, wrapper=procs.live())
    st.finalize_run(final, 1, Final("session", "lost", "lost", 17, "lost", "exit"))
    live = session(st, root, wrapper=procs.live())
    deleted = ids.new_session_id(st.session_id_used)
    st.conn.execute("INSERT INTO deleted_ids (csession_id, deleted_at) VALUES (?, 'x')", (deleted,))
    stranger = ids.new_session_id(st.session_id_used)  # another state directory's
    paths = {sid: Path(tmpdir.create(sid)) for sid in (final, live, deleted, stranger)}
    (paths[final] / "inner").mkdir()
    rootdir = Path(tmpdir.root_path())
    stale_final = rootdir / f".stale-{final}-00112233aabbccdd"
    stale_stranger = rootdir / f".stale-{stranger}-00112233aabbccdd"
    stale_final.mkdir()
    stale_stranger.mkdir()
    result = reconcile.run(st, cfg_of(tmp_path), gateway=SystemGateway(), environ=os.environ)
    assert not paths[final].exists() and not paths[deleted].exists()
    assert paths[live].exists() and paths[stranger].exists()
    assert not stale_final.exists() and stale_stranger.exists()
    removed = sorted(c["csession_id"] for c in result["changes"] if c["action"] == "tmpdir_removed")
    assert removed == sorted([final, final, deleted])


def test_a_tmpdir_a_new_run_made_meanwhile_is_put_back(st: Store, root: str, procs: Procs) -> None:
    sid = session(st, root, wrapper=procs.live())
    st.finalize_run(sid, 1, Final("session", "lost", "lost", 17, "lost", "exit"))
    path = Path(tmpdir.create(sid))
    assert not tmpdir.remove_checked(sid, lambda: False)  # the store changed meanwhile
    assert path.exists() and not any(Path(tmpdir.root_path()).glob(".stale-*"))


@pytest.mark.parametrize(
    "name",
    [
        ".stale-cs-aaaaaaaa-",
        ".stale-cs-AAAAAAAA-00",
        ".stale-x-00",
        "cs-aaaaaaaa",
        ".stale-cs-aaaaaaaa-zz",
    ],
)
def test_stale_names_outside_the_grammar_are_not_stale(name: str) -> None:
    assert tmpdir.stale_session(name) is None


# Orphaned post-state-change hook groups


def test_a_dead_deliverers_hook_group_is_ended(st: Store, procs: Procs) -> None:
    leader = procs.live(new_session=True)
    dead = procs.dead()
    st.add_hook_group("cs-aaaaaaaa", 1, leader[0], leader[1], store.Holder(*dead))
    assert toolhooks.end_orphaned_groups(st, None, kill_grace=5) == [leader[0]]
    proc = next(p for p in procs.started if p.pid == leader[0])
    assert proc.wait(timeout=10) == -signal.SIGTERM
    assert st.hook_groups() == []


def test_a_live_deliverers_hook_group_is_left_alone(st: Store, procs: Procs) -> None:
    leader = procs.live(new_session=True)
    st.add_hook_group("cs-aaaaaaaa", 1, leader[0], leader[1], store.Holder(*procs.live()))
    assert toolhooks.end_orphaned_groups(st, None, kill_grace=5) == []
    assert procs.running(leader[0]) and len(st.hook_groups()) == 1


@pytest.mark.parametrize("token", ["", "0:1"])
def test_an_unverified_hook_group_is_dropped_without_a_signal(
    st: Store, procs: Procs, token: str
) -> None:
    leader = procs.live(new_session=True)
    st.add_hook_group("cs-aaaaaaaa", 1, leader[0], token, store.Holder(*procs.dead()))
    assert toolhooks.end_orphaned_groups(st, None, kill_grace=5) == []
    assert procs.running(leader[0]) and st.hook_groups() == []


def test_a_group_that_is_not_its_own_leaders_is_never_signaled(st: Store, procs: Procs) -> None:
    """The recorded PID runs with its token but leads no group of its own (it is in this
    test's group): never signaled, so this test's own group is never hit."""
    member = procs.live(new_session=False)
    st.add_hook_group("cs-aaaaaaaa", 1, member[0], member[1], store.Holder(*procs.dead()))
    assert toolhooks.end_orphaned_groups(st, None, kill_grace=5) == []
    assert procs.running(member[0])


def test_a_hook_group_is_recorded_while_it_runs_and_removed_after(
    tmp_path: Path, st: Store, root: str, procs: Procs
) -> None:
    seen = tmp_path / "seen"
    cmd = f"cat > /dev/null; echo run > {seen}; sleep 0.3"
    cfg = cfg_of(tmp_path, f"hooks:\n  post-state-change:\n    - cmd: {cmd}\n")
    sid = session(st, root, wrapper=procs.dead())
    recorded: list[list[dict[str, Any]]] = []

    def watch() -> None:
        with Store.open(st.path) as own:
            deadline = time.monotonic() + 20
            while not seen.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            recorded.append(own.hook_groups(sid))

    watcher = threading.Thread(target=watch)
    watcher.start()
    toolhooks.deliver(st, cfg, sid, gateway=SystemGateway(), environ=os.environ, state_root=root)
    watcher.join(30)
    assert recorded and len(recorded[0]) == 1 and recorded[0][0]["leader_start"]
    assert st.hook_groups(sid) == []


# The waiting and resuming commands


def test_wait_ends_a_dead_run_and_delivers_its_final_event(tmp_path: Path, procs: Procs) -> None:
    hooked = tmp_path / "hooked.jsonl"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"hooks:\n  post-state-change:\n    - cmd: cat >> {hooked}; echo >> {hooked}\n",
        encoding="utf-8",
    )
    with store.open_state() as st:
        sid = salvaged(st, os.path.dirname(st.path), procs)
    code = cli.main(["wait", sid, "--json", "--config", str(cfg), "--timeout", "30s"])
    assert code == 17
    payloads = [json.loads(line) for line in hooked.read_text().splitlines() if line]
    assert payloads[-1]["final"] is True and payloads[-1]["cause"] == "liveness"


def test_resume_finalizes_a_dead_previous_run_instead_of_refusing_live(
    tmp_path: Path, work: Path, procs: Procs
) -> None:
    cfg = fake_config(tmp_path, [START, {"do": "report", "args": ["done", "--summary", "s"]}])
    first = subprocess.run(
        [
            *claude_session_cmd(),
            "exec",
            "--json",
            "--mode",
            "headless",
            "--config",
            str(cfg),
            *PROMPT,
        ],
        cwd=work,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    sid = json.loads(first.stdout)["csession_id"]
    with store.open_state() as st:
        # The run as a crash left it: final fields cleared, wrapper and child dead.
        dead_wrapper, dead_child = procs.dead(), procs.dead()
        st.conn.execute(
            "UPDATE runs SET ended_at = NULL, outcome = NULL, end_reason = NULL, "
            "exit_code = NULL, wrapper_pid = ?, wrapper_start = ?, child_pid = ?, "
            "child_start = ? WHERE csession_id = ?",
            (*dead_wrapper, *dead_child, sid),
        )
        st.conn.execute("DELETE FROM events WHERE final = 1")
    proc = subprocess.run(
        [*claude_session_cmd(), "resume", sid, "--json", "--config", str(cfg)],
        cwd=work,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["run"] == 2
    with store.open_state() as st:
        row = st.run_record(sid, 1)
        assert (row["outcome"], row["end_reason"]) == ("done", "wrapper_lost")
        assert final_events(st, sid)[0] == (1, "liveness", "exited")


def test_the_database_records_hook_groups_from_schema_1_2(st: Store) -> None:
    assert st.schema_version() >= (1, 2)
    db = sqlite3.connect(st.path)
    try:
        names = {row[1] for row in db.execute("PRAGMA table_info(hook_groups)")}
    finally:
        db.close()
    assert {"pgid", "leader_start", "holder_pid", "holder_start"} <= names
