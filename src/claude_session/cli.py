"""Command-line interface: argparse surface, argv-only flag rules, dispatch and error output.

The commands, flags and exit codes are those of docs/interface.md. ``hook-event`` never reaches
this module: ``__main__`` dispatches it before importing ``cli`` (design.md, Hook-event fast
path).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import signal
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn

from . import durations, ids, outbox, outcomefile, store
from .defaults import MAX_INTEGER
from .errors import CliError, InternalError, OutcomeFileRejected, RefusedError, UsageError

if TYPE_CHECKING:
    import threading

    from .config import Config
    from .launch import Allocated, Process
    from .signals import ExecSignals

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
ADOPTION_FLAGS = {"json", "dry_run", "outcome_file", "verbose", "quiet", "yes", "run"}
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


def _list(args: argparse.Namespace) -> int:
    with store.open_state() as state:
        sessions = state.list_sessions()
    if sessions:
        # TODO(#16): list items (current run, labels, liveness) and the list filters.
        raise InternalError(
            "list of existing sessions is not implemented yet", detail="not_implemented"
        )
    if args.json:
        print(json.dumps({"sessions": []}))
    else:
        print("No sessions.")
    return 0


def _report(args: argparse.Namespace) -> int:
    """``report`` (interface.md): validate, queue in the outbox; never opens state.db."""
    queued = outbox.file_report(
        status=args.status,
        summary=args.summary,
        summary_file=args.summary_file,
        reason_file=args.reason_file,
        artifact=args.artifact,
        environ=os.environ,
        stdin=sys.stdin.buffer,
    )
    if args.json:
        print(json.dumps(queued.as_json()))
    else:
        print("report queued")
    return 0


@dataclass
class _Begun:
    """A launching command's allocated (or adopted) run: the config its events are delivered
    with (None: not known; ``reconcile`` delivers them) and the call that launches and
    supervises it (``launch.run``; None: the run ended in the launch phase)."""

    alloc: Allocated
    cfg: Config | None
    run: Callable[[], Any]


def _supervised(
    args: argparse.Namespace,
    begin: Callable[[store.Store, Process, ExecSignals], _Begun],
) -> int:
    """The shape every launching command shares (``exec``, the adopting ``exec`` and
    ``resume``; interface.md, Launch order, exec and Signals): the signal handlers installed
    before ``begin`` allocates (or adopts) the run, the launch and supervision, the delivery
    of what the run's worker did not deliver, then the outcome object. A launch-phase end
    exits 12 with the outcome object, also when a signal interrupted the launch; a signal
    before allocation ends the command by that signal. The handlers are restored once the
    outcome is printed (until then a late signal is only recorded)."""
    from . import launch, supervisor
    from .gateway import SystemGateway
    from .signals import ExecSignals, LaunchInterrupted

    signals = ExecSignals()
    try:  # entered first: a signal during the install is handled and the handlers restored
        try:
            signals.install()  # before allocation (interface.md, Signals)
            with store.open_state() as st:
                process = launch.Process.current(SystemGateway(), os.path.dirname(st.path))
                begun = begin(st, process, signals)
                alloc = begun.alloc
                try:
                    ended = begun.run()
                except LaunchInterrupted as exc:
                    ended = None  # the run ended not-started: its outcome is in the store
                    signals.defer()
                    _end_interrupted_launch(st, alloc, process, exc)
                except RefusedError:
                    # Refused after allocation (a fork source tainted meanwhile): the run
                    # ended not-started; its events are delivered before the envelope.
                    signals.enter_delivery()
                    _deliver_rest(st, begun.cfg, alloc, process, signals.delivery_canceled)
                    raise
                signals.enter_delivery()  # a signal now cancels the remaining delivery
                _deliver_rest(st, begun.cfg, alloc, process, signals.delivery_canceled)
                outcome = supervisor.outcome_object(st, alloc.csession_id, alloc.run)
        except LaunchInterrupted as exc:  # before allocation: nothing to record
            _die_of(exc.signum)
        except KeyboardInterrupt:  # handlers not installed (not the main thread)
            _die_of(signal.SIGINT)
        signals.defer()  # until the outcome is printed, a signal is only recorded
        try:
            _print_outcome(outcome, args.json)
        except OSError as exc:
            # A terminal that hung up (interactive SIGHUP) cannot take the outcome: the run
            # is final and stored, and the exit code still carries it.
            if exc.errno != errno.EIO:
                raise
    finally:
        signals.restore()
    if ended is not None:
        return int(ended.exit_code)
    if outcome["exit_code"] is None:  # a run that is still not final: never read as a code
        raise InternalError("the run has no outcome after its launch was interrupted")
    return int(outcome["exit_code"])


def _exec(args: argparse.Namespace) -> int:
    """``exec`` of a new session: launch steps 2 to 7, then supervision (``_supervised``)."""
    if args.session_id is not None:
        return _adopt(args)
    if args.dry_run:
        return _dry_run(args)
    from . import launch, supervisor

    cfg = load_config(args)

    def begin(st: store.Store, process: Process, signals: ExecSignals) -> _Begun:
        req = launch.read_request(args, cfg)
        resolved = launch.resolve_kind(req, cfg, process)
        launch.check_outcome_file(req, resolved, process)
        # From here an error envelope's outcome file is checked as the run's is.
        args.outcome_file_writable = launch.step2_outcome_writable(resolved, process)
        signals.defer()  # a signal during allocation acts once the run exists
        alloc = launch.allocate(st, req, resolved, process)
        return _Begun(
            alloc,
            cfg,
            lambda: launch.run(
                st,
                cfg,
                req,
                resolved,
                alloc,
                process,
                supervise=supervisor.run_session,
                signals=signals,
            ),
        )

    return _supervised(args, begin)


def _reconcile(args: argparse.Namespace) -> int:
    """``reconcile [--dry-run]`` (interface.md, reconcile): one repair pass; exit 0 (also
    when another reconcile holds the lease: ``already_running``)."""
    from . import reconcile
    from .gateway import SystemGateway

    cfg = load_config(args)
    with store.open_state() as st:
        result = reconcile.run(
            st, cfg, gateway=SystemGateway(), environ=os.environ, dry_run=args.dry_run
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    if result["already_running"]:
        print("reconcile is already running")
    for change in result["changes"]:
        line = f"{change['csession_id']}"
        if change["run"] is not None:
            line += f" run {change['run']}"
        line += f": {change['action']}"
        if change["outcome"] is not None:
            line += f" -> {change['outcome']} ({change['end_reason']})"
        if change["detail"]:
            line += f"; {change['detail']}"
        print(sanitize(line))
    for orphan in result["orphans"]:
        print(
            sanitize(
                f"{orphan['csession_id']} run {orphan['run']}: orphaned child"
                f" {orphan['child_pid']} (wrapper gone; end it with stop)"
            )
        )
    for warning in result["warnings"]:
        print(f"{PROG}: warning: {sanitize(warning)}", file=sys.stderr)
    if not (result["changes"] or result["orphans"] or result["already_running"]):
        print("nothing to repair")
    return 0


def _stop(args: argparse.Namespace) -> int:
    """``stop <id> [--grace d] [--dry-run]`` (interface.md, stop): 0 when the run ended (or
    was already final, or was canceled), 12 when it is still live after the wait."""
    from . import launch, stopper
    from .gateway import SystemGateway

    cfg = load_config(args)
    grace = None if args.grace is None else durations.parse(args.grace)
    with store.open_state() as st:
        stopped = stopper.Stop(
            st,
            cfg,
            grace=grace,
            actor=launch.default_actor(args.actor, cfg),
            decision_ids=args.decision_id or [],
            gateway=SystemGateway(),
            environ=os.environ,
        ).run(args.id, dry_run=args.dry_run)
    output = stopped.as_json()
    if args.json:
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return stopped.exit_code
    line = f"{output['csession_id']} run {output['run']}: "
    if output["dry_run"]:
        line += "dry run: "
    if output["already_final"]:
        line += "already final, "
    elif output["canceled"]:
        line += "canceled, "
    for sent in output["signals"]:
        line += f"{sent['signal']} to {sent['role']} {sent['pid']}, "
    if output["ended"]:
        line += f"{output['outcome']} ({output['end_reason']}), exit {output['run_exit_code']}"
    else:
        line += f"still {output['state']}"
    print(sanitize(line))
    return stopped.exit_code


def _wait(args: argparse.Namespace) -> int:
    """``wait <id> [--run n] [--timeout d] [--outcome-file p]`` (interface.md, wait): the
    run's outcome code, or 22 when ``--timeout`` expired first. Ctrl-C ends it by the signal,
    printing nothing."""
    from . import liveness, toolhooks, waiter
    from .gateway import SystemGateway

    cfg = load_config(args)
    timeout = durations.parse_bound(args.timeout)
    changed: set[str] = set()
    try:
        with store.open_state() as st:
            state_root = os.path.dirname(st.path)

            def bound(row: dict[str, Any]) -> None:
                # Once bound, an error envelope is checked against the waited run's paths.
                args.outcome_file_writable = outcomefile.run_writable(row, state_root)

            def check(st: store.Store, sid: str) -> None:
                if liveness.check_session(st, cfg, sid).finalized:
                    changed.add(sid)
                outcomefile.backfill(st, sid)

            try:
                waited = waiter.wait_for_run(
                    st,
                    args.id,
                    run_no=args.run,
                    timeout=timeout,
                    poll_interval=cfg.poll_interval,
                    on_row=bound,
                    check=check,
                )
            finally:
                # What the liveness check finalized is delivered before wait exits (a run
                # with no live wrapper: nobody else delivers it now).
                for sid in sorted(changed):
                    toolhooks.deliver_best_effort(
                        st,
                        cfg,
                        sid,
                        gateway=SystemGateway(),
                        environ=os.environ,
                        state_root=state_root,
                    )
    except KeyboardInterrupt:
        _die_of(signal.SIGINT)
    output = waited.output
    if args.outcome_file is not None:
        writable = outcomefile.run_writable(waited.row, state_root)
        _write_wait_outcome_file(args.outcome_file, output, state_root, writable)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    elif output["timed_out"]:
        line = (
            f"{output['csession_id']} run {output['run']}: timed out, still {output['state']}"
            f" (exit {waiter.TIMED_OUT})"
        )
        print(f"{PROG}: {sanitize(line)}", file=sys.stderr)
    else:
        _print_outcome(output, False)
    return waited.exit_code


def _adopt(args: argparse.Namespace) -> int:
    """``exec --session-id <id>``: adopt the pending run ``start`` created and launch it
    (interface.md, Launch order; ``_supervised``); with ``--dry-run``, inspect it without
    adopting."""
    from . import lifecycle, supervisor

    if args.dry_run:
        from . import launch
        from .gateway import SystemGateway

        with store.open_state() as st:
            process = launch.Process.current(SystemGateway(), os.path.dirname(st.path))
            return _print_dry_run(args, *lifecycle.adoption_plan(st, args, process))

    def begin(st: store.Store, process: Process, signals: ExecSignals) -> _Begun:
        adopted = lifecycle.adopt(st, args, process, signals)
        return _Begun(
            adopted.alloc,
            adopted.cfg,
            lambda: lifecycle.launch_adopted(
                st, adopted, process, supervise=supervisor.run_session, signals=signals
            ),
        )

    return _supervised(args, begin)


def _start(args: argparse.Namespace) -> int:
    """``start`` of a new session (interface.md, start): steps 1 to 5, the run frozen and
    left pending; prints the start object. A launch-validation failure exits 12 with
    ``launch_failed``."""
    if args.dry_run:
        return _resume_dry_run(args) if args.resume is not None else _dry_run(args)
    from . import config, launch, lifecycle
    from .gateway import SystemGateway

    cfg = load_config(args)
    source = config.Source.of(args.config)
    with store.open_state() as st:
        process = launch.Process.current(SystemGateway(), os.path.dirname(st.path))
        result = lifecycle.start(st, args, cfg, source, process)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


def _resume(args: argparse.Namespace) -> int:
    """``resume <id>`` (interface.md, resume): a new run of the session (or with ``--fork`` a
    new session's run 1) built from the previous run (carry-over), launched and supervised
    in the foreground like ``exec`` (``_supervised``); exits with the run's outcome code."""
    if args.dry_run:
        return _resume_dry_run(args)
    from . import launch, resume, supervisor

    cfg = load_config(args)

    def begin(st: store.Store, process: Process, signals: ExecSignals) -> _Begun:
        planned = resume.prepare(st, args, cfg, process)
        launch.check_outcome_file(planned.req, planned.resolved, process)
        # From here an error envelope's outcome file is checked as the run's is.
        args.outcome_file_writable = launch.step2_outcome_writable(planned.resolved, process)
        # The inline liveness check and the delivery of what it changed run deferred (a hook
        # is never cut short by the handler); a signal recorded meanwhile still ends resume
        # before anything is allocated.
        signals.defer()
        resume.liveness(st, process, planned.source.csession_id, cfg)
        signals.raise_pending()
        signals.defer()  # a signal during allocation acts once the run exists
        alloc = resume.allocate(st, planned, process, cfg=cfg)
        return _Begun(
            alloc,
            cfg,
            lambda: launch.run(
                st,
                cfg,
                planned.req,
                planned.resolved,
                alloc,
                process,
                supervise=supervisor.run_session,
                signals=signals,
            ),
        )

    return _supervised(args, begin)


def _resume_dry_run(args: argparse.Namespace) -> int:
    """``resume --dry-run`` and ``start --resume --dry-run``: the plan of the run a launch
    would allocate, with ``would_refuse``; nothing allocated or finalized."""
    from . import dryrun, resume
    from .gateway import SystemGateway

    cfg = load_config(args)
    with store.open_state() as st:
        process = dryrun.current_process(SystemGateway())
        plan, message = resume.plan(st, args, cfg, process)
    return _print_dry_run(args, plan, message)


def _dry_run(args: argparse.Namespace) -> int:
    """``exec``/``start --dry-run`` of a new session (interface.md, Dry run): the plan, exit
    20 with ``would_refuse``, 12 with ``would_fail``, else 0. Nothing is allocated."""
    from . import dryrun
    from .gateway import SystemGateway

    cfg = load_config(args)
    plan, message = dryrun.new_session_plan(args, cfg, dryrun.current_process(SystemGateway()))
    return _print_dry_run(args, plan, message)


def _print_dry_run(args: argparse.Namespace, plan: dict[str, Any], message: str | None) -> int:
    from . import dryrun

    plan = dryrun.printable(plan)  # valid UTF-8 output, also for a non-UTF-8 path
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, allow_nan=False))
    else:
        _print_plan(plan, dryrun.printable(message))
    return dryrun.exit_code(plan)


def _print_plan(plan: dict[str, Any], message: str | None) -> None:
    """The short human-readable summary of a dry-run plan (untrusted strings escaped)."""
    profile = plan["profile"]
    kind = plan["run_kind"] + (" (unknown: review profile)" if profile["fallback"] else "")
    sandbox = plan["sandbox"]
    state = "available" if sandbox["available"] else "unavailable"
    if sandbox["mode"] == "off":
        state = "not used"
    lines = [
        f"dry run: kind {kind}, mode {plan['mode']}",
        f"cwd: {plan['cwd']}",
        f"sandbox: {sandbox['mode']} ({state})",
        f"permission profile: {plan['permission_profile'] or 'from file'}",
    ]
    if plan["would_refuse"] is not None:
        lines.append(f"result: a launch would be refused ({plan['would_refuse']})")
    elif plan["would_fail"] is not None:
        lines.append(f"result: a launch would fail ({plan['would_fail']}): {message or ''}")
    else:
        lines.append("result: a launch would pass the launch validations")
    lines += [f"warning: {w}" for w in plan["warnings"] if not w.startswith("a launch would fail")]
    for line in lines:
        print(sanitize(line))


def _kinds(args: argparse.Namespace) -> int:
    """``kinds`` (interface.md, kinds): the effective kind profiles."""
    from . import dryrun
    from .gateway import SystemGateway

    cfg = load_config(args)
    process = dryrun.current_process(SystemGateway())
    report = dryrun.printable(dryrun.kinds_report(cfg, process, kind=args.kind, repo=args.repo))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return 0
    claude = report["claude_bin_resolved"] or "not found"
    version = report["claude_version"] or "unknown"
    ok = {True: "ok", False: "too old", None: "unknown"}[report["claude_version_ok"]]
    lines = [
        f"claude: {claude} (version {version}, minimum {report['min_claude_version']}: {ok})",
        f"default kind: {report['default_kind']}",
    ]
    for profile in report["kinds"]:
        available = profile["sandbox_available"]
        sandbox = profile["sandbox"] + (
            "" if available is None else (" available" if available else " unavailable")
        )
        lines.append(
            f"{profile['name']}: profile {profile['permission_profile']}, sandbox {sandbox},"
            f" cwd {profile['cwd']}, third-party {'yes' if profile['reads_third_party'] else 'no'}"
        )
        lines += [f"  warning: {w}" for w in profile["warnings"]]
    lines += [f"warning: {w}" for w in [*report["state_dir_warnings"], *report["warnings"]]]
    for line in lines:
        print(sanitize(line))
    return 0


def _write_wait_outcome_file(
    path: str, output: dict[str, Any], state_root: str, writable: Callable[[str], bool] | None
) -> None:
    """``wait --outcome-file``: ``wait``'s own output (interface.md, Outcome file), never to a
    path the waited run's agent could write (``outcomefile.run_writable``; None: not
    determined, not written); a rejected, skipped or failed write adds a warning to the output
    and logs ``outcome_file_failed``, and never changes the exit code."""
    warning = outcomefile.write(outcomefile.absolute(path), outcomefile.encode(output), writable)
    if warning is not None:
        output["warnings"] = [*output["warnings"], warning]
        outcomefile.log_failure(
            state_root, warning, csession_id=output["csession_id"], run=output["run"]
        )


def _deliver_rest(
    st: store.Store,
    cfg: Config | None,
    alloc: Allocated,
    process: Process,
    cancel: threading.Event,
) -> None:
    """``post-state-change`` delivery of what the run's worker did not deliver: all of a
    launch-phase end (its ``launch`` and one final event; no worker ran), else normally
    nothing. A busy lease or a store error is retried for up to the lease TTL; ``cancel`` (a
    signal) stops it. Best effort: whatever stays undelivered (all of it without ``cfg``) is
    delivered by the next delivering command (``reconcile``); a hook failure never changes
    the outcome."""
    from . import toolhooks

    if cfg is None:
        return
    toolhooks.deliver_best_effort(
        st,
        cfg,
        alloc.csession_id,
        gateway=process.gateway,
        environ=process.environ,
        state_root=process.state_root,
        cancel=cancel,
    )


def _end_interrupted_launch(
    st: store.Store, alloc: Allocated, process: Process, exc: BaseException
) -> None:
    """Backstop: a ``LaunchInterrupted`` that reached ``exec`` with the run not final (it
    escaped the launch's own handling) ends the run ``not-started`` here."""
    from . import launch

    if st.run_record(alloc.csession_id, alloc.run)["ended_at"] is None:
        error = launch.interrupted_by(st, alloc, exc)
        launch.end_launch(st, alloc, None, None, process, error, exc=exc)


def _die_of(signum: int) -> NoReturn:
    """Exit by ``signum`` without a traceback: restore its default action and signal this
    process itself (its exact PID)."""
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    raise KeyboardInterrupt  # not reached: the signal ends the process


def _print_outcome(outcome: dict[str, Any], json_mode: bool) -> None:
    """``--json``: the outcome object on stdout; otherwise a one-line summary on stderr."""
    if json_mode:
        print(json.dumps(outcome, ensure_ascii=False, allow_nan=False))
        return
    line = (
        f"{outcome['csession_id']} run {outcome['run']}: {outcome['outcome']}"
        f" ({outcome['end_reason']}), exit {outcome['exit_code']}"
    )
    if outcome["message"]:
        line += f": {outcome['message']}"
    print(f"{PROG}: {sanitize(line)}", file=sys.stderr)
    for warning in outcome["warnings"]:
        print(f"{PROG}: warning: {sanitize(warning)}", file=sys.stderr)


# Command name (as typed, nested commands joined by a space) -> handler.
HANDLERS: dict[str, Handler] = {
    "exec": _exec,
    "start": _start,
    "resume": _resume,
    "stop": _stop,
    "wait": _wait,
    "report": _report,
    "status": _not_implemented,
    "list": _list,
    "kinds": _kinds,
    "label add": _not_implemented,
    "label remove": _not_implemented,
    "transcript": _not_implemented,
    "forget": _not_implemented,
    "reconcile": _reconcile,
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
    p.add_argument("--run", type=int, metavar="N", help="with --session-id: the run to adopt")

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
    if args.run is not None:
        if args.session_id is None:
            raise UsageError("--run requires --session-id")
        if not 1 <= args.run <= MAX_INTEGER:
            raise UsageError(f"--run must be a positive integer (at most {MAX_INTEGER})")
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
    return _value_from_argv(argv, "--outcome-file")


def _value_from_argv(argv: Sequence[str], flag: str) -> str | None:
    """The last value of ``flag`` in ``argv`` before any ``--`` (as argparse would take it)."""
    found: str | None = None
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == flag and index + 1 < len(argv):
            value = argv[index + 1]
            if not value.startswith("-") or NEGATIVE_NUMBER.match(value):
                found = value
        elif token.startswith(flag + "="):
            found = token.partition("=")[2]
    return found


def _envelope_writable(
    args: argparse.Namespace | None, argv: Sequence[str], state_root: str
) -> Callable[[str], bool] | None:
    """What an error envelope's ``--outcome-file`` write refuses: what the command set once it
    knew (``args.outcome_file_writable``: ``exec`` once the kind is resolved, ``wait`` once the
    run is bound), else, failing closed, ``<state>/sessions``, the tmp root, this process's
    cwd and ``--cwd`` (an isolated run's agent may write either; the kind may not be known)."""
    if args is not None and hasattr(args, "outcome_file_writable"):
        return args.outcome_file_writable  # type: ignore[no-any-return]
    cwd = os.getcwd()
    given = getattr(args, "cwd", None) if args else _value_from_argv(argv, "--cwd")
    extra = [cwd, *([os.path.join(cwd, given)] if given else [])]
    return outcomefile.baseline_writable(state_root, *(os.path.realpath(p) for p in extra))


def _write_outcome_file(path: str, error: CliError, writable: Callable[[str], bool] | None) -> None:
    """An error envelope to ``--outcome-file`` (``outcomefile.write``): never to a path
    ``writable`` refuses (None: not determined, never written), and never for the rejection
    of that path itself. A rejected, skipped or failed write is a warning on stderr, logged
    ``outcome_file_failed`` (best effort)."""
    if isinstance(error, OutcomeFileRejected):
        return
    state_root = store.state_dir()
    data = (json.dumps(error.envelope()) + "\n").encode()
    try:
        warning = outcomefile.write(outcomefile.absolute(path), data, writable)
    except OSError as exc:  # no cwd to make the path absolute against
        warning = f"{outcomefile.NOT_WRITTEN}: {exc.strerror or type(exc).__name__}"
    if warning is not None:
        print(f"{PROG}: warning: {sanitize(warning)}", file=sys.stderr)
        outcomefile.log_failure(state_root, warning)


def _print_error(error: CliError, json_mode: bool) -> None:
    message = sanitize(error.message)
    if error.detail is not None:
        message = f"{message} ({sanitize(error.detail)})"
    print(f"{PROG}: error: {message}", file=sys.stderr)
    if json_mode:
        print(json.dumps(error.envelope()))


# Schema check (interface.md, Launch order step 1).

# `report` never opens state.db; `hook-event` never reaches this module.
SCHEMA_CHECK_EXEMPT = ("report",)
HELP_OR_VERSION = ("-h", "--help", "--version")


def _needs_schema_check(argv: Sequence[str]) -> bool:
    """Whether ``argv`` runs a command that must check the schema before anything else.

    Exempt: ``report``; no command or an unknown one (a usage error); ``--help`` and
    ``--version`` anywhere before ``--``, which only print text and never open state.db
    (an option value spelled ``--help`` therefore also skips the check; interface.md,
    Launch order step 1).
    """
    commands = {name.split()[0] for name in HANDLERS}
    if not argv or argv[0] not in commands or argv[0] in SCHEMA_CHECK_EXEMPT:
        return False
    for token in argv:
        if token == "--":
            break
        if token in HELP_OR_VERSION:
            return False
    return True


def check_schema(argv: Sequence[str]) -> None:
    """Refuse a newer ``state.db`` (``schema_newer``, exit 12) and migrate an older one,
    before parsing, so nothing is allocated and no usage check runs first."""
    if _needs_schema_check(argv):
        store.open_state().close()


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args: argparse.Namespace | None = None
    schema_checked = False
    try:
        check_schema(argv)
        schema_checked = True
        args = parse(build_parser(), argv)
        check_flag_rules(args)
        return HANDLERS[args.handler](args)
    except CliError as error:
        # `wait --outcome-file` gets any error envelope wait prints after the schema check
        # (interface.md, Outcome file); the other commands' outcome files only usage errors
        # and refusals.
        is_wait = _command_name(args, argv) == "wait"
        if (is_wait and schema_checked) or isinstance(error, (UsageError, RefusedError)):
            _write_error_outcome_file(args, argv, error)
        code = error.exit_code
        _emit_error(error, args.json if args else _given(argv, "--json"))
        return code
    except BrokenPipeError:
        raise  # a closed output stream; __main__ handles it
    except Exception as exc:  # last resort: any bug is reported as `internal` (exit 1)
        error = InternalError(f"internal error: {type(exc).__name__}")
        code = error.exit_code
        if schema_checked and _command_name(args, argv) == "wait":
            # wait's outcome file gets this envelope too (interface.md, Outcome file); a
            # write failure only warns.
            _write_error_outcome_file(args, argv, error)
        try:
            if args.verbose if args else _given(argv, "--verbose"):
                traceback.print_exc(file=sys.stderr)
        except BrokenPipeError:
            pass
        _emit_error(error, args.json if args else _given(argv, "--json"))
        return code


def _command_name(args: argparse.Namespace | None, argv: Sequence[str]) -> str | None:
    """The command being run: the parsed handler, else the first argument."""
    if args:
        return str(args.handler)
    return argv[0] if argv else None


def _write_error_outcome_file(
    args: argparse.Namespace | None, argv: Sequence[str], error: CliError
) -> None:
    """Write ``error``'s envelope to the ``--outcome-file`` given, if any."""
    path = getattr(args, "outcome_file", None) if args else _outcome_file_from_argv(argv)
    if not path:
        return
    try:
        writable = _envelope_writable(args, argv, store.state_dir())
    except (OSError, ValueError):  # no cwd to refuse: fail closed
        writable = None
    _write_outcome_file(path, error, writable)


def _emit_error(error: CliError, json_mode: bool) -> None:
    """Print ``error``; a closed stream must not change the exit code (``__main__`` then
    points the stream at /dev/null)."""
    try:
        _print_error(error, json_mode)
    except BrokenPipeError:
        pass
