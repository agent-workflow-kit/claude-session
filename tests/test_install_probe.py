"""The sandbox availability probe for what a prerequisite probe cannot see (interface.md,
Isolation and Dry run): an interpreter of ``claude-session`` behind an alias directory the
sandbox does not list, and hard-linked code. Fakes and temporary directories only; no real
sandbox is needed."""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_session import config, dryrun, kinds, launch, pathrules, selfcheck, settings_builder
from claude_session.errors import LaunchValidationError

from .fake_gateway import FakeGateway
from .test_dryrun import process, real_plan, tool
from .test_selfcheck import FakeRunner, executable


class Layout:
    """A uv-style install: the venv python links to a versioned alias directory, itself a
    symlink to the real interpreter directory."""

    def __init__(self, root: Path) -> None:
        root = Path(os.path.realpath(root))
        self.store = root / "store"
        self.real = self.store / "cpython-3.12.5-linux"
        (self.real / "bin").mkdir(parents=True)
        executable(self.real / "bin" / "python3.12")
        self.alias = self.store / "cpython-3.12-linux"
        os.symlink(self.real.name, self.alias)
        self.bin = root / "venv" / "bin"
        self.bin.mkdir(parents=True)
        os.symlink(self.alias / "bin" / "python3.12", self.bin / "python")
        self.script = self.bin / "claude-session"
        executable(self.script, f"#!{self.bin}/python\nimport sys\n")

    def readable(self, *extra: Path) -> list[str]:
        return [str(self.bin), str(self.real), *map(str, extra)]


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path / "install")


# pathrules.unreadable_hop


def test_hop_names_the_directory_holding_an_unlisted_alias(layout: Layout) -> None:
    readable = layout.readable()
    hop = pathrules.unreadable_hop(
        str(layout.bin / "python"), lambda p: any(pathrules._under(p, r) for r in readable)
    )
    assert hop == str(layout.store)


def test_hop_passes_when_the_alias_directory_is_listed(layout: Layout) -> None:
    readable = layout.readable(layout.store)
    assert (
        pathrules.unreadable_hop(
            str(layout.bin / "python"), lambda p: any(pathrules._under(p, r) for r in readable)
        )
        is None
    )


def test_hop_flags_an_unlisted_real_file(tmp_path: Path) -> None:
    target = tmp_path / "plain"
    target.write_text("x")
    assert pathrules.unreadable_hop(str(target), lambda p: False) == os.path.realpath(target)


def test_hop_fails_closed_on_a_symlink_loop(tmp_path: Path) -> None:
    os.symlink(tmp_path / "b", tmp_path / "a")
    os.symlink(tmp_path / "a", tmp_path / "b")
    assert pathrules.unreadable_hop(str(tmp_path / "a"), lambda p: True) is not None


# settings_builder.interpreter_problem


def test_alias_interpreter_is_a_problem(layout: Layout) -> None:
    problem = settings_builder.interpreter_problem(str(layout.script), layout.readable())
    assert problem is not None
    assert str(layout.script) in problem and str(layout.store) in problem


def test_listed_alias_directory_is_no_problem(layout: Layout) -> None:
    assert (
        settings_builder.interpreter_problem(str(layout.script), layout.readable(layout.store))
        is None
    )


@pytest.mark.parametrize(
    "body",
    [
        "#!/usr/bin/env python3\n",  # looked up on the sandbox's PATH: not checked
        "#!python\n",  # relative: not checked
        "not a script\n",
        "",
        "#!",
    ],
)
def test_other_launchers_are_not_checked(tmp_path: Path, body: str) -> None:
    script = executable(tmp_path / "claude-session", body)
    assert settings_builder.interpreter_problem(script, []) is None


def test_a_missing_executable_is_not_checked(tmp_path: Path) -> None:
    assert settings_builder.interpreter_problem(str(tmp_path / "absent"), []) is None


def test_a_shebang_argument_is_ignored(layout: Layout) -> None:
    layout.script.write_text(f"#!{layout.bin}/python -I\n")
    assert settings_builder.interpreter_problem(str(layout.script), layout.readable()) is not None


# settings_builder.hard_link_problem


def test_hard_linked_code_is_a_problem(tmp_path: Path) -> None:
    code = tmp_path / "pkg"
    code.mkdir()
    (code / "a.py").write_text("x = 1\n")
    os.link(code / "a.py", tmp_path / "cache-a.py")
    problem = settings_builder.hard_link_problem([str(code)])
    assert problem is not None and "a.py" in problem and "link-mode=copy" in problem


def test_copied_code_is_no_problem(tmp_path: Path) -> None:
    code = tmp_path / "pkg"
    code.mkdir()
    (code / "a.py").write_text("x = 1\n")
    assert settings_builder.hard_link_problem([str(code)]) is None


# selfcheck.self_check


@pytest.fixture
def base() -> Iterator[Path]:
    """A short sticky stand-in for ``/tmp`` (the socket path limit)."""
    path = Path(tempfile.mkdtemp(prefix="cs-ip-", dir="/tmp"))
    path.chmod(0o1777)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def check(mode: str, base: Path, tools: tuple[str, str], install: tuple[str, ...]):
    return selfcheck.self_check(
        mode,
        None,
        bwrap=tools[0],
        socat=tools[1],
        platform="linux",
        gw=FakeRunner(0),
        tmp_base=str(base),
        create=False,
        install_problems=install,
    )


@pytest.fixture
def tools(tmp_path: Path) -> tuple[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return executable(bin_dir / "bwrap"), executable(bin_dir / "socat")


def test_install_problem_fails_a_required_sandbox(base: Path, tools: tuple[str, str]) -> None:
    with pytest.raises(LaunchValidationError) as caught:
        check("required", base, tools, ("the interpreter is unreachable",))
    assert caught.value.end_reason == "sandbox_unavailable"
    assert "the interpreter is unreachable" in caught.value.message


def test_install_problem_leaves_a_preferred_sandbox_active_with_an_accurate_warning(
    base: Path, tools: tuple[str, str]
) -> None:
    result = check("preferred", base, tools, ("the interpreter is unreachable",))
    assert result.sandbox_active is True
    assert result.problems == ("the interpreter is unreachable",)
    (warning,) = result.warnings
    assert "sandbox is active" in warning and "report` will fail" in warning
    assert "the interpreter is unreachable" in warning and "not sandboxed" not in warning


def test_install_problem_is_added_to_a_missing_prerequisite(base: Path) -> None:
    result = check("preferred", base, ("", ""), ("extra problem",))  # type: ignore[arg-type]
    assert len(result.problems) == 2 and result.problems[1] == "extra problem"
    assert result.sandbox_active is False
    assert all("will fail" not in w for w in result.warnings)  # the run is unsandboxed anyway


def test_no_install_problem_leaves_the_sandbox_active(base: Path, tools: tuple[str, str]) -> None:
    assert check("required", base, tools, ()).sandbox_active is True


def test_off_ignores_install_problems(base: Path, tools: tuple[str, str]) -> None:
    result = check("off", base, tools, ("ignored",))
    assert result.sandbox_active is False and result.problems == ()


# launch.check_sandbox (exec and exec --dry-run)


def sandbox_inputs(tmp_path: Path, layout: Layout, kind: str = "review"):
    bin_dir = tmp_path / "tbin"
    bwrap = tool(bin_dir, "bwrap")
    socat = tool(bin_dir, "socat")
    cfg = config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")})
    resolution = kinds.resolve(cfg, kind, mode="headless")
    tools = launch.Tools(
        claude=pathrules.ResolvedTool(str(tool(bin_dir, "claude")), "claude"),
        git=None,
        bwrap=pathrules.ResolvedTool(str(bwrap), "bwrap"),
        socat=pathrules.ResolvedTool(str(socat), "socat"),
    )
    gw = FakeGateway()
    gw.on([str(bwrap)])
    gw.on([str(socat)])
    proc = process(tmp_path, {"PATH": str(bin_dir), "HOME": str(tmp_path)}, gw)
    proc = dataclasses.replace(proc, claude_session=str(layout.script))
    alloc = launch.Allocated(
        csession_id="cs-abcd2345", run=1, session_dir="/s", private_dir="/p", warnings=()
    )
    return resolution, alloc, tools, proc


def test_sandbox_check_reports_the_alias_interpreter_for_a_preferred_kind(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    resolution, alloc, tools, proc = sandbox_inputs(tmp_path, layout, "rank")
    result = launch.Effects().sandbox(
        resolution, alloc, tools, proc, None, readable=layout.readable()
    )
    assert result.sandbox_active is True  # the sandbox runs; only `report` would fail
    assert any(str(layout.store) in p for p in result.problems)
    assert any("will fail" in w and str(layout.store) in w for w in result.warnings)


def test_sandbox_check_fails_a_required_kind_on_the_alias_interpreter(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    resolution, alloc, tools, proc = sandbox_inputs(tmp_path, layout, "review")
    with pytest.raises(LaunchValidationError) as caught:
        launch.Effects().sandbox(resolution, alloc, tools, proc, None, readable=layout.readable())
    assert caught.value.end_reason == "sandbox_unavailable"


def test_a_read_dir_listing_the_alias_directory_clears_it(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    resolution, alloc, tools, proc = sandbox_inputs(tmp_path, layout, "rank")
    # The real interpreter directory is not readable either: list the store as a whole.
    result = launch.Effects().sandbox(
        resolution, alloc, tools, proc, None, readable=layout.readable(layout.store)
    )
    assert result.sandbox_active is True and result.problems == ()


# dryrun.Prerequisites (kinds, and a dry run whose launch checks failed)


def kinds_proc(tmp_path: Path, layout: Layout, gw: FakeGateway) -> launch.Process:
    bin_dir = tmp_path / "tbin"
    tool(bin_dir, "bwrap")
    tool(bin_dir, "socat")
    tool(bin_dir, "claude")
    gw.on([str(bin_dir / "bwrap")])
    gw.on([str(bin_dir / "socat")])
    gw.on(["claude", "--version"], stdout="2.1.314 (Claude Code)\n")
    proc = process(tmp_path, {"PATH": str(bin_dir), "HOME": str(tmp_path / "home")}, gw)
    return dataclasses.replace(proc, claude_session=str(layout.script))


def kinds_row(tmp_path: Path, proc: launch.Process, gw: FakeGateway, **kw):
    report = dryrun.kinds_report(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        proc,
        kind="review",
        repo=None,
        prerequisites=dryrun.Prerequisites(gw, "linux", **kw),
    )
    (row,) = report["kinds"]
    return row


def test_kinds_reports_the_alias_interpreter_as_unavailable(tmp_path: Path, layout: Layout) -> None:
    gw = FakeGateway()
    row = kinds_row(tmp_path, kinds_proc(tmp_path, layout, gw), gw, link_dirs=())
    assert row["sandbox_available"] is False
    assert any(str(layout.store) in p for p in row["sandbox_problems"])
    assert any("a launch fails" in w for w in row["warnings"])


def test_kinds_reports_hard_linked_code_as_unavailable(tmp_path: Path, layout: Layout) -> None:
    code = tmp_path / "code"
    code.mkdir()
    (code / "m.py").write_text("x = 1\n")
    os.link(code / "m.py", tmp_path / "cache-m.py")
    layout.script.write_text("not a script\n")  # only the hard link is wrong
    gw = FakeGateway()
    row = kinds_row(tmp_path, kinds_proc(tmp_path, layout, gw), gw, link_dirs=[str(code)])
    assert row["sandbox_available"] is False
    assert any("hard links" in p for p in row["sandbox_problems"])


def test_kinds_reports_a_healthy_install_as_available(tmp_path: Path, layout: Layout) -> None:
    layout.script.write_text("not a script\n")
    gw = FakeGateway()
    row = kinds_row(tmp_path, kinds_proc(tmp_path, layout, gw), gw, link_dirs=())
    assert row["sandbox_available"] is True and row["sandbox_problems"] == []


def test_the_hard_link_scan_runs_once_per_report(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        settings_builder, "hard_link_problem", lambda dirs=None: calls.append(1) or None
    )
    gw = FakeGateway()
    proc = kinds_proc(tmp_path, layout, gw)
    report = dryrun.kinds_report(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        proc,
        kind=None,
        repo=None,
        prerequisites=dryrun.Prerequisites(gw, "linux"),
    )
    assert len(report["kinds"]) > 1 and calls == [1]


# R2: the symlink-hop probe is for Linux and WSL2 only


def test_macos_skips_the_alias_probe(tmp_path: Path, layout: Layout) -> None:
    gw = FakeGateway()
    proc = kinds_proc(tmp_path, layout, gw)
    resolution = kinds.resolve(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        "review",
        mode="headless",
    )
    assert dryrun.Prerequisites(gw, "linux", link_dirs=()).install_problems(proc, resolution)
    assert dryrun.Prerequisites(gw, "macos", link_dirs=()).install_problems(proc, resolution) == ()


def test_macos_still_scans_for_hard_links(tmp_path: Path, layout: Layout) -> None:
    code = tmp_path / "code"
    code.mkdir()
    (code / "m.py").write_text("x = 1\n")
    os.link(code / "m.py", tmp_path / "cache-m.py")
    gw = FakeGateway()
    proc = kinds_proc(tmp_path, layout, gw)
    resolution = kinds.resolve(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        "review",
        mode="headless",
    )
    (problem,) = dryrun.Prerequisites(gw, "macos", link_dirs=[str(code)]).install_problems(
        proc, resolution
    )
    assert "hard links" in problem


def test_exec_skips_the_alias_probe_on_macos(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "macos")
    monkeypatch.setattr(selfcheck, "_macos", lambda *a, **k: selfcheck.Prerequisites(True))
    resolution, alloc, tools, proc = sandbox_inputs(tmp_path, layout, "review")
    result = launch.Effects().sandbox(resolution, alloc, tools, proc, None, readable=[])
    assert result.sandbox_active is True and result.problems == ()


# R3: the readable set has every source of the real allowRead


def test_readable_set_has_every_allow_read_source(tmp_path: Path) -> None:
    alloc = launch.Allocated(
        csession_id="cs-abcd2345", run=1, session_dir="/s", private_dir="/p", warnings=()
    )
    expanded = {"sandbox": {"filesystem": {"allowRead": ["//opt/listed", "~/in-home"]}}}
    got = launch.sandbox_readable(
        "/c/cwd",
        alloc,
        {"system": ("/usr",), "git": ("/g/.git",)},
        ["/r/read"],
        ["/p/plugin"],
        expanded,
        "/home/u",
    )
    assert {"/c/cwd", "/s", "/usr", "/g/.git", "/r/read", "/p/plugin", "/opt/listed"} <= set(got)
    assert "/home/u/in-home" in got


def test_a_profile_allow_read_listing_the_holder_clears_the_problem(
    tmp_path: Path, layout: Layout
) -> None:
    alloc = launch.Allocated(
        csession_id="cs-abcd2345", run=1, session_dir="/s", private_dir="/p", warnings=()
    )
    expanded = {"sandbox": {"filesystem": {"allowRead": [str(layout.store)]}}}
    readable = launch.sandbox_readable(
        str(tmp_path), alloc, {"python": tuple(layout.readable())}, [], [], expanded, "/h"
    )
    assert settings_builder.interpreter_problem(str(layout.script), readable) is None


def test_the_cwd_counts_as_readable(layout: Layout) -> None:
    alloc = launch.Allocated(
        csession_id="cs-abcd2345", run=1, session_dir="/s", private_dir="/p", warnings=()
    )
    readable = launch.sandbox_readable(
        str(layout.store), alloc, {"python": tuple(layout.readable())}, [], [], {}, "/h"
    )
    assert settings_builder.interpreter_problem(str(layout.script), readable) is None


# end to end: exec --dry-run through checks and plan_sandbox


def script_for(root: Path, layout: Layout) -> None:
    from .test_dryrun import CS_PATH

    target = root / CS_PATH.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    executable(target, f"#!{layout.bin}/python\n")


def test_dry_run_reports_a_preferred_kind_unavailable_but_active(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    plan, _ = real_plan(
        tmp_path, monkeypatch, "rank", setup=lambda _p: script_for(tmp_path / "root", layout)
    )
    assert plan["would_fail"] is None, plan["warnings"]
    assert plan["sandbox"]["available"] is False
    assert any(str(layout.bin) in p for p in plan["sandbox"]["problems"])
    assert any("sandbox is active" in w for w in plan["warnings"])


def test_dry_run_fails_a_required_kind(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    plan, _ = real_plan(
        tmp_path, monkeypatch, "review", setup=lambda _p: script_for(tmp_path / "root", layout)
    )
    assert plan["would_fail"] == "sandbox_unavailable"


def test_dry_run_passes_read_dirs_into_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kind's read_dirs (the fixture lists /opt/toolchain) hold the whole install."""
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    inside = Layout(tmp_path / "root" / "opt" / "toolchain" / "install")
    plan, _ = real_plan(
        tmp_path, monkeypatch, "review", setup=lambda _p: script_for(tmp_path / "root", inside)
    )
    assert plan["would_fail"] is None, plan["warnings"]
    assert plan["sandbox"]["available"] is True and plan["sandbox"]["problems"] == []


# R4, R7, R9, R10, and a symlinked ancestor


def test_the_message_names_the_holder_and_what_listing_it_grants(layout: Layout) -> None:
    problem = settings_builder.interpreter_problem(str(layout.script), layout.readable())
    assert problem is not None
    assert f"list {str(layout.store)!r} itself in read_dirs" in problem
    assert "everything in it" in problem and "--copies" in problem


def test_a_symlinked_ancestor_in_an_unlisted_directory_is_a_problem(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "bin").mkdir(parents=True)
    executable(real / "bin" / "python")
    os.symlink(real, tmp_path / "alias")
    script = executable(tmp_path / "cs", f"#!{tmp_path}/alias/bin/python\n")
    problem = settings_builder.interpreter_problem(script, [str(real)])
    assert problem is not None and repr(os.path.realpath(tmp_path)) in problem


@pytest.mark.skipif(not os.path.islink("/bin"), reason="needs a usr-merged system")
def test_a_top_level_bin_symlink_is_readable_when_bin_is_listed(tmp_path: Path) -> None:
    script = executable(tmp_path / "cs", "#!/bin/sh\n")
    listed = [*settings_builder.system_read_dirs(), "/bin"]
    assert settings_builder.interpreter_problem(script, listed) is None
    assert settings_builder.interpreter_problem(script, ["/usr"]) is not None


def test_a_tab_ends_the_shebang_interpreter(layout: Layout) -> None:
    layout.script.write_text(f"#!{layout.bin}/python\t-I\n")
    assert (
        settings_builder.interpreter_problem(str(layout.script), layout.readable(layout.store))
        is None
    )


def test_a_missing_interpreter_has_its_own_message(tmp_path: Path) -> None:
    script = executable(tmp_path / "cs", f"#!{tmp_path}/gone/python\n")
    problem = settings_builder.interpreter_problem(script, [str(tmp_path)])
    assert problem is not None and "does not exist" in problem and "alias" not in problem


def test_a_symlink_loop_has_its_own_message(tmp_path: Path) -> None:
    os.symlink(tmp_path / "b", tmp_path / "a")
    os.symlink(tmp_path / "a", tmp_path / "b")
    script = executable(tmp_path / "cs", f"#!{tmp_path}/a\n")
    problem = settings_builder.interpreter_problem(script, [str(tmp_path)])
    assert problem is not None and "symlink loop or is behind more than" in problem


def test_too_long_a_chain_has_its_own_message(tmp_path: Path) -> None:
    executable(tmp_path / "l0")
    for index in range(1, pathrules.MAX_SYMLINKS + 2):
        os.symlink(tmp_path / f"l{index - 1}", tmp_path / f"l{index}")
    script = executable(tmp_path / "cs", f"#!{tmp_path}/l{pathrules.MAX_SYMLINKS + 1}\n")
    problem = settings_builder.interpreter_problem(script, [str(tmp_path)])
    assert problem is not None and "symlink loop or is behind more than" in problem
