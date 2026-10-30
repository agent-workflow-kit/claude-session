"""Argv-only usage rules (interface.md, Flag rules and the command sections): exit 2."""

from __future__ import annotations

import json

import pytest

from claude_session.cli import main

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
    (["start", "--json", "--timeout", "-5"], "--timeout must be positive"),
    (["wait", SID, "--timeout", "0"], "--timeout must be positive"),
    (["wait", SID, "--timeout", "0ms"], "--timeout must be positive"),
    # bounds
    (["exec", "--max-turns", "0"], "--max-turns must be at least 1"),
    (["list", "--limit", "0"], "--limit must be at least 1"),
    (["cron", "install", "--interval", "0"], "--interval must be between 1 and 59"),
    (["cron", "install", "--interval", "60"], "--interval must be between 1 and 59"),
    # input files
    (["exec", "--input-file", "-"], "--input-file - is not supported"),
    (["exec", "--input-file", "a/x.txt", "--input-file", "b/x.txt"], "duplicate --input-file"),
    (["exec", "--input-file", "a b.txt"], "--input-file name 'a b.txt' must match"),
    (["start", "--json", "--input-file", "dir/"], "--input-file name '' must match"),
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


def test_violation_with_json_prints_usage_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["wait", SID, "--timeout", "0", "--json"]) == 2
    envelope = json.loads(capsys.readouterr().out)
    validate(envelope, "claude-session/cli/error.json")
    assert envelope["error"]["code"] == "usage"


ALLOWED: list[list[str]] = [
    ["exec", "--session-id", SID, "--json", "--dry-run", "--outcome-file", "o"],
    ["exec", "--session-id", SID, "--verbose", "--yes"],
    ["exec", "--session-id", SID, "--quiet"],
    ["exec", "--mode", "headless", "--skill", "s"],
    ["exec", "--mode", "headless", "--prompt-file", "p.md"],
    ["exec", "--timeout", "none"],
    ["exec", "--timeout", "1.5s"],
    ["exec", "--hook", "pre-launch=a", "--hook-timeout", "pre-launch=5s"],
    ["exec", "--setting-sources", ""],
    ["exec", "--setting-sources", "user,project,local"],
    ["exec", "--input-file", "a/x.txt", "--input-file", "a/y.txt"],
    ["exec", "--max-turns", "1"],
    ["start", "--json", "--resume", SID, "--mode", "headless"],
    ["start", "--json", "--resume", SID, "--reset", "model", "--kind", "review"],
    ["resume", SID, "--fork", "--allow-tainted"],
    ["resume", SID, "--override-escalation"],
    ["wait", SID, "--timeout", "none"],
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
]


@pytest.mark.parametrize("argv", ALLOWED)
def test_allowed_combination_reaches_the_handler(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from claude_session import cli

    handler = cli.parse(cli.build_parser(), argv).handler
    monkeypatch.setitem(cli.HANDLERS, handler, lambda args: 77)
    assert main(argv) == 77, capsys.readouterr().err
