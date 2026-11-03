"""``--outcome-file`` writes (interface.md, Outcome file; design.md, Data model and Outcome
file).

Every write goes through ``write``: the path is first checked with
``pathrules.outcome_file_hop`` against the paths an agent could write, and a rejected path is
never written; then ``fsutil.write_private_file`` (realpath of the parent, opened one
component at a time without following symlinks, temporary file plus rename, mode 0600,
fsynced). A failure or a rejection is a warning, never an error: it never changes a run's
outcome or a command's exit code.

What is checked: every write refuses ``<state>/sessions`` (every session's
``$CSESSION_DIR``) and the tmp root of the private temporary directories, which the agents of
isolated runs may write whatever run the file belongs to (``baseline_writable``). A write for
a run (``run_writable``: the run's own write, ``wait``) also refuses, for an isolated run, its
cwd and the session-writable paths stored on the run (``writable_json``: the full set from
launch step 5, or what the launch knew when it ended before step 5 completed). An isolated
run with no stored set is not written at all (``UNDETERMINED``: fail closed).

A run's outcome file is written at most once (``write_run``), by whichever process wins the
finalization compare-and-set, right after it; ``backfill`` writes it for final runs whose
write was never claimed (a finalizer that died, or a store error). Writers serialize on the
lease ``outcome-file:<id>:<run>``, re-read the run under it and CLAIM the write before
writing: ``outcome_file_written_at`` is set by a compare-and-set, and only the claimant
writes. So a writer that lost the lease while stalled, or a store failure anywhere, never
leads to a second write; a store failure before the claim leaves the write to the backfill,
and a crash between the claim and the write leaves no file (the outcome stays in ``show``
and ``wait``). A failed or rejected write after the claim is not retried: the warning is
logged (``outcome_file_failed``) and added to the run's warnings.
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
UNDETERMINED = (
    "--outcome-file not written: the run's session-writable paths could not be determined"
)
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


def write(path: str, data: bytes, writable: Writable | None) -> str | None:
    """Write ``data`` to the absolute ``path`` unless it is rejected (``REJECTED``, nothing
    written) or ``writable`` is None (``UNDETERMINED``: the paths to refuse are not known).
    Returns the warning of a rejected, skipped or failed write, None when written."""
    if writable is None:
        return UNDETERMINED
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


def stored_writable(row: dict[str, Any]) -> list[str] | None:
    """The session-writable paths stored on a run (``writable_json``), or None when none are
    stored or they are unreadable."""
    try:
        paths = json.loads(row.get("writable_json") or "null")
    except ValueError:
        return None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return None
    return paths


def run_writable(row: dict[str, Any], state_root: str) -> Writable | None:
    """What a write for a run refuses: ``baseline_writable``, plus, for an isolated run, its
    cwd and its stored session-writable paths. None for an isolated run without stored paths
    (not determined: nothing is written)."""
    if not run_isolated(row):
        return baseline_writable(state_root)
    paths = stored_writable(row)
    if paths is None:
        return None
    cwd = row.get("cwd")
    return baseline_writable(state_root, *([cwd] if cwd else []), *paths)


def final_output(row: dict[str, Any]) -> dict[str, Any]:
    """A final run's outcome object (what ``exec --json`` prints), its state the run's own
    final state (the session's may be a later run's by the time of a backfill)."""
    from .supervisor import outcome_of_row

    output = outcome_of_row(row)
    output["state"] = endmap.final_state(row["outcome"])
    return output


def write_run(st: store.Store, csession_id: str, run_no: int) -> str | None:
    """Write the final run's stored outcome file at most once (see the module docs). Returns
    the warning (also logged and added to the run's warnings), or None: written, nothing to
    write, or another process claimed it. Never raises an ``Exception``."""
    state_root = os.path.dirname(st.path)
    lease = f"{LEASE_PREFIX}:{csession_id}:{run_no}"
    try:
        warning = _claim_and_write(st, csession_id, run_no, lease, state_root)
    except Exception as exc:  # a store error before the claim: the backfill writes it later
        warning = f"{NOT_WRITTEN} now ({type(exc).__name__}); reconcile writes it later"
    finally:
        try:
            st.release_lease(lease)
        except Exception:  # expires on its own; a dead holder's is taken over at once
            pass
    if warning is not None:
        log_failure(state_root, warning, csession_id=csession_id, run=run_no)
        try:
            st.add_run_warnings(csession_id, run_no, [warning])
        except Exception:
            pass
    return warning


def _claim_and_write(
    st: store.Store, csession_id: str, run_no: int, lease: str, state_root: str
) -> str | None:
    """Under the lease: re-read the run, claim the write, then write. Store errors raise
    (only before the claim: nothing touches the store after it)."""
    if not st.take_lease(lease):
        return None  # another writer holds it: it claims and writes the file
    row = st.run_record(csession_id, run_no)
    if (
        row["ended_at"] is None
        or row["outcome_file"] is None
        or row["outcome_file_written_at"] is not None
    ):
        return None
    data = encode(final_output(row))
    writable = run_writable(row, state_root)
    if not st.claim_outcome_file(csession_id, run_no):
        return None  # another writer (one that took a stalled holder's lease) claimed it
    try:
        return write(row["outcome_file"], data, writable)
    except Exception as exc:  # claimed: never reported as left to the backfill
        return f"{NOT_WRITTEN}: {type(exc).__name__}"


def backfill(st: store.Store, csession_id: str | None = None) -> list[str]:
    """Write the outcome files of final runs (of ``csession_id``, or of every session) whose
    ``outcome_file_written_at`` is unset. Returns the warnings."""
    warnings: list[str] = []
    for sid, run_no in st.unwritten_outcome_files(csession_id):
        warning = write_run(st, sid, run_no)
        if warning is not None:
            warnings.append(f"{sid} run {run_no}: {warning}")
    return warnings
