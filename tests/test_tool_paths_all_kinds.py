"""Tools a launch or a probe runs (bwrap, socat, git, claude, the notes command) must lie
outside the session-writable paths of every kind, not only the run's own
(``launch.other_kinds_writable``; interface.md, Launch order, step 5; design.md, PATH
sanitizing)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from claude_session import config, envbuild, launch, pathrules, toolhooks
from claude_session.errors import LaunchValidationError

from .test_launch import args, executable, load, process, resolution

# `scribe` is a configured kind (isolated: a configured kind is unless it says otherwise) whose
# runs may write ~/td; the run under test is another kind.
CONFIG = "kinds:\n  scribe:\n    write_dirs: ['~/td']\n"


def entries(tmp_path: Path, *dirs: Path) -> launch.Process:
    return process(tmp_path, PATH=":".join([*map(str, dirs), "/usr/bin", "/bin"]))


def planted(tmp_path: Path, name: str) -> Path:
    """An executable ``name`` in another kind's write dir; returns the directory."""
    return executable(tmp_path / "home" / "td" / "bin" / name).parent


def safe_tools(tmp_path: Path) -> Path:
    """Every tool a launch resolves, in a directory no kind writes."""
    safe = tmp_path / "safe"
    for name in ("claude", "bwrap", "socat", "git"):
        executable(safe / name)
    return safe


def check(cfg: config.Config, proc: launch.Process, kind: str, *, guarded: bool) -> launch.Tools:
    """``launch.check_tools`` for a run of ``kind`` that is not isolated itself (its own set
    is empty), with or without the union of every kind's paths."""
    own = launch.NO_SANDBOX
    git = pathrules.resolve_tool("git", launch._path_entries(proc.environ, []), own)
    guard = guard_for(cfg, proc) if guarded else None
    return launch.check_tools(cfg, resolution(cfg, kind), proc, own, git, guard=guard)


def guard_for(cfg: config.Config, proc: launch.Process) -> pathrules.SessionWritable:
    return launch.other_kinds_writable(cfg, proc, git_cwd=None)


def test_the_union_holds_another_kinds_write_dirs(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    union = guard_for(cfg, process(tmp_path))
    assert union(str(tmp_path / "home" / "td" / "bin" / "x"))
    assert not union(str(tmp_path / "elsewhere"))


@pytest.mark.parametrize("name", ["bwrap", "socat", "git"])
def test_a_tool_planted_in_another_kinds_write_dir_is_refused(tmp_path: Path, name: str) -> None:
    cfg = load(tmp_path, CONFIG)
    proc = entries(tmp_path, planted(tmp_path, name), safe_tools(tmp_path))
    assert check(cfg, proc, "review", guarded=False)  # the run's own set misses it
    with pytest.raises(LaunchValidationError) as info:
        check(cfg, proc, "review", guarded=True)
    assert info.value.end_reason == "unsafe_path"


def test_a_symlinked_tool_into_another_kinds_write_dir_is_refused(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    target = executable(tmp_path / "home" / "td" / "real" / "bwrap")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / "bwrap").symlink_to(target)
    proc = entries(tmp_path, link_dir, safe_tools(tmp_path))
    assert check(cfg, proc, "review", guarded=False)
    with pytest.raises(LaunchValidationError) as info:
        check(cfg, proc, "review", guarded=True)
    assert info.value.end_reason == "unsafe_path"


def test_a_path_entry_symlinked_into_another_kinds_write_dir_is_refused(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    link = tmp_path / "link"
    link.symlink_to(planted(tmp_path, "git"))
    proc = entries(tmp_path, link, safe_tools(tmp_path))
    assert check(cfg, proc, "implement", guarded=False)
    with pytest.raises(LaunchValidationError) as info:
        check(cfg, proc, "implement", guarded=True)
    assert info.value.end_reason == "unsafe_path"


def test_a_relative_path_entry_is_dropped_not_resolved_against_a_write_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load(tmp_path, CONFIG)
    planted(tmp_path, "git")
    monkeypatch.chdir(tmp_path / "home" / "td")
    safe = safe_tools(tmp_path)
    proc = process(tmp_path, PATH=f"bin:{safe}")
    found = check(cfg, proc, "implement", guarded=True)
    assert found.git is not None and found.git.path == str(safe / "git")


def test_a_tool_outside_every_kinds_paths_is_accepted(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    proc = entries(tmp_path, safe_tools(tmp_path))
    found = check(cfg, proc, "review", guarded=True)
    assert found.bwrap is not None and found.socat is not None


def test_unknown_write_paths_of_any_kind_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load(tmp_path, CONFIG)
    monkeypatch.setattr(launch, "kind_profile", lambda *_a, **_k: None)
    with pytest.raises(LaunchValidationError) as info:
        guard_for(cfg, process(tmp_path))
    assert info.value.end_reason == "unsafe_path"


def test_a_per_repo_override_adds_its_write_dirs(tmp_path: Path) -> None:
    cfg = load(
        tmp_path,
        "kinds:\n  scribe: {}\n"
        "repos:\n  o/r:\n    kinds:\n      scribe:\n        write_dirs: ['~/over']\n",
    )
    union = guard_for(cfg, process(tmp_path))
    assert union(str(tmp_path / "home" / "over" / "bin" / "x"))


# Item: a trusted kind's write_dirs are in the union too


def test_a_trusted_kinds_write_dirs_refuse_another_kinds_tool(tmp_path: Path) -> None:
    cfg = load(tmp_path, "kinds:\n  implement:\n    write_dirs: ['~/td']\n")
    assert not resolution(cfg, "implement").isolated()
    proc = entries(tmp_path, planted(tmp_path, "bwrap"), safe_tools(tmp_path))
    with pytest.raises(LaunchValidationError) as info:
        check(cfg, proc, "review", guarded=True)
    assert info.value.end_reason == "unsafe_path"


# The refusal names the kind and the write path that covers the tool


def test_the_refusal_names_the_kind_and_its_write_path(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    proc = entries(tmp_path, planted(tmp_path, "git"), safe_tools(tmp_path))
    guard = launch.tool_guard(cfg, proc, git_cwd=None)
    with pytest.raises(LaunchValidationError) as info, launch.named(guard):
        pathrules.resolve_tool("git", launch._path_entries(proc.environ, []), guard)  # pyright: ignore[reportPrivateUsage]
    assert info.value.end_reason == "unsafe_path"
    assert "kind 'scribe'" in info.value.message
    assert str(tmp_path / "home" / "td") in info.value.message


# Step 2: the git that derives the repo


@pytest.mark.parametrize("kind", ["implement", "respond"])  # trusted, isolated (caller cwd)
def test_step_2_never_runs_a_git_planted_in_another_kinds_write_dir(
    tmp_path: Path, kind: str
) -> None:
    cfg = load(tmp_path, CONFIG)
    git = planted(tmp_path, "git")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    proc = dataclasses.replace(entries(tmp_path, git, safe_tools(tmp_path)), caller_cwd=str(cwd))
    proc.gateway.on(["git"], stdout="https://example.com/group/project.git\n")  # pyright: ignore[reportAttributeAccessIssue]
    req = launch.read_request(args(mode="headless", kind=kind), cfg)
    resolved = launch.resolve_kind(req, cfg, proc)
    assert proc.gateway.calls == []  # pyright: ignore[reportAttributeAccessIssue]
    assert resolved.resolution.repo is None
    assert any("repo not derived" in w for w in resolved.resolution.warnings)


def test_step_2_runs_a_git_outside_every_kinds_paths(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    safe = safe_tools(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    proc = dataclasses.replace(entries(tmp_path, safe), caller_cwd=str(cwd))
    proc.gateway.on(["git"], stdout="https://example.com/group/project.git\n")  # pyright: ignore[reportAttributeAccessIssue]
    req = launch.read_request(args(mode="headless", kind="implement"), cfg)
    resolved = launch.resolve_kind(req, cfg, proc)
    (call,) = proc.gateway.calls  # pyright: ignore[reportAttributeAccessIssue]
    assert call.executable == str(safe / "git")
    assert resolved.resolution.repo == "example.com/group/project"


def test_step_2_runs_no_git_when_another_kinds_paths_are_not_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load(tmp_path, CONFIG)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    proc = dataclasses.replace(entries(tmp_path, safe_tools(tmp_path)), caller_cwd=str(cwd))
    real = launch.kind_profile

    def unknown_scribe(cfg: config.Config, resolution: object, *rest: object) -> object:
        if getattr(resolution, "kind", None) == "scribe":
            return None
        return real(cfg, resolution, *rest)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(launch, "kind_profile", unknown_scribe)
    req = launch.read_request(args(mode="headless", kind="implement"), cfg)
    resolved = launch.resolve_kind(req, cfg, proc)
    assert proc.gateway.calls == []  # pyright: ignore[reportAttributeAccessIssue]
    assert any("repo not derived" in w for w in resolved.resolution.warnings)


# The session PATH (envbuild) against the guard


def build(
    tmp_path: Path, cfg: config.Config, kind: str, path: str, guard: pathrules.Guard
) -> envbuild.SessionEnv:
    session = tmp_path / "state" / "sessions" / "cs-x"
    res = resolution(cfg, kind)
    own = pathrules.SessionWritable([str(session)]) if res.isolated() else None
    return envbuild.build(
        res,
        cfg,
        {"HOME": str(tmp_path / "home"), "PATH": path},
        csession_id="cs-x",
        csession_dir=str(session),
        cwd=str(tmp_path / "cwd"),
        exe_dir="/opt/cs/bin",
        session_writable=own,
        guard=guard,
    )


def test_an_isolated_session_path_entry_under_the_guard_is_refused(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    other = planted(tmp_path, "node")
    guard = launch.tool_guard(cfg, process(tmp_path), git_cwd=None)
    with pytest.raises(LaunchValidationError) as info:
        build(tmp_path, cfg, "review", f"{other}:/usr/bin:/bin", guard)
    assert info.value.end_reason == "unsafe_path"


def test_a_trusted_session_path_drops_entries_under_the_guard(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    other = planted(tmp_path, "node")
    guard = launch.tool_guard(cfg, process(tmp_path), git_cwd=None)
    built = build(tmp_path, cfg, "implement", f"{other}:/usr/bin::rel:/bin", guard)
    assert built.env["PATH"] == "/opt/cs/bin:/usr/bin::rel:/bin"  # the rest kept as it was
    (warning,) = built.warnings
    assert str(other) in warning and "kind 'scribe'" in warning


def test_a_path_set_by_env_set_is_filtered_too(tmp_path: Path) -> None:
    other = tmp_path / "home" / "td" / "bin"
    cfg = load(
        tmp_path,
        "kinds:\n  scribe:\n    write_dirs: ['~/td']\n"
        f"  implement:\n    env:\n      set:\n        PATH: '{other}:/usr/bin'\n",
    )
    other.mkdir(parents=True)
    guard = launch.tool_guard(cfg, process(tmp_path), git_cwd=None)
    built = build(tmp_path, cfg, "implement", "/bin", guard)
    assert str(other) not in built.env["PATH"].split(":")


# claude's #! interpreter as the session resolves it


def interp_case(tmp_path: Path) -> tuple[config.Config, launch.Process, Path]:
    """A claude ``#!/usr/bin/env cs-test-node``: a planted interpreter in another kind's
    write dir EARLIER on PATH, the real one later."""
    cfg = load(tmp_path, CONFIG)
    safe = safe_tools(tmp_path)
    executable(safe / "cs-test-node")
    other = planted(tmp_path, "cs-test-node")
    (safe / "claude").write_text("#!/usr/bin/env cs-test-node\n")
    return cfg, entries(tmp_path, other, safe), other


@pytest.mark.parametrize("kind", ["implement", "review"])
def test_claudes_interpreter_is_never_found_under_the_guard_on_the_session_path(
    tmp_path: Path, kind: str
) -> None:
    cfg, proc, other = interp_case(tmp_path)
    guard = launch.tool_guard(cfg, proc, git_cwd=None)
    res = resolution(cfg, kind)
    git = pathrules.resolve_tool("git", launch._path_entries(proc.environ, []), guard)  # pyright: ignore[reportPrivateUsage]
    tools = launch.check_tools(cfg, res, proc, launch.NO_SANDBOX, git, guard=guard)
    session = tmp_path / "state" / "sessions" / "cs-x"
    own = pathrules.SessionWritable([str(session)]) if res.isolated() else None
    try:
        built = envbuild.build(
            res,
            cfg,
            proc.environ,
            csession_id="cs-x",
            csession_dir=str(session),
            cwd=str(tmp_path),
            exe_dir="/opt/cs/bin",
            session_writable=own,
            guard=guard if own is None else launch.with_own(guard, own),
        )
    except LaunchValidationError as error:  # isolated: the entry is refused
        assert error.end_reason == "unsafe_path"
        return
    assert str(other) not in built.env["PATH"].split(":")  # trusted: the entry is dropped
    launch.check_session_interpreter(tools.claude, built, str(tmp_path), guard)


def test_a_session_path_interpreter_under_the_guard_is_refused(tmp_path: Path) -> None:
    cfg, proc, other = interp_case(tmp_path)
    guard = launch.tool_guard(cfg, proc, git_cwd=None)
    claude = pathrules.ResolvedTool(path=str(tmp_path / "safe" / "claude"), name="claude")
    unfiltered = envbuild.SessionEnv(
        env={"PATH": f"{other}:{tmp_path / 'safe'}"}, auth_names=(), secret_names=()
    )
    with pytest.raises(LaunchValidationError) as info:
        launch.check_session_interpreter(claude, unfiltered, str(tmp_path), guard)
    assert info.value.end_reason == "unsafe_path"
    assert "kind 'scribe'" in info.value.message


def test_a_notes_command_interpreter_under_the_guard_gives_no_notes(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    other = planted(tmp_path, "cs-test-node")
    notes = executable(tmp_path / "safe" / "notes-cmd")
    notes.write_text("#!/usr/bin/env cs-test-node\n")
    guard = launch.tool_guard(cfg, process(tmp_path), git_cwd=None)
    session = envbuild.SessionEnv(env={"PATH": f"{other}:/usr/bin"}, auth_names=(), secret_names=())
    tool = pathrules.ResolvedTool(path=str(notes), name="notes-cmd")
    found, problem = launch.check_notes_interpreter(tool, session, guard)
    assert found is None
    assert problem is not None and problem.startswith("notes command refused")


def test_a_notes_command_planted_in_another_kinds_write_dir_is_refused(tmp_path: Path) -> None:
    other = planted(tmp_path, "notes-cmd")
    cfg = load(tmp_path, CONFIG + "notes_context_cmd: [notes-cmd]\n")
    proc = entries(tmp_path, other, safe_tools(tmp_path))
    guard = launch.tool_guard(cfg, proc, git_cwd=None)
    found, problem = launch.resolve_notes(cfg, resolution(cfg, "implement"), proc, guard)
    assert found is None
    assert problem is not None and "kind 'scribe'" in problem


# Hooks: sh and PATH


def test_a_hook_path_excludes_the_guard_and_sh_there_is_never_found(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    other = planted(tmp_path, "sh")
    proc = process(tmp_path)
    guard = toolhooks.hook_guard(cfg, proc.environ, proc.state_root)
    env = toolhooks.hook_env(
        {**proc.environ, "PATH": f"{other}:/usr/bin:/bin"},
        secret_env=(),
        hook=toolhooks.PRE_LAUNCH,
        csession_id="cs-x",
        guard=guard,
    )
    assert str(other) not in env["PATH"].split(":")
    assert toolhooks.resolve_sh({"PATH": str(other)}, guard) is None
    sh = toolhooks.resolve_sh(env, guard)
    assert sh is not None and not str(sh).startswith(str(tmp_path))


def test_a_hook_sh_symlinked_into_the_guard_is_refused(tmp_path: Path) -> None:
    cfg = load(tmp_path, CONFIG)
    target = planted(tmp_path, "sh") / "sh"
    links = tmp_path / "links"
    links.mkdir()
    (links / "sh").symlink_to(target)
    proc = process(tmp_path)
    guard = toolhooks.hook_guard(cfg, proc.environ, proc.state_root)
    assert toolhooks.resolve_sh({"PATH": str(links)}, guard) is None


def test_a_hook_guard_that_is_not_known_finds_no_sh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load(tmp_path, CONFIG)
    monkeypatch.setattr(launch, "kind_profile", lambda *_a, **_k: None)
    proc = process(tmp_path)
    guard = toolhooks.hook_guard(cfg, proc.environ, proc.state_root)
    assert toolhooks.resolve_sh({"PATH": "/usr/bin:/bin"}, guard) is None


# The recorded session-writable paths of isolated runs


def record_run(state: Path, sid: str, paths: list[str]) -> None:
    """A run row holding only what ``store.recorded_writable`` reads."""
    db = sqlite3.connect(state / "state.db")
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS runs (csession_id TEXT, run_no INTEGER, writable_json TEXT)"
        )
        db.execute("INSERT INTO runs VALUES (?, 1, ?)", (sid, json.dumps(paths)))
        db.execute("INSERT INTO runs VALUES (?, 2, NULL)", (sid,))
        db.commit()
    finally:
        db.close()


def test_a_recorded_isolated_runs_paths_are_in_the_guard(tmp_path: Path) -> None:
    cfg = load(tmp_path)
    caller_cwd = tmp_path / "respond-cwd"  # the cwd of a respond run: no kind config shows it
    git = executable(caller_cwd / "bin" / "git").parent
    proc = entries(tmp_path, git, safe_tools(tmp_path))
    assert launch.tool_guard(cfg, proc, git_cwd=None).owner(str(git)) is None
    record_run(tmp_path / "state", "cs-respond1", [str(caller_cwd)])
    guard = launch.tool_guard(cfg, proc, git_cwd=None)
    with pytest.raises(LaunchValidationError) as info:
        launch.check_tools(
            cfg, resolution(cfg, "implement"), proc, launch.NO_SANDBOX, None, guard=guard
        )
    assert info.value.end_reason == "unsafe_path"
    assert guard.owner(str(git)) == f"{str(caller_cwd)!r} (a write path of run cs-respond1 #1)"


def test_a_store_that_cannot_be_read_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir(mode=0o700)
    (tmp_path / "state" / "state.db").write_bytes(b"not a database")
    with pytest.raises(LaunchValidationError) as info:
        launch.tool_guard(load(tmp_path), process(tmp_path), git_cwd=None)
    assert info.value.end_reason == "unsafe_path"


def test_no_store_has_no_recorded_paths(tmp_path: Path) -> None:
    assert launch.recorded_sources(process(tmp_path)) == []
