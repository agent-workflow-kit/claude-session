"""``status`` and ``list``: the session views over ``state.db`` and their human output.

The JSON objects follow ``schemas/cli/status.json`` and ``schemas/cli/list.json``. Every value
comes from ``state.db``; text written by the agent or derived from its input (report summaries
and reasons, artifact names, messages, warnings, labels, paths) is untrusted. The JSON objects
carry it as it is stored (the encoder escapes it; bytes that are not UTF-8 were already
replaced by U+FFFD when read, and lone surrogates from JSON escapes are replaced here), and
the human output passes every value through ``terminal.display``.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import endmap, ids, store
from .errors import NotFoundError
from .terminal import display, display_width, no_surrogates

LIVE_STATES = frozenset({"starting", "working", "idle", "needs-input", "reported"})
RESULT_FILE = endmap.RESULT_FILE
# `list` columns are separated by spaces and labels by commas: a cell escapes them, so no
# value can pass for an extra column or label.
_CELL_ESCAPED = " "
_LABEL_ESCAPED = ","


# The inline liveness check (interface.md, Liveness).


def check_liveness(st: store.Store, csession_id: str) -> None:
    """Hook point for the inline liveness check ``status`` and ``list`` run before reading:
    it may finalize a run whose wrapper is dead, and never runs hooks."""
    # TODO(#33): run the single-session liveness check (finalize when the wrapper is dead,
    # cause `liveness`, outcome-file backfill); status and list never deliver its events.
    del st, csession_id


def check_liveness_all(st: store.Store) -> None:
    """Hook point for ``list``: the inline liveness check of every live session."""
    # TODO(#33): run check_liveness for each session whose current run is live.
    del st


# Value coercion: state.db is SQLite (dynamic types), so every value is checked on the way out.


def _text(value: object) -> str | None:
    """A stored value as text: never a lone surrogate (a JSON ``\\udXXX`` escape becomes
    U+FFFD), so the JSON output always encodes as strict UTF-8."""
    if value is None:
        return None
    if isinstance(value, str):
        return no_surrogates(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _flag(value: object) -> bool:
    return bool(_int(value))


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _json(value: object) -> Any:
    text = _text(value)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):  # RecursionError: nested too deeply
        return None


def _json_texts(value: object) -> list[str]:
    """A stored JSON array of strings (anything else counts as empty)."""
    items = _json(value)
    if not isinstance(items, list):
        return []
    return [text for item in items if (text := _text(item)) is not None]


def _section(value: object, key: str) -> dict[str, Any]:
    parsed = _json(value)
    section = parsed.get(key) if isinstance(parsed, dict) else None
    return section if isinstance(section, dict) else {}


# Shared run facts


def _pending(run: dict[str, Any]) -> bool:
    """A pending run: created by ``start``, not yet adopted (no wrapper) and not ended."""
    return run.get("ended_at") is None and run.get("wrapper_pid") is None


def _expires_at(run: dict[str, Any] | None) -> str | None:
    """The adoption deadline, shown only while the run is pending (null otherwise)."""
    if run is None or not _pending(run):
        return None
    return _text(run.get("expires_at"))


def _live(run: dict[str, Any] | None) -> bool:
    return run is not None and run.get("ended_at") is None


def _run_state(run: dict[str, Any], session_state: str | None) -> str | None:
    """The run's own state: its final state once ended, else the session's state."""
    outcome = _text(run.get("outcome"))
    if run.get("ended_at") is not None and outcome is not None:
        try:
            return endmap.final_state(outcome)
        except ValueError:
            pass
    return session_state


def _report(run: dict[str, Any]) -> dict[str, Any] | None:
    """The run's latest report, or None when it has none or its outcome is not a report
    outcome (a run that ended otherwise reports ``report: null``; interface.md, Outcome
    object)."""
    status = _text(run.get("report_status"))
    outcome = _text(run.get("outcome"))
    if status is None or (outcome is not None and outcome not in endmap.REPORT_OUTCOMES):
        return None
    artifact = None
    if run.get("artifact_name") is not None:
        artifact = {
            "name": _text(run["artifact_name"]),
            "path": _text(run.get("artifact_path")),
            "sha256": _text(run.get("artifact_sha256")),
            "size": _int(run.get("artifact_size")),
        }
    return {
        "status": status,
        "implicit": _flag(run.get("report_implicit")),
        "summary": _text(run.get("report_summary")),
        "reason": _text(run.get("report_reason")),
        "artifact": artifact,
        "reported_at": _text(run.get("reported_at")),
    }


def _alive(st: store.Store, pid: object, start: object, *, ended: bool) -> bool:
    """Whether the exact process (PID and start-time token) still runs; read only. A PID
    outside 1..``store.MAX_PID`` names no process. For an ended run, an empty token or a
    check the platform cannot answer counts as gone (``store.pid_known_alive``): its PID may
    have been reused."""
    number = _int(pid)
    if number is None or not 0 < number <= store.MAX_PID:
        return False
    token = _text(start) or ""
    if ended:
        return bool(token) and store.pid_known_alive(number, token)
    return bool(st.liveness(number, token))


# status


def status_object(st: store.Store, text: str, run_no: int | None) -> dict[str, Any]:
    """``status <id> [--run n]`` (schemas/cli/status.json). ``NotFoundError`` (exit 10) for an
    unknown or forgotten session and a run that does not exist; ``AmbiguousError`` (21) for an
    ambiguous prefix."""
    sid = ids.lookup_session_id(text, st.session_id_candidates)
    check_liveness(st, sid)
    record = st.status_record(sid, run_no)
    if record is None or record.session.get("forgotten_at") is not None:
        raise NotFoundError(f"unknown session: {text}")
    if record.run is None:
        if run_no is None:
            raise NotFoundError(f"session {sid} has no current run record")
        raise NotFoundError(f"session {sid} has no run {run_no}")
    session = record.session
    current = record.current_run or {}
    warnings = _status_warnings(record.current_run)
    return {
        "csession_id": sid,
        "created_at": _text(session.get("created_at")),
        "launch_kind": _text(session.get("launch_kind")),
        "run_kind": _text(current.get("run_kind")),
        "state": _text(session.get("state")),
        "activity": _text(session.get("activity")),
        "needs_input_reason": _text(session.get("needs_input_reason")),
        "live": _live(record.current_run),
        "tainted": _flag(session.get("tainted")),
        "tainted_at": _text(session.get("tainted_at")),
        "tainted_by_run": _int(session.get("tainted_by_run")),
        "forked_from": _text(session.get("forked_from")),
        "labels": [_text(label) or "" for label in record.labels],
        "repo": _text(current.get("repo")),
        "mode": _text(current.get("mode")),
        "current_run": _int(session.get("current_run")),
        "pending": bool(record.current_run) and _pending(current),
        "expires_at": _expires_at(record.current_run),
        "updated_at": _text(session.get("updated_at")),
        "csession_dir": _text(session.get("csession_dir"))
        or os.path.join(os.path.dirname(st.path), "sessions", sid),
        "claude_session_id": _text(session.get("claude_session_id")),
        "claude_session_ids": [_claude_id(row) for row in record.claude_ids],
        "run": _run_object(st, record.run, session, is_current=record.run is record.current_run),
        "runs": record.runs,
        "recent_events": [_event(row) for row in record.events],
        "warnings": warnings,
    }


def _status_warnings(current: dict[str, Any] | None) -> list[str]:
    """A live run whose recorded hook interpreter no longer exists (an upgrade with live
    sessions): its injected hooks fail until it ends. A pending run has no hooks injected
    yet (adoption injects them)."""
    if current is None or not _live(current) or _pending(current):
        return []
    interpreter = _text(current.get("hook_interpreter"))
    if not interpreter or os.path.exists(interpreter):
        return []
    return [
        f"the hook interpreter of live run {current.get('run_no')} no longer exists: "
        f"{interpreter}; its hooks fail until the run ends (upgrade claude-session with no "
        "live sessions)"
    ]


def _run_object(
    st: store.Store, run: dict[str, Any], session: dict[str, Any], *, is_current: bool
) -> dict[str, Any]:
    run_no = _int(run.get("run_no")) or 0
    resolved = _section(run.get("resolved_json"), "profile")
    invocation = _json(run.get("invocation_json"))
    inputs = invocation.get("input_files") if isinstance(invocation, dict) else None
    mode = _text(run.get("mode"))
    phase = _text(run.get("phase")) or "launch"
    ended = run.get("ended_at") is not None
    private_dir = _text(session.get("private_dir"))
    result_path = None
    if mode == "headless":
        base = private_dir or os.path.join(
            os.path.dirname(st.path), "private", str(session.get("csession_id"))
        )
        result_path = os.path.join(base, "runs", str(run_no), RESULT_FILE)
    state_since = _text(session.get("state_since")) if is_current and not ended else None
    if state_since is None:
        state_since = _text(run.get("ended_at")) or _text(run.get("created_at"))
    is_error = run.get("is_error")
    return {
        "run": run_no,
        "run_kind": _text(run.get("run_kind")),
        "reads_third_party": _flag(run.get("reads_third_party")),
        "implicit_done": resolved.get("implicit_done") is True,
        "mode": mode,
        # Null until resolved: pending runs and runs that ended during launch.
        # TODO(#28): adoption must record the resolved cwd.
        "cwd": _text(run.get("cwd")) or None,
        "repo": _text(run.get("repo")),
        "model": _text(run.get("model")),
        "skill": _text(run.get("skill")),
        "has_prompt": _flag(run.get("has_prompt")),
        "sandbox": _sandbox(resolved, run),
        "sandbox_active": _flag(run.get("sandbox_active")),
        "permission_profile": _text(run.get("permission_profile")),
        "decision_ids": _json_texts(run.get("decision_ids_json")),
        "input_files": [t for item in inputs if (t := _text(item)) is not None]
        if isinstance(inputs, list)
        else [],
        "pending": _pending(run),
        "expires_at": _expires_at(run),
        "started_at": _text(run.get("started_at")),
        "session_started_at": _text(run.get("session_started_at")),
        "ended_at": _text(run.get("ended_at")),
        "state": _run_state(run, _text(session.get("state"))),
        "phase": phase,
        "message": _text(run.get("message")),
        "stop_requested_at": _text(run.get("stop_requested_at")),
        "warnings": _json_texts(run.get("warnings_json")),
        "state_since": state_since,
        "wrapper_pid": _int(run.get("wrapper_pid")),
        "wrapper_alive": _alive(st, run.get("wrapper_pid"), run.get("wrapper_start"), ended=ended),
        "child_pid": _int(run.get("child_pid")),
        "child_alive": _alive(st, run.get("child_pid"), run.get("child_start"), ended=ended),
        "backstop_used": _flag(run.get("backstop_used")),
        "report": _report(run),
        "outcome": _text(run.get("outcome")),
        "end_reason": _text(run.get("end_reason")),
        "session_end_reason": _text(run.get("session_end_reason")),
        "exit_code": _int(run.get("exit_code")),
        "result_path": result_path,
        "cost_usd": None if phase == "launch" else _finite(run.get("cost_usd")),
        "num_turns": _int(run.get("num_turns")),
        "is_error": None if is_error is None else _flag(is_error),
    }


def _sandbox(resolved: dict[str, Any], run: dict[str, Any]) -> str:
    """The resolved sandbox setting. When the stored profile does not say (corrupt), never
    less isolation than observed: ``required`` when the sandbox ran, else ``off``."""
    value = _text(resolved.get("sandbox"))
    if value in ("required", "preferred", "off"):
        return value
    return "required" if _flag(run.get("sandbox_active")) else "off"


def _claude_id(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "claude_session_id": _text(row.get("claude_session_id")),
        "run": _int(row.get("run_no")),
        "source": _text(row.get("source")) or "other",
        "transcript_path": _text(row.get("transcript_path")),
        "seen_at": _text(row.get("seen_at")),
    }


def _event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": _int(row.get("id")),
        "event_ts": _text(row.get("event_ts")),
        "run": _int(row.get("run_no")),
        "from_state": _text(row.get("from_state")),
        "to_state": _text(row.get("to_state")),
        "from_activity": _text(row.get("from_activity")),
        "to_activity": _text(row.get("to_activity")),
        "needs_input_reason": _text(row.get("needs_input_reason")),
        "cause": _text(row.get("cause")),
        "final": _flag(row.get("final")),
        "delivered": row.get("delivered_at") is not None,
    }


# list


@dataclass(frozen=True)
class ListFilter:
    """The ``list`` filters (interface.md, list). Empty tuples and None filter nothing."""

    labels: tuple[tuple[str, str], ...] = ()
    states: tuple[str, ...] = ()
    kind: str | None = None
    launch_kind: str | None = None
    run_kind: str | None = None
    mode: str | None = None
    needs_input: bool = False
    pending: bool = False
    repo: str | None = None
    since: datetime | None = None
    limit: int | None = None
    forked_from: str | None = None
    # Report statuses; None in the tuple stands for `none` (no report).
    report_statuses: tuple[str | None, ...] = ()
    live: bool = False
    tainted: bool = False


def list_items(st: store.Store, flt: ListFilter) -> list[dict[str, Any]]:
    """``list`` (schemas/cli/list.json ``sessions``): sessions that are not forgotten, most
    recently updated first, filtered, then capped at ``flt.limit``."""
    check_liveness_all(st)
    since = None if flt.since is None else _since_text(flt.since)
    items: list[dict[str, Any]] = []
    for record in st.list_records():
        item = _list_item(record)
        if not _matches(item, record, flt, since):
            continue
        items.append(item)
        if flt.limit is not None and len(items) >= flt.limit:
            break
    return items


def _since_text(when: datetime) -> str:
    """``when`` in ``updated_at``'s fixed-width form, so text order is time order. A time
    outside what UTC can represent (an offset near year 1 or 9999) is clamped; one before
    year 1000 (which ``%Y`` does not pad to four digits) is ``""``, before every time."""
    try:
        utc = when.astimezone(UTC)
    except OverflowError:
        utc = datetime.min.replace(tzinfo=UTC) if when.year == 1 else None
    if utc is None:
        return store.format_ts(datetime.max.replace(tzinfo=UTC))
    if utc.year < 1000:
        return ""
    return store.format_ts(utc)


def _list_item(record: store.ListRecord) -> dict[str, Any]:
    session = record.session
    run = record.run or {}
    return {
        "csession_id": _text(session.get("csession_id")),
        "created_at": _text(session.get("created_at")),
        "launch_kind": _text(session.get("launch_kind")),
        "run_kind": _text(run.get("run_kind")),
        "state": _text(session.get("state")),
        "activity": _text(session.get("activity")),
        "needs_input_reason": _text(session.get("needs_input_reason")),
        "live": _live(record.run),
        "tainted": _flag(session.get("tainted")),
        "tainted_at": _text(session.get("tainted_at")),
        "tainted_by_run": _int(session.get("tainted_by_run")),
        "forked_from": _text(session.get("forked_from")),
        "labels": [_text(label) or "" for label in record.labels],
        "repo": _text(run.get("repo")),
        "mode": _text(run.get("mode")),
        "current_run": _int(session.get("current_run")),
        "pending": bool(record.run) and _pending(run),
        "expires_at": _expires_at(record.run),
        "updated_at": _text(session.get("updated_at")),
        "phase": _text(run.get("phase")) or "launch",
        "last_outcome": _text(run.get("outcome")),
        "last_end_reason": _text(run.get("end_reason")),
        "exit_code": _int(run.get("exit_code")),
        "report_status": _text(run.get("report_status")),
    }


def _matches(
    item: dict[str, Any], record: store.ListRecord, flt: ListFilter, since: str | None
) -> bool:
    labels = set(item["labels"])
    checks = (
        all(f"{key}={value}" in labels for key, value in flt.labels),
        not flt.states or item["state"] in flt.states,
        flt.kind is None or flt.kind == item["launch_kind"] or flt.kind in record.run_kinds,
        flt.launch_kind is None or flt.launch_kind == item["launch_kind"],
        flt.run_kind is None or flt.run_kind == item["run_kind"],
        flt.mode is None or flt.mode == item["mode"],
        not flt.needs_input or item["state"] == "needs-input",
        not flt.pending or item["pending"],
        flt.repo is None or flt.repo == item["repo"],
        since is None or (item["updated_at"] or "") >= since,
        flt.forked_from is None or flt.forked_from == item["forked_from"],
        not flt.report_statuses or item["report_status"] in flt.report_statuses,
        not flt.live or item["live"],
        not flt.tainted or item["tainted"],
    )
    return all(checks)


# Human output


def status_lines(obj: dict[str, Any]) -> list[str]:
    """``status`` without ``--json``: one ``name: value`` line per field, every value through
    ``terminal.display``."""
    run = obj["run"]
    state = _state_text(obj["state"], obj["activity"], obj["needs_input_reason"])
    lines = [
        f"session:        {display(obj['csession_id'])}",
        f"state:          {state}",
        f"live:           {_yes(obj['live'])}",
        f"launch kind:    {display(obj['launch_kind'])}",
        f"mode:           {display(obj['mode'])}",
        f"current run:    {display(obj['current_run'])} of {display(obj['runs'])}",
        f"created:        {display(obj['created_at'])}",
        f"updated:        {display(obj['updated_at'])}",
        f"tainted:        {_yes(obj['tainted'])}",
        f"forked from:    {display(obj['forked_from'])}",
        f"repo:           {display(obj['repo'])}",
        f"labels:         {_joined(obj['labels'])}",
        f"session dir:    {display(obj['csession_dir'])}",
        f"claude session: {display(obj['claude_session_id'])}",
    ]
    if obj["pending"]:
        lines.append(f"pending until:  {display(obj['expires_at'])}")
    lines += [
        f"run {display(run['run'])}:",
        f"  kind:         {display(run['run_kind'])}",
        f"  state:        {display(run['state'])} (phase {display(run['phase'])})",
        f"  cwd:          {display(run['cwd'])}",
        f"  sandbox:      {display(run['sandbox'])} (active: {_yes(run['sandbox_active'])})",
        f"  profile:      {display(run['permission_profile'])}",
        f"  started:      {display(run['started_at'])}",
        f"  ended:        {display(run['ended_at'])}",
        f"  wrapper:      {_process(run['wrapper_pid'], run['wrapper_alive'])}",
        f"  child:        {_process(run['child_pid'], run['child_alive'])}",
    ]
    if run["outcome"] is not None:
        lines.append(
            f"  outcome:      {display(run['outcome'])}"
            f" ({display(run['end_reason'])}), exit {display(run['exit_code'])}"
        )
    if run["cost_usd"] is not None:
        lines.append(f"  cost:         ${run['cost_usd']:.4f}")
    if run["message"]:
        lines.append(f"  message:      {display(run['message'])}")
    report = run["report"]
    if report is not None:
        implicit = " (implicit)" if report["implicit"] else ""
        lines.append(
            f"  report:       {display(report['status'])}{implicit}"
            f" at {display(report['reported_at'])}"
        )
        if report["summary"] is not None:
            lines.append(f"    summary:    {display(report['summary'])}")
        if report["reason"] is not None:
            lines.append(f"    reason:     {display(report['reason'])}")
        artifact = report["artifact"]
        if artifact is not None:
            lines.append(
                f"    artifact:   {display(artifact['name'])}"
                f" {display(artifact['path'])} sha256 {display(artifact['sha256'])}"
            )
    lines += [f"  warning:      {display(warning)}" for warning in run["warnings"]]
    lines += [f"warning: {display(warning)}" for warning in obj["warnings"]]
    return lines


def list_lines(items: Sequence[dict[str, Any]]) -> list[str]:
    """``list`` without ``--json``: a header and one line per session."""
    if not items:
        return ["No sessions."]
    rows = [("ID", "STATE", "KIND", "RUN", "MODE", "UPDATED", "LABELS")]
    for item in items:
        rows.append(
            (
                display(item["csession_id"], _CELL_ESCAPED),
                _state_text(item["state"], item["activity"], item["needs_input_reason"], cell=True),
                display(item["run_kind"], _CELL_ESCAPED),
                display(item["current_run"], _CELL_ESCAPED),
                display(item["mode"], _CELL_ESCAPED),
                display(item["updated_at"], _CELL_ESCAPED),
                _joined(item["labels"]),
            )
        )
    widths = [max(display_width(row[col]) for row in rows) for col in range(len(rows[0]) - 1)]
    return [
        "  ".join(
            [*(_pad(cell, width) for cell, width in zip(row, widths, strict=False)), row[-1]]
        ).rstrip()
        for row in rows
    ]


def _pad(cell: str, width: int) -> str:
    return cell + " " * (width - display_width(cell))


def _state_text(state: object, activity: object, reason: object, *, cell: bool = False) -> str:
    """``state``, then ``(reason)`` or a differing ``[activity]`` (in a ``list`` cell with no
    space before it, and spaces in the values escaped)."""
    extra = _CELL_ESCAPED if cell else ""
    text = display(state, extra)
    if reason is not None:
        text += f"({display(reason, extra)})"
    elif activity is not None and activity != state:
        text += f"{'' if cell else ' '}[{display(activity, extra)}]"
    return text


def _yes(value: object) -> str:
    return "yes" if value else "no"


def _joined(values: Iterable[object]) -> str:
    """Labels joined by commas; a comma inside one is escaped."""
    return ",".join(display(value, _LABEL_ESCAPED) for value in values) or "-"


def _process(pid: object, alive: object) -> str:
    if pid is None:
        return "-"
    return f"{display(pid)} ({'alive' if alive else 'gone'})"
