"""Taint and ``--allow-tainted`` (interface.md, Taint, Launch order and Audit and log;
design.md, Overrides and actors): the human-only check, the refusal lifted at every refusal
point, the ``resume_tainted`` audit record (written once, right before step 7, fail closed),
taint propagation, and the ways an agent might try to launder a taint.

The end-to-end tests run real ``claude-session`` processes against the fake ``claude``, with
stdin on ``/dev/null`` (no terminal) unless a test patches it in process.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from claude_session import launch, store, taint
from claude_session.errors import RefusedError, UsageError

from .test_resume import (
    adopt_in_process,
    claude_id,
    exec_session,
    resume,
    resume_in_process,
    run_cs,
    starts,
    taint_after_validate,
)
from .test_resume import env as resume_env
from .test_start_adopt import Env, validate

env = resume_env  # the fixture
HUMAN = ("--actor", "human:tester")
TRUSTED = ("--kind", "draft-ticket")


def audit_records(env: Env) -> list[dict[str, Any]]:
    path = env.state / "audit.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for record in records:
        validate(record, "claude-session/audit.json")
    return records


def tainted_records(env: Env) -> list[dict[str, Any]]:
    return [r for r in audit_records(env) if r["action"] == "resume_tainted"]


def tainted_session(env: Env) -> str:
    sid = exec_session(env, "--reads-third-party", "--sandbox", "off")["csession_id"]
    assert env.rows("SELECT tainted FROM sessions WHERE csession_id = ?", (sid,)) == [(1,)]
    return sid


def is_tainted(env: Env, sid: str) -> bool:
    rows = env.rows("SELECT tainted FROM sessions WHERE csession_id = ?", (sid,))
    return rows == [(1,)]


# The human-only check (unit)


@pytest.mark.parametrize("actor", ["human:alice", "human:a b", "human:x@example.org"])
def test_an_explicit_human_actor_passes_without_a_terminal(actor: str) -> None:
    taint.check_human_only("--allow-tainted", actor, tty=False)


def test_a_terminal_passes_without_an_actor() -> None:
    taint.check_human_only("--allow-tainted", None, tty=True)


def test_no_terminal_and_no_actor_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="human-only"):
        taint.check_human_only("--allow-tainted", None, tty=False)


@pytest.mark.parametrize(
    "actor",
    [
        "auto:unknown",
        "auto:agent",
        "human:",
        "human:   ",
        "human:\n",
        "Human:alice",
        " human:alice",
        "humans:alice",
        "human",
        "",
        "auto:x;human:alice",
    ],
)
@pytest.mark.parametrize("tty", [False, True])
def test_an_explicit_actor_that_is_not_human_is_refused_even_with_a_terminal(
    actor: str, tty: bool
) -> None:
    with pytest.raises(UsageError, match="human-only"):
        taint.check_human_only("--allow-tainted", actor, tty=tty)


def test_from_args_records_the_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    args = argparse.Namespace(allow_tainted=True, actor=None)
    monkeypatch.setattr(sys, "stdin", Tty())
    assert taint.from_args(args) == taint.AllowTainted(tty=True)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    with pytest.raises(UsageError):
        taint.from_args(args)
    args.actor = "human:alice"
    assert taint.from_args(args) == taint.AllowTainted(tty=False)
    args.allow_tainted = False
    args.actor = None
    assert taint.from_args(args) is None


@pytest.mark.parametrize(
    "invocation_json",
    [
        None,
        "",
        "{}",
        "not json",
        "[]",
        '"allow_tainted"',
        '{"allow_tainted": true}',
        '{"allow_tainted": {}}',
        '{"allow_tainted": {"tty": "true"}}',
        '{"allow_tainted": {"tty": 1}}',
        '{"allow_tainted": [true]}',
        '{"allow_tainted": null}',
    ],
)
def test_a_malformed_recorded_flag_counts_as_not_given(invocation_json: str | None) -> None:
    assert not store.allows_tainted(invocation_json)
    try:
        parsed = json.loads(invocation_json or "{}")
    except ValueError:
        parsed = None
    assert taint.AllowTainted.from_invocation(parsed) is None


@pytest.mark.parametrize("tty", [False, True])
def test_the_recorded_flag_round_trips(tty: bool) -> None:
    allow = taint.AllowTainted(tty=tty)
    invocation = {store.ALLOW_TAINTED: allow.invocation()}
    assert store.allows_tainted(json.dumps(invocation))
    assert taint.AllowTainted.from_invocation(invocation) == allow


def test_the_refusal_point_lifts_only_with_the_flag() -> None:
    assert store._refuse_tainted("cs-aaaaaaaa", True) is False  # nothing to lift
    assert store._refuse_tainted("cs-aaaaaaaa", True, True) is False
    assert store._refuse_tainted("cs-aaaaaaaa", False, True) is True
    with pytest.raises(RefusedError) as caught:
        store._refuse_tainted("cs-aaaaaaaa", False)
    assert caught.value.reason == "tainted"


def test_the_audit_record_is_schema_valid_and_redacted(tmp_path: Path) -> None:
    secret = "s3cr3t-value-123"
    taint.audit_resume_tainted(
        str(tmp_path),
        "audit.jsonl",
        actor=f"human:{secret}",
        decision_ids=["d-1", f"x{secret}"],
        target="cs-aaaaaaaa",
        run=3,
        run_kind="draft-ticket",
        forked_from="cs-bbbbbbbb",
        allow=taint.AllowTainted(tty=False),
        secrets=[secret],
    )
    (line,) = (tmp_path / "audit.jsonl").read_text().splitlines()
    record = json.loads(line)
    validate(record, "claude-session/audit.json")
    assert secret not in line
    assert record["action"] == "resume_tainted" and record["result"] == "ok"
    assert record["inputs"] == {
        "run_kind": "draft-ticket",
        "run": 3,
        "forked_from": "cs-bbbbbbbb",
        "tty": False,
    }


def test_an_audit_record_that_cannot_be_written_raises(tmp_path: Path) -> None:
    (tmp_path / "audit.jsonl").mkdir()
    with pytest.raises(OSError):
        taint.audit_resume_tainted(
            str(tmp_path),
            "audit.jsonl",
            actor="human:a",
            decision_ids=[],
            target="cs-aaaaaaaa",
            run=2,
            run_kind="k",
            forked_from=None,
            allow=taint.AllowTainted(tty=True),
            secrets=[],
        )


# Human-only, end to end


@pytest.mark.parametrize("actor", [(), ("--actor", "auto:agent"), ("--actor", "human:")])
def test_allow_tainted_without_a_terminal_or_a_human_actor_exits_2(
    env: Env, actor: tuple[str, ...]
) -> None:
    sid = tainted_session(env)
    for argv in (
        ["resume", sid, "--json", *TRUSTED, "--allow-tainted", *actor],
        ["resume", sid, "--json", *TRUSTED, "--allow-tainted", "--fork", *actor],
        ["resume", sid, "--json", *TRUSTED, "--allow-tainted", "--dry-run", *actor],
        ["start", "--json", "--resume", sid, *TRUSTED, "--allow-tainted", *actor],
    ):
        proc, out = run_cs(env, argv)
        assert proc.returncode == 2, (argv, proc.stdout, proc.stderr)
        assert out["error"]["code"] == "usage" and "human-only" in out["error"]["message"]
    # Nothing allocated, nothing audited.
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(1,)]
    assert tainted_records(env) == []


def test_the_human_only_check_comes_before_the_lookup(env: Env) -> None:
    proc, out = resume(env, "cs-zzzzzzzz", "--allow-tainted")
    assert proc.returncode == 2 and out["error"]["code"] == "usage"


def test_allow_tainted_from_a_terminal_uses_the_default_human_actor(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = tainted_session(env)

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    code, out = resume_in_process(
        env, monkeypatch, capsys, sid, *TRUSTED, "--mode", "headless", "--allow-tainted"
    )
    assert code == 0, out
    (record,) = tainted_records(env)
    assert record["actor"].startswith("human:") and record["inputs"]["tty"] is True


# The refusal lifted, and the audit record


def test_resume_allow_tainted_runs_and_writes_one_record(env: Env) -> None:
    sid = tainted_session(env)
    proc, out = resume(env, sid, *TRUSTED, "--allow-tainted", *HUMAN, "--decision-id", "d-7")
    assert proc.returncode == 0, proc.stderr
    assert (out["csession_id"], out["run"], out["outcome"]) == (sid, 2, "done")
    assert env.run_row(sid, 2)["reads_third_party"] == 0
    assert len(starts(env)) == 2
    (record,) = tainted_records(env)
    assert record["actor"] == "human:tester" and record["decision_id"] == ["d-7"]
    assert record["target"] == sid and record["result"] == "ok"
    assert record["inputs"] == {
        "run_kind": "draft-ticket",
        "run": 2,
        "forked_from": None,
        "tty": False,
    }
    # The taint is never removed: the next trusted resume needs the flag again.
    assert is_tainted(env, sid)
    proc, out = resume(env, sid, *TRUSTED)
    assert proc.returncode == 20 and out["error"]["reason"] == "tainted"
    assert len(tainted_records(env)) == 1


def test_a_third_party_reading_run_needs_no_override_and_writes_no_record(env: Env) -> None:
    sid = tainted_session(env)
    proc, _out = resume(
        env, sid, "--reads-third-party", "--sandbox", "off", "--allow-tainted", *HUMAN
    )
    assert proc.returncode == 0, proc.stderr
    assert tainted_records(env) == []


def test_an_untainted_session_writes_no_record(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    proc, _out = resume(env, sid, "--allow-tainted", *HUMAN)
    assert proc.returncode == 0, proc.stderr
    assert tainted_records(env) == []


def test_a_dry_run_with_the_flag_does_not_refuse_and_audits_nothing(env: Env) -> None:
    sid = tainted_session(env)
    proc, out = resume(env, sid, *TRUSTED, "--dry-run", "--allow-tainted", *HUMAN)
    assert proc.returncode == 0, out
    assert out["would_refuse"] is None and out["allow_tainted"] is True
    proc, out = resume(env, sid, *TRUSTED, "--dry-run", "--fork", "--allow-tainted", *HUMAN)
    assert proc.returncode == 0 and out["would_refuse"] is None
    assert tainted_records(env) == []
    assert env.rows("SELECT COUNT(*) FROM runs") == [(1,)]


def test_a_fork_with_the_flag_copies_the_taint_and_writes_one_record(env: Env) -> None:
    sid = tainted_session(env)
    proc, out = resume(env, sid, *TRUSTED, "--fork", "--allow-tainted", *HUMAN)
    assert proc.returncode == 0, proc.stderr
    fork = out["csession_id"]
    assert fork != sid and is_tainted(env, fork)
    # Allocation and the check before step 7 both lifted the refusal: one record.
    (record,) = tainted_records(env)
    assert record["target"] == fork
    assert record["inputs"]["forked_from"] == sid and record["inputs"]["run"] == 1
    # The fork's own later trusted runs are refused without the flag.
    proc, out = resume(env, fork, *TRUSTED)
    assert proc.returncode == 20 and out["error"]["reason"] == "tainted"


def test_start_resume_audits_at_adoption_with_starts_actor(env: Env) -> None:
    sid = tainted_session(env)
    proc, out = run_cs(
        env, ["start", "--json", "--resume", sid, *TRUSTED, "--allow-tainted", *HUMAN]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["run"] == 2
    assert tainted_records(env) == []  # not at start
    invocation = json.loads(env.run_row(sid, 2)["invocation_json"])
    assert invocation[store.ALLOW_TAINTED] == {"tty": False}
    # The flag is not in the launch spec file: the row is the only record of it.
    spec = (env.private_run(sid, 2) / "launch-spec.json").read_text()
    assert "allow_tainted" not in spec
    proc, plan = env.adopt(sid, "--dry-run")
    assert proc.returncode == 0, plan
    assert plan["would_refuse"] is None and plan["allow_tainted"] is True
    assert tainted_records(env) == []
    proc, out = env.adopt(sid)  # no terminal, no --actor: the flag came from start
    assert proc.returncode == 0, out
    (record,) = tainted_records(env)
    assert record["actor"] == "human:tester" and record["target"] == sid
    assert record["inputs"]["run"] == 2


def test_start_resume_fork_with_the_flag_is_adopted_and_audited_once(env: Env) -> None:
    sid = tainted_session(env)
    proc, out = run_cs(
        env, ["start", "--json", "--resume", sid, "--fork", *TRUSTED, "--allow-tainted", *HUMAN]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fork = out["csession_id"]
    assert is_tainted(env, fork)
    proc, out = env.adopt(fork)
    assert proc.returncode == 0, out
    (record,) = tainted_records(env)
    assert record["target"] == fork and record["inputs"]["forked_from"] == sid


def test_a_pending_fork_with_the_flag_is_adopted_when_its_source_became_tainted(
    env: Env,
) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = run_cs(
        env, ["start", "--json", "--resume", sid, "--fork", "--allow-tainted", *HUMAN]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fork = out["csession_id"]
    assert not is_tainted(env, fork)
    proc, _out = resume(env, sid, "--reads-third-party", "--sandbox", "off")
    assert proc.returncode == 0, proc.stderr
    proc, plan = env.adopt(fork, "--dry-run")
    assert proc.returncode == 0 and plan["would_refuse"] is None
    proc, out = env.adopt(fork)
    assert proc.returncode == 0, out
    # The compare-and-set copied the source's taint onto the fork.
    assert is_tainted(env, fork)
    (record,) = tainted_records(env)
    assert record["target"] == fork and record["inputs"]["forked_from"] == sid


def test_a_fork_with_the_flag_whose_source_became_tainted_before_the_spawn_is_audited(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = exec_session(env)["csession_id"]
    taint_after_validate(env, monkeypatch, sid)
    code, out = resume_in_process(
        env, monkeypatch, capsys, sid, "--fork", "--allow-tainted", *HUMAN
    )
    assert code == 0, out
    fork = out["csession_id"]
    assert is_tainted(env, fork)
    (record,) = tainted_records(env)
    assert record["target"] == fork and record["inputs"]["forked_from"] == sid


def test_an_adopted_fork_with_the_flag_whose_source_became_tainted_before_the_spawn(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = run_cs(
        env, ["start", "--json", "--resume", sid, "--fork", "--allow-tainted", *HUMAN]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fork = out["csession_id"]
    taint_after_validate(env, monkeypatch, sid)
    code, out = adopt_in_process(env, monkeypatch, capsys, fork)
    assert code == 0, out
    assert is_tainted(env, fork)
    (record,) = tainted_records(env)
    assert record["target"] == fork


# Fail closed


def test_an_audit_that_cannot_be_written_never_launches_the_run(env: Env) -> None:
    sid = tainted_session(env)
    # Its sandbox_lowered record exists; the file is replaced by a directory.
    (env.state / "audit.jsonl").unlink()
    (env.state / "audit.jsonl").mkdir()
    proc, out = resume(env, sid, *TRUSTED, "--allow-tainted", *HUMAN)
    assert proc.returncode == 12, out
    assert (out["outcome"], out["end_reason"]) == ("not-started", "launch_interrupted")
    assert len(starts(env)) == 1  # claude never started for run 2
    assert is_tainted(env, sid)


def test_an_audit_that_cannot_be_written_at_adoption_never_launches_the_run(env: Env) -> None:
    sid = tainted_session(env)
    proc, out = run_cs(
        env, ["start", "--json", "--resume", sid, *TRUSTED, "--allow-tainted", *HUMAN]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (env.state / "audit.jsonl").unlink()
    os.symlink(env.tmp_path / "elsewhere.jsonl", env.state / "audit.jsonl")
    proc, out = env.adopt(sid)
    assert proc.returncode == 12, out
    assert (out["outcome"], out["end_reason"]) == ("not-started", "launch_interrupted")
    assert len(starts(env)) == 1
    assert not (env.tmp_path / "elsewhere.jsonl").exists()


# The error follow-up of a launch-phase record whose run never spawned claude


def results(env: Env, action: str) -> list[str]:
    return [r["result"] for r in audit_records(env) if r["action"] == action]


def failing_prepare(monkeypatch: pytest.MonkeyPatch, before: Any = None) -> None:
    def prepare(*_args: Any, **_kwargs: Any) -> Any:
        if before is not None:
            before()
        raise OSError("prepare failed")

    monkeypatch.setattr(launch, "prepare", prepare)


def test_a_run_that_fails_after_its_resume_tainted_record_gets_an_error_record(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = tainted_session(env)
    failing_prepare(monkeypatch)
    code, out = resume_in_process(
        env, monkeypatch, capsys, sid, *TRUSTED, "--allow-tainted", *HUMAN, "--decision-id", "d-7"
    )
    assert code == 12, out
    assert out["end_reason"] == "launch_interrupted"
    ok, error = tainted_records(env)
    assert (ok["result"], error["result"]) == ("ok", "error")
    for key in ("actor", "target", "decision_id", "inputs"):
        assert error[key] == ok[key], key
    assert len(starts(env)) == 1


def test_a_third_party_run_that_fails_after_its_sandbox_lowered_record_gets_an_error_record(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = exec_session(env)["csession_id"]
    failing_prepare(monkeypatch)
    code, out = resume_in_process(
        env, monkeypatch, capsys, sid, "--reads-third-party", "--sandbox", "off"
    )
    assert code == 12, out
    assert results(env, "sandbox_lowered") == ["ok", "error"]
    ok, error = (r for r in audit_records(env) if r["action"] == "sandbox_lowered")
    assert (error["target"], error["inputs"]) == (ok["target"], ok["inputs"])


def test_a_spawn_that_fails_after_the_resume_tainted_record_gets_an_error_record(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from claude_session import supervisor

    sid = tainted_session(env)

    def spawn(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("spawn failed")

    monkeypatch.setattr(supervisor, "spawn", spawn)
    code, out = resume_in_process(
        env, monkeypatch, capsys, sid, *TRUSTED, "--allow-tainted", *HUMAN
    )
    assert code == 12, out
    assert results(env, "resume_tainted") == ["ok", "error"]
    assert len(starts(env)) == 1


def test_a_run_that_spawns_gets_no_error_record(env: Env) -> None:
    sid = tainted_session(env)
    proc, _out = resume(env, sid, *TRUSTED, "--allow-tainted", *HUMAN)
    assert proc.returncode == 0, proc.stderr
    proc, _out = resume(env, sid, "--reads-third-party", "--sandbox", "off")
    assert proc.returncode == 0, proc.stderr
    assert results(env, "resume_tainted") == ["ok"]
    assert results(env, "sandbox_lowered") == ["ok", "ok"]


def test_an_error_record_that_cannot_be_written_does_not_change_the_exit_code(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = tainted_session(env)
    path = env.state / "audit.jsonl"

    def block_the_audit() -> None:
        path.rename(env.tmp_path / "audit.jsonl")
        path.mkdir()

    failing_prepare(monkeypatch, block_the_audit)
    code, out = resume_in_process(
        env, monkeypatch, capsys, sid, *TRUSTED, "--allow-tainted", *HUMAN
    )
    assert code == 12, out
    assert (out["outcome"], out["end_reason"]) == ("not-started", "launch_interrupted")
    moved = [json.loads(line) for line in (env.tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [r["result"] for r in moved if r["action"] == "resume_tainted"] == ["ok"]


# Laundering


def test_a_row_that_records_the_flag_but_a_request_that_lacks_it_never_launches(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = tainted_session(env)
    real_new_run = launch.new_run

    def recording(req: launch.Request, *args: Any, **kwargs: Any) -> store.NewRun:
        # The request lacks the flag, but the run row records it anyway.
        run = real_new_run(req, *args, **kwargs)
        run.invocation[store.ALLOW_TAINTED] = {"tty": True}
        return run

    monkeypatch.setattr(launch, "new_run", recording)
    code, out = resume_in_process(env, monkeypatch, capsys, sid, *TRUSTED)
    assert code == 12, out
    assert out["end_reason"] == "launch_interrupted"
    assert tainted_records(env) == []
    assert len(starts(env)) == 1


def test_a_launch_spec_edited_to_drop_reads_third_party_is_refused_at_adoption(env: Env) -> None:
    sid = tainted_session(env)
    proc, out = run_cs(
        env, ["start", "--json", "--resume", sid, "--reads-third-party", "--sandbox", "off"]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert env.run_row(sid, 2)["reads_third_party"] == 1

    def trusted(data: dict[str, Any]) -> None:
        data["resolution"]["profile"]["reads_third_party"] = False

    spec = env.private_run(sid, 2) / "launch-spec.json"
    data = json.loads(spec.read_text())
    trusted(data)
    spec.write_text(json.dumps(data))
    proc, out = env.adopt(sid)
    assert proc.returncode == 12, out
    assert (out["outcome"], out["end_reason"]) == ("not-started", "session_dir_unsafe")
    assert len(starts(env)) == 1


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda r: r.update(actor="human:someone-else"), id="actor"),
        pytest.param(lambda r: r.update(decision_ids=["d-other"]), id="decision-ids"),
        pytest.param(lambda r: r.update(decision_ids=[]), id="no-decision-ids"),
    ],
)
def test_a_launch_spec_edited_to_change_the_actor_or_decision_ids_is_refused_at_adoption(
    env: Env, edit: Any
) -> None:
    sid = tainted_session(env)
    proc, out = run_cs(
        env,
        [
            "start",
            "--json",
            "--resume",
            sid,
            *TRUSTED,
            "--allow-tainted",
            *HUMAN,
            "--decision-id",
            "d-7",
        ],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    spec = env.private_run(sid, 2) / "launch-spec.json"
    data = json.loads(spec.read_text())
    edit(data["request"])
    spec.write_text(json.dumps(data))
    proc, out = env.adopt(sid)
    assert proc.returncode == 12, out
    assert (out["outcome"], out["end_reason"]) == ("not-started", "session_dir_unsafe")
    assert tainted_records(env) == []
    assert len(starts(env)) == 1


def pending_trusted_run_tainted_after_start(env: Env) -> str:
    # A trusted pending run of a session that was untainted at start (so the row records no
    # --allow-tainted) and is tainted before adoption.
    sid = exec_session(env)["csession_id"]
    proc, _out = run_cs(env, ["start", "--json", "--resume", sid, *TRUSTED])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert store.ALLOW_TAINTED not in json.loads(env.run_row(sid, 2)["invocation_json"])
    env.write_db(
        "UPDATE sessions SET tainted = 1, tainted_at = '2026-01-01T00:00:00Z'"
        " WHERE csession_id = ?",
        (sid,),
    )
    return sid


def test_a_launch_spec_has_no_key_that_turns_the_flag_on(env: Env) -> None:
    sid = pending_trusted_run_tainted_after_start(env)
    spec = env.private_run(sid, 2) / "launch-spec.json"
    data = json.loads(spec.read_text())
    data["request"]["allow_tainted"] = {"tty": True}
    data["allow_tainted"] = {"tty": True}
    spec.write_text(json.dumps(data))
    proc, out = env.adopt(sid)
    assert proc.returncode == 12, out
    assert (out["outcome"], out["end_reason"]) == ("not-started", "session_dir_unsafe")
    assert tainted_records(env) == []
    assert len(starts(env)) == 1
    assert is_tainted(env, sid)


def test_an_untouched_launch_spec_of_a_run_tainted_after_start_is_refused_as_tainted(
    env: Env,
) -> None:
    sid = pending_trusted_run_tainted_after_start(env)
    proc, out = env.adopt(sid)
    assert proc.returncode == 20, out
    assert out["error"]["reason"] == "tainted"
    assert tainted_records(env) == []
    assert len(starts(env)) == 1
    assert is_tainted(env, sid)


def test_label_edits_and_resume_chains_never_clear_the_taint(env: Env) -> None:
    sid = tainted_session(env)
    for argv in (
        ["label", "remove", sid, "kind=default"],
        ["label", "add", sid, "kind=draft-ticket"],
        ["label", "add", sid, "tainted=false"],
    ):
        run_cs(env, argv, json_out=False)
        assert is_tainted(env, sid)
    # A third-party-reading resume keeps it; a trusted one is still refused.
    proc, _out = resume(env, sid, "--reads-third-party", "--sandbox", "off")
    assert proc.returncode == 0, proc.stderr
    assert is_tainted(env, sid)
    proc, out = resume(env, sid, *TRUSTED)
    assert proc.returncode == 20 and out["error"]["reason"] == "tainted"
    # --reset cannot drop reads-third-party.
    proc, out = resume(env, sid, "--reset", "reads-third-party")
    assert proc.returncode == 2
    # A third-party-reading fork of a fork stays tainted down the chain.
    proc, out = resume(env, sid, "--fork", "--reads-third-party", "--sandbox", "off")
    assert proc.returncode == 0, proc.stderr
    fork = out["csession_id"]
    assert is_tainted(env, fork) and claude_id(env, fork) is not None
    proc, out = resume(env, fork, "--fork", "--reads-third-party", "--sandbox", "off")
    assert proc.returncode == 0, proc.stderr
    assert is_tainted(env, out["csession_id"])
    proc, out = resume(env, out["csession_id"], *TRUSTED, "--fork")
    assert proc.returncode == 20 and out["error"]["reason"] == "tainted"
    assert tainted_records(env) == []
