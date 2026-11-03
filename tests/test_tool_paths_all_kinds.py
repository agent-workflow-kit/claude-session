"""Tools a launch or a probe runs (bwrap, socat, git, claude, the notes command) must lie
outside the session-writable paths of every kind, not only the run's own
(``launch.other_kinds_writable``; interface.md, Launch order, step 5; design.md, PATH
sanitizing)."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_session import config, launch, pathrules
from claude_session.errors import LaunchValidationError

from .test_launch import executable, load, process, resolution

# `scribe` is a trusted kind whose runs may write ~/td; the run under test is another kind.
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
