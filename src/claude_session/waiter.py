"""``wait`` (interface.md, wait; design.md, Launch, adoption and pending runs, and Liveness and
finalization).

``wait_for_run`` binds to the session's current run at invocation (or to ``--run n``) and
polls that run every ``poll_interval`` until it is final, running the inline liveness check
at each poll. Each poll is one read of the store (``Store.waited_run``), never a write, so a
waiter holds no lock between polls. A run that is final returns at once. ``--timeout`` gives
exit 22 with the run as it stands (``timed_out``, ``pending``, the current report and live
state, ``phase`` ``launch`` until ``claude`` was spawned). A session forgotten (or deleted)
while waiting is ``NotFoundError`` (exit 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import endmap, ids, store
from .clock import Clock, SystemClock
from .errors import InternalError, NotFoundError

# `wait --timeout` expired while the run was still live (interface.md, Exit codes).
TIMED_OUT = 22
# The fields wait adds to the outcome object (schemas/cli/wait.json).
WAIT_FIELDS = ("timed_out", "run_exit_code", "pending")


@dataclass(frozen=True)
class Waited:
    """``wait``'s exit code and its output (schemas/cli/wait.json)."""

    exit_code: int
    output: dict[str, Any]


def check_liveness(st: store.Store, csession_id: str) -> None:
    """Hook point for the inline liveness check every ``wait`` poll runs: it finalizes a run
    whose wrapper is dead (a pending run never adopted ends ``never_adopted``)."""
    # TODO(#33): run the single-session liveness check here (finalize when the wrapper is
    # dead, cause `liveness`, outcome-file backfill). Until then a run whose wrapper died
    # without finalizing keeps wait polling until --timeout.
    del st, csession_id


def wait_for_run(
    st: store.Store,
    text: str,
    *,
    run_no: int | None,
    timeout: float | None,
    poll_interval: float,
    clock: Clock | None = None,
) -> Waited:
    """Wait for run ``run_no`` (None: the current run at invocation) of the session ``text``
    names (a full id or a prefix: ``ids.lookup_session_id``) to be final, at most ``timeout``
    seconds (None: no limit). Raises ``NotFoundError`` for an unknown or forgotten session, a
    run it does not have, and a session forgotten while waiting; ``AmbiguousError`` for an
    ambiguous prefix. ``clock`` (default: the system clock) gives the time and the sleep."""
    clock = clock or SystemClock()
    deadline = None if timeout is None else clock.monotonic() + timeout
    sid = ids.lookup_session_id(text, st.session_id_candidates)
    check_liveness(st, sid)
    row = st.waited_run(sid, run_no)
    if row is None:
        if run_no is None:
            raise NotFoundError(f"unknown session: {text}")
        raise NotFoundError(f"session {sid} has no run {run_no}")
    bound = int(row["run_no"])
    while True:
        if row["ended_at"] is not None:
            return _final(st, sid, bound)
        now = clock.monotonic()
        if deadline is not None and now >= deadline:
            return _timed_out(st, sid, bound, row)
        clock.sleep(poll_interval if deadline is None else min(poll_interval, deadline - now))
        check_liveness(st, sid)
        row = st.waited_run(sid, bound)
        if row is None:
            raise NotFoundError(f"session {sid} was forgotten while waiting")


def _final(st: store.Store, sid: str, run_no: int) -> Waited:
    """The final run's outcome object; its state is the run's own final state (the
    session's may already be a later run's)."""
    from .supervisor import outcome_object

    output = outcome_object(st, sid, run_no)
    outcome, code = output["outcome"], output["exit_code"]
    if outcome is None or code is None:
        raise InternalError(f"run {run_no} of session {sid} is final without an outcome")
    output["state"] = endmap.final_state(outcome)
    output.update(timed_out=False, run_exit_code=int(code), pending=False)
    return Waited(exit_code=int(code), output=output)


def _timed_out(st: store.Store, sid: str, run_no: int, row: dict[str, Any]) -> Waited:
    """``--timeout`` expired: the run as ``row`` shows it, live. A run finalized since that
    read is reported final instead."""
    from .supervisor import outcome_object

    output = outcome_object(st, sid, run_no)
    if output["outcome"] is not None:
        return _final(st, sid, run_no)
    pending = row["wrapper_pid"] is None
    output.update(
        outcome=None,
        end_reason=None,
        exit_code=TIMED_OUT,
        # launch until claude was spawned (schemas/cli/outcome.json, phase), so always while
        # the run is pending.
        phase="launch" if pending or row["phase"] != "session" else "session",
        report=_current_report(row),
        timed_out=True,
        run_exit_code=None,
        pending=pending,
    )
    return Waited(exit_code=TIMED_OUT, output=output)


def _current_report(row: dict[str, Any]) -> dict[str, Any] | None:
    """The live run's latest report (the outcome object's ``report``), or None."""
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
