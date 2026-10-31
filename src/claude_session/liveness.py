"""The liveness check (interface.md, Liveness; design.md, Reconcile, salvage and retention).

One check, shared by every command that looks at a session's runs (``wait`` at each poll,
``resume`` and ``start --resume`` before allocation, ``reconcile`` for every session; ``stop``,
``forget``, ``status`` and ``list`` call it too). ``check_session`` looks at every run of the
session that is not final (the current run, and a superseded run a crash left open) and
finalizes it only when nobody owns it any more:

- a **pending** run (no wrapper) at or past its ``expires_at``: ``not-started``,
  ``never_adopted`` (``Store.expire_pending``), its launch spec deleted;
- a run whose **wrapper is dead** (gone or a zombie) and that has **no child** recorded (the
  launch phase: a ``start`` that died before the run became pending, an adopting or plain
  ``exec`` that died between allocation or the adoption compare-and-set and the spawn):
  ``not-started``, ``launch_interrupted`` (``canceled`` when the run has a ``stop`` request:
  ``stop`` during the launch window, interface.md, Outcome mapping), its launch spec deleted;
- a run whose **wrapper is dead and child alive**: left live and reported as an **orphan**
  (``reconcile`` lists it; ``stop`` ends it); nothing is ever signaled here;
- a run whose **wrapper and child are dead**: **salvaged** under the lease
  ``salvage:<id>:<run>`` (one salvager per run): its outbox and ``claimed/`` requests are
  ingested first (``hookevent.final_drain``, secret checked against that run's stored hash,
  with the run's secret index), then the end is mapped with the wrapper-lost rules
  (``endmap.map_wrapper_lost``: the cause table, ``finalized_from_cause``; a headless run's
  complete ``result.json``, ``finalized_from_result``, an implicit done checked as ``exec``
  checks it; a report, ``wrapper_lost``; else ``lost``).

Every finalization is a compare-and-set that also requires the wrapper and child the check
found dead to be still recorded (``Store.finalize_run(expect=...)``), so a run that changed
hands meanwhile is never finalized; there is exactly one final event per run. The final
event's cause is the caller's (``liveness``, or ``reconcile``), except ``timeout`` and
``auto_exit`` when ``exec`` recorded them before it died (as ``exec`` itself would). Right
after a won compare-and-set the run's outcome file is written (``outcomefile.write_run``);
then its secret index is deleted and the session's private tmpdir removed when the run is
the session's current one (``reconcile.remove_tmpdir``, identity-safe against a next run).

**Who is alive.** A wrapper or child is alive while the exact process (PID plus the recorded
start-time token, ``Store.liveness``) runs and is not a zombie. A token that could not be read
when it was recorded (``""``) is never taken as matching anything: the PID alone then decides,
and the process counts as alive while that PID exists (it may be the wrapper; a run is never
finalized on a guess). When the platform cannot answer, a process counts as alive too.

The check never delivers ``post-state-change`` events itself: the caller delivers what it
changed (``status`` and ``list`` never run hooks; ``reconcile`` delivers later), and writes
nothing in a dry run (``dry_run``: the changes it would make, not applied).

TODO(#16): ``views.check_liveness`` and ``views.check_liveness_all`` (``status``/``list``, on
the #65 line) call ``check_session`` (and ``sessions_with_open_runs`` for ``list``) without
delivering; ended runs keep their ``Store.known_liveness`` display.
``stop`` (``stopper``) runs ``check_session`` first (cause ``liveness``) and again, with cause
``stop``, to finalize a run whose wrapper it found gone or SIGKILLed.
TODO(#34): ``forget`` runs ``check_session`` (cause ``forget`` for its own changes,
``liveness`` for this check's) and delivers what it changed.
TODO(#36): ``verify-secret`` keeps its fail-closed reading (a run is live only when its
wrapper or child is confirmed alive by a non-empty token) and its constant-work probes; it may
reuse ``open_run_live`` only with that reading.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import endmap, hookevent, logs, outbox, outcomefile, secretindex, sessiondir, store
from .endmap import EXIT_CODES, HeadlessResult, Mapped

if TYPE_CHECKING:
    from .config import Config

LOG_NAME = "log.jsonl"
SALVAGE_LEASE = "salvage"
# Final drains of one salvaged run at most (as ``supervisor.FINAL_DRAINS``).
FINAL_DRAINS = 4
# Mappings of one end at most when a report keeps landing (as the supervisor's).
FINALIZE_ATTEMPTS = 3
# How long a check waits for another process's salvage of the same run (``patience``).
SALVAGE_POLL = 0.1
# End reasons whose final event keeps the cause exec recorded (as ``supervisor._FINAL_CAUSES``).
FINAL_CAUSES = {"auto_exit": "auto_exit", "timeout": "timeout"}
LAUNCH_LOST_MESSAGE = "the wrapper died before claude was spawned"
LAUNCH_CANCELED_MESSAGE = "launch canceled by stop: the wrapper ended before claude was spawned"
SECRET_NAME = sessiondir.SECRET

# Verdicts of ``classify``.
LIVE = "live"  # the wrapper runs (exec owns the run)
PENDING = "pending"  # no wrapper, before expires_at
EXPIRED = "expired"  # no wrapper, at or past expires_at
LAUNCH_LOST = "launch_lost"  # wrapper dead, no child recorded
ORPHAN = "orphan"  # wrapper dead, child alive
SALVAGE = "salvage"  # wrapper and child dead


@dataclass(frozen=True)
class Change:
    """One change, as ``reconcile --json`` reports it (schemas/cli/reconcile.json)."""

    csession_id: str
    run: int | None
    action: str
    from_state: str | None = None
    to_state: str | None = None
    outcome: str | None = None
    end_reason: str | None = None
    detail: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "csession_id": self.csession_id,
            "run": self.run,
            "action": self.action,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "outcome": self.outcome,
            "end_reason": self.end_reason,
            "detail": self.detail,
        }


@dataclass
class Checked:
    """What a check did (or, in a dry run, would do): the changes, the orphans (wrapper dead,
    child alive: ``{"csession_id", "run", "child_pid"}``) and warnings."""

    changes: list[Change] = field(default_factory=list)
    orphans: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: Checked) -> None:
        self.changes += other.changes
        self.orphans += other.orphans
        self.warnings += other.warnings

    @property
    def finalized(self) -> bool:
        """Whether a run was finalized (its events need delivering)."""
        return any(change.outcome is not None for change in self.changes)


# Who is alive


def process_alive(st: store.Store, pid: Any, start: Any) -> bool:
    """Whether the exact recorded process still runs (not a zombie). An empty token is never
    a match: the PID alone decides, alive while it exists. A PID that is not a valid process
    ID is not alive; an unanswerable check is alive (``Store.liveness``)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid < 2**31:
        return False
    token = start if isinstance(start, str) else ""
    if token == "":
        return store.pid_exists(pid)
    return bool(st.liveness(pid, token))


def classify(st: store.Store, row: dict[str, Any]) -> str:
    """The verdict for a run that is not final (see the module docs)."""
    if row["wrapper_pid"] is None:
        return EXPIRED if st.past_deadline(row["expires_at"]) else PENDING
    if process_alive(st, row["wrapper_pid"], row["wrapper_start"]):
        return LIVE
    if row["child_pid"] is None:
        return LAUNCH_LOST
    if process_alive(st, row["child_pid"], row["child_start"]):
        return ORPHAN
    return SALVAGE


def open_run_live(st: store.Store, row: dict[str, Any]) -> bool:
    """Whether a run that is not final stays live under the liveness rules (its wrapper or
    orphaned child runs, or it is pending before its deadline): the shared predicate."""
    return classify(st, row) in (LIVE, PENDING, ORPHAN)


# The check


def check_session(
    st: store.Store,
    cfg: Config | None,
    csession_id: str,
    *,
    cause: str = "liveness",
    dry_run: bool = False,
    patience: float = 0.0,
) -> Checked:
    """Apply the liveness rules to every run of the session that is not final (see the
    module docs). ``cfg`` gives ``artifact_max_bytes`` for an implicit done (None: such a
    salvage reads as ``result_rejected``, the conservative end). ``patience``: how long to
    wait for another process that is salvaging the same run (0: leave it to that process).
    Never raises for one run's failure: it is a warning, and the run is left as it was."""
    checked = Checked()
    state_root = os.path.dirname(st.path)
    for row in st.open_runs(csession_id):
        try:
            checked.extend(
                _check_run(
                    st, cfg, row, state_root, cause=cause, dry_run=dry_run, patience=patience
                )
            )
        except Exception as exc:
            st.rollback_open_transaction()
            warning = (
                f"{csession_id} run {row['run_no']}: the liveness check failed"
                f" ({type(exc).__name__}); the run is left as it was"
            )
            checked.warnings.append(warning)
            _log(state_root, "reconcile_change", "warn", csession_id, row["run_no"], warning)
    return checked


def _check_run(
    st: store.Store,
    cfg: Config | None,
    row: dict[str, Any],
    state_root: str,
    *,
    cause: str,
    dry_run: bool,
    patience: float,
) -> Checked:
    sid, run = str(row["csession_id"]), int(row["run_no"])
    verdict = classify(st, row)
    checked = Checked()
    if verdict in (LIVE, PENDING):
        return checked
    if verdict == ORPHAN:
        checked.orphans.append({"csession_id": sid, "run": run, "child_pid": row["child_pid"]})
        return checked
    before = row["session_state"]
    if dry_run:
        checked.changes.append(_predicted(st, row, verdict, state_root))
        return checked
    if verdict == EXPIRED:
        if st.expire_pending(sid, run, cause=cause):
            _after_launch_end(st, sid, run, state_root)
            checked.changes.append(_change(st, sid, run, "marked_not_started", before))
        return checked
    if verdict == LAUNCH_LOST:
        stopped = row["stop_requested_at"] is not None
        final = store.Final(
            phase="launch",
            outcome="not-started",
            end_reason="canceled" if stopped else "launch_interrupted",
            exit_code=EXIT_CODES["not-started"],
            state="not-started",
            cause=cause,
            message=LAUNCH_CANCELED_MESSAGE if stopped else LAUNCH_LOST_MESSAGE,
        )
        if st.finalize_run(sid, run, final, expect=_dead(row)):
            _after_launch_end(st, sid, run, state_root)
            checked.changes.append(_change(st, sid, run, "marked_not_started", before))
        return checked
    return _salvage(st, cfg, row, state_root, cause=cause, patience=patience)


def _dead(row: dict[str, Any]) -> dict[str, Any]:
    """The wrapper and child the check found dead: the compare-and-set requires them still."""
    return {
        "wrapper_pid": row["wrapper_pid"],
        "wrapper_start": row["wrapper_start"],
        "child_pid": row["child_pid"],
        "child_start": row["child_start"],
    }


def _after_launch_end(st: store.Store, sid: str, run: int, state_root: str) -> None:
    """After a won launch-phase end: the outcome file, then the launch spec and secret index
    deleted (both secret-equivalent) and the private tmpdir removed."""
    from . import lifecycle, reconcile

    outcomefile.write_run(st, sid, run)
    lifecycle.discard_spec_at(state_root, sid, run)
    _delete_index(state_root, sid, run)
    reconcile.remove_tmpdir(st, sid, expected_run=run)


def _change(
    st: store.Store, sid: str, run: int, action: str, before: str | None, detail: str | None = None
) -> Change:
    record = st.run_record(sid, run)
    return Change(
        csession_id=sid,
        run=run,
        action=action,
        from_state=before,
        to_state=endmap.final_state(record["outcome"]) if record["outcome"] else None,
        outcome=record["outcome"],
        end_reason=record["end_reason"],
        detail=detail,
    )


# Salvage


def _salvage(
    st: store.Store,
    cfg: Config | None,
    row: dict[str, Any],
    state_root: str,
    *,
    cause: str,
    patience: float,
) -> Checked:
    """Wrapper and child dead: ingest, map, finalize, under the run's salvage lease."""
    sid, run = str(row["csession_id"]), int(row["run_no"])
    checked = Checked()
    lease = f"{SALVAGE_LEASE}:{sid}:{run}"
    if not _take(st, lease, patience):
        return checked  # another process salvages it now
    try:
        if st.run_record(sid, run)["ended_at"] is not None:
            return checked
        before = st.run_record(sid, run)["session_state"]
        ingested = _final_drain(st, sid, run, state_root)
        if ingested:
            checked.changes.append(
                Change(sid, run, "outbox_ingested", before, None, None, None, "salvage")
            )
        won, action, warnings = _finalize_lost(st, cfg, row, state_root, cause=cause)
        if won:
            outcomefile.write_run(st, sid, run)
            checked.changes.append(_change(st, sid, run, action, before))
            if warnings:
                checked.warnings += [f"{sid} run {run}: {w}" for w in warnings]
    finally:
        try:
            st.release_lease(lease)
        except Exception:  # it expires on its own; a dead holder's is taken over at once
            pass
    if any(change.outcome is not None for change in checked.changes):
        _delete_index(state_root, sid, run)
        from . import reconcile

        reconcile.remove_tmpdir(st, sid, expected_run=run)
    return checked


def _take(st: store.Store, lease: str, patience: float) -> bool:
    deadline = time.monotonic() + patience
    while True:
        if st.take_lease(lease):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(SALVAGE_POLL)


def _final_drain(st: store.Store, sid: str, run: int, state_root: str) -> bool:
    """The salvaged run's final drain (``hookevent.final_drain``), repeated while requests
    wait (one drain may defer some at its deadline). Whether a drain was recorded."""
    recorded = False
    for _ in range(FINAL_DRAINS):
        drained = hookevent.final_drain(st, sid, run, state_root)
        recorded = recorded or drained
        if not drained or not _pending_requests(state_root, sid, run):
            break
    return recorded


def _pending_requests(state_root: str, sid: str, run: int) -> bool:
    """Whether report requests wait in ``outbox/`` or the run's ``claimed/`` (best effort,
    as ``supervisor._has_pending_requests``)."""
    for path in (
        os.path.join(sessiondir.session_dir(state_root, sid), outbox.OUTBOX),
        os.path.join(sessiondir.private_dir(state_root, sid), "runs", str(run), outbox.CLAIMED),
    ):
        try:
            if any(outbox.is_request_name(name) for name in os.listdir(path)):
                return True
        except OSError:
            continue
    return False


def _finalize_lost(
    st: store.Store,
    cfg: Config | None,
    row: dict[str, Any],
    state_root: str,
    *,
    cause: str,
) -> tuple[bool, str, list[str]]:
    """Map the wrapper-lost end and finalize it (the compare-and-set requires the report
    status it was mapped from and the dead wrapper and child). Returns whether this call
    won, the reconcile action and the warnings recorded with it."""
    sid, run = str(row["csession_id"]), int(row["run_no"])
    pending = _pending_requests(state_root, sid, run)
    warnings: list[str] = []
    if pending:
        warnings.append(
            "report requests of this run were not ingested: the salvage's final outbox drain"
            " could not process them (see log.jsonl, hook_event_failed)"
        )
    run_fd = _open_run_dir(state_root, sid, run)
    try:
        headless = row["mode"] == "headless"
        parsed = _result(run_fd) if headless else HeadlessResult()
        for _ in range(FINALIZE_ATTEMPTS):
            record = st.run_record(sid, run)
            if record["ended_at"] is not None:
                return False, "", []
            mapped = endmap.map_wrapper_lost(
                pending_cause=record["pending_cause"],
                report_status=record["report_status"],
                headless_result=parsed if headless else None,
                session_started=record["session_started_at"] is not None,
                implicit_done=_implicit_done(record),
            )
            expected = record["report_status"]
            if mapped.implicit and pending:
                mapped = Mapped("exited-unreported", "result_rejected")
            elif mapped.implicit:
                accepted = _implicit(st, cfg, record, run_fd, parsed, state_root)
                if accepted is None:
                    continue  # a report was recorded meanwhile: map it
                mapped = accepted
                expected = "done" if accepted.outcome == "done" else None
            final = _final(mapped, parsed, cause, tuple(warnings))
            if st.finalize_run(sid, run, final, report_status=expected, expect=_dead(row)):
                return True, _action(record, parsed, headless), warnings
            now = st.run_record(sid, run)
            if now["ended_at"] is not None:
                return False, "", []
            if any(now[key] != value for key, value in _dead(row).items()):
                return False, "", []  # the run changed hands: never finalized here
        return False, "", []
    finally:
        if run_fd is not None:
            os.close(run_fd)


def _action(record: dict[str, Any], parsed: HeadlessResult, headless: bool) -> str:
    """The reconcile action of a wrapper-lost end (schemas/cli/reconcile.json)."""
    if record["pending_cause"] is not None:
        return "finalized_from_cause"
    if headless and parsed.complete:
        return "finalized_from_result"
    return "marked_exited_wrapper_lost" if record["report_status"] is not None else "marked_lost"


def _final(
    mapped: Mapped, parsed: HeadlessResult, cause: str, warnings: tuple[str, ...]
) -> store.Final:
    is_error = None
    if parsed.final is not None and isinstance(parsed.final.get("is_error"), bool):
        is_error = parsed.final["is_error"]
    return store.Final(
        phase="session",
        outcome=mapped.outcome,
        end_reason=mapped.end_reason,
        exit_code=EXIT_CODES[mapped.outcome],
        state=endmap.final_state(mapped.outcome),
        cause=FINAL_CAUSES.get(mapped.end_reason, cause),
        cost_usd=parsed.cost_usd,
        num_turns=parsed.num_turns,
        is_error=is_error,
        warnings=warnings,
    )


def _implicit_done(record: dict[str, Any]) -> bool:
    """The run kind's ``implicit_done`` as resolved at launch; unreadable: False (the
    conservative end: never read as done)."""
    try:
        value = json.loads(record["resolved_json"] or "{}")["profile"]["implicit_done"]
    except (ValueError, KeyError, TypeError):
        return False
    return value is True


def _open_run_dir(state_root: str, sid: str, run: int) -> int | None:
    """The private ``runs/<run>/`` reached read only (no-follow, ours); None when missing or
    unsafe (then nothing in it is used: no result, no implicit done)."""
    try:
        return sessiondir.open_private_run_dir(state_root, sid, run)
    except (OSError, ValueError, sessiondir.SessionDirUnsafe):
        return None


def _result(run_fd: int | None) -> HeadlessResult:
    if run_fd is None:
        return HeadlessResult()
    try:
        return endmap.read_result_file(run_fd)
    except OSError:
        return HeadlessResult()


def _implicit(
    st: store.Store,
    cfg: Config | None,
    record: dict[str, Any],
    run_fd: int | None,
    parsed: HeadlessResult,
    state_root: str,
) -> Mapped | None:
    """The implicit done as ``exec`` decides it (``supervisor._implicit``): ``result.md``
    frozen and stored, ``result_rejected``, or None when a report was recorded since the end
    was mapped. The run secret must be the one the run stored (its digest), else the result
    is rejected (it cannot be checked)."""
    from . import supervisor

    sid, run = str(record["csession_id"]), int(record["run_no"])
    secret = _run_secret(state_root, sid, record["secret_sha256"])
    if cfg is None or run_fd is None or secret is None or parsed.final is None:
        return Mapped("exited-unreported", "result_rejected")
    data = supervisor.accept_result_md(
        parsed.final.get("result"),
        run_fd=run_fd,
        secret=secret,
        artifact_max_bytes=cfg.artifact_max_bytes,
    )
    if data is None:
        return Mapped("exited-unreported", "result_rejected")
    private_run_dir = os.path.join(sessiondir.private_dir(state_root, sid), "runs", str(run))
    frozen = supervisor.freeze_result_md(st, sid, run, run_fd, data, private_run_dir)
    if not st.set_implicit_report(sid, run, frozen):
        return None
    return Mapped("done", "normal")


def _run_secret(state_root: str, sid: str, digest: Any) -> str | None:
    """The run secret from ``$CSESSION_DIR/secret`` when its SHA-256 is the digest the run
    stored (the file is in the agent-writable session directory); None otherwise."""
    if not isinstance(digest, str):
        return None
    try:
        data = sessiondir.read_session_file(state_root, sid, SECRET_NAME, 4096)
    except (OSError, ValueError, sessiondir.SessionDirUnsafe):
        return None
    if data is None:
        return None
    text = data.decode("ascii", "replace").strip()
    if not hmac.compare_digest(hashlib.sha256(text.encode()).hexdigest(), digest):
        return None
    return text


def _delete_index(state_root: str, sid: str, run: int) -> None:
    """The run's ``secret-index`` is secret-equivalent: deleted once the run is final."""
    try:
        sessiondir.remove_private_run_file(state_root, sid, run, secretindex.INDEX_NAME)
    except (OSError, ValueError, sessiondir.SessionDirUnsafe):
        pass


# Dry run


def _launch_end_reason(row: dict[str, Any], verdict: str) -> str:
    if verdict == EXPIRED:
        return "never_adopted"
    return "canceled" if row["stop_requested_at"] is not None else "launch_interrupted"


def _predicted(st: store.Store, row: dict[str, Any], verdict: str, state_root: str) -> Change:
    """The change a check would make (nothing is read that a real one would ingest first, so
    the outcome is the one the run's state now maps to)."""
    sid, run = str(row["csession_id"]), int(row["run_no"])
    detail = "dry run: not applied"
    before = row["session_state"]
    if verdict in (EXPIRED, LAUNCH_LOST):
        return Change(
            sid,
            run,
            "marked_not_started",
            from_state=before,
            to_state="not-started",
            outcome="not-started",
            end_reason=_launch_end_reason(row, verdict),
            detail=detail,
        )
    headless = row["mode"] == "headless"
    run_fd = _open_run_dir(state_root, sid, run)
    try:
        parsed = _result(run_fd) if headless else HeadlessResult()
    finally:
        if run_fd is not None:
            os.close(run_fd)
    mapped = endmap.map_wrapper_lost(
        pending_cause=row["pending_cause"],
        report_status=row["report_status"],
        headless_result=parsed if headless else None,
        session_started=row["session_started_at"] is not None,
        implicit_done=_implicit_done(row),
    )
    reported = row["report_status"] is not None or mapped.implicit
    state = "exited" if reported else endmap.final_state(mapped.outcome)
    return Change(
        sid,
        run,
        _action(row, parsed, headless),
        before,
        state,
        mapped.outcome,
        mapped.end_reason,
        detail,
    )


# Log


def _log(
    state_root: str, event: str, level: str, sid: str | None, run: int | None, message: str
) -> None:
    try:
        logs.EventLog(os.path.join(state_root, LOG_NAME)).write(
            event, level=level, csession_id=sid, run=run, message=message
        )
    except (OSError, ValueError):
        pass


def log_change(state_root: str, change: Change) -> None:
    """A ``reconcile_change`` record for one change (best effort)."""
    text = f"{change.action}"
    if change.outcome is not None:
        text += f": {change.outcome} ({change.end_reason})"
    if change.detail:
        text += f"; {change.detail}"
    _log(state_root, "reconcile_change", "info", change.csession_id, change.run, text)
