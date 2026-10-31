"""``--outcome-file`` writes (interface.md, Outcome file; design.md, Data model and Outcome
file).

Every write goes through ``write``: the path is first checked with
``pathrules.outcome_file_hop`` against the paths an agent could write, and a rejected path is
never written; then ``fsutil.write_private_file`` (realpath of the parent, opened one
component at a time without following symlinks, temporary file plus rename, mode 0600,
fsynced). A failure or a rejection is a warning, never an error: it never changes a run's
outcome or a command's exit code.

What is checked (``baseline_writable``): every write refuses ``<state>/sessions`` (every
session's ``$CSESSION_DIR``) and the tmp root of the private temporary directories, which the
agents of isolated runs may write whatever run the file belongs to. A run's own write
(``write_run``) of an isolated run also refuses the run's cwd and, in the finalizing
``exec``, the run's full set of session-writable paths (``launch.Validated.writable``).

A run's outcome file is written exactly once (``write_run``): by whichever process wins the
finalization compare-and-set, right after it, and recorded in ``outcome_file_written_at``;
``backfill`` writes it for final runs where that is unset (a finalizer that died before
writing). Writers serialize on the lease ``outcome-file:<id>:<run>`` and re-read the run
under it, so a backfill racing the finalizer writes the file at most once. A failed or
rejected write is recorded as done too: the warning is logged (``outcome_file_failed``) and
added to the run's warnings, and the backfill does not retry it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from . import endmap, fsutil, logs, pathrules, sessiondir, store

LOG_NAME = "log.jsonl"
NOT_WRITTEN = "could not write --outcome-file"
REJECTED = "--outcome-file not written: it is under a session-writable path"
LEASE_PREFIX = "outcome-file"

Writable = Callable[[str], bool]


def baseline_writable(state_root: str, *extra: str) -> pathrules.SessionWritable:
    """What every outcome-file write refuses: ``<state>/sessions`` and the tmp root, plus
    ``extra`` (a run's cwd)."""
    paths = [os.path.join(state_root, sessiondir.SESSIONS), pathrules.tmp_parent(), *extra]
    return pathrules.SessionWritable(paths)


def absolute(path: str) -> str:
    """``path`` joined to the cwd. Not ``abspath``: ``..`` must be resolved after symlinks
    (realpath in ``fsutil.write_private_file``)."""
    return os.path.join(os.getcwd(), path)


def encode(output: Any) -> bytes:
    """The bytes written: what ``--json`` prints, one line."""
    return (json.dumps(output, ensure_ascii=False, allow_nan=False) + "\n").encode()


def write(path: str, data: bytes, writable: Writable) -> str | None:
    """Write ``data`` to the absolute ``path`` unless it is rejected (``REJECTED``, nothing
    written). Returns the warning of a rejected or failed write, None when written."""
    try:
        if pathrules.outcome_file_hop(path, writable) is not None:
            return REJECTED
        fsutil.write_private_file(path, data)
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or type(exc).__name__
        return f"{NOT_WRITTEN}: {reason}"
    return None


def log_failure(
    state_root: str, warning: str, *, csession_id: str | None = None, run: int | None = None
) -> None:
    """``outcome_file_failed`` in ``log.jsonl`` (best effort)."""
    try:
        logs.EventLog(os.path.join(state_root, LOG_NAME)).write(
            "outcome_file_failed",
            level="warn",
            csession_id=csession_id,
            run=run,
            message=warning,
        )
    except (OSError, ValueError):
        pass


def run_isolated(row: dict[str, Any]) -> bool:
    """Whether a stored run is isolated (``kinds.Resolution.isolated``: third-party-reading
    or a sandbox that is not ``off``), from its ``resolved_json``; unreadable: isolated."""
    try:
        profile = json.loads(row.get("resolved_json") or "{}")["profile"]
        sandbox = profile["sandbox"]
        reads = profile["reads_third_party"]
    except (ValueError, KeyError, TypeError):
        return True
    return bool(reads) or bool(row.get("reads_third_party")) or sandbox != "off"


def run_writable(row: dict[str, Any], state_root: str, extra: Writable | None = None) -> Writable:
    """What a write of a run's outcome file refuses: ``baseline_writable`` with the run's cwd
    when it is isolated, plus ``extra`` (the run's full session-writable set)."""
    cwd = row.get("cwd")
    base = baseline_writable(state_root, *([cwd] if cwd and run_isolated(row) else []))
    if extra is None:
        return base
    return lambda path: base(path) or extra(path)


def final_output(row: dict[str, Any]) -> dict[str, Any]:
    """A final run's outcome object (what ``exec --json`` prints), its state the run's own
    final state (the session's may be a later run's by the time of a backfill)."""
    from .supervisor import outcome_of_row

    output = outcome_of_row(row)
    output["state"] = endmap.final_state(row["outcome"])
    return output


def write_run(
    st: store.Store, csession_id: str, run_no: int, *, extra: Writable | None = None
) -> str | None:
    """Write the final run's stored outcome file once (see the module docs). Returns the
    warning (also logged and added to the run's warnings), or None: written, nothing to
    write, or another process writing it. Never raises an ``Exception``."""
    state_root = os.path.dirname(st.path)
    lease = f"{LEASE_PREFIX}:{csession_id}:{run_no}"
    warning: str | None = None
    try:
        if not st.take_lease(lease):
            return None  # another writer holds it: it writes the file
        try:
            row = st.run_record(csession_id, run_no)
            if (
                row["ended_at"] is None
                or row["outcome_file"] is None
                or row["outcome_file_written_at"] is not None
            ):
                return None
            data = encode(final_output(row))
            warning = write(row["outcome_file"], data, run_writable(row, state_root, extra))
            st.mark_outcome_file_written(csession_id, run_no)
        finally:
            try:
                st.release_lease(lease)
            except Exception:  # expires on its own; a dead holder's is taken over at once
                pass
    except Exception as exc:  # a store error: unrecorded, so the backfill writes it later
        warning = f"{NOT_WRITTEN} now ({type(exc).__name__}); reconcile writes it later"
    if warning is not None:
        log_failure(state_root, warning, csession_id=csession_id, run=run_no)
        try:
            st.add_run_warnings(csession_id, run_no, [warning])
        except Exception:
            pass
    return warning


def backfill(st: store.Store, csession_id: str | None = None) -> list[str]:
    """Write the outcome files of final runs (of ``csession_id``, or of every session) whose
    ``outcome_file_written_at`` is unset. Returns the warnings."""
    warnings: list[str] = []
    for sid, run_no in st.unwritten_outcome_files(csession_id):
        warning = write_run(st, sid, run_no)
        if warning is not None:
            warnings.append(f"{sid} run {run_no}: {warning}")
    return warnings
