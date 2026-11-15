"""The sandbox availability probe for what a prerequisite probe cannot see (interface.md,
Isolation and Dry run): an interpreter of ``claude-session`` behind an alias directory the
sandbox does not list, and hard-linked code. Fakes and temporary directories only; no real
sandbox is needed."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_session import config, dryrun, kinds, launch, pathrules, selfcheck, settings_builder
from claude_session.errors import LaunchValidationError

from .fake_gateway import FakeGateway
from .test_dryrun import process, tool
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


def test_install_problem_makes_a_preferred_sandbox_inactive(
    base: Path, tools: tuple[str, str]
) -> None:
    result = check("preferred", base, tools, ("the interpreter is unreachable",))
    assert result.sandbox_active is False
    assert result.problems == ("the interpreter is unreachable",)
    assert any("unreachable" in w for w in result.warnings)


def test_install_problem_is_added_to_a_missing_prerequisite(base: Path) -> None:
    result = check("preferred", base, ("", ""), ("extra problem",))  # type: ignore[arg-type]
    assert len(result.problems) == 2 and result.problems[1] == "extra problem"


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
    import dataclasses

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
    result = launch.Effects().sandbox(resolution, alloc, tools, proc, None)
    assert result.sandbox_active is False
    assert any(str(layout.store) in p for p in result.problems)


def test_sandbox_check_fails_a_required_kind_on_the_alias_interpreter(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    resolution, alloc, tools, proc = sandbox_inputs(tmp_path, layout, "review")
    with pytest.raises(LaunchValidationError) as caught:
        launch.Effects().sandbox(resolution, alloc, tools, proc, None)
    assert caught.value.end_reason == "sandbox_unavailable"


def test_a_read_dir_listing_the_alias_directory_clears_it(
    tmp_path: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(selfcheck, "detect_platform", lambda *a, **k: "linux")
    resolution, alloc, tools, proc = sandbox_inputs(tmp_path, layout, "rank")
    # The real interpreter directory is not readable either: list the store as a whole.
    result = launch.Effects().sandbox(
        resolution, alloc, tools, proc, None, read_dirs=[str(layout.store)]
    )
    assert result.sandbox_active is True and result.problems == ()


# dryrun.Prerequisites (kinds, and a dry run whose launch checks failed)


def kinds_proc(tmp_path: Path, layout: Layout, gw: FakeGateway) -> launch.Process:
    import dataclasses

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
