"""``reconcile`` (interface.md, reconcile; design.md, Reconcile, salvage and retention).

Repairs state against reality, safe to run from cron next to everything else: one
``reconcile`` lease (a second caller reports ``already_running`` and changes nothing), renewed
between the steps and sessions and, during long steps, from their own loops (the hook
delivery and the hook-group grace through a heartbeat, the outbox discard and the tmpdir
removal per item; ``_Lease``): once the lease is found taken over the pass stops. No long store
lock: each step is its own short transaction. It never signals a session process (wrapper or
child). In order:

1. the ``post-state-change`` hook process groups that dead deliverers left running are ended
   (``toolhooks.end_orphaned_groups``: verified leaders only; best effort, a store error is
   logged and the pass goes on);
2. the liveness check of every session with a run that is not final (``liveness``, cause
   ``reconcile``): pending runs past their deadline, wrapper-lost runs finalized and salvaged,
   orphans (wrapper dead, child alive) listed and never signaled;
3. the outcome files of final runs never written (``outcomefile.backfill``), then the secret
   index and launch spec a final run still holds (its finalizer died first) deleted
   (``secret_files_deleted``);
4. outbox requests waiting for a session whose current run is final: discarded under that run
   (``discarded``) and logged (``outbox_discarded``), at most ``DISCARD_MAX`` per session and
   pass, in transactions of ``DISCARD_BATCH`` that each check the run again (no run of that
   session can be allocated between the check and the move); deleted and logged after each
   commit. A final run is never ingested into; an outbox that cannot be trusted is left alone
   with a warning;
5. undelivered events delivered (``toolhooks.deliver``), the final events of runs that
   ``status``, ``list`` or a liveness check finalized included;
6. private temporary directories of sessions whose current run is final, or that were
   deleted, removed (``tmpdir.remove_checked``), then the ``.stale-*`` entries of such
   sessions (``tmpdir.remove_stale``). Ids this state directory does not know are never
   touched: another state directory may share the per-uid root.

``--dry-run`` takes no lease and writes nothing: it reports the changes the liveness check,
the outbox discard, the delivery (only events a delivery would deliver now: none behind a run
whose wrapper is live) and the tmpdir removal would make (``detail``: ``dry run``).

TODO(#34): forgotten sessions older than ``retention_days`` are deleted here (rows, both
directories and the tmpdir, fd-based; the id kept in ``deleted_ids``), action ``deleted``.
TODO(#40): add ``upgrades.hook_interpreter_warning(run.hook_interpreter)`` to ``warnings``
for every live run (on the #77 line).
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from . import liveness, logs, outcomefile, secretindex, sessiondir, store, tmpdir, toolhooks
from .liveness import Change, Checked

if TYPE_CHECKING:
    from .config import Config
    from .gateway import Gateway

LEASE = "reconcile"
LOG_NAME = "log.jsonl"
DRY = "dry run: not applied"
# The lease is renewed at least this often while reconcile works (interface.md: every 20 s;
# a long step renews it from its own loop, at most every half of that).
RENEW_EVERY = store.LEASE_RENEW_INTERVAL.total_seconds()
# Outbox requests discarded per transaction, and per session in one pass at most (the rest
# is left for the next pass and reported).
DISCARD_BATCH = 100
DISCARD_MAX = 2000


class LeaseLost(Exception):
    """The ``reconcile`` lease was taken over while reconcile worked."""


class _Lease:
    """The held ``reconcile`` lease: ``renew`` at a step boundary (raises ``LeaseLost`` once
    it was lost), ``heartbeat`` from inside a long step (time-based, never raises: a lost
    lease is noticed at the next boundary). A renewal that fails with a store error is
    retried at the next call."""

    def __init__(self, st: store.Store) -> None:
        self.st = st
        self.lost = False
        self.renewed = time.monotonic()

    def heartbeat(self) -> None:
        if not self.lost and time.monotonic() - self.renewed >= RENEW_EVERY / 2:
            self._renew()

    def renew(self) -> None:
        if not self.lost:
            self._renew()
        if self.lost:
            raise LeaseLost

    def _renew(self) -> None:
        asked = time.monotonic()
        try:
            held = self.st.renew_lease(LEASE)
        except sqlite3.Error:
            self.st.rollback_open_transaction()
            return
        if held:
            self.renewed = asked
        else:
            self.lost = True


class _DryLease(_Lease):
    """A dry run takes no lease: nothing to renew."""

    def heartbeat(self) -> None:
        return

    def renew(self) -> None:
        return


def run(
    st: store.Store,
    cfg: Config,
    *,
    gateway: Gateway,
    environ: Mapping[str, str],
    dry_run: bool = False,
) -> dict[str, Any]:
    """One ``reconcile`` pass; the ``reconcile --json`` object (schemas/cli/reconcile.json)."""
    state_root = os.path.dirname(st.path)
    result: dict[str, Any] = {
        "already_running": False,
        "dry_run": dry_run,
        "changes": [],
        "orphans": [],
        "warnings": [],
    }
    checked = Checked()
    if dry_run:
        _pass(st, cfg, checked, state_root, gateway, environ, dry_run=True, lease=_DryLease(st))
    else:
        if not st.take_lease(LEASE):
            result["already_running"] = True
            return result
        try:
            lease = _Lease(st)
            _pass(st, cfg, checked, state_root, gateway, environ, dry_run=False, lease=lease)
        except LeaseLost:
            checked.warnings.append("the reconcile lease was taken over; this pass stopped")
        finally:
            try:
                st.release_lease(LEASE)
            except Exception:  # it expires on its own
                st.rollback_open_transaction()
        for change in checked.changes:
            liveness.log_change(state_root, change)
    result["changes"] = [change.as_json() for change in checked.changes]
    result["orphans"] = checked.orphans
    result["warnings"] = list(dict.fromkeys(checked.warnings))
    return result


def _pass(
    st: store.Store,
    cfg: Config,
    checked: Checked,
    state_root: str,
    gateway: Gateway,
    environ: Mapping[str, str],
    *,
    dry_run: bool,
    lease: _Lease,
) -> None:
    if not dry_run:
        toolhooks.end_orphaned_groups(  # best effort: never raises
            st,
            None,
            kill_grace=cfg.kill_grace,
            log=logs.EventLog(_log_path(state_root)),
            heartbeat=lease.heartbeat,
        )
        lease.renew()
    for sid in st.sessions_with_open_runs():
        checked.extend(liveness.check_session(st, cfg, sid, cause="reconcile", dry_run=dry_run))
        lease.renew()
    if not dry_run:
        checked.warnings += outcomefile.backfill(st)
        lease.renew()
        _sweep_secret_files(st, state_root, checked, lease)
        lease.renew()
    for sid in st.session_ids():
        _discard_outbox(st, sid, state_root, checked, dry_run=dry_run, lease=lease)
        lease.heartbeat()
    lease.renew()
    for sid in st.sessions_with_undelivered():
        _deliver(st, cfg, sid, state_root, gateway, environ, checked, dry_run=dry_run, lease=lease)
        lease.renew()
    _tmpdirs(st, checked, dry_run=dry_run, lease=lease)


def _log_path(state_root: str) -> str:
    return os.path.join(state_root, LOG_NAME)


# Secret-equivalent files of final runs


def _sweep_secret_files(st: store.Store, state_root: str, checked: Checked, lease: _Lease) -> None:
    """Delete the ``secret-index`` and ``launch-spec.json`` a final run's private directory
    still holds (a process that finalized it died before deleting them): no-follow, each a
    ``secret_files_deleted`` change. Idempotent."""
    from . import lifecycle

    names = (secretindex.INDEX_NAME, lifecycle.SPEC_NAME)
    for sid, run_no in st.final_runs():
        lease.heartbeat()
        try:
            deleted = sessiondir.remove_private_run_files(state_root, sid, run_no, names)
        except (OSError, ValueError, sessiondir.SessionDirUnsafe) as exc:
            checked.warnings.append(
                f"{sid} run {run_no}: its secret-equivalent files could not be deleted ({exc})"
            )
            continue
        if deleted:
            checked.changes.append(
                Change(sid, run_no, "secret_files_deleted", detail=", ".join(deleted))
            )


# Outbox requests of final runs


def _waiting_requests(state_root: str, sid: str, checked: Checked) -> tuple[list[str], bool]:
    """Up to ``DISCARD_MAX`` requests waiting in the session's outbox (no-follow walk, as
    every other outbox access) and whether more wait. An outbox that cannot be trusted has
    nothing to discard: a warning, the same in a dry run."""
    try:
        return sessiondir.outbox_request_names(state_root, sid, limit=DISCARD_MAX)
    except (OSError, ValueError, sessiondir.SessionDirUnsafe) as exc:
        checked.warnings.append(f"{sid}: its outbox cannot be trusted and is left alone ({exc})")
        return [], False


def _discard_outbox(
    st: store.Store,
    sid: str,
    state_root: str,
    checked: Checked,
    *,
    dry_run: bool,
    lease: _Lease,
) -> None:
    """Requests waiting in the outbox of a session whose current run is final are discarded
    under that run (interface.md, reconcile), at most ``DISCARD_MAX`` per pass, in short
    transactions of ``DISCARD_BATCH``: each checks again that the current run is that final
    run and moves its batch out of the outbox (so no run of the session is allocated in
    between) and records it; the moved requests are deleted and logged after the commit."""
    names, more = _waiting_requests(state_root, sid, checked)
    if not names:
        return
    owner = st.tmpdir_owner(sid)
    if owner is None or not owner[1]:
        return  # a live (or pending) run: its requests are its own
    run_no = owner[0]
    left = "; more are left for the next pass" if more else ""
    if dry_run:
        checked.changes.append(
            Change(sid, run_no, "outbox_discarded", detail=f"{len(names)} request(s){left}; {DRY}")
        )
        return
    discarded: list[str] = []
    log = logs.EventLog(_log_path(state_root))
    try:
        with sessiondir.SessionDirs.open(state_root, sid) as dirs:
            for at in range(0, len(names), DISCARD_BATCH):
                with st.transaction() as conn:
                    if st.tmpdir_owner(sid) != (run_no, True):
                        break
                    moved = dirs.move_outbox_requests(run_no, names[at : at + DISCARD_BATCH])
                    st.record_discarded(conn, sid, run_no, [rid for rid, _ in moved])
                for path in dirs.remove_discarded(run_no, [trash for _, trash in moved]):
                    checked.warnings.append(f"{sid}: {path} could not be removed")
                for request_id, _ in moved:
                    _log_discarded(log, sid, run_no, request_id)
                discarded += [request_id for request_id, _ in moved]
                lease.heartbeat()
    except Exception as exc:
        st.rollback_open_transaction()
        checked.warnings.append(
            f"{sid}: the outbox of its final run could not be discarded ({type(exc).__name__})"
        )
    if discarded:
        checked.changes.append(
            Change(sid, run_no, "outbox_discarded", detail=f"{len(discarded)} request(s){left}")
        )


def _log_discarded(log: logs.EventLog, sid: str, run_no: int, request_id: str) -> None:
    try:
        log.write(
            "outbox_discarded",
            level="info",
            csession_id=sid,
            run=run_no,
            message=f"request {request_id}: the run is final",
        )
    except (OSError, ValueError):
        pass


# Delivery


def _deliver(
    st: store.Store,
    cfg: Config,
    sid: str,
    state_root: str,
    gateway: Gateway,
    environ: Mapping[str, str],
    checked: Checked,
    *,
    dry_run: bool,
    lease: _Lease,
) -> None:
    if dry_run:
        count = _deliverable(st, sid)
        if count:
            checked.changes.append(
                Change(sid, None, "events_delivered", detail=f"{count} event(s) undelivered; {DRY}")
            )
        return
    done = toolhooks.deliver_best_effort(
        st,
        cfg,
        sid,
        gateway=gateway,
        environ=environ,
        state_root=state_root,
        patience=0.0,
        heartbeat=lease.heartbeat,
    )
    if done is not None and done.delivered:
        checked.changes.append(
            Change(sid, None, "events_delivered", detail=f"{len(done.delivered)} event(s)")
        )


def _deliverable(st: store.Store, sid: str) -> int:
    """The undelivered events (of the first batch) a delivery would deliver now: those
    before the first event of a run whose wrapper is live (that ``exec`` delivers it)."""
    count = 0
    for event in st.undelivered_events(sid, toolhooks.BATCH):
        record = st.delivery_record(sid, int(event["run_no"]))
        if record is not None and toolhooks.other_live_wrapper(st, record, None):
            break
        count += 1
    return count


# Private temporary directories


def remove_tmpdir(st: store.Store, sid: str, *, expected_run: int | None = None) -> bool:
    """Remove the session's private tmpdir when its current run is final (``expected_run``:
    and is that run) or the session was deleted, never one a later run made
    (``tmpdir.remove_checked``). Best effort: False when nothing was removed (a leftover
    stays aside for the next ``reconcile``)."""
    owner = st.tmpdir_owner(sid)
    if owner is None or not owner[1]:
        return False
    if expected_run is not None and owner[0] not in (expected_run, 0):
        return False
    try:
        return tmpdir.remove_checked(sid, lambda: st.tmpdir_owner(sid) == owner)
    except (OSError, ValueError):
        return False


def _tmpdirs(st: store.Store, checked: Checked, *, dry_run: bool, lease: _Lease) -> None:
    try:
        sessions, stale = tmpdir.session_entries()
    except OSError as exc:
        checked.warnings.append(f"the private temporary directories could not be listed: {exc}")
        return
    for sid in sessions:
        lease.heartbeat()
        owner = st.tmpdir_owner(sid)
        if owner is None or not owner[1]:
            continue
        run_no = owner[0] or None
        if dry_run:
            checked.changes.append(Change(sid, run_no, "tmpdir_removed", detail=DRY))
        elif remove_tmpdir(st, sid):
            checked.changes.append(Change(sid, run_no, "tmpdir_removed"))
    for name in stale:
        lease.heartbeat()
        sid = tmpdir.stale_session(name)
        assert sid is not None
        owner = st.tmpdir_owner(sid)  # read after the listing (see tmpdir.remove_checked)
        if owner is None or not owner[1]:
            continue
        if dry_run:
            checked.changes.append(Change(sid, owner[0] or None, "tmpdir_removed", detail=DRY))
            continue
        try:
            removed = tmpdir.remove_stale(name)
        except (OSError, ValueError) as exc:
            checked.warnings.append(f"{name} could not be removed: {exc}")
            continue
        if removed:
            checked.changes.append(
                Change(sid, owner[0] or None, "tmpdir_removed", detail=f"leftover {name}")
            )
