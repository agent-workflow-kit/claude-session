"""Argv-only usage rules (interface.md, Flag rules and the command sections): exit 2."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from claude_session import cli
from claude_session.cli import main
from claude_session.errors import UsageError

from .schemas import validate

SID = "cs-abcd2345"

VIOLATIONS: list[tuple[list[str], str]] = [
    # Global flags
    (["list", "--verbose", "--quiet"], "--verbose and --quiet are mutually exclusive"),
    # start
    (["start"], "start requires --json"),
    (["start", "--json", "--fork"], "--fork requires --resume"),
    (["start", "--json", "--reset", "model"], "--reset requires --resume"),
    (["start", "--json", "--allow-tainted"], "--allow-tainted requires --resume"),
    (["start", "--json", "--override-escalation"], "--override-escalation requires --resume"),
    (["start", "--json", "--mode", "headless"], "needs --skill or --prompt-file"),
    # exec --session-id
    (["exec", "--session-id", SID, "--kind", "review"], "--kind cannot be used with --session-id"),
    (["exec", "--session-id", SID, "--config", "c"], "--config cannot be used with --session-id"),
    (
        ["exec", "--session-id", SID, "--no-auto-exit"],
        "--[no-]auto-exit cannot be used with --session-id",
    ),
    (["exec", "--session-id", SID, "--label", "a=b"], "--label cannot be used with"),
    (["exec", "--run", "2"], "--run requires --session-id"),
    (["exec", "--mode", "headless", "--skill", "s", "--run", "1"], "--run requires --session-id"),
    (["exec", "--session-id", SID, "--run", "0"], "--run must be a positive integer"),
    (["exec", "--session-id", SID, "--run", "-1"], "--run must be a positive integer"),
    (["exec", "--session-id", SID, "--run", "x"], "--run"),
    (["exec", "--session-id", SID, "--run", "1.5"], "--run"),
    (["exec", "--session-id", SID, "--run", "9007199254740992"], "--run must be a positive"),
    # exec new session
    (["exec", "--mode", "headless"], "a headless new session needs --skill or --prompt-file"),
    # --reset with the flag it resets
    (["resume", SID, "--reset", "model", "--model", "m"], "--reset model cannot be combined"),
    (
        ["resume", SID, "--reset", "auto-exit", "--auto-exit"],
        "--reset auto-exit cannot be combined with --[no-]auto-exit",
    ),
    (["resume", SID, "--reset", "plugin-dir", "--plugin-dir", "/p"], "--reset plugin-dir"),
    (
        ["start", "--json", "--resume", SID, "--reset", "timeout", "--timeout", "5m"],
        "--reset timeout cannot be combined",
    ),
    # hooks
    (["exec", "--hook-timeout", "pre-launch=5s"], "needs a --hook pre-launch="),
    (
        ["exec", "--hook", "post-state-change=x", "--hook-timeout", "pre-launch=5s"],
        "needs a --hook pre-launch=",
    ),
    (["exec", "--hook", "on-start=x"], "--hook: unknown hook name 'on-start'"),
    (["exec", "--hook", "pre-launch"], "--hook expects <name>=<value>"),
    (
        ["exec", "--hook", "pre-launch=x", "--hook-timeout", "post=5s"],
        "--hook-timeout: unknown hook name 'post'",
    ),
    # fork and escalation override
    (["resume", SID, "--fork", "--override-escalation"], "cannot be combined with --fork"),
    (
        ["start", "--json", "--resume", SID, "--fork", "--override-escalation"],
        "cannot be combined with --fork",
    ),
    # durations
    (["exec", "--timeout", "0"], "--timeout must be positive"),
    (["exec", "--timeout", "0s"], "--timeout must be positive"),
    (["resume", SID, "--timeout", "0.0m"], "--timeout must be positive"),
    (["start", "--json", "--timeout", "-5"], "--timeout: invalid duration"),
    (["wait", SID, "--timeout", "0"], "--timeout must be positive"),
    (["wait", SID, "--timeout", "0ms"], "--timeout must be positive"),
    # the full duration grammar (interface.md, Identifiers: Durations)
    (["exec", "--timeout", ".5"], "--timeout: invalid duration"),
    (["exec", "--timeout", "5."], "--timeout: invalid duration"),
    (["exec", "--timeout", "1e3"], "--timeout: invalid duration"),
    (["exec", "--timeout", "+5"], "--timeout: invalid duration"),
    (["exec", "--timeout", "1_000"], "--timeout: invalid duration"),
    (["exec", "--timeout", "5 m"], "--timeout: invalid duration"),
    (["exec", "--timeout", "5M"], "--timeout: invalid duration"),
    (["exec", "--timeout", "\u0665s"], "--timeout: invalid duration"),  # ARABIC-INDIC FIVE
    (["exec", "--timeout", "NONE"], "--timeout: invalid duration"),
    (["exec", "--timeout", ""], "--timeout: invalid duration"),
    (["exec", "--timeout", "36501d"], "--timeout: invalid duration: must be at most 36500d"),
    (["exec", "--timeout", "9" * 400], "--timeout: invalid duration: must be at most 36500d"),
    (["resume", SID, "--timeout", "soon"], "--timeout: invalid duration"),
    (["wait", SID, "--timeout", "5x"], "--timeout: invalid duration"),
    (
        ["exec", "--hook", "pre-launch=x", "--hook-timeout", "pre-launch=0"],
        "--hook-timeout pre-launch must be positive",
    ),
    (
        ["exec", "--hook", "pre-launch=x", "--hook-timeout", "pre-launch=none"],
        "--hook-timeout pre-launch: invalid duration",
    ),
    (
        ["exec", "--hook", "pre-launch=x", "--hook-timeout", "pre-launch="],
        "--hook-timeout pre-launch: invalid duration",
    ),
    (["stop", SID, "--grace", "-1"], "--grace: invalid duration"),
    (["stop", SID, "--grace", "none"], "--grace: invalid duration"),
    (["stop", SID, "--grace", "1w"], "--grace: invalid duration"),
    (["forget", "--older-than", "7 days"], "--older-than: invalid duration"),
    (["forget", "--older-than", "none"], "--older-than: invalid duration"),
    (["list", "--since", "yesterday"], "--since: expected an ISO 8601 time or a duration"),
    (["list", "--since", "1e3"], "--since: expected an ISO 8601 time or a duration"),
    (["list", "--since", "2026-13-01"], "--since: expected an ISO 8601 time or a duration"),
    (["export", "--since", "5 m"], "--since: expected an ISO 8601 time or a duration"),
    (["exec", "--env-allow", "*"], "--env-allow: invalid variable name"),
    (["exec", "--env-allow", "GH_*"], "--env-allow: invalid variable name"),
    (["exec", "--env-allow", "a b"], "--env-allow: invalid variable name"),
    (["exec", "--env-allow", "1X"], "--env-allow: invalid variable name"),
    (["exec", "--env-allow", "é"], "--env-allow: invalid variable name"),
    (["exec", "--env-allow", "A=B"], "--env-allow: invalid variable name"),
    (["exec", "--env-allowlist-only", "*"], "--env-allowlist-only: invalid variable name"),
    (["exec", "--env-allowlist-only", "GH_*"], "--env-allowlist-only: invalid variable name"),
    (["exec", "--env-allowlist-only", "a b"], "--env-allowlist-only: invalid variable name"),
    (["exec", "--env-allowlist-only", "1X"], "--env-allowlist-only: invalid variable name"),
    (["exec", "--env-allowlist-only", "é"], "--env-allowlist-only: invalid variable name"),
    (["exec", "--env-allowlist-only", "A=B"], "--env-allowlist-only: invalid variable name"),
    (["exec", "--env-allow", ""], "--env-allow: invalid variable name"),
    # bounds
    (["exec", "--max-turns", "0"], "--max-turns must be between 1 and 9007199254740991"),
    (
        ["exec", "--max-turns", "9007199254740992"],
        "--max-turns must be between 1 and 9007199254740991",
    ),
    (["exec", "--model=-x"], "--model must not start with '-'"),
    (["list", "--limit", "0"], "--limit must be at least 1"),
    (["cron", "install", "--interval", "0"], "--interval must be between 1 and 59"),
    (["cron", "install", "--interval", "60"], "--interval must be between 1 and 59"),
    # input files
    (["exec", "--input-file", "-"], "--input-file - is not supported"),
    (["exec", "--input-file", "a/x.txt", "--input-file", "b/x.txt"], "duplicate --input-file"),
    (["exec", "--input-file", "a b.txt"], "--input-file name 'a b.txt' must match"),
    (["start", "--json", "--input-file", "dir/"], "--input-file name '' must match"),
    (["exec", "--input-file", "x.txt\n"], "--input-file name 'x.txt\\n' must match"),
    (["exec", "--input-file", "a/.."], "--input-file name '..' must match"),
    # setting sources
    (["exec", "--setting-sources", "user,global"], "unknown source 'global'"),
    # report
    (["report", "done"], "report done requires a non-empty --summary or --summary-file"),
    (["report", "done", "--summary", ""], "requires a non-empty --summary"),
    (["report", "done", "--summary", " \t "], "requires a non-empty --summary"),
    (["report", "blocked", "--summary", "x"], "report blocked requires --reason-file"),
    (["report", "failed"], "report failed requires --reason-file"),
    (["report", "needs-input"], "report needs-input requires --reason-file"),
    # transcript
    (["transcript", SID, "--raw", "--json"], "--raw cannot be combined with --json"),
    # forget
    (["forget", SID, "--older-than", "7d"], "--older-than is for bulk mode"),
    (["forget", SID, "--state", "lost"], "--state is for bulk mode"),
    (["forget", SID, "--kind", "review"], "--kind is for bulk mode"),
    (["forget", SID, "--label", "a=b"], "--label is for bulk mode"),
    (["forget"], "forget needs a session id or --older-than"),
    (["forget", "--state", "lost", "--kind", "review"], "forget needs a session id or"),
    # export
    (["export", "--action", "launch"], "--action: unknown action 'launch'"),
    # session id prefixes: at least 4 characters after cs-
    (["status", "abc"], "session id 'abc' is too short"),
    (["status", "cs-abc", "--json"], "session id 'cs-abc' is too short"),
    (["resume", "cs-"], "session id 'cs-' is too short"),
    (["status", "CS-AB"], "session id 'CS-AB' is too short"),
    (["stop", "ab"], "at least 4 characters"),
    (["wait", "abc"], "at least 4 characters"),
    (["label", "add", "abc", "a=b"], "at least 4 characters"),
    (["label", "remove", "abc", "a=b"], "at least 4 characters"),
    (["transcript", "abc"], "at least 4 characters"),
    (["forget", "abc"], "at least 4 characters"),
    (["exec", "--session-id", "abc"], "--session-id: session id 'abc' is too short"),
    (["start", "--json", "--resume", "abc"], "--resume: session id 'abc' is too short"),
    (["list", "--forked-from", "cs-ab"], "--forked-from: session id 'cs-ab' is too short"),
    # kind names
    (["exec", "--kind", "Review"], "--kind: invalid kind name 'Review'"),
    (["start", "--json", "--kind=-x"], "--kind: invalid kind name '-x'"),
    (["resume", SID, "--kind", "a b"], "--kind: invalid kind name"),
    (["list", "--kind", "a.b"], "--kind: invalid kind name"),
    (["list", "--launch-kind", "A"], "--launch-kind: invalid kind name"),
    (["list", "--run-kind", "A"], "--run-kind: invalid kind name"),
    (["kinds", "--kind", "k" * 65], "--kind: invalid kind name"),
    (["forget", "--older-than", "7d", "--kind", "_k"], "--kind: invalid kind name"),
    # skill names
    (["exec", "--skill", ":plan"], "--skill: invalid skill name ':plan'"),
    (["start", "--json", "--skill", "a b"], "--skill: invalid skill name"),
    (["resume", SID, "--skill", "/review"], "--skill: invalid skill name"),
    # repo ids
    (["exec", "--repo", "https://example.org/r"], "--repo: invalid repo id"),
    (["start", "--json", "--repo", "Example.org/r"], "--repo: invalid repo id"),
    (["resume", SID, "--repo", "example.org"], "--repo: invalid repo id"),
    (["list", "--repo", "example.org/../r"], "--repo: invalid repo id"),
    (["kinds", "--repo", "example.org/r/"], "--repo: invalid repo id"),
    # labels
    (["exec", "--label", "novalue"], "--label: invalid label 'novalue'"),
    (["start", "--json", "--label", "Key=v"], "--label: invalid label 'Key=v'"),
    (["resume", SID, "--label", "k=a b"], "--label: invalid label"),
    (["list", "--label", "k=$(x)"], "--label: invalid label"),
    (["forget", "--older-than", "7d", "--label", "=v"], "--label: invalid label"),
    (["label", "add", SID, "a=b", "c d=e"], "invalid label 'c d=e'"),
    (["label", "remove", SID, "k"], "invalid label 'k'"),
]


@pytest.mark.parametrize(("argv", "message"), VIOLATIONS)
def test_violation_exits_2(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    captured = capsys.readouterr()
    if "--json" in argv:
        assert json.loads(captured.out)["error"]["code"] == "usage"
    else:
        assert captured.out == ""
    assert message in captured.err


def test_short_prefix_with_json_prints_golden_shaped_usage_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["status", "abc", "--json"]) == 2
    envelope = json.loads(capsys.readouterr().out)
    validate(envelope, "claude-session/cli/error.json")
    assert envelope["error"]["code"] == "usage"


def test_violation_with_json_prints_usage_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["wait", SID, "--timeout", "0", "--json"]) == 2
    envelope = json.loads(capsys.readouterr().out)
    validate(envelope, "claude-session/cli/error.json")
    assert envelope["error"]["code"] == "usage"


ALLOWED: list[list[str]] = [
    ["exec", "--session-id", SID, "--json", "--dry-run", "--outcome-file", "o"],
    ["exec", "--session-id", SID, "--verbose", "--yes"],
    ["exec", "--session-id", SID, "--quiet"],
    ["exec", "--session-id", SID, "--run", "2", "--json", "--dry-run"],
    ["exec", "--session-id", SID, "--run", "9007199254740991"],
    ["exec", "--mode", "headless", "--skill", "s"],
    ["exec", "--mode", "headless", "--prompt-file", "p.md"],
    ["exec", "--timeout", "none"],
    ["exec", "--timeout", "1.5s"],
    ["exec", "--hook", "pre-launch=a", "--hook-timeout", "pre-launch=5s"],
    ["exec", "--setting-sources", ""],
    ["exec", "--setting-sources", "user,project,local"],
    ["exec", "--input-file", "a/x.txt", "--input-file", "a/y.txt"],
    ["exec", "--max-turns", "1"],
    ["exec", "--max-turns", "9007199254740991"],
    ["exec", "--model", "m-1"],
    ["start", "--json", "--resume", SID, "--mode", "headless"],
    ["start", "--json", "--resume", SID, "--reset", "model", "--kind", "review"],
    ["resume", SID, "--fork", "--allow-tainted"],
    ["resume", SID, "--override-escalation"],
    ["wait", SID, "--timeout", "none"],
    ["wait", SID, "--timeout", "90"],
    ["exec", "--timeout", "36500d"],
    ["exec", "--timeout", "500ms"],
    ["exec", "--hook", "pre-launch=a", "--hook-timeout", "pre-launch=0.5"],
    ["stop", SID, "--grace", "0"],
    ["stop", SID, "--grace", "2.5s"],
    ["forget", "--older-than", "0"],
    ["forget", "--older-than", "30d"],
    ["list", "--since", "2h"],
    ["list", "--since", "3600"],
    ["list", "--since", "2026-10-30"],
    ["list", "--since", "2026-10-30T10:00:00+00:00"],
    ["export", "--since", "2026-10-30T10:00:00Z"],
    ["export", "--since", "7d"],
    ["exec", "--env-allow", "GH_TOKEN", "--env-allow", "_x1"],
    ["exec", "--env-allowlist-only", ""],
    ["exec", "--env-allowlist-only", "A", "--env-allowlist-only", ""],
    ["report", "done", "--summary-file", "-"],
    ["report", "blocked", "--reason-file", "r.md"],
    ["transcript", SID, "--raw"],
    ["transcript", SID, "--paths", "--json"],
    ["forget", SID],
    ["forget", "--older-than", "7d", "--state", "lost", "--kind", "k", "--label", "a=b"],
    ["list", "--limit", "1"],
    ["cron", "install", "--interval", "1"],
    ["cron", "install", "--interval", "59"],
    ["export", "--action", "resume_tainted", "--action", "sandbox_lowered"],
    ["status", "abcd"],
    ["status", "cs-abcd"],
    ["status", "CS-ABCD"],
    ["status", "zzzzzzzzzz"],  # malformed but long enough: unknown (exit 10) at lookup
    ["resume", "abcd", "--kind", "review-respond", "--skill", "plan:make"],
    ["exec", "--session-id", "abcd"],
    ["start", "--json", "--resume", "cs-abcd", "--label", "pr=example.org/g/r#42"],
    ["list", "--forked-from", "abcd", "--launch-kind", "k_1", "--run-kind", "0"],
    ["list", "--label", "a=", "--label", "expr=a=b", "--repo", "example.org:8443/g/r"],
    ["kinds", "--kind", "review", "--repo", "example.org/g/r"],
    ["exec", "--repo", "localhost/r", "--label", "owner=me@example.org"],
    ["label", "add", "abcd", "a=b", "c="],
    ["label", "remove", SID, "x.y_z-1=A,b+c:d"],
    ["forget", "abcd"],
    ["verify-secret", "abc"],  # needs the full id; mismatch is the handler's exit 11
]


@pytest.mark.parametrize("argv", ALLOWED)
def test_allowed_combination_reaches_the_handler(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = cli.parse(cli.build_parser(), argv).handler
    monkeypatch.setitem(cli.HANDLERS, handler, lambda args: 77)
    assert main(argv) == 77, capsys.readouterr().err


# --since threshold (interface.md, list): the duration grammar first, then an ISO 8601 time


def test_since_threshold_reads_digits_as_seconds() -> None:
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    assert cli.since_threshold("20240101", now=now) == now - timedelta(seconds=20240101)
    assert cli.since_threshold("2h", now=now) == now - timedelta(hours=2)


def test_since_threshold_reads_iso_times() -> None:
    now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    offset = cli.since_threshold("2026-10-30T10:00:00+02:00", now=now)
    assert offset == datetime(2026, 10, 30, 8, 0, tzinfo=UTC)
    assert cli.since_threshold("2026-10-30", now=now) == datetime(2026, 10, 30, tzinfo=UTC)
    assert cli.since_threshold("2026-10-30T10:00:00Z", now=now).utcoffset() == timedelta(0)


def test_since_threshold_rejects_other_text() -> None:
    with pytest.raises(UsageError):
        cli.since_threshold("yesterday")
