"""The sandbox self-check (interface.md, Platform notes; design.md, Generated settings:
``sandbox_active``): prerequisites per platform, socket path length, the private tmpdir."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from claude_session import fsutil, selfcheck, tmpdir
from claude_session.errors import LaunchValidationError
from claude_session.gateway import GatewayTimeout, Result
from claude_session.selfcheck import Prerequisites

from .fake_gateway import FakeGateway

SID = "cs-abcd2345"
REPO = Path(__file__).resolve().parent.parent


class FakeRunner(FakeGateway):
    """A gateway whose ``run`` records every call and answers with ``returncode`` or raises
    ``error``. ``by_tool`` maps the basename of ``argv[0]`` to a return code or an exception
    for that tool only."""

    def __init__(
        self,
        returncode: int = 0,
        error: BaseException | None = None,
        by_tool: Mapping[str, int | BaseException] | None = None,
    ) -> None:
        self.returncode = returncode
        self.error = error
        self.by_tool = dict(by_tool or {})
        super().__init__()
        self.probes: list[dict[str, Any]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        timeout: float | None = None,
        new_process_group: bool = False,
        kill_grace: float | None = None,
        executable: str | None = None,
        kill_group_on_exit: bool = False,
        output_limit: int | None = None,
        discard_output: bool = False,
        on_started: Callable[[], None] | None = None,
        canceled: Callable[[], bool] | None = None,
    ) -> Result:
        self.probes.append({"argv": list(argv), "timeout": timeout, "env": env, "stdin": stdin})
        answer: int | BaseException | None = self.by_tool.get(Path(argv[0]).name)
        if answer is None:
            answer = self.error if self.error is not None else self.returncode
        if isinstance(answer, BaseException):
            raise answer
        return Result(tuple(argv), answer, "", "probe stderr")


def executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> str:
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def tools(tmp_path: Path) -> tuple[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return executable(bin_dir / "bwrap"), executable(bin_dir / "socat")


@pytest.fixture
def base() -> Iterator[Path]:
    """A short sticky stand-in for ``/tmp``: pytest's ``tmp_path`` is too long for the socket
    path limit."""
    path = Path(tempfile.mkdtemp(prefix="cs-sc-", dir="/tmp"))
    path.chmod(0o1777)
    try:
        yield path
    finally:
        shutil.rmtree(path)


# Platform detection


@pytest.mark.parametrize(
    ("sys_platform", "release", "expected"),
    [
        ("linux", "5.15.0-179-generic", "linux"),
        ("linux", "6.8.0-45-generic", "linux"),
        ("linux", "5.15.153.1-microsoft-standard-WSL2", "wsl2"),
        ("linux", "6.6.36.6-microsoft-standard-WSL2+", "wsl2"),
        ("linux", "4.4.0-19041-Microsoft", "wsl1"),
        ("linux", "4.4.0-22621-Microsoft", "wsl1"),
        ("darwin", "23.6.0", "macos"),
        ("win32", "10", "unsupported"),
        ("cygwin", "3.5", "unsupported"),
        ("freebsd14", "14.1-RELEASE", "unsupported"),
    ],
)
def test_detect_platform(sys_platform: str, release: str, expected: str) -> None:
    assert selfcheck.detect_platform(sys_platform, release) == expected


def test_detect_platform_defaults_to_this_host() -> None:
    assert selfcheck.detect_platform() in {"linux", "wsl2", "wsl1", "macos", "unsupported"}


# Socket path length


@pytest.mark.parametrize(("platform", "limit"), [("linux", 108), ("wsl2", 108), ("macos", 104)])
def test_socket_path_boundary(platform: str, limit: int) -> None:
    longest = limit - 1 - 1 - selfcheck.SOCKET_NAME_RESERVE  # NUL, "/", socket name
    selfcheck.check_socket_path("/" + "a" * (longest - 1), platform)
    with pytest.raises(LaunchValidationError) as info:
        selfcheck.check_socket_path("/" + "a" * longest, platform)
    assert info.value.end_reason == "socket_path_too_long"
    assert info.value.hint is not None


def test_socket_path_length_counts_bytes_not_characters() -> None:
    longest = 108 - 2 - selfcheck.SOCKET_NAME_RESERVE
    selfcheck.check_socket_path("/" + "a" * (longest - 1), "linux")
    with pytest.raises(LaunchValidationError, match="bytes"):
        selfcheck.check_socket_path("/" + "é" * ((longest + 1) // 2), "linux")


def test_socket_reserve_covers_the_observed_proxy_socket_names() -> None:
    # claude-socks-<16 hex>.sock, claude-http-<16 hex>.sock (#41 verifies these live).
    for name in ("claude-socks-" + "0" * 16 + ".sock", "claude-http-" + "0" * 16 + ".sock"):
        assert len(name) <= selfcheck.SOCKET_NAME_RESERVE


def test_the_default_tmpdir_fits() -> None:
    selfcheck.check_socket_path(tmpdir.run_path(SID), "linux")
    selfcheck.check_socket_path("/private/tmp/csession-4294967294/" + SID, "macos")


# Prerequisites: Linux and WSL2


@pytest.mark.parametrize("platform", ["linux", "wsl2"])
def test_linux_prerequisites_present_and_probe_succeeds(
    platform: str, tools: tuple[str, str]
) -> None:
    runner = FakeRunner(0)
    result = selfcheck.check_prerequisites(platform, bwrap=tools[0], socat=tools[1], gw=runner)
    assert result == Prerequisites(available=True, problem=None, hint=None)
    socat_call, call = runner.probes
    assert socat_call["argv"] == [tools[1], "-V"]
    assert socat_call["timeout"] == selfcheck.PROBE_TIMEOUT
    assert socat_call["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
    assert call["argv"][0] == tools[0]
    assert "--unshare-user" in call["argv"]
    assert "--unshare-net" in call["argv"]
    assert "--unshare-pid" in call["argv"]
    assert call["argv"][-3:] == ["/bin/sh", "-c", ":"]
    assert call["timeout"] == selfcheck.PROBE_TIMEOUT
    assert call["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}


@pytest.mark.parametrize("missing", ["bwrap", "socat", "both"])
def test_linux_missing_tool_is_unavailable_with_install_hint(
    missing: str, tools: tuple[str, str]
) -> None:
    runner = FakeRunner(0)
    bwrap = None if missing in ("bwrap", "both") else tools[0]
    socat = None if missing in ("socat", "both") else tools[1]
    result = selfcheck.check_prerequisites("linux", bwrap=bwrap, socat=socat, gw=runner)
    assert not result.available
    assert result.problem is not None
    assert result.hint is not None and "apt install bubblewrap socat" in result.hint
    assert runner.probes == []  # nothing is probed without both tools


def test_linux_relative_tool_path_is_unavailable(tools: tuple[str, str]) -> None:
    runner = FakeRunner(0)
    result = selfcheck.check_prerequisites("linux", bwrap="bwrap", socat=tools[1], gw=runner)
    assert not result.available
    assert result.problem is not None and "absolute" in result.problem
    assert runner.probes == []


def test_linux_tool_that_is_not_executable_is_unavailable(
    tmp_path: Path, tools: tuple[str, str]
) -> None:
    socat = tmp_path / "socat-noexec"
    socat.write_text("")
    socat.chmod(0o644)
    result = selfcheck.check_prerequisites(
        "linux", bwrap=tools[0], socat=str(socat), gw=FakeRunner(0)
    )
    assert not result.available


def test_linux_tool_that_is_a_directory_is_unavailable(
    tmp_path: Path, tools: tuple[str, str]
) -> None:
    result = selfcheck.check_prerequisites(
        "linux", bwrap=str(tmp_path), socat=tools[1], gw=FakeRunner(0)
    )
    assert not result.available


def test_linux_failing_probe_means_no_user_namespaces(tools: tuple[str, str]) -> None:
    result = selfcheck.check_prerequisites(
        "linux", bwrap=tools[0], socat=tools[1], gw=FakeRunner(by_tool={"bwrap": 1})
    )
    assert not result.available
    assert result.problem is not None and "namespace" in result.problem
    assert result.hint is not None and "user namespaces" in result.hint


@pytest.mark.parametrize(
    "error",
    [
        GatewayTimeout(["/usr/bin/bwrap"], 10),
        FileNotFoundError(2, "No such file or directory"),
        PermissionError(13, "Permission denied"),
    ],
)
def test_linux_probe_that_cannot_run_is_unavailable(
    tools: tuple[str, str], error: BaseException
) -> None:
    result = selfcheck.check_prerequisites(
        "linux", bwrap=tools[0], socat=tools[1], gw=FakeRunner(by_tool={"bwrap": error})
    )
    assert not result.available
    assert result.problem is not None and result.problem.startswith("bubblewrap")


@pytest.mark.parametrize(
    "answer",
    [
        1,
        GatewayTimeout(["/usr/bin/socat"], 10),
        PermissionError(13, "Permission denied"),
    ],
)
def test_linux_socat_that_does_not_run_is_unavailable(
    tools: tuple[str, str], answer: int | BaseException
) -> None:
    runner = FakeRunner(by_tool={"socat": answer})
    result = selfcheck.check_prerequisites("linux", bwrap=tools[0], socat=tools[1], gw=runner)
    assert not result.available
    assert result.problem is not None and result.problem.startswith("socat does not run: ")
    assert result.hint is not None and "socat" in result.hint
    assert [Path(call["argv"][0]).name for call in runner.probes] == ["socat"]


def test_failing_socat_with_the_real_gateway(tmp_path: Path, tools: tuple[str, str]) -> None:
    socat = executable(tmp_path / "broken-socat", "#!/bin/sh\nexit 127\n")
    result = selfcheck.check_prerequisites("linux", bwrap=tools[0], socat=socat)
    assert not result.available
    assert result.problem == "socat does not run: probe exited with status 127"


def test_probe_with_the_real_gateway(tmp_path: Path, tools: tuple[str, str]) -> None:
    # A stand-in bwrap that records its argv and exits 0, run through gateway.run.
    record = tmp_path / "argv"
    bwrap = executable(
        tmp_path / "fake-bwrap", f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\nexit 0\n'
    )
    result = selfcheck.check_prerequisites("linux", bwrap=bwrap, socat=tools[1])
    assert result.available
    assert record.read_text().splitlines()[-3:] == ["/bin/sh", "-c", ":"]


def test_failing_probe_with_the_real_gateway(tmp_path: Path, tools: tuple[str, str]) -> None:
    bwrap = executable(tmp_path / "fake-bwrap", "#!/bin/sh\necho 'no userns' >&2\nexit 1\n")
    result = selfcheck.check_prerequisites("linux", bwrap=bwrap, socat=tools[1])
    assert not result.available


# Prerequisites: WSL1, macOS, other platforms


def test_wsl1_has_no_sandbox(tools: tuple[str, str]) -> None:
    runner = FakeRunner(0)
    result = selfcheck.check_prerequisites("wsl1", bwrap=tools[0], socat=tools[1], gw=runner)
    assert not result.available
    assert result.hint is not None and "WSL2" in result.hint
    assert runner.probes == []


def test_macos_with_sandbox_exec(tmp_path: Path) -> None:
    sandbox_exec = executable(tmp_path / "sandbox-exec")
    runner = FakeRunner(0)
    result = selfcheck.check_prerequisites(
        "macos", bwrap=None, socat=None, gw=runner, sandbox_exec=sandbox_exec
    )
    assert result.available
    (call,) = runner.probes
    assert call["argv"][0] == sandbox_exec
    assert call["argv"][1] == "-p"


def test_macos_default_sandbox_exec_path_is_fixed() -> None:
    assert selfcheck.SANDBOX_EXEC == "/usr/bin/sandbox-exec"


def test_macos_without_sandbox_exec(tmp_path: Path) -> None:
    runner = FakeRunner(0)
    result = selfcheck.check_prerequisites(
        "macos", bwrap=None, socat=None, gw=runner, sandbox_exec=str(tmp_path / "missing")
    )
    assert not result.available
    assert runner.probes == []


def test_macos_failing_probe(tmp_path: Path) -> None:
    sandbox_exec = executable(tmp_path / "sandbox-exec")
    result = selfcheck.check_prerequisites(
        "macos", bwrap=None, socat=None, gw=FakeRunner(71), sandbox_exec=sandbox_exec
    )
    assert not result.available


def test_unsupported_platform_has_no_sandbox(tools: tuple[str, str]) -> None:
    runner = FakeRunner(0)
    result = selfcheck.check_prerequisites("unsupported", bwrap=tools[0], socat=tools[1], gw=runner)
    assert not result.available
    assert runner.probes == []


# The whole self-check


def run_check(
    mode: str,
    base: Path,
    tools: tuple[str | None, str | None],
    runner: FakeRunner,
    *,
    platform: str = "linux",
    create: bool = True,
    csession_id: str | None = SID,
) -> selfcheck.SelfCheck:
    return selfcheck.self_check(
        mode,
        csession_id,
        bwrap=tools[0],
        socat=tools[1],
        platform=platform,
        gw=runner,
        tmp_base=str(base),
        create=create,
    )


def test_off_checks_and_creates_nothing(base: Path, tools: tuple[str, str]) -> None:
    runner = FakeRunner(0)
    result = run_check("off", base, (None, None), runner)
    assert result == selfcheck.SelfCheck(sandbox_active=False, tmpdir=None, warnings=())
    assert runner.probes == []
    assert list(base.iterdir()) == []


@pytest.mark.parametrize("mode", ["required", "preferred"])
def test_working_sandbox_is_active_with_a_fresh_tmpdir(
    mode: str, base: Path, tools: tuple[str, str]
) -> None:
    result = run_check(mode, base, tools, FakeRunner(0))
    assert result.sandbox_active
    assert result.warnings == ()
    assert result.tmpdir == tmpdir.run_path(SID, str(base))
    assert Path(tmpdir.run_path(SID, str(base))).is_dir()


def test_required_without_prerequisites_fails_and_leaves_nothing(base: Path) -> None:
    with pytest.raises(LaunchValidationError) as info:
        run_check("required", base, (None, None), FakeRunner(0))
    assert info.value.end_reason == "sandbox_unavailable"
    assert info.value.hint is not None and "apt install" in info.value.hint
    assert list(base.iterdir()) == []


def test_required_with_failing_probe_fails(base: Path, tools: tuple[str, str]) -> None:
    with pytest.raises(LaunchValidationError) as info:
        run_check("required", base, tools, FakeRunner(by_tool={"bwrap": 1}))
    assert info.value.end_reason == "sandbox_unavailable"


def test_required_on_wsl1_fails(base: Path, tools: tuple[str, str]) -> None:
    with pytest.raises(LaunchValidationError) as info:
        run_check("required", base, tools, FakeRunner(0), platform="wsl1")
    assert info.value.end_reason == "sandbox_unavailable"


def test_preferred_without_prerequisites_warns_and_still_gets_a_tmpdir(base: Path) -> None:
    result = run_check("preferred", base, (None, None), FakeRunner(0))
    assert not result.sandbox_active
    assert result.tmpdir is not None and Path(result.tmpdir).is_dir()
    (warning,) = result.warnings
    assert "preferred" in warning and "not sandboxed" in warning


@pytest.mark.parametrize("mode", ["required", "preferred"])
def test_socket_path_too_long_fails_every_sandboxed_mode(
    mode: str, tmp_path: Path, tools: tuple[str, str]
) -> None:
    deep = tmp_path / ("d" * 100)
    deep.mkdir(mode=0o700)
    runner = FakeRunner(0)
    with pytest.raises(LaunchValidationError) as info:
        run_check(mode, deep, tools, runner)
    assert info.value.end_reason == "socket_path_too_long"
    assert list(deep.iterdir()) == []


def test_required_with_broken_socat_fails(base: Path, tools: tuple[str, str]) -> None:
    with pytest.raises(LaunchValidationError) as info:
        run_check("required", base, tools, FakeRunner(by_tool={"socat": 1}))
    assert info.value.end_reason == "sandbox_unavailable"
    assert "socat" in str(info.value)
    assert list(base.iterdir()) == []


def test_preferred_with_broken_socat_warns(base: Path, tools: tuple[str, str]) -> None:
    result = run_check("preferred", base, tools, FakeRunner(by_tool={"socat": 1}))
    assert not result.sandbox_active
    (warning,) = result.warnings
    assert "socat does not run" in warning


def test_self_check_refuses_an_unsafe_root_through_create_and_creates_nothing(
    base: Path, tools: tuple[str, str]
) -> None:
    # The caller runs tmpdir.check earlier in step 5; create validates the root again.
    root = Path(tmpdir.root_path(str(base)))
    root.mkdir(mode=0o755)
    with pytest.raises(LaunchValidationError) as info:
        run_check("required", base, tools, FakeRunner(0))
    assert info.value.end_reason == "tmpdir_unsafe"
    assert list(root.iterdir()) == []


def test_self_check_does_not_check_the_tmpdir_itself(
    base: Path, tools: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The temporary-directory slot of step 5 comes before claude_bin, claude --version and
    # claude_args: the caller runs tmpdir.check there, not self_check.
    def not_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("tmpdir.check must not be called by self_check")

    monkeypatch.setattr(tmpdir, "check", not_called)
    assert run_check("required", base, tools, FakeRunner(0)).sandbox_active


@pytest.mark.parametrize(
    ("mode", "probe_status"), [("required", 0), ("preferred", 0), ("preferred", 1)]
)
def test_self_check_warns_about_a_leftover_moved_aside(
    base: Path,
    tools: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    probe_status: int,
) -> None:
    # A leftover deeper than the removal accepts is renamed aside; when it cannot be removed
    # under its new name either, the warning names it with the command that removes it.
    monkeypatch.setattr(fsutil, "tree_depth_cap", lambda: 3)
    run = Path(tmpdir.create(SID, base=str(base)))
    path = run
    for _ in range(6):
        path = path / "d"
        path.mkdir()
    result = run_check(mode, base, tools, FakeRunner(by_tool={"socat": probe_status}))
    assert result.tmpdir == str(run)
    assert list(run.iterdir()) == []
    (stale,) = Path(tmpdir.root_path(str(base))).glob(f"{tmpdir.STALE_PREFIX}*")
    assert result.warnings[-1] == (
        f"leftover private temporary directory moved aside: {stale}; remove it: rm -rf {stale}"
    )
    assert len(result.warnings) == (1 if probe_status == 0 else 2)


def test_self_check_gives_no_leftover_warning_when_the_leftover_is_removed(
    base: Path, tools: tuple[str, str]
) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    (run / "x").write_text("x")
    result = run_check("required", base, tools, FakeRunner(0))
    assert result.warnings == ()
    assert list(run.iterdir()) == []


@pytest.mark.parametrize("csession_id", [SID, None])
def test_dry_run_does_not_touch_the_tmpdir(
    base: Path, tools: tuple[str, str], monkeypatch: pytest.MonkeyPatch, csession_id: str | None
) -> None:
    root = Path(tmpdir.root_path(str(base)))
    root.mkdir(mode=0o755)  # unsafe, but only tmpdir.check (run by the caller) looks at it

    def not_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("a dry run must not check or create the tmpdir")

    monkeypatch.setattr(tmpdir, "check", not_called)
    monkeypatch.setattr(tmpdir, "create", not_called)
    monkeypatch.setattr(tmpdir, "create_reporting", not_called)
    monkeypatch.setattr(tmpdir, "create_pinned", not_called)
    result = run_check(
        "required", base, tools, FakeRunner(0), create=False, csession_id=csession_id
    )
    assert result.sandbox_active
    assert list(root.iterdir()) == []
    assert (root.stat().st_mode & 0o777) == 0o755


def test_prerequisites_are_reported_before_socket_path(tmp_path: Path) -> None:
    deep = tmp_path / ("d" * 100)
    deep.mkdir(mode=0o700)
    with pytest.raises(LaunchValidationError) as info:
        run_check("required", deep, (None, None), FakeRunner(0))
    assert info.value.end_reason == "sandbox_unavailable"


@pytest.mark.parametrize("csession_id", [SID, None])
def test_dry_run_creates_nothing(
    base: Path, tools: tuple[str, str], csession_id: str | None
) -> None:
    result = run_check(
        "required", base, tools, FakeRunner(0), create=False, csession_id=csession_id
    )
    assert result.sandbox_active
    assert list(base.iterdir()) == []
    if csession_id is None:
        assert result.tmpdir is None
    else:
        assert result.tmpdir == tmpdir.run_path(SID, str(base))


def test_dry_run_still_reports_failures(tmp_path: Path, tools: tuple[str, str]) -> None:
    deep = tmp_path / ("d" * 100)
    deep.mkdir(mode=0o700)
    with pytest.raises(LaunchValidationError) as info:
        run_check("required", deep, tools, FakeRunner(0), create=False, csession_id=None)
    assert info.value.end_reason == "socket_path_too_long"


def test_unknown_mode_is_a_programming_error(base: Path, tools: tuple[str, str]) -> None:
    with pytest.raises(ValueError):
        run_check("sometimes", base, tools, FakeRunner(0))


def test_real_run_needs_a_session_id(base: Path, tools: tuple[str, str]) -> None:
    with pytest.raises(ValueError):
        run_check("required", base, tools, FakeRunner(0), csession_id=None)


# End reasons are documented


def test_end_reasons_exist_in_the_interface_and_the_outcome_schema() -> None:
    schema = json.loads((REPO / "schemas/cli/outcome.json").read_text(encoding="utf-8"))
    launch = set(schema["$defs"]["launch_end_reason"]["enum"])
    start = set(schema["$defs"]["start_failure_end_reason"]["enum"])
    interface = (REPO / "docs/interface.md").read_text(encoding="utf-8")
    for reason in selfcheck.END_REASONS:
        assert reason in launch
        assert reason in start
        assert f"`{reason}`" in interface
