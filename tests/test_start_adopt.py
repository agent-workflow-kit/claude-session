"""``start`` and ``exec --session-id`` end to end (interface.md, Launch order, start, Flags at
start and adoption): the ``claude-session`` entry point allocates a pending run, and a later
``exec --session-id`` adopts it and launches the fake ``claude`` (``tests/fakebin``).

Adoption is a compare-and-set raced by real concurrent processes here, and it reruns every
launch check against the adopting process's environment: the tests change the cwd, the git
directory, the config and the staged files between ``start`` and adoption.
"""

from __future__ import annotations

import errno
import json
import os
import select
import sqlite3
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from claude_session import lifecycle, store

from .fakeclaude import claude_bin, read_jsonl, write_json
from .test_exec_headless import SECRET_VALUE, claude_session_cmd, private_home, validate_exec
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
        if "error" in out:
            validate(out, "claude-session/cli/error.json")
        elif "dry_run" not in out:
            validate_exec(out)
            assert out["exit_code"] == proc.returncode
        return proc, out

    def adopt_on_pty(self, sid: str) -> tuple[int, dict[str, Any]]:
        """``exec --session-id`` with stdin and stdout on a pseudo-terminal."""
        master, slave = os.openpty()
        try:
            proc = subprocess.Popen(
                [*claude_session_cmd(), "exec", "--session-id", sid, "--json"],
                cwd=self.work,
                env=self.environ(),
                stdin=slave,
                stdout=slave,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.close(slave)
        chunks: list[bytes] = []
        deadline = time.monotonic() + 60
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 1.0)
                if not ready:
                    continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:  # EIO: the child closed the terminal
                    assert exc.errno == errno.EIO
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            code = proc.wait(timeout=30)
        finally:
            os.close(master)
        out = json.loads(b"".join(chunks).decode())
        if "error" in out:
            validate(out, "claude-session/cli/error.json")
        return code, out

    def edit_spec(self, sid: str, change: Callable[[dict[str, Any]], None]) -> None:
        spec = self.private_run(sid) / lifecycle.SPEC_NAME
        data = json.loads(spec.read_text())
        change(data)
        spec.write_text(json.dumps(data))

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
    assert out["argv"][1:] == ["exec", "--session-id", sid, "--run", "1"]
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


def test_start_resume_of_an_unknown_session_exits_10(env: Env) -> None:
    proc = env.cs(["start", "--json", "--resume", "cs-abcdefgh", "--config", str(env.config)])
    assert proc.returncode == 10
    assert json.loads(proc.stdout)["error"]["code"] == "not_found"
    assert env.rows("SELECT COUNT(*) FROM runs") == [(0,)]


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
    # The launch spec may hold credentials: deleted once the adopting exec read it.
    assert not (env.private_run(sid) / lifecycle.SPEC_NAME).exists()


def test_adoption_writes_an_adopt_log_record(env: Env) -> None:
    sid = env.started()["csession_id"]
    assert env.adopt(sid)[0].returncode == 0
    records = read_jsonl(env.state / "log.jsonl")
    for record in records:
        validate(record, "claude-session/log.json")
    adopts = [r for r in records if r["event"] == "adopt"]
    assert [(r["csession_id"], r["run"], r["level"]) for r in adopts] == [(sid, 1, "info")]


def test_a_finished_run_cannot_be_adopted_again(env: Env) -> None:
    sid = env.started()["csession_id"]
    assert env.adopt(sid)[0].returncode == 0
    proc, out = env.adopt(sid)
    assert proc.returncode == 20
    assert (out["error"]["code"], out["error"]["reason"]) == ("refused", "expired")


def test_the_printed_argv_adopts_its_run(env: Env) -> None:
    out = env.started()
    proc = env.cs([*out["argv"][1:], "--json"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (json.loads(proc.stdout)["run"], json.loads(proc.stdout)["outcome"]) == (1, "done")


def test_an_argv_without_run_still_adopts_the_current_run(env: Env) -> None:
    sid = env.started()["csession_id"]
    proc, out = env.adopt(sid)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["run"] == 1


def test_an_adoption_of_another_run_is_refused_expired_and_changes_nothing(env: Env) -> None:
    sid = env.started()["csession_id"]
    proc, out = env.adopt(sid, "--run", "2", "--outcome-file", str(env.tmp_path / "o.json"))
    assert proc.returncode == 20
    assert (out["error"]["code"], out["error"]["reason"]) == ("refused", "expired")
    row = env.run_row(sid)
    assert row["wrapper_pid"] is None and row["ended_at"] is None
    assert row["outcome_file"] is None
    assert not env.log.exists()
    assert env.adopt(sid, "--run", "1")[0].returncode == 0


def test_the_adoption_dry_run_of_another_run_is_refused_expired(env: Env) -> None:
    sid = env.started()["csession_id"]
    proc, out = env.adopt(sid, "--run", "2", "--dry-run")
    assert proc.returncode == 20
    assert (out["error"]["code"], out["error"]["reason"]) == ("refused", "expired")
    proc, plan = env.adopt(sid, "--run", "1", "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (plan["run"], plan["would_refuse"]) == (1, None)
    assert env.run_row(sid)["wrapper_pid"] is None


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
    assert not (env.private_run(sid) / lifecycle.SPEC_NAME).exists()


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


def test_a_session_dir_cwd_is_frozen_and_used_at_adoption(env: Env) -> None:
    sid = env.started("--mode", "headless", "--cwd", "session-dir")["csession_id"]
    proc, _ = env.adopt(sid, cwd=env.tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert env.run_row(sid)["cwd"] == str(env.state / "sessions" / sid / "work")


def test_a_profile_file_is_frozen_at_start(env: Env) -> None:
    profile = env.work / "p.json"
    profile.write_text('{"permissions": {"allow": ["Read"]}}')
    out = env.started("--mode", "headless", "--permission-profile", "./p.json")
    sid = out["csession_id"]
    label = env.run_row(sid)["permission_profile"]
    assert label.startswith("file:")
    snapshot = env.private_run(sid) / "permission-profile.json"
    assert json.loads(snapshot.read_text()) == {"permissions": {"allow": ["Read"]}}
    profile.write_text('{"permissions": {"allow": ["Bash"]}}')  # changed after start
    proc, _ = env.adopt(sid)
    assert proc.returncode == 0, proc.stderr
    settings = json.loads((env.private_run(sid) / "settings.json").read_text())
    assert "Read" in settings["permissions"]["allow"]
    assert "Bash" not in settings["permissions"]["allow"]
    assert env.run_row(sid)["permission_profile"] == label


def test_a_named_profile_is_frozen_at_start(env: Env) -> None:
    env.write_config(
        "permission_profiles:\n  mine:\n    permissions: {allow: [Read]}\n"
        "kinds:\n  implement:\n    permission_profile: mine\n"
    )
    sid = env.started()["csession_id"]
    env.write_config(
        "permission_profiles:\n  mine:\n    permissions: {allow: [Bash]}\n"
        "kinds:\n  implement:\n    permission_profile: mine\n"
    )
    proc, _ = env.adopt(sid)
    assert proc.returncode == 0, proc.stderr
    settings = json.loads((env.private_run(sid) / "settings.json").read_text())
    assert "Read" in settings["permissions"]["allow"]
    assert "Bash" not in settings["permissions"]["allow"]


def test_a_plugin_dir_swapped_for_a_symlink_fails_the_adoption(env: Env, tmp_path: Path) -> None:
    plugin, other = tmp_path / "plugin", tmp_path / "other"
    plugin.mkdir()
    other.mkdir()
    sid = env.started("--mode", "headless", "--plugin-dir", str(plugin))["csession_id"]
    plugin.rmdir()
    plugin.symlink_to(other)
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "unsafe_plugin_dir")


def test_a_removed_plugin_dir_fails_the_adoption(env: Env, tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    sid = env.started("--mode", "headless", "--plugin-dir", "../plugin")["csession_id"]
    plugin.rmdir()
    proc, out = env.adopt(sid, cwd=tmp_path)  # a relative dir was resolved at start
    assert_not_started(proc, out, "plugin_dir_missing")


def test_a_unicode_prompt_is_delivered_byte_for_byte(env: Env) -> None:
    text = "Tâche: ünïcödé ‮\U0001f600 '; DROP TABLE runs; --\n"
    sid = env.started(prompt=text)["csession_id"]
    proc, _ = env.adopt(sid)
    assert proc.returncode == 0, proc.stderr
    assert (env.state / "sessions" / sid / "prompt.md").read_text() == text


@pytest.mark.parametrize("given", ["cs-'; DROP TABLE runs; --", "cs-aaaa%", "cs-AAAA*"])
def test_an_injection_shaped_session_id_is_refused(env: Env, given: str) -> None:
    proc = env.cs(["exec", "--session-id", given, "--json"])
    assert proc.returncode in (2, 10)
    assert json.loads(proc.stdout)["error"]["code"] in ("usage", "not_found")


def test_the_adopting_process_hook_interpreter_is_recorded(env: Env) -> None:
    sid = env.started()["csession_id"]
    env.write_db("UPDATE runs SET hook_interpreter = '/old/python'")
    proc, _ = env.adopt(sid)
    assert proc.returncode == 0, proc.stderr
    assert env.run_row(sid)["hook_interpreter"] != "/old/python"


def test_an_interactive_run_with_a_tty_stays_pending_until_supported(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from claude_session.cli import main

    sid = env.started("--mode", "interactive")["csession_id"]
    monkeypatch.setattr(lifecycle, "has_tty", lambda: True)
    assert main(["exec", "--session-id", sid, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["detail"] == "not_implemented"
    row = env.run_row(sid)
    assert row["wrapper_pid"] is None and row["ended_at"] is None


# The launch spec, read back


def spec_text(**changes: Any) -> bytes:
    base: dict[str, Any] = {
        "version": lifecycle.SPEC_VERSION,
        "resolution": {"kind": "implement"},
        "request": {},
        "caller_cwd": None,
        "cwd": "/w",
        "plugin_dirs": [],
        "profile": {"name": "x", "content": {}, "snapshot": None},
        "config": {},
    }
    return json.dumps({**base, **changes}).encode()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff\xfe",
        b"[]",
        b"null",
        spec_text(version=2),
        spec_text(cwd=5),
        spec_text(plugin_dirs="/x"),
        spec_text(resolution={"kind": "implement"}),  # incomplete
        spec_text(profile={"name": None, "content": [], "snapshot": None}),
    ],
)
def test_an_unusable_launch_spec_is_session_dir_unsafe(raw: bytes) -> None:
    from claude_session.sessiondir import SessionDirUnsafe

    with pytest.raises(SessionDirUnsafe) as info:
        lifecycle.parse_spec(raw, "/p/launch-spec.json")
    assert info.value.end_reason == "session_dir_unsafe"
    assert "/p/launch-spec.json" in info.value.message


def test_the_launch_spec_round_trips(env: Env) -> None:
    env.write_config("kinds:\n  implement:\n    env:\n      set: {A: b}\n")
    sid = env.started("--mode", "headless", "--env-allow", "X", "--timeout", "90s")["csession_id"]
    raw = (env.private_run(sid) / lifecycle.SPEC_NAME).read_bytes()
    frozen = lifecycle.parse_spec(raw, "spec")
    resolution = frozen.resolution
    assert resolution.profile.env.set == {"A": "b"}
    assert resolution.env_allow == ("X",)
    assert resolution.profile.timeout == 90
    assert lifecycle.parse_spec(lifecycle.spec_bytes(json.loads(raw)), "spec") == frozen


def test_the_adoption_dry_run_of_a_failed_start_is_refused(env: Env) -> None:
    env.config.write_text(f"claude_bin: {env.tmp_path / 'missing'}\n", encoding="utf-8")
    proc, out = env.start()
    sid = out["error"]["csession_id"]
    proc, err = env.adopt(sid, "--dry-run")
    assert proc.returncode == 20
    assert (err["error"]["code"], err["error"]["reason"]) == ("refused", "expired")
    proc, err = env.adopt(sid)
    assert proc.returncode == 20 and err["error"]["reason"] == "expired"


def test_a_relative_profile_file_is_resolved_against_starts_cwd(env: Env, tmp_path: Path) -> None:
    # An isolated run: at adoption, a relative profile path must not be read against the
    # adopting process's cwd (here the run's own, session-writable, cwd).
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "p.json").write_text('{"permissions": {"allow": ["Read"]}}')
    args = ("--mode", "headless", "--reads-third-party", "--sandbox", "off")
    args += ("--permission-profile", "./p.json", "--cwd", str(env.work))
    sid = env.started(*args, cwd=prof)["csession_id"]
    proc, out = env.adopt(sid, cwd=env.work)
    assert proc.returncode == 0, (out["end_reason"], out["message"])


# The run is pending only once start has frozen it (R1-1)


def in_process_start(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    """``start`` run by ``cli.main`` in this process (so a test can patch ``freeze``), with
    the environment ``Env.cs`` gives a subprocess."""
    from claude_session.cli import main

    for key, value in private_home(env.tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(env.work)
    prompt = env.tmp_path / "prompt.md"
    prompt.write_text("Do the task.\n", encoding="utf-8")
    argv = ["start", "--json", "--config", str(env.config), "--mode", "headless"]
    code = main([*argv, "--prompt-file", str(prompt)])
    return code, json.loads(capsys.readouterr().out)


def test_a_run_being_frozen_is_not_adoptable_and_expires_from_its_release(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.write_config("lost_grace: 30s\n")
    original = lifecycle.freeze
    seen: dict[str, Any] = {}

    def freeze(*args: Any) -> Any:
        sid = args[4].csession_id
        proc, out = env.adopt(sid)
        seen["adopt"] = (proc.returncode, out["error"]["reason"])
        assert env.run_row(sid)["wrapper_pid"] == os.getpid()
        time.sleep(1.0)
        seen["frozen_from"] = datetime.now(UTC)
        return original(*args)

    monkeypatch.setattr(lifecycle, "freeze", freeze)
    code, out = in_process_start(env, monkeypatch, capsys)
    assert code == 0, out
    validate(out, "claude-session/cli/start.json")
    assert seen["adopt"] == (20, "already_adopted")
    expires = store.parse_ts(out["expires_at"])
    assert expires >= seen["frozen_from"] - timedelta(milliseconds=1) + timedelta(seconds=30)
    row = env.run_row(out["csession_id"])
    assert (row["wrapper_pid"], row["wrapper_start"], row["ended_at"]) == (None, None, None)
    assert row["expires_at"] == out["expires_at"]
    proc, outcome = env.adopt(out["csession_id"])
    assert proc.returncode == 0, outcome


def test_a_run_ended_while_start_froze_it_fails_start_and_drops_the_spec(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    original = lifecycle.freeze
    seen: dict[str, str] = {}

    def freeze(*args: Any) -> Any:
        seen["sid"] = sid = args[4].csession_id
        env.write_db(
            "UPDATE runs SET ended_at = 'x', outcome = 'canceled', end_reason = 'canceled' "
            "WHERE csession_id = ?",
            (sid,),
        )
        return original(*args)

    monkeypatch.setattr(lifecycle, "freeze", freeze)
    code, out = in_process_start(env, monkeypatch, capsys)
    assert code == 12
    validate(out, "claude-session/cli/error.json")
    assert out["error"]["code"] == "failed"
    assert not (env.private_run(seen["sid"]) / lifecycle.SPEC_NAME).exists()
    row = env.run_row(seen["sid"])
    assert (row["end_reason"], row["wrapper_pid"]) == ("canceled", os.getpid())


# Frozen directories (R1-2)


@pytest.mark.parametrize("key", ["read_dirs", "write_dirs", "allowWrite"])
def test_a_frozen_directory_swapped_for_a_symlink_fails_the_adoption(
    env: Env, tmp_path: Path, key: str
) -> None:
    target, other = tmp_path / "d", tmp_path / "other"
    target.mkdir()
    other.mkdir()
    if key == "allowWrite":
        env.write_config(
            "permission_profiles:\n  mine:\n"
            f"    sandbox: {{filesystem: {{allowWrite: ['{target}']}}}}\n"
            "kinds:\n  implement:\n    permission_profile: mine\n"
        )
    else:
        env.write_config(f"kinds:\n  implement:\n    {key}: ['{target}']\n")
    sid = env.started()["csession_id"]
    target.rmdir()
    target.symlink_to(other)
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "invalid_permission_profile")
    assert "resolves to another directory than at start" in out["message"]


# The current config only tightens what start froze (R1-6)


@pytest.mark.parametrize("key", ["secret_env", "claude_auth_env"])
def test_a_credential_name_added_after_start_is_stripped_and_indexed(env: Env, key: str) -> None:
    env.scenario(
        [
            START,
            {
                "do": "bash",
                "command": 'printf "%s" "${VAULT_TOKEN-unset}" > "$CSESSION_DIR/artifacts/v.txt"',
            },
            STOP,
            result_step(f"the token is {SECRET_VALUE}!"),
        ]
    )
    sid = env.started()["csession_id"]
    env.write_config(f"{key}: [VAULT_TOKEN]\n")
    proc, out = env.adopt(sid, env={"VAULT_TOKEN": SECRET_VALUE})
    assert (env.state / "sessions" / sid / "artifacts" / "v.txt").read_text() == "unset"
    # Indexed: the implicit result holding the value is rejected (contains_secret).
    assert (proc.returncode, out["end_reason"]) == (14, "result_rejected")


# Interactive runs: refusals before not_implemented (R1-7)


def test_an_expired_interactive_run_on_a_terminal_is_refused_and_finalized(env: Env) -> None:
    env.write_config("lost_grace: 0.2s\n")
    sid = env.started("--mode", "interactive")["csession_id"]
    time.sleep(0.5)
    code, out = env.adopt_on_pty(sid)
    assert (code, out["error"]["reason"]) == (20, "expired")
    row = env.run_row(sid)
    assert (row["outcome"], row["end_reason"]) == ("not-started", "never_adopted")


def test_an_ended_interactive_run_on_a_terminal_is_refused(env: Env) -> None:
    sid = env.started("--mode", "interactive")["csession_id"]
    env.write_db("UPDATE runs SET ended_at = 'x', outcome = 'canceled', end_reason = 'canceled'")
    code, out = env.adopt_on_pty(sid)
    assert (code, out["error"]["reason"]) == (20, "expired")


# The launch spec is strict (R1-13, R1-14)


def test_a_file_profile_snapshot_that_lost_its_label_fails_the_adoption(env: Env) -> None:
    (env.work / "p.json").write_text('{"permissions": {"allow": ["Read"]}}')
    sid = env.started("--mode", "headless", "--permission-profile", "./p.json")["csession_id"]

    def change(data: dict[str, Any]) -> None:
        assert data["profile"]["content"] is None  # a file profile is its snapshot only
        data["profile"]["snapshot"] = '{"permissions":{"allow":["Bash"]}}'

    env.edit_spec(sid, change)
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "session_dir_unsafe")


def _set_request(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        data["request"][key] = value

    return change


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.update(extra=1),
        _set_request("extra", 1),
        lambda data: data.update(cwd="work"),
        _set_request("actor", "auto:a\x00b"),
        _set_request("mode", "interactive"),
        _set_request("skill", "skill\n"),
        _set_request("labels", ["Bad Label"]),
        _set_request("hooks", ["nope=cmd"]),
        _set_request("flags", {}),
        lambda data: data["resolution"]["origins"].update(model="elsewhere"),
    ],
    ids=[
        "unknown-key",
        "unknown-request-key",
        "relative-cwd",
        "bad-actor",
        "mode-mismatch",
        "skill-newline",
        "bad-label",
        "bad-hook",
        "missing-flags",
        "bad-origin",
    ],
)
def test_a_tampered_launch_spec_fails_the_adoption(
    env: Env, change: Callable[[dict[str, Any]], None]
) -> None:
    sid = env.started()["csession_id"]
    env.edit_spec(sid, change)
    proc, out = env.adopt(sid)
    assert_not_started(proc, out, "session_dir_unsafe")


# The adoption dry run without a usable launch spec (R1-3) and through a symlink (R1-4)


def test_the_adoption_dry_run_of_a_run_without_a_spec_prints_the_failed_envelope(
    env: Env,
) -> None:
    sid = env.started()["csession_id"]
    (env.private_run(sid) / lifecycle.SPEC_NAME).unlink()
    proc, out = env.adopt(sid, "--dry-run")
    assert proc.returncode == 12
    assert out["error"]["code"] == "failed"
    assert env.run_row(sid)["ended_at"] is None


def test_the_adoption_dry_run_works_under_a_symlinked_state_directory(env: Env) -> None:
    sid = env.started()["csession_id"]
    link = env.tmp_path / "state-link"
    link.symlink_to(env.state.parent)
    proc, plan = env.adopt(sid, "--dry-run", env={"XDG_STATE_HOME": str(link)})
    assert proc.returncode == 0, plan
    validate(plan, "claude-session/cli/exec-dry-run.json")
    assert plan["would_fail"] is None


def test_the_adoption_dry_run_refuses_a_symlinked_run_directory(env: Env) -> None:
    sid = env.started()["csession_id"]
    run_dir = env.private_run(sid)
    moved = env.tmp_path / "moved-run"
    run_dir.rename(moved)
    run_dir.symlink_to(moved)
    proc, out = env.adopt(sid, "--dry-run")
    assert proc.returncode == 12
    assert out["error"]["code"] == "failed"
    assert "session directory" in out["error"]["message"]
