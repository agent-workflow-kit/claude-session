"""Path rules (interface.md, Isolation: Session-writable paths and Path rules)."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from claude_session import config, defaults, kinds, pathrules, permission_profiles
from claude_session.errors import (
    InvalidPermissionProfile,
    LaunchValidationError,
    UsageError,
)
from claude_session.pathrules import Host, PathRules

SID = "cs-abcd2345"
OTHER = "cs-zzzz2345"


@dataclass(frozen=True)
class World:
    root: Path
    home: Path
    state: Path
    csdir: Path
    work: Path
    tmp_parent: Path
    tmpdir: Path
    repo: Path
    bin: Path
    xdg: Path

    def host(self, **kw: Any) -> Host:
        kw.setdefault("home", str(self.home))
        kw.setdefault("state_root", str(self.state))
        kw.setdefault("tmp_parent", str(self.tmp_parent))
        kw.setdefault("path_dirs", (str(self.bin), "/usr/bin"))
        kw.setdefault("xdg_dirs", (str(self.xdg),))
        return Host(**kw)

    def rules(self, *, cwd: Path | None = None, caller: Path | None = None, **kw: Any) -> PathRules:
        host = kw.pop("host", None) or self.host()
        return PathRules(
            host,
            session_dir=str(self.csdir),
            cwd=str(self.work if cwd is None else cwd),
            caller_cwd=str(self.repo if caller is None else caller),
            tmpdir=kw.pop("tmpdir", str(self.tmpdir)),
        )

    def writable(self, *, cwd: Path | None = None, **kw: Any) -> pathrules.SessionWritable:
        return pathrules.session_writable(
            session_dir=str(self.csdir),
            cwd=str(self.work if cwd is None else cwd),
            home=str(self.home),
            tmpdir=str(self.tmpdir),
            **kw,
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    root = tmp_path.resolve()
    home = root / "home" / "user"
    state = home / ".local" / "state" / "claude-session"
    csdir = state / "sessions" / SID
    for sub in ("work", "outbox", "artifacts", "inputs"):
        (csdir / sub).mkdir(parents=True)
    (state / "sessions" / OTHER / "work").mkdir(parents=True)
    (state / "private" / SID).mkdir(parents=True)
    for name in (".ssh", ".aws", ".config", ".claude", ".cache", ".nvm", ".local/share/uv"):
        (home / name).mkdir(parents=True)
    (home / ".netrc").write_text("")
    tmp_parent = root / "tmp" / "csession-1000"
    tmpdir = tmp_parent / SID
    tmpdir.mkdir(parents=True)
    (tmp_parent / OTHER).mkdir()
    repo = root / "work" / "repo"
    repo.mkdir(parents=True)
    bin_dir = root / "opt" / "bin"
    bin_dir.mkdir(parents=True)
    xdg = root / "xdg-config"
    xdg.mkdir()
    return World(
        root=root,
        home=home,
        state=state,
        csdir=csdir,
        work=csdir / "work",
        tmp_parent=tmp_parent,
        tmpdir=tmpdir,
        repo=repo,
        bin=bin_dir,
        xdg=xdg,
    )


# Session-writable paths


def test_session_writable_paths_definition(world: World) -> None:
    profile = {
        "permissions": {
            "allow": [
                f"Edit(/{world.root}/edited/**)",
                f"MultiEdit(/{world.root}/multi/**)",
                f"Read(/{world.root}/readonly/**)",
                "Bash(make:*)",
            ]
        },
        "sandbox": {"filesystem": {"allowWrite": [str(world.root / "sandbox-write")]}},
    }
    writable = world.writable(cwd=world.repo, write_dirs=[str(world.root / "wd")], profile=profile)
    expected = {
        world.repo,
        world.csdir,
        world.csdir / "work",
        world.csdir / "outbox",
        world.csdir / "artifacts",
        world.root / "wd",
        world.tmpdir,
        world.root / "sandbox-write",
        world.root / "edited",
        world.root / "multi",
    }
    assert set(writable.paths) == {str(p) for p in expected}


def test_session_writable_predicate(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    assert writable(str(world.repo))
    assert writable(str(world.repo / "missing" / "deeper" / "file"))  # nearest parent
    assert writable(str(world.csdir / "inputs" / "x"))  # $CSESSION_DIR is an --add-dir
    assert not writable(str(world.repo.parent))
    assert not writable(str(world.repo / ".." / "other"))  # `..` resolved, not prefixed
    assert not writable(str(world.root / "work" / "repo-sibling"))  # no string prefix match
    assert writable("/x\x00y")  # unresolvable: fail closed


def test_session_writable_follows_symlinks(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    link = world.root / "link-into-repo"
    link.symlink_to(world.repo)
    assert writable(str(link / "file"))
    escape = world.repo / "escape"
    escape.symlink_to(world.root / "opt")
    assert not writable(str(escape / "bin"))  # resolves outside: the target decides


def test_session_writable_overlaps(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    assert writable.overlaps(str(world.root / "work"))
    assert writable.overlaps(str(world.repo))
    assert not writable.overlaps(str(world.bin))
    link = world.root / "work-link"
    link.symlink_to(world.root / "work")
    assert writable.overlaps(str(link))


def fake_bind_mount(monkeypatch: pytest.MonkeyPatch, alias: Path, target: Path) -> None:
    """Make ``alias`` report the identity of ``target`` (what a bind mount looks like)."""
    real_stat = os.stat

    def stat(path: Any, *args: Any, **kw: Any) -> os.stat_result:
        if os.fspath(path) == str(alias):
            return real_stat(target)
        return real_stat(path, *args, **kw)

    monkeypatch.setattr(pathrules.os, "stat", stat)


def test_bind_mount_of_a_writable_directory_is_writable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = world.root / "mnt" / "repo-alias"
    alias.mkdir(parents=True)
    fake_bind_mount(monkeypatch, alias, world.repo)
    writable = world.writable(cwd=world.repo)
    assert writable(str(alias / "bin"))
    assert writable.overlaps(str(alias))
    assert not writable.overlaps(str(world.root / "mnt" / "elsewhere"))


def test_bind_mount_of_a_credential_directory_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = world.root / "mnt" / "keys"
    alias.mkdir(parents=True)
    fake_bind_mount(monkeypatch, alias, world.home / ".ssh")
    rules = world.rules()
    assert "credential" in (rules.read_entry_problem(str(alias)) or "")
    assert "$HOME/.<name>" in (rules.write_target_problem(str(alias / "x")) or "")


# Permission rule targets


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("Edit(//abs/dir/**)", ("Edit", "/abs/dir")),
        ("Write(//abs/file.txt)", ("Write", "/abs/file.txt")),
        ("Edit(//abs/pre*)", ("Edit", "/abs")),
        ("Edit(//abs/a/../..)", ("Edit", "/")),
        ("Edit(//**)", ("Edit", "/")),
        ("Edit", ("Edit", "/")),
        ("Edit()", ("Edit", "/")),
        ("Edit(~/notes/**)", ("Edit", "/h/notes")),
        ("Edit(~)", ("Edit", "/h")),
        ("Edit(~/*)", ("Edit", "/h")),
        ("Edit(src/**)", ("Edit", "/c/src")),
        ("Edit(./src)", ("Edit", "/c/src")),
        ("Edit(*.py)", ("Edit", "/c")),
        ("Edit(../../etc/**)", ("Edit", "/etc")),
        ("Read(//r/**)", ("Read", "/r")),
        ("Edit(//a/[b]/c)", ("Edit", "/a")),
        ("Edit(//a/b\\*c)", ("Edit", "/a")),
        ("Bash(rm -rf //)", None),
        ("WebFetch", None),
        ("mcp__x__y", None),
        ("not a rule", None),
    ],
)
def test_rule_target(rule: str, expected: tuple[str, str] | None) -> None:
    assert pathrules.rule_target(rule, cwd="/c", home="/h") == expected


def test_settings_relative_rule_is_refused_everywhere(world: World) -> None:
    rules = world.rules()
    content = {"permissions": {"allow": ["Read(/secrets/**)"]}}
    with pytest.raises(InvalidPermissionProfile) as info:
        rules.check_profile(content, isolated=False)
    assert "settings file" in (info.value.detail or "")


# Unsafe write targets


def write_problem(world: World, path: Path | str, **kw: Any) -> str | None:
    return world.rules(**kw).write_target_problem(str(path))


@pytest.mark.parametrize(
    ("target", "fragment"),
    [
        ("/", "ancestor of $HOME"),
        ("{home}", "ancestor of $HOME"),
        ("{home}/..", "ancestor of $HOME"),
        ("{bin}", "directory on PATH"),
        ("{bin}/sub", "directory on PATH"),
        ("/usr/bin", "directory on PATH"),
        ("{xdg}", "XDG directory"),
        ("{xdg}/app", "XDG directory"),
        ("{root}", "ancestor of $HOME"),
        ("{state}", "state root"),
        ("{state}/sessions/" + OTHER + "/work", "state root"),
        ("{state}/private", "private directory"),
        ("{csdir}/inputs", "state root"),
        ("{csdir}", "state root"),
        ("{tmp_parent}", "csession-1000"),
        ("{tmp_parent}/" + OTHER, "csession-1000"),
        ("{tmp_parent}/..", "csession-1000"),
        ("{home}/.claude", "~/.claude"),
        ("{home}/.claude/projects", "~/.claude"),
        ("{home}/.cache/build", "$HOME/.<name>"),
        ("{home}/.ssh", "$HOME/.<name>"),
        ("{home}/.newdir/sub", "$HOME/.<name>"),
        ("{home}/.netrc", "$HOME/.<name>"),
    ],
)
def test_unsafe_write_targets(world: World, target: str, fragment: str) -> None:
    path = target.format(**{k: str(v) for k, v in vars(world).items()})
    problem = write_problem(world, path)
    assert problem is not None and fragment in problem


@pytest.mark.parametrize(
    "target",
    [
        "{work}",
        "{work}/sub/new",
        "{csdir}/outbox",
        "{csdir}/artifacts/x",
        "{repo}",
        "{repo}/sub",
        "{tmpdir}",
        "{tmpdir}/x",
        "{home}/src/project",
        "{home}/visible.file",
    ],
)
def test_safe_write_targets(world: World, target: str) -> None:
    path = target.format(**{k: str(v) for k, v in vars(world).items()})
    assert write_problem(world, path) is None


def test_write_target_through_a_symlink_is_judged_by_its_realpath(world: World) -> None:
    link = world.root / "innocent"
    link.symlink_to(world.home / ".ssh")
    assert "$HOME/.<name>" in (write_problem(world, link) or "")
    to_work = world.root / "to-work"
    to_work.symlink_to(world.work)
    assert write_problem(world, to_work) is None


def test_symlinked_hidden_home_directory_protects_its_target(world: World) -> None:
    data = world.root / "data" / "config"
    data.mkdir(parents=True)
    (world.home / ".dotconfig").symlink_to(data)
    assert write_problem(world, data / "nvim") is not None
    assert write_problem(world, data.parent) is not None  # contains it


def test_exception_does_not_widen_through_case(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pathrules, "CASE_INSENSITIVE", True)
    upper = str(world.bin).upper()
    assert "directory on PATH" in (write_problem(world, upper) or "")
    variant = str(world.work).replace("/work", "/WORK")
    assert write_problem(world, variant) is not None  # no exception for a case variant


def test_case_sensitive_platform_compares_exactly(world: World) -> None:
    assert pathrules.CASE_INSENSITIVE is (sys.platform == "darwin")
    if not pathrules.CASE_INSENSITIVE:
        assert write_problem(world, str(world.bin).upper()) is None


def test_cwd_under_state_root_of_another_session_is_refused(world: World) -> None:
    other = world.state / "sessions" / OTHER / "work"
    with pytest.raises(LaunchValidationError) as info:
        world.rules(cwd=other).check_cwd()
    assert info.value.end_reason == "unsafe_cwd"


@pytest.mark.parametrize("cwd", ["{home}", "{home}/.cache/x", "/", "{tmp_parent}"])
def test_unsafe_cwd(world: World, cwd: str) -> None:
    path = Path(cwd.format(**{k: str(v) for k, v in vars(world).items()}))
    with pytest.raises(LaunchValidationError) as info:
        world.rules(cwd=path).check_cwd()
    assert info.value.end_reason == "unsafe_cwd"


@pytest.mark.parametrize("kind", sorted(defaults.BUILTIN_KINDS))
def test_builtin_default_cwds_pass(world: World, kind: str) -> None:
    cwd = world.work if defaults.BUILTIN_KINDS[kind]["cwd"] == "session-dir" else world.repo
    world.rules(cwd=cwd).check_cwd()


def test_home_inside_the_state_root_is_refused(world: World) -> None:
    home = world.state / "home"
    home.mkdir()
    rules = world.rules(host=world.host(home=str(home)))
    with pytest.raises(LaunchValidationError) as info:
        rules.check_host()
    assert info.value.end_reason == "unsafe_path"
    world.rules().check_host()


def test_write_dirs(world: World) -> None:
    rules = world.rules()
    rules.check_write_dirs([str(world.repo / "out"), str(world.work)], world.writable())
    with pytest.raises(InvalidPermissionProfile, match="write_dirs"):
        rules.check_write_dirs([str(world.home / ".config" / "app")], world.writable())


# Read entries


@pytest.mark.parametrize(
    ("target", "fragment"),
    [
        ("/", "ancestor of $HOME"),
        ("{home}", "ancestor of $HOME"),
        ("{home}/..", "ancestor of $HOME"),
        ("{home}/.ssh", "~/.ssh"),
        ("{home}/.aws/sub", "~/.aws"),
        ("{home}/.local", "~/.local/state"),
        ("{home}/.netrc", "~/.netrc"),
        ("{home}/.git-credentials", "~/.git-credentials"),
        ("{home}/.config/gh", "~/.config"),
        ("{home}/.kube", "~/.kube"),
        ("{state}/sessions/" + OTHER, "~/.local/state"),
        ("{tmp_parent}/" + OTHER, "csession-1000"),
        ("{tmpdir}/x", "private tmpdir"),
        ("{root}/tmp", "csession-1000"),
    ],
)
def test_unsafe_read_entries(world: World, target: str, fragment: str) -> None:
    path = target.format(**{k: str(v) for k, v in vars(world).items()})
    problem = world.rules().read_entry_problem(path)
    assert problem is not None and fragment in problem


@pytest.mark.parametrize(
    "target",
    ["{home}/.nvm", "{home}/.local/share/uv", "{csdir}/inputs", "{bin}", "{repo}/sub", "/usr"],
)
def test_safe_read_entries(world: World, target: str) -> None:
    path = target.format(**{k: str(v) for k, v in vars(world).items()})
    assert world.rules().read_entry_problem(path) is None


def test_state_root_outside_home_is_refused_as_a_read_entry(world: World) -> None:
    state = world.root / "srv" / "state"
    (state / "sessions" / SID).mkdir(parents=True)
    host = world.host(state_root=str(state))
    rules = PathRules(
        host,
        session_dir=str(state / "sessions" / SID),
        cwd=str(state / "sessions" / SID / "work"),
        caller_cwd=str(world.repo),
    )
    assert "state root" in (rules.read_entry_problem(str(state / "sessions" / OTHER)) or "")
    assert "state root" in (rules.read_entry_problem(str(world.root / "srv")) or "")
    assert rules.read_entry_problem(str(state / "sessions" / SID / "inputs")) is None


def test_credential_path_reached_through_a_symlink(world: World) -> None:
    secure = world.root / "secure"
    real_ssh = secure / "ssh"
    real_ssh.mkdir(parents=True)
    os.rmdir(world.home / ".ssh")
    (world.home / ".ssh").symlink_to(real_ssh)
    rules = world.rules()
    assert "~/.ssh" in (rules.read_entry_problem(str(secure)) or "")
    assert "~/.ssh" in (rules.read_entry_problem(str(real_ssh / "keys")) or "")
    link = world.root / "plain-name"
    link.symlink_to(world.home / ".aws")
    assert "~/.aws" in (rules.read_entry_problem(str(link)) or "")


def test_read_dirs_raise_invalid_permission_profile(world: World) -> None:
    with pytest.raises(InvalidPermissionProfile) as info:
        world.rules().check_read_dirs([str(world.home)], world.writable())
    assert info.value.end_reason == "invalid_permission_profile"


def test_generated_entries_refuse_the_callers_cwd(world: World) -> None:
    rules = world.rules()  # run cwd work/, caller cwd repo
    with pytest.raises(LaunchValidationError) as info:
        rules.check_generated({"Python read directory": [str(world.repo)]})
    assert info.value.end_reason == "unsafe_read_dir"
    assert "python -m claude_session" in info.value.message
    with pytest.raises(LaunchValidationError):
        rules.check_generated({"Python read directory": [str(world.repo.parent)]})
    rules.check_generated({"Python read directory": [str(world.repo / ".venv" / "lib")]})


def test_generated_entries_may_name_the_callers_cwd_when_it_is_the_runs(world: World) -> None:
    world.rules(cwd=world.repo).check_generated({"git directory": [str(world.repo)]})


@pytest.mark.parametrize("entry", ["/", "{home}", "{home}/.claude/local", "{state}"])
def test_generated_entries_refusals(world: World, entry: str) -> None:
    path = entry.format(**{k: str(v) for k, v in vars(world).items()})
    with pytest.raises(LaunchValidationError) as info:
        world.rules().check_generated({"tool directory": [path]})
    assert info.value.end_reason == "unsafe_read_dir"


# Plugin directories


def test_plugin_dirs(world: World) -> None:
    plugin = world.root / "plugins" / "kit"
    plugin.mkdir(parents=True)
    rules = world.rules(cwd=world.repo)
    writable = world.writable(cwd=world.repo)
    rules.check_plugin_dirs([str(plugin)], writable)
    cases = {
        str(world.repo / "plugin"): "plugin_dir_missing",
        str(world.root / "plugins" / "gone"): "plugin_dir_missing",
    }
    (world.repo / "inner").mkdir()
    cases[str(world.repo / "inner")] = "unsafe_plugin_dir"  # inside the writable cwd
    cases[str(world.root / "work")] = "unsafe_plugin_dir"  # contains the writable cwd
    cases[str(world.home / ".claude")] = "unsafe_plugin_dir"  # credential path
    for path, reason in cases.items():
        with pytest.raises(LaunchValidationError) as info:
            rules.check_plugin_dirs([path], writable)
        assert info.value.end_reason == reason, path


# Profiles


def test_profile_allow_read_of_a_credential_path_is_refused(world: World) -> None:
    content = permission_profiles.expand(
        {"sandbox": {"filesystem": {"allowRead": ["${HOME}/.ssh"]}}},
        {"HOME": str(world.home)},
    )
    with pytest.raises(InvalidPermissionProfile, match="unsafe read path"):
        world.rules().check_profile(content, isolated=True, writable=world.writable())
    world.rules().check_profile(content, isolated=False)  # trusted: refused paths only


@pytest.mark.parametrize(
    "content",
    [
        {"sandbox": {"filesystem": {"allowWrite": ["{home}"]}}},
        {"sandbox": {"filesystem": {"allowWrite": ["{bin}"]}}},
        {"permissions": {"allow": ["Edit(/{home}/.bashrc)"]}},
        {"permissions": {"allow": ["Write(/{root}/**)"]}},
        {"permissions": {"allow": ["Edit"]}},
        {"permissions": {"allow": ["NotebookEdit(~/.config/x)"]}},
    ],
)
def test_isolated_profile_write_additions(world: World, content: dict[str, Any]) -> None:
    filled = fill(content, world)
    with pytest.raises(InvalidPermissionProfile, match="unsafe path writable"):
        world.rules().check_profile(filled, isolated=True, writable=world.writable())


@pytest.mark.parametrize(
    "content",
    [
        {"permissions": {"allow": ["Read(/{state}/private/**)"]}},
        {"permissions": {"ask": ["Edit(/{state}/sessions/" + OTHER + "/**)"]}},
        {"permissions": {"additionalDirectories": ["{state}"]}},
        {"sandbox": {"filesystem": {"allowRead": ["{tmp_parent}/" + OTHER]}}},
        {"sandbox": {"filesystem": {"allowWrite": ["{state}/sessions"]}}},
    ],
)
def test_refused_paths_apply_to_trusted_runs(world: World, content: dict[str, Any]) -> None:
    with pytest.raises(InvalidPermissionProfile, match="refused path"):
        world.rules().check_profile(fill(content, world), isolated=False)


def test_profile_paths_inside_the_own_session_and_tmpdir_pass(world: World) -> None:
    content = fill(
        {
            "permissions": {
                "allow": ["Read(/{csdir}/inputs/**)", "Edit(/{csdir}/artifacts/**)"],
                "deny": ["Read(/{state}/**)"],
            },
            "sandbox": {"filesystem": {"allowWrite": ["{tmpdir}"], "denyRead": ["{state}"]}},
        },
        world,
    )
    world.rules().check_profile(content, isolated=True, writable=world.writable())
    world.rules().check_profile(content, isolated=False)


def test_builtin_respond_profile_passes(world: World) -> None:
    cfg = config.load(None, env={"XDG_CONFIG_HOME": str(world.root / "noconfig")})
    resolution = kinds.resolve(cfg, "respond", mode="headless", origin_url=lambda _: None)
    selected = permission_profiles.select(cfg, resolution, session_writable=lambda _: False)
    variables = {"CSESSION_DIR": str(world.csdir), "CWD": str(world.repo), "HOME": str(world.home)}
    content = permission_profiles.expand(selected.content, {**variables, "GIT_DIR": None})
    rules = world.rules(cwd=world.repo)
    writable = world.writable(cwd=world.repo, profile=content)
    rules.check_profile(content, isolated=True, writable=writable)
    assert str(world.repo) in writable.paths


def fill(node: Any, world: World) -> Any:
    values = {k: str(v) for k, v in vars(world).items()}
    if isinstance(node, dict):
        return {key: fill(value, world) for key, value in node.items()}
    if isinstance(node, list):
        return [fill(value, world) for value in node]
    return node.format(**values) if isinstance(node, str) else node


# read_dirs and write_dirs


def test_resolve_dirs(world: World) -> None:
    link = world.root / "tools-link"
    link.symlink_to(world.home / ".nvm")
    variables = {"CSESSION_DIR": str(world.csdir), "HOME": str(world.home)}
    resolved = pathrules.resolve_dirs(
        ["~/.nvm", "~", "${CSESSION_DIR}/work", str(link), "~/.nvm"],
        variables,
        home=str(world.home),
        key="read_dirs",
    )
    assert resolved == [str(world.home / ".nvm"), str(world.home), str(world.work)]


@pytest.mark.parametrize("entry", ["~other/x", "relative/dir", "${GIT_DIR}/x", "${NOPE}"])
def test_resolve_dirs_refusals(world: World, entry: str) -> None:
    with pytest.raises(InvalidPermissionProfile) as info:
        pathrules.resolve_dirs(
            [entry], {"HOME": str(world.home), "GIT_DIR": None}, home=str(world.home), key="w"
        )
    assert info.value.end_reason == "invalid_permission_profile"


def test_resolve_write_dirs(world: World) -> None:
    missing_outside = world.root / "missing"
    new_inside = world.work / "new"
    result = pathrules.resolve_write_dirs(
        [str(world.repo), str(new_inside), str(missing_outside), str(world.csdir / "outbox")],
        session_dir=str(world.csdir),
    )
    assert result.dirs == (str(world.repo), str(new_inside), str(world.csdir / "outbox"))
    assert result.create == (str(new_inside),)
    assert len(result.warnings) == 1 and str(missing_outside) in result.warnings[0]
    with pytest.raises(InvalidPermissionProfile, match="write_dirs"):
        pathrules.resolve_write_dirs([str(world.csdir / "inputs")], session_dir=str(world.csdir))


# PATH and tools


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/a:/b", ["/a", "/b"]),
        ("::/a::", ["/a"]),
        (".:bin:/a:./x:../y:~/z", ["/a"]),
        ("", []),
    ],
)
def test_sanitize_path(value: str, expected: list[str]) -> None:
    assert pathrules.sanitize_path(value) == expected


def test_path_entry_inside_a_writable_path_fails(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    pathrules.check_path_entries([str(world.bin), "/usr/bin"], writable)
    with pytest.raises(LaunchValidationError) as info:
        pathrules.check_path_entries(
            [str(world.bin), str(world.repo / "node_modules/.bin")], writable
        )
    assert info.value.end_reason == "unsafe_path"
    link = world.bin.parent / "bin-link"
    link.symlink_to(world.repo)
    with pytest.raises(LaunchValidationError):
        pathrules.check_path_entries([str(link)], writable)  # resolved before comparing


def executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_resolve_tool(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    tool = executable(world.bin / "tool")
    (world.root / "opt" / "first").mkdir()
    (world.root / "opt" / "first" / "tool").write_text("not executable")
    entries = [str(world.root / "opt" / "first"), str(world.bin)]
    assert pathrules.resolve_tool("tool", entries, writable) == (str(tool), "tool")
    assert pathrules.resolve_tool("absent", entries, writable) is None
    assert pathrules.resolve_tool(str(tool), [], writable) == (str(tool), "tool")


def test_resolve_tool_refuses_session_writable_binaries(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    planted = executable(world.repo / "claude")
    with pytest.raises(LaunchValidationError) as info:
        pathrules.resolve_tool(str(planted), [], writable)
    assert info.value.end_reason == "unsafe_path"
    (world.bin / "claude").symlink_to(planted)  # a safe directory, a writable target
    with pytest.raises(LaunchValidationError):
        pathrules.resolve_tool("claude", [str(world.bin)], writable)
    with pytest.raises(LaunchValidationError, match="bare name or an absolute path"):
        pathrules.resolve_tool("./claude", [str(world.bin)], writable)
    with pytest.raises(LaunchValidationError):
        pathrules.resolve_tool("", [str(world.bin)], writable)


# Other checks


@pytest.mark.parametrize(
    "args",
    [
        ["--dangerously-skip-permissions"],
        ["--debug=api"],
        ["--debug", "api"],
        ["--verbose", "--add-dir", "/"],
        ["--VERBOSE"],
        ["\t--verbose"],
        ["--append-system-prompt=never-echoed-value"],
    ],
)
def test_isolated_claude_args_allowlist(args: list[str]) -> None:
    with pytest.raises(LaunchValidationError) as info:
        pathrules.check_claude_args(args, isolated=True)
    assert info.value.end_reason == "unsafe_claude_args"
    refused = [a for a in args if a not in pathrules.ISOLATED_CLAUDE_ARGS]
    assert all(arg not in info.value.message for arg in refused)  # values are not echoed
    pathrules.check_claude_args(args, isolated=False)


def test_isolated_claude_args_accept_verbose_and_debug() -> None:
    pathrules.check_claude_args(["--verbose", "--debug", "--verbose"], isolated=True)
    pathrules.check_claude_args([], isolated=True)


def test_outcome_file_under_a_session_writable_path(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    pathrules.check_outcome_file(str(world.root / "out.json"), writable)
    for path in (world.repo / "out.json", world.repo / "new" / "dir" / "out.json"):
        with pytest.raises(UsageError) as info:
            pathrules.check_outcome_file(str(path), writable)
        assert info.value.exit_code == 2
    link = world.root / "via-link"
    link.symlink_to(world.csdir / "artifacts")
    with pytest.raises(UsageError):
        pathrules.check_outcome_file(str(link / "out.json"), writable)


# Host facts


def test_host_from_environ() -> None:
    environ = {
        "HOME": "/home/u",
        "PATH": "/usr/bin::.:bin:/opt/x",
        "XDG_CONFIG_HOME": "/cfg",
        "XDG_DATA_HOME": "relative",
        "XDG_STATE_HOME": "",
    }
    host = Host.from_environ(environ, state_root="/s", uid=42, path_prepend=["/pre"])
    assert host.path_dirs == ("/pre", "/usr/bin", "/opt/x")
    assert host.xdg_dirs == ("/cfg",)
    assert host.tmp_parent == "/tmp/csession-42"
    assert pathrules.tmp_parent(7) == "/tmp/csession-7"


@pytest.mark.parametrize("home", [None, "", "relative/home"])
def test_host_needs_an_absolute_home(home: str | None) -> None:
    environ = {} if home is None else {"HOME": home}
    with pytest.raises(LaunchValidationError) as info:
        Host.from_environ(environ, state_root="/s")
    assert info.value.end_reason == "unsafe_path"


# Lexical backstop


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("/", True),
        ("/home", True),
        ("/home/u", True),
        ("/home/u/.ssh", True),
        ("/home/u/.local", True),
        ("/home/u/.local/share/uv", False),
        ("/home/u/.local/state/cs/sessions/x/work", False),  # inside session_dir
        ("/home/u/.local/state/cs/sessions/y", True),
        ("/opt", False),
    ],
)
def test_lexical_read_problem(path: str, problem: bool) -> None:
    found = pathrules.lexical_read_problem(
        path, home="/home/u", session_dir="/home/u/.local/state/cs/sessions/x"
    )
    assert (found is not None) is problem


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The rules never read the process's own HOME: everything comes from Host.
    monkeypatch.setenv("HOME", "/nonexistent-home-for-tests")
    yield


def test_paths_with_glob_and_rule_characters_are_compared_literally(world: World) -> None:
    odd = world.root / "we*rd[1]{x}(y)"
    odd.mkdir()
    writable = world.writable(cwd=odd)
    assert writable(str(odd / "file"))
    assert not writable(str(world.root / "weXrd1"))
    assert world.rules(cwd=odd).write_target_problem(str(odd)) is None


def test_dot_alias_of_home_does_not_make_home_hidden(world: World) -> None:
    (world.home / ".home").symlink_to(world.home)
    (world.home / ".root").symlink_to("/")
    (world.home / "src" / "project").mkdir(parents=True)
    assert write_problem(world, world.home / "src" / "project") is None
    assert write_problem(world, world.repo) is None


# Review round 1 (#24)


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit", "Read"])
@pytest.mark.parametrize("pattern", ["*", ""])
def test_star_and_empty_rule_content_is_the_bare_tool(tool: str, pattern: str) -> None:
    assert pathrules.rule_target(f"{tool}({pattern})", cwd="/c", home="/h") == (tool, "/")


@pytest.mark.parametrize(
    "rule", ["Edit(*)", "Write(*)", "MultiEdit(*)", "NotebookEdit(*)", "Edit()"]
)
def test_star_write_rules_are_refused_for_isolated_runs(world: World, rule: str) -> None:
    content = {"permissions": {"allow": [rule]}}
    with pytest.raises(InvalidPermissionProfile, match="unsafe path writable"):
        world.rules().check_profile(content, isolated=True, writable=world.writable())


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("Edit(~//.ssh/**)", ("Edit", "/h/.ssh")),
        ("Edit(~//.bash_profile)", ("Edit", "/h/.bash_profile")),
        (
            "Read(~//.local/state/claude-session/private/**)",
            ("Read", "/h/.local/state/claude-session/private"),
        ),
        ("Edit(~///x)", ("Edit", "/h/x")),
    ],
)
def test_double_slash_after_tilde_stays_under_home(rule: str, expected: tuple[str, str]) -> None:
    assert pathrules.rule_target(rule, cwd="/c", home="/h") == expected


@pytest.mark.parametrize(
    "rule", ["Edit(~//.ssh/**)", "Edit(~//.bash_profile)", "Write(~//.config/x)"]
)
def test_double_slash_after_tilde_write_rules_are_refused(world: World, rule: str) -> None:
    with pytest.raises(InvalidPermissionProfile, match="unsafe path writable"):
        world.rules().check_profile(
            {"permissions": {"allow": [rule]}}, isolated=True, writable=world.writable()
        )


def test_double_slash_after_tilde_reaches_the_state_root(world: World) -> None:
    content = {"permissions": {"allow": ["Read(~//.local/state/claude-session/private/**)"]}}
    with pytest.raises(InvalidPermissionProfile, match="refused path"):
        world.rules().check_profile(content, isolated=False)


def test_double_slash_after_tilde_in_additional_directories(world: World) -> None:
    content = {"permissions": {"additionalDirectories": ["~//.local/state/claude-session"]}}
    with pytest.raises(InvalidPermissionProfile, match="refused path"):
        world.rules().check_profile(content, isolated=False)


def test_session_writable_with_double_slash_after_tilde(world: World) -> None:
    writable = world.writable(profile={"permissions": {"allow": ["Edit(~//.ssh/**)"]}})
    assert writable(str(world.home / ".ssh" / "authorized_keys"))


def test_resolve_dirs_double_slash_after_tilde(world: World) -> None:
    resolved = pathrules.resolve_dirs(
        ["~//.nvm"], {"HOME": str(world.home)}, home=str(world.home), key="read_dirs"
    )
    assert resolved == [str(world.home / ".nvm")]


def test_path_entry_through_a_two_hop_symlink_chain_in_the_cwd_fails(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    target = world.root / "opt" / "real-bin"
    target.mkdir()
    (world.repo / "hop").symlink_to(target)  # the agent can re-point this link
    first = world.root / "opt" / "first-link"
    first.symlink_to(world.repo / "hop")
    assert not writable(str(first))  # the realpath alone is outside the cwd
    with pytest.raises(LaunchValidationError) as info:
        pathrules.check_path_entries([str(first)], writable)
    assert info.value.end_reason == "unsafe_path"


def test_tool_through_a_two_hop_symlink_chain_in_the_cwd_fails(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    real = executable(world.root / "opt" / "real-tool")
    (world.repo / "hop").symlink_to(real)
    (world.bin / "tool").symlink_to(world.repo / "hop")
    with pytest.raises(LaunchValidationError) as info:
        pathrules.resolve_tool("tool", [str(world.bin)], writable)
    assert info.value.end_reason == "unsafe_path"


def test_resolve_tool_returns_the_realpath(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    real = executable(world.root / "opt" / "real-tool")
    (world.bin / "tool").symlink_to(real)
    resolved = pathrules.resolve_tool("tool", [str(world.bin)], writable)
    assert resolved is not None and resolved.path == str(real)


def test_writable_hop(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    assert pathrules.writable_hop(str(world.bin), writable) is None
    (world.repo / "hop").symlink_to(world.bin)
    link = world.root / "opt" / "via"
    link.symlink_to(world.repo / "hop")
    assert pathrules.writable_hop(str(link / "x"), writable) is not None
    assert pathrules.writable_hop(str(world.repo / "bin"), writable) is not None


def test_own_work_swapped_for_a_symlink_gets_no_exception(world: World) -> None:
    os.rmdir(world.work)
    world.work.symlink_to(world.home / ".ssh")
    rules = world.rules()
    with pytest.raises(LaunchValidationError) as info:
        rules.check_cwd()
    assert info.value.end_reason == "unsafe_cwd"
    assert rules.write_target_problem(str(world.home / ".ssh")) is not None


def test_private_tmpdir_swapped_for_a_symlink_gets_no_exception(world: World) -> None:
    os.rmdir(world.tmpdir)
    world.tmpdir.symlink_to(world.tmp_parent / OTHER)
    assert world.rules().write_target_problem(str(world.tmp_parent / OTHER)) is not None


def test_profile_sandbox_paths_are_mapped(world: World) -> None:
    (world.home / ".kube").mkdir()
    trusted = {"sandbox": {"filesystem": {"allowRead": ["~/.kube"]}}}
    world.rules().check_profile(trusted, isolated=False)
    isolated = {"sandbox": {"filesystem": {"allowRead": ["~/.ssh"]}}}
    with pytest.raises(InvalidPermissionProfile, match="unsafe read path"):
        world.rules().check_profile(isolated, isolated=True, writable=world.writable())
    state = {"sandbox": {"filesystem": {"allowWrite": ["~/.local/state/claude-session"]}}}
    with pytest.raises(InvalidPermissionProfile, match="refused path"):
        world.rules().check_profile(state, isolated=False)
    double = {"sandbox": {"filesystem": {"allowRead": [f"/{world.state}"]}}}
    with pytest.raises(InvalidPermissionProfile, match="refused path"):
        world.rules().check_profile(double, isolated=False)


@pytest.mark.parametrize("isolated", [True, False])
def test_relative_profile_sandbox_path_is_settings_relative(world: World, isolated: bool) -> None:
    content = {"sandbox": {"filesystem": {"allowRead": ["relative/dir"]}}}
    with pytest.raises(InvalidPermissionProfile) as info:
        world.rules().check_profile(content, isolated=isolated, writable=world.writable())
    assert "settings file" in (info.value.detail or "")


def test_session_writable_raises_for_a_settings_relative_rule(world: World) -> None:
    with pytest.raises(InvalidPermissionProfile):
        world.writable(profile={"permissions": {"allow": ["Edit(/x/**)"]}})
    with pytest.raises(InvalidPermissionProfile):
        world.writable(profile={"sandbox": {"filesystem": {"allowWrite": ["rel"]}}})


def test_session_writable_maps_tilde_sandbox_writes(world: World) -> None:
    writable = world.writable(profile={"sandbox": {"filesystem": {"allowWrite": ["~/out"]}}})
    assert str(world.home / "out") in writable.paths


def test_caller_cwd_rule_exempts_system_read_dirs(world: World) -> None:
    rules = world.rules(caller=Path("/usr/src/app"))
    rules.check_generated({"Python read directory": ["/usr"]})
    with pytest.raises(LaunchValidationError):
        world.rules(caller=world.repo / "x").check_generated({"tool directory": [str(world.repo)]})


def test_dot_entry_reached_through_a_home_alias_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = world.root / "mnt" / "home-alias"
    alias.mkdir(parents=True)
    fake_bind_mount(monkeypatch, alias, world.home)
    rules = world.rules()
    assert "$HOME/.<name>" in (rules.write_target_problem(str(alias / ".bash_profile")) or "")
    assert "$HOME/.<name>" in (rules.write_target_problem(str(alias / ".new" / "x")) or "")
    assert rules.write_target_problem(str(alias / "visible")) is None


def test_case_insensitive_key_is_unicode_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pathrules, "CASE_INSENSITIVE", True)
    assert pathrules._key("/h/.Café") == pathrules._key("/h/.café")


def test_user_entries_inside_a_session_writable_path_are_refused(world: World) -> None:
    rules = world.rules(cwd=world.repo)
    writable = world.writable(cwd=world.repo)
    (world.repo / "sub").mkdir()
    with pytest.raises(InvalidPermissionProfile, match="write_dirs"):
        rules.check_write_dirs([str(world.repo / "sub")], writable)
    with pytest.raises(InvalidPermissionProfile, match="read_dirs"):
        rules.check_read_dirs([str(world.repo / "x")], writable)
    content = {"sandbox": {"filesystem": {"allowRead": [str(world.repo / "tools")]}}}
    with pytest.raises(InvalidPermissionProfile, match="session-writable"):
        rules.check_profile(content, isolated=True, writable=writable)
    # Entries equal to a session-writable path or one of the run's own directories, or
    # elsewhere (also missing), stay allowed.
    rules.check_write_dirs([str(world.repo), str(world.csdir / "outbox")], writable)
    rules.check_read_dirs([str(world.csdir), str(world.root / "missing")], writable)


def test_read_dir_inside_a_write_dir_is_refused(world: World) -> None:
    out = world.root / "out"
    out.mkdir()
    writable = world.writable(write_dirs=[str(out)])
    with pytest.raises(InvalidPermissionProfile) as info:
        world.rules().check_read_dirs([str(out / "tools")], writable)
    assert "session-writable" in (info.value.detail or "")


def test_isolated_check_profile_needs_the_writable_paths(world: World) -> None:
    with pytest.raises(ValueError, match="writable"):
        world.rules().check_profile({}, isolated=True)


@pytest.mark.parametrize(
    "name",
    [
        ".cargo",
        ".cargo/credentials.toml",
        ".cargo/credentials",
        ".local/share/keyrings",
        ".gem/credentials",
        ".gradle/gradle.properties",
        ".m2",
        ".m2/settings-security.xml",
    ],
)
def test_toolchain_credential_files_are_refused_read_entries(world: World, name: str) -> None:
    problem = world.rules().read_entry_problem(str(world.home / name))
    assert problem is not None and "credential" in problem


def test_cargo_hint_names_narrower_directories(world: World) -> None:
    problem = world.rules().read_entry_problem(str(world.home / ".cargo"))
    assert problem is not None and "~/.cargo/bin" in problem
    assert world.rules().read_entry_problem(str(world.home / ".cargo" / "bin")) is None


def test_custom_xdg_dirs_are_credential_paths(world: World) -> None:
    host = Host.from_environ(
        {
            "HOME": str(world.home),
            "XDG_CONFIG_HOME": str(world.root / "cfg"),
            "XDG_STATE_HOME": str(world.root / "st"),
            "XDG_DATA_HOME": str(world.root / "data"),
        },
        state_root=str(world.state),
    )
    rules = world.rules(host=host)
    for path in (world.root / "cfg" / "gh", world.root / "st", world.root / "data" / "keyrings"):
        problem = rules.read_entry_problem(str(path))
        assert problem is not None and "credential" in problem, path
    assert rules.read_entry_problem(str(world.root / "data" / "fonts")) is None


def test_home_under_the_tmp_parent_is_refused(world: World) -> None:
    home = world.tmp_parent / "home"
    home.mkdir()
    with pytest.raises(LaunchValidationError) as info:
        world.rules(host=world.host(home=str(home))).check_host()
    assert info.value.end_reason == "unsafe_path"


def test_write_target_containing_an_xdg_dir_outside_home(world: World) -> None:
    xdg = world.root / "data" / "xdg"
    xdg.mkdir(parents=True)
    rules = world.rules(host=world.host(xdg_dirs=(str(xdg),)))
    assert "XDG directory" in (rules.write_target_problem(str(world.root / "data")) or "")


def test_symlinked_dot_entry_into_the_cwd_names_the_entry(world: World) -> None:
    config = world.repo / "config"
    config.mkdir()
    (world.home / ".tool").symlink_to(config)
    with pytest.raises(LaunchValidationError) as info:
        world.rules(cwd=world.repo).check_cwd()
    message = info.value.message
    assert "~/.tool" in message and str(config) in message
    assert "is or is inside a $HOME/.<name>" not in message


def test_credential_paths_of_the_account_home(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = world.root / "account"
    (account / ".ssh").mkdir(parents=True)

    class Entry:
        pw_dir = str(account)

    monkeypatch.setattr(pathrules.pwd, "getpwuid", lambda uid: Entry())
    host = Host.from_environ({"HOME": str(world.home)}, state_root=str(world.state))
    assert host.account_home == str(account)
    rules = world.rules(host=host)
    assert "credential" in (rules.read_entry_problem(str(account / ".ssh")) or "")
    assert rules.write_target_problem(str(account / ".bashrc")) is not None
    assert rules.write_target_problem(str(account / "src")) is None


def test_account_home_lookup_failure_is_ignored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(uid: int) -> Any:
        raise KeyError(uid)

    monkeypatch.setattr(pathrules.pwd, "getpwuid", fail)
    host = Host.from_environ({"HOME": str(world.home)}, state_root=str(world.state))
    assert host.account_home is None


@pytest.mark.parametrize("home", ["/", "//", "/."])
def test_home_root_is_refused(home: str) -> None:
    with pytest.raises(LaunchValidationError) as info:
        Host.from_environ({"HOME": home}, state_root="/s")
    assert info.value.end_reason == "unsafe_path"


# Round-2 verification findings


def _data_link(world: World) -> tuple[Path, Path]:
    """``<repo>/lnk -> <root>/data``: an entry through a symlink the agent can re-point."""
    data = world.root / "data"
    data.mkdir()
    link = world.repo / "lnk"
    link.symlink_to(data)
    return link, data


def test_profile_sandbox_entries_through_a_symlink_in_the_cwd_are_refused(world: World) -> None:
    link, _data = _data_link(world)
    rules = world.rules(cwd=world.repo)
    writable = world.writable(cwd=world.repo)
    for content in (
        {"sandbox": {"filesystem": {"allowRead": [str(link)]}}},
        {"sandbox": {"filesystem": {"allowWrite": [str(link)]}}},
        {"permissions": {"additionalDirectories": ["lnk"]}},
        {"permissions": {"additionalDirectories": [str(link)]}},
    ):
        with pytest.raises(InvalidPermissionProfile, match="session-writable"):
            rules.check_profile(content, isolated=True, writable=writable)


def test_read_and_write_dirs_through_a_symlink_in_the_cwd_are_refused(world: World) -> None:
    link, data = _data_link(world)
    rules = world.rules(cwd=world.repo)
    writable = world.writable(cwd=world.repo)
    with pytest.raises(InvalidPermissionProfile, match="read_dirs"):
        rules.check_read_dirs([str(link)], writable)
    with pytest.raises(InvalidPermissionProfile, match="write_dirs"):
        rules.check_write_dirs([str(link)], writable)
    with pytest.raises(InvalidPermissionProfile, match="read_dirs"):
        rules.check_read_dirs([str(link / "sub")], writable)
    # The resolved target itself, named directly, stays allowed.
    rules.check_read_dirs([str(data)], writable)
    rules.check_write_dirs([str(data)], writable)


def test_user_entry_through_a_symlink_into_the_cwd_is_refused(world: World) -> None:
    (world.repo / "tools").mkdir()
    via = world.root / "via"
    via.symlink_to(world.repo / "tools")  # a safe symlink to a path inside the cwd
    rules = world.rules(cwd=world.repo)
    with pytest.raises(InvalidPermissionProfile, match="read_dirs"):
        rules.check_read_dirs([str(via)], world.writable(cwd=world.repo))


@pytest.mark.parametrize(
    "entry",
    ["{csdir}/outbox/sub", "{csdir}/work/sub", "{csdir}/artifacts/x", "{tmpdir}/sub"],
)
def test_user_entries_below_the_runs_own_directories_are_refused(world: World, entry: str) -> None:
    path = entry.format(**{k: str(v) for k, v in vars(world).items()})
    rules = world.rules(cwd=world.repo)
    writable = world.writable(cwd=world.repo)
    with pytest.raises(InvalidPermissionProfile, match="write_dirs"):
        rules.check_write_dirs([path], writable)
    with pytest.raises(InvalidPermissionProfile, match="read_dirs"):
        rules.check_read_dirs([path], writable)


def test_user_entries_equal_to_the_runs_own_directories_pass(world: World) -> None:
    rules = world.rules(cwd=world.repo)
    writable = world.writable(cwd=world.repo)
    own = [str(world.csdir / name) for name in ("outbox", "artifacts", "work")]
    rules.check_write_dirs(own, writable)
    rules.check_read_dirs([str(world.csdir), *own], writable)
    content = {"sandbox": {"filesystem": {"allowWrite": [str(world.tmpdir)]}}}
    rules.check_profile(content, isolated=True, writable=writable)


def _deep_link(world: World, at: Path) -> Path:
    """``at -> <root>/data/a/b``: ``..`` after it climbs the target, not ``at``'s parent."""
    deep = world.root / "data" / "a" / "b"
    deep.mkdir(parents=True)
    (world.root / "data" / "c").mkdir()
    at.symlink_to(deep)
    return at


def test_profile_entries_with_dot_dot_after_a_symlink_in_the_cwd_are_refused(
    world: World,
) -> None:
    link = _deep_link(world, world.repo / "lnk")
    rules = world.rules(cwd=world.repo)
    writable = world.writable(cwd=world.repo)
    for content in (
        {"sandbox": {"filesystem": {"allowWrite": [f"{link}/.."]}}},
        {"sandbox": {"filesystem": {"allowRead": [f"{link}/../../c"]}}},
    ):
        with pytest.raises(InvalidPermissionProfile) as info:
            rules.check_profile(content, isolated=True, writable=writable)
        assert info.value.detail is not None and "must not contain '..'" in info.value.detail


def test_write_dirs_with_dot_dot_after_a_symlink_in_outbox_are_refused(world: World) -> None:
    link = _deep_link(world, world.csdir / "outbox" / "l")
    rules = world.rules(cwd=world.repo)
    with pytest.raises(InvalidPermissionProfile, match="write_dirs") as info:
        rules.check_write_dirs([f"{link}/../.."], world.writable(cwd=world.repo))
    assert info.value.detail is not None and "must not contain '..'" in info.value.detail


def test_user_entries_with_dot_dot_are_refused_without_symlinks(world: World) -> None:
    rules = world.rules(cwd=world.repo)
    writable = world.writable(cwd=world.repo)
    (world.root / "data").mkdir()
    with pytest.raises(InvalidPermissionProfile, match="read_dirs"):
        rules.check_read_dirs([f"{world.root}/data/../data"], writable)


def test_writable_hop_follows_dot_dot_after_a_symlink_as_the_kernel_does(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    hop = _deep_link(world, world.repo / "hop")
    via = world.root / "via"
    via.symlink_to(hop)  # /via -> <cwd>/hop -> /data/a/b; /via/.. is /data/a
    assert pathrules.writable_hop(str(world.root / "data"), writable) is None
    assert pathrules.writable_hop(f"{via}/../x", writable) is not None
    with pytest.raises(LaunchValidationError):
        pathrules.check_path_entries([f"{via}/.."], writable)


def test_resolve_tool_keeps_the_tool_name_for_argv0(world: World) -> None:
    writable = world.writable(cwd=world.repo)
    shim = executable(world.root / "opt" / "volta-shim")
    (world.bin / "node").symlink_to(shim)
    resolved = pathrules.resolve_tool("node", [str(world.bin)], writable)
    assert resolved == pathrules.ResolvedTool(path=str(shim), name="node")
    absolute = pathrules.resolve_tool(str(world.bin / "node"), [], writable)
    assert absolute is not None and absolute.name == "node" and absolute.path == str(shim)


def _aliased(world: World) -> Path:
    """``<root>/alias -> <root>``: a symlinked ancestor (macOS ``/tmp -> /private/tmp``)."""
    alias = world.root / "alias"
    alias.symlink_to(world.root)
    return alias


def test_exceptions_hold_through_a_symlinked_ancestor(world: World) -> None:
    alias = _aliased(world)
    rel = lambda p: alias / p.relative_to(world.root)  # noqa: E731
    rules = PathRules(
        world.host(),
        session_dir=str(rel(world.csdir)),
        cwd=str(rel(world.work)),
        caller_cwd=str(world.repo),
        tmpdir=str(rel(world.tmpdir)),
    )
    assert rules.write_target_problem(str(world.work)) is None
    assert rules.write_target_problem(str(rel(world.work))) is None
    assert rules.write_target_problem(str(world.tmpdir / "x")) is None
    rules.check_cwd()


def test_swapped_final_component_through_a_symlinked_ancestor_loses_the_exception(
    world: World,
) -> None:
    alias = _aliased(world)
    os.rmdir(world.work)
    world.work.symlink_to(world.home / ".ssh")
    rules = PathRules(
        world.host(),
        session_dir=str(alias / world.csdir.relative_to(world.root)),
        cwd=str(alias / world.work.relative_to(world.root)),
        caller_cwd=str(world.repo),
        tmpdir=str(world.tmpdir),
    )
    assert rules.write_target_problem(str(world.home / ".ssh")) is not None
    with pytest.raises(LaunchValidationError):
        rules.check_cwd()
