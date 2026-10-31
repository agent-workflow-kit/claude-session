"""The launch pipeline's step helpers (interface.md, Launch order and Path rules)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pytest

from claude_session import config, kinds, launch, pathrules
from claude_session.errors import InternalError, LaunchValidationError, UsageError
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


def test_interactive_exec_is_not_implemented_yet(tmp_path: Path) -> None:
    with pytest.raises(InternalError, match="--mode headless") as info:
        launch.read_request(args(), load(tmp_path))
    assert info.value.detail == "not_implemented"


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
