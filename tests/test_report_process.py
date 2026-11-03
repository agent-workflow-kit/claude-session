"""The report round trip as processes, the way an agent and Claude Code run them: the agent
runs ``claude-session report`` in its session, then Claude Code runs the injected
``PostToolUse`` hook (``/bin/sh -c '<interpreter> -I -m claude_session hook-event ...'``).

The same flow through the fake claude (#8) launched by ``exec`` (a ``report`` step with a
heredoc, a rejected report refiled from its ``on_block``, and the Stop backstop after a
rejection) is in ``test_exec_headless.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_session.store import Store

from .outbox_env import Run, rejection
from .test_hookevent import SID, common
from .test_hookevent_process import run_hook


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    yield Run(root).setup()


def agent_report(run: Run, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "claude_session", "report", *args],
        input=stdin,
        env={**os.environ, **run.environ()},
        cwd=run.csession_dir,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_report_then_post_tool_use_ingests_the_report(run: Run) -> None:
    run.write_artifact("result.md", b"# Result\n")
    path = f"{run.csession_dir}/artifacts/result.md"
    proc = agent_report(run, "done", "--summary-file", "-", "--artifact", path, stdin=b"Done.\n")
    assert proc.returncode == 0, proc.stderr
    hook = run_hook(run, "PostToolUse", common("PostToolUse", tool_name="Bash"))
    assert (hook.returncode, hook.stdout, hook.stderr) == (0, b"", b"")
    r = run.run()
    assert (r["report_status"], r["report_summary"], r["artifact_name"]) == (
        "done",
        "Done.\n",
        "result.md",
    )
    assert Path(r["artifact_path"]).read_bytes() == b"# Result\n"
    assert run.state() == ("reported", None, "working")


def test_a_rejected_report_reaches_the_agent_and_a_refiled_one_is_ingested(run: Run) -> None:
    run.write_artifact("result.md", b"v1")
    assert agent_report(run, "done", "--summary", "s", "--artifact", "result.md").returncode == 0
    run.write_artifact("result.md", b"v2")  # changed after report
    hook = run_hook(run, "PostToolUse", common("PostToolUse", tool_name="Bash"))
    assert hook.returncode == 0 and hook.stderr == b""
    assert rejection(hook.stdout.decode()) == "artifact_changed"
    assert run.run()["report_status"] is None
    assert agent_report(run, "done", "--summary", "s", "--artifact", "result.md").returncode == 0
    hook = run_hook(run, "PostToolUse", common("PostToolUse", tool_name="Bash"))
    assert hook.stdout == b""
    assert run.run()["artifact_sha256"] is not None
    assert [r["result"] for r in run.requests()] == ["artifact_changed", "ingested"]


def test_a_usage_error_exits_2_and_queues_nothing(run: Run) -> None:
    proc = agent_report(run, "done", "--summary", "s", "--artifact", "../run.json", "--json")
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["error"]["code"] == "usage"
    assert os.listdir(run.outbox) == []


def test_report_outside_a_session_exits_2(run: Run) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "claude_session", "report", "done", "--summary", "s"],
        env={k: v for k, v in os.environ.items() if not k.startswith("CSESSION_")},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2
    assert b"inside a claude-session session" in proc.stderr
    assert SID not in proc.stderr.decode()
