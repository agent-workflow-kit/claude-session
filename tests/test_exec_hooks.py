"""``post-state-change`` delivery by ``exec`` end to end (interface.md, Hooks: Delivery): the
background worker during the session phase, the final event before ``exec`` exits, and the
synchronous delivery of a launch-phase end. Hooks are ``tests/fakebin/post_hook.py``, which
records its start (see ``test_toolhooks``)."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from .schemas import validate
from .test_exec_headless import START, STOP, result_step, run_exec
from .test_exec_interactive import FAST, HANG, check_outcome, report, start_terminal

HOOK = Path(__file__).parent / "fakebin" / "post_hook.py"
PAYLOAD_SCHEMA = "claude-session/hooks/post-state-change.json"


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


def test_a_slow_hook_never_delays_supervision(tmp_path: Path, state: Path, work: Path) -> None:
    log = tmp_path / "hook.jsonl"
    # Every hook takes 0.3 s; the run still completes and every event is delivered in order.
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config=hook_config(log, "--sleep", "0.3"),
    )
    assert run.code == 0, run.proc.stderr
    ids = [row[0] for row in run.rows("SELECT id FROM events ORDER BY id")]
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
        config=FAST + f"hooks:\n  post-state-change:\n    - cmd: {json.dumps(cmd)}\n",
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
