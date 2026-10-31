"""The scenario-driven fake ``claude`` (tests/fakebin). Hook commands here are recorder stubs,
never ``claude-session hook-event``; ``report`` runs against a stand-in ``claude-session``.
Every test that signals the fake signals only the exact child it started."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from claude_session.gateway import DEVNULL, Child, Result, SystemGateway

from .fake_gateway import FakeGateway
from .fakeclaude import (
    FAKE_CLAUDE,
    INJECTED_HOOKS,
    claude_bin,
    fake_env,
    hook_settings,
    read_jsonl,
    recorder,
    recording_settings,
    report_stub,
    wait_for,
    write_json,
)

SID = "0b9f4a8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
OTHER_SID = "7e6d5c4b-3a29-4180-9f8e-7d6c5b4a3928"
HEADLESS = ["-p", "--output-format", "stream-json", "--verbose"]
FAKE_ERROR = 99


class Env:
    """One test's files: scenario, fake log, hook log, settings, cwd."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.scenario = tmp / "scenario.json"
        self.log = tmp / "fake.jsonl"
        self.hooks = tmp / "hooks.jsonl"
        self.settings = tmp / "settings.json"
        self.cwd = tmp / "work"
        self.cwd.mkdir()
        self.bin = tmp / "bin"

    def setup(
        self,
        steps: list[dict[str, Any]],
        *,
        settings: dict[str, Any] | None = None,
        **scenario: Any,
    ) -> None:
        write_json(self.scenario, {"steps": steps, **scenario})
        write_json(self.settings, recording_settings(self.hooks) if settings is None else settings)

    def env(self, **extra: str) -> dict[str, str]:
        return {**fake_env(self.scenario, self.log), "CSESSION_ID": "cs-abcd2345", **extra}

    def argv(self, *args: str) -> list[str]:
        return [str(FAKE_CLAUDE), "--settings", str(self.settings), *args]

    def run(self, *args: str, timeout: float = 30, **extra: str) -> Result:
        return SystemGateway().run(
            self.argv(*args), extra_env=self.env(**extra), cwd=str(self.cwd), timeout=timeout
        )

    def spawn(self, *args: str) -> Child:
        gw = FakeGateway(spawn_via=SystemGateway())
        child = gw.spawn(self.argv(*args), extra_env=self.env(), cwd=str(self.cwd), stdin=DEVNULL)
        assert gw.calls[0].kind == "spawn"
        return child

    def hook_calls(self) -> list[dict[str, Any]]:
        """``(event, payload)`` of every hook the fake ran, in order."""
        return [
            {"event": r["args"][0], "payload": json.loads(r["stdin"]), **r}
            for r in read_jsonl(self.hooks)
        ]

    def records(self, event: str) -> list[dict[str, Any]]:
        return [r for r in read_jsonl(self.log) if r["event"] == event]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def stream(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


# --- argv, version, scenario loading -------------------------------------------------------


def test_version_needs_no_scenario() -> None:
    result = SystemGateway().run([str(FAKE_CLAUDE), "--version"], timeout=30)
    assert (result.returncode, result.stdout) == (0, "2.1.312 (Claude Code)\n")


def test_version_can_be_overridden(env: Env) -> None:
    result = SystemGateway().run(
        [str(FAKE_CLAUDE), "--version"], extra_env={"FAKE_CLAUDE_VERSION": "2.0.1"}, timeout=30
    )
    assert result.stdout == "2.0.1\n"
    env.setup([], version="9.9.9 (Claude Code)")
    assert env.run("--version").stdout == "9.9.9 (Claude Code)\n"


def test_without_a_scenario_it_fails_loudly() -> None:
    result = SystemGateway().run([str(FAKE_CLAUDE), "-p", "hi"], timeout=30)
    assert result.returncode == FAKE_ERROR
    assert "FAKE_CLAUDE_SCENARIO" in result.stderr


def test_the_documented_argv_is_accepted_and_logged(env: Env) -> None:
    env.setup([])
    args = [
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--model",
        "m",
        "--add-dir",
        "/a",
        "--plugin-dir",
        "/p",
        "--add-dir",
        "/p",
        "--append-system-prompt-file",
        "/i.md",
        "--system-prompt-snapshot",
        "off",
        "--debug",
        "--resume",
        SID,
        "--fork-session",
        "--session-id",
        OTHER_SID,
        *HEADLESS,
        "--max-turns",
        "6",
        "/plugin:skill go",
    ]
    result = env.run(*args)
    assert result.returncode == 0, result.stderr
    (start,) = env.records("start")
    assert start["flags"]["--add-dir"] == ["/a", "/p"]
    assert start["flags"]["--setting-sources"] == ""
    assert start["prompt"] == "/plugin:skill go"
    assert start["headless"] is True
    assert start["session_id"] == OTHER_SID  # a fork takes its pre-assigned id
    assert start["cwd"] == str(env.cwd)
    assert env.records("exit") == [env.records("exit")[0]] and env.records("exit")[0]["code"] == 0


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--bogus"], "unknown option"),
        (["--model"], "argument missing"),
        (["--model", "a", "--model", "b"], "more than once"),
        (["one", "two"], "too many arguments"),
        (["--resume", SID, "--session-id", OTHER_SID], "--fork-session"),
        (["--fork-session"], "requires --resume"),
        (["-p", "--output-format", "stream-json"], "requires --verbose"),
        (["--output-format", "json"], "only works with --print"),
        (["-p", "--output-format", "yaml", "--verbose"], "invalid --output-format"),
        (["--session-id", "not-a-uuid"], "valid UUID"),
        (["--resume", "'; rm -rf /'"], "valid UUID"),
        (["-p", "--max-turns", "0"], "invalid --max-turns"),
    ],
)
def test_invalid_argv_is_rejected_like_claude(env: Env, args: list[str], message: str) -> None:
    env.setup([{"do": "touch", "path": str(env.tmp / "ran")}])
    result = env.run(*args)
    assert result.returncode == 1
    assert message in result.stderr
    assert not (env.tmp / "ran").exists()


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ({"steps": [], "stepz": []}, "unknown scenario keys"),
        ({"steps": [{"do": "dance"}]}, "unknown step"),
        ({"steps": [{"do": "stop", "background": []}]}, "unknown keys"),
        ({"steps": [{"do": "notification", "notification_type": "auth"}]}, "notification_type"),
        ({"steps": [{"do": "session_start", "source": "boot"}]}, "source"),
        ({"steps": [{"do": "exit", "code": 256}]}, "0..255"),
        ({"steps": [{"do": "exit", "code": True}]}, "wrong type"),
        ({"steps": [{"do": "stop", "on_block": [{"do": "nope"}]}]}, "on_block[0]"),
        ({"steps": [{"do": "report", "args": ["done"], "heredoc": "a\nEOF\nb"}]}, "EOF"),
        ({"steps": [], "signals": {"KILL": "ignore"}}, "unknown signal"),
        ({"steps": "all"}, "must be a list"),
    ],
)
def test_a_broken_scenario_fails_loudly(env: Env, scenario: dict[str, Any], message: str) -> None:
    write_json(env.scenario, scenario)
    write_json(env.settings, {})
    result = env.run("-p", "x")
    assert result.returncode == FAKE_ERROR
    assert message in result.stderr


@pytest.mark.parametrize(
    "settings",
    [
        "[]",
        '{"hooks": []}',
        '{"hooks": {"Stop": [{"command": "x"}]}}',
        '{"hooks": {"Stop": [{"hooks": [{"type": "prompt", "prompt": "x"}]}]}}',
        '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x", "timeout": 0}]}]}}',
        "{not json",
    ],
)
def test_broken_settings_fail_loudly(env: Env, settings: str) -> None:
    env.setup([])
    env.settings.write_text(settings, encoding="utf-8")
    assert env.run("-p", "x").returncode == FAKE_ERROR


def test_inline_settings_json_works(env: Env) -> None:
    env.setup([{"do": "stop"}])
    inline = json.dumps(recording_settings(env.hooks))
    result = SystemGateway().run(
        [str(FAKE_CLAUDE), "--settings", inline, "-p", "x"],
        extra_env=env.env(),
        cwd=str(env.cwd),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert [c["event"] for c in env.hook_calls()] == ["Stop"]


def test_no_settings_means_no_hooks(env: Env) -> None:
    env.setup([{"do": "session_start"}, {"do": "stop"}])
    result = SystemGateway().run(
        [str(FAKE_CLAUDE), "-p", "x"], extra_env=env.env(), cwd=str(env.cwd), timeout=30
    )
    assert result.returncode == 0
    assert env.hook_calls() == []


# --- hook events and payloads --------------------------------------------------------------


def test_every_injected_event_gets_its_documented_payload(env: Env) -> None:
    env.setup(
        [
            {"do": "session_start"},
            {"do": "user_prompt_submit"},
            {"do": "permission_request", "tool_name": "Bash", "tool_input": {"command": "ls"}},
            {"do": "notification", "notification_type": "permission_prompt"},
            {
                "do": "post_tool_use",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"stdout": "a\n", "stderr": ""},
            },
            {"do": "notification", "notification_type": "elicitation_dialog"},
            {"do": "stop"},
            {"do": "notification", "notification_type": "idle_prompt"},
            {"do": "session_end", "reason": "prompt_input_exit"},
        ]
    )
    assert env.run("--session-id", SID).returncode == 0
    calls = env.hook_calls()
    assert [c["event"] for c in calls] == [
        "SessionStart",
        "UserPromptSubmit",
        "PermissionRequest",
        "Notification",
        "PostToolUse",
        "Notification",
        "Stop",
        "Notification",
        "SessionEnd",
    ]
    payloads = [c["payload"] for c in calls]
    assert all(p["session_id"] == SID for p in payloads)
    transcript = payloads[0]["transcript_path"]
    project = re.sub(r"[^A-Za-z0-9]", "-", str(env.cwd))
    assert transcript.endswith(f"/.claude/projects/{project}/{SID}.jsonl")
    assert not Path(transcript).exists()
    assert payloads == [
        {"session_id": SID, "transcript_path": transcript, "source": "startup"},
        {"session_id": SID},
        {"session_id": SID, "tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"session_id": SID, "notification_type": "permission_prompt"},
        {
            "session_id": SID,
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"stdout": "a\n", "stderr": ""},
        },
        {"session_id": SID, "notification_type": "elicitation_dialog"},
        {"session_id": SID, "stop_hook_active": False, "background_tasks": []},
        {"session_id": SID, "notification_type": "idle_prompt"},
        {"session_id": SID, "reason": "prompt_input_exit"},
    ]


def test_hooks_run_in_the_cwd_with_the_claude_environment(env: Env) -> None:
    env.setup([{"do": "stop"}])
    assert env.run("-p", "x", CSESSION_DIR="/s/cs-abcd2345").returncode == 0
    (call,) = env.hook_calls()
    assert call["cwd"] == str(env.cwd)
    assert call["env"]["CSESSION_ID"] == "cs-abcd2345"
    assert call["env"]["CSESSION_DIR"] == "/s/cs-abcd2345"
    # Each hook runs in its own process group (led by its ``sh``), not the fake's (ours).
    (start,) = env.records("start")
    assert call["pgid"] not in (os.getpgid(0), start["pid"])


@pytest.mark.parametrize(
    ("args", "source", "session_id"),
    [
        ([], "startup", None),
        (["--session-id", SID], "startup", SID),
        (["--resume", SID], "resume", SID),
        (["--resume", SID, "--fork-session", "--session-id", OTHER_SID], "fork", OTHER_SID),
    ],
)
def test_session_start_source_and_id_follow_argv(
    env: Env, args: list[str], source: str, session_id: str | None
) -> None:
    env.setup([{"do": "session_start"}])
    assert env.run(*args).returncode == 0
    (call,) = env.hook_calls()
    assert call["payload"]["source"] == source
    if session_id is None:
        assert uuid.UUID(call["payload"]["session_id"])
    else:
        assert call["payload"]["session_id"] == session_id


def test_clear_changes_the_session_id_and_compact_keeps_it(env: Env) -> None:
    env.setup(
        [
            {"do": "session_start"},
            {"do": "session_end", "reason": "clear"},
            {"do": "session_start", "source": "clear"},
            {"do": "session_start", "source": "compact"},
            {"do": "stop"},
        ]
    )
    assert env.run("--session-id", SID).returncode == 0
    p = [c["payload"] for c in env.hook_calls()]
    assert p[1] == {"session_id": SID, "reason": "clear"}
    new = p[2]["session_id"]
    assert new != SID and uuid.UUID(new)
    assert p[2]["transcript_path"].endswith(f"/{new}.jsonl")
    assert (p[3]["session_id"], p[3]["source"], p[4]["session_id"]) == (new, "compact", new)


def test_ask_user_question_is_a_permission_request(env: Env) -> None:
    questions = {"questions": [{"question": "Which?", "options": ["a", "b"]}]}
    env.setup(
        [
            {"do": "permission_request", "tool_name": "AskUserQuestion", "tool_input": questions},
            {"do": "post_tool_use", "tool_name": "AskUserQuestion", "tool_input": questions},
        ]
    )
    assert env.run().returncode == 0
    req, post = (c["payload"] for c in env.hook_calls())
    assert (req["tool_name"], req["tool_input"]) == ("AskUserQuestion", questions)
    assert post["tool_response"] == {}


def test_post_tool_use_inside_a_subagent_keeps_the_parent_session_id(env: Env) -> None:
    env.setup(
        [{"do": "post_tool_use", "tool_name": "Read", "agent_id": "a1", "agent_type": "explorer"}]
    )
    assert env.run("--session-id", SID).returncode == 0
    (call,) = env.hook_calls()
    assert call["payload"] == {
        "session_id": SID,
        "tool_name": "Read",
        "tool_input": {},
        "tool_response": {},
        "agent_id": "a1",
        "agent_type": "explorer",
    }


def test_stop_with_background_tasks(env: Env) -> None:
    tasks = [{"id": "t1"}, "opaque"]
    env.setup([{"do": "stop", "background_tasks": tasks}, {"do": "stop"}])
    assert env.run().returncode == 0
    first, second = (c["payload"] for c in env.hook_calls())
    assert first["background_tasks"] == tasks
    assert second["background_tasks"] == []


def test_notification_matchers_select_only_their_entry(env: Env) -> None:
    env.setup([{"do": "notification", "notification_type": "idle_prompt"}])
    assert env.run().returncode == 0
    (call,) = env.hook_calls()
    assert call["payload"]["notification_type"] == "idle_prompt"
    assert len(env.records("hook_skipped")) == 2  # permission_prompt and elicitation_dialog


@pytest.mark.parametrize(
    ("matcher", "tool", "fires"),
    [
        (None, "Write", True),
        ("", "Write", True),
        ("*", "Write", True),
        ("Write", "Write", True),
        ("Edit|Write", "Write", True),
        ("Write", "WriteFile", False),
        ("Edit", "Write", False),
        ("mcp__.*", "mcp__srv__tool", True),
    ],
)
def test_tool_matchers(env: Env, matcher: str | None, tool: str, fires: bool) -> None:
    settings = hook_settings(lambda e: recorder(env.hooks, args=[e]), [("PostToolUse", matcher, 5)])
    env.setup([{"do": "post_tool_use", "tool_name": tool}], settings=settings)
    assert env.run().returncode == 0
    assert len(env.hook_calls()) == (1 if fires else 0)


def test_matcher_on_an_event_without_a_match_field_never_fires(env: Env) -> None:
    settings = hook_settings(lambda e: recorder(env.hooks, args=[e]), [("Stop", "Bash", 5)])
    env.setup([{"do": "stop"}], settings=settings)
    assert env.run().returncode == 0
    assert env.hook_calls() == []


def test_several_hooks_for_one_event_all_run_in_order(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e]), [("Stop", None, 5), ("Stop", None, 5)]
    )
    env.setup([{"do": "stop"}], settings=settings)
    assert env.run().returncode == 0
    assert [c["event"] for c in env.hook_calls()] == ["Stop", "Stop"]


# --- hostile payloads ----------------------------------------------------------------------


def test_payload_overrides_omissions_and_raw_payloads(env: Env) -> None:
    env.setup(
        [
            {"do": "session_start", "payload": {"source": "weird", "extra": [1]}},
            {"do": "stop", "omit": ["background_tasks", "stop_hook_active"]},
            {"do": "user_prompt_submit", "raw_payload": "{not json"},
            {"do": "session_end", "reason": "other", "raw_payload": ""},
        ]
    )
    assert env.run("--session-id", SID).returncode == 0
    records = read_jsonl(env.hooks)
    first = json.loads(records[0]["stdin"])
    assert (first["source"], first["extra"]) == ("weird", [1])
    assert json.loads(records[1]["stdin"]) == {"session_id": SID}
    assert records[2]["stdin"] == "{not json"
    assert records[3]["stdin"] == ""


def test_unicode_and_large_payloads_reach_the_hook_intact(env: Env) -> None:
    big = "x" * (2 * 1024 * 1024)
    text = 'café ☃ \U0001f600 ‮ \'; $(rm -rf ~) `x` "q" \\'
    env.setup(
        [
            {"do": "post_tool_use", "tool_name": "Write", "tool_input": {"content": big}},
            {"do": "post_tool_use", "tool_name": "Write", "tool_input": {"content": text}},
        ]
    )
    assert env.run().returncode == 0
    first, second = env.hook_calls()
    assert first["payload"]["tool_input"]["content"] == big
    assert second["payload"]["tool_input"]["content"] == text


# --- hook output -----------------------------------------------------------------------------


def test_a_blocked_stop_runs_on_block_and_sets_stop_hook_active(env: Env) -> None:
    block = json.dumps({"decision": "block", "reason": "file a report"})
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout=block), [("Stop", None, 60)]
    )
    env.setup(
        [{"do": "stop", "on_block": [{"do": "stop"}, {"do": "touch", "path": "after"}]}],
        settings=settings,
    )
    assert env.run().returncode == 0
    first, second = (c["payload"] for c in env.hook_calls())
    assert (first["stop_hook_active"], second["stop_hook_active"]) == (False, True)
    assert (env.cwd / "after").exists()
    assert [b["reasons"] for b in env.records("block")] == [["file a report"]] * 2


def test_exit_2_blocks_with_stderr_as_reason(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], exit_code=2, stderr="no report\n"),
        [("Stop", None, 60)],
    )
    env.setup([{"do": "stop", "on_block": [{"do": "exit", "code": 7}]}], settings=settings)
    assert env.run().returncode == 7
    assert env.records("block")[0]["reasons"] == ["no report"]


@pytest.mark.parametrize(
    ("stdout", "exit_code"),
    [
        ('{"decision": "approve"}', 0),
        ("not json", 0),
        ('{"decision": "block", "reason": "x"}', 1),  # a non-blocking error: output ignored
        ("[1, 2]", 0),
    ],
)
def test_non_blocking_hook_output_does_not_block(env: Env, stdout: str, exit_code: int) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout=stdout, exit_code=exit_code),
        [("Stop", None, 60)],
    )
    env.setup(
        [{"do": "stop", "on_block": [{"do": "exit", "code": 7}]}, {"do": "stop"}], settings=settings
    )
    assert env.run().returncode == 0
    second = env.hook_calls()[1]["payload"]
    assert second["stop_hook_active"] is False


def test_session_start_context_is_recorded(env: Env) -> None:
    context = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "claude-session directory: /d",
        }
    }
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout=json.dumps(context)),
        [("SessionStart", None, 30)],
    )
    env.setup([{"do": "session_start"}], settings=settings)
    assert env.run().returncode == 0
    assert env.records("context")[0]["contexts"] == ["claude-session directory: /d"]


def test_plain_session_start_stdout_is_context(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout="hello\n"), [("SessionStart", None, 30)]
    )
    env.setup([{"do": "session_start"}], settings=settings)
    assert env.run().returncode == 0
    assert env.records("context")[0]["contexts"] == ["hello\n"]


@pytest.mark.parametrize("ignore_term", [False, True])
def test_a_hook_past_its_timeout_is_ended_and_its_output_dropped(
    env: Env, ignore_term: bool
) -> None:
    block = json.dumps({"decision": "block", "reason": "late"})
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout=block, sleep=30, ignore_term=ignore_term),
        [("Stop", None, 1)],
    )
    env.setup([{"do": "stop", "on_block": [{"do": "exit", "code": 7}]}], settings=settings)
    result = env.run(timeout=20)
    assert result.returncode == 0
    (hook,) = env.records("hook")
    assert hook["timed_out"] is True
    assert hook["exit"] == -signal.SIGTERM  # the group leader, ``sh``
    assert "stdout" not in hook
    assert env.records("block") == []
    # The recorder (which may ignore SIGTERM) got SIGKILL with its group, long before its
    # 30 s sleep ended (the run's timeout is 20 s).
    if sys.platform.startswith("linux"):
        assert not Path(f"/proc/{env.hook_calls()[0]['pid']}").exists()


# --- agent actions ---------------------------------------------------------------------------


def test_report_runs_the_bare_command_through_path(env: Env) -> None:
    calls = env.tmp / "report.jsonl"
    report_stub(env.bin, calls, stdout="report queued\n")
    env.setup(
        [
            {
                "do": "report",
                "args": ["done", "--summary", "it's done; $(nope)"],
                "agent_id": "a1",
                "agent_type": "worker",
            }
        ]
    )
    result = env.run(PATH=f"{env.bin}:{os.environ['PATH']}")
    assert result.returncode == 0, result.stderr
    (call,) = read_jsonl(calls)
    assert call["args"] == ["report", "done", "--summary", "it's done; $(nope)"]
    assert call["cwd"] == str(env.cwd)
    assert call["env"]["CSESSION_ID"] == "cs-abcd2345"
    (post,) = env.hook_calls()
    command = post["payload"]["tool_input"]["command"]
    assert command.startswith("claude-session report done --summary ")
    assert post["payload"]["tool_name"] == "Bash"
    assert post["payload"]["tool_response"] == {"stdout": "report queued\n", "stderr": ""}
    assert (post["payload"]["agent_id"], post["payload"]["agent_type"]) == ("a1", "worker")


def test_report_with_a_heredoc_summary(env: Env) -> None:
    calls = env.tmp / "report.jsonl"
    report_stub(env.bin, calls)
    text = "line one\n  'quoted' $HOME `x`\nEOFX"
    env.setup([{"do": "report", "args": ["done", "--summary-file", "-"], "heredoc": text}])
    assert env.run(PATH=f"{env.bin}:{os.environ['PATH']}").returncode == 0
    (call,) = read_jsonl(calls)
    assert call["stdin"] == text + "\n"
    command = env.hook_calls()[0]["payload"]["tool_input"]["command"]
    assert command.endswith("<<'EOF'\n" + text + "\nEOF")


def test_report_command_is_configurable(env: Env) -> None:
    calls = env.tmp / "report.jsonl"
    stub = report_stub(env.bin, calls)
    env.setup(
        [{"do": "report", "args": ["blocked", "--reason-file", "-"], "heredoc": "why"}],
        report_command=f"{stub} report",
    )
    assert env.run().returncode == 0
    assert read_jsonl(calls)[0]["args"] == ["report", "blocked", "--reason-file", "-"]


@pytest.mark.parametrize(("mode", "fires"), [("on_success", False), ("always", True)])
def test_a_failed_command_fires_post_tool_use_only_when_asked(
    env: Env, mode: str, fires: bool
) -> None:
    calls = env.tmp / "report.jsonl"
    report_stub(env.bin, calls, exit_code=2, stderr="summary required\n")
    env.setup([{"do": "report", "args": ["done"], "post_tool_use": mode}])
    assert env.run(PATH=f"{env.bin}:{os.environ['PATH']}").returncode == 0
    assert len(env.hook_calls()) == (1 if fires else 0)
    (bash,) = env.records("bash")
    assert (bash["exit"], bash["stderr"]) == (2, "summary required\n")


def test_a_rejected_report_is_filed_again_on_block(env: Env) -> None:
    calls = env.tmp / "report.jsonl"
    report_stub(env.bin, calls)
    reject = json.dumps({"decision": "block", "reason": "claude-session report rejected"})
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout=reject), [("PostToolUse", None, 60)]
    )
    env.setup(
        [
            {
                "do": "report",
                "args": ["done", "--summary", "a"],
                "on_block": [
                    {"do": "report", "args": ["done", "--summary", "b"], "post_tool_use": "never"}
                ],
            }
        ],
        settings=settings,
    )
    assert env.run(PATH=f"{env.bin}:{os.environ['PATH']}").returncode == 0
    assert [c["args"][-1] for c in read_jsonl(calls)] == ["a", "b"]


def test_bash_and_write_file(env: Env) -> None:
    env.setup(
        [
            {"do": "write_file", "path": "summary.md", "content": "sé\n"},
            {"do": "bash", "command": "cat summary.md; echo err >&2"},
        ]
    )
    assert env.run().returncode == 0
    (post,) = env.hook_calls()
    assert post["payload"]["tool_response"] == {"stdout": "sé\n", "stderr": "err\n"}


# --- stream-json and exits -----------------------------------------------------------------


def test_several_result_objects_and_raw_lines(env: Env) -> None:
    env.setup(
        [
            {"do": "emit", "line": {"type": "system", "subtype": "init"}},
            {"do": "result", "result": "turn 1", "total_cost_usd": 0.25, "num_turns": 2},
            {"do": "emit", "line": "not json at all"},
            {
                "do": "result",
                "result": "turn 2",
                "subtype": "success",
                "is_error": True,
                "total_cost_usd": 0.5,
                "num_turns": 1,
                "duration_ms": 10,
                "terminal_reason": "completed",
                "permission_denials": [],
            },
        ]
    )
    result = env.run(*HEADLESS, "--session-id", SID)
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[2] == "not json at all"
    first, last = json.loads(lines[1]), json.loads(lines[3])
    assert first == {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "turn 1",
        "session_id": SID,
        "total_cost_usd": 0.25,
        "num_turns": 2,
    }
    assert (last["is_error"], last["terminal_reason"], last["session_id"]) == (
        True,
        "completed",
        SID,
    )


def test_a_truncated_tail(env: Env) -> None:
    env.setup([{"do": "result", "result": "one"}, {"do": "truncated_result", "chars": 20}])
    result = env.run(*HEADLESS)
    first, tail = result.stdout.split("\n")
    assert json.loads(first)["result"] == "one"
    assert len(tail) == 20 and tail.startswith('{"type": "result"')
    with pytest.raises(json.JSONDecodeError):
        json.loads(tail)


def test_truncation_must_cut_the_object(env: Env) -> None:
    env.setup([{"do": "truncated_result", "chars": 100000}])
    assert env.run(*HEADLESS).returncode == FAKE_ERROR


def test_exit_code_stderr_and_later_steps_skipped(env: Env) -> None:
    env.setup(
        [
            {"do": "stderr", "text": "boom\n"},
            {"do": "exit", "code": 3},
            {"do": "touch", "path": "never"},
        ]
    )
    result = env.run()
    assert (result.returncode, result.stderr) == (3, "boom\n")
    assert not (env.cwd / "never").exists()


# --- signals -------------------------------------------------------------------------------


def wait_step(env: Env, kind: str) -> None:
    wait_for(env.log, lambda r: r["event"] == "step" and r["step"]["do"] == kind)


def test_sigterm_fires_session_end_other_and_exits_143(env: Env) -> None:
    env.setup([{"do": "session_start"}, {"do": "hang"}])
    child = env.spawn(*HEADLESS)
    wait_step(env, "hang")
    child.send_signal(signal.SIGTERM)
    assert child.wait(15) == 143
    assert [c["event"] for c in env.hook_calls()] == ["SessionStart", "SessionEnd"]
    assert env.hook_calls()[1]["payload"]["reason"] == "other"
    assert env.records("exit")[0]["code"] == 143


def test_sighup_exits_129_without_session_end(env: Env) -> None:
    env.setup([{"do": "hang"}])
    child = env.spawn()
    wait_step(env, "hang")
    child.send_signal(signal.SIGHUP)
    assert child.wait(15) == 129
    assert env.hook_calls() == []


def test_ignoring_sigterm(env: Env) -> None:
    env.setup([{"do": "ignore_signals", "signals": ["TERM"]}, {"do": "hang"}])
    child = env.spawn(*HEADLESS)
    wait_step(env, "hang")
    child.send_signal(signal.SIGTERM)
    wait_for(env.log, lambda r: r["event"] == "signal_ignored")
    assert child.wait(0.5) is None
    child.send_signal(signal.SIGKILL)
    assert child.wait(15) == -signal.SIGKILL
    assert env.hook_calls() == []


def test_signal_actions_from_the_scenario(env: Env) -> None:
    env.setup(
        [{"do": "hang"}],
        signals={"TERM": {"exit": 0, "session_end": "logout"}, "INT": "ignore"},
    )
    child = env.spawn()
    wait_step(env, "hang")
    child.send_signal(signal.SIGINT)
    wait_for(env.log, lambda r: r["event"] == "signal_ignored" and r["signal"] == "INT")
    child.send_signal(signal.SIGTERM)
    assert child.wait(15) == 0
    assert env.hook_calls()[0]["payload"]["reason"] == "logout"


def test_sigterm_during_a_hook_ends_the_hook_first(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], sleep=60 if e == "Stop" else 0),
        [("Stop", None, 60), ("SessionEnd", None, 30)],
    )
    env.setup([{"do": "stop"}], settings=settings)
    child = env.spawn()
    hook = wait_for(env.hooks, lambda r: r["args"] == ["Stop"])
    child.send_signal(signal.SIGTERM)
    assert child.wait(15) == 143
    assert [c["event"] for c in env.hook_calls()] == ["Stop", "SessionEnd"]
    if sys.platform.startswith("linux"):
        assert not Path(f"/proc/{hook['pid']}").exists()


def test_sleep_then_exit(env: Env) -> None:
    env.setup([{"do": "sleep", "seconds": 0.1}, {"do": "exit", "code": 0}])
    assert env.run().returncode == 0


# --- claude_bin ----------------------------------------------------------------------------


def test_claude_bin_wrapper_sets_its_own_scenario(env: Env) -> None:
    env.setup([{"do": "stop"}, {"do": "result", "result": "ok"}])
    path = claude_bin(env.tmp / "cbin", env.scenario, env.log)
    gw = SystemGateway()
    assert gw.run([str(path), "--version"], env={"PATH": "/usr/bin:/bin"}, timeout=30).stdout == (
        "2.1.312 (Claude Code)\n"
    )
    result = gw.run(
        [str(path), "--settings", str(env.settings), *HEADLESS, "go"],
        env={"PATH": "/usr/bin:/bin", "HOME": str(env.tmp)},  # a scrubbed environment
        cwd=str(env.cwd),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert stream(result.stdout)[0]["result"] == "ok"
    assert [c["event"] for c in env.hook_calls()] == ["Stop"]


def test_claude_bin_wrapper_keeps_the_pid(env: Env) -> None:
    env.setup([{"do": "hang"}])
    path = claude_bin(env.tmp / "cbin", env.scenario, env.log)
    child = FakeGateway(spawn_via=SystemGateway()).spawn([str(path)], cwd=str(env.cwd))
    start = wait_for(env.log, lambda r: r["event"] == "start")
    assert start["pid"] == child.pid
    child.send_signal(signal.SIGTERM)
    assert child.wait(15) == 143


def test_injected_hooks_cover_the_documented_events() -> None:
    events = {e for e, _, _ in INJECTED_HOOKS}
    assert events == {
        "SessionStart",
        "UserPromptSubmit",
        "PermissionRequest",
        "Notification",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }
    timeouts = {e: t for e, _, t in INJECTED_HOOKS}
    assert (timeouts["PostToolUse"], timeouts["Stop"], timeouts["SessionStart"]) == (60, 60, 30)
