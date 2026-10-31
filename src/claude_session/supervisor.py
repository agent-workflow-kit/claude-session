"""Supervision of a run's ``claude`` (design.md, Supervision; interface.md, exec, Signals and
Outcome mapping).

``run_session`` spawns ``claude``. **Headless**: from the main thread (``die_with_parent``:
Linux sets ``PR_SET_PDEATHSIG(SIGTERM)`` in the child, and the kernel sends it when the
*spawning thread* exits, so it must be a thread that lives as long as the process) in its own
process group, with ``/dev/null`` as stdin, stdout redirected straight to the private
``result.json`` and stderr to ``stderr.log``. **Interactive**: in this process's group (the
terminal's foreground group, no ``setsid``), with the terminal inherited on stdin, stdout and
stderr; it gets SIGINT, SIGTSTP and SIGHUP from the terminal itself. The spawn is the last
step of the launch phase: a signal received since the launch steps, or a spawn that fails,
ends the run ``not-started`` (``launch_interrupted``, or ``canceled`` after a ``stop``
request; ``launch.end_launch``). It then records the child's PID and start time and the
``session`` phase and supervises the child until it exits (``Supervision``): every signal
``exec`` receives is handled per the signal table (``signals``; ``pending_cause`` recorded
before the one forward to the exact child, SIGKILL to that same child after ``kill_grace``),
and every ``poll_interval`` the auto-exit rule, the run's bound and (headless) the
``session_start_timeout`` warning are checked. Then, in this order: drains the
run's outbox (and ``claimed/``), maps the end (``endmap.map_headless`` or
``endmap.map_interactive``, from the recorded ``pending_cause``), accepts or rejects an
implicit done (``result.md``, headless only), finalizes the run (the compare-and-set, which
also requires the report status the end was mapped from; a report that landed meanwhile is
mapped again), deletes the secret index and removes the private tmpdir. A terminating signal
received before the child was reaped (a terminal hangup also reaches ``claude``) still records
its cause when the run has none, though nothing is sent; signals received after the reap are
recorded and dropped: the run is finalized either way.

Only the exact child is ever signaled: ``Child`` is the unreaped ``Popen``, so its PID cannot
have been reused, and it signals nothing once reaped.

If supervision hits an internal error after the spawn (any exception), it ignores SIGINT while
it cleans up, records ``pending_cause`` ``internal_error``, sends SIGTERM to the exact child it
spawned (SIGKILL after ``kill_grace``, also when the wait is interrupted), finalizes the run
directly (``killed``, ``internal_error``, unless a report exists; no drain, no result parsing,
cost null), deletes the secret index and removes the tmpdir on every path, and re-raises.

The run's bound (``--timeout``, else the kind's ``timeout`` headless or ``interactive_timeout``
interactive; None: no bound) counts from the spawn: once it is reached, ``pending_cause``
``timeout`` is recorded and the child gets SIGTERM, then SIGKILL after ``kill_grace``; the
final event's cause is then ``timeout``. A busy store holds the SIGTERM back (retried at each
poll) until the bound plus ``kill_grace``; then, or at once on any other store error,
supervision fails and the internal-error path ends the child. A headless run with no
``SessionStart`` within ``session_start_timeout`` of the spawn gets a warning (logged
``session_start_missing`` and added to the run's ``warnings``); the run goes on.

``post-state-change`` delivery (``toolhooks``): once ``claude`` is spawned (never before: the
headless spawn must precede any thread), a background worker delivers the session's events in
order; the poll loop only wakes it, so a slow hook never delays supervision. Once the run is
final the worker delivers what is left, the final event included, and stops (``exec`` waits
for it, each hook bounded by its own timeout). A launch-phase end is delivered synchronously
by ``exec`` (``cli``). A SIGTERM, SIGINT or SIGHUP once the child was reaped cancels the
remaining delivery (``ExecSignals.enter_delivery``; ``toolhooks``, Cancel).

The process that wins the finalization compare-and-set (``finish``, or the internal-error
path) writes the run's ``--outcome-file`` right after it (``outcomefile.write_run``, checked
against the run's full session-writable set stored at launch step 5), before the secret
index and the tmpdir are removed; a failure there is a run warning and never changes the exit
code. Warnings known before the compare-and-set are recorded with it, so the file carries
them; later ones (cleanup, the outcome-file warning itself) are stdout's only.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import (
    endmap,
    fsutil,
    hookevent,
    launch,
    logs,
    outbox,
    outcomefile,
    secretindex,
    store,
    tmpdir,
    toolhooks,
)
from .config import Config
from .endmap import EXIT_CODES, HeadlessResult, Mapped
from .errors import InternalError
from .gateway import DEVNULL, INHERIT, Child
from .signals import ExecSignals, signal_name, stop_cause

IMPLICIT_ARTIFACT = "result.md"
# Final drains of one run at most (each claims and checks for up to outbox.DRAIN_BUDGET).
FINAL_DRAINS = 4
# Mappings of one end at most when a report keeps landing between mapping and finalization.
FINALIZE_ATTEMPTS = 3


@dataclass(frozen=True)
class Ended:
    """How the run ended, for the outcome object: the exit code of ``exec`` and the run's
    warnings gathered by the supervisor."""

    exit_code: int
    warnings: tuple[str, ...]


# The longest wait for the child between two checks of the signals ``exec`` received.
SIGNAL_SLICE = 0.05


def spawn(
    prepared: launch.Prepared,
    v: launch.Validated,
    process: launch.Process,
    signals: ExecSignals,
    *,
    mode: str,
) -> Child:
    """Start ``claude`` (see the module docs), first raising a signal received since the
    launch steps (``LaunchInterrupted``). Closes the output descriptors."""
    try:
        signals.raise_pending()
        if mode == "interactive":
            return process.gateway.spawn(
                prepared.argv,
                executable=prepared.executable,
                env=v.session_env.env,
                cwd=v.cwd,
                stdin=INHERIT,
                stdout=INHERIT,
                stderr=INHERIT,
            )
        assert prepared.stdout_fd is not None and prepared.stderr_fd is not None
        return process.gateway.spawn(
            prepared.argv,
            executable=prepared.executable,
            env=v.session_env.env,
            cwd=v.cwd,
            stdin=DEVNULL,
            stdout=prepared.stdout_fd,
            stderr=prepared.stderr_fd,
            new_process_group=True,
            die_with_parent=True,
        )
    finally:
        for fd in (prepared.stdout_fd, prepared.stderr_fd):
            if fd is not None:
                os.close(fd)


def _idle_stop(event: dict[str, Any]) -> bool:
    """A Stop without background tasks (one with them keeps the activity ``working``)."""
    return not event["final"] and event["cause"] == "Stop" and event["to_activity"] == "idle"


def qualifying_stop(event: dict[str, Any] | None) -> bool:
    """Whether ``event`` is a Stop without background tasks that left the run ``reported``
    (a waiting prompt or an open escalation is ``needs-input``)."""
    return event is not None and _idle_stop(event) and event["to_state"] == "reported"


def auto_exit_candidate(latest: list[dict[str, Any]]) -> int | None:
    """The id of the run's latest event when auto-exit acts on it (``latest``: the newest
    events, newest first), else None: a qualifying Stop, or the ``report`` event of a report
    the Stop's own hook ingested (that Stop, idle, is the event right before it, and the
    report left the run ``reported`` and idle)."""
    if not latest:
        return None
    event = latest[0]
    if qualifying_stop(event):
        return int(event["id"])
    if (
        len(latest) > 1
        and not event["final"]
        and event["cause"] == "report"
        and event["to_state"] == "reported"
        and event["to_activity"] == "idle"
        and _idle_stop(latest[1])
    ):
        return int(event["id"])
    return None


class Supervision:
    """The session phase of one run: waits for the exact ``child`` and, until it exits,
    forwards the signals ``exec`` receives (interface.md, Signals), ends the child for
    auto-exit and SIGKILLs it ``kill_grace`` after the first signal ``exec`` sent it.

    ``exec`` signals the child once with a terminating signal (``ended`` records it), always
    after recording its cause with ``store.set_pending_cause`` (an earlier cause, such as a
    ``stop`` request's, stands). Later signals are logged and dropped."""

    def __init__(
        self,
        st: store.Store,
        cfg: Config,
        alloc: launch.Allocated,
        child: Child,
        signals: ExecSignals,
        log: logs.EventLog,
        *,
        auto_exit: bool,
        monotonic: Any = time.monotonic,
        bound: float | None = None,
        session_start_timeout: float | None = None,
        spawned_at: float | None = None,
        deliver: Callable[[], None] | None = None,
    ) -> None:
        self.st, self.cfg, self.alloc, self.child = st, cfg, alloc, child
        self.signals, self.log = signals, log
        self.auto_exit = auto_exit
        self.monotonic = monotonic
        self.ended = False  # exec sent the child its terminating signal
        self.kill_at: float | None = None
        self.candidate: int | None = None  # the auto_exit_candidate event id
        self.due = 0.0
        # The run's bound and the session_start_timeout check (None: none), both counted from
        # the spawn (``spawned_at`` on the ``monotonic`` clock; default: now).
        self.bound = bound
        self.session_start_timeout = session_start_timeout
        self.spawned_at = self.monotonic() if spawned_at is None else spawned_at
        self.session_start_checked = False
        self.session_start_logged = False
        self.deliver = deliver  # wakes the post-state-change worker (never blocks)

    def wait(self) -> int:
        """Supervise until the child exits; its return code (negative: killed by that
        signal)."""
        next_poll = self.monotonic()
        while True:
            code = self.child.wait(min(SIGNAL_SLICE, self.cfg.poll_interval))
            if code is not None:
                for signum in self.signals.take():
                    self.received_at_exit(signum)
                return code
            for signum in self.signals.take():
                self.forward(signum)
            now = self.monotonic()
            if self.kill_at is not None and now >= self.kill_at:
                self.kill()
            if now >= next_poll:
                next_poll = now + self.cfg.poll_interval
                if self.auto_exit and not self.ended:
                    self.check_auto_exit(now)
                self.check_bound(now)
                self.check_session_start(now)
                if self.deliver is not None:
                    self.deliver()  # post-state-change: the worker delivers new events

    def forward(self, signum: int) -> None:
        """Forward a signal ``exec`` received, once, its cause recorded first."""
        if self.ended:
            self._log_not_sent(signum, "received; not forwarded: the child was already signaled")
            return
        self.end(signum, self._cause(signum))

    def received_at_exit(self, signum: int) -> None:
        """A signal received before the child was reaped (e.g. a terminal hangup, which the
        child got too): nothing is sent, but the first terminating one records its cause
        (unless the run has one), as if it had been forwarded."""
        if self.ended:
            self._log_not_sent(signum, "received; not forwarded: the child was already signaled")
            return
        self.ended = True
        cause = self._cause(signum)
        self.st.set_pending_cause(self.alloc.csession_id, self.alloc.run, cause)
        self._log_not_sent(signum, f"received; {cause}; not sent: the child had exited")

    def _cause(self, signum: int) -> str:
        record = self.st.run_record(self.alloc.csession_id, self.alloc.run)
        return stop_cause(signum, record["stop_requested_at"] is not None)

    def end(self, signum: int, cause: str, *, recorded: bool = False) -> None:
        """Record ``cause`` (unless the run has one, or ``recorded``: the caller's
        compare-and-set just did), then send ``signum`` to the exact child and arm the
        SIGKILL after ``kill_grace``."""
        sid, run_no = self.alloc.csession_id, self.alloc.run
        if not recorded and not self.st.set_pending_cause(sid, run_no, cause):
            # An earlier cause stands: log the one recorded, not the one asked for.
            cause = self.st.run_record(sid, run_no)["pending_cause"] or cause
        self.ended = True
        self.kill_at = self.monotonic() + self.cfg.kill_grace
        self.child.send_signal(signum)
        self._log_sent(signum, cause)

    def kill(self) -> None:
        """SIGKILL the same child after ``kill_grace`` (``Child`` signals nothing once it is
        reaped, so a reused PID is never hit)."""
        self.kill_at = None
        if self.child.poll() is None:
            self.child.send_signal(signal.SIGKILL)
            self._log_sent(signal.SIGKILL, "kill_grace")

    def check_auto_exit(self, now: float) -> None:
        """Auto-exit (design.md, Supervision): once the run has a ``done`` report and its
        latest event is a qualifying Stop (or the report the Stop's own hook ingested;
        ``auto_exit_candidate``), wait ``auto_exit_delay``; then re-check that this event is
        still the latest and the report still ``done`` (in the same compare-and-set that
        records ``auto_exit``) and SIGTERM the child, with no further store write. A newer
        event resets the wait to the next qualifying Stop; a busy store keeps the candidate
        for the next poll."""
        sid, run_no = self.alloc.csession_id, self.alloc.run
        if self.st.run_record(sid, run_no)["report_status"] != "done":
            self.candidate = None
            return
        event_id = auto_exit_candidate(self.st.latest_events(sid, run_no, 2))
        if event_id is None:
            self.candidate = None
            return
        if self.candidate != event_id:
            self.candidate = event_id
            self.due = now + self.cfg.auto_exit_delay
        if now < self.due:
            return
        try:
            recorded = self.st.set_auto_exit_cause(sid, run_no, event_id)
        except sqlite3.OperationalError as exc:
            self._log_not_sent(
                signal.SIGTERM,
                f"auto_exit; not sent: the store was busy ({exc}); retried",
                level="warn",
            )
            return
        if recorded:
            self.end(signal.SIGTERM, "auto_exit", recorded=True)
        else:
            self.candidate = None

    def check_bound(self, now: float) -> None:
        """The run's bound (design.md, Supervision): once ``bound`` seconds have passed since
        the spawn, record ``pending_cause`` ``timeout`` (an earlier cause stands) and SIGTERM
        the child, SIGKILL after ``kill_grace``. A busy store sends nothing (the cause must be
        recorded first) and is retried at the next poll, up to the bound plus ``kill_grace``;
        then, or at once on any other store error, ``InternalError``: supervision fails and the
        internal-error path ends the child. Never after the child was signaled."""
        if self.ended or self.bound is None or now < self.spawned_at + self.bound:
            return
        try:
            self.end(signal.SIGTERM, "timeout")
        except sqlite3.OperationalError as exc:
            if not store.is_busy(exc):
                raise InternalError(f"the store failed at the run's bound: {exc}") from exc
            if now >= self.spawned_at + self.bound + self.cfg.kill_grace:
                raise InternalError(
                    f"the store stayed busy past the run's bound plus kill_grace: {exc}"
                ) from exc
            self._log_not_sent(
                signal.SIGTERM,
                f"timeout; not sent: the store was busy ({exc}); retried",
                level="warn",
            )

    def check_session_start(self, now: float) -> None:
        """Headless runs (interface.md, exec): no ``SessionStart`` within
        ``session_start_timeout`` of the spawn is logged once (``session_start_missing``)
        and added to the run's warnings (retried at the next poll while the store is busy);
        the run goes on. Checked once."""
        timeout = self.session_start_timeout
        if timeout is None or self.session_start_checked or now < self.spawned_at + timeout:
            return
        sid, run_no = self.alloc.csession_id, self.alloc.run
        warning = (
            f"no SessionStart within {timeout:g}s of the spawn: the injected hooks may be"
            " suppressed (disableAllHooks or managed-only hooks), so the run's state is not"
            " tracked"
        )
        if not self.session_start_logged:
            # Checked once: after the log line, a retry only retries the add, so the log and
            # the run's warnings never disagree.
            if self.st.run_record(sid, run_no)["session_started_at"] is not None:
                self.session_start_checked = True
                return
            self.session_start_logged = True
            launch.log_best_effort(
                self.log,
                "session_start_missing",
                level="warn",
                csession_id=sid,
                run=run_no,
                message=warning,
            )
        try:
            self.st.add_run_warnings(sid, run_no, [warning])
        except sqlite3.OperationalError:
            return  # retried at the next poll
        self.session_start_checked = True

    def _log_sent(self, signum: int, why: str) -> None:
        launch.log_best_effort(
            self.log,
            "signal_sent",
            csession_id=self.alloc.csession_id,
            run=self.alloc.run,
            pid=self.child.pid,
            signal=signal_name(signum),
            message=why,
        )

    def _log_not_sent(self, signum: int, why: str, *, level: str = "debug") -> None:
        """A signal ``exec`` did not send (received and dropped, or held back): never
        ``signal_sent``."""
        launch.log_best_effort(
            self.log,
            "signal_not_sent",
            level=level,
            csession_id=self.alloc.csession_id,
            run=self.alloc.run,
            signal=signal_name(signum),
            message=why,
        )


def end_child(child: Child, *, kill_grace: float) -> None:
    """SIGTERM to the exact child spawned (``Child`` signals nothing once it is reaped, so a
    reused PID is never hit), SIGKILL after ``kill_grace`` (also when the wait is
    interrupted), then reap it."""
    try:
        child.send_signal(signal.SIGTERM)
        child.wait(kill_grace)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGKILL)
            child.wait()


# Implicit done


def accept_result_md(
    text: object, *, run_fd: int, secret: str | None, artifact_max_bytes: int
) -> bytes | None:
    """The ``result.md`` bytes when the final result's text passes the ingestion checks
    (a string, at most ``artifact_max_bytes``, neither the run secret in any letter case nor
    a ``contains_secret`` value); None otherwise. A missing or unusable index rejects."""
    if not isinstance(text, str):
        return None
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(data) > artifact_max_bytes:
        return None
    if secret is not None and secret.encode("ascii").lower() in data.lower():
        return None
    try:
        index = secretindex.read_at(run_fd)
    except (OSError, secretindex.SecretIndexError):
        return None
    if index.contains(data):
        return None
    return data


def freeze_result_md(
    st: store.Store, sid: str, run_no: int, run_fd: int, data: bytes, private_run_dir: str
) -> outbox.Frozen:
    """Write ``artifact/<ingest_seq>/result.md`` in the private run directory (``ingest_seq``:
    the run's ingested requests plus this one)."""
    seq = st.ingested_count(sid, run_no) + 1
    artifact_fd = fsutil.ensure_private_dir_at(run_fd, outbox.ARTIFACT)
    try:
        seq_fd = fsutil.ensure_private_dir_at(artifact_fd, str(seq))
        try:
            fsutil.write_private_file_at(seq_fd, IMPLICIT_ARTIFACT, data)
        finally:
            os.close(seq_fd)
    finally:
        os.close(artifact_fd)
    return outbox.Frozen(
        name=IMPLICIT_ARTIFACT,
        path=os.path.join(private_run_dir, outbox.ARTIFACT, str(seq), IMPLICIT_ARTIFACT),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        ingest_seq=seq,
    )


# The end


def finish(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    process: launch.Process,
    *,
    returncode: int | None,
    implicit_done: bool,
    secret: str | None,
    mode: str = "headless",
) -> Ended:
    """After ``claude`` exited: final drain, end mapping, implicit done (headless),
    finalization, then the secret index deleted and the tmpdir removed (on every path).
    Returns the outcome's exit code."""
    sid, run_no = alloc.csession_id, alloc.run
    warnings: list[str] = []
    try:
        for _ in range(FINAL_DRAINS):
            # Again while requests wait: one drain may defer some at its deadline.
            drained = hookevent.final_drain(st, sid, run_no, process.state_root)
            if not drained or not _has_pending_requests(v, run_no):
                break
        pending = _has_pending_requests(v, run_no)
        if pending:
            warnings.append(
                "report requests of this run were not ingested: the final outbox drain could"
                " not process them (see log.jsonl, hook_event_failed)"
            )
        run_fd = v.dirs.open_run_dir(run_no)
        try:
            # An interactive run has no result.json (and no cost: null in its outcome).
            parsed = endmap.read_result_file(run_fd) if mode == "headless" else HeadlessResult()
            # The warnings known now are recorded with the compare-and-set, so the outcome
            # file carries them.
            known = tuple(warnings)
            won = _finalize(
                st,
                cfg,
                alloc,
                v,
                run_fd,
                parsed,
                returncode,
                implicit_done,
                pending,
                secret,
                mode,
                known,
            )
            if won:  # right after the compare-and-set, before anything else (hooks: #20)
                outcomefile.write_run(st, sid, run_no)
        finally:
            _delete_index(run_fd, warnings)
            os.close(run_fd)
    finally:
        _remove_tmpdir(alloc, v, warnings)
    st.add_run_warnings(sid, run_no, warnings)
    final = st.run_record(sid, run_no)
    return Ended(exit_code=int(final["exit_code"]), warnings=tuple(warnings))


def _finalize(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    run_fd: int,
    parsed: HeadlessResult,
    returncode: int | None,
    implicit_done: bool,
    pending: bool,
    secret: str | None,
    mode: str,
    warnings: tuple[str, ...] = (),
) -> bool:
    """Map the end (from the recorded ``pending_cause``) and finalize, requiring the report
    status it was mapped from: a report that landed in between is mapped again
    (``FINALIZE_ATTEMPTS``); ``warnings`` are recorded in the same transaction. Returns
    whether this call finalized the run."""
    sid, run_no = alloc.csession_id, alloc.run
    for _ in range(FINALIZE_ATTEMPTS):
        record = st.run_record(sid, run_no)
        if record["ended_at"] is not None:
            return False  # lost: another finalizer won
        if mode == "interactive":
            if returncode is None:
                raise InternalError("an interactive run's end needs claude's exit status")
            mapped = endmap.map_interactive(
                pending_cause=record["pending_cause"],
                report_status=record["report_status"],
                returncode=returncode,
            )
        else:
            mapped = endmap.map_headless(
                pending_cause=record["pending_cause"],
                report_status=record["report_status"],
                result=parsed,
                returncode=returncode,
                session_started=record["session_started_at"] is not None,
                implicit_done=implicit_done,
            )
        expected = record["report_status"]
        if mapped.implicit and pending:
            # A report the agent filed may wait unprocessed: never read the run as done.
            mapped = Mapped("exited-unreported", "result_rejected")
        elif mapped.implicit:
            accepted = _implicit(st, cfg, alloc, v, run_fd, parsed, secret)
            if accepted is None:
                continue  # a report was recorded meanwhile: map it
            mapped = accepted
            expected = "done" if accepted.outcome == "done" else None
        final = _final(mapped, parsed, warnings)
        if st.finalize_run(sid, run_no, final, report_status=expected):
            return True
    raise InternalError("the run's report kept changing while its end was mapped")


def _has_pending_requests(v: launch.Validated, run_no: int) -> bool:
    """Whether report requests wait in ``outbox/`` or the run's ``claimed/`` (best effort)."""
    for path in (
        os.path.join(v.dirs.session_path, outbox.OUTBOX),
        os.path.join(v.dirs.private_path, "runs", str(run_no), outbox.CLAIMED),
    ):
        try:
            if any(outbox.is_request_name(name) for name in os.listdir(path)):
                return True
        except OSError:
            continue
    return False


def _implicit(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    run_fd: int,
    parsed: HeadlessResult,
    secret: str | None,
) -> Mapped | None:
    """The implicit done (``result.md`` frozen and stored), ``result_rejected``, or None
    when a report was recorded since the end was mapped."""
    assert parsed.final is not None
    data = accept_result_md(
        parsed.final.get("result"),
        run_fd=run_fd,
        secret=secret,
        artifact_max_bytes=cfg.artifact_max_bytes,
    )
    if data is None:
        return Mapped("exited-unreported", "result_rejected")
    private_run_dir = os.path.join(v.dirs.private_path, "runs", str(alloc.run))
    frozen = freeze_result_md(st, alloc.csession_id, alloc.run, run_fd, data, private_run_dir)
    if not st.set_implicit_report(alloc.csession_id, alloc.run, frozen):
        return None  # a report was recorded meanwhile: never overwrite it
    return Mapped("done", "normal")


# End reasons whose final event has a cause of its own (the others: ``exit``).
_FINAL_CAUSES = {"auto_exit": "auto_exit", "timeout": "timeout"}


def _final(mapped: Mapped, parsed: HeadlessResult, warnings: tuple[str, ...] = ()) -> store.Final:
    is_error = None
    if parsed.final is not None and isinstance(parsed.final.get("is_error"), bool):
        is_error = parsed.final["is_error"]
    return store.Final(
        phase="session",
        outcome=mapped.outcome,
        end_reason=mapped.end_reason,
        exit_code=EXIT_CODES[mapped.outcome],
        state=endmap.final_state(mapped.outcome),
        # The final event's cause (interface.md, State transitions): auto_exit or timeout when
        # exec ended claude for auto-exit or at the run's bound, else exit.
        cause=_FINAL_CAUSES.get(mapped.end_reason, "exit"),
        cost_usd=parsed.cost_usd,
        num_turns=parsed.num_turns,
        is_error=is_error,
        warnings=warnings,
    )


def _remove_tmpdir(alloc: launch.Allocated, v: launch.Validated, warnings: list[str]) -> None:
    # Identity-checked: the run is final by now, and the next run of the session may have
    # created its own tmpdir at the same path; that one is left alone.
    if v.tmpdir is None or v.tmpdir_pin is None:
        return
    try:
        tmpdir.remove(alloc.csession_id, pin=v.tmpdir_pin)
    except tmpdir.TmpdirLeftover as exc:
        warnings.append(f"{exc}; {exc.hint}")
    except OSError as exc:
        warnings.append(f"could not remove the private temporary directory: {exc.strerror}")


def _delete_index(run_fd: int, warnings: list[str]) -> None:
    """The secret index is secret-equivalent: delete it once nothing scans for this run."""
    try:
        os.unlink(secretindex.INDEX_NAME, dir_fd=run_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        warnings.append(f"could not delete the run's secret index: {exc.strerror}")


# The whole run


def run_session(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    prepared: launch.Prepared,
    process: launch.Process,
    *,
    mode: str,
    implicit_done: bool,
    auto_exit: bool,
    signals: ExecSignals,
    bound: float | None = None,
) -> Ended:
    """Spawn, record, supervise and finish (see the module docs). A spawn that fails ends
    the run in the launch phase (exit 12); so does a signal received before the spawn
    (``LaunchInterrupted``, re-raised once the run is final, like a ``KeyboardInterrupt``).
    ``bound``: the run's wall-clock bound in seconds, counted from the spawn (None: none)."""
    sid, run_no = alloc.csession_id, alloc.run
    secret = prepared.run_secret
    signals.defer()  # nothing interrupts the spawn: a signal during it is forwarded after
    try:  # only the spawn here: its finally closes the output descriptors
        child = spawn(prepared, v, process, signals, mode=mode)
        spawned_at = time.monotonic()
    except Exception as exc:
        error = launch.interrupted(exc, _not_started(exc))
        launch.end_launch(st, alloc, v, None, process, error, exc=exc)
        return Ended(exit_code=EXIT_CODES["not-started"], warnings=())
    except BaseException as exc:
        error = launch.interrupted_by(st, alloc, exc)
        launch.end_launch(st, alloc, v, None, process, error, exc=exc)
        raise
    worker: toolhooks.Worker | None = None
    try:
        signals.enter_session(interactive=mode == "interactive")
        st.update_run(
            sid,
            run_no,
            phase="session",
            child_pid=child.pid,
            child_start=store.start_time(child.pid, zombie=True),  # unreaped: still ours
            started_at=store.format_ts(st.clock()),
        )
        record = st.run_record(sid, run_no)
        log = launch.event_log(process, sorted(prepared.secrets))
        launch.log_best_effort(
            log,
            "launch",
            csession_id=sid,
            run=run_no,
            pid=child.pid,
            decision_ids=list(_decision_ids(record)),
        )
        worker = _start_worker(st, cfg, alloc, process, signals)
        returncode = Supervision(
            st,
            cfg,
            alloc,
            child,
            signals,
            log,
            auto_exit=auto_exit,
            bound=bound,
            # Interactive runs are exempt: they may wait at the trust dialog.
            session_start_timeout=cfg.session_start_timeout if mode == "headless" else None,
            spawned_at=spawned_at,
            deliver=worker.wake,
        ).wait()
        # The child is gone: later signals are recorded, and cancel the remaining delivery.
        signals.enter_delivery()
    except BaseException:
        try:
            _internal_error(st, cfg, alloc, v, child)
        finally:
            if worker is not None:
                worker.close()
        raise
    try:
        return finish(
            st,
            cfg,
            alloc,
            v,
            process,
            returncode=returncode,
            implicit_done=implicit_done,
            secret=secret,
            mode=mode,
        )
    except BaseException:
        _internal_error(st, cfg, alloc, v, None)
        raise
    finally:
        worker.close()  # after finalization (and the outcome file): the final event too


def _start_worker(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    process: launch.Process,
    signals: ExecSignals,
) -> toolhooks.Worker:
    """The started ``post-state-change`` worker of the run's session (after the spawn); a
    signal after the child was reaped cancels its remaining delivery."""
    return toolhooks.start_worker(
        st,
        cfg,
        alloc.csession_id,
        gateway=process.gateway,
        environ=process.environ,
        state_root=process.state_root,
        cancel=signals.delivery_canceled,
    )


def _not_started(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return f"claude could not be started: {exc.strerror or type(exc).__name__}"
    return f"claude could not be started ({type(exc).__name__})"


def _decision_ids(record: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(record.get("decision_ids_json") or "[]")
    except ValueError:
        return []
    return [item for item in value if isinstance(item, str)]


def _ignore_sigint() -> Any:
    """Ignore SIGINT (a second Ctrl-C must not cut the cleanup short); returns the handler to
    restore, or None when this is not the main thread."""
    try:
        return signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        return None


def _internal_error(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    child: Child | None,
) -> None:
    """``exec`` must exit after an internal error: with SIGINT ignored, record
    ``internal_error`` before any signal (unless the run already has a cause, which stands),
    end the child, and finalize directly (``killed``,
    or the report's outcome when one exists; cost null), then delete the secret index and
    remove the tmpdir on every path. Best effort: a failure here is left to the liveness
    check (#33), which finds the wrapper dead."""
    sid, run_no = alloc.csession_id, alloc.run
    previous = _ignore_sigint()
    try:
        try:  # unless the run is final or already has a cause (e.g. auto_exit), which stands
            st.set_pending_cause(sid, run_no, "internal_error")
        except Exception:  # the child is ended either way
            pass
        if child is not None:
            try:
                end_child(child, kill_grace=cfg.kill_grace)
            except Exception:
                pass
        try:
            if _finalize_internal_error(st, sid, run_no):
                outcomefile.write_run(st, sid, run_no)
        except Exception:  # the original error is re-raised by the caller
            pass
    finally:
        warnings: list[str] = []
        try:
            run_fd = v.dirs.open_run_dir(run_no)
            try:
                _delete_index(run_fd, warnings)
            finally:
                os.close(run_fd)
        except OSError:
            pass
        _remove_tmpdir(alloc, v, warnings)
        try:
            st.add_run_warnings(sid, run_no, warnings)
        except Exception:
            pass
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _finalize_internal_error(st: store.Store, sid: str, run_no: int) -> bool:
    """Returns whether this call finalized the run."""
    for _ in range(FINALIZE_ATTEMPTS):
        record = st.run_record(sid, run_no)
        if record["ended_at"] is not None:
            return False
        mapped = endmap.map_headless(
            pending_cause="internal_error",
            report_status=record["report_status"],
            result=HeadlessResult(),
            returncode=None,
            session_started=record["session_started_at"] is not None,
            implicit_done=False,
        )
        if st.finalize_run(
            sid, run_no, _final(mapped, HeadlessResult()), report_status=record["report_status"]
        ):
            return True
    return False


# The outcome object


def report_of(row: dict[str, Any]) -> dict[str, Any] | None:
    """The run's report as the outcome object shows it (``report``), from a ``run_record``
    row; None when the run has none."""
    if row["report_status"] is None:
        return None
    artifact = None
    if row["artifact_name"] is not None:
        artifact = {
            "name": row["artifact_name"],
            "path": row["artifact_path"],
            "sha256": row["artifact_sha256"],
            "size": row["artifact_size"],
        }
    return {
        "status": row["report_status"],
        "implicit": bool(row["report_implicit"]),
        "summary": row["report_summary"],
        "reason": row["report_reason"],
        "artifact": artifact,
        "reported_at": row["reported_at"],
    }


def outcome_object(
    st: store.Store, sid: str, run_no: int, extra: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The run's outcome object (schemas/cli/outcome.json ``$defs/outcome``)."""
    return outcome_of_row(st.run_record(sid, run_no), extra)


def outcome_of_row(row: dict[str, Any], extra: tuple[str, ...] = ()) -> dict[str, Any]:
    """The outcome object of a ``run_record`` row (one read: every field from ``row``)."""
    report = report_of(row) if row["outcome"] in endmap.REPORT_OUTCOMES else None
    warnings = json.loads(row["warnings_json"] or "[]")
    return {
        "csession_id": row["csession_id"],
        "run": row["run_no"],
        "forked_from": row["session_forked_from"],
        "claude_session_id": row["session_claude_session_id"],
        "phase": row["phase"],
        "outcome": row["outcome"],
        "end_reason": row["end_reason"],
        "exit_code": row["exit_code"],
        "state": row["session_state"],
        "report": report,
        "session_started_at": row["session_started_at"],
        "cost_usd": None if row["phase"] == "launch" else row["cost_usd"],
        "message": row["message"],
        "warnings": list(dict.fromkeys([*warnings, *extra])),
    }
