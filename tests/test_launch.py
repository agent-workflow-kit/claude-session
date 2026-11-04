"""The launch pipeline's step helpers (interface.md, Launch order and Path rules)."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from claude_session import config, kinds, launch, pathrules
from claude_session.errors import LaunchValidationError, UsageError
from claude_session.gateway import SystemGateway

from .fake_gateway import FakeGateway

SID = "cs-abcd2345"


def load(tmp_path: Path, text: str = "") -> config.Config:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return config.load(str(path))


def process(tmp_path: Path, **environ: str) -> launch.Process:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True, mode=0o700)
    return launch.Process(
        environ={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", **environ},
        caller_cwd=str(tmp_path),
        claude_session="/opt/cs/bin/claude-session",
        interpreter="/usr/bin/python3",
        state_root=str(state),
        gateway=FakeGateway(),
    )


def alloc(tmp_path: Path) -> launch.Allocated:
    root = tmp_path / "state"
    return launch.Allocated(
        csession_id=SID,
        run=1,
        session_dir=str(root / "sessions" / SID),
        private_dir=str(root / "private" / SID),
        warnings=(),
    )


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def resolution(cfg: config.Config, kind: str = "implement", **flags: Any) -> kinds.Resolution:
    return kinds.resolve(cfg, kind, mode="headless", flags=flags or None, origin_url=lambda _: None)


# read_request


def args(**given: Any) -> argparse.Namespace:
    from claude_session import cli

    parsed = cli.build_parser().parse_args(["exec"])
    for key, value in given.items():
        setattr(parsed, key, value)
    return parsed


class _Stream:
    def __init__(self, tty: bool) -> None:
        self.tty = tty
        self.buffer = self

    def isatty(self) -> bool:
        return self.tty


@pytest.mark.parametrize(("stdin", "stdout"), [(False, False), (True, False), (False, True)])
def test_interactive_mode_needs_a_terminal_on_stdin_and_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdin: bool, stdout: bool
) -> None:
    monkeypatch.setattr("sys.stdin", _Stream(stdin))
    monkeypatch.setattr("sys.stdout", _Stream(stdout))
    with pytest.raises(UsageError, match="terminal on stdin and stdout"):
        launch.read_request(args(), load(tmp_path))  # interactive is the default mode
    with pytest.raises(UsageError):
        launch.read_request(args(mode="interactive"), load(tmp_path))


def test_interactive_mode_with_a_terminal_reads_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", _Stream(True))
    monkeypatch.setattr("sys.stdout", _Stream(True))
    request = launch.read_request(args(), load(tmp_path))
    assert request.mode == "interactive" and request.prompt is None


def test_interactive_prompt_from_a_terminal_stdin_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", _Stream(True))
    monkeypatch.setattr("sys.stdout", _Stream(True))
    with pytest.raises(UsageError, match="--prompt-file -"):
        launch.read_request(args(prompt_file="-"), load(tmp_path))


def test_a_raised_unreadable_isatty_counts_as_no_terminal() -> None:
    class Broken:
        def isatty(self) -> bool:
            raise ValueError("closed")

    assert launch._isatty(Broken()) is False
    assert launch._isatty(None) is False


def test_read_request_reads_the_prompt_and_inputs_capped(tmp_path: Path) -> None:
    prompt = tmp_path / "p.md"
    prompt.write_bytes(b"x" * 10)
    request = launch.read_request(
        args(mode="headless", prompt_file=str(prompt), model="m"), load(tmp_path)
    )
    assert request.prompt == b"x" * 10 and request.flags["model"] == "m"
    with pytest.raises(UsageError):
        launch.read_request(
            args(mode="headless", prompt_file=str(prompt)), load(tmp_path, "input_max_bytes: 5\n")
        )


def _escaped_to(limit: int, char: str = "a") -> str:
    """A string whose escaped size (quotes included) is exactly ``limit`` bytes, built of
    ``char`` (1 to 12 escaped bytes each) plus ASCII filler."""
    unit = launch.toolhooks.escaped_size(char) - 2
    count = (limit - 2) // unit
    text = char * count
    text += "a" * (limit - launch.toolhooks.escaped_size(text))
    assert launch.toolhooks.escaped_size(text) == limit
    return text


def _request(tmp_path: Path, **given: Any) -> launch.Request:
    return launch.read_request(args(mode="headless", **given), load(tmp_path))


def test_actor_at_the_cap_is_accepted_and_one_over_is_refused(tmp_path: Path) -> None:
    cap = launch.ACTOR_MAX_ESCAPED_BYTES
    assert _request(tmp_path, actor=_escaped_to(cap)).actor == _escaped_to(cap)
    with pytest.raises(UsageError, match="--actor"):
        _request(tmp_path, actor=_escaped_to(cap + 1))


@pytest.mark.parametrize("char", ["\x00", '"', "\\", "\u00e9", "\u20ac", "\U0001f600"])
def test_actor_is_capped_by_its_escaped_size_not_its_length(tmp_path: Path, char: str) -> None:
    cap = launch.ACTOR_MAX_ESCAPED_BYTES
    under = _escaped_to(cap, char)
    assert len(under) < cap or char in '"\\'
    assert _request(tmp_path, actor=under).actor == under
    with pytest.raises(UsageError):
        _request(tmp_path, actor=_escaped_to(cap + 1, char))


def test_an_oversized_ascii_actor_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="--actor"):
        _request(tmp_path, actor="a" * 1_000_000)


def test_the_effective_actor_from_the_config_name_is_capped_too(tmp_path: Path) -> None:
    name = "n" * (launch.ACTOR_MAX_ESCAPED_BYTES)
    cfg = load(tmp_path, f"me:\n  name: {name}\n")
    parsed = args(mode="headless", actor=None)
    from unittest import mock

    with mock.patch.object(launch.sys, "stdin", _Stream(True)):
        with pytest.raises(UsageError, match="--actor"):
            launch.read_request(parsed, cfg)


def test_decision_ids_at_the_cap_are_accepted_and_one_over_is_refused(tmp_path: Path) -> None:
    cap = launch.ARGV_DECISION_IDS_MAX_BYTES
    # Each id costs its escaped size plus one separator byte, as for the hooks' ids.
    one = _escaped_to(cap - 1)
    assert _request(tmp_path, decision_id=[one]).decision_ids == (one,)
    with pytest.raises(UsageError, match="--decision-id"):
        _request(tmp_path, decision_id=[_escaped_to(cap)])


def test_decision_ids_are_capped_in_all_not_each(tmp_path: Path) -> None:
    cap = launch.ARGV_DECISION_IDS_MAX_BYTES
    half = cap // 2
    first, second = _escaped_to(half - 1), _escaped_to(cap - half - 1)
    assert _request(tmp_path, decision_id=[first, second]).decision_ids == (first, second)
    with pytest.raises(UsageError, match="--decision-id"):
        _request(tmp_path, decision_id=[first, second + "a"])


def test_many_small_decision_ids_are_capped_in_all(tmp_path: Path) -> None:
    ids = ["x"] * (launch.ARGV_DECISION_IDS_MAX_BYTES // 4 + 1)  # 3 escaped + 1 separator each
    with pytest.raises(UsageError, match="--decision-id"):
        _request(tmp_path, decision_id=ids)


@pytest.mark.parametrize("char", ["\x00", '"', "\u20ac", "\U0001f600"])
def test_decision_ids_are_capped_by_escaped_size_not_length(tmp_path: Path, char: str) -> None:
    cap = launch.ARGV_DECISION_IDS_MAX_BYTES
    fits = _escaped_to(cap - 1, char)
    assert _request(tmp_path, decision_id=[fits]).decision_ids == (fits,)
    with pytest.raises(UsageError, match="--decision-id"):
        _request(tmp_path, decision_id=[_escaped_to(cap, char)])


def test_an_oversized_ascii_decision_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="--decision-id"):
        _request(tmp_path, decision_id=["d" * 1_000_000])


def test_the_cap_is_checked_before_the_prompt_is_read(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.md")
    with pytest.raises(UsageError, match="--decision-id"):
        _request(tmp_path, decision_id=["d" * 1_000_000], prompt_file=missing)


def test_a_cwd_that_is_not_a_directory_is_a_usage_error(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    request = launch.read_request(args(mode="headless", cwd=str(tmp_path / "nope")), cfg)
    with pytest.raises(UsageError, match="--cwd"):
        launch.resolve_kind(request, cfg, process(tmp_path))


def test_the_repo_is_derived_with_git_run_by_name_from_its_realpath(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    proc = process(tmp_path)
    gw = proc.gateway
    assert isinstance(gw, FakeGateway)
    gw.on(["git"], stdout="https://example.com/group/project.git\n")
    resolved = launch.resolve_kind(launch.read_request(args(mode="headless"), cfg), cfg, proc)
    assert resolved.resolution.repo == "example.com/group/project"
    (call,) = gw.calls
    assert call.argv[0] == "git" and call.executable == os.path.realpath(call.executable or "")
    assert resolved.caller_cwd == os.path.realpath(tmp_path)


def test_step_2_never_runs_a_git_the_session_could_have_planted(tmp_path: Path) -> None:
    executable(tmp_path / "bin" / "git")
    cfg = load(tmp_path)
    proc = process(tmp_path, PATH=f"{tmp_path / 'bin'}:/usr/bin:/bin")
    gw = proc.gateway
    assert isinstance(gw, FakeGateway)
    gw.on(["git"], stdout="https://example.com/group/project.git\n")
    resolved = launch.resolve_kind(launch.read_request(args(mode="headless"), cfg), cfg, proc)
    assert gw.calls == []
    assert resolved.resolution.repo is None
    assert any("session-writable" in w for w in resolved.resolution.warnings)


def test_step_2_git_under_the_tmp_root_or_the_sessions_is_never_run(tmp_path: Path) -> None:
    proc = process(tmp_path)
    for path in (pathrules.tmp_parent(), os.path.join(proc.state_root, "sessions")):
        assert launch.step2_writable(proc, str(tmp_path / "cwd"))(os.path.join(path, "x", "git"))
    assert not launch.step2_writable(proc, None)("/usr/bin/git")


# Tools


def test_claude_not_found(tmp_path: Path) -> None:
    cfg = load(tmp_path, f"claude_bin: {tmp_path / 'missing'}\n")
    with pytest.raises(LaunchValidationError) as info:
        launch.check_tools(cfg, resolution(cfg), process(tmp_path), launch.NO_SANDBOX, None)
    assert info.value.end_reason == "claude_not_found"
    assert info.value.hint


def test_claude_inside_a_session_writable_path_is_refused(tmp_path: Path) -> None:
    claude = executable(tmp_path / "cwd" / "bin" / "claude")
    cfg = load(tmp_path, f"claude_bin: {claude}\n")
    writable = pathrules.SessionWritable([str(tmp_path / "cwd")])
    git = pathrules.resolve_tool("git", ["/usr/bin", "/bin"], launch.NO_SANDBOX)
    with pytest.raises(LaunchValidationError) as info:
        launch.check_tools(cfg, resolution(cfg), process(tmp_path), writable, git)
    assert info.value.end_reason == "unsafe_path"


def test_a_claude_whose_interpreter_is_under_a_session_writable_path_is_refused(
    tmp_path: Path,
) -> None:
    """No run lets ``claude --version`` (or the session) run a planted ``#!`` interpreter;
    a trusted run (``NO_SANDBOX``) checks it too, and refuses a line it cannot check."""
    planted = executable(tmp_path / "cwd" / "cs-test-interp")
    claude = tmp_path / "safe" / "claude"
    claude.parent.mkdir()
    claude.write_text(f"#!{planted}\n")
    claude.chmod(0o755)
    cfg = load(tmp_path, f"claude_bin: {claude}\n")
    writable = pathrules.SessionWritable([str(tmp_path / "cwd")])
    git = pathrules.resolve_tool("git", ["/usr/bin", "/bin"], launch.NO_SANDBOX)
    with pytest.raises(LaunchValidationError) as info:
        launch.check_tools(cfg, resolution(cfg), process(tmp_path), writable, git)
    assert info.value.end_reason == "unsafe_path"
    assert "interpreter" in info.value.message
    claude.write_text("#!/usr/bin/env -S cs-test-interp\n")  # cannot be checked: refused
    with pytest.raises(LaunchValidationError) as info:
        launch.check_tools(cfg, resolution(cfg), process(tmp_path), launch.NO_SANDBOX, git)
    assert info.value.end_reason == "unsafe_path"
    claude.write_text("#!/bin/sh\n")
    tools = launch.check_tools(cfg, resolution(cfg), process(tmp_path), launch.NO_SANDBOX, git)
    assert tools.claude.path == str(claude)


def test_claude_resolves_to_its_realpath_with_its_name(tmp_path: Path) -> None:
    real = executable(tmp_path / "real" / "claude-2.1")
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir()
    link.symlink_to(real)
    cfg = load(tmp_path)
    proc = process(tmp_path, PATH=f"{tmp_path / 'bin'}:/usr/bin:/bin")
    git = pathrules.resolve_tool("git", launch._path_entries(proc.environ, []), launch.NO_SANDBOX)  # pyright: ignore[reportPrivateUsage]
    tools = launch.check_tools(cfg, resolution(cfg), proc, launch.NO_SANDBOX, git)
    assert tools.claude == pathrules.ResolvedTool(path=str(real), name="claude")
    assert tools.bwrap is None and tools.socat is None  # sandbox off: never looked up


# The claude --version check


def test_the_version_probe_path_is_the_tool_lookup_path_without_writable_entries(
    tmp_path: Path,
) -> None:
    """``claude`` was looked up on ``env.path_prepend`` then the caller's ``PATH``; its
    ``#!/usr/bin/env node`` line finds ``node`` there too, never under a writable path."""
    prepend, unsafe = tmp_path / "prepend", tmp_path / "cwd" / "bin"
    prepend.mkdir()
    unsafe.mkdir(parents=True)
    cfg = load(tmp_path, f"kinds:\n  implement:\n    env:\n      path_prepend: [{prepend}]\n")
    proc = process(tmp_path, PATH=f"{unsafe}:rel:/usr/bin:/bin", ANTHROPIC_API_KEY="sk-secret")
    gw = proc.gateway
    assert isinstance(gw, FakeGateway)
    gw.on(["claude", "--version"], stdout="2.1.312 (Claude Code)\n")
    claude = pathrules.ResolvedTool("/opt/claude/claude", "claude")
    writable = pathrules.SessionWritable([str(tmp_path / "cwd")])
    checked = launch.check_claude_version(cfg, resolution(cfg), proc, claude, writable)
    assert (checked.text, checked.warning) == ("2.1.312 (Claude Code)", None)
    (call,) = gw.calls
    assert call.env is not None
    assert call.env["PATH"] == f"{prepend}:/usr/bin:/bin"
    assert "ANTHROPIC_API_KEY" not in call.env
    assert (call.executable, call.argv, call.cwd) == (
        "/opt/claude/claude",
        ("claude", "--version"),
        "/",
    )


def test_the_version_check_uses_the_configured_minimum(tmp_path: Path) -> None:
    cfg = load(tmp_path, "min_claude_version: '3'\n")
    proc = process(tmp_path)
    assert isinstance(proc.gateway, FakeGateway)
    proc.gateway.on(["claude", "--version"], stdout="2.9.999 (Claude Code)\n")
    claude = pathrules.ResolvedTool("/opt/claude/claude", "claude")
    with pytest.raises(LaunchValidationError) as info:
        launch.check_claude_version(cfg, resolution(cfg), proc, claude, launch.NO_SANDBOX)
    assert info.value.end_reason == "claude_too_old"


@pytest.mark.parametrize(
    "answer", [{"stdout": "no version here\n"}, {"returncode": 1}, {"timeout": True}]
)
def test_an_unknown_version_warns_and_the_launch_effects_go_on(
    tmp_path: Path, answer: dict[str, Any]
) -> None:
    claude = executable(tmp_path / "tools" / "claude")
    cfg = load(tmp_path, f"claude_bin: {claude}\n")
    proc = process(tmp_path)
    assert isinstance(proc.gateway, FakeGateway)
    proc.gateway.on(["claude", "--version"], **answer)
    tools = launch.Tools(
        claude=pathrules.ResolvedTool(str(claude), "claude"), git=None, bwrap=None, socat=None
    )
    found = launch.Recording.claude_version(
        launch.Recording.__new__(launch.Recording),
        cfg,
        resolution(cfg),
        proc,
        tools,
        launch.NO_SANDBOX,
        lambda: None,
    )
    (warning,) = found
    assert warning.startswith("claude_version_unknown: ")


def dry_run_version(
    tmp_path: Path,
    writable: pathrules.SessionWritable,
    known: pathrules.SessionWritable | None,
) -> tuple[list[str], FakeGateway]:
    claude = executable(tmp_path / "tools" / "claude")
    cfg = load(tmp_path, f"claude_bin: {claude}\n")
    proc = process(tmp_path)
    assert isinstance(proc.gateway, FakeGateway)
    proc.gateway.on(["claude", "--version"], stdout="2.1.312 (Claude Code)\n")
    tools = launch.Tools(
        claude=pathrules.ResolvedTool(str(claude), "claude"), git=None, bwrap=None, socat=None
    )
    found = launch.Effects().claude_version(
        cfg, resolution(cfg), proc, tools, writable, lambda: known
    )
    return found, proc.gateway


def test_a_dry_run_probes_a_claude_outside_every_known_path(tmp_path: Path) -> None:
    found, gw = dry_run_version(
        tmp_path, pathrules.SessionWritable([]), pathrules.SessionWritable([])
    )
    assert found == [] and len(gw.calls) == 1


@pytest.mark.parametrize("where", ["known", "writable"])
def test_a_dry_run_never_runs_a_claude_under_a_known_or_run_path(
    tmp_path: Path, where: str
) -> None:
    under = pathrules.SessionWritable([str(tmp_path / "tools")])
    nothing = pathrules.SessionWritable([])
    known, writable = (under, nothing) if where == "known" else (nothing, under)
    found, gw = dry_run_version(tmp_path, writable, known)
    assert gw.calls == []
    (warning,) = found
    assert warning.startswith("claude --version not run (not probed by a dry run")


def test_a_dry_run_with_unknown_paths_runs_no_claude(tmp_path: Path) -> None:
    found, gw = dry_run_version(tmp_path, pathrules.SessionWritable([]), None)
    assert gw.calls == [] and "not known" in found[0]


def test_a_git_that_changed_once_every_writable_path_is_known_is_refused(tmp_path: Path) -> None:
    claude = executable(tmp_path / "bin" / "claude")
    cfg = load(tmp_path, f"claude_bin: {claude}\n")
    other = pathrules.ResolvedTool(path="/somewhere/else/git", name="git")
    with pytest.raises(LaunchValidationError) as info:
        launch.check_tools(cfg, resolution(cfg), process(tmp_path), launch.NO_SANDBOX, other)
    assert info.value.end_reason == "unsafe_path"


# Git


def test_no_git_with_a_git_entry_above_is_unsafe_cwd(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "sub").mkdir()
    cfg = load(tmp_path)
    with pytest.raises(LaunchValidationError) as info:
        launch.check_git(resolution(cfg), str(tmp_path / "sub"), None, process(tmp_path))
    assert info.value.end_reason == "unsafe_cwd"


def test_no_git_and_no_repository_is_no_git_dir(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    assert launch.check_git(resolution(cfg), str(tmp_path), None, process(tmp_path)) == (None, [])
    with pytest.raises(LaunchValidationError) as info:
        launch.check_git(resolution(cfg, "respond"), str(tmp_path), None, process(tmp_path))
    assert info.value.end_reason == "no_git_cwd"


def test_a_git_failure_is_unsafe_cwd(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    proc = process(tmp_path)
    assert isinstance(proc.gateway, FakeGateway)
    proc.gateway.on(["git"], returncode=128, stderr="fatal: detected dubious ownership\n")
    git = pathrules.ResolvedTool(path="/usr/bin/git", name="git")
    with pytest.raises(LaunchValidationError) as info:
        launch.check_git(resolution(cfg), str(tmp_path), git, proc)
    assert info.value.end_reason == "unsafe_cwd"
    assert "dubious" in (info.value.detail or "")


def test_a_real_repository_is_found_through_the_real_gateway(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path / "r")], check=True, timeout=30)
    cfg = load(tmp_path)
    proc = launch.Process(**{**process(tmp_path).__dict__, "gateway": SystemGateway()})
    git = pathrules.resolve_tool("git", ["/usr/bin", "/bin"], launch.NO_SANDBOX)
    dirs, warnings = launch.check_git(resolution(cfg), str(tmp_path / "r"), git, proc)
    assert dirs is not None and dirs.common_dir == os.path.realpath(tmp_path / "r" / ".git")
    assert warnings == []


# Plugins, secret index


def test_a_missing_plugin_dir_fails_a_trusted_run_too(tmp_path: Path) -> None:
    rules = pathrules.PathRules(
        pathrules.Host.from_environ(process(tmp_path).environ, state_root=str(tmp_path / "state")),
        session_dir=str(tmp_path / "state" / "sessions" / SID),
        cwd=str(tmp_path),
        caller_cwd=str(tmp_path),
    )
    with pytest.raises(LaunchValidationError) as info:
        launch.check_plugins(rules, ["missing"], None, str(tmp_path))
    assert info.value.end_reason == "plugin_dir_missing"
    (tmp_path / "plug").mkdir()
    assert launch.check_plugins(rules, ["plug", "plug"], None, str(tmp_path)) == (
        str(tmp_path / "plug"),
    )


def test_secret_values_over_the_index_limit_fail_the_launch() -> None:
    from claude_session import secretindex

    values = frozenset({"a" * (secretindex.VALUES_MAX_BYTES + 1)})
    with pytest.raises(LaunchValidationError) as info:
        launch.secret_index(values)
    assert info.value.end_reason == "config_invalid"
    assert "secret values exceed 256 KiB" in launch.failure_message(info.value)
    assert launch.secret_index(frozenset({"b" * 64})).startswith(secretindex.MAGIC)


def test_the_failure_message_is_the_error_its_detail_and_the_hint() -> None:
    error = LaunchValidationError("bad\x07 thing", detail="at /x", end_reason="unsafe_path")
    assert launch.failure_message(error) == "bad thing: at /x"
    error = LaunchValidationError("no claude", end_reason="claude_not_found")
    assert launch.failure_message(error) == f"no claude; {launch.HINTS['claude_not_found']}"
    long = LaunchValidationError("x" * 5000, end_reason="unsafe_path")
    assert len(launch.failure_message(long)) == 2048


def test_fail_launch_records_the_hint_as_the_message(tmp_path: Path) -> None:
    from claude_session import ids
    from claude_session.store import Store

    from .test_store_runs import new_run

    with Store.open(str(tmp_path / "s" / "state.db")) as st:
        sid = st.allocate(new_run(), new_id=ids.new_session_id)
        a = launch.Allocated(sid, 1, "/s", "/p", ())
        error = LaunchValidationError("no bwrap", end_reason="sandbox_unavailable", hint="install")
        assert launch.fail_launch(st, a, error)
        row = st.run_record(sid, 1)
    assert (row["outcome"], row["end_reason"], row["exit_code"], row["message"]) == (
        "not-started",
        "sandbox_unavailable",
        12,
        "no bwrap; install",
    )
    assert row["phase"] == "launch" and row["session_state"] == "not-started"


# The notes command (resolved like the tools; never fails the launch)


def test_notes_command_resolves_on_the_path_to_its_realpath(tmp_path: Path) -> None:
    real = executable(tmp_path / "real" / "memoctl-1.0")
    link = tmp_path / "bin" / "memoctl"
    link.parent.mkdir()
    link.symlink_to(real)
    cfg = load(tmp_path)
    proc = process(tmp_path, PATH=f"rel:{tmp_path / 'bin'}:/usr/bin")
    tool, problem = launch.resolve_notes(cfg, resolution(cfg), proc, launch.NO_SANDBOX)
    assert (tool, problem) == (pathrules.ResolvedTool(path=str(real), name="memoctl"), None)


def test_a_missing_notes_command_is_a_problem_not_an_error(tmp_path: Path) -> None:
    cfg = load(tmp_path, "notes_context_cmd: [no-such-notes-command, context]\n")
    tool, problem = launch.resolve_notes(cfg, resolution(cfg), process(tmp_path), launch.NO_SANDBOX)
    assert tool is None
    assert problem == "notes command not found: 'no-such-notes-command'"


@pytest.mark.parametrize("name", ["bin/memoctl", "./memoctl"])
def test_a_relative_notes_command_path_is_refused(tmp_path: Path, name: str) -> None:
    cfg = load(tmp_path, f"notes_context_cmd: [{name}]\n")
    tool, problem = launch.resolve_notes(cfg, resolution(cfg), process(tmp_path), launch.NO_SANDBOX)
    assert tool is None and problem is not None and problem.startswith("notes command refused")


def test_a_notes_command_in_a_session_writable_path_is_refused(tmp_path: Path) -> None:
    cmd = executable(tmp_path / "cwd" / "memoctl")
    cfg = load(tmp_path, f"notes_context_cmd: [{cmd}]\n")
    writable = pathrules.SessionWritable([str(tmp_path / "cwd")])
    tool, problem = launch.resolve_notes(cfg, resolution(cfg), process(tmp_path), writable)
    assert tool is None and problem is not None and "session-writable" in problem


def test_a_kind_without_notes_context_resolves_nothing(tmp_path: Path) -> None:
    cfg = load(tmp_path, "notes_context_cmd: [/no/such]\n")
    tool, problem = launch.resolve_notes(
        cfg, resolution(cfg, "audit"), process(tmp_path), launch.NO_SANDBOX
    )
    assert (tool, problem) == (None, None)


def test_hookconf_drops_notes_settings_too_large_for_the_file(tmp_path: Path) -> None:
    from claude_session import notes

    cfg = load(tmp_path)
    big = notes.NotesConf(
        argv=("m", *(f"--label=k{i}={'v' * 250}" for i in range(300))),
        executable="/usr/bin/m",
        env={"PATH": "/bin"},
        timeout=5.0,
        max_bytes=10,
        sources=("startup",),
    )
    data, problem = launch.hookconf([], cfg, big)
    assert problem == "notes command settings too large"
    assert json.loads(data)["notes"] is None
    data, problem = launch.hookconf([], cfg, dataclasses.replace(big, argv=("m",)))
    assert problem is None and json.loads(data)["notes"]["argv"] == ["m"]


def test_hookconf_drops_invalid_notes_settings_without_calling_them_too_large(
    tmp_path: Path,
) -> None:
    from claude_session import notes

    cfg = load(tmp_path)
    bad = notes.NotesConf(
        argv=("m", "a\x00b"),
        executable="/usr/bin/m",
        env={"PATH": "/bin"},
        timeout=5.0,
        max_bytes=10,
        sources=("startup",),
    )
    data, problem = launch.hookconf([], cfg, bad)
    assert problem == "notes command settings invalid"
    assert json.loads(data)["notes"] is None
