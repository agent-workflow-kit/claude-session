"""The scenario-driven fake ``claude`` (tests/fakebin). Hook commands here are recorder stubs,
never ``claude-session hook-event``; ``report`` runs against a stand-in ``claude-session``.
Every test that signals the fake signals only the exact child it started; processes the fake
may leak are recorded by exact PID (``leaked``) and killed by that PID at teardown."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from claude_session.gateway import DEVNULL, Child, Result, SystemGateway

from .fake_gateway import FakeGateway
from .fakeclaude import (
    FAKE_CLAUDE,
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
LINUX = sys.platform.startswith("linux")
linux_only = pytest.mark.skipif(not LINUX, reason="uses /proc and PR_SET_PDEATHSIG")


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

    def run(
        self, *args: str, timeout: float = 30, stdin: str | None = None, **extra: str
    ) -> Result:
        return SystemGateway().run(
            self.argv(*args),
            extra_env=self.env(**extra),
            cwd=str(self.cwd),
            timeout=timeout,
            stdin=stdin,
        )

    def spawn(self, *args: str) -> Child:
        gw = FakeGateway(spawn_via=SystemGateway())
        child = gw.spawn(self.argv(*args), extra_env=self.env(), cwd=str(self.cwd), stdin=DEVNULL)
        assert gw.calls[0].kind == "spawn"
        return child

    def hook_calls(self) -> list[dict[str, Any]]:
        """``(event, payload)`` of every hook the fake ran, in start order."""
        return [
            {"event": r["args"][0], "payload": json.loads(r["stdin"]), **r}
            for r in read_jsonl(self.hooks)
        ]

    def records(self, event: str) -> list[dict[str, Any]]:
        return [r for r in read_jsonl(self.log) if r["event"] == event]

    def common(self, event: str, sid: str = SID, **extra: Any) -> dict[str, Any]:
        """The fields every hook payload carries by default."""
        project = re.sub(r"[^A-Za-z0-9]", "-", str(self.cwd))
        transcript = str(Path.home() / ".claude" / "projects" / project / f"{sid}.jsonl")
        return {
            "session_id": sid,
            "transcript_path": transcript,
            "cwd": str(self.cwd),
            "permission_mode": "default",
            "hook_event_name": event,
            **extra,
        }


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _dead(pid: int) -> bool:
    """Whether ``pid`` is gone or a zombie (a zombie we do not own is as good as dead)."""
    if LINUX:
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                return fh.read().rpartition(b")")[2].split()[0] == b"Z"
        except FileNotFoundError:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def gone(pid: int, timeout: float = 10.0) -> bool:
    """Wait up to ``timeout`` seconds for ``pid`` to die."""
    deadline = time.monotonic() + timeout
    while not _dead(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


@pytest.fixture
def leaked() -> Iterator[list[int]]:
    """PIDs of processes the fake may leave behind; any still alive at teardown gets SIGKILL
    by that exact PID."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        if not _dead(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)


def read_pid(path: Path, timeout: float = 15.0) -> int:
    """A PID a step or hook wrote (``echo $$ > path``), once the line is complete."""
    deadline = time.monotonic() + timeout
    while True:
        with contextlib.suppress(FileNotFoundError):
            text = path.read_text(encoding="utf-8")
            if text.endswith("\n"):
                return int(text)
        if time.monotonic() > deadline:
            raise AssertionError(f"no PID in {path} after {timeout} s")
        time.sleep(0.02)


def stream(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


def exec_recorder(log: Path, **kwargs: Any) -> str:
    """A recorder that is the hook's own process (``exec``), whatever ``/bin/sh`` is."""
    return "exec " + recorder(log, **kwargs)


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
    assert start["flags"]["--debug"] is True
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
        (["-p", "--output-format", "stream-json", "x"], "requires --verbose"),
        (["--output-format", "json"], "only works with --print"),
        (["-p", "--output-format", "yaml", "--verbose", "x"], "invalid --output-format"),
        (["--session-id", "not-a-uuid"], "valid UUID"),
        (["--resume", "'; rm -rf /'"], "valid UUID"),
        (["-p", "--max-turns", "0", "x"], "invalid --max-turns"),
        (["--verbose=yes"], "does not take an argument"),
        (["--add-dir"], "argument missing"),
        (["-p"], "Input must be provided"),
        (["--", "a", "b"], "too many arguments"),
    ],
)
def test_invalid_argv_is_rejected_like_claude(env: Env, args: list[str], message: str) -> None:
    env.setup([{"do": "touch", "path": str(env.tmp / "ran")}])
    result = env.run(*args)
    assert result.returncode == 1
    assert message in result.stderr
    assert not (env.tmp / "ran").exists()


def test_print_mode_reads_the_prompt_from_stdin(env: Env) -> None:
    env.setup([])
    result = env.run("-p", stdin="from stdin")
    assert result.returncode == 0, result.stderr
    assert env.records("start")[0]["prompt"] == "from stdin"


def _spawn_with_open_stdin(env: Env, leaked: list[int]) -> subprocess.Popen[bytes]:
    """The fake with ``-p``, no prompt and a stdin pipe that stays open and is never written."""
    proc = subprocess.Popen(
        env.argv("-p"),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, **env.env()},
        cwd=str(env.cwd),
    )
    leaked.append(proc.pid)
    return proc


def test_print_mode_gives_up_on_a_silent_stdin(env: Env, leaked: list[int]) -> None:
    env.setup([{"do": "touch", "path": str(env.tmp / "ran")}])
    proc = _spawn_with_open_stdin(env, leaked)
    assert proc.stdin is not None and proc.stderr is not None
    try:
        assert proc.wait(10) == 1  # the fake waits 3 s, like claude; stdin stays open
    finally:
        proc.stdin.close()
    assert b"Input must be provided" in proc.stderr.read()
    proc.stderr.close()
    assert not (env.tmp / "ran").exists()


def test_a_signal_while_waiting_for_stdin_ends_the_fake(env: Env, leaked: list[int]) -> None:
    env.setup([])
    proc = _spawn_with_open_stdin(env, leaked)
    assert proc.stdin is not None and proc.stderr is not None
    time.sleep(0.5)  # most likely inside the wait by now; earlier, it dies all the same
    start = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        assert proc.wait(10) == -signal.SIGTERM
    finally:
        proc.stdin.close()
        proc.stderr.close()
    assert time.monotonic() - start < 2  # not the 3 s give-up


def test_option_equals_value_and_end_of_options(env: Env) -> None:
    env.setup([])
    result = env.run("--model=m", "--add-dir=/a", "--debug=api", "--", "--looks-like-an-option")
    assert result.returncode == 0, result.stderr
    (start,) = env.records("start")
    assert start["flags"]["--model"] == "m"
    assert start["flags"]["--add-dir"] == ["/a"]
    assert start["flags"]["--debug"] == "api"
    assert start["prompt"] == "--looks-like-an-option"


def test_add_dir_takes_values_until_the_next_option(env: Env) -> None:
    env.setup([])
    assert env.run("--add-dir", "/a", "/b", "--model", "m", "--add-dir", "/c").returncode == 0
    (start,) = env.records("start")
    assert start["flags"]["--add-dir"] == ["/a", "/b", "/c"]
    assert start["prompt"] is None


@pytest.mark.parametrize(
    ("args", "debug", "prompt"),
    [
        (["--debug", "--verbose"], True, None),
        (["-d", "api"], "api", None),
        # Like the real claude, an optional-value --debug eats a following bootstrap prompt
        # (and so does --add-dir): the ordering hazard is visible here.
        (["--debug", "/plugin:skill go"], "/plugin:skill go", None),
        (["--add-dir", "/a", "/plugin:skill go"], True, None),
    ],
)
def test_optional_and_variadic_values_eat_a_following_prompt(
    env: Env, args: list[str], debug: object, prompt: str | None
) -> None:
    env.setup([])
    assert env.run(*args).returncode == 0
    (start,) = env.records("start")
    assert start["prompt"] == prompt
    if "--add-dir" in args:
        assert start["flags"]["--add-dir"] == ["/a", "/plugin:skill go"]
    else:
        assert start["flags"]["--debug"] == debug


def test_extra_flags_extend_the_accepted_argv(env: Env) -> None:
    env.setup([], extra_flags={"--permission-mode": "value", "--foo": "bool"})
    result = env.run("--permission-mode", "plan", "--foo", "go")
    assert result.returncode == 0, result.stderr
    (start,) = env.records("start")
    assert (start["flags"]["--permission-mode"], start["flags"]["--foo"]) == ("plan", True)
    assert start["prompt"] == "go"
    env.setup([])
    assert env.run("--permission-mode", "plan").returncode == 1


def issue12_argv(
    env: Env,
    *,
    headless: bool = False,
    claude_args: tuple[str, ...] = (),
    resume_from: str | None = None,
    fork: bool = False,
    session_id: str | None = None,
    prompt: str | None = None,
) -> list[str]:
    """The argv claude-session builds (#12, ``build_argv``; design.md, Generated settings:
    argv) after the ``--settings`` ``Env.argv`` adds: ``... [claude_args] [--resume <id>
    [--fork-session]] [--session-id <uuid>] [-- <bootstrap prompt>]``."""
    args = ["--setting-sources", "user", "--strict-mcp-config", "--model", "m"]
    args += ["--add-dir", str(env.tmp), "--plugin-dir", "/p", "--add-dir", "/p"]
    args += ["--append-system-prompt-file", "/i.md", "--system-prompt-snapshot", "off"]
    if headless:
        args += [*HEADLESS, "--max-turns", "6"]
    args += claude_args
    if resume_from is not None:
        args += ["--resume", resume_from]
        if fork:
            args.append("--fork-session")
    if session_id is not None:
        args += ["--session-id", session_id]
    if prompt is not None:
        args += ["--", prompt]
    return args


# Variadic and optional-value options from claude_args, right before the conversation flags.
GREEDY_ARGS = ("--allowedTools", "Bash(git *)", "Edit", "--debug")


@pytest.mark.parametrize(
    ("kwargs", "session_id", "source"),
    [
        ({"session_id": SID, "prompt": "/plugin:skill go"}, SID, "startup"),
        ({"headless": True, "session_id": SID, "prompt": "Do the task."}, SID, "startup"),
        ({"resume_from": SID, "prompt": "--looks-like-an-option"}, SID, "resume"),
        ({"headless": True, "resume_from": SID, "prompt": "Continue."}, SID, "resume"),
        ({"resume_from": SID}, SID, "resume"),
        (
            {"resume_from": SID, "fork": True, "session_id": OTHER_SID, "prompt": "go\non"},
            OTHER_SID,
            "fork",
        ),
        (
            {
                "headless": True,
                "resume_from": SID,
                "fork": True,
                "session_id": OTHER_SID,
                "prompt": "Continue.",
            },
            OTHER_SID,
            "fork",
        ),
    ],
)
def test_the_issue_12_argv_shapes_keep_the_prompt_after_end_of_options(
    env: Env, kwargs: dict[str, Any], session_id: str, source: str
) -> None:
    """Every argv shape #12 builds (interactive/headless, fresh/resume/fork, with and without
    a prompt) parses with greedy ``claude_args`` before it; the ``--`` is live-verified to
    protect the prompt (#12 review, Claude Code 2.1.314)."""
    env.setup([{"do": "session_start"}], extra_flags={"--allowedTools": "variadic"})
    result = env.run(*issue12_argv(env, claude_args=GREEDY_ARGS, **kwargs))
    assert result.returncode == 0, result.stderr
    (start,) = env.records("start")
    assert start["prompt"] == kwargs.get("prompt")
    assert start["headless"] is kwargs.get("headless", False)
    assert start["session_id"] == session_id
    assert start["flags"]["--allowedTools"] == ["Bash(git *)", "Edit"]
    assert start["flags"]["--debug"] is True
    assert start["flags"]["--add-dir"] == [str(env.tmp), "/p"]
    assert start["flags"].get("--resume") == kwargs.get("resume_from")
    assert env.hook_calls()[0]["payload"]["source"] == source


@pytest.mark.parametrize(
    ("claude_args", "flag", "value"),
    [
        # Live (#12 review, Claude Code 2.1.314): both swallow a following prompt.
        (["--debug"], "--debug", "go"),
        (["--allowedTools", "Edit"], "--allowedTools", ["Edit", "go"]),
        # From `claude --help` (2.1.314): the same grammar, not checked live.
        (["--allowed-tools", "Edit"], "--allowedTools", ["Edit", "go"]),
        (["-d"], "--debug", "go"),
        (["--add-dir", "/a"], "--add-dir", ["/a", "go"]),
        (["--mcp-config", "a.json"], "--mcp-config", ["a.json", "go"]),
        (["--betas", "b1"], "--betas", ["b1", "go"]),
        (["--worktree"], "--worktree", "go"),
    ],
)
def test_a_greedy_claude_arg_swallows_a_prompt_without_end_of_options(
    env: Env, claude_args: list[str], flag: str, value: object
) -> None:
    """A variadic or optional-value option right before the prompt takes it as a value; ``--``
    keeps it a prompt. Verified live (#12 review, Claude Code 2.1.314): ``--debug`` and
    ``--allowedTools X``. From ``claude --help`` 2.1.314 only: ``--allowed-tools``, ``-d``,
    ``--add-dir``, ``--mcp-config``, ``--betas``, ``--worktree``."""
    extra = {"--allowedTools": "variadic", "--mcp-config": "variadic", "--betas": "variadic"}
    env.setup([], extra_flags={**extra, "--worktree": "optional"})
    assert env.run(*claude_args, "go").returncode == 0
    (start,) = env.records("start")
    assert start["prompt"] is None
    assert start["flags"][flag] == value
    # With "--" (as #12 now builds it) the prompt is kept.
    env.log.unlink()
    assert env.run(*claude_args, "--", "go").returncode == 0
    (start,) = env.records("start")
    assert start["prompt"] == "go"
    assert start["flags"][flag] == (value[:-1] if isinstance(value, list) else True)


def test_a_swallowed_headless_prompt_leaves_print_mode_without_input(env: Env) -> None:
    env.setup(
        [{"do": "touch", "path": str(env.tmp / "ran")}], extra_flags={"--allowedTools": "variadic"}
    )
    result = env.run(*HEADLESS, "--allowedTools", "Edit", "Do the task.")
    assert result.returncode == 1
    assert "Input must be provided" in result.stderr
    assert not (env.tmp / "ran").exists()


@pytest.mark.parametrize(
    ("args", "flags", "prompt"),
    [
        # commander: a required value is taken even when it starts with "-".
        (["--add-dir", "--verbose"], {"--add-dir": ["--verbose"]}, None),
        (["--add-dir", "--", "x"], {"--add-dir": ["--", "x"]}, None),
        # A lone "-" is a value, not an option; "--" ends a variadic list.
        (["--add-dir", "/a", "-", "--", "x"], {"--add-dir": ["/a", "-"]}, "x"),
        (["--debug", "-"], {"--debug": "-"}, None),
        (["--debug", "--", "x"], {"--debug": True}, "x"),
        # "--opt=value" ends a variadic list at once.
        (["--add-dir=/a", "x"], {"--add-dir": ["/a"]}, "x"),
        (["-r", SID, "--", "x"], {"--resume": SID}, "x"),
        (["--resume=" + SID], {"--resume": SID}, None),
        (["--allowed-tools", "A", "--allowedTools", "B"], {"--allowedTools": ["A", "B"]}, None),
    ],
)
def test_commander_value_grammar(
    env: Env, args: list[str], flags: dict[str, Any], prompt: str | None
) -> None:
    """Edge cases of commander.js's documented parsing (not checked live on claude): a
    required value may start with "-", a lone "-" is a value, "--" ends a variadic list and
    ``--opt=value`` gives one value; option kinds from ``claude --help`` 2.1.314."""
    env.setup([], extra_flags={"--allowed-tools": "variadic"})
    result = env.run(*args)
    assert result.returncode == 0, result.stderr
    (start,) = env.records("start")
    assert {k: start["flags"][k] for k in flags} == flags
    assert start["prompt"] == prompt


@pytest.mark.parametrize(
    ("extra", "args", "flags", "prompt"),
    [
        ({"--foo": "optional"}, ["--foo", "x"], {"--foo": "x"}, None),
        ({"--foo": "optional"}, ["--foo", "--", "x"], {"--foo": True}, "x"),
        ({"--foo": "variadic"}, ["--foo", "a", "b", "--", "x"], {"--foo": ["a", "b"]}, "x"),
        ({"--foo": "value"}, ["--foo", "-a", "x"], {"--foo": "-a"}, "x"),
        ({"--foo": "bool"}, ["--foo", "x"], {"--foo": True}, "x"),
    ],
)
def test_extra_flags_declare_any_kind(
    env: Env, extra: dict[str, str], args: list[str], flags: dict[str, Any], prompt: str | None
) -> None:
    env.setup([], extra_flags=extra)
    result = env.run(*args)
    assert result.returncode == 0, result.stderr
    (start,) = env.records("start")
    assert {k: start["flags"][k] for k in flags} == flags
    assert start["prompt"] == prompt


@pytest.mark.parametrize(
    "extra",
    [
        {"--allowedTools": "value"},
        {"--allowed-tools": "bool"},
        {"--debug": "value"},
        {"--permission-mode": "optional"},
        {"-r": "value"},
    ],
)
def test_extra_flags_must_match_claudes_grammar(env: Env, extra: dict[str, str]) -> None:
    """A scenario cannot declare a real option with another kind than ``claude --help``
    2.1.314 gives it, so a test cannot pass on the fake and fail on claude."""
    write_json(env.scenario, {"steps": [], "extra_flags": extra})
    write_json(env.settings, {})
    result = env.run("-p", "x")
    assert result.returncode == FAKE_ERROR
    assert "Claude Code's grammar" in result.stderr


def test_resume_without_an_id_is_not_modelled(env: Env) -> None:
    env.setup([{"do": "touch", "path": str(env.tmp / "ran")}])
    result = env.run("--resume", "--", "x")
    assert result.returncode == FAKE_ERROR
    assert "--resume" in result.stderr
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
        ({"steps": [], "signals": {"HUP": {"die": True, "exit": 1}}}, "signals.HUP"),
        ({"steps": "all"}, "must be a list"),
        ({"steps": [{"do": "truncated_result", "chars": 0}]}, "chars"),
        ({"steps": [{"do": "truncated_result", "bytes": 0}]}, "bytes"),
        ({"steps": [{"do": "truncated_result", "chars": 3, "bytes": 3}]}, "exactly one"),
        ({"steps": [{"do": "parallel", "steps": [{"do": "stop"}]}]}, "parallel"),
        ({"steps": [], "extra_flags": {"--x": "maybe"}}, "extra_flags"),
        ({"steps": [], "extra_flags": {"x": "bool"}}, "extra_flags"),
    ],
)
def test_a_broken_scenario_fails_loudly(env: Env, scenario: dict[str, Any], message: str) -> None:
    write_json(env.scenario, scenario)
    write_json(env.settings, {})
    result = env.run("-p", "x")
    assert result.returncode == FAKE_ERROR
    assert message in result.stderr


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_numbers_are_refused(env: Env, number: str) -> None:
    text = '{"steps": [{"do": "sleep", "seconds": NUMBER}]}'.replace("NUMBER", number)
    env.scenario.write_text(text, encoding="utf-8")
    write_json(env.settings, {})
    result = env.run("-p", "x")
    assert result.returncode == FAKE_ERROR
    assert "number" in result.stderr


def test_an_internal_error_exits_fake_error_not_1(env: Env) -> None:
    env.setup([{"do": "write_file", "path": "missing/dir/file.md", "content": "x"}])
    result = env.run()
    assert result.returncode == FAKE_ERROR
    assert "Traceback" not in result.stderr
    assert env.records("error")
    assert env.records("exit")[0]["code"] == FAKE_ERROR


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
            {"do": "user_prompt_submit", "prompt": "hi"},
            {"do": "permission_request", "tool_name": "Bash", "tool_input": {"command": "ls"}},
            {"do": "notification", "notification_type": "permission_prompt", "message": "m"},
            {
                "do": "post_tool_use",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "tool_response": {"stdout": "a\n", "stderr": ""},
                "tool_use_id": "toolu_1",
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
    assert not Path(payloads[0]["transcript_path"]).exists()
    for n in (5, 7):  # the default notification messages
        assert isinstance(payloads[n].pop("message"), str) and payloads[n] != {}
    c = env.common
    assert payloads == [
        c("SessionStart", source="startup"),
        c("UserPromptSubmit", prompt="hi"),
        c("PermissionRequest", tool_name="Bash", tool_input={"command": "ls"}),
        c("Notification", notification_type="permission_prompt", message="m"),
        c(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "ls"},
            tool_response={"stdout": "a\n", "stderr": ""},
            tool_use_id="toolu_1",
        ),
        c("Notification", notification_type="elicitation_dialog"),
        c("Stop", stop_hook_active=False, background_tasks=[]),
        c("Notification", notification_type="idle_prompt"),
        c("SessionEnd", reason="prompt_input_exit"),
    ]


def test_permission_mode_and_prompt_defaults(env: Env) -> None:
    env.setup([{"do": "user_prompt_submit"}], permission_mode="plan")
    assert env.run("-p", "the prompt").returncode == 0
    (call,) = env.hook_calls()
    assert (call["payload"]["permission_mode"], call["payload"]["prompt"]) == ("plan", "the prompt")


def test_hooks_run_in_the_cwd_with_the_claude_environment(env: Env) -> None:
    env.setup([{"do": "stop"}])
    assert env.run("-p", "x", CSESSION_DIR="/s/cs-abcd2345").returncode == 0
    (call,) = env.hook_calls()
    assert call["cwd"] == str(env.cwd)
    assert call["env"]["CSESSION_ID"] == "cs-abcd2345"
    assert call["env"]["CSESSION_DIR"] == "/s/cs-abcd2345"
    # Each hook runs in a process group of its own, not the fake's (nor ours).
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
    assert p[1] == env.common("SessionEnd", reason="clear")
    new = p[2]["session_id"]
    assert new != SID and uuid.UUID(new)
    assert p[2]["transcript_path"].endswith(f"/{new}.jsonl")
    assert (p[3]["session_id"], p[3]["source"], p[4]["session_id"]) == (new, "compact", new)
    assert p[4]["transcript_path"] == p[2]["transcript_path"]


def test_a_session_start_with_a_session_id_recomputes_the_transcript_path(env: Env) -> None:
    env.setup([{"do": "session_start", "session_id": OTHER_SID}, {"do": "stop"}])
    assert env.run("--session-id", SID).returncode == 0
    start, stop = (c["payload"] for c in env.hook_calls())
    assert start["transcript_path"] == env.common("x", OTHER_SID)["transcript_path"]
    assert (stop["session_id"], stop["transcript_path"]) == (OTHER_SID, start["transcript_path"])


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
    assert post["tool_use_id"].startswith("toolu_")


def test_post_tool_use_inside_a_subagent_keeps_the_parent_session_id(env: Env) -> None:
    env.setup(
        [
            {
                "do": "post_tool_use",
                "tool_name": "Read",
                "agent_id": "a1",
                "agent_type": "explorer",
                "tool_use_id": "toolu_2",
            }
        ]
    )
    assert env.run("--session-id", SID).returncode == 0
    (call,) = env.hook_calls()
    assert call["payload"] == env.common(
        "PostToolUse",
        tool_name="Read",
        tool_input={},
        tool_response={},
        tool_use_id="toolu_2",
        agent_id="a1",
        agent_type="explorer",
    )


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
        ("Write", "WriteFile", False),  # identifier characters only: exact match
        ("rite", "Write", False),
        ("Edit", "Write", False),
        ("mcp__.*", "mcp__srv__tool", True),  # anything else: an unanchored regex search
        ("Wri.e", "Write", True),
        ("rit.", "Write", True),
        ("^Edit", "Write", False),
    ],
)
def test_tool_matchers(env: Env, matcher: str | None, tool: str, fires: bool) -> None:
    settings = hook_settings(lambda e: recorder(env.hooks, args=[e]), [("PostToolUse", matcher, 5)])
    env.setup([{"do": "post_tool_use", "tool_name": tool}], settings=settings)
    assert env.run().returncode == 0
    assert len(env.hook_calls()) == (1 if fires else 0)


@pytest.mark.parametrize("event", ["Stop", "UserPromptSubmit"])
def test_events_without_matchers_ignore_the_matcher(env: Env, event: str) -> None:
    settings = hook_settings(lambda e: recorder(env.hooks, args=[e]), [(event, "Bash", 5)])
    step = "stop" if event == "Stop" else "user_prompt_submit"
    env.setup([{"do": step}], settings=settings)
    assert env.run().returncode == 0
    assert [c["event"] for c in env.hook_calls()] == [event]


@pytest.mark.parametrize(
    ("event", "matcher", "step", "fires"),
    [
        ("PermissionRequest", "Bash", {"do": "permission_request", "tool_name": "Bash"}, True),
        ("PermissionRequest", "Bash", {"do": "permission_request", "tool_name": "Read"}, False),
        ("SessionEnd", "clear", {"do": "session_end", "reason": "clear"}, True),
        ("SessionEnd", "clear", {"do": "session_end", "reason": "other"}, False),
        ("SessionStart", "resume", {"do": "session_start", "source": "resume"}, True),
        ("SessionStart", "resume", {"do": "session_start", "source": "startup"}, False),
    ],
)
def test_matcher_fields_of_other_events(
    env: Env, event: str, matcher: str, step: dict[str, Any], fires: bool
) -> None:
    settings = hook_settings(lambda e: recorder(env.hooks, args=[e]), [(event, matcher, 5)])
    env.setup([step], settings=settings)
    assert env.run().returncode == 0
    assert len(env.hook_calls()) == (1 if fires else 0)


def test_hooks_of_one_event_run_concurrently(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], sleep=2),
        [("Stop", None, 30)],
    )
    second = recorder(env.hooks, args=["Stop", "second"], sleep=2)
    settings["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": second}]})
    env.setup([{"do": "stop"}], settings=settings)
    assert env.run().returncode == 0
    assert len(env.hook_calls()) == 2
    (start,), (end,) = env.records("start"), env.records("exit")
    assert end["ts"] - start["ts"] < 3.5  # in sequence: 4 s at least


def test_identical_hook_commands_run_once(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e]), [("Stop", None, 5), ("Stop", None, 5)]
    )
    env.setup([{"do": "stop"}], settings=settings)
    assert env.run().returncode == 0
    assert [c["event"] for c in env.hook_calls()] == ["Stop"]


def test_parallel_tool_calls_fire_their_hooks_concurrently(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], sleep=2), [("PostToolUse", None, 30)]
    )
    env.setup(
        [
            {
                "do": "parallel",
                "steps": [
                    {"do": "post_tool_use", "tool_name": "Read", "tool_use_id": "toolu_a"},
                    {"do": "post_tool_use", "tool_name": "Grep", "tool_use_id": "toolu_b"},
                ],
            }
        ],
        settings=settings,
    )
    assert env.run().returncode == 0
    ids = {c["payload"]["tool_use_id"] for c in env.hook_calls()}
    assert ids == {"toolu_a", "toolu_b"}  # the same command, run once per tool call
    (start,), (end,) = env.records("start"), env.records("exit")
    assert end["ts"] - start["ts"] < 3.5


def test_a_blocked_parallel_call_runs_on_block(env: Env) -> None:
    block = json.dumps({"decision": "block", "reason": "no"})
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout=block), [("PostToolUse", "Grep", 30)]
    )
    env.setup(
        [
            {
                "do": "parallel",
                "steps": [
                    {"do": "post_tool_use", "tool_name": "Read"},
                    {"do": "post_tool_use", "tool_name": "Grep"},
                ],
                "on_block": [{"do": "exit", "code": 5}],
            }
        ],
        settings=settings,
    )
    assert env.run().returncode == 5


# --- hostile payloads ----------------------------------------------------------------------


def test_payload_overrides_omissions_and_raw_payloads(env: Env) -> None:
    env.setup(
        [
            {"do": "session_start", "payload": {"source": "weird", "extra": [1]}},
            {"do": "stop", "omit": ["background_tasks", "stop_hook_active"]},
            {"do": "user_prompt_submit", "raw_payload": "{not json"},
            {"do": "session_end", "reason": "other", "raw_payload": ""},
            {"do": "stop", "omit": ["background_tasks", "permission_mode", "cwd"]},
        ]
    )
    assert env.run("--session-id", SID).returncode == 0
    records = read_jsonl(env.hooks)
    first = json.loads(records[0]["stdin"])
    assert (first["source"], first["extra"]) == ("weird", [1])
    assert json.loads(records[1]["stdin"]) == env.common("Stop")
    assert records[2]["stdin"] == "{not json"
    assert records[3]["stdin"] == ""
    minimal = env.common("Stop", stop_hook_active=False)
    del minimal["permission_mode"], minimal["cwd"]
    assert json.loads(records[4]["stdin"]) == minimal


def test_unicode_and_large_payloads_reach_the_hook_intact(env: Env) -> None:
    big = "x" * (2 * 1024 * 1024)
    text = 'café ☃ \U0001f600 ‮ \'; $(touch pwned) `x` "q" \\'
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
    assert not (env.cwd / "pwned").exists()


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


def test_user_prompt_submit_resets_stop_hook_active(env: Env) -> None:
    block = json.dumps({"decision": "block", "reason": "again"})
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout=block if e == "Stop" else ""),
        [("Stop", None, 60), ("UserPromptSubmit", None, 60)],
    )
    env.setup(
        [{"do": "stop", "on_block": [{"do": "user_prompt_submit"}, {"do": "stop"}]}],
        settings=settings,
    )
    assert env.run().returncode == 0
    stops = [c["payload"] for c in env.hook_calls() if c["event"] == "Stop"]
    assert [s["stop_hook_active"] for s in stops] == [False, False]


def test_exit_2_blocks_with_stderr_as_reason(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], exit_code=2, stderr="no report\n"),
        [("Stop", None, 60)],
    )
    env.setup([{"do": "stop", "on_block": [{"do": "exit", "code": 7}]}], settings=settings)
    assert env.run().returncode == 7
    assert env.records("block")[0]["reasons"] == ["no report"]


@pytest.mark.parametrize(
    ("event", "step"),
    [
        ("SessionStart", {"do": "session_start"}),
        ("Notification", {"do": "notification", "notification_type": "idle_prompt"}),
        ("SessionEnd", {"do": "session_end", "reason": "other"}),
    ],
)
@pytest.mark.parametrize("how", ["exit2", "decision"])
def test_blocking_only_applies_to_blocking_events(
    env: Env, event: str, step: dict[str, Any], how: str
) -> None:
    answer: dict[str, Any] = (
        {"exit_code": 2, "stderr": "no\n"}
        if how == "exit2"
        else {"stdout": json.dumps({"decision": "block", "reason": "no"})}
    )
    settings = hook_settings(lambda e: recorder(env.hooks, args=[e], **answer), [(event, None, 5)])
    env.setup([{**step, "on_block": [{"do": "exit", "code": 7}]}], settings=settings)
    assert env.run().returncode == 0
    assert env.records("block") == []
    assert len(env.hook_calls()) == 1


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


@pytest.mark.parametrize("name", ["Stop", None])
def test_hook_specific_output_for_another_event_is_invalid(env: Env, name: str | None) -> None:
    specific: dict[str, Any] = {"additionalContext": "x"}
    if name is not None:
        specific["hookEventName"] = name
    stdout = json.dumps({"hookSpecificOutput": specific})
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout=stdout), [("SessionStart", None, 30)]
    )
    env.setup([{"do": "session_start"}], settings=settings)
    assert env.run().returncode == 0
    assert env.records("context") == []
    assert len(env.records("hook_output_invalid")) == 1


def test_plain_session_start_stdout_is_context(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout="hello\n"), [("SessionStart", None, 30)]
    )
    env.setup([{"do": "session_start"}], settings=settings)
    assert env.run().returncode == 0
    assert env.records("context")[0]["contexts"] == ["hello\n"]


@pytest.mark.parametrize("ignore_term", [False, True])
def test_a_hook_past_its_timeout_is_ended_and_its_output_dropped(
    env: Env, ignore_term: bool, leaked: list[int]
) -> None:
    block = json.dumps({"decision": "block", "reason": "late"})
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], stdout=block, sleep=30, ignore_term=ignore_term),
        [("Stop", None, 1)],
    )
    env.setup([{"do": "stop", "on_block": [{"do": "exit", "code": 7}]}], settings=settings)
    result = env.run(timeout=20)
    leaked.append(env.hook_calls()[0]["pid"])
    assert result.returncode == 0
    (hook,) = env.records("hook")
    assert hook["timed_out"] is True
    # SIGTERM to the hook's process, SIGKILL to its group after the grace; whether the hook's
    # process is ``sh`` or (exec'd) the recorder depends on /bin/sh.
    assert hook["exit"] in (-signal.SIGTERM, -signal.SIGKILL)
    assert "stdout" not in hook
    assert env.records("block") == []
    # The recorder (which may ignore SIGTERM) is gone long before its 30 s sleep ends.
    assert gone(env.hook_calls()[0]["pid"])


@pytest.mark.skipif(shutil.which("setsid") is None, reason="needs setsid")
def test_a_timed_out_hook_does_not_wait_for_a_grandchild_holding_its_output(
    env: Env, leaked: list[int]
) -> None:
    pidfile = env.tmp / "grandchild.pid"
    command = f"setsid sh -c 'echo $$ > {pidfile}; exec sleep 8' & echo started"
    hook = {"type": "command", "command": command, "timeout": 1}
    settings = {"hooks": {"Stop": [{"hooks": [hook]}]}}
    env.setup([{"do": "stop"}], settings=settings)
    began = time.monotonic()
    try:
        result = env.run(timeout=30)
        elapsed = time.monotonic() - began
    finally:
        with contextlib.suppress(AssertionError):
            leaked.append(read_pid(pidfile, timeout=5))
    assert result.returncode == 0
    assert env.records("hook")[0]["timed_out"] is True
    assert elapsed < 4


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
    assert post["payload"]["tool_response"] == {
        "stdout": "report queued\n",
        "stderr": "",
        "interrupted": False,
        "isImage": False,
    }
    assert post["payload"]["tool_use_id"].startswith("toolu_")
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


def test_a_failed_command_fires_post_tool_use_failure(env: Env) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e]),
        [("PostToolUse", None, 60), ("PostToolUseFailure", "Bash", 60)],
    )
    env.setup([{"do": "bash", "command": "echo bad >&2; exit 3"}], settings=settings)
    assert env.run().returncode == 0
    (call,) = env.hook_calls()
    assert call["event"] == "PostToolUseFailure"
    payload = call["payload"]
    assert payload["hook_event_name"] == "PostToolUseFailure"
    assert (payload["tool_name"], payload["tool_input"]) == (
        "Bash",
        {"command": "echo bad >&2; exit 3"},
    )
    assert payload["tool_use_id"].startswith("toolu_")
    assert "Exit code 3" in payload["error"] and "bad" in payload["error"]


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
    assert len(env.hook_calls()) == 1  # ``never``: the second report fired no PostToolUse


def test_bash_and_write_file(env: Env) -> None:
    env.setup(
        [
            {"do": "write_file", "path": "summary.md", "content": "sé\n"},
            {"do": "bash", "command": "cat summary.md; echo err >&2"},
        ]
    )
    assert env.run().returncode == 0
    write, post = env.hook_calls()
    path = str(env.cwd / "summary.md")
    assert write["payload"]["tool_name"] == "Write"
    assert write["payload"]["tool_input"] == {"file_path": path, "content": "sé\n"}
    assert write["payload"]["tool_response"] == {"filePath": path, "success": True}
    assert post["payload"]["tool_response"]["stdout"] == "sé\n"
    assert post["payload"]["tool_response"]["stderr"] == "err\n"


def test_write_file_post_tool_use_can_be_turned_off(env: Env) -> None:
    env.setup([{"do": "write_file", "path": "a.md", "content": "", "post_tool_use": "never"}])
    assert env.run().returncode == 0
    assert env.hook_calls() == []
    assert (env.cwd / "a.md").exists()


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
    result = env.run(*HEADLESS, "--session-id", SID, "go")
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
    result = env.run(*HEADLESS, "go")
    first, tail = result.stdout.split("\n")
    assert json.loads(first)["result"] == "one"
    assert len(tail) == 20 and tail.startswith('{"type": "result"')
    with pytest.raises(json.JSONDecodeError):
        json.loads(tail)


def test_a_truncated_tail_cut_inside_a_utf8_sequence(env: Env) -> None:
    prefix = '{"type": "result", "subtype": "success", "is_error": false, "result": "'
    cut = len(prefix.encode("utf-8")) + 1
    env.setup([{"do": "truncated_result", "result": "éé", "bytes": cut}])
    proc = subprocess.run(
        env.argv(*HEADLESS, "go"),
        env={**os.environ, **env.env()},
        cwd=env.cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == prefix.encode("utf-8") + b"\xc3"
    with pytest.raises(UnicodeDecodeError):
        proc.stdout.decode("utf-8")


def test_truncation_must_cut_the_object(env: Env) -> None:
    env.setup([{"do": "truncated_result", "chars": 100000}])
    assert env.run(*HEADLESS, "go").returncode == FAKE_ERROR


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
    child = env.spawn(*HEADLESS, "go")
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


def test_die_action_dies_by_the_signal(env: Env) -> None:
    env.setup([{"do": "hang"}], signals={"HUP": {"die": True, "session_end": "other"}})
    child = env.spawn()
    wait_step(env, "hang")
    child.send_signal(signal.SIGHUP)
    assert child.wait(15) == -signal.SIGHUP
    assert [c["payload"]["reason"] for c in env.hook_calls()] == ["other"]


def test_interactive_sigint_is_ignored_by_default(env: Env) -> None:
    env.setup([{"do": "hang"}])
    child = env.spawn()
    wait_step(env, "hang")
    child.send_signal(signal.SIGINT)
    wait_for(env.log, lambda r: r["event"] == "signal_ignored" and r["signal"] == "INT")
    assert child.wait(0.2) is None
    child.send_signal(signal.SIGTERM)
    assert child.wait(15) == 143


def test_headless_sigint_exits_130(env: Env) -> None:
    env.setup([{"do": "hang"}])
    child = env.spawn("-p", "go")
    wait_step(env, "hang")
    child.send_signal(signal.SIGINT)
    assert child.wait(15) == 130


def test_ignoring_sigterm(env: Env) -> None:
    env.setup([{"do": "ignore_signals", "signals": ["TERM"]}, {"do": "hang"}])
    child = env.spawn(*HEADLESS, "go")
    wait_step(env, "hang")
    child.send_signal(signal.SIGTERM)
    wait_for(env.log, lambda r: r["event"] == "signal_ignored")
    assert child.wait(0.5) is None
    child.send_signal(signal.SIGKILL)
    assert child.wait(15) == -signal.SIGKILL
    assert env.hook_calls() == []


def test_an_ignored_signal_still_reaches_hooks_started_later(env: Env, leaked: list[int]) -> None:
    settings = hook_settings(
        lambda e: exec_recorder(env.hooks, args=[e], sleep=60), [("Stop", None, 60)]
    )
    env.setup([{"do": "ignore_signals", "signals": ["TERM"]}, {"do": "stop"}], settings=settings)
    child = env.spawn()
    hook = wait_for(env.hooks, lambda r: r["args"] == ["Stop"])
    leaked.append(hook["pid"])
    os.kill(hook["pid"], signal.SIGTERM)  # the hook is the fake's unreaped child: exact
    assert child.wait(15) == 0
    assert env.records("hook")[0]["exit"] == -signal.SIGTERM


def test_signal_actions_from_the_scenario(env: Env) -> None:
    env.setup(
        [{"do": "hang"}],
        signals={"TERM": {"exit": 0, "session_end": "logout"}, "INT": "ignore"},
    )
    child = env.spawn("-p", "go")
    wait_step(env, "hang")
    child.send_signal(signal.SIGINT)
    wait_for(env.log, lambda r: r["event"] == "signal_ignored" and r["signal"] == "INT")
    child.send_signal(signal.SIGTERM)
    assert child.wait(15) == 0
    assert env.hook_calls()[0]["payload"]["reason"] == "logout"


def test_sigterm_during_a_hook_ends_the_hook_first(env: Env, leaked: list[int]) -> None:
    settings = hook_settings(
        lambda e: recorder(env.hooks, args=[e], sleep=60 if e == "Stop" else 0),
        [("Stop", None, 60), ("SessionEnd", None, 30)],
    )
    env.setup([{"do": "stop"}], settings=settings)
    child = env.spawn()
    hook = wait_for(env.hooks, lambda r: r["args"] == ["Stop"])
    leaked.append(hook["pid"])
    child.send_signal(signal.SIGTERM)
    assert child.wait(15) == 143
    assert [c["event"] for c in env.hook_calls()] == ["Stop", "SessionEnd"]
    assert gone(hook["pid"])


def test_a_second_signal_while_unwinding_is_only_logged(env: Env, leaked: list[int]) -> None:
    settings = hook_settings(
        lambda e: exec_recorder(env.hooks, args=[e], sleep=60), [("SessionEnd", None, 60)]
    )
    env.setup([{"do": "hang"}], settings=settings)
    child = env.spawn()
    wait_step(env, "hang")
    child.send_signal(signal.SIGTERM)
    hook = wait_for(env.hooks, lambda r: r["args"] == ["SessionEnd"])
    leaked.append(hook["pid"])
    child.send_signal(signal.SIGTERM)
    wait_for(env.log, lambda r: r["event"] == "signal" and len(env.records("signal")) == 2)
    os.kill(hook["pid"], signal.SIGTERM)  # the hook is the fake's unreaped child: exact
    assert child.wait(15) == 143
    assert env.records("hook")[0]["exit"] == -signal.SIGTERM


def test_a_signal_after_the_last_step_takes_its_outcome(env: Env) -> None:
    env.setup([{"do": "touch", "path": "ran"}], raise_after_steps="TERM")
    result = env.run("-p", "go")
    assert result.returncode == 143
    assert "Traceback" not in result.stderr
    assert [c["payload"]["reason"] for c in env.hook_calls()] == ["other"]
    assert env.records("exit")[0]["code"] == 143


def test_a_signal_while_handling_an_error_still_cleans_up(env: Env) -> None:
    env.setup(
        [{"do": "write_file", "path": "missing/dir/file.md", "content": "x"}],
        signal_on_error="TERM",
    )
    result = env.run()
    assert result.returncode == FAKE_ERROR
    assert "internal error" not in result.stderr
    assert [r["signal"] for r in env.records("signal")] == ["TERM"]
    assert env.records("error")
    assert env.records("exit")[0]["code"] == FAKE_ERROR


@linux_only
def test_sigterm_during_a_bash_step_leaves_no_child(env: Env, leaked: list[int]) -> None:
    pidfile = env.tmp / "child.pid"
    env.setup([{"do": "bash", "command": f"sleep 60 & echo $! > {pidfile}; wait"}])
    child = env.spawn()
    pid = read_pid(pidfile)
    leaked.append(pid)
    child.send_signal(signal.SIGTERM)
    assert child.wait(15) == 143
    assert gone(pid)


@linux_only
def test_a_killed_fake_leaves_no_hook_behind(env: Env, leaked: list[int]) -> None:
    settings = hook_settings(
        lambda e: exec_recorder(env.hooks, args=[e], sleep=60), [("Stop", None, 60)]
    )
    env.setup([{"do": "stop"}], settings=settings)
    child = env.spawn()
    hook = wait_for(env.hooks, lambda r: r["args"] == ["Stop"])
    leaked.append(hook["pid"])
    child.send_signal(signal.SIGKILL)
    assert child.wait(15) == -signal.SIGKILL
    assert gone(hook["pid"])


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
