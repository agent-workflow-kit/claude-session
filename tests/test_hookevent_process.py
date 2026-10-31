"""hook-event as Claude Code runs it: ``/bin/sh -c '<interpreter> -I -m claude_session
hook-event --state-dir <root> <Event>'`` with the payload on stdin (design.md, Generated
settings and Hook-event fast path).

TODO(#15): an end-to-end run through the fake claude (#8), which reads the generated
``--settings`` and fires these hooks itself.
"""

from __future__ import annotations

import json
import os
import shlex
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from claude_session import hookevent, settings_builder
from claude_session.store import Store

from .test_hookevent import SID, Env, common


@pytest.fixture
def env(tmp_path: Path) -> Env:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    return Env(root)


def run_hook(
    env: Env, event: str, payload: Any, *, raw: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    command = shlex.join(
        [
            settings_builder.current_interpreter(),
            "-I",
            "-m",
            "claude_session",
            "hook-event",
            "--state-dir",
            str(env.root),
            event,
        ]
    )
    data = raw if raw is not None else json.dumps(payload).encode()
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=data,
        capture_output=True,
        env={**os.environ, "CSESSION_ID": SID},
        cwd="/",
        timeout=60,
        check=False,
    )


def log_records(env: Env) -> list[dict[str, Any]]:
    path = env.root / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_session_start_prints_exactly_the_context_json(env: Env) -> None:
    env.add(inputs=["brief.md"])
    proc = run_hook(env, "SessionStart", common("SessionStart", source="startup"))
    assert (proc.returncode, proc.stderr) == (0, b"")
    assert json.loads(proc.stdout) == hookevent.session_start_output(
        env.csession_dir, 1, ["brief.md"]
    )
    assert env.state() == ("working", None, "working")
    assert log_records(env) == []


def test_the_backstop_blocks_through_stdout(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    proc = run_hook(env, "Stop", common("Stop", stop_hook_active=False, background_tasks=[]))
    assert (proc.returncode, proc.stderr) == (0, b"")
    assert json.loads(proc.stdout) == hookevent.backstop_output()


def test_a_state_only_event_prints_nothing(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    proc = run_hook(env, "PermissionRequest", common("PermissionRequest", tool_name="Bash"))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert env.state() == ("needs-input", "permission", "working")


@pytest.mark.parametrize("raw", [b"", b"{not json", b"\x00\xff", b"[]"])
def test_a_malformed_payload_exits_0_silently_and_is_logged(env: Env, raw: bytes) -> None:
    env.add(state="working", activity="working", started=True)
    proc = run_hook(env, "SessionStart", None, raw=raw)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    (record,) = log_records(env)
    assert (record["event"], record["level"], record["csession_id"]) == (
        "hook_event_failed",
        "error",
        SID,
    )
    assert record["message"] == "SessionStart: HookEventError"
    assert env.state() == ("working", None, "working")


def test_an_unknown_session_exits_0_silently(env: Env) -> None:
    proc = run_hook(env, "PostToolUse", {})
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    assert [r["event"] for r in log_records(env)] == ["hook_event_failed"]


def test_hook_latency(env: Env) -> None:
    """The design's budget is ~50-80 ms per hook (design.md, Decisions). The bound here is
    loose so a loaded CI machine does not flake; the median is printed for the record."""
    env.add(mode="interactive", state="idle", activity="idle", started=True)
    payload = common("PostToolUse", tool_name="Bash", tool_response={"stdout": "x" * 10_000})
    samples = []
    for _ in range(7):
        began = time.monotonic()
        proc = run_hook(env, "PostToolUse", payload)
        samples.append(time.monotonic() - began)
        assert proc.returncode == 0
    median = statistics.median(samples)
    print(f"hook-event median latency: {median * 1000:.0f} ms")
    assert median < 1.0
