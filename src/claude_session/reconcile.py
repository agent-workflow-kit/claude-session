"""``reconcile`` (interface.md, reconcile; design.md, Reconcile, salvage and retention).

Repairs state against reality, safe to run from cron next to everything else: one
``reconcile`` lease (a second caller reports ``already_running`` and changes nothing), renewed
between the steps and sessions (the lease is lost: it stops), and no long store lock (each
step is its own short transaction; the outbox discard holds the write lock only for the
renames of one session's outbox, so no run of that session can be allocated between the
check that its current run is final and the discard). In order:

1. the ``post-state-change`` hook process groups that dead deliverers left running are ended
   (``toolhooks.end_orphaned_groups``: verified leaders only);
2. the liveness check of every session with a run that is not final (``liveness``, cause
   ``reconcile``): pending runs past their deadline, wrapper-lost runs finalized and salvaged,
   orphans (wrapper dead, child alive) listed and never signaled;
3. the outcome files of final runs never written (``outcomefile.backfill``);
4. outbox requests waiting for a session whose current run is final: discarded under that run
   (``discarded``) and logged (``outbox_discarded``); a final run is never ingested into;
5. undelivered events delivered (``toolhooks.deliver``), the final events of runs that
   ``status``, ``list`` or a liveness check finalized included;
6. private temporary directories of sessions whose current run is final, or that were
   deleted, removed (``tmpdir.remove_checked``), then the ``.stale-*`` entries of such
   sessions (``tmpdir.remove_stale``). Ids this state directory does not know are never
   touched: another state directory may share the per-uid root.

``--dry-run`` takes no lease and writes nothing: it reports the changes the liveness check,
the outbox discard, the delivery and the tmpdir removal would make (``detail``: ``dry run``).

TODO(#34): forgotten sessions older than ``retention_days`` are deleted here (rows, both
directories and the tmpdir, fd-based; the id kept in ``deleted_ids``), action ``deleted``.
TODO(#40): add ``upgrades.hook_interpreter_warning(run.hook_interpreter)`` to ``warnings``
for every live run (on the #77 line).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from . import liveness, logs, outbox, outcomefile, sessiondir, store, tmpdir, toolhooks
from .liveness import Change, Checked

if TYPE_CHECKING:
    from .config import Config
    from .gateway import Gateway

LEASE = "reconcile"
LOG_NAME = "log.jsonl"
DRY = "dry run: not applied"


class LeaseLost(Exception):
    """The ``reconcile`` lease was taken over while reconcile worked."""


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
        _pass(st, cfg, checked, state_root, gateway, environ, dry_run=True, renew=lambda: None)
    else:
        if not st.take_lease(LEASE):
            result["already_running"] = True
            return result

        def renew() -> None:
            if not st.renew_lease(LEASE):
                raise LeaseLost

        try:
            _pass(st, cfg, checked, state_root, gateway, environ, dry_run=False, renew=renew)
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
    renew: Callable[[], None],
) -> None:
    if not dry_run:
        toolhooks.end_orphaned_groups(
            st, None, kill_grace=cfg.kill_grace, log=logs.EventLog(_log_path(state_root))
        )
        renew()
    for sid in st.sessions_with_open_runs():
        checked.extend(liveness.check_session(st, cfg, sid, cause="reconcile", dry_run=dry_run))
        renew()
    if not dry_run:
        checked.warnings += outcomefile.backfill(st)
        renew()
    for sid in st.session_ids():
        _discard_outbox(st, sid, state_root, checked, dry_run=dry_run)
    renew()
    for sid in st.sessions_with_undelivered():
        _deliver(st, cfg, sid, state_root, gateway, environ, checked, dry_run=dry_run)
        renew()
    _tmpdirs(st, checked, dry_run=dry_run)


def _log_path(state_root: str) -> str:
    return os.path.join(state_root, LOG_NAME)


# Outbox requests of final runs


def _waiting_requests(state_root: str, sid: str) -> list[str]:
    path = os.path.join(sessiondir.session_dir(state_root, sid), outbox.OUTBOX)
    try:
        return [name for name in os.listdir(path) if outbox.is_request_name(name)]
    except OSError:
        return []


def _discard_outbox(
    st: store.Store, sid: str, state_root: str, checked: Checked, *, dry_run: bool
) -> None:
    """Requests waiting in the outbox of a session whose current run is final are discarded
    under that run (interface.md, reconcile). The check and the renames share one write
    transaction: no run of the session is allocated in between."""
    if not _waiting_requests(state_root, sid):
        return
    owner = st.tmpdir_owner(sid)
    if owner is None or not owner[1]:
        return  # a live (or pending) run: its requests are its own
    run_no = owner[0]
    if dry_run:
        count = len(_waiting_requests(state_root, sid))
        checked.changes.append(
            Change(sid, run_no, "outbox_discarded", detail=f"{count} request(s); {DRY}")
        )
        return
    try:
        with st.transaction() as conn:
            if st.tmpdir_owner(sid) != (run_no, True):
                return
            with sessiondir.SessionDirs.open(state_root, sid) as dirs:
                discarded = dirs.discard_outbox(run_no)
            st.record_discarded(conn, sid, run_no, discarded)
    except Exception as exc:
        st.rollback_open_transaction()
        checked.warnings.append(
            f"{sid}: the outbox of its final run could not be discarded ({type(exc).__name__})"
        )
        return
    if not discarded:
        return
    log = logs.EventLog(_log_path(state_root))
    for request_id in discarded:
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
    checked.changes.append(
        Change(sid, run_no, "outbox_discarded", detail=f"{len(discarded)} request(s)")
    )


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
) -> None:
    if dry_run:
        count = len(st.undelivered_events(sid, toolhooks.BATCH))
        checked.changes.append(
            Change(sid, None, "events_delivered", detail=f"{count} event(s) undelivered; {DRY}")
        )
        return
    done = toolhooks.deliver_best_effort(
        st, cfg, sid, gateway=gateway, environ=environ, state_root=state_root, patience=0.0
    )
    if done is not None and done.delivered:
        checked.changes.append(
            Change(sid, None, "events_delivered", detail=f"{len(done.delivered)} event(s)")
        )


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


def _tmpdirs(st: store.Store, checked: Checked, *, dry_run: bool) -> None:
    try:
        sessions, stale = tmpdir.session_entries()
    except OSError as exc:
        checked.warnings.append(f"the private temporary directories could not be listed: {exc}")
        return
    for sid in sessions:
        owner = st.tmpdir_owner(sid)
        if owner is None or not owner[1]:
            continue
        run_no = owner[0] or None
        if dry_run:
            checked.changes.append(Change(sid, run_no, "tmpdir_removed", detail=DRY))
        elif remove_tmpdir(st, sid):
            checked.changes.append(Change(sid, run_no, "tmpdir_removed"))
    for name in stale:
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
