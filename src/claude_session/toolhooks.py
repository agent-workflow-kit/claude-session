"""Lifecycle hooks: ``post-state-change`` selection, execution and delivery (interface.md,
Hooks; design.md, Lifecycle hooks and Data model).

**Entries.** The configured entries of the *current* config (``hooks.post-state-change``) plus
the per-invocation ``--hook post-state-change=<cmd>`` entries stored with the event's run
(``runs.invocation_json``; timeout from its ``--hook-timeout``, default 30 s; no selectors).

**Selectors** (``selected``), combined with AND: ``kinds`` (the run kind is one of),
``exclude_kinds`` (none of), ``labels`` (every ``key=value`` pair, parsed with
``ids.parse_label``, is among the labels stored with the event, i.e. at event time, also on
late delivery) and, for ``post-state-change``, ``states`` (the event's new state is one of).
An empty list selects literally (``kinds: []`` never matches); a selector or stored label
that does not parse never matches (the conservative reading).

**Execution** (``run_entry``): ``sh -c <cmd>``, ``sh`` resolved to an absolute path on the
sanitized ``PATH``, through ``gateway.run(..., new_process_group=True, kill_grace=...)`` with
the payload on stdin and the state root as cwd: on timeout the hook's process group gets
SIGTERM, then SIGKILL after ``kill_grace`` (the only group this tool signals). The environment
(``hook_env``) is claude-session's minus every ``CSESSION_*`` and ``secret_env`` variable, with
the sanitized ``PATH`` and ``CSESSION_HOOK``, ``CSESSION_ID``, ``CSESSION_OBJECT_TYPE=session``.
Hook output is untrusted and unused: it is drained and discarded (``output_limit=0``), never
logged. A failing or timed-out hook is logged (``hook_failed``/``hook_timeout``, warn) and
never fatal: ``post-state-change`` cannot veto.

**Delivery** (``Deliverer``): at least once, in event-id order per session. A deliverer takes
the session's lease ``delivery:<id>`` (the store's lease: holder PID + start time + token,
60 s expiry, a dead holder's lease taken over at once), runs the selected entries of each
undelivered event in order, renewing the lease every ``LEASE_RENEW_INTERVAL`` while a hook
runs, and marks the event delivered (``events.delivered_at``) in the transaction that renews
the lease, only while it still holds it. A crash between a hook and the mark leaves the event
undelivered: the next deliverer runs it again, so receivers deduplicate on ``event_id``. An
event whose run has a live wrapper other than this process stops the pass (that ``exec``
delivers it; later events wait behind it). Entries with ``async: true`` do not hold up later
entries or events: each runs on a thread of its own (its timeout and group kill still apply)
and the event is marked once it was started; the deliverer waits for them before it returns
(``deliver``) or stops (``Worker.close``), so no hook is left unsupervised.

``exec`` delivers on a background thread (``Worker``, its own store connection) woken by the
poll loop, so a slow hook delays later hooks, never supervision, and never the hook-event fast
path, which only appends events. Commands that change a run with no live wrapper deliver
synchronously with ``deliver``.

TODO(#31): ``pre-launch`` reuses ``Entry``, ``selected`` (without ``states``), ``hook_env``
and the ``sh -c`` execution, with its own outcome rules.
TODO(#20, #27): ``dryrun.hooks_json`` (feat/dry-run) has its own selector match; once both
branches are merged it must call ``selected`` (without ``state``) so plan and run agree.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import durations, ids, logs, pathrules, store
from .endmap import REPORT_OUTCOMES
from .gateway import DEFAULT_PATH, Gateway, GatewayTimeout

if TYPE_CHECKING:
    from .config import Config

POST_STATE_CHANGE = "post-state-change"
OBJECT_TYPE = "session"
TOOL = "claude-session"
DEFAULT_TIMEOUT = 30.0
LOG_NAME = "log.jsonl"
# Events read per query of a delivery pass.
BATCH = 100
LEASE_RENEW_INTERVAL = store.LEASE_RENEW_INTERVAL.total_seconds()


def lease_name(csession_id: str) -> str:
    return f"{store.DELIVERY_LEASE_PREFIX}{csession_id}"


# Entries and selectors


@dataclass(frozen=True)
class Entry:
    """One hook entry. ``origin`` is ``config`` or ``invocation``; ``index`` its 1-based
    position there (how logs name it: never by its command)."""

    cmd: str
    timeout: float
    origin: str
    index: int
    async_: bool = False
    kinds: tuple[str, ...] | None = None
    exclude_kinds: tuple[str, ...] | None = None
    labels: tuple[str, ...] | None = None
    states: tuple[str, ...] | None = None

    @property
    def name(self) -> str:
        return f"{self.origin} entry {self.index}"


def _tuple(values: Iterable[str] | None) -> tuple[str, ...] | None:
    return None if values is None else tuple(values)


def config_entries(cfg: Config) -> list[Entry]:
    """The ``hooks.post-state-change`` entries of ``cfg``, in order."""
    return [
        Entry(
            cmd=hook.cmd,
            timeout=hook.timeout,
            origin="config",
            index=n,
            async_=hook.async_,
            kinds=_tuple(hook.kinds),
            exclude_kinds=_tuple(hook.exclude_kinds),
            labels=_tuple(hook.labels),
            states=_tuple(hook.states),
        )
        for n, hook in enumerate(cfg.hooks.post_state_change, start=1)
    ]


def invocation_entries(invocation_json: str | None, hook: str = POST_STATE_CHANGE) -> list[Entry]:
    """The per-invocation ``--hook <hook>=<cmd>`` entries stored in ``invocation_json``, in
    order; ``--hook-timeout <hook>=<duration>`` (the last one given) sets their timeout. Stored
    data that does not have the expected shape gives no entries and a timeout that does not
    parse the default (both are written by this tool; cli checked them)."""
    try:
        invocation = json.loads(invocation_json or "{}")
    except ValueError:
        return []
    if not isinstance(invocation, dict):
        return []
    timeout = DEFAULT_TIMEOUT
    for item in _strings(invocation.get("hook_timeouts")):
        name, sep, value = item.partition("=")
        if sep and name == hook:
            try:
                timeout = durations.parse(value, positive=True)
            except ValueError:
                timeout = DEFAULT_TIMEOUT
    entries: list[Entry] = []
    for item in _strings(invocation.get("hooks")):
        name, sep, cmd = item.partition("=")
        if sep and name == hook:
            entries.append(
                Entry(cmd=cmd, timeout=timeout, origin="invocation", index=len(entries) + 1)
            )
    return entries


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _label_pairs(labels: Iterable[str]) -> set[tuple[str, str]] | None:
    """The parsed pairs, or None when any does not parse."""
    pairs: set[tuple[str, str]] = set()
    for label in labels:
        try:
            pairs.add(ids.parse_label(label))
        except ValueError:
            return None
    return pairs


def stored_labels(labels_json: str | None) -> list[str]:
    """The labels stored with an event (a JSON list of ``key=value``); [] when unusable."""
    try:
        value = json.loads(labels_json or "[]")
    except ValueError:
        return []
    return _strings(value)


def selected(
    entry: Entry, *, run_kind: str, labels: Sequence[str], state: str | None = None
) -> bool:
    """Whether ``entry`` runs for a run of ``run_kind`` whose session had ``labels`` (and,
    when ``state`` is given, whose new state is ``state``): every selector it sets matches."""
    if entry.kinds is not None and run_kind not in entry.kinds:
        return False
    if entry.exclude_kinds is not None and run_kind in entry.exclude_kinds:
        return False
    if entry.labels is not None:
        wanted = _label_pairs(entry.labels)
        have = {pair for label in labels for pair in (_label_pairs([label]) or ())}
        if wanted is None or not wanted <= have:
            return False
    if state is not None and entry.states is not None and state not in entry.states:
        return False
    return True


# Environment and execution


def hook_env(
    environ: Mapping[str, str], *, secret_env: Iterable[str], hook: str, csession_id: str
) -> dict[str, str]:
    """A hook's environment (interface.md, Hooks: Execution)."""
    secret = set(secret_env)
    env = {
        name: value
        for name, value in environ.items()
        if name
        and "=" not in name
        and "\x00" not in name
        and "\x00" not in value
        and not name.startswith("CSESSION_")
        and name not in secret
    }
    env["PATH"] = ":".join(pathrules.sanitize_path(environ.get("PATH", ""))) or DEFAULT_PATH
    env["CSESSION_HOOK"] = hook
    env["CSESSION_ID"] = csession_id
    env["CSESSION_OBJECT_TYPE"] = OBJECT_TYPE
    return env


def resolve_sh(env: Mapping[str, str]) -> str | None:
    """The absolute path of ``sh`` on the hook environment's ``PATH``, or None."""
    try:
        tool = pathrules.resolve_tool("sh", env["PATH"].split(":"), lambda _path: False)
    except Exception:  # an unusable PATH entry: no shell
        return None
    return None if tool is None else tool.path


@dataclass(frozen=True)
class Ran:
    """How one entry ran: ``ok``, ``failed`` (non-zero exit, or could not start) or
    ``timeout``; ``detail`` is fixed text and numbers, never hook output."""

    result: str
    detail: str


def run_entry(
    gateway: Gateway,
    entry: Entry,
    *,
    sh: str | None,
    env: Mapping[str, str],
    cwd: str,
    payload: str,
    kill_grace: float,
) -> Ran:
    """Run ``entry`` once (see the module docs). Never raises for the hook's own failure."""
    if sh is None:
        return Ran("failed", "sh not found on the sanitized PATH")
    try:
        result = gateway.run(
            ["sh", "-c", entry.cmd],
            executable=sh,
            env=env,
            cwd=cwd,
            stdin=payload,
            timeout=entry.timeout,
            new_process_group=True,
            kill_grace=kill_grace,
            output_limit=0,
        )
    except GatewayTimeout:
        return Ran("timeout", f"timed out after {entry.timeout:g} s; process group ended")
    except OSError as exc:
        return Ran("failed", f"could not start sh ({type(exc).__name__})")
    if result.returncode != 0:
        return Ran("failed", f"exit {result.returncode}")
    return Ran("ok", "exit 0")


# Payload


def _report(record: Mapping[str, Any]) -> dict[str, Any] | None:
    if record["report_status"] is None:
        return None
    artifact = None
    if record["artifact_name"] is not None:
        artifact = {
            "name": record["artifact_name"],
            "path": record["artifact_path"],
            "sha256": record["artifact_sha256"],
            "size": record["artifact_size"],
        }
    return {
        "status": record["report_status"],
        "implicit": bool(record["report_implicit"]),
        "summary": record["report_summary"],
        "reason": record["report_reason"],
        "artifact": artifact,
        "reported_at": record["reported_at"],
    }


def _decision_ids(record: Mapping[str, Any]) -> list[str]:
    try:
        value = json.loads(record["decision_ids_json"] or "[]")
    except ValueError:
        return []
    return _strings(value)


def build_payload(
    event: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    csession_id: str,
    labels: Sequence[str],
    now: str,
) -> dict[str, Any]:
    """The ``post-state-change`` payload (schemas/hooks/post-state-change.json) of ``event``
    (an ``undelivered_events`` row) of the run ``record`` (``delivery_record``), delivered at
    ``now``. State, activity, reason, cause, labels and finality are the event's; the rest is
    the run as it is now (a late delivery shows its current report). ``session_end_reason``
    is the reason of the latest SessionEnd hook Claude Code sent for the run, verbatim (it can
    be ``prompt_input_exit`` while the run's ``end_reason`` is ``forwarded_hup``); it is not
    the run's end reason, which is ``end_reason``. The outcome fields are
    set only on the final event; a final run's report is shown only when its outcome is a
    report's, as in the outcome object."""
    final = bool(event["final"])
    report = _report(record)
    if final and record["outcome"] not in REPORT_OUTCOMES:
        report = None
    phase = record["phase"] if record["phase"] in ("launch", "session") else "launch"
    return {
        "tool": TOOL,
        "hook": POST_STATE_CHANGE,
        "object_type": OBJECT_TYPE,
        "object_id": csession_id,
        "repo": record["repo"],
        "labels": list(labels),
        "ts": now,
        "event_id": int(event["id"]),
        "event_ts": event["event_ts"],
        "run": int(record["run_no"]),
        "pending": record["wrapper_pid"] is None and record["ended_at"] is None,
        "phase": phase,
        "forked_from": record["session_forked_from"],
        "launch_kind": record["session_launch_kind"],
        "run_kind": record["run_kind"],
        "reads_third_party": bool(record["reads_third_party"]),
        "tainted": bool(record["session_tainted"]),
        "mode": record["mode"],
        "sandbox_active": bool(record["sandbox_active"]),
        "decision_ids": _decision_ids(record),
        "state": event["to_state"],
        "activity": event["to_activity"],
        "needs_input_reason": event["needs_input_reason"],
        "previous_state": event["from_state"],
        "previous_activity": event["from_activity"],
        "live": not final,
        "final": final,
        "report": report,
        "outcome": record["outcome"] if final else None,
        "end_reason": record["end_reason"] if final else None,
        "exit_code": record["exit_code"] if final else None,
        "cost_usd": record["cost_usd"] if final and phase == "session" else None,
        "claude_session_id": record["session_claude_session_id"],
        "session_started_at": record["session_started_at"],
        "session_end_reason": record["session_end_reason"],
        "message": record["message"],
        "cause": event["cause"],
    }


# Delivery


@dataclass
class Delivery:
    """What one ``deliver`` call did."""

    delivered: list[int] = field(default_factory=list)  # event ids marked delivered
    lease_busy: bool = False  # another live holder delivers: nothing done
    lease_lost: bool = False  # the lease was lost mid-pass: the rest is left undelivered
    blocked: bool = False  # stopped at an event of a run another live wrapper delivers


def _log(log: logs.EventLog, event: str, **fields: Any) -> None:
    try:
        log.write(event, **fields)
    except (OSError, ValueError):
        pass  # best effort: delivery never fails for its log


class Deliverer:
    """Delivers a session's undelivered events (see the module docs). Uses ``st`` from one
    thread only (the one that calls it); hooks run on helper threads."""

    def __init__(
        self,
        st: store.Store,
        cfg: Config,
        *,
        gateway: Gateway,
        environ: Mapping[str, str],
        state_root: str,
        log: logs.EventLog | None = None,
        renew_interval: float = LEASE_RENEW_INTERVAL,
    ) -> None:
        self.st, self.cfg, self.gateway = st, cfg, gateway
        self.environ, self.state_root = environ, state_root
        self.log = log or logs.EventLog(os.path.join(state_root, LOG_NAME))
        self.renew_interval = renew_interval
        self.entries = config_entries(cfg)
        self._async: list[threading.Thread] = []

    def deliver(self, csession_id: str, *, wait_async: bool = True) -> Delivery:
        """Take the session's delivery lease and deliver its undelivered events in id order,
        then release the lease (and, with ``wait_async``, wait for the async entries
        started). A lease another live holder has means nothing is done."""
        done = Delivery()
        lease = lease_name(csession_id)
        try:
            if not self.st.take_lease(lease):
                done.lease_busy = True
                return done
            try:
                self._pass(csession_id, lease, done)
            finally:
                self.st.release_lease(lease)
        finally:
            if wait_async:
                self.wait_async()
        return done

    def wait_async(self) -> None:
        """Wait for every async entry started (each is bounded by its own timeout)."""
        while self._async:
            self._async.pop().join()

    def _pass(self, sid: str, lease: str, done: Delivery) -> None:
        me = self.st.holder
        while True:
            events = self.st.undelivered_events(sid, BATCH)
            if not events:
                return
            for event in events:
                record = self.st.delivery_record(sid, int(event["run_no"]))
                if record is not None and self._other_live_wrapper(record, me):
                    done.blocked = True
                    return
                if record is not None and not self._run_event(sid, lease, event, record):
                    done.lease_lost = True
                    return
                if not self.st.mark_delivered(int(event["id"]), lease):
                    done.lease_lost = True
                    return
                done.delivered.append(int(event["id"]))

    def _other_live_wrapper(self, record: Mapping[str, Any], me: store.Holder) -> bool:
        """Whether the run is open with a wrapper that is alive and not this process."""
        pid, start = record["wrapper_pid"], record["wrapper_start"] or ""
        if record["ended_at"] is not None or pid is None or pid == me.pid:
            return False
        if start == "":
            return store.pid_exists(int(pid))
        return self.st.liveness(int(pid), start)

    def _run_event(
        self, sid: str, lease: str, event: Mapping[str, Any], record: Mapping[str, Any]
    ) -> bool:
        """Run the event's selected entries in order; False when the lease was lost."""
        labels = stored_labels(event["labels_json"])
        entries = [*self.entries, *invocation_entries(record["invocation_json"])]
        chosen = [
            entry
            for entry in entries
            if selected(entry, run_kind=record["run_kind"], labels=labels, state=event["to_state"])
        ]
        if not chosen:
            return True
        now = store.format_ts(self.st.clock())
        payload = json.dumps(
            build_payload(event, record, csession_id=sid, labels=labels, now=now),
            ensure_ascii=False,
        )
        env = hook_env(
            self.environ, secret_env=self.cfg.secret_env, hook=POST_STATE_CHANGE, csession_id=sid
        )
        sh = resolve_sh(env)
        for entry in chosen:
            if not self.st.renew_lease(lease):
                return False
            hook = _Hook(
                self,
                entry,
                sh=sh,
                env=env,
                payload=payload,
                sid=sid,
                run_no=int(record["run_no"]),
                event_id=int(event["id"]),
            )
            thread = threading.Thread(
                target=hook.run, name=f"hook {entry.name}, event {event['id']}"
            )
            thread.start()
            if entry.async_:
                self._async.append(thread)
                continue
            if not self._wait(thread, lease):
                return False
        return True

    def _wait(self, thread: threading.Thread, lease: str) -> bool:
        """Wait for a hook thread, renewing the lease every ``renew_interval``; False when a
        renewal found the lease lost (the hook still runs to its end or timeout)."""
        held = True
        while True:
            thread.join(self.renew_interval)
            if not thread.is_alive():
                return held
            if held and not self.st.renew_lease(lease):
                held = False


@dataclass(frozen=True)
class _Hook:
    """One entry run for one event, on its own thread: run it, then log how it ran
    (``hook_delivered``; ``hook_failed`` or ``hook_timeout`` at ``warn`` as well)."""

    deliverer: Deliverer
    entry: Entry
    sh: str | None
    env: Mapping[str, str]
    payload: str
    sid: str
    run_no: int
    event_id: int

    def run(self) -> None:
        d = self.deliverer
        ran = run_entry(
            d.gateway,
            self.entry,
            sh=self.sh,
            env=self.env,
            cwd=d.state_root,
            payload=self.payload,
            kill_grace=d.cfg.kill_grace,
        )
        message = f"{POST_STATE_CHANGE} {self.entry.name}, event {self.event_id}: {ran.detail}"
        fields: dict[str, Any] = {"csession_id": self.sid, "run": int(self.run_no)}
        _log(d.log, "hook_delivered", message=message, **fields)
        if ran.result != "ok":
            failure = "hook_timeout" if ran.result == "timeout" else "hook_failed"
            _log(d.log, failure, level="warn", message=message, **fields)


def deliver(
    st: store.Store,
    cfg: Config,
    csession_id: str,
    *,
    gateway: Gateway,
    environ: Mapping[str, str],
    state_root: str,
) -> Delivery:
    """Synchronous delivery for a command that changed a run with no live wrapper (or
    ``exec`` after its run ended)."""
    return Deliverer(st, cfg, gateway=gateway, environ=environ, state_root=state_root).deliver(
        csession_id
    )


class Worker:
    """``exec``'s background delivery thread (design.md, Supervision): ``wake`` from the
    poll loop (non-blocking), ``close`` once the run is final (one last pass, then waits for
    the hooks, each bounded by its timeout). Uses a store connection of its own; never
    raises into ``exec``: an error is logged and retried at the next wake."""

    def __init__(
        self,
        open_store: Callable[[], store.Store],
        cfg: Config,
        csession_id: str,
        *,
        gateway: Gateway,
        environ: Mapping[str, str],
        state_root: str,
        idle: float = LEASE_RENEW_INTERVAL,
        renew_interval: float = LEASE_RENEW_INTERVAL,
    ) -> None:
        self.open_store, self.cfg, self.csession_id = open_store, cfg, csession_id
        self.gateway, self.environ, self.state_root = gateway, environ, state_root
        self.idle, self.renew_interval = idle, renew_interval
        self.log = logs.EventLog(os.path.join(state_root, LOG_NAME))
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._main, name="post-state-change worker")

    def start(self) -> Worker:
        self._thread.start()
        return self

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> None:
        """Deliver what is left (the final event included) and stop. Idempotent."""
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive() or self._thread.ident is not None:
            self._thread.join()

    def _main(self) -> None:
        try:
            st = self.open_store()
        except Exception as exc:
            self._failed(exc)
            return
        try:
            deliverer = Deliverer(
                st,
                self.cfg,
                gateway=self.gateway,
                environ=self.environ,
                state_root=self.state_root,
                log=self.log,
                renew_interval=self.renew_interval,
            )
            try:
                while True:
                    stopping = self._stop.is_set()  # read before the pass: close() comes last
                    self._wake.clear()
                    try:
                        if st.has_undelivered(self.csession_id):
                            deliverer.deliver(self.csession_id, wait_async=False)
                    except Exception as exc:
                        st.rollback_open_transaction()
                        self._failed(exc)
                    if stopping:
                        return
                    self._wake.wait(self.idle)
            finally:
                deliverer.wait_async()
        finally:
            st.close()

    def _failed(self, exc: BaseException) -> None:
        _log(
            self.log,
            "hook_failed",
            level="warn",
            csession_id=self.csession_id,
            message=f"{POST_STATE_CHANGE} delivery failed ({type(exc).__name__}); retried later",
        )


def start_worker(
    st: store.Store,
    cfg: Config,
    csession_id: str,
    *,
    gateway: Gateway,
    environ: Mapping[str, str],
    state_root: str,
) -> Worker:
    """A started ``Worker`` for ``exec``'s session, on a new connection to ``st``'s database
    (same liveness check and clock)."""

    def open_store() -> store.Store:
        return store.Store.open(st.path, migrate=False, liveness=st.liveness, clock=st.clock)

    return Worker(
        open_store,
        cfg,
        csession_id,
        gateway=gateway,
        environ=environ,
        state_root=state_root,
        idle=min(cfg.poll_interval, LEASE_RENEW_INTERVAL),
    ).start()
