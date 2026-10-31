"""``pre-launch`` hooks and ``--override-escalation`` end to end (interface.md, Launch order step
6, Hooks, Flag rules and Audit and log; design.md, Lifecycle hooks): real ``claude-session``
processes run the fake ``claude`` and ``tests/fakebin/pre_hook.py`` as the hook, which logs its
PID, process group, payload and environment and answers as told.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from claude_session import audit, launch, lifecycle, toolhooks
from claude_session.errors import LaunchValidationError
from claude_session.toolhooks import Entry

from .schemas import validate
from .test_exec_headless import SECRET_VALUE, claude_session_cmd, validate_exec
from .test_resume import (
    STEPS,
    adopt_in_process,
    exec_session,
    resume,
    resume_in_process,
    run_cs,
    starts,
)
from .test_start_adopt import Env

HOOK = Path(__file__).parent / "fakebin" / "pre_hook.py"
PAYLOAD_SCHEMA = "claude-session/hooks/pre-launch.json"
AUDIT_SCHEMA = "claude-session/audit.json"
LINUX = sys.platform.startswith("linux")
HUMAN = ("--actor", "human:alice")


@pytest.fixture
def env(tmp_path: Path, _private_state_dir: Path) -> Env:
    work = tmp_path / "work"
    work.mkdir()
    state = _private_state_dir / "claude-session"
    e = Env(tmp_path, state, work, tmp_path / "config.yaml", tmp_path / "fake.jsonl")
    e.scenario(STEPS)
    e.write_config()
    return e


def hook_cmd(env: Env, *args: str, name: str = "hook") -> str:
    log = env.tmp_path / "hook.jsonl"
    return shlex.join([sys.executable, "-I", str(HOOK), "--log", str(log), "--name", name, *args])


def hooks(env: Env, *entries: dict[str, Any], extra: str = "") -> None:
    """Configure these ``pre-launch`` entries."""
    lines = "".join(f"    - {json.dumps(entry)}\n" for entry in entries)
    env.write_config(f"{extra}hooks:\n  pre-launch:\n{lines}")


def calls(env: Env) -> list[dict[str, Any]]:
    path = env.tmp_path / "hook.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def log_records(env: Env) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (env.state / "log.jsonl").read_text().splitlines()]


def audit_records(env: Env) -> list[dict[str, Any]]:
    path = env.state / "audit.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines()]
    for record in records:
        validate(record, AUDIT_SCHEMA)
    return records


def exec_out(env: Env, *args: str) -> tuple[int, dict[str, Any]]:
    prompt = env.tmp_path / "prompt.md"
    prompt.write_text("Do the task.\n", encoding="utf-8")
    proc, out = run_cs(
        env, ["exec", "--json", "--mode", "headless", "--prompt-file", str(prompt), *args]
    )
    validate_exec(out)
    assert out["exit_code"] == proc.returncode, proc.stderr
    return proc.returncode, out


def config_hash(env: Env, cmd: str, index: int = 1) -> str:
    """The hash of a configured entry without selectors."""
    return toolhooks.entry_hash(Entry(cmd=cmd, timeout=30, origin="config", index=index))


def running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rpartition(b")")[2].split()[0]
    except OSError:
        return False
    return state not in (b"Z", b"X")


# exec


def test_hooks_that_continue_let_the_run_launch_with_a_valid_payload(env: Env) -> None:
    first = hook_cmd(env, "--stdout", '{"decision_id": ["hook-a", "hook-b"], "x": 1}', name="a")
    second = hook_cmd(env, "--stdout", "not json", name="b")
    hooks(env, {"cmd": first}, {"cmd": second}, extra="secret_env: [VAULT_TOKEN]\n")
    code, out = exec_out(env, "--decision-id", "argv-1", "--label", "team=x", "--actor", "auto:ci")
    assert code == 0 and out["outcome"] == "done"
    sid = out["csession_id"]
    got = calls(env)
    assert [c["name"] for c in got] == ["a", "b"]
    payload = got[0]["payload"]
    validate(payload, PAYLOAD_SCHEMA)
    assert (payload["object_id"], payload["run"], payload["pending"]) == (sid, 1, False)
    assert (payload["resume"], payload["fork"], payload["forked_from"]) == (False, False, None)
    assert payload["previous_run_kind"] is None and payload["override_escalation"] is False
    assert payload["decision_ids"] == ["argv-1"] and payload["actor"] == "auto:ci"
    assert payload["labels"] == ["kind=implement", "team=x"]
    assert payload["cwd"] == str(env.work.resolve()) and payload["has_prompt"] is True
    assert payload["run_kind"] == payload["launch_kind"] == "implement"
    assert payload["ts"] != got[1]["payload"]["ts"] or payload["ts"].endswith("Z")
    # The hook ran in the state root with the documented environment, no secret.
    record = got[0]
    assert record["cwd"] == str(env.state)
    assert record["env"]["CSESSION_HOOK"] == "pre-launch" and record["env"]["CSESSION_ID"] == sid
    assert "VAULT_TOKEN" not in record["env"]
    assert SECRET_VALUE not in json.dumps(got)
    row = env.run_row(sid)
    assert json.loads(row["decision_ids_json"]) == ["argv-1", "hook-a", "hook-b"]
    assert row["escalated_hooks"] is None
    (launched,) = [r for r in log_records(env) if r["event"] == "launch"]
    assert launched["decision_ids"] == ["argv-1", "hook-a", "hook-b"]
    assert [r for r in log_records(env) if r["event"] == "hook_output_ignored"]
    assert len(starts(env)) == 1


def test_the_secret_env_values_are_neither_in_the_hook_env_nor_its_payload(env: Env) -> None:
    hooks(env, {"cmd": hook_cmd(env)}, extra="secret_env: [VAULT_TOKEN]\n")
    proc = env.cs(
        ["exec", "--json", "--mode", "headless", "--skill", "s", "--config", str(env.config)],
        env={"VAULT_TOKEN": SECRET_VALUE, "PLAIN_VAR": "plain"},
    )
    assert proc.returncode == 0, proc.stderr
    (record,) = calls(env)
    assert "VAULT_TOKEN" not in record["env"] and record["env"]["PLAIN_VAR"] == "plain"
    assert SECRET_VALUE not in json.dumps(record)


def test_a_veto_ends_the_run_vetoed_with_exit_11_and_never_starts_claude(env: Env) -> None:
    vetoing = hook_cmd(env, "--exit", "3", "--stderr", "blocked: \x1bpolicy\n", name="v")
    hooks(env, {"cmd": vetoing}, {"cmd": hook_cmd(env, name="never")})
    code, out = exec_out(env)
    assert code == 11
    assert (out["phase"], out["outcome"], out["end_reason"]) == ("launch", "vetoed", "hook_veto")
    assert (out["state"], out["exit_code"], out["cost_usd"]) == ("not-started", 11, None)
    assert out["message"] == "blocked: policy"
    assert [c["name"] for c in calls(env)] == ["v"]
    assert starts(env) == []
    sid = out["csession_id"]
    # The launch-phase end cleaned up: no private tmpdir, no secret index.
    assert not (env.private_run(sid) / "secret-index").exists()
    failed = [r for r in log_records(env) if r["event"] == "launch"]
    assert failed[-1]["reason"] == "hook_veto" and failed[-1]["level"] == "error"


def test_an_escalation_ends_the_run_escalated_with_exit_13(env: Env) -> None:
    cmd = hook_cmd(env, "--exit", "13", "--stderr", "ask a human", "--stdout",
                   '{"decision_id": "esc-1"}')  # fmt: skip
    hooks(env, {"cmd": cmd}, {"cmd": hook_cmd(env, name="after")})
    code, out = exec_out(env)
    assert code == 13
    assert (out["phase"], out["outcome"], out["end_reason"]) == (
        "launch",
        "escalated",
        "hook_escalate",
    )
    assert out["message"] == "ask a human" and out["state"] == "not-started"
    assert [c["name"] for c in calls(env)] == ["hook", "after"]  # later hooks still ran
    row = env.run_row(out["csession_id"])
    assert json.loads(row["escalated_hooks"]) == [config_hash(env, cmd)]
    assert json.loads(row["decision_ids_json"]) == ["esc-1"]
    assert starts(env) == []


@pytest.mark.skipif(not LINUX, reason="process groups are checked through /proc")
def test_a_hook_timeout_vetoes_with_hook_timeout_and_ends_its_group(env: Env) -> None:
    cmd = hook_cmd(env, "--ignore-term", "--child", "--sleep", "30")
    hooks(env, {"cmd": cmd, "timeout": "1s"}, extra="kill_grace: 0.5s\n")
    started = time.monotonic()
    code, out = exec_out(env)
    assert time.monotonic() - started < 30
    assert code == 11 and (out["outcome"], out["end_reason"]) == ("vetoed", "hook_timeout")
    (record,) = calls(env)
    deadline = time.monotonic() + 10
    while running(record["pid"]) or running(record["child"]):
        assert time.monotonic() < deadline, "the hook's group outlived its timeout"
        time.sleep(0.02)
    assert starts(env) == []


def test_selectors_choose_the_hooks_that_run(env: Env) -> None:
    hooks(
        env,
        {"cmd": hook_cmd(env, "--exit", "3", name="excluded"), "exclude_kinds": ["implement"]},
        {"cmd": hook_cmd(env, "--exit", "3", name="other"), "kinds": ["review"]},
        {"cmd": hook_cmd(env, "--exit", "3", name="unlabeled"), "labels": ["team=y"]},
        {"cmd": hook_cmd(env, name="chosen"), "kinds": ["implement"], "labels": ["team=x"]},
    )
    code, _out = exec_out(env, "--label", "team=x")
    assert code == 0
    assert [c["name"] for c in calls(env)] == ["chosen"]


def test_a_per_invocation_pre_launch_hook_runs_after_the_configured_ones(env: Env) -> None:
    hooks(env, {"cmd": hook_cmd(env, name="config")})
    flag = hook_cmd(env, "--exit", "5", name="flag")
    code, out = exec_out(env, "--hook", f"pre-launch={flag}", "--hook-timeout", "pre-launch=10s")
    assert code == 11 and out["outcome"] == "vetoed"
    assert [c["name"] for c in calls(env)] == ["config", "flag"]


def test_the_dry_run_runs_no_pre_launch_hook(env: Env) -> None:
    hooks(env, {"cmd": hook_cmd(env, "--exit", "3")})
    proc, out = run_cs(env, ["exec", "--json", "--mode", "headless", "--skill", "s",
                             "--dry-run"])  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    (planned,) = [h for h in out["hooks"] if h["hook"] == "pre-launch"]
    assert planned["selected"] is True
    assert calls(env) == []


# --override-escalation


def escalated_session(env: Env, cmd: str) -> str:
    hooks(env, {"cmd": cmd})
    code, out = exec_out(env)
    assert code == 13
    return out["csession_id"]


def test_override_escalation_overrides_the_recorded_entries_and_is_audited(env: Env) -> None:
    cmd = hook_cmd(env, "--exit", "13", "--stdout", '{"decision_id": "gate-7"}')
    sid = escalated_session(env, cmd)
    digest = config_hash(env, cmd)
    proc, out = resume(env, sid, "--override-escalation", *HUMAN, "--decision-id", "ticket-1")
    assert proc.returncode == 0, proc.stderr
    assert (out["run"], out["outcome"]) == (2, "done")
    payload = calls(env)[-1]["payload"]
    validate(payload, PAYLOAD_SCHEMA)
    assert payload["override_escalation"] is True and payload["resume"] is True
    assert payload["previous_run_kind"] == "implement" and payload["run"] == 2
    (record,) = audit_records(env)
    assert record["action"] == "launch_escalation_override" and record["result"] == "ok"
    assert record["actor"] == "human:alice" and record["target"] == sid
    assert record["decision_id"] == ["ticket-1", "gate-7"]
    assert record["inputs"] == {"run_kind": "implement", "run": 2, "overridden_hooks": [digest]}
    # The hook exited 13 again: still recorded, so a later override covers it too.
    assert json.loads(env.run_row(sid, 2)["escalated_hooks"]) == [digest]
    assert len(starts(env)) == 1


def test_override_escalation_does_not_cover_a_hook_that_did_not_escalate(env: Env) -> None:
    cmd = hook_cmd(env, "--exit", "13", name="first")
    sid = escalated_session(env, cmd)
    newcomer = hook_cmd(env, "--exit", "13", "--stderr", "new gate", name="second")
    hooks(env, {"cmd": cmd}, {"cmd": newcomer})
    proc, out = resume(env, sid, "--override-escalation", *HUMAN)
    assert proc.returncode == 13 and out["outcome"] == "escalated"
    assert out["message"] == "new gate"
    (record,) = audit_records(env)
    assert record["result"] == "refused"
    assert record["inputs"]["overridden_hooks"] == [config_hash(env, cmd)]
    # Both exited 13: the next override covers both.
    assert json.loads(env.run_row(sid, 2)["escalated_hooks"]) == [
        config_hash(env, cmd),
        config_hash(env, newcomer, 2),
    ]
    proc, out = resume(env, sid, "--override-escalation", *HUMAN)
    assert proc.returncode == 0, proc.stderr
    assert [r["result"] for r in audit_records(env)] == ["refused", "ok"]


def test_override_escalation_never_overrides_a_veto(env: Env) -> None:
    cmd = hook_cmd(env, "--exit", "13", name="gate")
    sid = escalated_session(env, cmd)
    hooks(env, {"cmd": cmd}, {"cmd": hook_cmd(env, "--exit", "3", name="veto")})
    proc, out = resume(env, sid, "--override-escalation", *HUMAN)
    assert proc.returncode == 11 and out["outcome"] == "vetoed"
    assert [r["result"] for r in audit_records(env)] == ["refused"]


def test_an_edited_hook_is_not_covered_by_the_override(env: Env) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    hooks(env, {"cmd": hook_cmd(env, "--exit", "13", "--stderr", "edited")})
    proc, out = resume(env, sid, "--override-escalation", *HUMAN)
    assert proc.returncode == 13 and out["message"] == "edited"


@pytest.mark.parametrize(
    "actor",
    [(), ("--actor", "auto:ci"), ("--actor", "human:"), ("--actor", "human:  "), ("--actor", "x")],
)
def test_override_escalation_is_human_only(env: Env, actor: tuple[str, ...]) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    proc, out = resume(env, sid, "--override-escalation", *actor)
    assert proc.returncode == 2 and out["error"]["code"] == "usage"
    assert "human-only" in out["error"]["message"]
    assert env.rows("SELECT COUNT(*) FROM runs WHERE csession_id = ?", (sid,)) == [(1,)]
    assert audit_records(env) == []


@pytest.mark.parametrize("exit_code", ["0", "3"])
def test_override_escalation_needs_a_previous_run_that_ended_escalated(
    env: Env, exit_code: str
) -> None:
    hooks(env, {"cmd": hook_cmd(env, "--exit", exit_code)})
    _code, out = exec_out(env)
    sid = out["csession_id"]
    proc, out = resume(env, sid, "--override-escalation", *HUMAN)
    assert proc.returncode == 2 and "ended escalated" in out["error"]["message"]
    assert env.rows("SELECT COUNT(*) FROM runs WHERE csession_id = ?", (sid,)) == [(1,)]


def test_override_escalation_with_fork_is_a_usage_error(env: Env) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    proc, out = resume(env, sid, "--fork", "--override-escalation", *HUMAN)
    assert proc.returncode == 2 and out["error"]["code"] == "usage"


def test_an_override_audit_that_cannot_be_written_interrupts_the_launch(env: Env) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    elsewhere = env.tmp_path / "elsewhere"
    elsewhere.write_text("")
    os.symlink(elsewhere, env.state / "audit.jsonl")  # refused: never written through a link
    proc, out = resume(env, sid, "--override-escalation", *HUMAN)
    assert proc.returncode == 12 and out["end_reason"] == "launch_interrupted"
    assert starts(env) == [] and elsewhere.read_text() == ""


def test_a_refused_override_record_that_cannot_be_written_is_logged(env: Env) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    hooks(env, {"cmd": hook_cmd(env, "--exit", "13", "--stderr", "edited")})
    elsewhere = env.tmp_path / "elsewhere"
    elsewhere.write_text("")
    os.symlink(elsewhere, env.state / "audit.jsonl")  # refused: never written through a link
    proc, out = resume(env, sid, "--override-escalation", *HUMAN, "--decision-id", "ticket-9")
    assert proc.returncode == 13 and out["outcome"] == "escalated"
    assert elsewhere.read_text() == ""
    (failed,) = [r for r in log_records(env) if r["event"] == "audit_failed"]
    validate(failed, "claude-session/log.json")
    assert failed["level"] == "warn" and failed["csession_id"] == sid
    assert failed["message"] == "a launch_escalation_override refused record could not be written"
    assert "ticket-9" not in json.dumps(failed)


def test_a_launch_that_fails_after_the_override_was_audited_gets_an_error_record(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))

    def failing(*_args: Any, **_kwargs: Any) -> Any:
        raise LaunchValidationError("step 7 broke", end_reason="session_dir_unsafe")

    monkeypatch.setattr(launch, "prepare", failing)
    code, out = resume_in_process(env, monkeypatch, capsys, sid, "--override-escalation", *HUMAN)
    assert code == 12 and out["end_reason"] == "session_dir_unsafe"
    assert [r["result"] for r in audit_records(env)] == ["ok", "error"]
    first, second = audit_records(env)
    assert first["inputs"] == second["inputs"] and first["decision_id"] == second["decision_id"]


# start and adoption


def test_the_adopting_exec_runs_the_hooks_with_pending_true(env: Env) -> None:
    hooks(env, {"cmd": hook_cmd(env, "--exit", "4", "--stderr", "not now")})
    out = env.started()
    assert calls(env) == []  # start never runs pre-launch hooks
    proc, out = env.adopt(out["csession_id"])
    assert proc.returncode == 11 and out["outcome"] == "vetoed" and out["message"] == "not now"
    (record,) = calls(env)
    validate(record["payload"], PAYLOAD_SCHEMA)
    assert record["payload"]["pending"] is True
    assert starts(env) == []


def test_adoption_uses_the_current_configs_hooks(env: Env) -> None:
    out = env.started()
    hooks(env, {"cmd": hook_cmd(env, name="added later")})
    proc, _ = env.adopt(out["csession_id"])
    assert proc.returncode == 0, proc.stderr
    assert [c["name"] for c in calls(env)] == ["added later"]


def test_start_resume_override_escalation_is_checked_at_start_and_audited_at_adoption(
    env: Env,
) -> None:
    cmd = hook_cmd(env, "--exit", "13")
    sid = escalated_session(env, cmd)
    proc, out = run_cs(env, ["start", "--json", "--resume", sid, "--override-escalation"])
    assert proc.returncode == 2 and "human-only" in out["error"]["message"]
    proc, out = run_cs(env, ["start", "--json", "--resume", sid, "--override-escalation", *HUMAN])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert audit_records(env) == []  # audited at adoption, not at start
    proc, out = env.adopt(sid)
    assert proc.returncode == 0, proc.stderr
    assert calls(env)[-1]["payload"]["override_escalation"] is True
    assert calls(env)[-1]["payload"]["pending"] is True
    (record,) = audit_records(env)
    assert record["result"] == "ok" and record["inputs"]["overridden_hooks"] == [
        config_hash(env, cmd)
    ]


def edit_spec(env: Env, sid: str, run: int, edit: Any) -> None:
    path = env.private_run(sid, run) / lifecycle.SPEC_NAME
    data = json.loads(path.read_text())
    edit(data["request"])
    path.write_text(json.dumps(data))


def test_a_spec_and_run_without_the_override_key_read_as_no_override(env: Env) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    proc, _ = run_cs(env, ["start", "--json", "--resume", sid, "--override-escalation", *HUMAN])
    assert proc.returncode == 0, proc.stderr
    # As a claude-session from before the flag wrote them: no key in either.
    edit_spec(env, sid, 2, lambda request: request.pop("override_escalation"))
    invocation = json.loads(env.run_row(sid, 2)["invocation_json"])
    assert invocation.pop("override_escalation") is True
    env.write_db(
        "UPDATE runs SET invocation_json = ? WHERE csession_id = ? AND run_no = 2",
        (json.dumps(invocation), sid),
    )
    proc, out = env.adopt(sid)
    assert proc.returncode == 13 and out["outcome"] == "escalated"
    assert audit_records(env) == []


def test_a_spec_that_turns_the_override_on_against_the_run_row_is_refused(env: Env) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    proc, _ = run_cs(env, ["start", "--json", "--resume", sid, *HUMAN])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(env.run_row(sid, 2)["invocation_json"])["override_escalation"] is False
    edit_spec(env, sid, 2, lambda request: request.update(override_escalation=True))
    proc, out = env.adopt(sid)
    assert proc.returncode == 12 and out["end_reason"] == "session_dir_unsafe"
    assert len(calls(env)) == 1  # only run 1's hook: the adoption never reached step 6
    assert audit_records(env) == [] and len(starts(env)) == 0


def test_a_frozen_override_needs_a_human_frozen_actor(env: Env) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    proc, _ = run_cs(env, ["start", "--json", "--resume", sid, "--override-escalation", *HUMAN])
    assert proc.returncode == 0, proc.stderr
    edit_spec(env, sid, 2, lambda request: request.update(actor="auto:ci"))
    proc, out = env.adopt(sid)
    assert proc.returncode == 12 and out["end_reason"] == "session_dir_unsafe"
    assert audit_records(env) == []


def test_the_adoption_dry_run_reports_the_frozen_override(env: Env) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    proc, _ = run_cs(env, ["start", "--json", "--resume", sid, "--override-escalation", *HUMAN])
    assert proc.returncode == 0, proc.stderr
    proc, out = env.adopt(sid, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out["override_escalation"] is True


# Signals and crashes during step 6


def test_a_signal_during_the_override_ok_record_still_gets_the_error_record(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = escalated_session(env, hook_cmd(env, "--exit", "13"))
    real = audit.append

    def append(*args: Any, **kwargs: Any) -> Any:
        written = real(*args, **kwargs)
        if kwargs.get("result") == "ok":
            os.kill(os.getpid(), signal.SIGINT)  # right after the ok record is written
        return written

    monkeypatch.setattr(audit, "append", append)
    code, out = resume_in_process(env, monkeypatch, capsys, sid, "--override-escalation", *HUMAN)
    assert code == 12 and out["end_reason"] == "launch_interrupted"
    assert [r["result"] for r in audit_records(env)] == ["ok", "error"]
    assert starts(env) == []


def test_a_signal_during_a_later_hook_keeps_the_decision_ids_already_returned(env: Env) -> None:
    hooks(
        env,
        {"cmd": hook_cmd(env, "--stdout", '{"decision_id": "d1"}', name="first")},
        {"cmd": "kill -INT $PPID; sleep 30"},  # $PPID: the exec running the hook
    )
    code, out = exec_out(env, "--decision-id", "argv-1")
    assert code == 12 and out["end_reason"] == "launch_interrupted"
    row = env.run_row(out["csession_id"])
    assert json.loads(row["decision_ids_json"]) == ["argv-1", "d1"]
    (logged,) = [r for r in log_records(env) if r["event"] == "launch_interrupted"]
    assert logged["decision_ids"] == ["argv-1", "d1"]
    assert starts(env) == []


@pytest.mark.skipif(not LINUX, reason="process groups are checked through /proc")
def test_reconcile_ends_a_pre_launch_hook_whose_exec_was_killed(env: Env) -> None:
    hooks(env, {"cmd": hook_cmd(env, "--sleep", "60"), "timeout": "120s"})
    prompt = env.tmp_path / "prompt.md"
    prompt.write_text("Do the task.\n", encoding="utf-8")
    argv = ["exec", "--json", "--mode", "headless", "--prompt-file", str(prompt)]
    exec_proc = subprocess.Popen(
        [*claude_session_cmd(), *argv, "--config", str(env.config)],
        cwd=env.work,
        env=env.environ(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not calls(env):
            assert time.monotonic() < deadline, "the hook never started"
            time.sleep(0.05)
        (record,) = calls(env)
        # The hook's group was recorded while it ran, held by this exec.
        groups = env.rows("SELECT pgid, event_id, holder_pid FROM hook_groups", ())
        assert groups == [(record["pgid"], 0, exec_proc.pid)]
    finally:
        exec_proc.kill()  # the exact process this test started
        exec_proc.wait(timeout=30)
    assert running(record["pid"])  # left behind by the killed exec
    proc, _out = run_cs(env, ["reconcile", "--json"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    deadline = time.monotonic() + 10
    while running(record["pid"]):
        assert time.monotonic() < deadline, "reconcile did not end the orphaned hook"
        time.sleep(0.02)
    assert env.rows("SELECT COUNT(*) FROM hook_groups", ()) == [(0,)]
    (ended,) = [r for r in log_records(env) if r["event"] == "hook_failed"]
    assert ended["message"].startswith("pre-launch: the hook its dead exec left running")
    sid = env.rows("SELECT csession_id FROM sessions", ())[0][0]
    assert env.run_row(sid)["end_reason"] == "launch_interrupted"


# Order: step 6 before the fork taint re-check


def test_a_fork_source_tainted_while_the_hooks_run_is_still_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = exec_session(env)["csession_id"]
    taint = (
        "UPDATE sessions SET tainted = 1, tainted_at = '2026-01-01T00:00:00Z' "
        f"WHERE csession_id = '{sid}'"
    )
    hooks(env, {"cmd": hook_cmd(env, "--sql", taint)})
    proc, out = resume(env, sid, "--fork")
    assert proc.returncode == 20 and out["error"]["reason"] == "tainted", out
    (fork,) = [r[0] for r in env.rows("SELECT csession_id FROM sessions WHERE forked_from = ?",
                                      (sid,))]  # fmt: skip
    assert env.run_row(fork)["end_reason"] == "launch_interrupted"
    assert len(calls(env)) == 1 and calls(env)[0]["payload"]["fork"] is True
    assert calls(env)[0]["payload"]["forked_from"] == sid
    assert len(starts(env)) == 1  # the source's run only


def test_an_adopted_fork_runs_its_hooks_before_the_taint_re_check(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = run_cs(env, ["start", "--json", "--resume", sid, "--fork"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    fork = out["csession_id"]
    taint = (
        "UPDATE sessions SET tainted = 1, tainted_at = '2026-01-01T00:00:00Z' "
        f"WHERE csession_id = '{sid}'"
    )
    hooks(env, {"cmd": hook_cmd(env, "--sql", taint)})
    code, out = adopt_in_process(env, monkeypatch, capsys, fork)
    assert code == 20 and out["error"]["reason"] == "tainted", out
    assert len(calls(env)) == 1 and calls(env)[0]["payload"]["pending"] is True
    assert len(starts(env)) == 1
