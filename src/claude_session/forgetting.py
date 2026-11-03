"""``forget``, single and bulk (interface.md, forget; design.md, Reconcile, salvage and
retention).

``forget`` hides sessions: later commands report them as unknown. It never deletes anything;
``reconcile`` deletes the rows and directories ``retention_days`` later (``reconcile``, the
retention step).

For each session:

1. the inline liveness check runs first (``liveness.check_session``, cause ``forget``), with
   the outcome files of its final runs backfilled and the events it finalized delivered
   (``toolhooks.deliver_best_effort``: the run has no live wrapper, so ``forget`` delivers);
2. a session with a run that is not final (a live one, or a pending resume or new-session
   record) is refused: ``live`` (exit 20) for ``forget <id>``, a ``skipped`` entry in bulk
   mode. ``forget`` has no cancel path: ``stop`` ends a run;
3. ``Store.forget_session`` then checks that again, in the transaction that sets
   ``forgotten_at``. Every run allocation checks ``forgotten_at`` in its own write
   transaction, so a run that starts between the liveness check and the update either
   refuses the forget (it is open) or is refused itself (the session is forgotten). The
   ``forget`` audit record (``result: ok``) is written inside that transaction, just before
   the update: when it cannot be written nothing is forgotten and the command exits 1 (a
   bulk run stops there; sessions already forgotten keep their records). When the update
   itself then fails, or anything (a signal included) interrupts it after the record was
   attempted, a best-effort ``result: error`` record follows and the error propagates.

``forget <id>`` records a refusal (``result: refused``) on both paths: refused after the
check, or refused by ``Store.forget_session`` because a run started meanwhile. When that
record cannot be written the command exits 1 (``InternalError``; interface.md, Audit and
log). Bulk mode audits only the sessions it forgets; ``skipped`` entries get no record.

Bulk mode selects the sessions once, before the liveness check: not forgotten, last updated
before ``--older-than`` (``updated_at``), of launch ``--kind`` and with every ``--label``.
Only those are checked. ``--state`` is then matched on the state after the check (in a dry
run, the state the check would leave), live sessions are skipped, and the update is a
compare-and-set on ``updated_at`` as read after the check (the session's own check does not
count as a change): a session that changed between that read and its update is left alone,
neither forgotten nor reported.

``--dry-run`` writes nothing (no audit record, no liveness change, no delivery) and reports
what would be forgotten; a session whose runs the liveness check would finalize counts as
not live.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from . import audit, envbuild, liveness, outcomefile, store, toolhooks
from .errors import InternalError, NotFoundError, RefusedError

if TYPE_CHECKING:
    from .config import Config
    from .gateway import Gateway

CAUSE = "forget"
AUDIT_NAME = "audit.jsonl"
LIVE = "live"


def delete_after(forgotten_at: str, retention_days: int) -> str:
    """When ``reconcile`` deletes a session forgotten at ``forgotten_at``."""
    return store.format_ts(store.parse_ts(forgotten_at) + timedelta(days=retention_days))


class _Forgetter:
    def __init__(
        self,
        st: store.Store,
        cfg: Config,
        *,
        gateway: Gateway,
        environ: Mapping[str, str],
        actor: str,
        decision_ids: Sequence[str],
        dry_run: bool,
    ) -> None:
        self.st = st
        self.cfg = cfg
        self.gateway = gateway
        self.environ = environ
        self.actor = actor
        self.decision_ids = list(decision_ids)
        self.dry_run = dry_run
        self.state_root = os.path.dirname(st.path)
        self.audit_path = os.path.join(self.state_root, AUDIT_NAME)
        self.secrets = envbuild.credential_values(environ, cfg)
        # A dry run's liveness changes per session (nothing was applied).
        self.dry_changes: dict[str, list[liveness.Change]] = {}

    # Liveness

    def check(self, sids: Sequence[str]) -> None:
        """The inline liveness check of each session (cause ``forget``)."""
        for sid in sids:
            if not self.st.has_open_run(sid):
                continue
            checked = liveness.check_session(
                self.st, self.cfg, sid, cause=CAUSE, dry_run=self.dry_run
            )
            if self.dry_run:
                self.dry_changes[sid] = list(checked.changes)
                continue
            try:  # best effort: an unwritten outcome file stays for the next check or reconcile
                outcomefile.backfill(self.st, sid)
            except Exception:
                self.st.rollback_open_transaction()
            if checked.finalized:
                toolhooks.deliver_best_effort(
                    self.st,
                    self.cfg,
                    sid,
                    gateway=self.gateway,
                    environ=self.environ,
                    state_root=self.state_root,
                )

    def state_after(self, sid: str) -> tuple[str, str] | None:
        """``(state, updated_at)`` of the session after the check (a dry run: the state the
        check would leave), or None when it is gone or forgotten."""
        row = self.st.conn.execute(
            "SELECT state, updated_at FROM sessions WHERE csession_id = ? AND forgotten_at IS NULL",
            (sid,),
        ).fetchone()
        if row is None:
            return None
        state = str(row[0])
        for change in self.dry_changes.get(sid, []):
            if change.to_state is not None:
                state = change.to_state
        return state, str(row[1])

    def live(self, sid: str) -> bool:
        if not self.st.has_open_run(sid):
            return False
        if not self.dry_run:
            return True
        # A dry run changed nothing: the sessions the check would finalize count as final
        # when every open run would be (one finalizing change per run).
        open_runs = {int(r["run_no"]) for r in self.st.open_runs(sid)}
        done = {c.run for c in self.dry_changes.get(sid, []) if c.outcome is not None}
        return not open_runs <= done

    # Audit

    def _record(self, sid: str, result: str) -> dict[str, Any]:
        row = self.st.conn.execute(
            "SELECT launch_kind, current_run FROM sessions WHERE csession_id = ?", (sid,)
        ).fetchone()
        inputs = None if row is None else {"kind": row[0], "run": int(row[1])}
        return audit.append(
            self.audit_path,
            actor=self.actor,
            target=sid,
            action="forget",
            result=result,
            decision_ids=self.decision_ids,
            inputs=inputs,
            secrets=self.secrets,
        )

    def _best_effort(self, sid: str, result: str) -> None:
        try:
            self._record(sid, result)
        except (OSError, ValueError):
            pass

    def _forgotten(self, sid: str) -> bool:
        """Whether ``sid`` is forgotten; False when that cannot be read."""
        try:
            return self.st.forgotten_since(sid) is not None
        except (sqlite3.Error, OSError):
            return False

    def refused(self, sid: str) -> RefusedError:
        """The ``live`` refusal of ``forget <id>``, its ``refused`` record written first
        (fails closed: ``InternalError`` when it cannot be; a dry run writes nothing)."""
        if not self.dry_run:
            try:
                self._record(sid, "refused")
            except (OSError, ValueError) as exc:
                raise InternalError(
                    f"the audit record of the refused forget of {sid} could not be written"
                    f" ({type(exc).__name__})"
                ) from exc
        return RefusedError(
            f"{sid} is live or has a pending run; end it with stop first", reason=LIVE
        )

    # Forgetting

    def forget_one(self, sid: str, updated_at: str | None = None) -> str | None:
        """Forget ``sid``: its ``forgotten_at``, or None when it is live, gone or changed
        (the caller decides which it reports). Raises ``InternalError`` when the audit record
        cannot be written."""
        if self.dry_run:
            return store.format_ts(self.st.clock())
        # Set before the ``ok`` record is attempted: an interruption inside the write may
        # come after the record reached the file, so the ``error`` record follows it then too.
        audited = False

        def before_commit(_forgotten_at: str) -> None:
            nonlocal audited
            audited = True
            try:
                self._record(sid, "ok")
            except (OSError, ValueError) as exc:
                raise _AuditFailed(type(exc).__name__) from exc

        try:
            outcome = self.st.forget_session(
                sid, updated_at=updated_at, before_commit=before_commit
            )
        except _AuditFailed as exc:
            raise InternalError(
                f"the audit record for {sid} could not be written ({exc}); it was not forgotten"
            ) from exc
        except BaseException:
            # The update failed or was interrupted (a signal) after the ``ok`` record. Before
            # the commit the transaction rolled back and nothing was forgotten: the ``error``
            # record says so. After the commit the session is forgotten and ``ok`` stands.
            if audited and not self._forgotten(sid):
                self._best_effort(sid, "error")
            raise
        if outcome != "forgotten":
            if audited:  # cannot happen: the record is written only when it goes ahead
                self._best_effort(sid, "error")
            return None
        return self.st.forgotten_since(sid)


class _AuditFailed(Exception):
    pass


def _entry(forgotten_at: str, sid: str, retention_days: int) -> dict[str, str]:
    return {
        "csession_id": sid,
        "forgotten_at": forgotten_at,
        "delete_after": delete_after(forgotten_at, retention_days),
    }


def forget_session(
    st: store.Store,
    cfg: Config,
    csession_id: str,
    *,
    gateway: Gateway,
    environ: Mapping[str, str],
    actor: str,
    decision_ids: Sequence[str] = (),
    dry_run: bool = False,
) -> dict[str, Any]:
    """``forget <id>`` (``csession_id`` already resolved): the ``forget --json`` object, or
    ``RefusedError`` (``live``, exit 20) when a run of the session is not final."""
    worker = _Forgetter(
        st,
        cfg,
        gateway=gateway,
        environ=environ,
        actor=actor,
        decision_ids=decision_ids,
        dry_run=dry_run,
    )
    worker.check([csession_id])
    if worker.live(csession_id):
        raise worker.refused(csession_id)
    forgotten_at = worker.forget_one(csession_id)
    if forgotten_at is None:
        # Gone, forgotten or started a run meanwhile: the same answer the caller would get
        # asking again.
        if st.forgotten_since(csession_id) is not None:
            raise NotFoundError(f"unknown session: {csession_id}")
        raise worker.refused(csession_id)
    return {
        "dry_run": dry_run,
        "forgotten": [_entry(forgotten_at, csession_id, cfg.retention_days)],
        "skipped": [],
    }


def forget_older_than(
    st: store.Store,
    cfg: Config,
    seconds: float,
    *,
    gateway: Gateway,
    environ: Mapping[str, str],
    actor: str,
    decision_ids: Sequence[str] = (),
    states: Sequence[str] = (),
    kind: str | None = None,
    labels: Sequence[tuple[str, str]] = (),
    dry_run: bool = False,
) -> dict[str, Any]:
    """``forget --older-than`` (interface.md, forget): every session last updated more than
    ``seconds`` ago that matches the filters is forgotten, live ones (pending records
    included) are skipped; the ``forget --json`` object."""
    worker = _Forgetter(
        st,
        cfg,
        gateway=gateway,
        environ=environ,
        actor=actor,
        decision_ids=decision_ids,
        dry_run=dry_run,
    )
    threshold = st.clock() - timedelta(seconds=seconds)
    selected = [sid for sid, _ in st.forget_candidates(threshold, kind=kind, labels=labels)]
    worker.check(selected)
    forgotten: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for sid in selected:
        after = worker.state_after(sid)
        if after is None or (states and after[0] not in states):
            continue
        if worker.live(sid):
            skipped.append({"csession_id": sid, "reason": LIVE})
            continue
        forgotten_at = worker.forget_one(sid, after[1])
        if forgotten_at is not None:
            forgotten.append(_entry(forgotten_at, sid, cfg.retention_days))
        elif st.has_open_run(sid):  # a run started since the check
            skipped.append({"csession_id": sid, "reason": LIVE})
    return {"dry_run": dry_run, "forgotten": forgotten, "skipped": skipped}
