"""Golden ``--json`` outputs of the launch commands (interface.md, Commands and Output by
exit code): ``start``, ``exec`` and ``resume`` run end to end against the fake ``claude``, and
the ``pre-launch`` and ``post-state-change`` hook payloads they send. Each output is
validated against its schema first (``assert_golden``), then compared with
``tests/golden/[hooks/]<schema>-<case>.json`` after the volatile values are replaced."""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from claude_session import envbuild

from .golden import assert_golden
from .test_exec_hooks import HOOK as POST_HOOK
from .test_exec_prelaunch import HOOK as PRE_HOOK
from .test_resume import STEPS, exec_session, resume, run_cs
from .test_start_adopt import Env

TS = "2026-01-01T00:00:00.000000Z"  # valid placeholders: goldens are schema-validated too
CLAUDE_ID = "c0ffee00-0000-4000-8000-000000000001"
SID = re.compile(r"cs-[a-z2-7]{8}")
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def env(tmp_path: Path, _private_state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """A configured state directory and a fake ``claude`` that reports done."""
    return make_env(tmp_path, _private_state_dir, monkeypatch)


def make_env(tmp_path: Path, private_state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """The ``env`` fixture's state, without the caller's built-in ``claude_auth_env`` variables
    (the default config adds none; the provider flags among them gate the conditional ones):
    they would appear in ``required_env_names`` and make the goldens machine-dependent."""
    for name in envbuild.AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    state = private_state_dir / "claude-session"
    e = Env(tmp_path, state, work, tmp_path / "config.yaml", tmp_path / "fake.jsonl")
    e.scenario(STEPS)
    e.write_config()
    return e


def scrubber(env: Env) -> Any:
    """Replace session ids, times, UUIDs and this test's paths in every string."""
    paths = [
        (str(env.state), "<state>"),
        (str(env.work), "<cwd>"),
        (str(env.tmp_path), "<tmp>"),
    ]

    def text(s: str) -> str:
        for old, new in paths:
            s = s.replace(old, new)
        return UUID.sub(CLAUDE_ID, TIMESTAMP.sub(TS, SID.sub("cs-aaaaaaaa", s)))

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return text(value)
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        return value

    return scrub


def test_start_prints_the_start_object(env: Env) -> None:
    out = env.started("--mode", "headless")
    scrub = scrubber(env)

    def normalize(value: dict[str, Any]) -> dict[str, Any]:
        value = scrub(value)
        value["argv"][0] = "<claude-session>"  # the executable of this installation
        return value

    assert_golden(out, "start-new", normalize=normalize)


def test_the_start_golden_does_not_depend_on_the_callers_auth_env(
    tmp_path: Path, _private_state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    test_start_prints_the_start_object(make_env(tmp_path, _private_state_dir, monkeypatch))


def test_exec_prints_the_outcome_object(env: Env) -> None:
    out = exec_session(env)
    assert_golden(out, "exec-done", normalize=scrubber(env))


def test_resume_prints_the_outcome_object_of_the_new_run(env: Env) -> None:
    sid = exec_session(env)["csession_id"]
    proc, out = resume(env, sid)
    assert proc.returncode == 0, proc.stderr
    assert_golden(out, "resume-done", normalize=scrubber(env))


def test_a_pre_launch_hook_gets_the_pre_launch_payload(env: Env) -> None:
    log = env.tmp_path / "hook.jsonl"
    cmd = shlex.join([sys.executable, "-I", str(PRE_HOOK), "--log", str(log), "--name", "gate"])
    env.write_config(f"hooks:\n  pre-launch:\n    - {json.dumps({'cmd': cmd})}\n")
    prompt = env.tmp_path / "prompt.md"
    prompt.write_text("Do the task.\n", encoding="utf-8")
    proc, out = run_cs(
        env,
        [
            "exec",
            "--json",
            "--mode",
            "headless",
            "--prompt-file",
            str(prompt),
            "--actor",
            "auto:ci",
        ],
    )
    assert proc.returncode == 0, proc.stderr
    (record,) = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    scrub = scrubber(env)

    def normalize(payload: dict[str, Any]) -> dict[str, Any]:
        payload = scrub(payload)
        # Names of the inherited environment depend on the machine: keep claude-session's own.
        assert {"CSESSION_ID", "CSESSION_DIR", "CSESSION_KIND"} <= set(payload["env_names"])
        payload["env_names"] = [n for n in payload["env_names"] if n.startswith("CSESSION_")]
        return payload

    assert_golden(record["payload"], "hooks/pre-launch-exec", normalize=normalize)


def test_a_post_state_change_hook_gets_the_final_event_payload(env: Env) -> None:
    log = env.tmp_path / "hook.jsonl"
    cmd = shlex.join([sys.executable, "-I", str(POST_HOOK), "--log", str(log)])
    env.write_config(f"hooks:\n  post-state-change:\n    - {json.dumps({'cmd': cmd})}\n")
    out = exec_session(env)
    payloads = [json.loads(line)["payload"] for line in log.read_text().splitlines()]
    final = [p for p in payloads if p["final"]]
    assert len(final) == 1 and final[0]["object_id"] == out["csession_id"]
    assert_golden(final[0], "hooks/post-state-change-final", normalize=scrubber(env))
