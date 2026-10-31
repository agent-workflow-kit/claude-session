"""The notes context end to end: ``exec`` resolves the notes command and writes it into
``hookconf.json``; the fake claude fires SessionStart and records the context it gets
(interface.md, Config: ``notes_context_*``; design.md, Hook-event fast path)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claude_session import notes

from .test_exec_headless import (
    FAST,
    START,
    STOP,
    read_jsonl,
    result_step,
    run_exec,
    script,
)


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


API_KEY = "sk-test-not-a-real-key-0123456789"
SECRET_ENV_VALUE = "webhook-token-abcdefghijkl"


def notes_script(tmp_path: Path, body: str = "echo 'the local queue must be running'\n") -> Path:
    """A notes command that records its argv, cwd and environment, then runs ``body``."""
    record = tmp_path / "notes-record.json"
    return script(
        tmp_path / "notes-bin" / "notes-cmd",
        "#!/bin/sh\n"
        f'/usr/bin/env python3 -I -c \'import json, os, sys; json.dump({{"argv": sys.argv[1:], '
        f'"cwd": os.getcwd(), "env": dict(os.environ)}}, open("{record}", "w"))\' "$@"\n' + body,
    )


def recorded(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "notes-record.json").read_text())


def contexts(fake_log: list[dict[str, Any]]) -> list[str]:
    return [c for r in fake_log if r["event"] == "context" for c in r["contexts"]]


def notes_records(state: Path) -> list[dict[str, Any]]:
    return [r for r in read_jsonl(state / "log.jsonl") if r["event"] == "notes_failed"]


def test_the_notes_block_reaches_the_session_start_context(
    tmp_path: Path, state: Path, work: Path
) -> None:
    cmd = notes_script(tmp_path)
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--label", "ticket=T-1", "--repo", "example.org/group/project"],
        config=FAST + f"notes_context_cmd: [{cmd}, context]\nsecret_env: [HOOK_TOKEN]\n",
        env={"ANTHROPIC_API_KEY": API_KEY, "HOOK_TOKEN": SECRET_ENV_VALUE},
    )
    assert run.code == 0, run.proc.stderr
    (context,) = contexts(run.fake_log())
    first, block = context.split("\n", 1)
    assert first.startswith("claude-session directory: ")
    assert block.startswith("Notes left by other sessions follow.")
    assert '\nthe local queue must be running\n</untrusted id="' in block
    seen = recorded(tmp_path)
    assert seen["argv"] == [
        "context",
        "--repo=example.org/group/project",
        "--label=kind=implement",
        "--label=ticket=T-1",
    ]
    assert seen["cwd"] == str(state)  # the state root
    env = seen["env"]
    assert API_KEY not in env.values() and SECRET_ENV_VALUE not in env.values()
    assert not any(name.startswith("CSESSION_") for name in env)
    shell = {"PWD", "SHLVL", "_"}  # set by /bin/sh itself
    assert {n for n in env if not n.startswith("LC_")} <= notes.ENV_NAMES | {"PATH"} | shell
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "home" / ".config")
    assert notes_records(state) == []
    conf = json.loads((run.private_run / "hookconf.json").read_text())["notes"]
    assert conf["executable"] == str(cmd) and conf["argv"][0] == "notes-cmd"
    assert (conf["timeout"], conf["max_bytes"], conf["sources"]) == (5.0, 16384, ["startup"])


def test_a_missing_notes_command_is_logged_and_the_run_goes_on(
    tmp_path: Path, state: Path, work: Path
) -> None:
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config=FAST + f"notes_context_cmd: [{tmp_path / 'no-such-notes'}]\n",
    )
    assert run.code == 0, run.proc.stderr
    (context,) = contexts(run.fake_log())
    assert "\n" not in context
    (record,) = notes_records(state)
    assert (record["level"], record["run"]) == ("warn", 1)
    assert record["message"].startswith("notes command not found")
    assert json.loads((run.private_run / "hookconf.json").read_text())["notes"] is None


def test_a_failing_notes_command_is_logged_by_the_hook(
    tmp_path: Path, state: Path, work: Path
) -> None:
    cmd = notes_script(tmp_path, "echo partial; exit 3\n")
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config=FAST + f"notes_context_cmd: [{cmd}]\n",
    )
    assert run.code == 0, run.proc.stderr
    (context,) = contexts(run.fake_log())
    assert "partial" not in context
    assert [r["message"] for r in notes_records(state)] == ["exit 3"]


def test_a_kind_without_notes_context_runs_no_command(
    tmp_path: Path, state: Path, work: Path
) -> None:
    cmd = notes_script(tmp_path)
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--kind", "audit"],
        config=FAST + f"notes_context_cmd: [{cmd}]\n",
    )
    assert run.code == 0, run.proc.stderr
    assert not (tmp_path / "notes-record.json").exists()
    assert json.loads((run.private_run / "hookconf.json").read_text())["notes"] is None
    assert notes_records(state) == []


def test_the_timeout_is_bounded_by_the_hook_budget(tmp_path: Path, state: Path, work: Path) -> None:
    cmd = notes_script(tmp_path)
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config=FAST + f"notes_context_cmd: [{cmd}]\nnotes_context_timeout: 10m\n"
        "notes_context_sources: [startup, resume, startup]\n",
    )
    assert run.code == 0, run.proc.stderr
    conf = json.loads((run.private_run / "hookconf.json").read_text())["notes"]
    assert conf["timeout"] == 15.0
    assert conf["sources"] == ["startup", "resume"]


def test_an_isolated_run_never_runs_a_notes_command_it_could_have_planted(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """A notes command inside a session-writable path (here the cwd of an isolated run) is
    refused, logged, and the launch goes on without notes."""
    cmd = notes_script(work)  # inside the cwd
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--reads-third-party", "--sandbox", "off"],
        config=FAST + f"notes_context_cmd: [{cmd}]\n",
    )
    assert run.code == 0, json.dumps(run.outcome)
    (record,) = notes_records(state)
    assert record["message"].startswith("notes command refused: executable is inside")
    assert not (work / "notes-record.json").exists()
    assert json.loads((run.private_run / "hookconf.json").read_text())["notes"] is None


def test_short_secret_env_and_auth_variables_never_reach_the_command(
    tmp_path: Path, state: Path, work: Path
) -> None:
    """A secret_env value under the secret-value threshold is not a secret value, so the
    variable is dropped by name (as is a claude_auth_env name)."""
    cmd = notes_script(tmp_path)
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config=FAST + f"notes_context_cmd: [{cmd}]\nsecret_env: [LC_PIN]\n"
        "claude_auth_env: [LC_AUTH_HINT]\n",
        env={"LC_PIN": "1234", "LC_AUTH_HINT": "x", "LC_PAPER": "C"},
    )
    assert run.code == 0, run.proc.stderr
    env = recorded(tmp_path)["env"]
    assert "LC_PIN" not in env and "LC_AUTH_HINT" not in env
    assert env["LC_PAPER"] == "C"


def test_a_label_holding_a_secret_value_is_left_out_and_logged(
    tmp_path: Path, state: Path, work: Path
) -> None:
    cmd = notes_script(tmp_path)
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--label", f"ticket=x-{SECRET_ENV_VALUE}", "--label", "team=core"],
        config=FAST + f"notes_context_cmd: [{cmd}]\nsecret_env: [HOOK_TOKEN]\n",
        env={"HOOK_TOKEN": SECRET_ENV_VALUE},
    )
    assert run.code == 0, run.proc.stderr
    argv = recorded(tmp_path)["argv"]
    assert argv == ["--label=kind=implement", "--label=team=core"]
    conf = json.loads((run.private_run / "hookconf.json").read_text())["notes"]
    assert not any(SECRET_ENV_VALUE in token for token in conf["argv"])
    (record,) = notes_records(state)
    assert record["level"] == "warn"
    assert "'ticket'" in record["message"] and SECRET_ENV_VALUE not in json.dumps(record)


def test_the_size_cap_is_bounded_to_one_mib(tmp_path: Path, state: Path, work: Path) -> None:
    cmd = notes_script(tmp_path)
    run = run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        config=FAST + f"notes_context_cmd: [{cmd}]\nnotes_context_max_bytes: 9007199254740991\n",
    )
    assert run.code == 0, run.proc.stderr
    conf = json.loads((run.private_run / "hookconf.json").read_text())["notes"]
    assert conf["max_bytes"] == notes.MAX_BYTES_MAX == 1 << 20
