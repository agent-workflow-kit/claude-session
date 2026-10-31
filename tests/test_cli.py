from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sqlite3
import stat
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from claude_session import __version__, cli
from claude_session.cli import build_parser, main
from claude_session.errors import (
    AmbiguousError,
    InvalidPermissionProfile,
    LaunchValidationError,
    NotFoundError,
    RefusedError,
    UsageError,
)

from .golden import assert_golden
from .schemas import validate

ROOT = Path(__file__).resolve().parent.parent
SID = "cs-abcd2345"
ERROR_SCHEMA = "claude-session/cli/error.json"

# One invocation per command form, together using every flag interface.md lists.
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
        *("--json", "--config", "c.yaml", "--verbose"),
    ],
    "exec-adopt": ["exec", "--session-id", SID, "--dry-run", "--outcome-file", "o", "--json"],
    "start": [
        "start",
        *("--kind", "implement", "--mode", "interactive", "--auto-exit", "--label", "a=b"),
        *("--setting-sources", "user,project", "--dry-run", "--json"),
    ],
    "start-resume": [
        "start",
        *("--resume", SID, "--fork", "--reset", "model", "--reset", "sandbox"),
        *("--allow-tainted", "--cwd", "/w", "--dry-run", "--json"),
    ],
    "start-resume-escalation": ["start", "--resume", SID, "--override-escalation", "--json"],
    "resume": [
        "resume",
        SID,
        *("--reset", "timeout", "--allow-tainted", "--override-escalation"),
        *("--kind", "review", "--outcome-file", "o", "--dry-run", "--json", "--quiet"),
    ],
    "resume-fork": ["resume", SID, "--fork", "--cwd", "/w"],
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
}


def parse(argv: list[str]) -> argparse.Namespace:
    return cli.parse(build_parser(), argv)


def run_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def raising(error: BaseException) -> Callable[[Any], int]:
    def handler(args: Any) -> int:
        raise error

    return handler


SENTINEL = 77


def sentinel(args: argparse.Namespace) -> int:
    return SENTINEL


# --version


def test_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]
    assert capsys.readouterr().out == f"claude-session {__version__}\n"


def test_version_is_plain_text_after_a_command_with_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["list", "--json", "--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"claude-session {__version__}\n"


def test_version_metadata_is_read_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(name: str) -> str:
        raise AssertionError("importlib.metadata.version called")

    monkeypatch.setattr(importlib.metadata, "version", fail)
    assert parse(["list"]).handler == "list"


def test_version_falls_back_to_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out == "claude-session unknown\n"


# argparse surface


def test_handlers_cover_public_and_internal_commands() -> None:
    assert set(cli.HANDLERS) == PUBLIC_COMMANDS | {"verify-secret"}
    assert set(build_parser().leaves) == set(cli.HANDLERS)


def test_help_lists_public_commands_only() -> None:
    text = build_parser().format_help()
    for name in ("exec", "list", "label", "cron", "reconcile"):
        assert f"    {name} " in text
    assert "verify-secret" not in text
    assert "hook-event" not in text


@pytest.mark.parametrize("name", sorted(FULL_INVOCATIONS))
def test_every_documented_flag_parses_and_passes_rules(name: str) -> None:
    args = parse(FULL_INVOCATIONS[name])
    cli.check_flag_rules(args)
    assert args.handler in cli.HANDLERS


def test_launch_flags_are_parsed_into_fields() -> None:
    args = parse(FULL_INVOCATIONS["exec"])
    assert args.kind == "review"
    assert args.mode == "headless"
    assert args.input_file == ["a.txt", "b.txt"]
    assert args.plugin_dir == ["/p1", "/p2"]
    assert args.label == ["a=b", "c="]
    assert args.hook == ["pre-launch=true"]
    assert args.hook_timeout == ["pre-launch=10s"]
    assert args.max_turns == 5
    assert args.setting_sources == ""
    assert args.reads_third_party is True
    assert args.auto_exit is False
    assert args.env_allowlist_only == ["B"]
    assert args.decision_id == ["d1", "d2"]
    assert (args.dry_run, args.json, args.verbose, args.config) == (True, True, True, "c.yaml")


def test_unset_launch_flags_are_none() -> None:
    args = parse(["exec"])
    assert (args.auto_exit, args.reads_third_party, args.max_turns, args.label) == (
        None,
        None,
        None,
        None,
    )


def test_resume_only_flags_are_parsed() -> None:
    args = parse(FULL_INVOCATIONS["start-resume"])
    assert (args.resume, args.fork, args.reset, args.allow_tainted) == (
        SID,
        True,
        ["model", "sandbox"],
        True,
    )


def test_nested_commands_and_positionals() -> None:
    args = parse(FULL_INVOCATIONS["label-add"])
    assert (args.handler, args.id, args.pairs) == ("label add", SID, ["a=b", "c=d"])
    args = parse(FULL_INVOCATIONS["cron-install"])
    assert (args.handler, args.interval, args.env_file) == ("cron install", 5, "/e")
    args = parse(FULL_INVOCATIONS["list"])
    assert args.state == ["working", "idle"]
    assert args.report_status == ["done", "none"]


def test_label_pairs_after_an_option_are_collected() -> None:
    args = parse(["label", "add", SID, "a=b", "--dry-run", "c=d"])
    assert (args.pairs, args.dry_run) == (["a=b", "c=d"], True)


@pytest.mark.parametrize("name", sorted(FULL_INVOCATIONS))
def test_yes_is_accepted_as_a_no_op(name: str) -> None:
    without = vars(parse(FULL_INVOCATIONS[name]))
    with_yes = vars(parse([*FULL_INVOCATIONS[name], "--yes"]))
    assert with_yes.pop("yes") is True
    assert without.pop("yes") is False
    assert with_yes == without


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["exec", "--reset", "model"], "unrecognized arguments: --reset model"),
        (["exec", "--fork"], "unrecognized arguments: --fork"),
        (["exec", "--allow-tainted"], "unrecognized arguments: --allow-tainted"),
        (["exec", "--override-escalation"], "unrecognized arguments: --override-escalation"),
        (["exec", "--mode", "batch"], "invalid choice: 'batch'"),
        (["exec", "--max-turns", "many"], "invalid int value: 'many'"),
        (["resume", SID, "--reset", "reads-third-party"], "invalid choice: 'reads-third-party'"),
        (["list", "--state", "sleeping"], "invalid choice: 'sleeping'"),
        (["list", "--report-status", "maybe"], "invalid choice: 'maybe'"),
        (["report", "finished", "--summary", "x"], "invalid choice: 'finished'"),
        (["report", "done", "--summary", "x", "--summary-file", "f"], "not allowed with"),
        (["transcript", SID, "--raw", "--paths"], "not allowed with"),
        (["label", "add", SID], "the following arguments are required: KEY=VALUE"),
        (["label"], "the following arguments are required: <action>"),
        (["cron"], "the following arguments are required: <action>"),
        (["status"], "the following arguments are required: id"),
        (["list", "--jso"], "unrecognized arguments: --jso"),
        (["nonsense"], "invalid choice: 'nonsense'"),
        ([], "the following arguments are required: <command>"),
    ],
)
def test_usage_errors_exit_2(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


def test_unrecognized_arguments_show_the_command_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["status", SID, "--bogus"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("usage: claude-session status ")


# Dispatch


def test_handler_return_code_is_the_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(cli.HANDLERS, "kinds", sentinel)
    assert main(["kinds", "--json"]) == SENTINEL


def test_json_after_double_dash_is_a_positional(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    def handler(args: argparse.Namespace) -> int:
        seen.append(args.id)
        assert args.json is False
        return SENTINEL

    monkeypatch.setitem(cli.HANDLERS, "status", handler)
    assert main(["status", "--", "--json"]) == SENTINEL
    assert seen == ["--json"]


def test_unimplemented_command_is_internal_not_implemented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, envelope, err = run_json(["kinds", "--json"], capsys)
    assert code == 1
    assert_golden(envelope, "error-not-implemented")
    assert "not implemented" in err


def test_interactive_exec_without_a_terminal_is_a_usage_error_before_allocation(
    capsys: pytest.CaptureFixture[str], _private_state_dir: Path
) -> None:
    """interface.md, Launch order step 2: plain exec in interactive mode (the default) needs
    a TTY on stdin and stdout; without one it exits 2 with a hint and allocates nothing."""
    code, envelope, _ = run_json(["exec", "--json"], capsys)
    assert code == 2
    assert envelope["error"]["code"] == "usage"
    assert "--mode headless" in envelope["error"]["message"]
    db = _private_state_dir / "claude-session" / "state.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)
    finally:
        conn.close()


# --json error envelopes


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--json"],
        ["list", "--limit", "x", "--json"],
        ["nonsense", "--json"],
        ["--json"],
        ["label", "add", SID, "--json"],
        ["list", "--json=yes"],
    ],
)
def test_usage_error_envelope(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    code, envelope, err = run_json(argv, capsys)
    assert code == 2
    validate(envelope, ERROR_SCHEMA)
    assert envelope["error"]["code"] == "usage"
    assert envelope["error"]["message"] in err


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
    assert_golden(envelope, f"error-{golden}")
    assert envelope["error"]["message"] in err


def test_error_without_json_goes_to_stderr_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "status", raising(NotFoundError("unknown session: xxxx")))
    assert main(["status", "xxxx"]) == 10
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "claude-session: error: unknown session: xxxx\n"


@pytest.mark.parametrize(
    "error",
    [
        LaunchValidationError("cwd is unsafe", end_reason="unsafe_cwd"),
        InvalidPermissionProfile("invalid permission profile", detail="sandbox.x: not allowed"),
    ],
)
def test_uncaught_launch_validation_error_exits_12(
    error: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "status", raising(error))
    code, envelope, _ = run_json(["status", SID, "--json"], capsys)
    assert code == 12
    validate(envelope, ERROR_SCHEMA)
    assert envelope["error"]["code"] == "failed"


# Internal errors


def test_internal_error_envelope_hides_exception_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "status", raising(RuntimeError("secret-ish detail")))
    code, envelope, err = run_json(["status", SID, "--json"], capsys)
    assert code == 1
    validate(envelope, ERROR_SCHEMA)
    assert envelope == {"error": {"code": "internal", "message": "internal error: RuntimeError"}}
    assert err == "claude-session: error: internal error: RuntimeError\n"


def test_internal_error_traceback_only_with_verbose(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "status", raising(RuntimeError("secret-ish detail")))
    assert main(["status", SID, "--verbose"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "secret-ish detail" in err


# Human output sanitization


def test_sanitize_escapes_c0_and_c1_controls() -> None:
    assert cli.sanitize("a\x1b[31mb\x85c\td\x7f") == "a\\x1b[31mb\\x85c\\x09d\\x7f"


def test_human_error_message_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "status", raising(NotFoundError("unknown: x\x1b[2J")))
    code, envelope, err = run_json(["status", "xxxx", "--json"], capsys)
    assert code == 10
    assert envelope["error"]["message"] == "unknown: x\x1b[2J"
    assert "\x1b" not in err
    assert "x\\x1b[2J" in err


# --outcome-file for usage errors and refusals


def read_envelope(path: Path) -> Any:
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    envelope = json.loads(path.read_text())
    validate(envelope, ERROR_SCHEMA)
    return envelope


def test_flag_rule_error_is_written_to_outcome_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "outcome.json"
    assert main(["exec", "--timeout", "0", "--outcome-file", str(out)]) == 2
    assert read_envelope(out)["error"]["code"] == "usage"


def test_parse_error_is_written_to_outcome_file_from_argv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "outcome.json"
    assert main(["wait", SID, f"--outcome-file={out}", "--bogus"]) == 2
    assert read_envelope(out)["error"]["message"] == "unrecognized arguments: --bogus"


def test_relative_outcome_file_is_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["start", "--outcome-file", "o.json"]) == 2
    assert read_envelope(tmp_path / "o.json")["error"]["message"] == "start requires --json"


def test_refusal_is_written_to_outcome_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "resume", raising(RefusedError("live", reason="live")))
    out = tmp_path / "outcome.json"
    assert main(["resume", SID, "--outcome-file", str(out)]) == 20
    assert read_envelope(out) == {"error": {"code": "refused", "message": "live", "reason": "live"}}


def test_not_found_is_not_written_to_outcome_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "wait", raising(NotFoundError("unknown")))
    out = tmp_path / "outcome.json"
    assert main(["wait", SID, "--outcome-file", str(out)]) == 10
    assert not out.exists()


def test_outcome_file_ignored_for_commands_without_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "outcome.json"
    assert main(["list", "--outcome-file", str(out)]) == 2
    assert not out.exists()


def test_outcome_file_write_failure_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "missing-dir" / "outcome.json"
    assert main(["exec", "--timeout", "0", "--outcome-file", str(out), "--json"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == "usage"
    assert "warning: could not write --outcome-file" in captured.err
    assert not os.path.exists(out)


def test_outcome_file_prescan_ignores_a_flag_as_its_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["exec", "--outcome-file", "--json"]) == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("extra", [[], ["--bogus"]], ids=["parsed", "parse-failed"])
def test_repeated_outcome_file_writes_the_last_path(
    extra: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, last = tmp_path / "first.json", tmp_path / "last.json"
    argv = ["exec", "--timeout", "0", "--outcome-file", str(first), "--outcome-file", str(last)]
    assert main([*argv, *extra]) == 2
    assert read_envelope(last)["error"]["code"] == "usage"
    assert not first.exists()


def test_outcome_file_dotdot_is_resolved_after_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "deep" / "real").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "deep" / "real")
    monkeypatch.chdir(tmp_path)
    assert main(["exec", "--timeout", "0", "--outcome-file", "link/../o.json"]) == 2
    assert read_envelope(tmp_path / "deep" / "o.json")["error"]["code"] == "usage"
    assert not (tmp_path / "o.json").exists()


def test_outcome_file_prescan_accepts_a_negative_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["exec", "--bogus", "--outcome-file", "-1"]) == 2
    assert read_envelope(tmp_path / "-1")["error"]["code"] == "usage"


@pytest.mark.parametrize("value", [".", "sub/.."])
def test_outcome_file_naming_a_directory_is_a_warning(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["exec", "--timeout", "0", "--outcome-file", value, "--json"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["error"]["code"] == "usage"
    assert "warning: could not write --outcome-file" in captured.err
    assert "Traceback" not in captured.err
