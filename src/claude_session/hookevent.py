"""The injected-hook fast path (``hook-event``) and the state transition table.

See design.md (Hook-event fast path, State machine) and interface.md (State transitions,
Internal commands). ``__main__`` dispatches here before importing ``cli``, so this module and
everything it imports stay standard library only, plus ``store``, ``outbox`` and
``secretindex`` (both stdlib only) and, once it is needed, ``gateway``: never ``config``,
Pydantic, YAML or argparse.

``PostToolUse`` and ``Stop`` drain the run's outbox: requests are claimed and pre-checked
before the transaction (``prepare_drain``), recorded inside it after the hook's own row, and
cleaned up and logged after the commit; a rejection found by ``PostToolUse`` is the hook's
output (design.md, Outbox and ingestion).

Contract with ``__main__``: ``main`` raises on any error and writes the hook's JSON output to
``sys.stdout`` only on success; ``__main__`` contains the error (logged as
``hook_event_failed`` with the exception type only, never its text), drops partial output and
always exits 0.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, BinaryIO

from . import outbox, secretindex
from .store import BUSY_TIMEOUT, HookRun, Store, db_path

# The events the injected hooks pass (interface.md, Internal commands). Kept equal to
# ``__main__.HOOK_EVENTS`` (tested): ``__main__`` must not import this module to log.
EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PermissionRequest",
        "Notification",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }
)
LIVE_STATES = frozenset({"starting", "working", "idle", "needs-input", "reported"})
MODES = frozenset({"headless", "interactive"})

# The session-id grammar (ids.SESSION_ID; ids is not on the hook path, so it is restated here
# and tested equal).
SESSION_ID = re.compile(r"cs-[a-z2-7]{8}")
# Claude Code session ids are UUIDs; anything else is refused (it is later passed to
# ``claude --resume``).
CLAUDE_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Input basenames (design.md, Session directories: the artifact-name grammar).
INPUT_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")

# Bytes of the payload kept for parsing. stdin is always read to its end (so Claude Code never
# sees a broken pipe); a longer payload is refused by the events that need its fields.
PAYLOAD_MAX_BYTES = 16 * 1024 * 1024
PATH_MAX_CHARS = 4096
SESSION_END_REASON_MAX_CHARS = 256
HOOKCONF_NAME = "hookconf.json"
HOOKCONF_MAX_BYTES = 64 * 1024
HOOKCONF_MAX_INPUTS = 1024

BACKSTOP_REASON = (
    "claude-session: no status report has been filed for this run. File exactly one report "
    "now with claude-session report, as described in your instructions (done, blocked, "
    "failed or needs-input), then stop."
)


class HookEventError(Exception):
    """A hook-event invocation that cannot be processed. ``__main__`` logs only the type."""


# The transition table (interface.md, State transitions)


@dataclass(frozen=True)
class Values:
    """The four per-run values the state is derived from."""

    activity: str | None
    prompt: str | None  # permission, question or elicitation: a prompt waiting for the user
    escalation_open: bool
    has_report: bool


class _Keep:
    def __repr__(self) -> str:
        return "KEEP"


class _ByMode:
    def __repr__(self) -> str:
        return "BY_MODE"


KEEP: Any = _Keep()
BY_MODE: Any = _ByMode()  # headless: working, interactive: idle


@dataclass(frozen=True)
class Rule:
    """One row of the table: what the event changes, and where it applies."""

    activity: Any = KEEP  # "working", "idle", KEEP or BY_MODE
    prompt: Any = KEEP  # None (cleared), a prompt kind, or KEEP
    keep_other_prompt: bool = False  # a waiting prompt of another kind is kept
    escalation_open: Any = KEEP  # True, False or KEEP
    report: bool = False  # sets has_report
    after_session_start: bool = False  # applies only after the run's first SessionStart
    only_in_starting: bool = False  # applies only in state ``starting``


TABLE: dict[str, Rule] = {
    # SessionStart startup/resume/fork: in starting, activity by mode; elsewhere unchanged.
    "session_start_new": Rule(activity=BY_MODE, only_in_starting=True),
    # SessionStart clear/compact: the Claude id is recorded, the state is unchanged.
    "session_start_other": Rule(),
    "user_prompt_submit": Rule(activity="working", prompt=None, escalation_open=False),
    "permission": Rule(activity="working", prompt="permission", after_session_start=True),
    # Notification permission_prompt: it follows a PermissionRequest (about 6 s later, also
    # for AskUserQuestion), so a waiting question or elicitation is kept.
    "permission_notification": Rule(
        activity="working",
        prompt="permission",
        keep_other_prompt=True,
        after_session_start=True,
    ),
    "question": Rule(activity="working", prompt="question", after_session_start=True),
    "elicitation": Rule(activity="working", prompt="elicitation", after_session_start=True),
    # TODO(#41): a subagent's PostToolUse carries the parent's session id (spike A1/A2), so a
    # background subagent's tool clears a pending permission prompt of the main agent. Check
    # live whether PermissionRequest carries agent_id / tool_use_id; if so, clear only on the
    # matching PostToolUse (design.md, State machine: accepted until then).
    "post_tool_use": Rule(activity="working", prompt=None, after_session_start=True),
    "stop_background": Rule(activity="working", after_session_start=True),
    # Stop without background tasks, Notification idle_prompt.
    "stop_idle": Rule(activity="idle", after_session_start=True),
    # Report rows: applied by outbox ingestion (``_apply_report``) through ``transition``.
    "report_final": Rule(escalation_open=False, report=True),
    "report_needs_input": Rule(escalation_open=True, report=True),
    "session_end": Rule(),
}


def derive(v: Values) -> tuple[str, str | None, str | None]:
    """``(state, needs_input_reason, activity)`` of a live run (interface.md, Derivation)."""
    if v.prompt is not None:
        return "needs-input", v.prompt, v.activity
    if v.escalation_open:
        return "needs-input", "escalation", v.activity
    if v.has_report:
        return "reported", None, v.activity
    if v.activity is None:
        return "starting", None, None
    return v.activity, None, v.activity


def transition(v: Values, event: str, *, mode: str, session_started: bool) -> Values:
    """The values after table event ``event`` (``KeyError`` for an unknown one). An event that
    does not apply leaves them unchanged."""
    rule = TABLE[event]
    if rule.after_session_start and not session_started:
        return v
    if rule.only_in_starting and derive(v)[0] != "starting":
        return v
    activity = v.activity
    if rule.activity is BY_MODE:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        activity = "working" if mode == "headless" else "idle"
    elif rule.activity is not KEEP:
        activity = rule.activity
    prompt = v.prompt if rule.prompt is KEEP else rule.prompt
    if rule.keep_other_prompt and v.prompt is not None:
        prompt = v.prompt
    return replace(
        v,
        activity=activity,
        prompt=prompt,
        escalation_open=v.escalation_open if rule.escalation_open is KEEP else rule.escalation_open,
        has_report=v.has_report or rule.report,
    )


def values_from(
    *,
    state: str,
    activity: str | None,
    needs_input_reason: str | None,
    escalation_open: bool,
    has_report: bool,
) -> Values:
    """The values behind a stored live state. A waiting prompt is the stored
    ``needs_input_reason`` (prompts take precedence, so it is always visible);
    ``escalation_open`` and ``has_report`` come from the run's report (design.md, State
    machine)."""
    prompt = needs_input_reason if state == "needs-input" else None
    if prompt == "escalation":
        prompt = None
    return Values(
        activity=activity,
        prompt=prompt,
        escalation_open=escalation_open,
        has_report=has_report,
    )


# Arguments, environment and payload


_RUN_ARG = re.compile(r"[1-9][0-9]{0,8}")


def parse_argv(argv: list[str]) -> tuple[str, str, int | None]:
    """Exactly ``--state-dir <absolute dir> --run <n> <Event>``, as the injected hooks pass
    it (the run whose generated settings hold the hook), or the older ``--state-dir
    <absolute dir> <Event>`` without the run (hooks of a session launched by an earlier
    version, kept working across an upgrade). Returns the state dir, the event and the run
    (None for the older form)."""
    run: int | None = None
    if len(argv) == 5 and argv[0] == "--state-dir" and argv[2] == "--run":
        if _RUN_ARG.fullmatch(argv[3]) is None:
            raise HookEventError("--run must be a run number")
        run = int(argv[3])
        argv = [argv[0], argv[1], argv[4]]
    if len(argv) != 3 or argv[0] != "--state-dir":
        raise HookEventError("usage: hook-event --state-dir <abs> --run <n> <event>")
    state_dir, event = argv[1], argv[2]
    if not os.path.isabs(state_dir) or "\0" in state_dir:
        raise HookEventError("--state-dir must be an absolute path")
    if event not in EVENTS:
        raise HookEventError("unknown hook event")
    return state_dir, event, run


def session_id_from(environ: Mapping[str, str]) -> str:
    value = environ.get("CSESSION_ID", "")
    if SESSION_ID.fullmatch(value) is None:
        raise HookEventError("CSESSION_ID is missing or not a session id")
    return value


def read_payload(stdin: BinaryIO | None = None) -> bytes | None:
    """Read ``stdin`` (default: ``sys.stdin``'s binary buffer; none reads as empty) to its end;
    returns the first ``PAYLOAD_MAX_BYTES`` bytes, or None when it is longer."""
    if stdin is None:
        stdin = getattr(sys.stdin, "buffer", None)
        if stdin is None:
            return b""
    kept = bytearray()
    oversized = False
    while chunk := stdin.read(65536):
        if not oversized:
            kept += chunk
            if len(kept) > PAYLOAD_MAX_BYTES:
                oversized = True
                kept.clear()
    return None if oversized else bytes(kept)


def _payload_object(data: bytes | None) -> dict[str, Any]:
    if data is None:
        raise HookEventError("payload too large")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise HookEventError("payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise HookEventError("payload is not a JSON object")
    return payload


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise HookEventError(f"payload field {key} is missing or not a string")
    return value


def _has_control(text: str) -> bool:
    return any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in text)


@dataclass(frozen=True)
class Hook:
    """One classified hook invocation."""

    cause: str  # the hook event name (events.cause)
    event: str  # the table event
    claude_session_id: str | None = None
    source: str | None = None
    transcript_path: str | None = None
    session_end_reason: str | None = None
    backstop_candidate: bool = False  # a Stop without background tasks


def classify(cause: str, data: bytes | None) -> Hook | None:
    """Map a hook event and its payload to a table event; None for a notification type
    claude-session does not track. ``UserPromptSubmit`` and ``PostToolUse`` need no payload
    field, so their (possibly huge) payloads are never parsed. Malformed fields raise."""
    if cause == "UserPromptSubmit":
        return Hook(cause, "user_prompt_submit")
    if cause == "PostToolUse":
        return Hook(cause, "post_tool_use")
    payload = _payload_object(data)
    if cause == "SessionStart":
        return _session_start(payload)
    if cause == "PermissionRequest":
        question = _text(payload, "tool_name") == "AskUserQuestion"
        return Hook(cause, "question" if question else "permission")
    if cause == "Notification":
        event = {
            "permission_prompt": "permission_notification",
            "elicitation_dialog": "elicitation",
            "idle_prompt": "stop_idle",
        }.get(_text(payload, "notification_type"))
        return None if event is None else Hook(cause, event)
    if cause == "Stop":
        tasks = payload.get("background_tasks", [])
        if not isinstance(tasks, list):
            raise HookEventError("payload field background_tasks is not a list")
        if tasks:
            return Hook(cause, "stop_background")
        return Hook(cause, "stop_idle", backstop_candidate=True)
    if cause == "SessionEnd":
        reason = _text(payload, "reason")
        if not reason or len(reason) > SESSION_END_REASON_MAX_CHARS or _has_control(reason):
            raise HookEventError("payload field reason is not a SessionEnd reason")
        return Hook(cause, "session_end", session_end_reason=reason)
    raise HookEventError("unknown hook event")


def _session_start(payload: dict[str, Any]) -> Hook:
    source = _text(payload, "source")
    if source in ("startup", "resume", "fork"):
        event = "session_start_new"
    elif source in ("clear", "compact"):
        event = "session_start_other"
    else:
        raise HookEventError("payload field source is not a SessionStart source")
    claude_id = _text(payload, "session_id")
    if CLAUDE_SESSION_ID.fullmatch(claude_id) is None:
        raise HookEventError("payload field session_id is not a Claude session id")
    transcript = payload.get("transcript_path")
    if transcript is not None and (
        not isinstance(transcript, str)
        or not os.path.isabs(transcript)
        or len(transcript) > PATH_MAX_CHARS
        or _has_control(transcript)
    ):
        raise HookEventError("payload field transcript_path is not an absolute path")
    return Hook(
        "SessionStart",
        event,
        claude_session_id=claude_id,
        source=source,
        transcript_path=transcript,
    )


# hookconf.json: hook-event's configuration, written by exec into the private run directory


@dataclass(frozen=True)
class HookConf:
    """``hookconf.json``: the run's input names and the report caps ingestion applies."""

    input_files: list[str]
    summary_max_bytes: int
    artifact_max_bytes: int

    @property
    def limits(self) -> outbox.Limits:
        return outbox.Limits(
            summary_max_bytes=self.summary_max_bytes,
            artifact_max_bytes=self.artifact_max_bytes,
        )


HOOKCONF_KEYS = frozenset({"v", "input_files", "summary_max_bytes", "artifact_max_bytes"})


def hookconf_bytes(
    input_files: list[str], *, summary_max_bytes: int, artifact_max_bytes: int
) -> bytes:
    """The content of ``private/<id>/runs/<n>/hookconf.json`` for a run whose inputs are
    ``input_files`` (basenames, in order) and whose report caps are the given ones (outbox
    ingestion reads them here, never from config or an agent-writable file).

    ``exec`` writes it at launch step 7 (``SessionDirs.write_private_run_file``).
    TODO(#38): add the notes command settings (``notes_context_cmd`` and its limits).
    """
    conf = {
        "v": 1,
        "input_files": list(input_files),
        "summary_max_bytes": summary_max_bytes,
        "artifact_max_bytes": artifact_max_bytes,
    }
    _check_hookconf(conf)
    return json.dumps(conf, ensure_ascii=False).encode("utf-8")


def _cap(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 2**53 - 1


def _check_hookconf(conf: object) -> HookConf:
    """Validate a hookconf object."""
    if not isinstance(conf, dict) or set(conf) != HOOKCONF_KEYS or conf["v"] != 1:
        raise HookEventError("hookconf.json has an unknown shape")
    names = conf["input_files"]
    if not isinstance(names, list) or len(names) > HOOKCONF_MAX_INPUTS:
        raise HookEventError("hookconf.json input_files is not a list")
    for name in names:
        if not isinstance(name, str) or INPUT_NAME.fullmatch(name) is None or name in (".", ".."):
            raise HookEventError("hookconf.json has an invalid input name")
    if len(set(names)) != len(names):
        raise HookEventError("hookconf.json has duplicate input names")
    if not _cap(conf["summary_max_bytes"]) or not _cap(conf["artifact_max_bytes"]):
        raise HookEventError("hookconf.json has an invalid size cap")
    return HookConf(
        input_files=names,
        summary_max_bytes=conf["summary_max_bytes"],
        artifact_max_bytes=conf["artifact_max_bytes"],
    )


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _open_own_dir(name: str, dir_fd: int | None) -> int:
    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        os.close(fd)
        raise HookEventError("private directory is not ours")
    return fd


def private_run_path(private_dir: str, run_no: int) -> str:
    return os.path.join(os.path.normpath(private_dir), "runs", str(run_no))


def open_private_run_dir(private_dir: str, run_no: int) -> int:
    """Open ``<private_dir>/runs/<run_no>`` without following a symlink at any step under the
    trusted ``private`` directory (only that parent is resolved; it is then walked component
    by component, and ``<id>``, ``runs`` and ``<n>`` are opened without following a symlink
    and must be the user's own). Returns a descriptor the caller closes."""
    normalized = os.path.normpath(private_dir)
    session_part = os.path.basename(normalized)
    if not os.path.isabs(normalized) or session_part in ("", ".", ".."):
        raise HookEventError("private directory is not an absolute session path")
    trusted = os.path.realpath(os.path.dirname(normalized))
    parts = [p for p in trusted.split("/") if p] + [session_part, "runs", str(run_no)]
    fd = os.open("/", _DIR_FLAGS)
    try:
        for index, part in enumerate(parts):
            if index >= len(parts) - 3:  # private/<id>, runs, <n>: verified ours
                next_fd = _open_own_dir(part, fd)
            else:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_hookconf_at(run_fd: int) -> HookConf:
    """Read ``hookconf.json`` in the private run directory ``run_fd``: a regular single-link
    file of the user's own that no one else can access, of at most ``HOOKCONF_MAX_BYTES``."""
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    file_fd = os.open(HOOKCONF_NAME, file_flags, dir_fd=run_fd)
    try:
        info = os.fstat(file_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > HOOKCONF_MAX_BYTES
        ):
            raise HookEventError("hookconf.json is not a private regular file")
        data = os.read(file_fd, HOOKCONF_MAX_BYTES + 1)
    finally:
        os.close(file_fd)
    if len(data) > HOOKCONF_MAX_BYTES:
        raise HookEventError("hookconf.json is too large")
    try:
        conf = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HookEventError("hookconf.json is not JSON") from exc
    return _check_hookconf(conf)


def read_hookconf(private_dir: str, run_no: int) -> HookConf:
    """Read ``<private_dir>/runs/<run_no>/hookconf.json`` (``open_private_run_dir``,
    ``read_hookconf_at``)."""
    fd = open_private_run_dir(private_dir, run_no)
    try:
        return read_hookconf_at(fd)
    finally:
        os.close(fd)


# Output


def backstop_output() -> dict[str, Any]:
    return {"decision": "block", "reason": BACKSTOP_REASON}


def session_start_output(csession_dir: str, run_no: int, inputs: list[str]) -> dict[str, Any]:
    """The context line (interface.md, Launch order): input paths are relative to the session
    directory, and the inputs part is omitted when the run has none."""
    context = f"claude-session directory: {csession_dir}; run {run_no}"
    if inputs:
        context += "; inputs: " + ", ".join(f"inputs/{run_no}/{name}" for name in inputs)
    # TODO(#38): append "\n<notes block>" when a notes command is configured.
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


# The handler

# The hooks that drain the outbox (design.md, Hook-event fast path).
DRAIN_CAUSES = frozenset({"PostToolUse", "Stop"})
LOG_NAME = "log.jsonl"


def _escalation_open(store: Store, run: HookRun) -> bool:
    """An unanswered escalation: the run's report is ``needs-input`` and no
    ``UserPromptSubmit`` event of the run follows its latest ``report`` event, by event id,
    never by timestamp (design.md, State machine)."""
    if run.report_status != "needs-input":
        return False
    return not store.prompt_submitted_after_report(run)


def _drainable(run: HookRun | None) -> bool:
    """A run whose outbox a hook may drain: adopted, open, live, not forgotten, with the
    secret digest and directory identities ``exec`` recorded."""
    return (
        run is not None
        and not run.forgotten
        and run.adopted_and_open
        and run.state in LIVE_STATES
        and run.csession_dir is not None
        and run.private_dir is not None
        and run.secret_sha256 is not None
        and run.outbox_dev_ino is not None
        and run.artifacts_dev_ino is not None
    )


def prepare_drain(
    store: Store, csession_id: str, log: _Log, *, run_no: int | None = None
) -> outbox.Drain | None:
    """Claim and pre-check the current run's outbox requests, before the hook's transaction
    (design.md, Hook-event fast path). None when there is nothing to drain, or when
    ``run_no`` (the hook's run) is not the current run. An ``outbox/`` that cannot be
    trusted is logged (``outside_dir``) and never drained; the hook's own event still
    applies."""
    run = store.hook_run(csession_id)
    if not _drainable(run) or (run is not None and run_no is not None and run.run_no != run_no):
        return None
    assert run is not None and run.private_dir is not None and run.csession_dir is not None
    assert run.secret_sha256 is not None
    assert run.outbox_dev_ino is not None and run.artifacts_dev_ino is not None
    run_fd = open_private_run_dir(run.private_dir, run.run_no)
    try:
        return outbox.Drain.prepare(
            csession_id=csession_id,
            run_no=run.run_no,
            csession_dir=run.csession_dir,
            private_run_fd=run_fd,
            private_run_dir=private_run_path(run.private_dir, run.run_no),
            outbox_dev_ino=run.outbox_dev_ino,
            artifacts_dev_ino=run.artifacts_dev_ino,
            secret_sha256=run.secret_sha256,
            limits=lambda: read_hookconf_at(run_fd).limits,
            scanner=lambda: secretindex.read_at(run_fd),
        )
    except outbox.OutboxDirError:
        log.write("report_rejected", "warn", csession_id, run.run_no, reason="outside_dir")
        return None
    finally:
        os.close(run_fd)


@dataclass(frozen=True)
class Applied:
    """What ``apply_hook`` did: the run it applied to (None: ignored), the output decided
    inside the transaction and whether the drain's requests were recorded."""

    run: HookRun | None
    output: dict[str, Any] | None = None
    drained: bool = False


def _apply_report(store: Store, csession_id: str, drain: outbox.Drain) -> None:
    """Record the drained requests after the hook's own row (interface.md: "its row applies
    after that hook's own event") and apply the ingested report through the table, with a
    ``report`` event even when only the report changed. Runs in a savepoint: a failure (for
    example an artifact that cannot be frozen) rolls back only the drain, which keeps its
    requests in ``claimed/`` and its failure for the log, so the hook's own event still
    applies (design.md, Ingestion). A failure after which SQLite rolled the whole
    transaction back is re-raised: nothing may then run in autocommit, so the hook fails as
    a whole (logged ``hook_event_failed``, nothing committed)."""
    try:
        with store.savepoint("drain"):
            _record_report(store, csession_id, drain)
    except Exception as exc:
        if not store.conn.in_transaction:
            raise
        drain.rolled_back(type(exc).__name__)


def _record_report(store: Store, csession_id: str, drain: outbox.Drain) -> None:
    run = store.hook_run(csession_id)  # reloaded: the hook's own row changed it
    assert run is not None
    before = values_from(
        state=run.state,
        activity=run.activity,
        needs_input_reason=run.needs_input_reason,
        escalation_open=_escalation_open(store, run),
        has_report=run.report_status is not None,
    )
    status = drain.record(store, run_no=run.run_no, live=True)
    if status is None:
        return
    event = "report_needs_input" if status == "needs-input" else "report_final"
    after = transition(
        before, event, mode=run.mode or "", session_started=run.session_started_at is not None
    )
    state, reason, activity = derive(after)
    # Never delivered here (the fast path): exec's worker or the next delivering command
    # delivers the appended event (cause report) through toolhooks.
    store.apply_state(
        run,
        state=state,
        activity=activity,
        needs_input_reason=reason,
        cause="report",
        always=True,
    )


class _Backstop(Exception):
    """Rolls back a headless Stop that ended without a report, so the backstop can block."""


def _apply_own_row(store: Store, run: HookRun, hook: Hook) -> None:
    """The hook's own row: the transition table and its event."""
    before = values_from(
        state=run.state,
        activity=run.activity,
        needs_input_reason=run.needs_input_reason,
        escalation_open=_escalation_open(store, run),
        has_report=run.report_status is not None,
    )
    started = run.session_started_at is not None
    after = transition(before, hook.event, mode=run.mode or "", session_started=started)
    state, reason, activity = derive(after)
    # Never delivered here (the fast path): exec's worker or the next delivering command
    # delivers the appended event through toolhooks.
    store.apply_state(
        run, state=state, activity=activity, needs_input_reason=reason, cause=hook.cause
    )


def apply_hook(
    store: Store,
    csession_id: str,
    hook: Hook,
    drain: outbox.Drain | None = None,
    *,
    unchecked_requests: bool = False,
    run_no: int | None = None,
) -> Applied:
    """In one transaction: load the session's current run, record SessionStart and SessionEnd
    facts, apply the table and append the event, then record the drained outbox requests
    (``drain``, prepared before the transaction) and apply an ingested report, and decide
    the Stop backstop on whether the run then has a report. ``unchecked_requests``: the
    drain failed with claimed requests it could not check (``outbox.DrainSetupError``), so a
    report may be pending and the backstop is not taken. A run that is not adopted, already
    ended or not in a live state is left alone (its claimed requests stay for the next
    drain), and so is a hook of another run than the current one (``run_no``: a leftover
    hook of an earlier run, whose settings named that run)."""
    with store.transaction():
        run = store.hook_run(csession_id)
        if run is None:
            raise HookEventError("unknown session")
        if run.forgotten:
            return Applied(None)  # ignored silently (design.md, Input and configuration)
        if not run.adopted_and_open or run.state not in LIVE_STATES:
            return Applied(None)
        if run_no is not None and run.run_no != run_no:
            return Applied(None)  # a stale hook of an earlier run: ignored silently
        if drain is not None and drain.run_no != run.run_no:
            # A new run began after the drain was prepared: leave its requests. They stay in
            # the old run's claimed/, which that run's final drain (``final_drain``: every
            # Drain.prepare picks up its run's claimed/ first) or its salvage processes.
            # (A run whose wrapper is lost: its salvage, liveness, calls final_drain.)
            drain = None
        if hook.claude_session_id is not None:
            assert hook.source is not None
            store.record_session_start(
                run,
                claude_session_id=hook.claude_session_id,
                source=hook.source,
                transcript_path=hook.transcript_path,
            )
        if hook.session_end_reason is not None:
            store.record_session_end_reason(run, hook.session_end_reason)
        backstop = (
            hook.backstop_candidate
            and run.session_started_at is not None
            and run.mode == "headless"
            and run.report_status is None
            and not run.backstop_used
            and not unchecked_requests
        )
        try:
            with store.savepoint("hook"):
                _apply_own_row(store, run, hook)
                if drain is not None:
                    _apply_report(store, csession_id, drain)
                if backstop:
                    after = store.hook_run(csession_id)
                    if after is not None and after.report_status is None:
                        raise _Backstop  # no report, even after this Stop's own drain
        except _Backstop:
            if not store.conn.in_transaction:
                raise  # the transaction is gone: never take the backstop in autocommit
            # A blocked Stop changes nothing and records no event (design.md, State machine);
            # the drained requests (none ingested) are recorded again, unless recording failed.
            taken = store.take_backstop(run)
            assert taken  # backstop_used and report_status were read in this transaction
            if drain is not None and drain.failure is None:
                drain.rolled_back()
                _apply_report(store, csession_id, drain)
            drained = drain is not None and drain.failure is None
            return Applied(run, backstop_output(), drained=drained)
    return Applied(run, drained=drain is not None and drain.failure is None)


def final_drain(store: Store, csession_id: str, run_no: int, state_dir: str) -> bool:
    """``exec``'s final drain of its run, after ``claude`` exited and before the end mapping
    (interface.md, Outcome mapping: "first ingests the outbox"): claim the run's ``outbox/``
    requests and those left in its private ``claimed/`` (by a canceled drain, or by a hook
    whose run changed before its transaction), check them, then record them and apply an
    ingested report through the transition table in one transaction (inside a savepoint, as
    ``apply_hook`` does). The run must still be the session's current, open run. Logged like
    a hook drain. Returns whether a drain was recorded; a drain that cannot run is logged
    (``hook_event_failed`` with the exception type only) and its requests stay claimed."""
    log = _Log(state_dir)
    try:  # only while run_no is the current run: never claims a later run's requests
        drain = prepare_drain(store, csession_id, log, run_no=run_no)
    except Exception as exc:
        log.failure(csession_id, f"final drain: {type(exc).__name__}")
        return False
    if drain is None:
        return False
    recorded = False
    try:
        with store.transaction():
            run = store.hook_run(csession_id)
            if run is not None and run.run_no == drain.run_no == run_no and run.adopted_and_open:
                _apply_report(store, csession_id, drain)
                recorded = drain.failure is None
    except BaseException:
        drain.finish(committed=False)
        raise
    drain.finish(committed=recorded)
    if recorded:
        log_drain(log, drain)
    else:
        log_malformed(log, drain)
    if drain.failure is not None:
        log.failure(csession_id, f"final drain: {drain.failure}")
    return recorded


class _Log:
    """Best-effort ``log.jsonl`` records of the hook path (``logs`` is imported only when a
    record is written). Never agent text: request ids are validated UUIDs."""

    def __init__(self, state_dir: str) -> None:
        self.path = os.path.join(state_dir, LOG_NAME)

    def write(
        self,
        event: str,
        level: str,
        csession_id: str,
        run_no: int,
        *,
        reason: str | None = None,
        message: str | None = None,
        count: int | None = None,
    ) -> None:
        try:
            from . import logs

            logs.EventLog(self.path).write(
                event,
                level=level,
                csession_id=csession_id,
                run=run_no,
                reason=reason,
                message=message,
                count=count,
            )
        except Exception:  # logging is best effort (interface.md, Audit and log)
            pass

    def failure(self, csession_id: str, message: str) -> None:
        try:
            from . import logs

            logs.EventLog(self.path).write(
                "hook_event_failed", level="error", csession_id=csession_id, message=message
            )
        except Exception:
            pass


def log_malformed(log: _Log, drain: outbox.Drain) -> None:
    """One aggregate ``report_rejected`` ``malformed`` record (with ``count``) for the
    malformed entries the drain removed, if any."""
    if drain.removed_malformed:
        log.write(
            "report_rejected",
            "warn",
            drain.csession_id,
            drain.run_no,
            reason="malformed",
            count=drain.removed_malformed,
        )


def log_drain(log: _Log, drain: outbox.Drain) -> None:
    """One record per recorded request: ``report_ingested``, ``report_rejected`` (with the
    reason) or ``outbox_discarded``; plus the aggregate record of the malformed entries."""
    sid, run_no = drain.csession_id, drain.run_no
    log_malformed(log, drain)
    for item in drain.items:
        request = f"request {item.request_id}"
        if item.duplicate:
            log.write("outbox_discarded", "info", sid, run_no, message=f"{request}: duplicate")
        elif item.result == "ingested":
            log.write("report_ingested", "info", sid, run_no, message=request)
        elif item.result == "discarded":
            if item.unchecked:
                why = "over the drain cap"
            elif item.superseded:
                why = "superseded by a newer request"
            else:
                why = "a newer report was already ingested"
            log.write("outbox_discarded", "info", sid, run_no, message=f"{request}: {why}")
        elif item.result is not None:
            log.write("report_rejected", "warn", sid, run_no, reason=item.result, message=request)


def main(
    argv: list[str],
    *,
    stdin: BinaryIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Handle one injected hook event (``hook-event --state-dir <abs> --run <n> <Event>``)."""
    data = read_payload(stdin)  # first: stdin is read to its end even for a rejected argv
    state_dir, cause, hook_run_no = parse_argv(argv)
    csession_id = session_id_from(os.environ if environ is None else environ)
    hook = classify(cause, data)
    if hook is None:
        return 0
    log = _Log(state_dir)
    with Store.open(db_path(state_dir), migrate=False, busy_timeout=BUSY_TIMEOUT) as store:
        drain = None
        unchecked = False
        if cause in DRAIN_CAUSES:
            try:
                drain = prepare_drain(store, csession_id, log, run_no=hook_run_no)
            except outbox.DrainSetupError as exc:
                # Claimed requests wait, unchecked, for a usable hookconf.json or
                # secret-index: logged with the reason and the exception type only.
                unchecked = True
                cause_name = type(exc.__cause__).__name__
                log.failure(csession_id, f"{cause}: outbox drain: {exc.reason} ({cause_name})")
            except Exception as exc:
                # The hook's own event must not depend on the outbox: a drain that cannot run
                # (an unusable private directory) is logged with the exception type only, and
                # its claimed requests stay for the next drain.
                log.failure(csession_id, f"{cause}: outbox drain: {type(exc).__name__}")
        try:
            applied = apply_hook(
                store,
                csession_id,
                hook,
                drain,
                unchecked_requests=unchecked,
                run_no=hook_run_no,
            )
        except BaseException:
            if drain is not None:
                drain.finish(committed=False)  # claimed requests stay for the next drain
            raise
    run = applied.run
    output = applied.output
    if drain is not None:
        drain.finish(committed=applied.drained)
        if applied.drained:
            log_drain(log, drain)
            rejection = drain.feedback()
            if cause == "PostToolUse" and rejection is not None:
                output = outbox.rejection_output(rejection)
        else:
            log_malformed(log, drain)
        if drain.failure is not None:
            log.failure(csession_id, f"{cause}: outbox drain: {drain.failure}")
    if run is not None and cause == "SessionStart":
        if run.csession_dir is None or run.private_dir is None:
            raise HookEventError("session has no directories")
        conf = read_hookconf(run.private_dir, run.run_no)
        output = session_start_output(run.csession_dir, run.run_no, conf.input_files)
    if output is not None:
        sys.stdout.write(json.dumps(output) + "\n")
    return 0
