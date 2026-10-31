"""``stop`` (interface.md, stop and Signals; design.md, Supervision: Stop).

``stop <id>`` ends a session's current run deliberately:

1. The inline liveness check (``liveness.check_session``, cause ``liveness``) runs first and
   what it changed is delivered. A run it finalized, or one already final, is left alone
   (``already_final``). Otherwise the ``stop`` audit record is written (fail closed: a record
   that cannot be written means nothing is done and ``stop`` exits 1), then
   ``stop_requested_at`` is recorded (``Store.request_stop``), which ``exec`` reads in both
   phases.
2. **Pending run** (no wrapper): the cancel compare-and-set (``Store.cancel_pending``, the
   condition ``Store.adopt`` checks, so one of them wins) ends it ``not-started``,
   ``canceled``, with no signal; the launch spec (it may hold credentials) is deleted, the
   outcome file written and the final event delivered (``canceled: true``). When adoption won,
   the signal path follows.
3. **Signal path**: SIGTERM to the verified wrapper (it forwards to ``claude`` as ``stopped``,
   or, in its launch window, ends the run ``canceled``). A wrapper that cannot be verified, or
   that is gone with a live child, gets nothing; the verified child gets the SIGTERM instead,
   with ``pending_cause`` ``stopped`` recorded first (as ``exec`` records its cause before it
   signals). Signals are remembered per process (PID and start token): a wrapper or child
   recorded after a signal (a hand-over) gets its own, and a send that did not happen is
   tried again on the next poll. After ``--grace`` (default ``kill_grace``): ``pending_cause``
   ``stopped`` (unless the run has a cause) and SIGKILL to the verified child (also to an
   orphaned child found later); the wrapper is always given ``kill_grace`` more to reap it and
   finalize, also when no child SIGKILL was sent. Then the run is re-read: a child recorded
   meanwhile gets its SIGKILL first, and the wrapper is SIGKILLed (``pending_cause``
   ``stopped`` first when a child is recorded), except while a recorded child's PID exists but
   cannot be verified: that wrapper is left to supervise it (``signal_not_sent``), the run
   stays live. A cause this ``stop`` recorded is cleared again when its signal was not sent.
   Whenever the wrapper is found gone, ``stop`` finalizes the run itself through the liveness
   path (cause ``stop``, ``recorded_by_stop``) and delivers its final event. A run ``start``
   released meanwhile (pending again) is canceled. A run found final before this ``stop``
   sent anything is ``already_final`` (it lost the race).
4. ``stop`` waits until the run is final, up to grace + ``kill_grace`` + 5 s from the first
   signal; a run still live then is ``ended: false`` (exit 12).

**Exact PIDs only.** A wrapper or child is signaled only when its recorded PID and start-time
token (procinfo) match the running process: on Linux through a pidfd opened before the token is
checked, so the signal can never reach a reused PID (``gateway.pidfd_open``: the os function,
else the syscall through ``ctypes`` on x86_64 and aarch64). Without a pidfd (macOS, another
machine, a Linux kernel before 5.3) the token is checked right before ``kill``, which leaves a
PID-reuse window as long as that check.
An empty token (not readable when it was recorded) is never a match: such a process is not
signaled (``signal_not_sent`` is logged). Every signal sent is logged ``signal_sent`` with its
PID. Nothing is ever signaled by name or pattern.

``--dry-run`` writes nothing (no audit record, no request, no signal): it prints the first
signal that would be sent, or the cancel or the liveness end that would happen. It always
exits 0 (no wait happens; ``ended`` reflects the current state or the predicted end).
"""

from __future__ import annotations

import errno
import os
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import audit, endmap, lifecycle, liveness, logs, outcomefile, store, toolhooks
from . import gateway as gw
from .errors import InternalError

if TYPE_CHECKING:
    from .config import Config
    from .gateway import Gateway

LOG_NAME = "log.jsonl"
AUDIT_NAME = "audit.jsonl"
# The wait after the last signal's grace (interface.md, stop, step 5).
SETTLE = 5.0
# The poll of the run while ``stop`` waits (at most; ``poll_interval`` when shorter).
POLL = 0.1
STOPPED = "stopped"
LINUX = sys.platform.startswith("linux")
# ``_pidfd``: no pidfd on this platform (never a valid descriptor).
NO_PIDFD = -1


@dataclass
class Stopped:
    """The ``stop --json`` object (schemas/cli/stop.json) and the exit code."""

    csession_id: str
    run: int
    dry_run: bool
    already_final: bool = False
    canceled: bool = False
    recorded_by_stop: bool = False
    signals: list[dict[str, Any]] = field(default_factory=list)
    record: dict[str, Any] = field(default_factory=dict)
    # A dry run's predicted end (outcome, end_reason) when it would end the run.
    predicted: tuple[str, str, str] | None = None

    def as_json(self) -> dict[str, Any]:
        rec = self.record
        if self.predicted is not None:
            outcome, end_reason, state = self.predicted
            ended, exit_code = True, endmap.EXIT_CODES[outcome]
        elif rec["ended_at"] is not None:
            outcome, end_reason, exit_code = rec["outcome"], rec["end_reason"], rec["exit_code"]
            ended = True
            reported = rec["report_status"] is not None
            state = "exited" if reported else endmap.final_state(outcome)
        else:
            ended, outcome, end_reason, exit_code = False, None, None, None
            state = rec["session_state"]
        return {
            "csession_id": self.csession_id,
            "run": self.run,
            "already_final": self.already_final,
            "canceled": self.canceled,
            "dry_run": self.dry_run,
            "ended": ended,
            "run_exit_code": exit_code,
            "signals": list(self.signals),
            "state": state,
            "outcome": outcome,
            "end_reason": end_reason,
            "recorded_by_stop": self.recorded_by_stop,
        }

    @property
    def exit_code(self) -> int:
        """0 when the run ended (also already final or canceled) and for a dry run; 12 when
        it is still live after the wait."""
        if self.dry_run or self.as_json()["ended"]:
            return 0
        return 12


# Exact signals


def verified(pid: Any, token: Any) -> bool:
    """Whether ``pid`` is running (not a zombie) with the start-time token ``token``. An empty
    or missing token, an invalid PID or a platform that cannot answer is never a match."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid < 2**31:
        return False
    if not isinstance(token, str) or token == "":
        return False
    try:
        return bool(store.system_procinfo().is_alive(pid, token))
    except Exception:
        return False


def _pidfd(pid: int) -> int | None:
    """A pidfd for ``pid`` (``gateway.pidfd_open``: the os function, else the syscall through
    ctypes on x86_64 and aarch64); None when the process is gone; ``NO_PIDFD`` when this
    platform or kernel has none (another machine, a kernel before 5.3)."""
    try:
        return gw.pidfd_open(pid)
    except OSError as exc:
        return NO_PIDFD if exc.errno == errno.ENOSYS else None


def send_verified(pid: Any, token: Any, sig: int) -> bool:
    """Send ``sig`` to the exact process ``pid`` with start-time token ``token``. On Linux the
    pidfd is opened before the token is checked, so it names the verified process and never
    a reused PID. Returns whether the signal was sent."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid < 2**31:
        return False
    fd = _pidfd(pid) if LINUX else NO_PIDFD
    if fd is None:
        return False  # the process is gone
    if fd != NO_PIDFD:
        try:
            if not verified(pid, token):
                return False
            gw.pidfd_send_signal(fd, sig)
        except OSError:
            return False
        finally:
            os.close(fd)
        return True
    if not verified(pid, token):  # no pidfd: checked right before the kill
        return False
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True


# The command


class Stop:
    """One ``stop`` invocation (see the module docs)."""

    def __init__(
        self,
        st: store.Store,
        cfg: Config,
        *,
        grace: float | None,
        actor: str,
        decision_ids: Sequence[str],
        gateway: Gateway,
        environ: Mapping[str, str],
    ) -> None:
        self.st = st
        self.cfg = cfg
        self.grace = cfg.kill_grace if grace is None else grace
        self.actor = actor
        self.decision_ids = list(decision_ids)
        self.gateway = gateway
        self.environ = environ
        self.state_root = os.path.dirname(st.path)
        self.log = logs.EventLog(os.path.join(self.state_root, LOG_NAME))
        self.poll = min(float(cfg.poll_interval), POLL)
        # (role, signal, pid, start token) of each signal sent, and of each not-sent logged.
        self._sent: set[tuple[Any, ...]] = set()
        self._noted: set[tuple[Any, ...]] = set()

    def run(self, given: str, *, dry_run: bool = False) -> Stopped:
        sid, _ = lifecycle.lookup(self.st, given)
        checked = liveness.check_session(self.st, self.cfg, sid, dry_run=dry_run)
        if not dry_run and checked.finalized:
            self._deliver(sid)
        record = self.st.current_run(sid)
        if record is None:
            raise InternalError(f"session {sid} has no current run")
        run = int(record["run_no"])
        if dry_run:
            return self._predict(sid, run, record, checked)
        if record["ended_at"] is not None:
            return Stopped(sid, run, False, already_final=True, record=record)
        self._audit(sid, run, "ok")
        try:
            return self._stop(sid, run)
        except Exception:
            try:
                self._audit(sid, run, "error")
            except Exception:  # best effort: the failed action's error stands
                pass
            raise

    def _stop(self, sid: str, run: int) -> Stopped:
        result = Stopped(sid, run, False)
        if not self.st.request_stop(sid, run):
            result.already_final = True
            result.record = self.st.run_record(sid, run)
            return result
        record = self.st.run_record(sid, run)
        if record["wrapper_pid"] is None and self._cancel(sid, run):
            result.canceled = True
        else:
            self._signal_path(result)
        result.record = self.st.run_record(sid, run)
        return result

    # Pending runs

    def _cancel(self, sid: str, run: int) -> bool:
        """The cancel compare-and-set; when it wins, the launch spec is deleted, the outcome
        file written and the final event delivered."""
        if not self.st.cancel_pending(sid, run, store.CANCELED):
            return False
        lifecycle.discard_spec_at(self.state_root, sid, run)
        outcomefile.write_run(self.st, sid, run)
        self._deliver(sid)
        return True

    # Signals

    def _signal_path(self, result: Stopped) -> None:
        """Poll the run and signal it until it is final or the wait is over (module docs,
        step 3). Signals are remembered per (role, signal, PID, start token): a wrapper or
        child recorded later (a hand-over) gets its own, and a send that did not happen is
        tried again on the next poll."""
        sid, run = result.csession_id, result.run
        start = time.monotonic()
        kill_at = start + self.grace
        give_up = kill_at + float(self.cfg.kill_grace) + SETTLE
        wrapper_kill_at: float | None = None
        first = True
        while True:
            record = self.st.run_record(sid, run)
            if record["ended_at"] is not None:
                # Ended by another process before this stop did anything: it lost the race.
                result.already_final = first and not result.signals
                return
            first = False
            now = time.monotonic()
            kill_phase = now >= kill_at
            if kill_phase and wrapper_kill_at is None:
                # Grace is over: SIGKILL to the child; the wrapper always gets kill_grace to
                # reap it and finalize (also with no child, or one already gone).
                self._signal_child(result, record, signal.SIGKILL)
                wrapper_kill_at = now + float(self.cfg.kill_grace)
            verdict = liveness.classify(self.st, record)
            if verdict == liveness.PENDING:
                # ``start`` released the run (it was not pending when stop looked).
                if self._cancel(sid, run):
                    result.canceled = not result.signals
                    result.recorded_by_stop = bool(result.signals)
                    return
            elif verdict == liveness.LIVE:
                if wrapper_kill_at is not None and now >= wrapper_kill_at:
                    self._kill_wrapper(result, sid, run)
                elif not self._signal(result, record, "wrapper", signal.SIGTERM):
                    # The wrapper cannot be verified: the verified child gets the SIGTERM
                    # instead (pending_cause first), as an orphan would.
                    if not kill_phase:
                        self._signal_child(result, record, signal.SIGTERM)
            elif verdict == liveness.ORPHAN:
                self._signal_child(result, record, signal.SIGKILL if kill_phase else signal.SIGTERM)
            else:  # the wrapper is gone: stop records the end itself (liveness path)
                if self._finalize(sid, run, max(0.0, give_up - time.monotonic())):
                    result.recorded_by_stop = True
                    return
            if time.monotonic() >= give_up:
                return
            time.sleep(self.poll)

    def _kill_wrapper(self, result: Stopped, sid: str, run: int) -> None:
        """kill_grace is over: re-read the run; a child recorded meanwhile gets its SIGKILL
        first, then the wrapper (``pending_cause`` ``stopped`` recorded first when a child is
        recorded). A recorded child whose PID exists but cannot be verified keeps its wrapper:
        without it nobody could stop that child, so the run stays live."""
        record = self.st.run_record(sid, run)
        if record["ended_at"] is not None:
            return
        child_pid, child_start = record["child_pid"], record["child_start"]
        if child_pid is None:
            self._signal(result, record, "wrapper", signal.SIGKILL)
            return
        if verified(child_pid, child_start):
            self._signal_child(result, record, signal.SIGKILL)
        elif liveness.process_alive(self.st, child_pid, child_start):
            self._note(
                result,
                ("wrapper", signal.SIGKILL, record["wrapper_pid"], record["wrapper_start"]),
                "the recorded child runs but cannot be verified; the wrapper is left to"
                " supervise it",
            )
            return
        key = ("wrapper", signal.SIGKILL, record["wrapper_pid"], record["wrapper_start"])
        if key in self._sent:
            return
        recorded = self.st.set_pending_cause(sid, run, STOPPED)
        if not self._signal(result, record, "wrapper", signal.SIGKILL) and recorded:
            self.st.clear_pending_cause(sid, run, STOPPED)

    def _signal_child(self, result: Stopped, record: dict[str, Any], sig: int) -> bool:
        """``pending_cause`` ``stopped`` (an earlier cause stands), then ``sig`` to the
        verified child; the cause this call recorded is cleared again when the signal was
        not sent. Returns whether it was sent (now or earlier)."""
        pid, token = record["child_pid"], record["child_start"]
        if pid is None:
            return False
        key = ("child", sig, pid, token)
        if key in self._sent:
            return True
        if not verified(pid, token):
            self._note(result, key, "the child is gone or cannot be verified")
            return False
        sid, run = result.csession_id, result.run
        recorded = self.st.set_pending_cause(sid, run, STOPPED)
        if self._signal(result, record, "child", sig):
            return True
        if recorded:
            self.st.clear_pending_cause(sid, run, STOPPED)
        return False

    def _signal(self, result: Stopped, record: dict[str, Any], role: str, sig: int) -> bool:
        """``sig`` to the ``role`` once per recorded (PID, start token): True when it was sent,
        now or earlier. A send that did not happen is logged once and tried again later."""
        key = (role, sig, record[f"{role}_pid"], record[f"{role}_start"])
        if key in self._sent:
            return True
        if not self._send(result, record, role, sig):
            self._note(result, key, f"the {role} is gone or cannot be verified")
            return False
        self._sent.add(key)
        return True

    def _send(self, result: Stopped, record: dict[str, Any], role: str, sig: int) -> bool:
        """One send to the exact recorded process; logged ``signal_sent`` when it happened."""
        pid, token = record[f"{role}_pid"], record[f"{role}_start"]
        if not send_verified(pid, token, sig):
            return False
        name = signal.Signals(sig).name
        result.signals.append({"pid": pid, "role": role, "signal": name})
        self._log(
            "signal_sent", result, pid=pid, signal=name, message=f"stop: {role}", level="info"
        )
        return True

    def _note(self, result: Stopped, key: tuple[Any, ...], why: str) -> None:
        """``signal_not_sent`` for ``key`` (role, signal, PID, token), once per key."""
        if key in self._noted:
            return
        self._noted.add(key)
        self._not_sent(result, key[0], key[1], why)

    def _not_sent(self, result: Stopped, role: str, sig: int, why: str) -> None:
        self._log(
            "signal_not_sent",
            result,
            signal=signal.Signals(sig).name,
            message=f"stop: {role} not signaled: {why}",
            level="warn",
        )

    def _finalize(self, sid: str, run: int, patience: float) -> bool:
        """The liveness path with cause ``stop`` (wrapper gone); whether it finalized the
        run. Its changes are delivered."""
        checked = liveness.check_session(self.st, self.cfg, sid, cause="stop", patience=patience)
        if checked.finalized:
            self._deliver(sid)
        won = any(c.run == run and c.outcome is not None for c in checked.changes)
        if not won:
            time.sleep(self.poll)  # another process salvages it, or the check failed
        return won

    # Dry run

    def _predict(
        self, sid: str, run: int, record: dict[str, Any], checked: liveness.Checked
    ) -> Stopped:
        result = Stopped(sid, run, True, record=record)
        if record["ended_at"] is not None:
            result.already_final = True
            return result
        for change in checked.changes:
            if change.run == run and change.outcome is not None:
                assert change.end_reason is not None and change.to_state is not None
                result.already_final = True
                result.predicted = (change.outcome, change.end_reason, change.to_state)
                return result
        verdict = liveness.classify(self.st, record)
        if verdict == liveness.PENDING:
            result.canceled = True
            result.predicted = ("not-started", "canceled", "not-started")
        elif verdict == liveness.LIVE:
            self._predict_signal(result, record, "wrapper")
        elif verdict == liveness.ORPHAN:
            self._predict_signal(result, record, "child")
        return result

    @staticmethod
    def _predict_signal(result: Stopped, record: dict[str, Any], role: str) -> None:
        pid = record[f"{role}_pid"]
        if verified(pid, record[f"{role}_start"]):
            result.signals.append({"pid": pid, "role": role, "signal": "SIGTERM"})

    # Audit, log, delivery

    def _audit(self, sid: str, run: int, result: str) -> None:
        """The ``stop`` audit record; raises when it cannot be written (fail closed:
        interface.md, Audit and log)."""
        secrets = logs.secret_values(
            self.environ,
            secret_env=self.cfg.secret_env,
            claude_auth_env=self.cfg.claude_auth_env,
        )
        try:
            audit.append(
                os.path.join(self.state_root, AUDIT_NAME),
                actor=self.actor,
                target=sid,
                action="stop",
                result=result,
                decision_ids=self.decision_ids,
                inputs={"run": run, "grace": self.grace},
                secrets=secrets,
            )
        except (OSError, ValueError) as exc:
            raise InternalError(
                f"the stop audit record could not be written ({type(exc).__name__});"
                " nothing was done",
                detail=f"{AUDIT_NAME}: {type(exc).__name__}",
            ) from exc

    def _log(self, event: str, result: Stopped, **fields: Any) -> None:
        try:
            self.log.write(event, csession_id=result.csession_id, run=result.run, **fields)
        except (OSError, ValueError):
            pass

    def _deliver(self, sid: str) -> None:
        """``post-state-change`` delivery after a change to a run with no live wrapper."""
        toolhooks.deliver_best_effort(
            self.st,
            self.cfg,
            sid,
            gateway=self.gateway,
            environ=self.environ,
            state_root=self.state_root,
        )
