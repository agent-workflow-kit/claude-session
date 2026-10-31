"""Command-line interface: argparse surface, argv-only flag rules, dispatch and error output.

The commands, flags and exit codes are those of docs/interface.md. ``hook-event`` never reaches
this module: ``__main__`` dispatches it before importing ``cli`` (design.md, Hook-event fast
path).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn

from . import durations, fsutil, ids
from .defaults import MAX_INTEGER
from .errors import CliError, InternalError, RefusedError, UsageError

if TYPE_CHECKING:
    from .config import Config

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
HOOK_NAMES = ("pre-launch", "post-state-change")
SETTING_SOURCES = ("user", "project", "local")
# schemas/audit.json, property `action`.
AUDIT_ACTIONS = (
    "resume_tainted",
    "forget",
    "stop",
    "launch_escalation_override",
    "sandbox_lowered",
)
# `--reset <setting>` -> the argparse dest of the flag it resets.
RESETTABLE = {
    "model": "model",
    "plugin-dir": "plugin_dir",
    "env-allow": "env_allow",
    "env-allowlist-only": "env_allowlist_only",
    "setting-sources": "setting_sources",
    "sandbox": "sandbox",
    "timeout": "timeout",
    "max-turns": "max_turns",
    "permission-profile": "permission_profile",
    "auto-exit": "auto_exit",
}
# The only flags `exec --session-id` accepts (interface.md, Exit codes, exit 2).
ADOPTION_FLAGS = {"json", "dry_run", "outcome_file", "verbose", "quiet", "yes"}
OUTCOME_FILE_COMMANDS = ("exec", "start", "resume", "wait")
# Session id arguments (dest -> flag; None for the positional id). verify-secret is not
# listed: it requires the full id and answers a mismatch with exit 11, never a usage error.
SESSION_ID_ARGS: dict[str, str | None] = {
    "id": None,
    "session_id": "--session-id",
    "resume": "--resume",
    "forked_from": "--forked-from",
}
KIND_ARGS = ("kind", "launch_kind", "run_kind")
# argparse takes a token like this as an option value, not as an option.
NEGATIVE_NUMBER = re.compile(r"^-\d+$|^-\d*\.\d+$")


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that raises ``UsageError`` instead of exiting, so ``--json`` works."""

    # Leaf command name -> its parser (set on the top-level parser only).
    leaves: dict[str, _Parser]

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        raise UsageError(message)


class _VersionAction(argparse.Action):
    """``--version``: reads the package metadata only when the flag is given."""

    def __init__(self, option_strings: list[str], dest: str = argparse.SUPPRESS) -> None:
        super().__init__(
            option_strings,
            dest,
            nargs=0,
            default=argparse.SUPPRESS,
            help="show the version and exit",
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        from . import __version__

        print(f"{PROG} {__version__}")
        parser.exit()


def _not_implemented(args: argparse.Namespace) -> int:
    raise InternalError(f"{args.handler} is not implemented yet", detail="not_implemented")


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


# Parser


def _global_flags() -> _Parser:
    """Flags every command accepts (interface.md, Global flags)."""
    flags = _Parser(add_help=False, allow_abbrev=False)
    flags.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    flags.add_argument("--config", metavar="PATH", help="config file")
    flags.add_argument("--verbose", action="store_true", help="more detail on stderr")
    flags.add_argument("--quiet", action="store_true", help="less detail on stderr")
    flags.add_argument("--yes", action="store_true", help="no-op: claude-session never prompts")
    flags.add_argument("--version", action=_VersionAction)
    return flags


def _command(
    subparsers: argparse._SubParsersAction[_Parser],
    leaves: dict[str, _Parser],
    name: str,
    handler: str,
    help_text: str | None,
) -> _Parser:
    """Add a leaf command; ``help_text=None`` keeps it out of ``--help`` (internal commands)."""
    kwargs: dict[str, Any] = {"parents": [_global_flags()], "allow_abbrev": False}
    if help_text is not None:
        kwargs["help"] = help_text
    parser = subparsers.add_parser(name, **kwargs)
    parser.set_defaults(handler=handler)
    leaves[handler] = parser
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
        parser.add_argument("--reset", action="append", choices=tuple(RESETTABLE))
        parser.add_argument("--allow-tainted", action="store_true")
        parser.add_argument("--override-escalation", action="store_true")


def build_parser() -> _Parser:
    parser = _Parser(prog=PROG, description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--version", action=_VersionAction)
    leaves: dict[str, _Parser] = {}
    parser.leaves = leaves
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    p = _command(commands, leaves, "exec", "exec", "run a session in the foreground")
    _add_launch_flags(p, resumable=False)
    p.add_argument("--session-id", metavar="ID", help="adopt a pending run created by start")

    p = _command(commands, leaves, "start", "start", "prepare a session for another process")
    _add_launch_flags(p, resumable=True)
    p.add_argument("--resume", metavar="ID")

    p = _command(commands, leaves, "resume", "resume", "run a new run of a session")
    p.add_argument("id")
    _add_launch_flags(p, resumable=True)

    p = _command(commands, leaves, "stop", "stop", "end a session's current run")
    p.add_argument("id")
    p.add_argument("--grace", metavar="DURATION")
    _add_audit_flags(p)
    _add_dry_run(p)

    p = _command(commands, leaves, "wait", "wait", "wait until a run is final")
    p.add_argument("id")
    p.add_argument("--run", type=int, metavar="N")
    p.add_argument("--timeout", metavar="DURATION")
    p.add_argument("--outcome-file", metavar="PATH")

    p = _command(commands, leaves, "report", "report", "file a status report (agents)")
    p.add_argument("status", choices=REPORT_STATUSES)
    summary = p.add_mutually_exclusive_group()
    summary.add_argument("--summary-file", metavar="PATH")
    summary.add_argument("--summary", metavar="TEXT")
    p.add_argument("--reason-file", metavar="PATH")
    p.add_argument("--artifact", metavar="NAME_OR_PATH")

    p = _command(commands, leaves, "status", "status", "show a session")
    p.add_argument("id")
    p.add_argument("--run", type=int, metavar="N")

    p = _command(commands, leaves, "list", "list", "list sessions")
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

    p = _command(commands, leaves, "kinds", "kinds", "show effective kind profiles")
    p.add_argument("--kind", metavar="NAME")
    p.add_argument("--repo", metavar="REPO_ID")

    label = commands.add_parser("label", help="add or remove session labels", allow_abbrev=False)
    label_commands = label.add_subparsers(dest="label_command", metavar="<action>", required=True)
    for action in ("add", "remove"):
        p = _command(label_commands, leaves, action, f"label {action}", f"{action} labels")
        p.add_argument("id")
        p.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
        _add_dry_run(p)

    p = _command(commands, leaves, "transcript", "transcript", "show a session's transcripts")
    p.add_argument("id")
    form = p.add_mutually_exclusive_group()
    form.add_argument("--raw", action="store_true")
    form.add_argument("--paths", action="store_true")
    p.add_argument("--subagents", action="store_true")
    p.add_argument("--run", type=int, metavar="N")

    p = _command(commands, leaves, "forget", "forget", "hide sessions")
    p.add_argument("id", nargs="?")
    p.add_argument("--older-than", metavar="DURATION")
    p.add_argument("--state", action="append", choices=STATES)
    p.add_argument("--kind", metavar="KIND")
    p.add_argument("--label", action="append", metavar="KEY=VALUE")
    _add_audit_flags(p)
    _add_dry_run(p)

    p = _command(commands, leaves, "reconcile", "reconcile", "repair state against reality")
    _add_dry_run(p)

    p = _command(commands, leaves, "export", "export", "print audit records")
    p.add_argument("--since", metavar="TIME")
    p.add_argument("--action", action="append", metavar="ACTION")

    cron = commands.add_parser("cron", help="manage the crontab block", allow_abbrev=False)
    cron_commands = cron.add_subparsers(dest="cron_command", metavar="<action>", required=True)
    p = _command(cron_commands, leaves, "install", "cron install", "install the crontab block")
    p.add_argument("--interval", type=int, metavar="MINUTES")
    p.add_argument("--env-file", metavar="PATH")
    _add_dry_run(p)
    p = _command(cron_commands, leaves, "uninstall", "cron uninstall", "remove the crontab block")
    _add_dry_run(p)

    # Internal: run by agent-spawn, not listed in --help.
    p = _command(commands, leaves, "verify-secret", "verify-secret", None)
    p.add_argument("id")
    return parser


def parse(parser: _Parser, argv: Sequence[str]) -> argparse.Namespace:
    """Parse ``argv``; leftover arguments are reported with the chosen command's usage."""
    args, extras = parser.parse_known_args(argv)
    if extras and args.handler in ("label add", "label remove"):
        # Pairs given after an option (`label add <id> a=b --dry-run c=d`).
        if not any(extra.startswith("-") for extra in extras):
            args.pairs.extend(extras)
            extras = []
    if extras:
        parser.leaves[args.handler].error(f"unrecognized arguments: {' '.join(extras)}")
    return args


# Argv-only flag rules (interface.md, Flag rules and the command sections); exit 2.


def _flag(dest: str) -> str:
    if dest == "auto_exit":
        return "--[no-]auto-exit"
    return "--" + dest.replace("_", "-")


def _check_duration(flag: str, text: str, *, bound: bool = False, positive: bool = False) -> None:
    """The duration grammar (interface.md, Identifiers); ``bound`` also accepts ``none`` and
    refuses zero (use ``none``), ``positive`` refuses zero."""
    if bound and text == durations.NONE:
        return
    try:
        seconds = durations.parse(text)
    except ValueError as exc:
        raise UsageError(f"{flag}: {exc}") from None
    if seconds <= 0 and (bound or positive):
        hint = " (use 'none')" if bound else ""
        raise UsageError(f"{flag} must be positive, got {text!r}{hint}")


def _check_timeout(args: argparse.Namespace) -> None:
    if args.timeout is not None:
        _check_duration("--timeout", args.timeout, bound=True)


def since_threshold(text: str, *, now: datetime | None = None) -> datetime:
    """The time ``--since`` stands for (timezone-aware). A duration (that long before ``now``)
    is tried first, so bare digits such as ``20240101`` are seconds; then an ISO 8601 time,
    UTC when it has no offset."""
    try:
        seconds = durations.parse(text)
    except ValueError:
        pass
    else:
        return (now or datetime.now(UTC)) - timedelta(seconds=seconds)
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise UsageError(
            "--since: expected an ISO 8601 time or a duration (seconds or <number><unit> "
            "with unit ms, s, m, h or d)"
        ) from None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _check_since(text: str) -> None:
    since_threshold(text)


def _split_hook(flag: str, entry: str) -> str:
    name, sep, _ = entry.partition("=")
    if not sep:
        raise UsageError(f"{flag} expects <name>=<value>, got {entry!r}")
    if name not in HOOK_NAMES:
        raise UsageError(f"{flag}: unknown hook name {name!r} (one of {', '.join(HOOK_NAMES)})")
    return name


def _check_launch_flags(args: argparse.Namespace) -> None:
    hooks = {_split_hook("--hook", entry) for entry in args.hook or []}
    for entry in args.hook_timeout or []:
        name = _split_hook("--hook-timeout", entry)
        if name not in hooks:
            raise UsageError(f"--hook-timeout {name}=... needs a --hook {name}=... too")
        _check_duration(f"--hook-timeout {name}", entry.partition("=")[2], positive=True)
    _check_timeout(args)
    if args.max_turns is not None and not 1 <= args.max_turns <= MAX_INTEGER:
        raise UsageError(f"--max-turns must be between 1 and {MAX_INTEGER}")
    if args.model is not None and args.model.startswith("-"):
        raise UsageError("--model must not start with '-'")
    basenames: set[str] = set()
    for path in args.input_file or []:
        if path == "-":
            raise UsageError("--input-file - is not supported; pass a file")
        name = os.path.basename(path)
        if not ids.is_artifact_name(name):
            pattern = ids.ARTIFACT_NAME.pattern
            raise UsageError(f"--input-file name {name!r} must match ^{pattern}$, not . or ..")
        if name in basenames:
            raise UsageError(f"duplicate --input-file name {name!r}")
        basenames.add(name)
    for dest in ("env_allow", "env_allowlist_only"):
        for name in getattr(args, dest) or []:
            if name == "" and dest == "env_allowlist_only":
                continue  # `--env-allowlist-only ''`: an empty list
            if not ids.is_env_name(name):
                raise UsageError(
                    f"{_flag(dest)}: invalid variable name {name!r} (^{ids.ENV_NAME.pattern}$)"
                )
    if args.setting_sources:
        for source in args.setting_sources.split(","):
            if source not in SETTING_SOURCES:
                raise UsageError(
                    f"--setting-sources: unknown source {source!r} (one of "
                    f"{', '.join(SETTING_SOURCES)}, or '' for none)"
                )
    for setting in getattr(args, "reset", None) or []:
        if getattr(args, RESETTABLE[setting]) is not None:
            flag = _flag(RESETTABLE[setting])
            raise UsageError(f"--reset {setting} cannot be combined with {flag}")
    if getattr(args, "fork", False) and getattr(args, "override_escalation", False):
        raise UsageError("--override-escalation cannot be combined with --fork")


def _check_new_headless(args: argparse.Namespace) -> None:
    if args.mode == "headless" and not args.skill and not args.prompt_file:
        raise UsageError("a headless new session needs --skill or --prompt-file")


def _check_exec(args: argparse.Namespace) -> None:
    if args.session_id is not None:
        defaults = vars(build_parser().parse_args(["exec"]))
        for dest, value in vars(args).items():
            if dest in ADOPTION_FLAGS or dest in ("command", "handler", "session_id"):
                continue
            if value != defaults[dest]:
                raise UsageError(f"{_flag(dest)} cannot be used with --session-id")
        return
    _check_launch_flags(args)
    _check_new_headless(args)


def _check_start(args: argparse.Namespace) -> None:
    if not args.json:
        raise UsageError("start requires --json")
    if args.resume is None:
        for dest in ("fork", "reset", "allow_tainted", "override_escalation"):
            if getattr(args, dest):
                raise UsageError(f"{_flag(dest)} requires --resume")
        _check_new_headless(args)
    _check_launch_flags(args)


def _check_wait(args: argparse.Namespace) -> None:
    _check_timeout(args)


def _check_report(args: argparse.Namespace) -> None:
    if args.status == "done":
        summary_given = args.summary is not None and args.summary.strip() != ""
        if not summary_given and args.summary_file is None:
            raise UsageError("report done requires a non-empty --summary or --summary-file")
    elif args.reason_file is None:
        raise UsageError(f"report {args.status} requires --reason-file")


def _check_stop(args: argparse.Namespace) -> None:
    if args.grace is not None:
        _check_duration("--grace", args.grace)


def _check_list(args: argparse.Namespace) -> None:
    if args.limit is not None and args.limit < 1:
        raise UsageError("--limit must be at least 1")
    if args.since is not None:
        _check_since(args.since)


def _check_transcript(args: argparse.Namespace) -> None:
    if args.raw and args.json:
        raise UsageError("--raw cannot be combined with --json")


def _check_forget(args: argparse.Namespace) -> None:
    bulk = [dest for dest in ("older_than", "state", "kind", "label") if getattr(args, dest)]
    if args.id is not None and bulk:
        raise UsageError(f"{_flag(bulk[0])} is for bulk mode and cannot be used with an id")
    if args.id is None and args.older_than is None:
        raise UsageError("forget needs a session id or --older-than")
    if args.older_than is not None:
        _check_duration("--older-than", args.older_than)


def _check_export(args: argparse.Namespace) -> None:
    if args.since is not None:
        _check_since(args.since)
    for action in args.action or []:
        if action not in AUDIT_ACTIONS:
            known = ", ".join(AUDIT_ACTIONS)
            raise UsageError(f"--action: unknown action {action!r} (one of {known})")


def _check_cron_install(args: argparse.Namespace) -> None:
    if args.interval is not None and not 1 <= args.interval <= 59:
        raise UsageError("--interval must be between 1 and 59 minutes")


FLAG_RULES: dict[str, Callable[[argparse.Namespace], None]] = {
    "exec": _check_exec,
    "start": _check_start,
    "resume": _check_launch_flags,
    "stop": _check_stop,
    "wait": _check_wait,
    "report": _check_report,
    "list": _check_list,
    "transcript": _check_transcript,
    "forget": _check_forget,
    "export": _check_export,
    "cron install": _check_cron_install,
}


def _check_label(text: str, where: str) -> None:
    try:
        ids.parse_label(text)
    except ValueError as exc:
        raise UsageError(f"{where}invalid label {text!r}: {exc}") from None


def _check_identifiers(args: argparse.Namespace) -> None:
    """Session id prefixes and the name grammars (interface.md, Identifiers)."""
    if args.handler != "verify-secret":
        for dest, flag in SESSION_ID_ARGS.items():
            value = getattr(args, dest, None)
            if value is not None:
                ids.session_id_prefix(value, flag)
    for dest in KIND_ARGS:
        value = getattr(args, dest, None)
        if value is not None and not ids.is_kind_name(value):
            raise UsageError(
                f"{_flag(dest)}: invalid kind name {value!r} (^{ids.KIND_NAME.pattern}$)"
            )
    skill = getattr(args, "skill", None)
    if skill is not None and not ids.is_skill_name(skill):
        raise UsageError(f"--skill: invalid skill name {skill!r} (^{ids.SKILL_NAME.pattern}$)")
    repo = getattr(args, "repo", None)
    if repo is not None and not ids.is_repo_id(repo):
        raise UsageError(f"--repo: invalid repo id {repo!r} (expected host[:port]/path)")
    for text in getattr(args, "label", None) or []:
        _check_label(text, "--label: ")
    for text in getattr(args, "pairs", None) or []:
        _check_label(text, "")


def check_flag_rules(args: argparse.Namespace) -> None:
    """Raise ``UsageError`` for any rule that can be decided from argv alone."""
    if args.verbose and args.quiet:
        raise UsageError("--verbose and --quiet are mutually exclusive")
    _check_identifiers(args)
    rule = FLAG_RULES.get(args.handler)
    if rule is not None:
        rule(args)


# Config


def load_config(args: argparse.Namespace) -> Config:
    """The config for a command: ``--config`` names the only file read (it must exist);
    without it, the default ``config.yaml`` and its ``config.d/`` drop-ins."""
    from . import config  # Pydantic and PyYAML: only for commands that need the config

    return config.load(args.config)


# Output


def sanitize(text: str) -> str:
    """Escape C0 and C1 control characters for human-readable output."""
    return "".join(f"\\x{ord(c):02x}" if _is_control(c) else c for c in text)


def _is_control(char: str) -> bool:
    code = ord(char)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _given(argv: Sequence[str], flag: str) -> bool:
    """Whether ``flag`` (or ``flag=...``) is in ``argv`` before any ``--``; used when parsing
    failed, so the error can still honor ``--json`` and ``--verbose``."""
    for token in argv:
        if token == "--":
            return False
        if token == flag or token.startswith(flag + "="):
            return True
    return False


def _outcome_file_from_argv(argv: Sequence[str]) -> str | None:
    """The last ``--outcome-file`` value in ``argv`` (as argparse would take it)."""
    if not argv or argv[0] not in OUTCOME_FILE_COMMANDS:
        return None
    found: str | None = None
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == "--outcome-file" and index + 1 < len(argv):
            value = argv[index + 1]
            if not value.startswith("-") or NEGATIVE_NUMBER.match(value):
                found = value
        elif token.startswith("--outcome-file="):
            found = token.partition("=")[2]
    return found


def _write_outcome_file(path: str, error: CliError) -> None:
    # TODO(#21): skip a path rejected by the session-writable check (isolated runs).
    data = (json.dumps(error.envelope()) + "\n").encode()
    try:
        # Not abspath: `..` must be resolved after symlinks (realpath in write_private_file).
        fsutil.write_private_file(os.path.join(os.getcwd(), path), data)
    except OSError as exc:
        reason = sanitize(str(exc))
        print(f"{PROG}: warning: could not write --outcome-file: {reason}", file=sys.stderr)


def _print_error(error: CliError, json_mode: bool) -> None:
    message = sanitize(error.message)
    if error.detail is not None:
        message = f"{message} ({sanitize(error.detail)})"
    print(f"{PROG}: error: {message}", file=sys.stderr)
    if json_mode:
        print(json.dumps(error.envelope()))


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args: argparse.Namespace | None = None
    try:
        args = parse(build_parser(), argv)
        check_flag_rules(args)
        return HANDLERS[args.handler](args)
    except CliError as error:
        if isinstance(error, (UsageError, RefusedError)):
            path = getattr(args, "outcome_file", None) if args else _outcome_file_from_argv(argv)
            if path:
                _write_outcome_file(path, error)
        code = error.exit_code
        _emit_error(error, args.json if args else _given(argv, "--json"))
        return code
    except BrokenPipeError:
        raise  # a closed output stream; __main__ handles it
    except Exception as exc:  # last resort: any bug is reported as `internal` (exit 1)
        error = InternalError(f"internal error: {type(exc).__name__}")
        code = error.exit_code
        try:
            if args.verbose if args else _given(argv, "--verbose"):
                traceback.print_exc(file=sys.stderr)
        except BrokenPipeError:
            pass
        _emit_error(error, args.json if args else _given(argv, "--json"))
        return code


def _emit_error(error: CliError, json_mode: bool) -> None:
    """Print ``error``; a closed stream must not change the exit code (``__main__`` then
    points the stream at /dev/null)."""
    try:
        _print_error(error, json_mode)
    except BrokenPipeError:
        pass
