"""``wait`` (interface.md, wait; design.md, Launch, adoption and pending runs, and Liveness and
finalization).

``wait_for_run`` binds to the session's current run at invocation (or to ``--run n``) and
polls that run every ``poll_interval`` until it is final, running the inline liveness check
at each poll. Each poll is one read of the store (``Store.waited_run``), so a waiter holds no
lock between polls; it writes only when the liveness check finalizes a dead run. A run that
is final returns at once. ``--timeout`` gives
exit 22 with the run as it stands (``timed_out``, ``pending``, the current report and live
state, ``phase`` ``launch`` until ``claude`` was spawned). A session forgotten (or deleted)
while waiting is ``NotFoundError`` (exit 10).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import endmap, ids, store
from .clock import Clock, SystemClock
from .errors import InternalError, NotFoundError

# `wait --timeout` expired while the run was still live (interface.md, Exit codes).
TIMED_OUT = 22
# The fields wait adds to the outcome object (schemas/cli/wait.json).
WAIT_FIELDS = ("timed_out", "run_exit_code", "pending")
# The largest SQLite INTEGER: a larger ``--run`` names no run.
SQLITE_INTEGER_MAX = 2**63 - 1


@dataclass(frozen=True)
class Waited:
    """``wait``'s exit code, its output (schemas/cli/wait.json) and the bound run's row as last
    read (what ``wait --outcome-file`` is checked against)."""

    exit_code: int
    output: dict[str, Any]
    row: dict[str, Any]


Check = Callable[[store.Store, str], None]


def check_liveness(st: store.Store, csession_id: str) -> None:
    """The inline liveness check every ``wait`` poll runs (``liveness.check_session``, cause
    ``liveness``): it finalizes a run whose wrapper is dead (salvaged, or ``not-started``
    when it never spawned ``claude``) and a pending run past its deadline
    (``never_adopted``), writing each one's outcome file; then the session's final runs
    whose outcome file is still unwritten are backfilled. ``wait`` (``cli``) passes its own
    check, with the config and the delivery of what it changed."""
    from . import liveness, outcomefile

    liveness.check_session(st, None, csession_id)
    outcomefile.backfill(st, csession_id)


def wait_for_run(
    st: store.Store,
    text: str,
    *,
    run_no: int | None,
    timeout: float | None,
    poll_interval: float,
    clock: Clock | None = None,
    on_row: Callable[[dict[str, Any]], None] | None = None,
    check: Check | None = None,
) -> Waited:
    """Wait for run ``run_no`` (None: the current run at invocation) of the session ``text``
    names (a full id or a prefix: ``ids.lookup_session_id``) to be final, at most ``timeout``
    seconds (None: no limit). Raises ``NotFoundError`` for an unknown or forgotten session, a
    run it does not have, and a session forgotten while waiting; ``AmbiguousError`` for an
    ambiguous prefix. ``clock`` (default: the system clock) gives the time and the sleep.
    ``on_row`` gets the bound run's row each time it is read (from binding on). ``check``:
    the liveness check run before each read (default ``check_liveness``)."""
    clock = clock or SystemClock()
    check = check or check_liveness
    seen = on_row or (lambda _row: None)
    deadline = None if timeout is None else clock.monotonic() + timeout
    sid = ids.lookup_session_id(text, st.session_id_candidates)
    if run_no is not None and not 1 <= run_no <= SQLITE_INTEGER_MAX:
        # No such run (runs count from 1; a query with a value past SQLite's 64-bit integer
        # range would raise OverflowError).
        raise NotFoundError(f"session {sid} has no run {run_no}")
    check(st, sid)
    row = st.waited_run(sid, run_no)
    if row is None:
        if run_no is None:
            raise NotFoundError(f"unknown session: {text}")
        raise NotFoundError(f"session {sid} has no run {run_no}")
    bound = int(row["run_no"])
    while True:
        seen(row)
        if row["ended_at"] is not None:
            return _final(st, sid, bound, row)
        now = clock.monotonic()
        if deadline is not None and now >= deadline:
            return _timed_out(st, sid, bound, seen, check)
        clock.sleep(poll_interval if deadline is None else min(poll_interval, deadline - now))
        check(st, sid)
        row = st.waited_run(sid, bound)
        if row is None:
            raise NotFoundError(f"session {sid} was forgotten while waiting")


def _final(st: store.Store, sid: str, run_no: int, row: dict[str, Any]) -> Waited:
    """The final run's outcome object; its state is the run's own final state (the
    session's may already be a later run's)."""
    from .supervisor import outcome_object

    output = outcome_object(st, sid, run_no)
    outcome, code = output["outcome"], output["exit_code"]
    if outcome is None or code is None:
        raise InternalError(f"run {run_no} of session {sid} is final without an outcome")
    output["state"] = endmap.final_state(outcome)
    output.update(timed_out=False, run_exit_code=int(code), pending=False)
    return Waited(exit_code=int(code), output=output, row=row)


def _timed_out(
    st: store.Store,
    sid: str,
    run_no: int,
    seen: Callable[[dict[str, Any]], None],
    check: Check,
) -> Waited:
    """``--timeout`` expired: the liveness check once more, then the run as one fresh read
    shows it, live (every field from that read). A run finalized since the last poll is
    reported final instead."""
    from .supervisor import outcome_of_row, report_of

    check(st, sid)
    row = st.run_record(sid, run_no)
    seen(row)
    if row["outcome"] is not None:
        return _final(st, sid, run_no, row)
    output = outcome_of_row(row)
    current = st.current_run(sid)
    if current is not None and current["run_no"] != run_no:
        # A superseded run the liveness check left open (its wrapper or child runs): its own
        # latest state, never the session's (a later run's).
        latest = st.latest_event(sid, run_no)
        output["state"] = latest["to_state"] if latest is not None else "starting"
    pending = row["wrapper_pid"] is None
    output.update(
        outcome=None,
        end_reason=None,
        exit_code=TIMED_OUT,
        # launch until claude was spawned (schemas/cli/outcome.json, phase), so always while
        # the run is pending.
        phase="launch" if pending or row["phase"] != "session" else "session",
        report=report_of(row),
        timed_out=True,
        run_exit_code=None,
        pending=pending,
    )
    return Waited(exit_code=TIMED_OUT, output=output, row=row)
