from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from claude_session import __version__, cli
from claude_session.cli import main
from claude_session.errors import AmbiguousError, NotFoundError, UsageError

from .schemas import validate

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden"
SID = "cs-abcd2345"

# One invocation per command, each with every flag interface.md lists for it.
FULL_INVOCATIONS: dict[str, list[str]] = {
    "exec": [
        "exec",
        *("--kind", "review", "--mode", "headless", "--skill", "plan:make"),
        *("--prompt-file", "p.md", "--input-file", "a.txt", "--input-file", "b.txt"),
        *("--plugin-dir", "/p1", "--plugin-dir", "/p2", "--repo", "example.org/g/r"),
        *("--label", "a=b", "--label", "c=", "--hook", "pre-launch=true"),
        *("--hook-timeout", "pre-launch=10s", "--cwd", "session-dir", "--timeout", "none"),
        *("--max-turns", "5", "--model", "m", "--permission-profile", "review"),
        *("--sandbox", "required", "--setting-sources", "", "--reads-third-party"),
        *("--no-auto-exit", "--env-allow", "A", "--env-allowlist-only", "B"),
        *("--outcome-file", "/tmp/o.json", "--actor", "auto:test"),
        *("--decision-id", "d1", "--decision-id", "d2", "--dry-run"),
        *("--json", "--config", "c.yaml", "--verbose", "--yes"),
    ],
    "exec-adopt": ["exec", "--session-id", SID, "--dry-run", "--outcome-file", "o", "--json"],
    "start": [
        "start",
        *("--kind", "implement", "--mode", "interactive", "--auto-exit", "--label", "a=b"),
        *("--dry-run", "--json"),
    ],
    "start-resume": [
        "start",
        *("--resume", SID, "--fork", "--reset", "model", "--reset", "sandbox"),
        *("--allow-tainted", "--override-escalation", "--cwd", "/w", "--dry-run", "--json"),
    ],
    "resume": [
        "resume",
        SID,
        *("--fork", "--reset", "timeout", "--allow-tainted", "--override-escalation"),
        *("--kind", "review", "--outcome-file", "o", "--dry-run", "--json", "--quiet"),
    ],
    "stop": [
        "stop",
        SID,
        *("--grace", "5s", "--actor", "human:me", "--decision-id", "d", "--dry-run", "--json"),
    ],
    "wait": ["wait", SID, "--run", "2", "--timeout", "10m", "--outcome-file", "o", "--json"],
    "report": [
        "report",
        "needs-input",
        *("--summary-file", "-", "--reason-file", "r.md", "--artifact", "out.md", "--json"),
    ],
    "report-summary": ["report", "done", "--summary", "one line"],
    "status": ["status", SID, "--run", "1", "--json"],
    "list": [
        "list",
        *("--label", "a=b", "--label", "c=d", "--state", "working", "--state", "idle"),
        *("--kind", "review", "--launch-kind", "review", "--run-kind", "implement"),
        *("--mode", "headless", "--needs-input", "--pending", "--repo", "example.org/r"),
        *("--since", "2h", "--limit", "10", "--forked-from", SID),
        *("--report-status", "done", "--report-status", "none", "--live", "--tainted", "--json"),
    ],
    "kinds": ["kinds", "--kind", "review", "--repo", "example.org/r", "--json"],
    "label-add": ["label", "add", SID, "a=b", "c=d", "--dry-run", "--json"],
    "label-remove": ["label", "remove", SID, "a=b", "--dry-run", "--json"],
    "transcript": ["transcript", SID, "--paths", "--subagents", "--run", "1", "--json"],
    "transcript-raw": ["transcript", SID, "--raw"],
    "forget": ["forget", SID, "--actor", "auto:x", "--decision-id", "d", "--dry-run", "--json"],
    "forget-bulk": [
        "forget",
        *("--older-than", "7d", "--state", "exited", "--state", "lost", "--kind", "review"),
        *("--label", "a=b", "--actor", "auto:x", "--decision-id", "d", "--dry-run", "--json"),
    ],
    "reconcile": ["reconcile", "--dry-run", "--json"],
    "export": ["export", "--since", "1d", "--action", "stop", "--action", "forget", "--json"],
    "cron-install": [
        "cron",
        *("install", "--interval", "5", "--env-file", "/e", "--dry-run", "--json"),
    ],
    "cron-uninstall": ["cron", "uninstall", "--dry-run", "--json"],
    "verify-secret": ["verify-secret", SID, "--json"],
}

PUBLIC_COMMANDS = {
    "exec",
    "start",
    "resume",
    "stop",
    "wait",
    "report",
    "status",
    "list",
    "kinds",
    "label add",
    "label remove",
    "transcript",
    "forget",
    "reconcile",
    "export",
    "cron install",
    "cron uninstall",
    "verify-secret",
}


def run_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def raising(error: Exception) -> Callable[[Any], int]:
    def handler(args: Any) -> int:
        raise error

    return handler


# --version


def test_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]
    assert capsys.readouterr().out.strip() == f"claude-session {__version__}"


def test_version_accepted_after_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["list", "--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"claude-session {__version__}"


# argparse surface


def test_parser_has_every_public_command() -> None:
    assert set(cli.HANDLERS) == PUBLIC_COMMANDS


@pytest.mark.parametrize("name", sorted(FULL_INVOCATIONS))
def test_every_documented_flag_parses(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = FULL_INVOCATIONS[name]
    # Stubs fail with `failed` (12); a parse error would be usage (2).
    assert main(argv) == 12, capsys.readouterr().err


@pytest.mark.parametrize("name", sorted(FULL_INVOCATIONS))
def test_yes_is_accepted_as_a_no_op(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [*FULL_INVOCATIONS[name], "--yes"]
    without = main(FULL_INVOCATIONS[name])
    first = capsys.readouterr()
    assert main(argv) == without
    second = capsys.readouterr()
    assert (second.out, second.err) == (first.out, first.err)


def test_yes_is_accepted_by_hook_event() -> None:
    assert main(["hook-event", "--state-dir", "/nonexistent", "Stop", "--yes"]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["exec", "--reset", "model"],
        ["exec", "--fork"],
        ["exec", "--allow-tainted"],
        ["exec", "--override-escalation"],
        ["exec", "--mode", "batch"],
        ["exec", "--max-turns", "many"],
        ["list", "--state", "sleeping"],
        ["list", "--report-status", "maybe"],
        ["report", "finished", "--summary", "x"],
        ["report", "done", "--summary", "x", "--summary-file", "f"],
        ["transcript", SID, "--raw", "--paths"],
        ["label", "add", SID],
        ["label"],
        ["cron"],
        ["status"],
        ["nonsense"],
        [],
    ],
)
def test_usage_errors_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "claude-session" in captured.err


def test_abbreviated_flags_are_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--jso"]) == 2
    assert capsys.readouterr().out == ""


# Unimplemented commands


def test_unimplemented_command_reports_failed(capsys: pytest.CaptureFixture[str]) -> None:
    code, envelope, err = run_json(["list", "--json"], capsys)
    assert code == 12
    validate(envelope, "claude-session/cli/error.json")
    assert envelope == json.loads((GOLDEN / "error-not-implemented.json").read_text())
    assert "not implemented" in err


# --json error envelopes


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--json"],
        ["list", "--limit", "x", "--json"],
        ["nonsense", "--json"],
        ["--json"],
        ["label", "add", SID, "--json"],
    ],
)
def test_usage_error_envelope(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, envelope, err = run_json(argv, capsys)
    assert code == 2
    validate(envelope, "claude-session/cli/error.json")
    assert envelope["error"]["code"] == "usage"
    assert envelope["error"]["message"] in err


def test_json_after_double_dash_is_not_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--", "--json"]) == 12
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("error", "exit_code", "golden"),
    [
        (UsageError("invalid config", detail="kinds.review.timeout: bad duration"), 2, "usage"),
        (NotFoundError("unknown session: cs-abcd2345"), 10, "not-found"),
        (
            AmbiguousError("ambiguous session id: abcd", matches=["cs-abcd2345", "cs-abcd7777"]),
            21,
            "ambiguous",
        ),
    ],
)
def test_error_envelope_golden(
    error: Exception,
    exit_code: int,
    golden: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "status", raising(error))
    code, envelope, err = run_json(["status", "abcd", "--json"], capsys)
    assert code == exit_code
    validate(envelope, "claude-session/cli/error.json")
    assert envelope == json.loads((GOLDEN / f"error-{golden}.json").read_text())
    assert envelope["error"]["message"] in err


def test_error_without_json_goes_to_stderr_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "status", raising(NotFoundError("unknown session: x")))
    assert main(["status", "x"]) == 10
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "claude-session: error: unknown session: x\n"


def test_unexpected_exception_is_internal_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "status", raising(RuntimeError("boom")))
    code, envelope, _ = run_json(["status", SID, "--json"], capsys)
    assert code == 1
    validate(envelope, "claude-session/cli/error.json")
    assert envelope["error"]["code"] == "internal"


def test_handler_return_code_is_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "kinds", lambda args: 0)
    assert main(["kinds", "--json"]) == 0


# Human output sanitization


def test_sanitize_escapes_c0_and_c1_controls() -> None:
    assert cli.sanitize("a\x1b[31mb\x85c\td\x7f") == "a\\x1b[31mb\\x85c\\x09d\\x7f"


def test_human_error_message_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "status", raising(NotFoundError("unknown: x\x1b[2J")))
    code, envelope, err = run_json(["status", "x", "--json"], capsys)
    assert code == 10
    assert envelope["error"]["message"] == "unknown: x\x1b[2J"
    assert "\x1b" not in err
    assert "x\\x1b[2J" in err
