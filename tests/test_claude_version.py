"""The launch's ``claude --version`` check (interface.md, Launch order step 5 and Path rules:
Claude Code version; ``claude_too_old``). The probe's environment and process group are
tested with ``kinds`` in ``test_dryrun``."""

from __future__ import annotations

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
    assert call.max_output == claude_version.MAX_OUTPUT
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
    "answer",
    [
        {"returncode": 1, "stdout": "2.1.314"},
        {"returncode": 127, "stderr": "env: 'node': No such file or directory"},
        {"stdout": ""},
        {"error": PermissionError("not executable")},
        {"error": OSError(8, "Exec format error")},
        {"timeout": True},
    ],
)
def test_a_claude_that_cannot_run_its_version_fails_claude_not_found(
    answer: dict[str, Any],
) -> None:
    """A ``claude_bin`` that is not a working program (not a valid binary, a missing
    interpreter, a hang) fails here rather than at spawn (``launch_interrupted``)."""
    with pytest.raises(LaunchValidationError) as info:
        require(gateway(**answer))
    assert info.value.end_reason == "claude_not_found"
    assert CLAUDE.path in info.value.message and "--version" in info.value.message


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
