"""The launch's ``claude --version`` check (interface.md, Launch order step 5 and Path rules:
Claude Code version; ``claude_too_old``). The probe's environment and process group are
tested with ``kinds`` in ``test_dryrun``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_session import claude_version, launch, pathrules
from claude_session.errors import LaunchValidationError

from .fake_gateway import FakeGateway

NOTHING = pathrules.SessionWritable([])
CLAUDE = pathrules.ResolvedTool("/opt/claude/bin/claude-real", "claude")
ENVIRON = {"PATH": "/usr/bin:/bin", "HOME": "/home/x"}


def gateway(**answer: Any) -> FakeGateway:
    gw = FakeGateway()
    gw.on(["claude", "--version"], **answer)
    return gw


def require(gw: FakeGateway, minimum: str = "2.1.312") -> str:
    return claude_version.require(CLAUDE, ENVIRON, gw, minimum, writable=NOTHING)


# Parsing and comparing, hostile output included


@pytest.mark.parametrize(
    "text",
    [
        "٢.١.٣١٢ (Claude Code)",  # non-ASCII digits are not a version
        "２.1.312",
        "2.1.312-beta",
        "2.1.312\x00",
        "-2.1.312",
        "+2.1.312",
        "2.1.312.",
        ".2.1",
        "2,1,312",
        "1e3",
        "2.1.312" + "​",
    ],
)
def test_odd_version_text_is_no_version(text: str) -> None:
    assert claude_version.parse(text) is None
    assert claude_version.at_least(text, "1") is None


def test_a_version_with_more_digits_than_int_accepts_is_compared_not_crashed() -> None:
    """Python refuses ``int()`` of more than 4300 digits; the comparison must not raise."""
    huge = "9" * 5000
    assert claude_version.at_least(huge, "2.1.312") is True
    assert claude_version.at_least("2.1.312", huge) is False
    assert claude_version.at_least(huge, huge) is True
    assert claude_version.at_least(huge + "0", huge) is True
    assert claude_version.at_least("2." + huge, "2." + huge + "1") is False


@pytest.mark.parametrize(
    ("text", "minimum", "ok"),
    [
        ("002.01.312", "2.1.312", True),  # leading zeros are the same number
        ("2.1.312", "002.001.0312", True),
        ("2.1.311", "002.001.0312", False),
        ("2.1.312.0.0", "2.1.312", True),
        ("2.1.312", "2.1.312.0.1", False),
        ("0", "0", True),
        ("10", "9", True),
        ("9", "10", False),
    ],
)
def test_at_least_compares_numerically(text: str, minimum: str, ok: bool) -> None:
    assert claude_version.at_least(text, minimum) is ok


def test_a_huge_parse_is_none_not_an_error() -> None:
    assert claude_version.parse("9" * 5000) is None


# The probe is bounded


def test_the_probe_keeps_a_bounded_output() -> None:
    gw = gateway(stdout="2.1.314 (Claude Code)\n" + "x" * 10)
    assert require(gw) == "2.1.314 (Claude Code)"
    (call,) = gw.calls
    assert call.output_limit == claude_version.MAX_OUTPUT
    assert call.timeout == claude_version.PROBE_TIMEOUT


# require: the launch check


def test_a_new_enough_claude_passes_and_returns_its_version() -> None:
    assert require(gateway(stdout="2.1.312 (Claude Code)\n")) == "2.1.312 (Claude Code)"
    assert require(gateway(stdout="3.0 (Claude Code)\n")) == "3.0 (Claude Code)"


def test_an_older_claude_fails_claude_too_old_naming_both_versions() -> None:
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(stdout="2.1.311 (Claude Code)\n"))
    error = info.value
    assert error.end_reason == "claude_too_old"
    assert "2.1.311 (Claude Code)" in error.message and "2.1.312" in error.message
    assert CLAUDE.path in error.message
    message = launch.failure_message(error)
    assert "upgrade Claude Code" in message and "min_claude_version" in message


def test_the_minimum_comes_from_the_argument() -> None:
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(stdout="2.1.312 (Claude Code)\n"), minimum="2.1.313")
    assert info.value.end_reason == "claude_too_old"
    assert require(gateway(stdout="2.0\n"), minimum="1.9.9") == "2.0"


@pytest.mark.parametrize(
    "stdout",
    ["Claude Code\n", "v2.1.312\n", "2.1.312-beta\n", "\x1b[31m\n", "٢.١.٣١٢\n"],
)
def test_a_version_that_cannot_be_read_fails_closed(stdout: str) -> None:
    """Conservative: a version that cannot be compared is not known to be new enough."""
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(stdout=stdout))
    assert info.value.end_reason == "claude_too_old"
    assert "no version" in info.value.message


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({"returncode": 1, "stdout": "2.1.314"}, "exited with status 1"),
        (
            {"returncode": 127, "stderr": "env: 'nodez': No such file or directory\nmore\n"},
            "exited with status 127; stderr: \"env: 'nodez': No such file or directory\"",
        ),
        ({"stdout": ""}, "printed nothing"),
        ({"stdout": " \n", "stderr": "boom\n"}, "printed nothing; stderr: 'boom'"),
        ({"error": PermissionError(13, "Permission denied")}, "could not start: Permission denied"),
        ({"error": OSError(8, "Exec format error")}, "could not start: Exec format error"),
        ({"timeout": True}, "did not finish within 10 s"),
    ],
)
def test_a_claude_that_cannot_run_its_version_fails_claude_not_found_naming_why(
    answer: dict[str, Any], reason: str
) -> None:
    """A ``claude_bin`` that is not a working program (not a valid binary, a missing
    interpreter, a hang) fails here rather than at spawn (``launch_interrupted``), saying
    which failure it was, with its own hint (not "install Claude Code")."""
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(**answer))
    error = info.value
    assert error.end_reason == "claude_not_found"
    assert CLAUDE.path in error.message and "--version" in error.message
    assert reason in error.message
    assert error.hint == claude_version.NOT_RUN_HINT
    assert "install Claude Code" not in launch.failure_message(error)


def test_the_stderr_line_shown_is_stripped_of_control_characters_and_capped() -> None:
    stderr = "\x1b]0;pwned\x07" + "z" * 5000 + "\n"
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(returncode=2, stderr=stderr))
    message = info.value.message
    assert "\x1b" not in message and "\x07" not in message
    assert "z" * claude_version.MAX_TEXT not in message
    assert "z" * (claude_version.MAX_TEXT - 20) in message


def test_output_without_a_version_has_its_own_hint() -> None:
    """``min_claude_version`` cannot help a program whose output holds no version."""
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(stdout="Claude Code\n"))
    assert info.value.end_reason == "claude_too_old"
    assert info.value.hint == claude_version.NO_VERSION_HINT
    assert "min_claude_version" not in launch.failure_message(info.value)
    with pytest.raises(LaunchValidationError) as older:
        require(gateway(stdout="2.1.311 (Claude Code)\n"))
    assert older.value.hint == claude_version.TOO_OLD_HINT


@pytest.mark.parametrize(
    "stdout",
    [
        "2.1.3\x0012\n",  # control characters are not removed before parsing
        "\n\n2.1.312 (Claude Code)\n",  # the first line, literally
        " 2.1.312 (Claude Code)\n",  # no leading whitespace
        "2.1.312\xa0(Claude Code)\n",  # only a space or a tab ends the version
    ],
)
def test_the_version_is_read_from_the_raw_first_line(stdout: str) -> None:
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(stdout=stdout))
    assert info.value.end_reason == "claude_too_old"
    assert "no version" in info.value.message


def test_a_tab_ends_the_version_rather_than_vanishing() -> None:
    """``2.1.3<TAB>12`` is version 2.1.3, not 2.1.312."""
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(stdout="2.1.3\t12 (Claude Code)\n"))
    assert info.value.end_reason == "claude_too_old"
    assert "'2.1.3 12 (Claude Code)'" in info.value.message
    assert info.value.hint == claude_version.TOO_OLD_HINT


@pytest.mark.parametrize(
    ("stdout", "shown"),
    [
        ("2.1.400\t(Claude Code)\n", "2.1.400 (Claude Code)"),
        ("2.1.400\r\n", "2.1.400"),
        ("2.1.400", "2.1.400"),
    ],
)
def test_a_version_followed_by_a_tab_or_the_line_end_is_read(stdout: str, shown: str) -> None:
    assert require(gateway(stdout=stdout)) == shown


def test_the_message_never_carries_control_characters_or_unbounded_text() -> None:
    hostile = "1.0 \x1b]0;pwned\x07" + "y" * 5000
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(stdout=hostile))
    message = launch.failure_message(info.value)
    assert "\x1b" not in message and "\x07" not in message
    assert len(info.value.message) < 2 * claude_version.MAX_TEXT + len(CLAUDE.path) + 200


def test_with_no_path_entry_left_nothing_runs_and_it_fails_closed(tmp_path: Any) -> None:
    gw = FakeGateway()
    writable = pathrules.SessionWritable([str(tmp_path)])
    with pytest.raises(LaunchValidationError) as info:
        claude_version.require(CLAUDE, {"PATH": str(tmp_path)}, gw, "2.1.312", writable=writable)
    assert gw.calls == []
    assert info.value.end_reason == "claude_not_found"
    assert "PATH" in info.value.message


# The interpreter of a script claude (one level: its #! line)

INTERP = "cs-test-interp"  # a name no host has, so no test depends on the host


def script(path: Path, first_line: bytes) -> pathrules.ResolvedTool:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(first_line + b"exit 0\n")
    path.chmod(0o755)
    return pathrules.ResolvedTool(str(path), "claude")


def interp(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / INTERP
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def check(claude: pathrules.ResolvedTool, entries: list[str], writable: Any) -> None:
    claude_version.check_interpreter(claude, entries, writable)


@pytest.mark.parametrize(
    "first", [b"\x7fELF\x02\x01\x01\n", b"#!/bin/sh\n", b"#! /bin/sh -e\n", b""]
)
def test_a_binary_or_a_safe_interpreter_passes(tmp_path: Path, first: bytes) -> None:
    writable = pathrules.SessionWritable([str(tmp_path / "w")])
    check(script(tmp_path / "claude", first), ["/usr/bin"], writable)


def test_an_absolute_interpreter_under_a_writable_path_is_refused(tmp_path: Path) -> None:
    planted = interp(tmp_path / "w")
    writable = pathrules.SessionWritable([str(tmp_path / "w")])
    claude = script(tmp_path / "safe" / "claude", f"#!{planted}\n".encode())
    with pytest.raises(LaunchValidationError) as info:
        check(claude, [], writable)
    assert info.value.end_reason == "unsafe_path"
    assert "interpreter" in info.value.message


def test_an_absolute_interpreter_reached_through_a_writable_hop_is_refused(
    tmp_path: Path,
) -> None:
    real = interp(tmp_path / "real")
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "link").symlink_to(real.parent)
    writable = pathrules.SessionWritable([str(tmp_path / "w")])
    claude = script(tmp_path / "safe" / "claude", f"#!{tmp_path}/w/link/{INTERP} -x\n".encode())
    with pytest.raises(LaunchValidationError) as info:
        check(claude, [], writable)
    assert info.value.end_reason == "unsafe_path"


def test_an_env_interpreter_symlinked_into_a_writable_path_is_refused(tmp_path: Path) -> None:
    """The repro: the ``PATH`` entry itself is safe, the interpreter in it is a symlink
    into the session's cwd."""
    planted = interp(tmp_path / "cwd")
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / INTERP).symlink_to(planted)
    writable = pathrules.SessionWritable([str(tmp_path / "cwd")])
    claude = script(tmp_path / "tools" / "claude", f"#!/usr/bin/env {INTERP}\n".encode())
    with pytest.raises(LaunchValidationError) as info:
        check(claude, [str(safe)], writable)
    assert info.value.end_reason == "unsafe_path"
    assert INTERP in info.value.message


def test_an_env_interpreter_found_safely_or_not_at_all_passes(tmp_path: Path) -> None:
    safe = interp(tmp_path / "safe").parent
    writable = pathrules.SessionWritable([str(tmp_path / "cwd")])
    claude = script(tmp_path / "tools" / "claude", f"#!/usr/bin/env  {INTERP} \n".encode())
    check(claude, [str(safe)], writable)
    check(claude, [str(tmp_path / "empty")], writable)  # not found: the probe then fails


@pytest.mark.parametrize(
    "first",
    [
        b"#!/usr/bin/env -S cs-test-interp --flag\n",
        b"#!/usr/bin/env -i cs-test-interp\n",
        b"#!/usr/bin/env A=1\n",
        b"#!/usr/bin/env cs-test-interp extra\n",
        b"#!/usr/bin/env\n",
        b"#!/bin/env cs-test-interp\tx\n",
        b"#!cs-test-interp\n",
        b"#!\n",
        b"#!/bin/sh" + b" " * 300 + b"\n",  # no end of line within the bytes the kernel reads
        b"#!/bin/\xffsh\n",
    ],
)
def test_a_line_that_cannot_be_checked_fails_closed(tmp_path: Path, first: bytes) -> None:
    writable = pathrules.SessionWritable([str(tmp_path / "w")])
    with pytest.raises(LaunchValidationError) as info:
        check(script(tmp_path / "claude", first), ["/usr/bin"], writable)
    assert info.value.end_reason == "unsafe_path"
    assert "cannot be checked" in info.value.message


def test_an_unreadable_claude_fails_closed(tmp_path: Path) -> None:
    missing = pathrules.ResolvedTool(str(tmp_path / "gone"), "claude")
    with pytest.raises(LaunchValidationError) as info:
        check(missing, [], pathrules.SessionWritable([]))
    assert info.value.end_reason == "unsafe_path"
