"""Signals and timing of headless (`-p`) runs, for the end mapping and auto-exit (#64, #67, #57).

Signals go to the exact child these tests spawned (`LiveEnv.spawn_headless`, its own process
group); the group is killed at teardown while the leader is still unreaped, and the run is
charged its reported cost (or the estimate)."""

from __future__ import annotations

import shlex
import signal
import sys

import pytest

from ..fakeclaude import recorder
from .harness import LiveEnv, hook_group, parse_stream, read_log, results, wait_for

SLOW = "Write the numbers from 1 to 300, one per line, then say DONE."


@pytest.mark.live
@pytest.mark.parametrize(
    ("sig", "accepted"),
    [
        (signal.SIGINT, {130, -signal.SIGINT}),
        (signal.SIGTERM, {143, -signal.SIGTERM}),
        (signal.SIGHUP, {129, -signal.SIGHUP}),
    ],
    ids=["sigint", "sigterm", "sighup"],
)
def test_signals_end_a_headless_run_with_128_plus_n_or_by_the_signal(
    live: LiveEnv, sig: signal.Signals, accepted: set[int]
) -> None:
    """#64 and #67 item 3, #57 (SIGHUP): which of the two forms real claude uses. The end
    mapping accepts both; the one seen is recorded so exit codes 129 to 192 can be settled."""
    out, err = live.root / "out.jsonl", live.root / "err.txt"
    child = live.spawn_headless(SLOW, out=out, err=err)
    assert wait_for(lambda: out.exists() and out.stat().st_size > 0, 60), err.read_text()
    child.send_signal(sig)
    assert wait_for(lambda: child.returncode() is not None, 30), "claude ignored the signal"
    code = child.returncode()
    live.observe(f"claude -p after {sig.name}", code)
    assert code in accepted, code


@pytest.mark.live
def test_the_final_result_line_follows_a_stop_within_the_auto_exit_delay(live: LiveEnv) -> None:
    """#67 item 4: `exec` SIGTERMs claude `auto_exit_delay` (2 s) after a qualifying Stop, so
    `claude -p` must have written its `result` line by then, or `cost_usd` would be null."""
    stamp = live.root / "stop-time"
    now = shlex.join([sys.executable, "-I", "-c", "import time; print(time.time())"])
    hooks = {"Stop": [hook_group(f"{now} > {shlex.quote(str(stamp))}")]}  # portable (#43)
    settings = live.write_json("s.json", {"hooks": hooks})
    out, err = live.root / "out.jsonl", live.root / "err.txt"
    child = live.spawn_headless("Reply OK.", "--settings", str(settings), out=out, err=err)
    assert wait_for(lambda: child.returncode() is not None, 120), "claude -p did not finish"
    assert child.returncode() == 0, err.read_text()
    stopped = float(stamp.read_text(encoding="utf-8"))
    last_write = out.stat().st_mtime
    live.observe("seconds from Stop to the last output write", round(last_write - stopped, 2))
    assert results(parse_stream(out.read_text(encoding="utf-8")))
    assert last_write - stopped < 2.0, "the result line came later than auto_exit_delay"


@pytest.mark.live
def test_background_subagents_make_one_result_object_per_turn(live: LiveEnv) -> None:
    """A1: `-p` waits for a background subagent and runs another turn, so the stream holds
    several `result` objects and the final one is the last; costs add up across them."""
    stops = live.root / "stops.jsonl"
    hooks = {"Stop": [hook_group(recorder(stops, args=["Stop"]))]}
    settings = live.write_json("s.json", {"hooks": hooks})
    run = live.headless(
        "Start a background subagent (the Task or Agent tool with run_in_background true) "
        "whose job is to reply with the word LATER. Do not wait for it: reply STARTED at once.",
        "--allowedTools", "Task", "Agent", "--settings", str(settings),
    )  # fmt: skip
    found = results(run.events)
    live.observe("result objects with a background subagent", len(found))
    assert len(found) >= 2, "no second turn: the prompt did not start a background subagent"
    assert run.final is found[-1]
    total = sum(float(r["total_cost_usd"]) for r in found)
    assert total > 0
    stop_events = [p for p in read_log(stops) if p["payload"] is not None]
    assert len(stop_events) >= 2
    assert any(p["payload"].get("background_tasks") for p in stop_events[:-1])
