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
from .test_dryrun import HOME, process, real_plan, tool
from .test_selfcheck import FakeRunner, executable


class Layout:
    """A uv-style install: the venv python links to a versioned alias directory, itself a
    symlink to the real interpreter directory."""

    def __init__(self, root: Path, store: Path | None = None) -> None:
        """``store``: the directory holding the alias (default ``<root>/store``); the home
        directory makes a chain no launch can list."""
        root = Path(os.path.realpath(root))
        self.store = root / "store" if store is None else Path(os.path.realpath(store))
        self.store.mkdir(parents=True, exist_ok=True)
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


@pytest.fixture
def stuck(tmp_path: Path) -> Layout:
    """An alias that sits directly in the home directory of ``kinds_proc`` (and, for the
    dry-run plan, of ``real_plan``): listing it would list the home directory."""
    return Layout(tmp_path / "install", store=tmp_path / "home")


@pytest.fixture
def stuck_plan(tmp_path: Path) -> Layout:
    """The same for ``real_plan``, whose home is ``<tmp>/root<HOME>``."""
    return Layout(tmp_path / "install", store=tmp_path / "root" / HOME.lstrip("/"))


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


# pathrules.symlink_holders and settings_builder.interpreter_holders


def test_symlink_holders_are_the_exact_directories_holding_the_links(layout: Layout) -> None:
    assert pathrules.symlink_holders(str(layout.bin / "python")) == [
        str(layout.bin),
        str(layout.store),
    ]


def test_symlink_holders_of_a_loop_is_none(tmp_path: Path) -> None:
    os.symlink(tmp_path / "b", tmp_path / "a")
    os.symlink(tmp_path / "a", tmp_path / "b")
    assert pathrules.symlink_holders(str(tmp_path / "a")) is None


def test_holders_list_the_alias_store_but_not_what_is_readable(layout: Layout) -> None:
    holders = settings_builder.interpreter_holders(
        str(layout.script), layout.readable(), lambda _p: True
    )
    assert holders == (str(layout.store),)  # venv/bin is readable already
    assert (
        settings_builder.interpreter_problem(str(layout.script), [*layout.readable(), *holders])
        is None
    )


def test_holders_never_include_the_ancestors_or_siblings_of_the_store(layout: Layout) -> None:
    (layout.store.parent / "sibling").mkdir()
    holders = settings_builder.interpreter_holders(str(layout.script), [], lambda _p: True)
    assert set(holders) == {str(layout.bin), str(layout.store)}


def test_a_refused_holder_is_left_out_and_the_chain_stays_a_problem(layout: Layout) -> None:
    holders = settings_builder.interpreter_holders(
        str(layout.script), layout.readable(), lambda p: p != str(layout.store)
    )
    assert holders == ()
    problem = settings_builder.interpreter_problem(str(layout.script), layout.readable())
    assert problem is not None and str(layout.store) in problem


def test_the_root_directory_is_never_a_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink directly under `/` makes `/` a holder of the chain; it is never listed.
    Host independent: the chain is faked."""
    script = executable(tmp_path / "claude-session", "#!/bin/sh\n")
    monkeypatch.setattr(pathrules, "symlink_holders", lambda _p: ["/", str(tmp_path)])
    holders = settings_builder.interpreter_holders(script, [], lambda _p: True)
    assert holders == (str(tmp_path),)


@pytest.mark.parametrize(
    "body",
    ["#!/usr/bin/env python3\n", "#!python\n", "not a script\n", ""],
)
def test_no_holders_for_other_launchers(tmp_path: Path, body: str) -> None:
    script = executable(tmp_path / "claude-session", body)
    assert settings_builder.interpreter_holders(script, [], lambda _p: True) == ()


def test_holders_of_a_missing_or_looping_interpreter_are_none(tmp_path: Path) -> None:
    os.symlink(tmp_path / "b", tmp_path / "a")
    os.symlink(tmp_path / "a", tmp_path / "b")
    script = executable(tmp_path / "claude-session", f"#!{tmp_path}/a\n")
    assert settings_builder.interpreter_holders(script, [], lambda _p: True) == ()


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


def kinds_row(tmp_path: Path, proc: launch.Process, gw: FakeGateway, kind: str = "review", **kw):
    report = dryrun.kinds_report(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        proc,
        kind=kind,
        repo=None,
        prerequisites=dryrun.Prerequisites(gw, "linux", **kw),
    )
    (row,) = report["kinds"]
    return row


def test_kinds_reports_the_alias_interpreter_as_unavailable(tmp_path: Path, stuck: Layout) -> None:
    gw = FakeGateway()
    row = kinds_row(tmp_path, kinds_proc(tmp_path, stuck, gw), gw, link_dirs=())
    assert row["sandbox_available"] is False
    assert any(str(stuck.store) in p for p in row["sandbox_problems"])
    assert any("a launch fails" in w for w in row["warnings"])


def test_kinds_preferred_kind_warns_only_that_report_fails(tmp_path: Path, stuck: Layout) -> None:
    gw = FakeGateway()
    row = kinds_row(tmp_path, kinds_proc(tmp_path, stuck, gw), gw, "rank", link_dirs=())
    assert row["sandbox_available"] is False
    assert_only_report_fails(row["warnings"])


def test_kinds_preferred_third_party_kind_warns_only_that_report_fails(
    tmp_path: Path, stuck: Layout
) -> None:
    gw = FakeGateway()
    proc = kinds_proc(tmp_path, stuck, gw)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("kinds: {review: {sandbox: preferred}}\n")
    report = dryrun.kinds_report(
        config.load(cfg_dir / "config.yaml"),
        proc,
        kind="review",
        repo=None,
        prerequisites=dryrun.Prerequisites(gw, "linux", link_dirs=()),
    )
    (row,) = report["kinds"]
    assert row["sandbox_available"] is False
    assert_only_report_fails(row["warnings"])


def test_kinds_missing_tools_still_warn_unsandboxed(tmp_path: Path, layout: Layout) -> None:
    gw = FakeGateway()
    proc = kinds_proc(tmp_path, layout, gw)
    proc = dataclasses.replace(proc, environ={**proc.environ, "PATH": str(tmp_path / "empty")})
    row = kinds_row(tmp_path, proc, gw, "rank", link_dirs=())
    assert any("not sandboxed" in w for w in row["warnings"]), row["warnings"]


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


def test_macos_skips_the_alias_probe(tmp_path: Path, stuck: Layout) -> None:
    gw = FakeGateway()
    proc = kinds_proc(tmp_path, stuck, gw)
    resolution = kinds.resolve(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        "review",
        mode="headless",
    )
    none = pathrules.SessionWritable([])
    assert dryrun.Prerequisites(gw, "linux", link_dirs=()).install_problems(proc, resolution, none)
    assert (
        dryrun.Prerequisites(gw, "macos", link_dirs=()).install_problems(proc, resolution, none)
        == ()
    )


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
        proc, resolution, pathrules.SessionWritable([])
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
    tmp_path: Path, stuck_plan: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    plan, _ = real_plan(
        tmp_path, monkeypatch, "rank", setup=lambda _p: script_for(tmp_path / "root", stuck_plan)
    )
    assert plan["would_fail"] is None, plan["warnings"]
    assert plan["sandbox"]["available"] is False
    assert any(str(stuck_plan.bin) in p for p in plan["sandbox"]["problems"])
    assert any("sandbox is active" in w for w in plan["warnings"])


def test_dry_run_preferred_third_party_kind_warns_only_that_report_fails(
    tmp_path: Path, stuck_plan: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    plan, _ = real_plan(
        tmp_path,
        monkeypatch,
        "review",
        setup=lambda _p: script_for(tmp_path / "root", stuck_plan),
        extra_args=("--sandbox", "preferred"),
    )
    assert plan["would_fail"] is None, plan["warnings"]
    assert_only_report_fails(plan["warnings"])
    assert_only_report_fails(plan["profile"]["warnings"])


def assert_only_report_fails(warnings: list[str]) -> None:
    assert any("sandbox is active" in w and "report` will fail" in w for w in warnings), warnings
    assert not any("not sandboxed" in w or "unsandboxed" in w for w in warnings), warnings


def test_dry_run_preferred_kind_profile_warns_only_that_report_fails(
    tmp_path: Path, stuck_plan: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    plan, _ = real_plan(
        tmp_path, monkeypatch, "rank", setup=lambda _p: script_for(tmp_path / "root", stuck_plan)
    )
    assert_only_report_fails(plan["profile"]["warnings"])
    assert_only_report_fails(plan["warnings"])


def test_dry_run_fails_a_required_kind(
    tmp_path: Path, stuck_plan: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    plan, _ = real_plan(
        tmp_path, monkeypatch, "review", setup=lambda _p: script_for(tmp_path / "root", stuck_plan)
    )
    assert plan["would_fail"] == "sandbox_unavailable"


def test_dry_run_lists_the_alias_store_and_passes_a_required_kind(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uv layout with no read_dirs: the store holding the alias is added to allowRead,
    shown in the plan, and the sandbox stays available."""
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    plan, _ = real_plan(
        tmp_path, monkeypatch, "review", setup=lambda _p: script_for(tmp_path / "root", layout)
    )
    assert plan["would_fail"] is None, plan["warnings"]
    assert plan["sandbox"]["available"] is True and plan["sandbox"]["problems"] == []
    store = str(layout.store)
    assert store in plan["sandbox"]["allow_read"]
    assert store not in plan["sandbox"]["allow_write"]
    assert store in plan["profile"]["read_dirs"]
    # only the holders: not the install root above the store, not the venv's siblings
    assert str(layout.store.parent) not in plan["sandbox"]["allow_read"]


def holder_rules(tmp_path: Path, home: Path) -> pathrules.PathRules:
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    host = pathrules.Host.from_environ({"HOME": str(home)}, state_root=str(state), path_prepend=[])
    return pathrules.PathRules(
        host,
        session_dir=str(state / "sessions" / "cs-abcd2345"),
        cwd=str(cwd),
        caller_cwd=str(cwd),
        tmpdir=None,
        planned=True,
    )


def holders_with(
    tmp_path: Path, layout: Layout, writable: list[str], home: Path | None = None
) -> tuple[str, ...]:
    proc = dataclasses.replace(
        process(tmp_path, {}, FakeGateway()), claude_session=str(layout.script)
    )
    rules = holder_rules(tmp_path, home or tmp_path / "home")
    return launch.interpreter_holders(
        proc, rules, pathrules.SessionWritable(writable), layout.readable()
    )


def test_a_holder_is_listed_when_nothing_forbids_it(tmp_path: Path, layout: Layout) -> None:
    assert holders_with(tmp_path, layout, []) == (str(layout.store),)


def test_a_session_writable_holder_is_not_listed(tmp_path: Path, layout: Layout) -> None:
    assert holders_with(tmp_path, layout, [str(layout.store)]) == ()


def test_a_holder_inside_a_session_writable_directory_is_not_listed(
    tmp_path: Path, layout: Layout
) -> None:
    assert holders_with(tmp_path, layout, [str(layout.store.parent)]) == ()


def test_a_holder_containing_a_session_writable_path_is_not_listed(
    tmp_path: Path, layout: Layout
) -> None:
    assert holders_with(tmp_path, layout, [str(layout.real / "bin")]) == ()


def test_the_home_directory_and_its_ancestors_are_not_listed(tmp_path: Path, stuck: Layout) -> None:
    assert holders_with(tmp_path, stuck, [], home=stuck.store) == ()
    assert holders_with(tmp_path, stuck, [], home=stuck.store / "user") == ()


def test_a_credential_directory_is_not_listed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ssh = Layout(tmp_path / "install", store=home / ".ssh")
    assert holders_with(tmp_path, ssh, [], home=home) == ()


def test_the_state_root_is_not_listed(tmp_path: Path) -> None:
    state = Layout(tmp_path / "install", store=tmp_path / "state" / "sub")
    assert holders_with(tmp_path, state, []) == ()


def test_a_hostile_chain_through_the_root_or_home_lists_nothing_dangerous(tmp_path: Path) -> None:
    """A symlink whose target is `/`, the home directory or /etc adds no directory: only
    the directories that hold links are candidates, and those the rules refuse are dropped."""
    home = tmp_path / "home"
    home.mkdir()
    os.symlink("/", tmp_path / "to-root")
    os.symlink(home, tmp_path / "to-home")
    os.symlink("/etc", tmp_path / "to-etc")
    (home / "python").write_text("")
    script = executable(tmp_path / "claude-session", f"#!{tmp_path}/to-home/python\n")
    proc = dataclasses.replace(process(tmp_path, {}, FakeGateway()), claude_session=script)
    rules = holder_rules(tmp_path, home)
    got = launch.interpreter_holders(proc, rules, pathrules.SessionWritable([]), [])
    # the only holder is tmp_path, an ancestor of the home directory: refused
    assert got == ()
    # a link that sits in the home directory itself is refused
    os.symlink(home / "python", home / "link")
    script2 = executable(tmp_path / "cs2", f"#!{home}/link\n")
    proc2 = dataclasses.replace(process(tmp_path, {}, FakeGateway()), claude_session=script2)
    assert launch.interpreter_holders(proc2, rules, pathrules.SessionWritable([]), []) == ()


def test_kinds_lists_the_alias_store_and_reports_available(tmp_path: Path, layout: Layout) -> None:
    gw = FakeGateway()
    row = kinds_row(tmp_path, kinds_proc(tmp_path, layout, gw), gw, link_dirs=())
    assert row["sandbox_available"] is True and row["sandbox_problems"] == []
    assert str(layout.store) in row["read_dirs"]


def test_kinds_refuses_a_holder_that_is_the_home_directory(tmp_path: Path, stuck: Layout) -> None:
    gw = FakeGateway()
    row = kinds_row(tmp_path, kinds_proc(tmp_path, stuck, gw), gw, link_dirs=())
    assert row["sandbox_available"] is False
    assert str(stuck.store) not in row["read_dirs"]


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


def test_the_message_names_the_holder_and_why_it_stays_unlisted(layout: Layout) -> None:
    problem = settings_builder.interpreter_problem(str(layout.script), layout.readable())
    assert problem is not None
    assert repr(str(layout.store)) in problem
    assert "cannot be listed" in problem and "--copies" in problem


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


# #124 review round 1


def test_a_sandbox_off_kind_lists_no_holders_in_kinds(tmp_path: Path, layout: Layout) -> None:
    """R1-1: with `sandbox: off` no probe runs and allowRead is inert."""
    gw = FakeGateway()
    proc = kinds_proc(tmp_path, layout, gw)
    off = kinds_row(tmp_path, proc, gw, "implement", link_dirs=())
    assert off["sandbox"] == "off"
    assert str(layout.store) not in off["read_dirs"]
    on = kinds_row(tmp_path, proc, gw, "review", link_dirs=())
    assert str(layout.store) in on["read_dirs"]


def test_a_sandbox_off_dry_run_lists_no_holders(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    plan, _ = real_plan(
        tmp_path, monkeypatch, "implement", setup=lambda _p: script_for(tmp_path / "root", layout)
    )
    assert plan["sandbox"]["allow_read"] == []
    assert str(layout.store) not in plan["profile"]["read_dirs"]


def test_a_sandbox_off_third_party_kind_lists_no_holders(tmp_path: Path, layout: Layout) -> None:
    gw = FakeGateway()
    proc = kinds_proc(tmp_path, layout, gw)
    resolution = kinds.resolve(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        "review",
        mode="headless",
    )
    resolution = dataclasses.replace(
        resolution, profile=resolution.profile.model_copy(update={"sandbox": "off"})
    )
    assert resolution.profile.sandbox == "off" and resolution.isolated()
    assert (
        dryrun._interpreter_holders(
            proc, resolution, pathrules.SessionWritable([]), layout.readable()
        )
        == ()
    )


def test_macos_leaves_the_holder_out_of_kinds_and_the_plan(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-6: the holder listing is for Linux and WSL2 (selfcheck.symlink_hops_apply)."""
    gw = FakeGateway()
    proc = kinds_proc(tmp_path, layout, gw)
    report = dryrun.kinds_report(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        proc,
        kind="review",
        repo=None,
        prerequisites=dryrun.Prerequisites(gw, "macos", link_dirs=()),
    )
    (row,) = report["kinds"]
    assert str(layout.store) not in row["read_dirs"]
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "macos")
    plan, _ = real_plan(
        tmp_path, monkeypatch, "review", setup=lambda _p: script_for(tmp_path / "root", layout)
    )
    assert str(layout.store) not in plan["sandbox"]["allow_read"]
    assert str(layout.store) not in plan["profile"]["read_dirs"]


def test_a_world_writable_holder_is_not_listed(tmp_path: Path, layout: Layout) -> None:
    """R1-9: another user could plant files in it."""
    layout.store.chmod(0o777)
    assert holders_with(tmp_path, layout, []) == ()
    layout.store.chmod(0o755)
    assert holders_with(tmp_path, layout, []) == (str(layout.store),)


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="needs /proc")
def test_a_pseudo_filesystem_holder_is_not_listed(tmp_path: Path) -> None:
    """R1-8: /proc/self/exe is a symlink held by /proc (and /proc/<pid>)."""
    script = executable(tmp_path / "claude-session", "#!/proc/self/exe\n")
    proc = dataclasses.replace(process(tmp_path, {}, FakeGateway()), claude_session=script)
    holders = settings_builder.interpreter_holders(script, [], lambda _p: True)
    assert "/proc" in holders  # the unfiltered chain does run through /proc
    rules = holder_rules(tmp_path, tmp_path / "home")
    assert launch.interpreter_holders(proc, rules, pathrules.SessionWritable([]), []) == ()


def test_generated_holders_pass_the_generated_entry_rules(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-7: launch.checks runs rules.check_generated over the holders."""
    seen: list[dict] = []
    original = pathrules.PathRules.check_generated

    def spy(self, entries):  # type: ignore[no-untyped-def]
        seen.append({k: tuple(v) for k, v in entries.items()})
        return original(self, entries)

    monkeypatch.setattr(pathrules.PathRules, "check_generated", spy)
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    real_plan(
        tmp_path, monkeypatch, "review", setup=lambda _p: script_for(tmp_path / "root", layout)
    )
    assert any(set(e) == {"interpreter"} and str(layout.store) in e["interpreter"] for e in seen)


def test_chains_through_a_root_or_etc_link_list_nothing_dangerous(tmp_path: Path) -> None:
    """R1-5: links whose targets are `/` or /etc: the holder is the directory holding the
    link (never `/` or /etc), and what is listed passes the generated-entry rules."""
    home = tmp_path / "home"
    home.mkdir()
    os.symlink("/", tmp_path / "to-root")
    os.symlink("/etc", tmp_path / "to-etc")
    rules = holder_rules(tmp_path, home)
    for link, rest in (("to-root", "usr/bin/env"), ("to-etc", "hostname")):
        script = executable(tmp_path / f"cs-{link}", f"#!{tmp_path}/{link}/{rest}\n")
        proc = dataclasses.replace(process(tmp_path, {}, FakeGateway()), claude_session=script)
        got = launch.interpreter_holders(proc, rules, pathrules.SessionWritable([]), [])
        assert "/" not in got and "/etc" not in got
        assert all(rules.read_entry_problem(h, generated=True) is None for h in got)
