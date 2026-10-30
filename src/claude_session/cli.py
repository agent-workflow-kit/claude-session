"""Command-line entry point: argparse surface, dispatch, error envelopes and exit codes.

The commands, flags and exit codes are those of docs/interface.md. ``hook-event`` is dispatched
before the parser is built, so the injected-hook path never imports ``config`` or Pydantic
(design.md, Hook-event fast path). Keep this module's top-level imports to the standard library
and ``errors``.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Callable, Sequence
from typing import NoReturn

from .errors import CliError, FailedError, InternalError, UsageError

PROG = "claude-session"
DESCRIPTION = "Runs and supervises Claude Code sessions (terminal-agnostic)."

Handler = Callable[[argparse.Namespace], int]

STATES = (
    "starting",
    "working",
    "idle",
    "needs-input",
    "reported",
    "exited",
    "exited-unreported",
    "killed",
    "timeout",
    "lost",
    "not-started",
)
REPORT_STATUSES = ("done", "blocked", "failed", "needs-input")
RESETTABLE = (
    "model",
    "plugin-dir",
    "env-allow",
    "env-allowlist-only",
    "setting-sources",
    "sandbox",
    "timeout",
    "max-turns",
    "permission-profile",
    "auto-exit",
)


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that raises ``UsageError`` instead of exiting, so ``--json`` works."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        raise UsageError(message)


def _not_implemented(args: argparse.Namespace) -> int:
    raise FailedError(f"{args.handler} is not implemented yet", detail="not_implemented")


# Command name (as typed, nested commands joined by a space) -> handler.
HANDLERS: dict[str, Handler] = {
    "exec": _not_implemented,
    "start": _not_implemented,
    "resume": _not_implemented,
    "stop": _not_implemented,
    "wait": _not_implemented,
    "report": _not_implemented,
    "status": _not_implemented,
    "list": _not_implemented,
    "kinds": _not_implemented,
    "label add": _not_implemented,
    "label remove": _not_implemented,
    "transcript": _not_implemented,
    "forget": _not_implemented,
    "reconcile": _not_implemented,
    "export": _not_implemented,
    "cron install": _not_implemented,
    "cron uninstall": _not_implemented,
    "verify-secret": _not_implemented,
}


def _version_text() -> str:
    from . import __version__

    return f"{PROG} {__version__}"


def _global_flags(version: str) -> _Parser:
    """Flags every command accepts (interface.md, Global flags)."""
    flags = _Parser(add_help=False, allow_abbrev=False)
    flags.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    flags.add_argument("--config", metavar="PATH", help="config file")
    flags.add_argument("--verbose", action="store_true", help="more detail on stderr")
    flags.add_argument("--quiet", action="store_true", help="less detail on stderr")
    flags.add_argument("--yes", action="store_true", help="no-op: claude-session never prompts")
    flags.add_argument("--version", action="version", version=version)
    return flags


def _command(
    subparsers: argparse._SubParsersAction[_Parser],
    name: str,
    handler: str,
    parents: list[_Parser],
    help_text: str,
) -> _Parser:
    parser = subparsers.add_parser(name, parents=parents, help=help_text, allow_abbrev=False)
    parser.set_defaults(handler=handler)
    return parser


def _add_audit_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", metavar="ACTOR")
    parser.add_argument("--decision-id", action="append", metavar="ID")


def _add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")


def _add_launch_flags(parser: argparse.ArgumentParser, *, resumable: bool) -> None:
    """Launch flags (interface.md, Launch flags); ``resumable`` adds the resume-only ones."""
    parser.add_argument("--kind", metavar="NAME")
    parser.add_argument("--mode", choices=("interactive", "headless"))
    parser.add_argument("--skill", metavar="NAME")
    parser.add_argument("--prompt-file", metavar="PATH")
    parser.add_argument("--input-file", action="append", metavar="PATH")
    parser.add_argument("--plugin-dir", action="append", metavar="DIR")
    parser.add_argument("--repo", metavar="REPO_ID")
    parser.add_argument("--label", action="append", metavar="KEY=VALUE")
    parser.add_argument("--hook", action="append", metavar="NAME=CMD")
    parser.add_argument("--hook-timeout", action="append", metavar="NAME=DURATION")
    parser.add_argument("--cwd", metavar="PATH")
    parser.add_argument("--timeout", metavar="DURATION")
    parser.add_argument("--max-turns", type=int, metavar="N")
    parser.add_argument("--model", metavar="NAME")
    parser.add_argument("--permission-profile", metavar="NAME_OR_FILE")
    parser.add_argument("--sandbox", choices=("required", "preferred", "off"))
    parser.add_argument("--setting-sources", metavar="LIST")
    parser.add_argument("--reads-third-party", action="store_true", default=None)
    parser.add_argument("--auto-exit", action=argparse.BooleanOptionalAction)
    parser.add_argument("--env-allow", action="append", metavar="VAR")
    parser.add_argument("--env-allowlist-only", action="append", metavar="VAR")
    parser.add_argument("--outcome-file", metavar="PATH")
    _add_audit_flags(parser)
    _add_dry_run(parser)
    if resumable:
        parser.add_argument("--fork", action="store_true")
        parser.add_argument("--reset", action="append", choices=RESETTABLE)
        parser.add_argument("--allow-tainted", action="store_true")
        parser.add_argument("--override-escalation", action="store_true")


def build_parser() -> _Parser:
    version = _version_text()
    parser = _Parser(prog=PROG, description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=version)
    common = [_global_flags(version)]
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    p = _command(commands, "exec", "exec", common, "run a session in the foreground")
    _add_launch_flags(p, resumable=False)
    p.add_argument("--session-id", metavar="ID", help="adopt a pending run created by start")

    p = _command(commands, "start", "start", common, "prepare a session for another process")
    _add_launch_flags(p, resumable=True)
    p.add_argument("--resume", metavar="ID")

    p = _command(commands, "resume", "resume", common, "run a new run of a session")
    p.add_argument("id")
    _add_launch_flags(p, resumable=True)

    p = _command(commands, "stop", "stop", common, "end a session's current run")
    p.add_argument("id")
    p.add_argument("--grace", metavar="DURATION")
    _add_audit_flags(p)
    _add_dry_run(p)

    p = _command(commands, "wait", "wait", common, "wait until a run is final")
    p.add_argument("id")
    p.add_argument("--run", type=int, metavar="N")
    p.add_argument("--timeout", metavar="DURATION")
    p.add_argument("--outcome-file", metavar="PATH")

    p = _command(commands, "report", "report", common, "file a status report (agents)")
    p.add_argument("status", choices=REPORT_STATUSES)
    summary = p.add_mutually_exclusive_group()
    summary.add_argument("--summary-file", metavar="PATH")
    summary.add_argument("--summary", metavar="TEXT")
    p.add_argument("--reason-file", metavar="PATH")
    p.add_argument("--artifact", metavar="NAME_OR_PATH")

    p = _command(commands, "status", "status", common, "show a session")
    p.add_argument("id")
    p.add_argument("--run", type=int, metavar="N")

    p = _command(commands, "list", "list", common, "list sessions")
    p.add_argument("--label", action="append", metavar="KEY=VALUE")
    p.add_argument("--state", action="append", choices=STATES)
    p.add_argument("--kind", metavar="KIND")
    p.add_argument("--launch-kind", metavar="KIND")
    p.add_argument("--run-kind", metavar="KIND")
    p.add_argument("--mode", choices=("interactive", "headless"))
    p.add_argument("--needs-input", action="store_true")
    p.add_argument("--pending", action="store_true")
    p.add_argument("--repo", metavar="REPO_ID")
    p.add_argument("--since", metavar="TIME")
    p.add_argument("--limit", type=int, metavar="N")
    p.add_argument("--forked-from", metavar="ID")
    p.add_argument("--report-status", action="append", choices=(*REPORT_STATUSES, "none"))
    p.add_argument("--live", action="store_true")
    p.add_argument("--tainted", action="store_true")

    p = _command(commands, "kinds", "kinds", common, "show effective kind profiles")
    p.add_argument("--kind", metavar="NAME")
    p.add_argument("--repo", metavar="REPO_ID")

    label = commands.add_parser("label", help="add or remove session labels", allow_abbrev=False)
    label_commands = label.add_subparsers(dest="label_command", metavar="<action>", required=True)
    for action in ("add", "remove"):
        p = _command(label_commands, action, f"label {action}", common, f"{action} labels")
        p.add_argument("id")
        p.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
        _add_dry_run(p)

    p = _command(commands, "transcript", "transcript", common, "show a session's transcripts")
    p.add_argument("id")
    form = p.add_mutually_exclusive_group()
    form.add_argument("--raw", action="store_true")
    form.add_argument("--paths", action="store_true")
    p.add_argument("--subagents", action="store_true")
    p.add_argument("--run", type=int, metavar="N")

    p = _command(commands, "forget", "forget", common, "hide sessions")
    p.add_argument("id", nargs="?")
    p.add_argument("--older-than", metavar="DURATION")
    p.add_argument("--state", action="append", choices=STATES)
    p.add_argument("--kind", metavar="KIND")
    p.add_argument("--label", action="append", metavar="KEY=VALUE")
    _add_audit_flags(p)
    _add_dry_run(p)

    p = _command(commands, "reconcile", "reconcile", common, "repair state against reality")
    _add_dry_run(p)

    p = _command(commands, "export", "export", common, "print audit records")
    p.add_argument("--since", metavar="TIME")
    p.add_argument("--action", action="append", metavar="ACTION")

    cron = commands.add_parser(
        "cron", help="manage the reconcile crontab block", allow_abbrev=False
    )
    cron_commands = cron.add_subparsers(dest="cron_command", metavar="<action>", required=True)
    p = _command(cron_commands, "install", "cron install", common, "install the crontab block")
    p.add_argument("--interval", type=int, metavar="MINUTES")
    p.add_argument("--env-file", metavar="PATH")
    _add_dry_run(p)
    p = _command(cron_commands, "uninstall", "cron uninstall", common, "remove the crontab block")
    _add_dry_run(p)

    p = _command(commands, "verify-secret", "verify-secret", common, "internal: check a run secret")
    p.add_argument("id")
    return parser


def sanitize(text: str) -> str:
    """Escape C0 and C1 control characters for human-readable output."""
    return "".join(f"\\x{ord(c):02x}" if _is_control(c) else c for c in text)


def _is_control(char: str) -> bool:
    code = ord(char)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _wants_json(argv: Sequence[str]) -> bool:
    """Whether ``--json`` was given; known even when parsing fails. Stops at ``--``."""
    for token in argv:
        if token == "--":
            return False
        if token == "--json":
            return True
    return False


def _print_error(error: CliError, json_mode: bool) -> None:
    message = sanitize(error.message)
    if error.detail is not None:
        message = f"{message} ({sanitize(error.detail)})"
    print(f"{PROG}: error: {message}", file=sys.stderr)
    if json_mode:
        print(json.dumps(error.envelope()))


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["hook-event"]:
        from . import hookevent

        return hookevent.main(args_list[1:])

    json_mode = _wants_json(args_list)
    try:
        args = build_parser().parse_args(args_list)
        return HANDLERS[args.handler](args)
    except CliError as error:
        _print_error(error, json_mode)
        return error.exit_code
    except Exception as exc:  # last resort: report any bug as `internal` (exit 1)
        traceback.print_exc(file=sys.stderr)
        error = InternalError(f"internal error: {type(exc).__name__}: {exc}")
        _print_error(error, json_mode)
        return error.exit_code
