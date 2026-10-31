"""``start`` and ``exec --session-id`` end to end (interface.md, Launch order, start, Flags at
start and adoption): the ``claude-session`` entry point allocates a pending run, and a later
``exec --session-id`` adopts it and launches the fake ``claude`` (``tests/fakebin``).

Adoption is a compare-and-set raced by real concurrent processes here, and it reruns every
launch check against the adopting process's environment: the tests change the cwd, the git
directory, the config and the staged files between ``start`` and adoption.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from claude_session import lifecycle

from .fakeclaude import claude_bin, read_jsonl, write_json
from .test_exec_headless import claude_session_cmd, private_home, validate_exec
from .test_schemas import registry

FAST = "poll_interval: 0.05s\n"
START = {"do": "session_start"}
STOP = {"do": "stop"}
DONE = {"do": "report", "args": ["done", "--summary", "all good"]}


def result_step(text: str = "the result text") -> dict[str, Any]:
    return {"do": "result", "result": text}


def validate(obj: Any, schema_id: str) -> None:
    schema = registry().contents(schema_id)
    validator = Draft202012Validator(schema, registry=registry(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(obj), key=str)
    assert not errors, [e.message for e in errors]


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@dataclass
class Env:
    tmp_path: Path
    state: Path
    work: Path
    config: Path
    log: Path

    def environ(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {**os.environ, **private_home(self.tmp_path), **(extra or {})}

    def cs(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 90,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*claude_session_cmd(), *argv],
            cwd=cwd or self.work,
            env=self.environ(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def write_config(self, text: str = "") -> None:
        self.config.write_text(f"claude_bin: {self.fake}\n{FAST}{text}", encoding="utf-8")

    @property
    def fake(self) -> Path:
        return self.tmp_path / "fakebin" / "claude"

    def scenario(self, steps: list[dict[str, Any]]) -> None:
        scenario = write_json(self.tmp_path / "scenario.json", {"steps": steps})
        claude_bin(self.tmp_path / "fakebin", scenario, self.log)

    def start(
        self,
        args: Sequence[str] = ("--mode", "headless"),
        *,
        prompt: str | None = "Do the task.\n",
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = ["start", "--json", "--config", str(self.config), *args]
        if prompt is not None:
            prompt_file = self.tmp_path / "prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            argv += ["--prompt-file", str(prompt_file)]
        proc = self.cs(argv, env=env, cwd=cwd)
        assert proc.stdout, proc.stderr
        return proc, json.loads(proc.stdout)

    def started(self, *args: str, **kwargs: Any) -> dict[str, Any]:
        proc, out = self.start(args, **kwargs) if args else self.start(**kwargs)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        validate(out, "claude-session/cli/start.json")
        return out

    def adopt(
        self,
        sid: str,
        *extra: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        proc = self.cs(["exec", "--session-id", sid, "--json", *extra], env=env, cwd=cwd)
        assert proc.stdout, proc.stderr
        out = json.loads(proc.stdout)
        if "error" not in out and "dry_run" not in out:
            validate_exec(out)
            assert out["exit_code"] == proc.returncode
        return proc, out

    def run_row(self, sid: str, run: int = 1) -> dict[str, Any]:
        db = sqlite3.connect(f"file:{self.state / 'state.db'}?mode=ro", uri=True)
        try:
            cursor = db.execute(
                "SELECT * FROM runs WHERE csession_id = ? AND run_no = ?", (sid, run)
            )
            names = [c[0] for c in cursor.description]
            row = cursor.fetchone()
            assert row is not None
            return dict(zip(names, row, strict=True))
        finally:
            db.close()

    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        db = sqlite3.connect(f"file:{self.state / 'state.db'}?mode=ro", uri=True)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def write_db(self, sql: str, params: Sequence[Any] = ()) -> None:
        db = sqlite3.connect(str(self.state / "state.db"))
        try:
            with db:
                db.execute(sql, params)
        finally:
            db.close()

    def private_run(self, sid: str, run: int = 1) -> Path:
        return self.state / "private" / sid / "runs" / str(run)


@pytest.fixture
def env(tmp_path: Path, state: Path, work: Path) -> Env:
    e = Env(tmp_path, state, work, tmp_path / "config.yaml", tmp_path / "fake.jsonl")
    e.scenario([START, DONE, STOP, result_step()])
    e.write_config()
    return e


# start


def test_start_creates_a_pending_run_and_prints_the_start_object(env: Env) -> None:
    out = env.started()
    sid = out["csession_id"]
    assert out["argv"][1:] == ["exec", "--session-id", sid]
    assert os.path.isabs(out["argv"][0])
    assert (out["run"], out["forked_from"]) == (1, None)
    assert out["csession_dir"] == str(env.state / "sessions" / sid)
    row = env.run_row(sid)
    assert row["wrapper_pid"] is None and row["ended_at"] is None
    assert row["expires_at"] == out["expires_at"]
    assert row["config_path"] == str(env.config)
    assert row["config_hash"] is not None and len(row["config_hash"]) == 64
    assert row["cwd"] is None  # recorded at adoption
    assert row["permission_profile"] is not None
    assert env.rows("SELECT state FROM sessions") == [("starting",)]
    # The prompt is staged in the private directory, not delivered.
    staged = env.private_run(sid) / "staged"
    assert (staged / "prompt.md").read_text() == "Do the task.\n"
    assert not (env.state / "sessions" / sid / "prompt.md").exists()
    spec = env.private_run(sid) / lifecycle.SPEC_NAME
    assert stat.S_IMODE(spec.stat().st_mode) == 0o600
    assert not env.log.exists()  # claude never started


def test_start_requires_json(env: Env) -> None:
    proc = env.cs(["start", "--config", str(env.config), "--mode", "headless", "--skill", "s"])
    assert proc.returncode == 2
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(0,)]


def test_start_reports_required_env_names_and_inheritance(env: Env) -> None:
    env.write_config(
        "kinds:\n  implement:\n    env:\n      allow: [MY_TOKEN, GH_*]\n      set: {FOO: bar}\n"
    )
    out = env.started("--mode", "headless", env={"ANTHROPIC_API_KEY": "k"})
    names = out["required_env_names"]
    assert names[0] == "ANTHROPIC_API_KEY"
    assert names.index("MY_TOKEN") < names.index("GH_*") < names.index("FOO")
    assert out["env_inherit"] is True
    out = env.started("--mode", "headless", "--env-allowlist-only", "ONLY")
    assert out["env_inherit"] is False
    assert "ONLY" in out["required_env_names"]


def test_a_launch_validation_failure_at_start_is_launch_failed(env: Env) -> None:
    env.config.write_text(f"claude_bin: {env.tmp_path / 'missing'}\n", encoding="utf-8")
    proc, out = env.start()
    assert proc.returncode == 12
    validate(out, "claude-session/cli/error.json")
    error = out["error"]
    assert (error["code"], error["end_reason"], error["run"]) == (
        "launch_failed",
        "claude_not_found",
        1,
    )
    assert error["hint"]
    row = env.run_row(error["csession_id"])
    assert (row["outcome"], row["end_reason"], row["phase"]) == (
        "not-started",
        "claude_not_found",
        "launch",
    )
    assert env.rows("SELECT state FROM sessions") == [("not-started",)]


def test_a_usage_error_at_start_allocates_nothing(env: Env) -> None:
    proc, out = env.start(("--mode", "headless", "--cwd", str(env.tmp_path / "nope")))
    assert proc.returncode == 2 and out["error"]["code"] == "usage"
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(0,)]


def test_start_resume_is_not_implemented_here(env: Env) -> None:
    proc = env.cs(["start", "--json", "--resume", "cs-abcdefgh", "--config", str(env.config)])
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["error"]["detail"] == "not_implemented"


# Adoption


def test_start_then_adopt_runs_the_session_with_the_staged_prompt(env: Env) -> None:
    out = env.started()
    (env.tmp_path / "prompt.md").unlink()  # start staged a copy
    proc, outcome = env.adopt(out["csession_id"])
    assert proc.returncode == 0, proc.stderr
    assert (outcome["outcome"], outcome["phase"]) == ("done", "session")
    sid = out["csession_id"]
    assert (env.state / "sessions" / sid / "prompt.md").read_text() == "Do the task.\n"
    assert list((env.private_run(sid) / "staged").iterdir()) == []  # moved into place
    row = env.run_row(sid)
    assert row["wrapper_pid"] is not None
    assert row["cwd"] == str(env.work.resolve())


def test_a_finished_run_cannot_be_adopted_again(env: Env) -> None:
    sid = env.started()["csession_id"]
    assert env.adopt(sid)[0].returncode == 0
    proc, out = env.adopt(sid)
    assert proc.returncode == 20
    assert (out["error"]["code"], out["error"]["reason"]) == ("refused", "expired")


def test_concurrent_adopters_have_exactly_one_winner(env: Env) -> None:
    gate = env.tmp_path / "running"
    env.scenario(
        [START, {"do": "touch", "path": str(gate)}, {"do": "sleep", "seconds": 2}, DONE, STOP]
        + [result_step()]
    )
    sid = env.started()["csession_id"]
    procs = [
        subprocess.Popen(
            [*claude_session_cmd(), "exec", "--session-id", sid, "--json"],
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
        stdout, _ = proc.communicate(timeout=90)
        results.append((proc.returncode, json.loads(stdout)))
    codes = sorted(code for code, _ in results)
    assert codes == [0, 20, 20, 20], results
    reasons = {out["error"]["reason"] for code, out in results if code == 20}
    # A loser that ran before the winner ended sees it adopted; one after it, final.
    assert reasons <= {"already_adopted", "expired"}
    assert len({entry["pid"] for entry in read_jsonl(env.log)}) == 1  # one claude started


def test_an_expired_pending_run_is_finalized_never_adopted_and_refused(env: Env) -> None:
    env.write_config("lost_grace: 0.2s\n")
    sid = env.started()["csession_id"]
    time.sleep(0.5)
    proc, out = env.adopt(sid)
    assert proc.returncode == 20
    assert out["error"]["reason"] == "expired"
    row = env.run_row(sid)
    assert (row["outcome"], row["end_reason"], row["exit_code"]) == (
        "not-started",
        "never_adopted",
        12,
    )
    assert row["wrapper_pid"] is None
    assert env.rows("SELECT cause FROM events WHERE final = 1") == [("adopt",)]
    assert not env.log.exists()


def test_unknown_and_forgotten_sessions_exit_10(env: Env) -> None:
    proc, out = env.adopt("cs-zzzzzzzz")
    assert proc.returncode == 10 and out["error"]["code"] == "not_found"
    sid = env.started()["csession_id"]
    env.write_db("UPDATE sessions SET forgotten_at = '2026-01-01T00:00:00Z'")
    proc, out = env.adopt(sid)
    assert proc.returncode == 10
    assert env.run_row(sid)["wrapper_pid"] is None


def test_adoption_accepts_a_short_id_prefix(env: Env) -> None:
    sid = env.started()["csession_id"]
    proc, outcome = env.adopt(sid.removeprefix("cs-")[:5].upper())
    assert proc.returncode == 0 and outcome["csession_id"] == sid


def test_an_outcome_file_given_at_adoption_replaces_the_stored_path(env: Env) -> None:
    sid = env.started("--mode", "headless", "--outcome-file", str(env.tmp_path / "a.json"))[
        "csession_id"
    ]
    assert env.run_row(sid)["outcome_file"] == str(env.tmp_path / "a.json")
    proc, _ = env.adopt(sid, "--outcome-file", "b.json")
    assert proc.returncode == 0
    assert env.run_row(sid)["outcome_file"] == str(env.work / "b.json")


# Re-validation at adoption


def assert_not_started(proc: subprocess.CompletedProcess[str], out: dict[str, Any], reason: str):
    assert proc.returncode == 12, proc.stdout + proc.stderr
    assert (out["phase"], out["outcome"], out["end_reason"], out["state"]) == (
        "launch",
        "not-started",
        reason,
        "not-started",
    )


def test_a_removed_cwd_fails_the_adoption(env: Env, tmp_path: Path) -> None:
    sid = env.started()["csession_id"]
    env.work.rename(tmp_path / "moved")
    proc, out = env.adopt(sid, cwd=tmp_path)
    assert_not_started(proc, out, "unsafe_cwd")
    assert env.rows("SELECT cause FROM events WHERE final = 1") == [("adopt",)]
    assert not env.log.exists()


def test_a_cwd_swapped_for_a_symlink_fails_the_adoption(env: Env, tmp_path: Path) -> None:
    sid = env.started()["csession_id"]
    other = tmp_path / "other"
    other.mkdir()
    env.work.rename(tmp_path / "moved")
    env.work.symlink_to(other)
    proc, out = env.adopt(sid, cwd=tmp_path)
    assert_not_started(proc, out, "unsafe_cwd")
    assert "resolves to another directory" in out["message"]


def test_a_git_directory_that_appeared_fails_the_adoption(env: Env) -> None:
    sid = env.started()["csession_id"]
    git_env = {**env.environ(), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(env.work)], env=git_env, check=True, timeout=30)
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "git_dir_changed")


def test_an_invalid_config_at_adoption_ends_the_run_config_invalid(env: Env) -> None:
    sid = env.started()["csession_id"]
    env.config.write_text("lost_grace: [not, a, duration]\n", encoding="utf-8")
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "config_invalid")


def test_a_deleted_config_at_adoption_ends_the_run_config_invalid(env: Env) -> None:
    sid = env.started()["csession_id"]
    env.config.unlink()
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "config_invalid")


def test_a_changed_config_warns_and_its_claude_bin_is_used(env: Env) -> None:
    sid = env.started()["csession_id"]
    env.config.write_text(f"claude_bin: {env.tmp_path / 'gone'}\n", encoding="utf-8")
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "claude_not_found")
    assert any("config changed since start" in w for w in out["warnings"])


def test_a_changed_config_keeps_the_frozen_launch_settings(env: Env) -> None:
    env.scenario(
        [
            START,
            {"do": "bash", "command": 'printf "%s" "$FROZEN" > "$CSESSION_DIR/artifacts/v.txt"'},
            DONE,
            STOP,
            result_step(),
        ]
    )
    env.write_config("kinds:\n  implement:\n    env:\n      set: {FROZEN: at-start}\n")
    sid = env.started()["csession_id"]
    env.write_config("kinds:\n  implement:\n    env:\n      set: {FROZEN: changed}\n")
    proc, out = env.adopt(sid)
    assert proc.returncode == 0, proc.stderr
    assert any("config changed since start" in w for w in out["warnings"])
    text = (env.state / "sessions" / sid / "artifacts" / "v.txt").read_text()
    assert text == "at-start"


def test_the_session_environment_comes_from_the_adopting_process(env: Env) -> None:
    env.scenario(
        [
            START,
            {"do": "bash", "command": 'printf "%s" "$MY_VAR" > "$CSESSION_DIR/artifacts/v.txt"'},
            DONE,
            STOP,
            result_step(),
        ]
    )
    env.write_config("kinds:\n  implement:\n    env:\n      allow: [MY_VAR]\n")
    sid = env.started(env={"MY_VAR": "from-start"})["csession_id"]
    proc, _ = env.adopt(sid, env={"MY_VAR": "from-adopter"})
    assert proc.returncode == 0, proc.stderr
    assert (env.state / "sessions" / sid / "artifacts" / "v.txt").read_text() == "from-adopter"


def test_a_tampered_staging_area_fails_the_adoption(env: Env) -> None:
    sid = env.started()["csession_id"]
    (env.private_run(sid) / "staged" / "extra").write_text("x")
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "session_dir_unsafe")


def test_a_missing_launch_spec_fails_the_adoption(env: Env) -> None:
    sid = env.started()["csession_id"]
    (env.private_run(sid) / lifecycle.SPEC_NAME).unlink()
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "session_dir_unsafe")


def test_a_symlinked_launch_spec_fails_the_adoption(env: Env) -> None:
    sid = env.started()["csession_id"]
    spec = env.private_run(sid) / lifecycle.SPEC_NAME
    copy = env.tmp_path / "spec.json"
    copy.write_bytes(spec.read_bytes())
    spec.unlink()
    spec.symlink_to(copy)
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "session_dir_unsafe")


def test_a_corrupt_launch_spec_fails_the_adoption(env: Env) -> None:
    sid = env.started()["csession_id"]
    spec = env.private_run(sid) / lifecycle.SPEC_NAME
    data = json.loads(spec.read_text())
    data["config"]["summary_max_bytes"] = -1
    spec.write_text(json.dumps(data))
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "session_dir_unsafe")


def test_an_interactive_run_adopted_without_a_tty_ends_no_tty(env: Env) -> None:
    sid = env.started("--mode", "interactive")["csession_id"]
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "no_tty")
    assert not env.log.exists()


def test_inputs_are_staged_and_placed(env: Env) -> None:
    env.scenario([START, DONE, STOP, result_step()])
    data = env.tmp_path / "data.txt"
    data.write_text("input body")
    sid = env.started("--mode", "headless", "--input-file", str(data))["csession_id"]
    data.unlink()
    proc, _ = env.adopt(sid)
    assert proc.returncode == 0, proc.stderr
    assert (env.state / "sessions" / sid / "inputs" / "1" / "data.txt").read_text() == "input body"


def test_a_lowered_sandbox_is_audited_at_adoption_not_at_start(env: Env) -> None:
    out = env.started("--mode", "headless", "--reads-third-party", "--sandbox", "off")
    assert any("sandbox_lowered" in w or "sandbox" in w for w in out["warnings"])
    assert read_jsonl(env.state / "audit.jsonl") == []
    proc, _ = env.adopt(out["csession_id"])
    assert proc.returncode == 0, proc.stderr
    records = read_jsonl(env.state / "audit.jsonl")
    assert [r["action"] for r in records] == ["sandbox_lowered"]
    assert records[0]["target"] == out["csession_id"]


# The adoption dry run


def test_the_adoption_dry_run_inspects_without_adopting(env: Env) -> None:
    sid = env.started()["csession_id"]
    proc, plan = env.adopt(sid, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    validate(plan, "claude-session/cli/exec-dry-run.json")
    assert (plan["csession_id"], plan["run"], plan["would_refuse"], plan["would_fail"]) == (
        sid,
        1,
        None,
        None,
    )
    assert plan["config_hash_changed"] is False
    assert plan["has_prompt"] is True
    assert plan["cwd"] == str(env.work.resolve())
    assert str(env.state / "sessions" / sid) in json.dumps(plan["settings"])
    assert env.run_row(sid)["wrapper_pid"] is None
    assert not env.log.exists()


def test_the_adoption_dry_run_reports_refusals_and_changes(env: Env) -> None:
    env.write_config("lost_grace: 0.2s\n")
    sid = env.started()["csession_id"]
    env.write_config("lost_grace: 0.3s\n")
    time.sleep(0.5)
    proc, plan = env.adopt(sid, "--dry-run")
    assert proc.returncode == 20
    validate(plan, "claude-session/cli/exec-dry-run.json")
    assert plan["would_refuse"] == "expired"
    assert plan["config_hash_changed"] is True
    assert env.run_row(sid)["ended_at"] is None  # a dry run never finalizes


def test_the_adoption_dry_run_reports_a_failed_check(env: Env, tmp_path: Path) -> None:
    sid = env.started()["csession_id"]
    env.work.rename(tmp_path / "moved")
    proc, plan = env.adopt(sid, "--dry-run", cwd=tmp_path)
    assert proc.returncode == 12
    validate(plan, "claude-session/cli/exec-dry-run.json")
    assert plan["would_fail"] == "unsafe_cwd"
    assert env.run_row(sid)["ended_at"] is None


def test_the_adoption_dry_run_of_an_adopted_run(env: Env) -> None:
    sid = env.started()["csession_id"]
    env.write_db("UPDATE runs SET wrapper_pid = 1, wrapper_start = 'x'")
    proc, plan = env.adopt(sid, "--dry-run")
    assert proc.returncode == 20 and plan["would_refuse"] == "already_adopted"
