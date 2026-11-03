"""``post-state-change`` delivery by ``exec`` end to end (interface.md, Hooks: Delivery): the
background worker during the session phase, the final event before ``exec`` exits, and the
synchronous delivery of a launch-phase end. Hooks are ``tests/fakebin/post_hook.py``, which
records its start (see ``test_toolhooks``)."""

from __future__ import annotations

import json
import shlex
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from .fakeclaude import read_jsonl
from .schemas import validate
from .test_exec_headless import (
    FAST,
    START,
    STOP,
    exec_in_process,
    result_step,
    run_exec,
    run_row_of,
    start_hanging,
)
from .test_exec_interactive import FAST as INTERACTIVE_FAST
from .test_exec_interactive import HANG, check_outcome, report, start_terminal

HOOK = Path(__file__).parent / "fakebin" / "post_hook.py"
PAYLOAD_SCHEMA = "claude-session/hooks/post-state-change.json"
LINUX = sys.platform.startswith("linux")


def hook_config(log: Path, *extra: str, entry: str = "") -> str:
    cmd = shlex.join([sys.executable, "-I", str(HOOK), "--log", str(log), *extra])
    return (
        f"poll_interval: 0.05s\nhooks:\n  post-state-change:\n    - cmd: {json.dumps(cmd)}\n{entry}"
    )


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def payloads(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line)["payload"] for line in log.read_text().splitlines()]


def test_exec_delivers_every_event_once_in_order_and_the_final_one_before_it_exits(
    tmp_path: Path, state: Path, work: Path
) -> None:
    log = tmp_path / "hook.jsonl"
    run = run_exec(
        tmp_path,
        state,
        work,
        [
            START,
            {"do": "report", "args": ["done", "--summary", "ok"]},
            STOP,
            result_step(),
        ],
        config=hook_config(log),
    )
    assert run.code == 0, run.proc.stderr
    got = payloads(log)
    events = run.rows("SELECT id, cause, final, delivered_at FROM events ORDER BY id")
    assert [p["event_id"] for p in got] == [row[0] for row in events]
    assert [p["cause"] for p in got] == [row[1] for row in events]
    assert all(row[3] is not None for row in events)  # every event marked delivered
    assert [p["final"] for p in got].count(True) == 1 and got[-1]["final"] is True
    for payload in got:
        validate(payload, PAYLOAD_SCHEMA)
    final = got[-1]
    assert (final["outcome"], final["exit_code"], final["state"]) == ("done", 0, "exited")
    assert final["object_id"] == run.sid and final["phase"] == "session"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert {r["cwd"] for r in records} == {str(state)}  # the state root
    assert {r["env"]["CSESSION_ID"] for r in records} == {run.sid}
    assert run.rows("SELECT COUNT(*) FROM leases WHERE name LIKE 'delivery:%'") == [(0,)]


@pytest.mark.skipif(not LINUX, reason="start ticks come from /proc")
def test_a_slow_hook_never_delays_supervision(tmp_path: Path, state: Path, work: Path) -> None:
    """Every hook blocks until released; meanwhile the poll loop must still forward a SIGTERM
    to claude (a delivery inside the poll loop would sit in the first hook instead)."""
    log = tmp_path / "hook.jsonl"
    release = tmp_path / "release"
    hanging = tmp_path / "hanging"
    steps = [START, {"do": "touch", "path": str(hanging)}, {"do": "hang"}]
    config = hook_config(log, "--wait-for", str(release)).removeprefix(FAST)
    proc = start_hanging(tmp_path, work, steps, hanging, config=config)
    try:
        wait_for(lambda: log.exists() and log.read_text() != "")  # the worker is in a hook
        proc.send_signal(signal.SIGTERM)  # the exact exec this test started
        fake = tmp_path / "fake.jsonl"
        wait_for(lambda: any(r["event"] == "exit" for r in read_jsonl(fake)), 10)
        assert not release.exists()  # claude ended while the first hook still blocked
    finally:
        release.touch()
        out, err = proc.communicate(timeout=60)
    assert proc.returncode == 15, err
    assert json.loads(out)["end_reason"] == "forwarded_term"
    ids = [row[0] for row in run_row_events(state)]
    assert [p["event_id"] for p in payloads(log)] == ids


def test_a_failing_hook_never_changes_the_outcome(tmp_path: Path, state: Path, work: Path) -> None:
    log = tmp_path / "hook.jsonl"
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config=hook_config(log, "--exit", "7"),
    )
    assert run.code == 0 and run.outcome["outcome"] == "done"
    assert run.outcome["warnings"] == []
    logged = [json.loads(line) for line in (state / "log.jsonl").read_text().splitlines()]
    failed = [r for r in logged if r["event"] == "hook_failed"]
    assert failed and all(r["message"].endswith(": exit 7") for r in failed)


def test_states_selects_the_events_a_hook_gets(tmp_path: Path, state: Path, work: Path) -> None:
    log = tmp_path / "hook.jsonl"
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config=hook_config(log, entry="      states: [exited]\n"),
    )
    assert run.code == 0
    (only,) = payloads(log)
    assert only["final"] is True and only["state"] == "exited"


def test_a_launch_phase_end_delivers_launch_and_one_final_event(
    tmp_path: Path, state: Path, work: Path
) -> None:
    log = tmp_path / "hook.jsonl"
    run = run_exec(
        tmp_path,
        state,
        work,
        [],
        config=hook_config(log),
        claude=tmp_path / "missing" / "claude",
    )
    assert run.code == 12 and run.outcome["end_reason"] == "claude_not_found"
    got = payloads(log)
    assert [(p["cause"], p["final"]) for p in got] == [("launch", False), ("launch", True)]
    for payload in got:
        validate(payload, PAYLOAD_SCHEMA)
    assert got[-1]["state"] == "not-started" and got[-1]["phase"] == "launch"


def test_a_per_invocation_hook_is_delivered_too(tmp_path: Path, state: Path, work: Path) -> None:
    log = tmp_path / "hook.jsonl"
    cmd = shlex.join([sys.executable, "-I", str(HOOK), "--log", str(log), "--name", "flag"])
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config="poll_interval: 0.05s\n",
        args=["--hook", f"post-state-change={cmd}", "--hook-timeout", "post-state-change=5s"],
    )
    assert run.code == 0
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert {r["name"] for r in records} == {"flag"}
    assert records[-1]["payload"]["final"] is True


def test_an_interactive_run_with_hooks_still_auto_exits_and_delivers_the_final_event(
    tmp_path: Path, state: Path, work: Path
) -> None:
    # Delivery writes no events (only delivered_at), so it never comes between the idle Stop
    # and the report auto-exit reads (supervisor.auto_exit_candidate).
    log = tmp_path / "hook.jsonl"
    cmd = shlex.join([sys.executable, "-I", str(HOOK), "--log", str(log)])
    term = start_terminal(
        tmp_path,
        state,
        work,
        [START, report("done"), STOP, HANG],
        config=INTERACTIVE_FAST + f"hooks:\n  post-state-change:\n    - cmd: {json.dumps(cmd)}\n",
    )
    code, outcome, err = term.finish()
    outcome = check_outcome(code, outcome, err)
    assert (code, outcome["end_reason"]) == (0, "auto_exit")
    got = payloads(log)
    assert [p["final"] for p in got].count(True) == 1
    assert (got[-1]["cause"], got[-1]["mode"], got[-1]["state"]) == (
        "auto_exit",
        "interactive",
        "exited",
    )
    for payload in got:
        validate(payload, PAYLOAD_SCHEMA)


def wait_for(check: Any, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


def run_row_events(state: Path) -> list[tuple[Any, ...]]:
    db = sqlite3.connect(f"file:{state / 'state.db'}?mode=ro", uri=True)
    try:
        return db.execute("SELECT id, delivered_at, final FROM events ORDER BY id").fetchall()
    finally:
        db.close()


def running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            status = fh.read().rpartition(b")")[2].split()[0]
    except OSError:
        return False
    return status not in (b"Z", b"X")


def test_exec_waits_for_an_async_hook_before_it_exits(
    tmp_path: Path, state: Path, work: Path
) -> None:
    done = tmp_path / "async-done"
    cmd = f"cat > /dev/null; sleep 1; echo done > {shlex.quote(str(done))}"
    config = (
        "poll_interval: 0.05s\nhooks:\n  post-state-change:\n"
        f"    - cmd: {json.dumps(cmd)}\n      async: true\n      states: [exited]\n"
    )
    run = run_exec(tmp_path, state, work, [START, STOP, result_step()], config=config)
    assert run.code == 0, run.proc.stderr
    assert done.read_text() == "done\n"  # finished before exec exited


@pytest.mark.skipif(not LINUX, reason="start ticks come from /proc")
def test_a_signal_during_the_final_delivery_ends_the_hook_and_leaves_the_event(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """A SIGTERM after claude exited, while the final event's hook runs: exec ends the hook's
    process group (SIGTERM, then SIGKILL after kill_grace), does not mark the event, and exits
    with the run's outcome; the event stays for the next delivering command."""
    log = tmp_path / "hook.jsonl"
    config = (
        "kill_grace: 0.5s\n"
        + hook_config(log, "--ignore-term", "--child", "--sleep", "60").removeprefix(FAST)
        + "      timeout: 120s\n      states: [exited]\n"
    )
    proc = start_hanging(tmp_path, work, [START, STOP, result_step()], log, config=config)
    try:
        wait_for(lambda: log.read_text().endswith("\n"))
        (record,) = [json.loads(line) for line in log.read_text().splitlines()]
        assert record["payload"]["final"] is True
        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)  # the exact exec this test started
        out, err = proc.communicate(timeout=30)
        elapsed = time.monotonic() - start
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert elapsed < 0.5 + 4, elapsed
    assert proc.returncode == 0, err
    assert json.loads(out)["outcome"] == "done"
    assert not running(record["pid"]) and not running(record["child"])
    final = [row for row in run_row_events(state) if row[2]]
    assert final and final[0][1] is None  # the final event is not marked delivered
    assert run_row_of(state)["ended_at"] is not None
    with sqlite3.connect(f"file:{state / 'state.db'}?mode=ro", uri=True) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM leases WHERE name LIKE 'delivery:%'"
        ).fetchone() == (0,)


def test_exec_closes_the_worker_even_when_the_internal_error_path_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: Path,
    work: Path,
) -> None:
    from claude_session import supervisor

    workers: list[Any] = []
    real_start, real_internal = supervisor._start_worker, supervisor._internal_error

    def start_worker(*args: Any, **kwargs: Any) -> Any:
        workers.append(real_start(*args, **kwargs))
        return workers[-1]

    def supervise_fails(self: Any) -> int:
        raise RuntimeError("supervision bug")

    def internal_error_fails(*args: Any, **kwargs: Any) -> None:
        real_internal(*args, **kwargs)  # ends claude and finalizes, then fails
        raise RuntimeError("cleanup bug")

    monkeypatch.setattr(supervisor, "_start_worker", start_worker)
    monkeypatch.setattr(supervisor.Supervision, "wait", supervise_fails)
    monkeypatch.setattr(supervisor, "_internal_error", internal_error_fails)
    try:
        code, printed = exec_in_process(
            monkeypatch, capsys, tmp_path, work, [START, {"do": "hang"}]
        )
        assert code == 1 and printed["error"]["code"] == "internal"
        (worker,) = workers
        assert not worker._thread.is_alive()  # closed: exec does not hang on it
    finally:
        for worker in workers:
            worker.close()
