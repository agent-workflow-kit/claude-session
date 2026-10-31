"""``resume``, ``start --resume`` and ``--fork`` end to end (interface.md, Resume and fork
carry-over, resume and start; design.md, Resume and fork): real ``claude-session`` processes
run the fake ``claude`` (``tests/fakebin``), which records the argv it got.

The sessions resumed here are made by real launches (``exec`` and ``start``), so the stored
explicit flags, cwd, git directory, Claude id and staged files are the ones a launch writes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from claude_session import launch, outbox

from .fakeclaude import read_jsonl
from .test_exec_headless import CLAUDE_SESSION, claude_session_cmd, private_home
from .test_start_adopt import DONE, START, STOP, Env, result_step, validate

STEPS = [START, DONE, STOP, result_step()]


@pytest.fixture
def env(tmp_path: Path, _private_state_dir: Path) -> Env:
    work = tmp_path / "work"
    work.mkdir()
    state = _private_state_dir / "claude-session"
    e = Env(tmp_path, state, work, tmp_path / "config.yaml", tmp_path / "fake.jsonl")
    e.scenario(STEPS)
    e.write_config()
    return e


def run_cs(
    env: Env, argv: Sequence[str], *, cwd: Path | None = None, json_out: bool = True
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    proc = env.cs([*argv, "--config", str(env.config)], cwd=cwd)
    out = json.loads(proc.stdout) if json_out and proc.stdout else {}
    return proc, out


def exec_session(env: Env, *args: str, prompt: str | None = "Do the task.\n") -> dict[str, Any]:
    """A headless ``exec`` session; returns its outcome object."""
    argv = ["exec", "--json", "--mode", "headless", *args]
    if prompt is not None:
        prompt_file = env.tmp_path / "prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        argv += ["--prompt-file", str(prompt_file)]
    _proc, out = run_cs(env, argv)
    assert "csession_id" in out, out
    return out


def resume(
    env: Env, sid: str, *args: str, cwd: Path | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    proc, out = run_cs(env, ["resume", sid, "--json", *args], cwd=cwd)
    if "error" in out:
        validate(out, "claude-session/cli/error.json")
    elif "dry_run" in out:
        validate(out, "claude-session/cli/exec-dry-run.json")
    else:
        validate(out, "claude-session/cli/resume.json")
        assert out["exit_code"] == proc.returncode
    return proc, out


def starts(env: Env) -> list[dict[str, Any]]:
    """The fake's ``start`` records (one per ``claude`` started)."""
    return [r for r in read_jsonl(env.log) if r["event"] == "start"]


def flag_value(record: dict[str, Any], name: str) -> Any:
    return record["flags"].get(name)


def spec(env: Env, sid: str, run: int) -> dict[str, Any]:
    return json.loads(env.run_row(sid, run)["spec_json"])


def claude_id(env: Env, sid: str) -> str | None:
    return env.rows("SELECT claude_session_id FROM sessions WHERE csession_id = ?", (sid,))[0][0]


# resume


def test_resume_continues_the_conversation_with_the_latest_claude_id(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    first = claude_id(env, sid)
    assert first is not None
    proc, out = resume(env, sid)
    assert proc.returncode == 0, proc.stderr
    assert (out["csession_id"], out["run"], out["outcome"]) == (sid, 2, "done")
    second = starts(env)[-1]
    assert flag_value(second, "--resume") == first
    assert "--session-id" not in second["flags"] and "--fork-session" not in second["flags"]
    # A headless resume without a prompt gets the resume text (interface.md, Bootstrap
    # prompt), and the run runs in the previous run's cwd.
    assert second["prompt"].endswith(
        "Continue the task. When you are finished, file your status report."
    )
    assert second["cwd"] == str(env.work.resolve())
    assert env.run_row(sid, 2)["cwd"] == env.run_row(sid, 1)["cwd"]
    assert env.rows("SELECT current_run, state FROM sessions") == [(2, "exited")]
    run_json = json.loads((env.state / "sessions" / sid / "run.json").read_text())
    assert run_json["run"] == 2


def test_resume_with_a_prompt_writes_prompts_n(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    prompt = env.tmp_path / "next.md"
    prompt.write_text("Now the next part.\n", encoding="utf-8")
    proc, _out = resume(env, sid, "--prompt-file", str(prompt))
    assert proc.returncode == 0, proc.stderr
    placed = env.state / "sessions" / sid / "prompts" / "2.md"
    assert placed.read_text() == "Now the next part.\n"
    assert starts(env)[-1]["prompt"].endswith(f"Read and follow {placed}")
    assert (env.state / "sessions" / sid / "prompt.md").read_text() == "Do the task.\n"


def test_resume_accepts_a_short_id_and_rejects_unknown_ones(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = resume(env, sid.removeprefix("cs-")[:5])
    assert proc.returncode == 0 and out["csession_id"] == sid
    proc, out = resume(env, "cs-zzzzzzzz")
    assert proc.returncode == 10 and out["error"]["code"] == "not_found"
    env.write_db("UPDATE sessions SET forgotten_at = '2026-01-01T00:00:00Z'")
    proc, out = resume(env, sid)
    assert proc.returncode == 10
    assert env.rows("SELECT COUNT(*) FROM runs") == [(2,)]


def assert_not_started(
    proc: subprocess.CompletedProcess[str], out: dict[str, Any], end_reason: str
) -> None:
    assert proc.returncode == 12, (proc.stdout, proc.stderr)
    assert (out["outcome"], out["end_reason"], out["phase"]) == (
        "not-started",
        end_reason,
        "launch",
    ), out


# The carry-over table


def test_same_kind_carries_the_explicit_flags(env: Env) -> None:
    sid = exec_session(env, "--model", "m1", "--max-turns", "5", "--timeout", "1h")["csession_id"]
    proc, _out = resume(env, sid)
    assert proc.returncode == 0, proc.stderr
    second = starts(env)[-1]
    assert flag_value(second, "--model") == "m1"
    assert flag_value(second, "--max-turns") == "6"  # one more for the backstop
    assert spec(env, sid, 2) == {"model": "m1", "max_turns": 5, "timeout": "1h"}
    origins = json.loads(env.run_row(sid, 2)["resolved_json"])["origins"]
    assert origins["model"] == origins["max_turns"] == origins["timeout"] == "previous_run"
    assert env.run_row(sid, 2)["model"] == "m1"


def test_a_flag_given_now_replaces_the_carried_value(env: Env) -> None:
    sid = exec_session(env, "--model", "m1")["csession_id"]
    proc, _out = resume(env, sid, "--model", "m2")
    assert proc.returncode == 0, proc.stderr
    assert flag_value(starts(env)[-1], "--model") == "m2"
    assert spec(env, sid, 2)["model"] == "m2"
    assert json.loads(env.run_row(sid, 2)["resolved_json"])["origins"]["model"] == "flag"
    # The value given now is what the next run carries.
    proc, _out = resume(env, sid)
    assert flag_value(starts(env)[-1], "--model") == "m2"


def test_a_kind_change_carries_no_explicit_flag(env: Env) -> None:
    sid = exec_session(env, "--model", "m1", "--max-turns", "5")["csession_id"]
    proc, _out = resume(env, sid, "--kind", "draft-ticket")
    assert proc.returncode == 0, proc.stderr
    second = starts(env)[-1]
    assert "--model" not in second["flags"] and "--max-turns" not in second["flags"]
    assert spec(env, sid, 2) == {}
    assert env.run_row(sid, 2)["run_kind"] == "draft-ticket"
    # The launch kind never changes; the cwd and repo are carried anyway.
    assert env.rows("SELECT launch_kind FROM sessions") == [("implement",)]
    assert env.run_row(sid, 2)["cwd"] == env.run_row(sid, 1)["cwd"]
    # A resume without --kind continues in the previous run's kind.
    proc, _out = resume(env, sid)
    assert env.run_row(sid, 3)["run_kind"] == "draft-ticket"


def test_reset_drops_a_carried_setting(env: Env) -> None:
    sid = exec_session(env, "--model", "m1", "--max-turns", "5")["csession_id"]
    proc, _out = resume(env, sid, "--reset", "model")
    assert proc.returncode == 0, proc.stderr
    second = starts(env)[-1]
    assert "--model" not in second["flags"]
    assert flag_value(second, "--max-turns") == "6"
    assert spec(env, sid, 2) == {"max_turns": 5}


def test_reset_with_the_flag_it_resets_exits_2_and_allocates_nothing(env: Env) -> None:
    sid = exec_session(env, "--model", "m1")["csession_id"]
    proc, out = resume(env, sid, "--reset", "model", "--model", "m2")
    assert proc.returncode == 2 and out["error"]["code"] == "usage"
    proc, out = resume(env, sid, "--reset", "reads-third-party")
    assert proc.returncode == 2
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]


def test_labels_given_on_resume_are_added(env: Env) -> None:
    sid = exec_session(env, "--label", "a=1")["csession_id"]
    proc, _out = resume(env, sid, "--label", "b=2")
    assert proc.returncode == 0, proc.stderr
    assert env.rows("SELECT key, value FROM labels ORDER BY key") == [
        ("a", "1"),
        ("b", "2"),
        ("kind", "implement"),
    ]


# cwd


def test_resume_runs_in_the_previous_cwd_wherever_it_is_called_from(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    elsewhere = env.tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc, _out = resume(env, sid, cwd=elsewhere)
    assert proc.returncode == 0, proc.stderr
    assert starts(env)[-1]["cwd"] == str(env.work.resolve())
    # --cwd naming the same directory is accepted; another one exits 2.
    proc, _out = resume(env, sid, "--cwd", str(env.work), cwd=elsewhere)
    assert proc.returncode == 0, proc.stderr
    proc, out = resume(env, sid, "--cwd", str(elsewhere))
    assert proc.returncode == 2 and "previous run's cwd" in out["error"]["message"]
    assert env.rows("SELECT COUNT(*) FROM runs") == [(3,)]


def test_a_session_dir_cwd_is_kept_on_resume(env: Env) -> None:
    sid = exec_session(env, "--cwd", "session-dir")["csession_id"]
    work_dir = (env.state / "sessions" / sid / "work").resolve()
    assert env.run_row(sid)["cwd"] == str(work_dir)
    proc, _out = resume(env, sid, "--cwd", "session-dir")
    assert proc.returncode == 0, proc.stderr
    assert env.run_row(sid, 2)["cwd"] == str(work_dir)


def test_a_git_directory_that_appeared_fails_the_resume(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    assert env.rows("SELECT git_dir FROM sessions") == [("none",)]
    git_env = {**env.environ(), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(env.work)], env=git_env, check=True, timeout=30)
    proc, out = resume(env, sid)
    assert_not_started(proc, out, "git_dir_changed")
    assert env.rows("SELECT git_dir FROM sessions") == [("none",)]  # never overwritten


def test_a_carried_plugin_dir_that_no_longer_exists_fails_the_launch(env: Env) -> None:
    plugin = env.tmp_path / "plugin"
    plugin.mkdir()
    sid = exec_session(env, "--plugin-dir", "../plugin")["csession_id"]
    # Stored absolute: a later run carries it from any cwd.
    assert spec(env, sid, 1)["plugin_dir"] == [str(plugin)]
    elsewhere = env.tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc, _out = resume(env, sid, cwd=elsewhere)
    assert proc.returncode == 0, proc.stderr
    assert flag_value(starts(env)[-1], "--plugin-dir") == [str(plugin)]
    plugin.rmdir()
    proc, out = resume(env, sid)
    assert_not_started(proc, out, "plugin_dir_missing")
    # --reset plugin-dir drops it.
    proc, _out = resume(env, sid, "--reset", "plugin-dir")
    assert proc.returncode == 0, proc.stderr
    assert "--plugin-dir" not in starts(env)[-1]["flags"]


# A fresh conversation, and the carry-over of a not-started run


def test_a_session_without_a_claude_id_resumes_as_a_fresh_conversation(env: Env) -> None:
    env.scenario([{"do": "exit", "code": 0}])  # no SessionStart: no Claude id
    first = exec_session(env)
    sid = first["csession_id"]
    assert (first["outcome"], first["end_reason"]) == ("exited-unreported", "no_session_start")
    assert claude_id(env, sid) is None
    env.scenario(STEPS)
    proc, out = resume(env, sid)
    assert proc.returncode == 0, proc.stderr
    second = starts(env)[-1]
    assert "--resume" not in second["flags"]
    fresh = flag_value(second, "--session-id")
    assert str(uuid.UUID(fresh)) == fresh
    assert any("fresh conversation" in w for w in out["warnings"])
    assert claude_id(env, sid) == fresh


def test_a_not_started_run_carries_its_prompt_and_inputs(env: Env) -> None:
    data = env.tmp_path / "a.txt"
    data.write_text("input data\n", encoding="utf-8")
    missing = env.tmp_path / "gone-plugin"
    first = exec_session(env, "--plugin-dir", str(missing), "--input-file", str(data))
    sid = first["csession_id"]
    assert (first["outcome"], first["end_reason"]) == ("not-started", "plugin_dir_missing")
    # Never delivered: still staged in the private directory.
    staged = env.private_run(sid) / "staged"
    assert (staged / "prompt.md").read_text() == "Do the task.\n"
    (env.tmp_path / "prompt.md").unlink()
    data.unlink()
    proc, out = resume(env, sid, "--reset", "plugin-dir")
    assert proc.returncode == 0, proc.stderr
    session = env.state / "sessions" / sid
    assert (session / "prompts" / "2.md").read_text() == "Do the task.\n"
    assert (session / "inputs" / "2" / "a.txt").read_text() == "input data\n"
    assert starts(env)[-1]["prompt"].endswith(f"Read and follow {session / 'prompts' / '2.md'}")
    row = env.run_row(sid, 2)
    assert row["has_prompt"] == 1
    assert json.loads(row["invocation_json"])["input_files"] == ["a.txt"]
    run_json = json.loads((session / "run.json").read_text())
    assert run_json["input_files"] == ["a.txt"]
    assert "fresh conversation" in " ".join(out["warnings"])


def test_a_prompt_or_input_given_now_replaces_the_carried_one(env: Env) -> None:
    data = env.tmp_path / "a.txt"
    data.write_text("old\n", encoding="utf-8")
    first = exec_session(env, "--plugin-dir", str(env.tmp_path / "nope"), "--input-file", str(data))
    sid = first["csession_id"]
    other = env.tmp_path / "b.txt"
    other.write_text("new\n", encoding="utf-8")
    proc, _out = resume(env, sid, "--reset", "plugin-dir", "--input-file", str(other))
    assert proc.returncode == 0, proc.stderr
    inputs = env.state / "sessions" / sid / "inputs" / "2"
    assert sorted(p.name for p in inputs.iterdir()) == ["b.txt"]
    # The prompt was not given now: it is still carried.
    assert (env.state / "sessions" / sid / "prompts" / "2.md").read_text() == "Do the task.\n"


def test_a_delivered_prompt_is_never_carried(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    proc, _out = resume(env, sid)
    assert proc.returncode == 0, proc.stderr
    assert env.run_row(sid, 2)["has_prompt"] == 0
    assert not (env.state / "sessions" / sid / "prompts").exists()


def test_a_missing_undelivered_prompt_exits_2(env: Env) -> None:
    first = exec_session(env, "--plugin-dir", str(env.tmp_path / "nope"))
    sid = first["csession_id"]
    (env.private_run(sid) / "staged" / "prompt.md").unlink()
    proc, out = resume(env, sid, "--reset", "plugin-dir")
    assert proc.returncode == 2
    assert "--prompt-file" in out["error"]["detail"]
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]


def test_a_symlinked_undelivered_prompt_is_refused(env: Env) -> None:
    first = exec_session(env, "--plugin-dir", str(env.tmp_path / "nope"))
    sid = first["csession_id"]
    staged = env.private_run(sid) / "staged" / "prompt.md"
    secret = env.tmp_path / "secret.txt"
    secret.write_text("not for the agent\n", encoding="utf-8")
    staged.unlink()
    staged.symlink_to(secret)
    proc, _out = resume(env, sid, "--reset", "plugin-dir")
    assert proc.returncode == 2
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]


# Refusals


def test_a_pending_run_is_live_and_refuses_a_resume(env: Env) -> None:
    sid = env.started()["csession_id"]
    proc, out = resume(env, sid)
    assert proc.returncode == 20
    assert (out["error"]["code"], out["error"]["reason"]) == ("refused", "live")
    assert "--fork still works" in out["error"]["message"]
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]
    # The dry run reports the same refusal and allocates nothing.
    proc, out = resume(env, sid, "--dry-run")
    assert proc.returncode == 20 and out["would_refuse"] == "live"
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]


def test_an_expired_pending_run_is_finalized_and_its_prompt_carried(env: Env) -> None:
    env.write_config("lost_grace: 0.2s\n")
    sid = env.started()["csession_id"]
    import time

    time.sleep(0.5)
    env.write_config()
    proc, _out = resume(env, sid, "--mode", "headless")
    assert proc.returncode == 0, proc.stderr
    row = env.run_row(sid, 1)
    assert (row["outcome"], row["end_reason"]) == ("not-started", "never_adopted")
    assert env.rows("SELECT cause FROM events WHERE final = 1 AND run_no = 1") == [("liveness",)]
    assert (env.state / "sessions" / sid / "prompts" / "2.md").read_text() == "Do the task.\n"


def test_a_live_session_refuses_a_second_resume(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    gate = env.tmp_path / "running"
    env.scenario(
        [START, {"do": "touch", "path": str(gate)}, {"do": "sleep", "seconds": 3}, DONE, STOP]
        + [result_step()]
    )
    first = subprocess.Popen(
        [*claude_session_cmd(), "resume", sid, "--json", "--config", str(env.config)],
        cwd=env.work,
        env=env.environ(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = 60.0
        import time

        start = time.monotonic()
        while not gate.exists():
            assert time.monotonic() - start < deadline, "the first resume never started"
            time.sleep(0.05)
        proc, out = resume(env, sid)
        assert proc.returncode == 20 and out["error"]["reason"] == "live"
        # A fork is not refused for a live source.
        env.scenario(STEPS)
        proc, out = resume(env, sid, "--fork")
        assert proc.returncode == 0, proc.stderr
        assert out["csession_id"] != sid
    finally:
        stdout, _ = first.communicate(timeout=90)
    assert first.returncode == 0, stdout
    assert env.rows("SELECT COUNT(*) FROM runs WHERE csession_id = ?", (sid,)) == [(2,)]


def test_concurrent_resumes_have_exactly_one_winner(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    procs = [
        subprocess.Popen(
            [*claude_session_cmd(), "resume", sid, "--json", "--config", str(env.config)],
            cwd=env.work,
            env=env.environ(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    results = []
    for proc in procs:
        stdout, _ = proc.communicate(timeout=120)
        results.append((proc.returncode, json.loads(stdout)))
    winners = [out for code, out in results if code == 0]
    losers = [out for code, out in results if code == 20]
    # A loser that lost the race to the winner's allocation is refused live; one that came
    # after the winner ended resumes it again (run 3, 4, ...): never two runs at once.
    assert winners and len(winners) + len(losers) == 4, results
    assert all(out["error"]["reason"] == "live" for out in losers)
    runs = sorted(out["run"] for out in winners)
    assert runs == list(range(2, 2 + len(winners)))
    rows = env.rows("SELECT run_no, ended_at FROM runs WHERE csession_id = ?", (sid,))
    assert len(rows) == 1 + len(winners) and all(ended for _run, ended in rows)


def test_a_tainted_session_refuses_a_trusted_run(env: Env) -> None:
    first = exec_session(env, "--reads-third-party", "--sandbox", "off")
    sid = first["csession_id"]
    assert env.rows("SELECT tainted FROM sessions") == [(1,)]
    # Same kind: --reads-third-party is carried, so the run is third-party-reading (a
    # carried --sandbox off never beats its implication: given again with it here).
    proc, _out = resume(env, sid, "--reads-third-party", "--sandbox", "off")
    assert proc.returncode == 0, proc.stderr
    assert env.run_row(sid, 2)["reads_third_party"] == 1
    # Another kind carries nothing: a trusted run of a tainted session is refused.
    proc, out = resume(env, sid, "--kind", "draft-ticket")
    assert proc.returncode == 20 and out["error"]["reason"] == "tainted"
    proc, out = resume(env, sid, "--kind", "draft-ticket", "--fork")
    assert proc.returncode == 20 and out["error"]["reason"] == "tainted"
    proc, out = resume(env, sid, "--kind", "draft-ticket", "--dry-run")
    assert proc.returncode == 20 and out["would_refuse"] == "tainted"
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(1,)]
    assert env.rows("SELECT COUNT(*) FROM runs") == [(2,)]


def test_a_carried_sandbox_never_overrides_the_reads_third_party_implication(
    env: Env,
) -> None:
    # Given together, --sandbox off beats the implication (and is audited as lowered).
    sid = exec_session(env, "--reads-third-party", "--sandbox", "off")["csession_id"]
    assert json.loads(env.run_row(sid)["resolved_json"])["profile"]["sandbox"] == "off"
    # Carried, it never does: the resumed run is back at sandbox required.
    proc, out = resume(env, sid, "--dry-run")
    assert out["profile"]["sandbox"] == "required"
    assert out["origins"]["sandbox"] == "flag-implied"
    assert out["origins"]["reads_third_party"] == "previous_run"
    assert any("overridden by --reads-third-party" in w for w in out["warnings"])
    # Nor does a --sandbox given now without --reads-third-party in the same call.
    proc, out = resume(env, sid, "--dry-run", "--sandbox", "off")
    assert out["profile"]["sandbox"] == "required"


# The permission profile snapshot


def test_a_profile_file_is_carried_as_its_snapshot(env: Env) -> None:
    profile = env.tmp_path / "p.json"
    profile.write_text(json.dumps({"permissions": {"allow": ["Read"]}}), encoding="utf-8")
    sid = exec_session(env, "--permission-profile", str(profile))["csession_id"]
    snapshot = env.private_run(sid) / "permission-profile.json"
    assert env.run_row(sid)["permission_profile_snapshot"] == str(snapshot)
    label = env.run_row(sid)["permission_profile"]
    profile.unlink()  # never re-read
    proc, _out = resume(env, sid)
    assert proc.returncode == 0, proc.stderr
    assert spec(env, sid, 2)["permission_profile"] == str(snapshot)
    assert spec(env, sid, 2)["permission_profile_label"] == label
    assert env.run_row(sid, 2)["permission_profile"] == label
    # An edited snapshot fails the next launch.
    snapshot2 = env.private_run(sid, 2) / "permission-profile.json"
    snapshot2.write_text(json.dumps({"permissions": {"allow": ["Bash"]}}), encoding="utf-8")
    proc, out = resume(env, sid)
    assert_not_started(proc, out, "invalid_permission_profile")
    # --reset permission-profile goes back to the kind's profile.
    proc, _out = resume(env, sid, "--reset", "permission-profile")
    assert proc.returncode == 0, proc.stderr
    assert env.run_row(sid, 4)["permission_profile"] == "default"


# The previous run's outbox


def test_the_previous_runs_outbox_requests_are_discarded(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    request = outbox.new_request_id()
    planted = env.state / "sessions" / sid / "outbox" / f"{request}.json"
    planted.write_text(json.dumps({"v": 1, "run": 1}), encoding="utf-8")
    proc, _out = resume(env, sid)
    assert proc.returncode == 0, proc.stderr
    assert not planted.exists()
    assert env.rows("SELECT run_no, result FROM requests WHERE request_id = ?", (request,)) == [
        (1, "discarded")
    ]
    records = [r for r in read_jsonl(env.state / "log.jsonl") if r["event"] == "outbox_discarded"]
    assert [(r["run"], request in r["message"]) for r in records] == [(1, True)]
    for record in records:
        validate(record, "claude-session/log.json")


# Fork


def test_fork_creates_a_new_session_from_the_source_conversation(env: Env) -> None:
    data = env.tmp_path / "a.txt"
    data.write_text("input data\n", encoding="utf-8")
    sid = exec_session(env, "--input-file", str(data), "--label", "x=1")["csession_id"]
    source_claude = claude_id(env, sid)
    proc, out = resume(env, sid, "--fork", "--label", "y=2")
    assert proc.returncode == 0, proc.stderr
    fork = out["csession_id"]
    assert fork != sid and out["forked_from"] == sid and out["run"] == 1
    record = starts(env)[-1]
    assert flag_value(record, "--resume") == source_claude
    assert record["flags"].get("--fork-session") is True
    new_id = flag_value(record, "--session-id")
    assert new_id != source_claude and claude_id(env, fork) == new_id
    # Inputs copied; labels: kind= plus the given ones only; the source is unchanged.
    copied = env.state / "sessions" / fork / "inputs" / "1" / "a.txt"
    assert copied.read_text() == "input data\n"
    assert env.rows(
        "SELECT key, value FROM labels WHERE csession_id = ? ORDER BY key", (fork,)
    ) == [
        ("kind", "implement"),
        ("y", "2"),
    ]
    assert env.rows("SELECT COUNT(*) FROM runs WHERE csession_id = ?", (sid,)) == [(1,)]
    assert env.rows("SELECT forked_from FROM sessions WHERE csession_id = ?", (fork,)) == [(sid,)]
    assert claude_id(env, sid) == source_claude
    assert record["cwd"] == str(env.work.resolve())


def test_a_fork_input_given_now_is_placed_beside_the_copied_ones(env: Env) -> None:
    data = env.tmp_path / "a.txt"
    data.write_text("source\n", encoding="utf-8")
    sid = exec_session(env, "--input-file", str(data))["csession_id"]
    other = env.tmp_path / "b.txt"
    other.write_text("fork\n", encoding="utf-8")
    proc, out = resume(env, sid, "--fork", "--input-file", str(other))
    assert proc.returncode == 0, proc.stderr
    inputs = env.state / "sessions" / out["csession_id"] / "inputs" / "1"
    assert sorted(p.name for p in inputs.iterdir()) == ["a.txt", "b.txt"]
    invocation = json.loads(env.run_row(out["csession_id"])["invocation_json"])
    assert invocation["input_files"] == ["b.txt"]
    # The inputs the fork copied from its source are recorded on its run 1.
    assert invocation["copied_inputs"] == ["inputs/1/a.txt"]
    # A name the copy already holds is refused before anything is allocated.
    proc, out = resume(env, sid, "--fork", "--input-file", str(data))
    assert proc.returncode == 2 and "inputs/1/" in out["error"]["message"]
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(2,)]


@pytest.mark.parametrize("plant", ["symlink", "hardlink", "fifo", "directory"])
def test_the_fork_input_copy_refuses_planted_entries(env: Env, plant: str) -> None:
    data = env.tmp_path / "a.txt"
    data.write_text("input\n", encoding="utf-8")
    sid = exec_session(env, "--input-file", str(data))["csession_id"]
    run_inputs = env.state / "sessions" / sid / "inputs" / "1"
    secret = env.tmp_path / "secret.txt"
    secret.write_text("not for the fork\n", encoding="utf-8")
    target = run_inputs / "evil.txt"
    if plant == "symlink":
        target.symlink_to(secret)
    elif plant == "hardlink":
        os.link(secret, target)
    elif plant == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    proc, out = resume(env, sid, "--fork")
    assert_not_started(proc, out, "session_dir_unsafe")
    fork = out["csession_id"]
    assert not (env.state / "sessions" / fork / "inputs" / "1" / "evil.txt").exists()
    assert len(starts(env)) == 1  # claude never started for the fork


def test_a_fork_may_run_in_another_cwd(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    other = env.tmp_path / "checkout"
    other.mkdir()
    proc, out = resume(env, sid, "--fork", "--cwd", str(other))
    assert proc.returncode == 0, proc.stderr
    assert starts(env)[-1]["cwd"] == str(other.resolve())
    assert env.run_row(out["csession_id"])["cwd"] == str(other.resolve())
    proc, out = resume(env, sid, "--fork", "--cwd", str(env.tmp_path / "missing"))
    assert proc.returncode == 2


def test_a_fork_of_a_session_dir_cwd_needs_cwd(env: Env) -> None:
    sid = exec_session(env, "--cwd", "session-dir")["csession_id"]
    proc, out = resume(env, sid, "--fork")
    assert_not_started(proc, out, "fork_cwd_unsupported")
    proc, out = resume(env, sid, "--fork", "--cwd", "session-dir")
    assert proc.returncode == 0, proc.stderr
    fork_work = (env.state / "sessions" / out["csession_id"] / "work").resolve()
    assert env.run_row(out["csession_id"])["cwd"] == str(fork_work)


def test_a_fork_copies_the_taint(env: Env) -> None:
    sid = exec_session(env, "--reads-third-party", "--sandbox", "off")["csession_id"]
    proc, out = resume(env, sid, "--fork", "--reads-third-party", "--sandbox", "off")
    assert proc.returncode == 0, proc.stderr
    fork = out["csession_id"]
    assert env.rows(
        "SELECT tainted, tainted_by_run FROM sessions WHERE csession_id = ?", (fork,)
    ) == [(1, None)]


def test_a_fork_of_a_not_started_source_carries_its_prompt_and_inputs(env: Env) -> None:
    data = env.tmp_path / "a.txt"
    data.write_text("undelivered\n", encoding="utf-8")
    sid = exec_session(env, "--plugin-dir", str(env.tmp_path / "nope"), "--input-file", str(data))[
        "csession_id"
    ]
    proc, out = resume(env, sid, "--fork", "--reset", "plugin-dir")
    assert proc.returncode == 0, proc.stderr
    fork = env.state / "sessions" / out["csession_id"]
    assert (fork / "prompt.md").read_text() == "Do the task.\n"
    assert (fork / "inputs" / "1" / "a.txt").read_text() == "undelivered\n"
    assert any("fresh conversation" in w for w in out["warnings"])


# start --resume


def test_start_resume_creates_a_pending_run_that_adoption_runs(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    source_claude = claude_id(env, sid)
    proc, out = run_cs(env, ["start", "--json", "--resume", sid, "--model", "m3"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    validate(out, "claude-session/cli/start.json")
    assert (out["csession_id"], out["run"], out["forked_from"]) == (sid, 2, None)
    row = env.run_row(sid, 2)
    assert row["wrapper_pid"] is None and row["ended_at"] is None
    assert row["mode"] == "headless"  # the previous run's mode
    # Pending: a second resume is refused live.
    proc2, out2 = resume(env, sid)
    assert proc2.returncode == 20 and out2["error"]["reason"] == "live"
    proc, outcome = env.adopt(sid)
    assert proc.returncode == 0, proc.stderr
    assert (outcome["run"], outcome["outcome"]) == (2, "done")
    record = starts(env)[-1]
    assert flag_value(record, "--resume") == source_claude
    assert flag_value(record, "--model") == "m3"


def test_start_resume_fork_allocates_the_fork(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = run_cs(env, ["start", "--json", "--resume", sid, "--fork"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    validate(out, "claude-session/cli/start.json")
    fork = out["csession_id"]
    assert fork != sid and out["forked_from"] == sid and out["run"] == 1
    proc, outcome = env.adopt(fork)
    assert proc.returncode == 0, proc.stderr
    record = starts(env)[-1]
    assert record["flags"].get("--fork-session") is True
    assert flag_value(record, "--resume") == claude_id(env, sid)


def test_start_resume_dry_run_allocates_nothing(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = run_cs(env, ["start", "--json", "--resume", sid, "--dry-run"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    validate(out, "claude-session/cli/exec-dry-run.json")
    assert (out["csession_id"], out["run"], out["fork"], out["would_refuse"]) == (
        sid,
        2,
        False,
        None,
    )
    assert "--resume" in out["claude_argv"]
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]


# Dry run


def test_resume_dry_run_reports_the_carried_plan(env: Env) -> None:
    sid = exec_session(env, "--model", "m1", "--label", "a=1")["csession_id"]
    proc, out = resume(env, sid, "--dry-run")
    assert proc.returncode == 0, proc.stdout
    assert (out["csession_id"], out["run"], out["fork"], out["forked_from"]) == (
        sid,
        2,
        False,
        None,
    )
    assert out["profile"]["model"] == "m1" and out["origins"]["model"] == "previous_run"
    assert out["labels"] == ["a=1", "kind=implement"]
    assert out["cwd"] == str(env.work.resolve())
    argv = out["claude_argv"]
    assert argv[argv.index("--resume") + 1] == claude_id(env, sid)
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]
    # A fork's plan has no id yet.
    proc, out = resume(env, sid, "--dry-run", "--fork")
    assert proc.returncode == 0, proc.stdout
    assert (out["csession_id"], out["run"], out["fork"], out["forked_from"]) == (
        None,
        1,
        True,
        sid,
    )
    assert out["labels"] == ["kind=implement"]
    assert "--fork-session" in out["claude_argv"]
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(1,)]


def test_resume_dry_run_reports_a_git_dir_change(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    git_env = {**env.environ(), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(env.work)], env=git_env, check=True, timeout=30)
    proc, out = resume(env, sid, "--dry-run")
    assert proc.returncode == 12 and out["would_fail"] == "git_dir_changed"


def test_resume_json_output_validates_for_a_launch_failure(env: Env, tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    sid = exec_session(env, "--plugin-dir", str(plugin))["csession_id"]
    shutil.rmtree(plugin)
    proc, out = resume(env, sid)
    assert_not_started(proc, out, "plugin_dir_missing")
    assert out["run"] == 2 and out["cost_usd"] is None


def test_an_interactive_resume_without_a_terminal_exits_2(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = resume(env, sid, "--mode", "interactive")
    assert proc.returncode == 2 and "terminal" in out["error"]["message"]
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]


def test_override_escalation_with_fork_exits_2(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = resume(env, sid, "--fork", "--override-escalation", "--actor", "human:me")
    assert proc.returncode == 2
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(1,)]


# Later runs of a fork, the fork's taint and cwd, and repeated forks


def write_input(env: Env, name: str, text: str) -> Path:
    path = env.tmp_path / "in" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def two_run_fork(env: Env) -> tuple[str, str]:
    """A source whose runs 1 and 2 had inputs (inputs/1/a.txt, inputs/2/b.txt), and its
    fork (which copied both)."""
    sid = exec_session(env, "--input-file", str(write_input(env, "a.txt", "a\n")))["csession_id"]
    proc, _out = resume(env, sid, "--input-file", str(write_input(env, "b.txt", "b\n")))
    assert proc.returncode == 0, proc.stderr
    proc, out = resume(env, sid, "--fork")
    assert proc.returncode == 0, proc.stderr
    return sid, out["csession_id"]


def test_a_later_run_of_a_fork_merges_into_its_copied_inputs(env: Env) -> None:
    _sid, fork = two_run_fork(env)
    inputs = env.state / "sessions" / fork / "inputs"
    assert (inputs / "2" / "b.txt").read_text() == "b\n"
    # Run 2 of the fork goes into the copied inputs/2/: a name already there exits 2
    # before anything is allocated (resume and start --resume alike) ...
    clash = write_input(env, "b.txt", "other\n")
    proc, out = resume(env, fork, "--input-file", str(clash))
    assert proc.returncode == 2 and "inputs/2/" in out["error"]["message"]
    proc, out = run_cs(env, ["start", "--json", "--resume", fork, "--input-file", str(clash)])
    assert proc.returncode == 2 and out["error"]["code"] == "usage"
    assert env.rows("SELECT COUNT(*) FROM runs WHERE csession_id = ?", (fork,)) == [(1,)]
    # ... and a new name is placed beside the copied one.
    proc, out = resume(env, fork, "--input-file", str(write_input(env, "c.txt", "c\n")))
    assert proc.returncode == 0, proc.stderr
    assert out["run"] == 2
    assert sorted(p.name for p in (inputs / "2").iterdir()) == ["b.txt", "c.txt"]


def test_a_pending_fork_is_refused_at_adoption_when_its_source_became_tainted(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = run_cs(env, ["start", "--json", "--resume", sid, "--fork"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fork = out["csession_id"]
    # The source is tainted after the fork was allocated (and appends to the transcript the
    # fork would resume).
    proc, _out = resume(env, sid, "--reads-third-party", "--sandbox", "off")
    assert proc.returncode == 0, proc.stderr
    assert env.rows("SELECT tainted FROM sessions WHERE csession_id = ?", (sid,)) == [(1,)]
    proc, out = env.adopt(fork, "--dry-run")
    assert proc.returncode == 20 and out["would_refuse"] == "tainted"
    proc, out = env.adopt(fork)
    assert proc.returncode == 20 and out["error"]["reason"] == "tainted"
    row = env.run_row(fork)
    assert (row["outcome"], row["end_reason"]) == ("not-started", "never_adopted")
    assert len(starts(env)) == 2  # the source's two runs; the fork never started
    assert not (env.private_run(fork) / "launch-spec.json").exists()


def resume_in_process(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, Any]]:
    """``cli.main`` of ``resume`` in this process (so a test can patch launch steps)."""
    from claude_session import cli

    monkeypatch.chdir(env.work)
    monkeypatch.setattr(sys, "argv", [str(CLAUDE_SESSION)])
    for key, value in private_home(env.tmp_path).items():
        monkeypatch.setenv(key, value)
    capsys.readouterr()
    code = cli.main(["resume", *argv, "--json", "--config", str(env.config)])
    return code, json.loads(capsys.readouterr().out)


def test_a_fork_whose_source_became_tainted_before_the_spawn_is_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = exec_session(env)["csession_id"]
    real_validate = launch.validate

    def tainting(*args: Any, **kwargs: Any) -> launch.Validated:
        validated = real_validate(*args, **kwargs)
        # Between the fork's allocation and step 7, a third-party-reading run of the source
        # taints it.
        env.write_db(
            "UPDATE sessions SET tainted = 1, tainted_at = '2026-01-01T00:00:00Z' "
            "WHERE csession_id = ?",
            (sid,),
        )
        return validated

    monkeypatch.setattr(launch, "validate", tainting)
    code, out = resume_in_process(env, monkeypatch, capsys, sid, "--fork")
    assert code == 20 and out["error"]["reason"] == "tainted", out
    (fork,) = [
        row[0] for row in env.rows("SELECT csession_id FROM sessions WHERE forked_from = ?", (sid,))
    ]
    row = env.run_row(fork)
    assert (row["outcome"], row["end_reason"]) == ("not-started", "launch_interrupted")
    assert "tainted" in row["message"]
    assert len(starts(env)) == 1


def adopt_in_process(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sid: str
) -> tuple[int, dict[str, Any]]:
    """``cli.main`` of the adopting ``exec`` in this process (so a test can patch launch
    steps)."""
    from claude_session import cli

    monkeypatch.chdir(env.work)
    monkeypatch.setattr(sys, "argv", [str(CLAUDE_SESSION)])
    for key, value in private_home(env.tmp_path).items():
        monkeypatch.setenv(key, value)
    capsys.readouterr()
    code = cli.main(["exec", "--session-id", sid, "--json"])
    return code, json.loads(capsys.readouterr().out)


def taint_after_validate(env: Env, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """A third-party-reading run of ``source`` taints it between step 5 and step 7."""
    real_validate = launch.validate

    def tainting(*args: Any, **kwargs: Any) -> launch.Validated:
        validated = real_validate(*args, **kwargs)
        env.write_db(
            "UPDATE sessions SET tainted = 1, tainted_at = '2026-01-01T00:00:00Z' "
            "WHERE csession_id = ?",
            (source,),
        )
        return validated

    monkeypatch.setattr(launch, "validate", tainting)


def test_an_adopted_fork_whose_source_became_tainted_before_the_spawn_is_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = run_cs(env, ["start", "--json", "--resume", sid, "--fork"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fork = out["csession_id"]
    # The source is tainted after the adoption's compare-and-set, before step 7.
    taint_after_validate(env, monkeypatch, sid)
    code, out = adopt_in_process(env, monkeypatch, capsys, fork)
    assert code == 20 and out["error"]["reason"] == "tainted", out
    row = env.run_row(fork)
    assert (row["outcome"], row["end_reason"]) == ("not-started", "launch_interrupted")
    assert "tainted" in row["message"]
    assert len(starts(env)) == 1  # the source's run; the fork never started


def test_an_adopted_repeated_fork_whose_source_became_tainted_before_the_spawn_is_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = exec_session(env, "--cwd", "session-dir")["csession_id"]
    proc, out = resume(env, sid, "--fork")
    assert_not_started(proc, out, "fork_cwd_unsupported")
    fork = out["csession_id"]
    assert claude_id(env, fork) is None
    # start --resume of the fork repeats the fork; its source is tainted before step 7.
    proc, out = run_cs(env, ["start", "--json", "--resume", fork, "--cwd", str(env.work)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    taint_after_validate(env, monkeypatch, sid)
    code, out = adopt_in_process(env, monkeypatch, capsys, fork)
    assert code == 20 and out["error"]["reason"] == "tainted", out
    row = env.run_row(fork, 2)
    assert (row["outcome"], row["end_reason"]) == ("not-started", "launch_interrupted")
    assert len(starts(env)) == 1


def test_a_resume_of_a_fork_that_never_started_repeats_the_fork(env: Env) -> None:
    sid = exec_session(env, "--cwd", "session-dir")["csession_id"]
    source_claude = claude_id(env, sid)
    proc, out = resume(env, sid, "--fork")
    assert_not_started(proc, out, "fork_cwd_unsupported")
    fork = out["csession_id"]
    # Without --cwd the repeated fork follows the fork's cwd rule again.
    proc, out = resume(env, fork)
    assert_not_started(proc, out, "fork_cwd_unsupported")
    other = env.tmp_path / "other"
    other.mkdir()
    proc, out = resume(env, fork, "--cwd", str(other))
    assert proc.returncode == 0, proc.stderr
    assert (out["csession_id"], out["run"]) == (fork, 3)
    record = starts(env)[-1]
    assert flag_value(record, "--resume") == source_claude
    assert record["flags"].get("--fork-session") is True
    new_id = flag_value(record, "--session-id")
    assert new_id != source_claude and claude_id(env, fork) == new_id
    assert record["cwd"] == str(other.resolve())
    # Once the fork has its own conversation, a resume continues it as usual.
    proc, out = resume(env, fork)
    assert proc.returncode == 0, proc.stderr
    record = starts(env)[-1]
    assert flag_value(record, "--resume") == new_id and "--fork-session" not in record["flags"]


def test_a_fork_without_cwd_never_runs_in_another_sessions_work_dir(env: Env) -> None:
    first = exec_session(env, "--cwd", "session-dir")["csession_id"]
    first_work = env.state / "sessions" / first / "work"
    proc, out = resume(env, first, "--fork", "--cwd", str(first_work))
    assert proc.returncode == 0, proc.stderr
    second = out["csession_id"]
    proc, out = resume(env, second, "--fork")
    assert_not_started(proc, out, "fork_cwd_unsupported")


def test_a_trusted_resume_never_continues_in_an_isolated_runs_work_dir(env: Env) -> None:
    sid = exec_session(env, "--kind", "rank")["csession_id"]
    work = (env.state / "sessions" / sid / "work").resolve()
    assert env.run_row(sid)["cwd"] == str(work)
    for extra in ([], ["--dry-run"]):
        proc, out = resume(env, sid, "--kind", "implement", *extra)
        assert proc.returncode == 2 and out["error"]["code"] == "usage"
        assert "--fork --cwd" in out["error"]["detail"]
    proc, out = run_cs(env, ["start", "--json", "--resume", sid, "--kind", "implement"])
    assert proc.returncode == 2
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]
    # An isolated kind may continue there.
    proc, _out = resume(env, sid)
    assert proc.returncode == 0, proc.stderr


def test_an_expired_pending_run_is_not_finalized_by_a_usage_error(env: Env) -> None:
    env.write_config("lost_grace: 0.2s\n")
    sid = env.started()["csession_id"]
    time.sleep(0.5)
    env.write_config()
    (env.private_run(sid) / "staged" / "prompt.md").unlink()
    proc, _out = resume(env, sid, "--mode", "headless")
    assert proc.returncode == 2
    assert env.run_row(sid)["ended_at"] is None


def not_started_source(env: Env, run2_text: str) -> str:
    """A source whose run 1 delivered inputs/1/a.txt ("a") and whose run 2 ended
    not-started with an undelivered a.txt (``run2_text``)."""
    sid = exec_session(env, "--input-file", str(write_input(env, "a.txt", "a\n")))["csession_id"]
    again = write_input(env, "a.txt", run2_text)
    proc, out = resume(
        env, sid, "--input-file", str(again), "--plugin-dir", str(env.tmp_path / "gone")
    )
    assert_not_started(proc, out, "plugin_dir_missing")
    return sid


def test_a_carried_input_identical_to_a_copied_one_is_kept_once(env: Env) -> None:
    sid = not_started_source(env, "a\n")
    proc, out = resume(env, sid, "--fork", "--reset", "plugin-dir")
    assert proc.returncode == 0, proc.stderr
    inputs = env.state / "sessions" / out["csession_id"] / "inputs" / "1"
    assert [p.name for p in inputs.iterdir()] == ["a.txt"]
    assert (inputs / "a.txt").read_text() == "a\n"
    invocation = json.loads(env.run_row(out["csession_id"])["invocation_json"])
    assert invocation["input_files"] == ["a.txt"]


def test_a_carried_input_that_differs_from_a_copied_one_exits_2(env: Env) -> None:
    sid = not_started_source(env, "changed\n")
    proc, out = resume(env, sid, "--fork", "--reset", "plugin-dir")
    assert proc.returncode == 2
    assert "carried from the not-started run 2" in out["error"]["message"]
    assert "--input-file replaces the carried input set" in out["error"]["detail"]
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(1,)]


def test_a_fork_that_failed_its_copy_still_carries_its_prompt(env: Env) -> None:
    sid = exec_session(env, "--input-file", str(write_input(env, "a.txt", "a\n")))["csession_id"]
    secret = write_input(env, "secret.txt", "not for the fork\n")
    (env.state / "sessions" / sid / "inputs" / "1" / "evil.txt").symlink_to(secret)
    prompt = env.tmp_path / "fork-prompt.md"
    prompt.write_text("Fork task.\n", encoding="utf-8")
    proc, out = resume(env, sid, "--fork", "--prompt-file", str(prompt))
    assert_not_started(proc, out, "session_dir_unsafe")
    fork = out["csession_id"]
    prompt.unlink()
    proc, out = resume(env, fork)
    assert proc.returncode == 0, proc.stderr
    placed = env.state / "sessions" / fork / "prompts" / "2.md"
    assert placed.read_text() == "Fork task.\n"


def test_an_unsearchable_source_inputs_dir_is_refused(env: Env) -> None:
    sid = exec_session(env, "--input-file", str(write_input(env, "a.txt", "a\n")))["csession_id"]
    inputs = env.state / "sessions" / sid / "inputs"
    inputs.chmod(0o000)
    try:
        proc, out = resume(env, sid, "--fork", "--dry-run")
        assert proc.returncode == 12 and out["would_fail"] == "session_dir_unsafe"
        proc, out = resume(env, sid, "--fork")
        assert_not_started(proc, out, "session_dir_unsafe")
    finally:
        inputs.chmod(0o700)


def test_the_fork_copy_copies_every_file_and_caps_each(env: Env) -> None:
    files = [str(write_input(env, f"{name}.txt", f"{name}\n")) for name in "abc"]
    argv = [arg for path in files for arg in ("--input-file", path)]
    sid = exec_session(env, *argv)["csession_id"]
    proc, out = resume(env, sid, "--fork")
    assert proc.returncode == 0, proc.stderr
    inputs = env.state / "sessions" / out["csession_id"] / "inputs" / "1"
    assert sorted(p.name for p in inputs.iterdir()) == ["a.txt", "b.txt", "c.txt"]
    # A source file over input_max_bytes is refused.
    (env.state / "sessions" / sid / "inputs" / "1" / "b.txt").chmod(0o600)
    (env.state / "sessions" / sid / "inputs" / "1" / "b.txt").write_bytes(b"x" * 2048)
    env.write_config("input_max_bytes: 1024\n")
    proc, out = resume(env, sid, "--fork")
    assert_not_started(proc, out, "session_dir_unsafe")


def test_a_fork_into_its_sources_cwd_named_by_cwd_checks_the_git_dir(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    git_env = {**env.environ(), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(env.work)], env=git_env, check=True, timeout=30)
    proc, out = resume(env, sid, "--fork", "--cwd", str(env.work), "--dry-run")
    assert proc.returncode == 12 and out["would_fail"] == "git_dir_changed"
    proc, out = resume(env, sid, "--fork", "--cwd", str(env.work))
    assert_not_started(proc, out, "git_dir_changed")


def test_a_fork_dry_run_inspects_the_source_inputs(env: Env) -> None:
    sid = exec_session(env, "--input-file", str(write_input(env, "a.txt", "a\n")))["csession_id"]
    secret = write_input(env, "secret.txt", "not for the fork\n")
    (env.state / "sessions" / sid / "inputs" / "1" / "evil.txt").symlink_to(secret)
    proc, out = resume(env, sid, "--fork", "--dry-run")
    assert proc.returncode == 12 and out["would_fail"] == "session_dir_unsafe"
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(1,)]


def test_an_unusable_stored_plugin_dir_is_recovered_by_reset_or_a_given_one(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    env.write_db(
        "UPDATE runs SET spec_json = ? WHERE csession_id = ?",
        (json.dumps({"plugin_dir": "relative"}), sid),
    )
    proc, out = resume(env, sid)
    assert proc.returncode == 2 and "--reset plugin-dir" in out["error"]["detail"]
    proc, _out = resume(env, sid, "--reset", "plugin-dir")
    assert proc.returncode == 0, proc.stderr
    env.write_db(
        "UPDATE runs SET spec_json = ? WHERE csession_id = ?",
        (json.dumps({"plugin_dir": "relative"}), sid),
    )
    plugin = env.tmp_path / "plugin"
    plugin.mkdir()
    proc, _out = resume(env, sid, "--plugin-dir", str(plugin))
    assert proc.returncode == 0, proc.stderr
    assert flag_value(starts(env)[-1], "--plugin-dir") == [str(plugin)]


def test_the_repo_is_carried_across_a_kind_change_and_into_a_fork(env: Env) -> None:
    repo = "example.com/org/repo"
    sid = exec_session(env, "--repo", repo)["csession_id"]
    proc, _out = resume(env, sid, "--kind", "draft-ticket")
    assert proc.returncode == 0, proc.stderr
    assert env.run_row(sid, 2)["repo"] == repo
    proc, out = resume(env, sid, "--fork")
    assert proc.returncode == 0, proc.stderr
    assert env.run_row(out["csession_id"])["repo"] == repo
