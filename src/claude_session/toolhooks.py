"""Lifecycle hooks: ``post-state-change`` selection, execution and delivery, and the
``pre-launch`` runner (interface.md, Hooks; design.md, Lifecycle hooks and Data model).

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
Hook output is untrusted and unused: stdout and stderr go to ``/dev/null``
(``discard_output``), never logged, and an entry has finished when its ``sh`` has exited
(processes it leaves in the background are not waited for). A failing or timed-out hook, or one
the runner could not run at all, is logged (``hook_failed``/``hook_timeout``, warn) and never
fatal: ``post-state-change`` cannot veto. The payload's ``ts`` is taken just before each entry
runs.

**Delivery** (``Deliverer``): at least once, in event-id order per session. A deliverer takes
the session's lease ``delivery:<id>`` (the store's lease: holder PID + start time + token,
60 s expiry, a dead holder's lease taken over at once), runs the selected entries of each
undelivered event in order, renewing the lease every ``LEASE_RENEW_INTERVAL`` while a hook
runs (a renewal that fails with a store error is retried at the next interval; the lease counts
as lost only when a renewal finds it lost or its expiry has passed, and a running hook is
always waited for), and marks the event delivered (``events.delivered_at``) in the transaction
that renews the lease, only while it still holds it. A crash between a hook and the mark leaves
the event undelivered: the next deliverer runs it again, so receivers deduplicate on
``event_id``. An event whose run has a live wrapper other than this process stops the pass
(that ``exec`` delivers it; later events wait behind it). Entries with ``async: true`` do not
hold up later entries or events: each runs on a thread of its own (its timeout and group kill
still apply) and the event is marked once its hook has started and received its payload (stdin
written and closed, or the runner ended without starting it); the deliverer waits for them
before it returns (``deliver``) or stops (``Worker.close``). A deliverer that is itself killed
(SIGKILL, crash) leaves the hook it was running unsupervised in its own process group. Each
hook's group is therefore recorded as it starts (``hook_groups``: the group ID, its leader's
start-time token and the deliverer) and removed once the hook has finished; the next deliverer
of the session (and ``reconcile``) first ends the groups of deliverers that are gone
(``end_orphaned_groups``: only while the leader is verified by its token), so the redelivered
event never overlaps its orphaned copy. A group whose leader exited while members it started
still run cannot be verified and is left alone.

**Cancel.** A deliverer given a ``cancel`` event (``exec`` after a SIGTERM, SIGINT or SIGHUP
once its child was reaped) starts no further hook once it is set, ends the running hooks'
process groups (SIGTERM, then SIGKILL after ``kill_grace``), leaves the interrupted event
unmarked, releases the lease and returns; the remaining events stay for the next delivering
command.

**Closing passes** (``Worker.close``, ``deliver(..., patience=...)``): a pass that finds the
lease busy or fails with a store error is retried every ``RETRY_DELAY`` until the session has
no undelivered event, ``patience`` seconds passed (then it gives up and logs) or it is
canceled.

``exec`` delivers on a background thread (``Worker``, its own store connection) woken by the
poll loop, so a slow hook delays later hooks, never supervision, and never the hook-event fast
path, which only appends events. Commands that change a run with no live wrapper deliver
synchronously with ``deliver``.

**Pre-launch** (launch step 6, ``run_pre_launch``; ``launch.pre_launch`` builds the payload
and records the result): the same ``Entry``, ``selected`` (without ``states``), ``hook_env``
and ``sh -c`` execution, but synchronous and reading the output (``run_captured``: pipes,
``PRE_LAUNCH_KEEP`` raw bytes kept per stream; the group is SIGKILLed once ``sh`` exits, so
nothing it left in the group outlives it; the group is recorded in ``hook_groups`` with
event 0, so ``reconcile`` ends it if ``exec`` is killed meanwhile). Exit 0 continues, exit 13
escalates (later entries still run), any other exit, a timeout or an entry that could not run
vetoes at once (fail closed). Stdout is an untrusted decision protocol, read strictly from
its raw bytes (``parse_decision``); the decision ids of all entries are capped at
``DECISION_IDS_MAX_BYTES`` escaped, so the log and audit records carrying them stay small.
The outcome message is the hook's stderr, control characters stripped, redacted against the
session's secret values and capped at ``MESSAGE_MAX`` UTF-8 bytes (``outcome_message``).
Each entry that exited 13 is recorded by ``entry_hash`` (name, command, selectors), the input
of a later ``--override-escalation``. ``dryrun.hooks_json`` selects with ``selected`` too, so
the plan and a run never disagree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sqlite3
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import durations, ids, logs, pathrules, store
from .endmap import REPORT_OUTCOMES
from .gateway import DEFAULT_PATH, Gateway, GatewayCanceled, GatewayTimeout

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
LEASE_TTL = store.LEASE_TTL.total_seconds()
# Pause between the retries of a closing pass that found the lease busy or the store failing.
RETRY_DELAY = 0.3


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


def pre_launch_config_entries(cfg: Config) -> list[Entry]:
    """The ``hooks.pre-launch`` entries of ``cfg``, in order (no ``async``, no ``states``)."""
    return [
        Entry(
            cmd=hook.cmd,
            timeout=hook.timeout,
            origin="config",
            index=n,
            kinds=_tuple(hook.kinds),
            exclude_kinds=_tuple(hook.exclude_kinds),
            labels=_tuple(hook.labels),
        )
        for n, hook in enumerate(cfg.hooks.pre_launch, start=1)
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
    return flag_entries(
        _strings(invocation.get("hooks")), _strings(invocation.get("hook_timeouts")), hook
    )


def flag_entries(hooks: Sequence[str], hook_timeouts: Sequence[str], hook: str) -> list[Entry]:
    """The ``--hook <hook>=<cmd>`` entries among ``hooks``, in order, with the timeout of the
    last ``--hook-timeout <hook>=<duration>`` among ``hook_timeouts`` (default 30 s; one that
    does not parse: the default)."""
    timeout = DEFAULT_TIMEOUT
    for item in hook_timeouts:
        name, sep, value = item.partition("=")
        if sep and name == hook:
            try:
                timeout = durations.parse(value, positive=True)
            except ValueError:
                timeout = DEFAULT_TIMEOUT
    entries: list[Entry] = []
    for item in hooks:
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
    """How one entry ran: ``ok``, ``failed`` (non-zero exit, or could not start or run),
    ``timeout`` or ``canceled``; ``detail`` is fixed text and numbers, never hook output."""

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
    on_started: Callable[[], None] | None = None,
    canceled: Callable[[], bool] | None = None,
    on_spawn: Callable[[int], None] | None = None,
) -> Ran:
    """Run ``entry`` once (see the module docs). Never raises for the hook's own failure.
    ``on_spawn`` gets the hook's process group ID (``sh``'s PID) as soon as it exists;
    ``on_started`` is called once ``sh`` has started and its stdin was written and closed;
    ``canceled`` ends the hook's group early (``Ran("canceled")``)."""
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
            discard_output=True,
            on_started=on_started,
            canceled=canceled,
            on_spawn=on_spawn,
        )
    except GatewayTimeout:
        return Ran("timeout", f"timed out after {entry.timeout:g} s; process group ended")
    except GatewayCanceled:
        return Ran("canceled", "canceled by a signal; process group ended")
    except OSError as exc:
        return Ran("failed", f"could not start sh ({type(exc).__name__})")
    if result.returncode != 0:
        return Ran("failed", f"exit {result.returncode}")
    return Ran("ok", "exit 0")


# Pre-launch (launch step 6)

PRE_LAUNCH = "pre-launch"
# The exit code that escalates; 0 continues, any other code vetoes.
ESCALATE_EXIT = 13
# Bytes of a pre-launch hook's stdout read as its decision (a longer output is ignored).
PRE_LAUNCH_OUTPUT_MAX = 2048
# Raw bytes kept of each stream of a pre-launch hook: more than ``PRE_LAUNCH_OUTPUT_MAX``, so
# a longer stdout is told apart, and room for stderr's control characters to be stripped
# before the message is capped.
PRE_LAUNCH_KEEP = 8 * 1024
# The outcome message: control characters stripped, then at most this many UTF-8 bytes.
MESSAGE_MAX = 2048
# ``hook_groups.event_id`` of a pre-launch hook's group (events are numbered from 1).
PRE_LAUNCH_EVENT = 0
# All the decision ids one run's pre-launch hooks may add, measured as escaped ASCII JSON (the
# form the log and audit records are written in, where one character can take up to 12
# bytes). With at most ``PRE_LAUNCH_OUTPUT_MAX`` bytes of stdout per hook, this keeps a
# record that carries them well inside the 4 MiB audit record limit however many hooks run.
DECISION_IDS_MAX_BYTES = 256 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_JSON_SPACE = " \t\n\r"


def entry_hash(entry: Entry) -> str:
    """The hash that names an entry in ``runs.escalated_hooks`` and ``overridden_hooks``: the
    SHA-256 (hex) of its name (origin and position), command and selectors as canonical JSON.
    Moving, editing or reselecting an entry changes it, so an override never covers a hook
    configuration the human did not see escalate."""
    data = {
        "name": entry.name,
        "cmd": entry.cmd,
        "kinds": None if entry.kinds is None else list(entry.kinds),
        "exclude_kinds": None if entry.exclude_kinds is None else list(entry.exclude_kinds),
        "labels": None if entry.labels is None else list(entry.labels),
    }
    text = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def parse_decision(stdout: bytes) -> tuple[list[str], str | None]:
    """The ``decision_id``s of a pre-launch hook's raw stdout, and why the output was ignored
    (None: it was not). Read strictly, as untrusted input: more than
    ``PRE_LAUNCH_OUTPUT_MAX`` bytes is ignored whatever it holds; the bytes must be valid
    UTF-8 (decoded strictly, no newline translation); empty (or whitespace-only) output
    carries no decision and is not an error; anything else must be one JSON object without
    duplicate keys or ``NaN`` and ``Infinity``, whose ``decision_id``, when present, is a
    string or a list of strings without unpaired surrogates. Other keys are ignored. Output
    that breaks any rule is ignored as a whole (no decision id taken from it)."""
    if len(stdout) > PRE_LAUNCH_OUTPUT_MAX:
        return [], f"longer than {PRE_LAUNCH_OUTPUT_MAX} bytes"
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return [], "not valid UTF-8"
    if text.strip(_JSON_SPACE) == "":
        return [], None
    try:
        value = json.loads(text, parse_constant=_no_constant, object_pairs_hook=_unique_keys)
    except (ValueError, RecursionError):
        return [], "not JSON"
    if not isinstance(value, dict):
        return [], "not a JSON object"
    if "decision_id" not in value:
        return [], None
    found = value["decision_id"]
    if isinstance(found, str):
        found = [found]
    if not isinstance(found, list) or not all(isinstance(item, str) for item in found):
        return [], "decision_id is not a string or a list of strings"
    # json.loads joins an escaped surrogate pair into one character; what is left unpaired
    # could not be written as UTF-8 and could pair up differently once re-escaped.
    if any(_SURROGATE.search(item) for item in found):
        return [], "decision_id holds an unpaired surrogate"
    return list(found), None


def escaped_size(value: str) -> int:
    """The bytes ``value`` takes as an ASCII JSON string (quotes included)."""
    return len(json.dumps(value, ensure_ascii=True))


def outcome_message(stderr: bytes, secrets: Collection[str] = ()) -> str:
    """A hook's raw stderr as the outcome message: decoded (invalid UTF-8 replaced), control
    characters stripped, redacted against ``secrets`` (``logs.redact_text``), then cut to at
    most ``MESSAGE_MAX`` UTF-8 bytes on a character boundary."""
    text = _CONTROL.sub("", stderr.decode("utf-8", "replace"))
    text = logs.redact_text(text, logs.usable_secrets(secrets))
    return text.encode("utf-8")[:MESSAGE_MAX].decode("utf-8", "ignore")


@dataclass(frozen=True)
class Captured:
    """How one pre-launch entry ran: ``exited`` (with its exit code and raw output),
    ``timeout`` (with the stderr read before it) or ``failed`` (it could not be started or
    run); ``detail`` is fixed text and numbers, never hook output."""

    result: str
    detail: str
    returncode: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""


def run_captured(
    gateway: Gateway,
    entry: Entry,
    *,
    sh: str | None,
    env: Mapping[str, str],
    cwd: str,
    payload: str,
    kill_grace: float,
    on_spawn: Callable[[int], None] | None = None,
) -> Captured:
    """Run a pre-launch ``entry`` once, reading its output (``run_entry`` discards it):
    ``sh -c <cmd>`` in its own process group, the payload on stdin, at most
    ``PRE_LAUNCH_KEEP`` raw bytes kept of each stream (the rest is read and dropped, so the
    hook never blocks on a full pipe). Once ``sh`` exits its group gets SIGKILL
    (``kill_group_on_exit``: a process it left in the group does not outlive it); on timeout
    the group gets SIGTERM, then SIGKILL after ``kill_grace``. ``on_spawn`` gets the group
    ID once it exists. Any ``Exception`` from the runner is a ``failed`` result, never
    raised; a ``BaseException`` (a signal ending the launch) propagates once the gateway has
    ended the group."""
    if sh is None:
        return Captured("failed", "sh not found on the sanitized PATH")
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
            kill_group_on_exit=True,
            output_limit=PRE_LAUNCH_KEEP,
            on_spawn=on_spawn,
        )
    except GatewayTimeout as exc:
        return Captured(
            "timeout",
            f"timed out after {entry.timeout:g} s; process group ended",
            stderr=exc.stderr,
        )
    except OSError as exc:
        return Captured("failed", f"could not start sh ({type(exc).__name__})")
    except Exception as exc:  # e.g. a NUL in a --hook command: a defined veto, no traceback
        return Captured("failed", f"could not run the hook ({type(exc).__name__})")
    return Captured(
        "exited",
        f"exit {result.returncode}",
        returncode=result.returncode,
        stdout=result.stdout_bytes,
        stderr=result.stderr_bytes,
    )


@dataclass(frozen=True)
class Verdict:
    """What step 6 decided: ``outcome`` ``continue``, ``vetoed`` or ``escalated``, with the end
    reason (``hook_veto``, ``hook_timeout``, ``hook_escalate``) and outcome message of a veto
    or escalation; the hooks' ``decision_ids`` in order; ``escalated``, the hashes of every
    entry that exited 13 (overridden or not: the next ``--override-escalation`` covers them
    all); ``overridden``, those whose exit 13 counted as continue."""

    outcome: str = "continue"
    end_reason: str | None = None
    message: str | None = None
    decision_ids: tuple[str, ...] = ()
    escalated: tuple[str, ...] = ()
    overridden: tuple[str, ...] = ()


@dataclass
class Collected:
    """What step 6's entries have returned so far, filled by ``run_pre_launch`` as each entry
    finishes, so a signal that ends the launch midway still leaves the decision ids and
    escalating hashes already collected for the caller to record."""

    decision_ids: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    overridden: list[str] = field(default_factory=list)

    def verdict(
        self, outcome: str = "continue", end_reason: str | None = None, message: str | None = None
    ) -> Verdict:
        return Verdict(
            outcome,
            end_reason,
            message,
            tuple(self.decision_ids),
            tuple(self.escalated),
            tuple(self.overridden),
        )


def run_pre_launch(
    gateway: Gateway,
    entries: Sequence[Entry],
    *,
    payload: Callable[[], str],
    sh: str | None,
    env: Mapping[str, str],
    cwd: str,
    kill_grace: float,
    overridable: Collection[str] = (),
    log: logs.EventLog,
    csession_id: str,
    run: int,
    st: store.Store | None = None,
    secrets: Collection[str] = (),
    collected: Collected | None = None,
) -> Verdict:
    """Launch step 6 (interface.md, Hooks: ``pre-launch`` outcome rules): the selected
    ``entries`` run one after another, each with a fresh ``payload()``. Exit 0 continues;
    exit 13 escalates (the remaining entries still run), unless the entry's hash is in
    ``overridable`` (``--override-escalation``: the previous run's ``escalated_hooks``), when
    it continues; any other exit, a timeout, or an entry that could not run vetoes at once
    (fail closed). The first veto, or else the first escalation that was not overridden,
    gives the outcome; its stderr is the message (``outcome_message``, redacted against
    ``secrets``; a fixed description when it wrote none). With ``st``, each entry's process
    group is recorded in ``hook_groups`` (event ``PRE_LAUNCH_EVENT``, holder ``st.holder``)
    while it runs. Each entry's stdout is read with ``parse_decision``; ids beyond
    ``DECISION_IDS_MAX_BYTES`` in all are dropped and logged. What the entries return is
    accumulated in ``collected`` as they finish. Every veto, escalation, timeout, failure and
    ignored output is logged, naming the entry by origin and position, never by its command
    or output."""
    fields: dict[str, Any] = {"csession_id": csession_id, "run": run}
    got = Collected() if collected is None else collected
    budget = DECISION_IDS_MAX_BYTES
    escalation: str | None = None  # the message of the first escalation that stands
    for entry in entries:
        what = f"{PRE_LAUNCH} {entry.name}"
        group = None if st is None else _GroupRecord(st, csession_id, PRE_LAUNCH_EVENT, log)
        try:
            ran = run_captured(
                gateway,
                entry,
                sh=sh,
                env=env,
                cwd=cwd,
                payload=payload(),
                kill_grace=kill_grace,
                on_spawn=None if group is None else group.record,
            )
        finally:
            if group is not None:
                group.remove()
        text = outcome_message(ran.stderr, secrets) or None
        if ran.result == "timeout":
            _log(log, "hook_timeout", level="warn", message=f"{what}: {ran.detail}", **fields)
            return got.verdict("vetoed", "hook_timeout", text or f"{what} {ran.detail}")
        if ran.result == "failed":
            _log(log, "hook_failed", level="warn", message=f"{what}: {ran.detail}", **fields)
            _log(log, "hook_veto", level="warn", message=f"{what}: {ran.detail}", **fields)
            return got.verdict("vetoed", "hook_veto", f"{what} {ran.detail}")
        found, problem = parse_decision(ran.stdout)
        if problem is not None:
            _log(
                log,
                "hook_output_ignored",
                level="warn",
                message=f"{what}: stdout ignored ({problem})",
                **fields,
            )
        for item in found:
            size = escaped_size(item) + 1  # and its separator
            if size > budget:
                _log(
                    log,
                    "hook_output_ignored",
                    level="warn",
                    message=f"{what}: decision ids beyond {DECISION_IDS_MAX_BYTES} bytes in"
                    " all were dropped",
                    **fields,
                )
                budget = 0
                break
            budget -= size
            got.decision_ids.append(item)
        if ran.returncode == 0:
            continue
        if ran.returncode == ESCALATE_EXIT:
            digest = entry_hash(entry)
            got.escalated.append(digest)
            if digest in overridable:
                got.overridden.append(digest)
                _log(
                    log,
                    "hook_escalate",
                    message=f"{what}: exit 13, overridden by --override-escalation",
                    **fields,
                )
                continue
            _log(log, "hook_escalate", level="warn", message=f"{what}: exit 13", **fields)
            if escalation is None:
                escalation = text or f"{what} escalated the launch (exit 13)"
            continue
        _log(log, "hook_veto", level="warn", message=f"{what}: {ran.detail}", **fields)
        return got.verdict(
            "vetoed", "hook_veto", text or f"{what} vetoed the launch ({ran.detail})"
        )
    if escalation is not None:
        return got.verdict("escalated", "hook_escalate", escalation)
    return got.verdict()


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
    canceled: bool = False  # stopped by ``cancel``: the rest is left undelivered


def _log(log: logs.EventLog, event: str, **fields: Any) -> None:
    try:
        log.write(event, **fields)
    except (OSError, ValueError):
        pass  # best effort: delivery never fails for its log


def _joined(thread: threading.Thread, timeout: float) -> bool:
    thread.join(timeout)
    return not thread.is_alive()


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
        cancel: threading.Event | None = None,
    ) -> None:
        self.st, self.cfg, self.gateway = st, cfg, gateway
        self.environ, self.state_root = environ, state_root
        self.log = log or logs.EventLog(os.path.join(state_root, LOG_NAME))
        self.renew_interval = renew_interval
        self.cancel = cancel
        self.entries = config_entries(cfg)
        self._async: list[threading.Thread] = []
        # When the lease expires (time.monotonic), as of the last successful take or renewal.
        self._expiry = 0.0

    def canceled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def deliver(self, csession_id: str, *, wait_async: bool = True) -> Delivery:
        """Take the session's delivery lease and deliver its undelivered events in id order,
        then release the lease (and, with ``wait_async``, wait for the async entries
        started). A lease another live holder has means nothing is done."""
        done = Delivery()
        lease = lease_name(csession_id)
        try:
            if self.canceled():
                done.canceled = True
                return done
            asked = time.monotonic()
            if not self.st.take_lease(lease):
                done.lease_busy = True
                return done
            self._expiry = asked + LEASE_TTL
            try:
                # A deliverer that died mid-hook left that hook's group running: end it before
                # its event is delivered again (``end_orphaned_groups``).
                end_orphaned_groups(
                    self.st, csession_id, kill_grace=self.cfg.kill_grace, log=self.log
                )
                self._pass(csession_id, lease, done)
            finally:
                self.st.release_lease(lease)
        finally:
            if wait_async:
                self.wait_async()
        return done

    def deliver_all(
        self, csession_id: str, *, patience: float, wait_async: bool = True
    ) -> Delivery:
        """``deliver``, retried every ``RETRY_DELAY`` while the lease is busy or the store
        fails, until the session has no undelivered event, ``patience`` seconds passed (then
        it gives up and logs) or it is canceled. Other errors propagate."""
        deadline = time.monotonic() + patience
        try:
            while True:
                try:
                    done = self.deliver(csession_id, wait_async=False)
                    if not done.lease_busy:
                        return done
                    problem = "the delivery lease is busy"
                except sqlite3.Error as exc:
                    self.st.rollback_open_transaction()
                    done = Delivery()
                    problem = f"store error {type(exc).__name__}"
                if self.canceled():
                    done.canceled = True
                    return done
                try:
                    if not self.st.has_undelivered(csession_id):
                        return done
                except sqlite3.Error:
                    self.st.rollback_open_transaction()
                if time.monotonic() >= deadline:
                    _log(
                        self.log,
                        "hook_failed",
                        level="warn",
                        csession_id=csession_id,
                        message=f"{POST_STATE_CHANGE} delivery gave up after {patience:g} s "
                        f"({problem}); the next delivering command delivers the rest",
                    )
                    return done
                time.sleep(RETRY_DELAY)
        finally:
            if wait_async:
                self.wait_async()

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
                if self.canceled():
                    done.canceled = True
                    return
                record = self.st.delivery_record(sid, int(event["run_no"]))
                if record is not None and self._other_live_wrapper(record, me):
                    done.blocked = True
                    return
                if record is not None:
                    result = self._run_event(sid, lease, event, record)
                    if result != "done":
                        done.lease_lost = result == "lost"
                        done.canceled = result == "canceled"
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
    ) -> str:
        """Run the event's selected entries in order: ``done``, ``lost`` (the lease was lost)
        or ``canceled``. Never returns while a synchronous entry's hook still runs."""
        labels = stored_labels(event["labels_json"])
        entries = [*self.entries, *invocation_entries(record["invocation_json"])]
        chosen = [
            entry
            for entry in entries
            if selected(entry, run_kind=record["run_kind"], labels=labels, state=event["to_state"])
        ]
        if not chosen:
            return "done"
        env = hook_env(
            self.environ, secret_env=self.cfg.secret_env, hook=POST_STATE_CHANGE, csession_id=sid
        )
        sh = resolve_sh(env)
        for entry in chosen:
            if self.canceled():
                return "canceled"
            if not self._renew(lease, sid):
                return "lost"
            now = store.format_ts(self.st.clock())  # per entry: when this one is delivered
            payload = json.dumps(
                build_payload(event, record, csession_id=sid, labels=labels, now=now),
                ensure_ascii=False,
            )
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
            if entry.async_:
                self._async = [t for t in self._async if t.is_alive()]
                self._async.append(thread)
                thread.start()
                # Marked only once the hook got its payload (or its runner ended).
                held = self._wait(hook.started.wait, lease, sid)
            else:
                thread.start()
                try:
                    held = self._wait(lambda t, thread=thread: _joined(thread, t), lease, sid)
                finally:
                    thread.join()
            if self.canceled():
                return "canceled"
            if not held:
                return "lost"
        return "done"

    def _wait(self, ready: Callable[[float], bool], lease: str, sid: str) -> bool:
        """Wait until ``ready(renew_interval)`` is true, renewing the lease every
        ``renew_interval``; False when the lease was lost meanwhile (waiting goes on)."""
        held = True
        while not ready(self.renew_interval):
            if held and not self._renew(lease, sid):
                held = False
        return held

    def _renew(self, lease: str, sid: str) -> bool:
        """Renew the lease. False when a renewal finds it lost, or when a renewal fails with a
        store error (logged; retried at the next call) after its expiry has passed."""
        asked = time.monotonic()
        try:
            held = self.st.renew_lease(lease)
        except sqlite3.Error as exc:
            self.st.rollback_open_transaction()
            _log(
                self.log,
                "hook_failed",
                level="warn",
                csession_id=sid,
                message=f"{POST_STATE_CHANGE} delivery lease renewal failed "
                f"({type(exc).__name__}); retried",
            )
            return time.monotonic() < self._expiry
        if held:
            self._expiry = asked + LEASE_TTL
        return held


@dataclass
class _Hook:
    """One entry run for one event, on its own thread: run it, then log how it ran
    (``hook_delivered``; ``hook_failed`` or ``hook_timeout`` at ``warn`` as well; a canceled
    hook only ``hook_failed``). ``started`` is set once the hook got its payload, or when the
    thread ends without that."""

    deliverer: Deliverer
    entry: Entry
    sh: str | None
    env: Mapping[str, str]
    payload: str
    sid: str
    run_no: int
    event_id: int
    started: threading.Event = field(default_factory=threading.Event)

    def run(self) -> None:
        d = self.deliverer
        group = _GroupRecord(d.st, self.sid, self.event_id, d.log)
        try:
            try:
                ran = run_entry(
                    d.gateway,
                    self.entry,
                    sh=self.sh,
                    env=self.env,
                    cwd=d.state_root,
                    payload=self.payload,
                    kill_grace=d.cfg.kill_grace,
                    on_started=self.started.set,
                    canceled=None if d.cancel is None else d.cancel.is_set,
                    on_spawn=group.record,
                )
            except Exception as exc:  # never escapes the thread unlogged (e.g. a NUL in cmd)
                ran = Ran("failed", f"could not run the hook ({type(exc).__name__})")
            finally:
                group.remove()
            message = f"{POST_STATE_CHANGE} {self.entry.name}, event {self.event_id}: {ran.detail}"
            fields: dict[str, Any] = {"csession_id": self.sid, "run": int(self.run_no)}
            if ran.result != "canceled":
                _log(d.log, "hook_delivered", message=message, **fields)
            if ran.result != "ok":
                failure = "hook_timeout" if ran.result == "timeout" else "hook_failed"
                _log(d.log, failure, level="warn", message=message, **fields)
        finally:
            self.started.set()


class _GroupRecord:
    """A running hook's process group in ``hook_groups`` (``Store.add_hook_group``), on a
    store connection of its own (the hook thread never uses the deliverer's), removed once
    the hook has finished. The holder is ``st``'s process: the deliverer, or the ``exec``
    running a ``pre-launch`` hook (``event_id`` ``PRE_LAUNCH_EVENT``). Best effort: a group
    that cannot be recorded is only logged (a holder that dies then leaves it unrecorded,
    the documented limit)."""

    def __init__(self, st: store.Store, sid: str, event_id: int, log: logs.EventLog) -> None:
        self.path, self.liveness, self.clock = st.path, st.liveness, st.clock
        self.holder = st.holder  # the deliverer's identity, read before the hook starts
        self.sid, self.event_id, self.log = sid, event_id, log
        self.own: store.Store | None = None
        self.row: int | None = None

    def record(self, pgid: int) -> None:
        try:
            own = store.Store.open(
                self.path, migrate=False, liveness=self.liveness, clock=self.clock
            )
            self.own = own  # closed by remove(), even when a signal ends the hook below
            leader = store.start_time(pgid)
            self.row = own.add_hook_group(self.sid, self.event_id, pgid, leader, self.holder)
        except Exception as exc:
            _log(
                self.log,
                "hook_failed",
                level="warn",
                csession_id=self.sid,
                message=f"{_group_hook(self.event_id)}: the hook's process group could not be"
                f" recorded ({type(exc).__name__})",
            )

    def remove(self) -> None:
        try:
            if self.own is not None and self.row is not None:
                self.own.remove_hook_group(self.row)
        except Exception:
            pass  # a leftover row is ended (verified) or dropped by the next deliverer
        finally:
            if self.own is not None:
                self.own.close()
                self.own = None


# How often an orphaned hook group's leader is checked while it is given kill_grace.
_GROUP_POLL = 0.05


def end_orphaned_groups(
    st: store.Store,
    csession_id: str | None,
    *,
    kill_grace: float,
    log: logs.EventLog | None = None,
) -> list[int]:
    """End the hook process groups a dead holder (a deliverer, or an ``exec`` killed during a
    ``pre-launch`` hook) left running (of ``csession_id``, or of every session): for each
    recorded group whose holder is gone, the group is signaled
    only while its leader is verified (the recorded PID still runs with the recorded
    start-time token and still leads its group): SIGTERM, then SIGKILL after ``kill_grace``
    if the verified leader still runs. Groups of a live deliverer are left alone; a row
    whose leader cannot be verified (gone, reused, token unknown) is dropped without any
    signal (members that outlived the leader cannot be told apart from a reused group ID).
    Returns the process group IDs signaled."""
    ended: list[int] = []
    for row in st.hook_groups(csession_id):
        if st.holder_alive(int(row["holder_pid"]), str(row["holder_start"] or "")):
            continue
        pgid, token = int(row["pgid"]), str(row["leader_start"] or "")
        if _verified_leader(pgid, token):
            ended.append(pgid)
            _end_group(pgid, token, kill_grace)
            if log is not None:
                _log(
                    log,
                    "hook_failed",
                    level="warn",
                    csession_id=str(row["csession_id"]),
                    message=f"{_group_hook(int(row['event_id']))}: the hook its dead"
                    f" {_group_holder(int(row['event_id']))} left running was ended (process"
                    f" group {pgid})",
                )
        st.remove_hook_group(int(row["id"]))
    return ended


def _group_hook(event_id: int) -> str:
    """How a recorded hook group's hook is named in the log: ``pre-launch`` (event 0) or
    ``post-state-change event <n>``."""
    if event_id == PRE_LAUNCH_EVENT:
        return PRE_LAUNCH
    return f"{POST_STATE_CHANGE} event {event_id}"


def _group_holder(event_id: int) -> str:
    return "exec" if event_id == PRE_LAUNCH_EVENT else "deliverer"


def _verified_leader(pgid: int, token: str) -> bool:
    """Whether ``pgid`` is still the process the recorded token names, leading its group."""
    if token == "" or not 0 < pgid < 2**31:
        return False
    if store.start_time(pgid) != token:
        return False
    try:
        return os.getpgid(pgid) == pgid
    except OSError:
        return False


def _end_group(pgid: int, token: str, kill_grace: float) -> None:
    """SIGTERM the verified group; SIGKILL it after ``kill_grace`` while its leader is still
    the verified process (checked again right before)."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + kill_grace
    while time.monotonic() < deadline:
        if not _verified_leader(pgid, token):
            return
        time.sleep(_GROUP_POLL)
    if _verified_leader(pgid, token):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


def deliver(
    st: store.Store,
    cfg: Config,
    csession_id: str,
    *,
    gateway: Gateway,
    environ: Mapping[str, str],
    state_root: str,
    cancel: threading.Event | None = None,
    patience: float = 0.0,
) -> Delivery:
    """Synchronous delivery for a command that changed a run with no live wrapper (or
    ``exec`` after its run ended). With ``patience``, a busy lease or a store error is
    retried up to that many seconds (``Deliverer.deliver_all``)."""
    d = Deliverer(st, cfg, gateway=gateway, environ=environ, state_root=state_root, cancel=cancel)
    if patience > 0:
        return d.deliver_all(csession_id, patience=patience)
    return d.deliver(csession_id)


def deliver_best_effort(
    st: store.Store,
    cfg: Config,
    csession_id: str,
    *,
    gateway: Gateway,
    environ: Mapping[str, str],
    state_root: str,
    cancel: threading.Event | None = None,
    patience: float = LEASE_TTL,
) -> Delivery | None:
    """``deliver`` with ``patience`` (default: the lease TTL) for a command that changed a run
    with no live wrapper, never raising an ``Exception``: an error is logged and whatever stays
    undelivered is left for the next delivering command (``reconcile``). Returns what was
    done, or None after an error."""
    try:
        return deliver(
            st,
            cfg,
            csession_id,
            gateway=gateway,
            environ=environ,
            state_root=state_root,
            cancel=cancel,
            patience=patience,
        )
    except Exception as exc:
        st.rollback_open_transaction()
        _log(
            logs.EventLog(os.path.join(state_root, LOG_NAME)),
            "hook_failed",
            level="warn",
            csession_id=csession_id,
            message=f"{POST_STATE_CHANGE} delivery failed ({type(exc).__name__}); the next"
            " delivering command delivers the rest",
        )
        return None


class Worker:
    """``exec``'s background delivery thread (design.md, Supervision): ``wake`` from the
    poll loop (non-blocking), ``close`` once the run is final (one last pass, retried while
    the lease is busy or the store fails, up to ``patience`` seconds; then waits for the
    hooks, each bounded by its timeout). Uses a store connection of its own; never raises
    into ``exec``: an error is logged and retried at the next wake. Once ``cancel`` is set it
    stops (see the module docs)."""

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
        cancel: threading.Event | None = None,
        patience: float = LEASE_TTL,
    ) -> None:
        self.open_store, self.cfg, self.csession_id = open_store, cfg, csession_id
        self.gateway, self.environ, self.state_root = gateway, environ, state_root
        self.idle, self.renew_interval = idle, renew_interval
        self.cancel, self.patience = cancel, patience
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
                cancel=self.cancel,
            )
            try:
                while True:
                    stopping = self._stop.is_set()  # read before the pass: close() comes last
                    self._wake.clear()
                    if deliverer.canceled():
                        return
                    try:
                        if stopping:
                            deliverer.deliver_all(
                                self.csession_id, patience=self.patience, wait_async=False
                            )
                        elif st.has_undelivered(self.csession_id):
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
    cancel: threading.Event | None = None,
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
        cancel=cancel,
    ).start()
