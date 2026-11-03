"""The tool guard end to end through ``exec`` (fake ``claude``): no binary under any kind's
session-writable paths, or those recorded on an isolated run, is ever executed by a launch,
its hooks or handed to the session on its ``PATH`` (interface.md, PATH rules; issue #97)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_exec_headless import (
    FAST,
    START,
    STOP,
    private_home,
    read_jsonl,
    result_step,
    run_exec,
    script,
)

# A configured kind (isolated) whose runs may write ~/td.
OTHER_KIND = "kinds:\n  scribe:\n    write_dirs: ['~/td']\n"


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def plant(directory: Path, marker: Path, *names: str) -> Path:
    """Programs that record that they ran (and then fail)."""
    for name in names:
        script(directory / name, f"#!/bin/sh\necho {name} >> {marker}\nexit 1\n")
    return directory


def home(tmp_path: Path) -> Path:
    return tmp_path / "home"  # run_exec's private HOME


def test_a_trusted_launch_refuses_tools_planted_in_another_kinds_write_dir(
    tmp_path: Path, state: Path, work: Path
) -> None:
    marker = tmp_path / "ran"
    planted = plant(home(tmp_path) / "td" / "bin", marker, "git", "bwrap", "socat")
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--kind", "implement", "--repo", "example.org/group/project"],
        config=FAST + OTHER_KIND,
        env={"PATH": f"{planted}:/usr/bin:/bin"},
    )
    assert run.code == 12, run.proc.stderr
    assert run.outcome["end_reason"] == "unsafe_path"
    message = run.outcome["message"]
    assert "kind 'scribe'" in message and str(home(tmp_path) / "td") in message, message
    assert not marker.exists(), marker.read_text()


def test_a_notes_command_planted_in_another_kinds_write_dir_never_runs(
    tmp_path: Path, state: Path, work: Path
) -> None:
    marker = tmp_path / "ran"
    planted = plant(home(tmp_path) / "td" / "bin", marker, "notes-cmd")
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config=FAST + OTHER_KIND + "notes_context_cmd: [notes-cmd]\n",
        env={"PATH": f"/usr/bin:/bin:{planted}"},
    )
    assert run.code == 0, json.dumps(run.outcome)
    (record,) = [r for r in read_jsonl(state / "log.jsonl") if r["event"] == "notes_failed"]
    assert record["message"].startswith("notes command refused"), record
    assert "kind 'scribe'" in record["message"]
    assert not marker.exists(), marker.read_text()


def test_the_session_and_hooks_never_get_a_path_entry_under_another_kinds_write_dir(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """A trusted run: the entry is dropped from the session ``PATH`` (a warning) and from the
    pre-launch hook's; the hook's ``sh`` is the real one, the planted one never runs."""
    marker = tmp_path / "ran"
    planted = plant(home(tmp_path) / "td" / "bin", marker, "sh", "node")
    seen = tmp_path / "session-path.txt"
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, {"do": "bash", "command": f'printf %s "$PATH" > {seen}'}, STOP, result_step()],
        args=["--kind", "implement", "--hook", "pre-launch=true"],
        config=FAST + OTHER_KIND,
        env={"PATH": f"{planted}:/usr/bin:/bin"},
    )
    assert run.code == 0, json.dumps(run.outcome)
    assert str(planted) not in seen.read_text().split(":")
    assert any(str(planted) in w and "dropped" in w for w in run.outcome["warnings"]), run.outcome
    assert not marker.exists(), marker.read_text()


def test_an_isolated_session_path_entry_under_another_kinds_write_dir_is_refused(
    tmp_path: Path, state: Path, work: Path
) -> None:
    planted = home(tmp_path) / "td" / "bin"
    planted.mkdir(parents=True)
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--kind", "review", "--sandbox", "off"],
        config=FAST + OTHER_KIND,
        env={"PATH": f"/usr/bin:/bin:{planted}"},
    )
    assert run.code == 12, run.proc.stderr
    assert run.outcome["end_reason"] == "unsafe_path"
    assert "PATH entry" in run.outcome["message"] and "kind 'scribe'" in run.outcome["message"]


def test_a_recorded_isolated_runs_cwd_refuses_a_later_launchs_tool(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """A ``respond`` run (isolated, the caller's cwd) may write its cwd, which no kind config
    shows: once recorded, a later trusted launch never runs a git planted there."""
    checkout = tmp_path / "checkout"
    git(tmp_path, "init", "-q", str(checkout))
    first = run_exec(
        tmp_path,
        state,
        checkout,
        [START, STOP, result_step()],
        args=["--kind", "respond", "--sandbox", "off"],
        config=FAST,
    )
    assert first.code == 0, json.dumps(first.outcome)
    marker = tmp_path / "ran"
    planted = plant(checkout / "bin", marker, "git")
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--kind", "implement", "--repo", "example.org/group/project"],
        config=FAST,
        env={"PATH": f"{planted}:/usr/bin:/bin"},
    )
    assert run.code == 12, run.proc.stderr
    assert run.outcome["end_reason"] == "unsafe_path"
    assert first.outcome["csession_id"] in run.outcome["message"]
    assert not marker.exists(), marker.read_text()


def test_a_per_call_permission_profile_write_path_refuses_a_later_launchs_tool(
    tmp_path: Path, state: Path, work: Path
) -> None:
    profiles = (
        "permission_profiles:\n  wide:\n    sandbox:\n      filesystem:\n"
        "        allowWrite: ['~/aw']\n"
    )
    (home(tmp_path) / "aw").mkdir(parents=True)
    first = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--kind", "review", "--sandbox", "off", "--permission-profile", "wide"],
        config=FAST + profiles,
    )
    assert first.code == 0, json.dumps(first.outcome)
    marker = tmp_path / "ran"
    planted = plant(home(tmp_path) / "aw", marker, "git")
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--kind", "implement", "--repo", "example.org/group/project"],
        config=FAST + profiles,
        env={"PATH": f"{planted}:/usr/bin:/bin"},
    )
    assert run.code == 12, run.proc.stderr
    assert run.outcome["end_reason"] == "unsafe_path"
    assert first.outcome["csession_id"] in run.outcome["message"]
    assert not marker.exists(), marker.read_text()


def git(tmp_path: Path, *argv: str) -> None:
    subprocess.run(
        ["/usr/bin/git", *argv],
        check=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", **private_home(tmp_path)},
    )


def test_the_step_2_git_never_runs_a_git_planted_in_another_kinds_write_dir(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """Without ``--repo`` the repo is derived from ``origin`` before the run's own paths are
    known: a git under another kind's write dir is not run (no repo derived, a warning), and
    step 5 then refuses it."""
    git(tmp_path, "init", "-q", str(work))
    git(tmp_path, "-C", str(work), "remote", "add", "origin", "https://example.org/group/project")
    marker = tmp_path / "ran"
    planted = plant(home(tmp_path) / "td" / "bin", marker, "git")
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--kind", "implement"],
        config=FAST + OTHER_KIND,
        env={"PATH": f"{planted}:/usr/bin:/bin"},
    )
    assert run.code == 12, run.proc.stderr
    assert run.outcome["end_reason"] == "unsafe_path"
    assert not marker.exists(), marker.read_text()
