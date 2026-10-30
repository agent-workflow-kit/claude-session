"""``log.jsonl`` writer (interface.md, Audit and log; schemas/log.json).

Standard library only and import-light: ``hook-event`` logs its internal errors through it
(design.md, Hook-event fast path). Every string in a record is redacted against the configured
secret values before it is written; records are appended with one ``os.write`` each.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime

from . import fsutil

LEVELS = ("debug", "info", "warn", "error")
EVENTS = frozenset(
    {
        "launch",
        "adopt",
        "state_change",
        "report_ingested",
        "report_rejected",
        "outbox_discarded",
        "hook_failed",
        "hook_timeout",
        "hook_veto",
        "hook_escalate",
        "hook_output_ignored",
        "hook_event_failed",
        "notes_failed",
        "signal_sent",
        "reconcile_change",
        "config_warning",
        "session_start_missing",
        "migration",
        "sandbox_lowered",
        "launch_interrupted",
        "outcome_file_failed",
        "hook_delivered",
    }
)
REDACTED = "[redacted]"
TRUNCATED = "[truncated]"
MESSAGE_MAX_CHARS = 4096

type JSONValue = (
    str | int | float | bool | None | list[JSONValue] | tuple[JSONValue, ...] | dict[str, JSONValue]
)


def secret_values(names: Iterable[str], environ: Mapping[str, str]) -> frozenset[str]:
    """The non-empty values of the variables ``names`` in ``environ``: the configured secrets
    (``secret_env``) a writer must redact."""
    return frozenset(value for name in names if (value := environ.get(name, "")))


def _redact_text(text: str, secrets: Collection[str]) -> str:
    spans: list[tuple[int, int]] = []
    for secret in secrets:
        start = text.find(secret)
        while start != -1:
            spans.append((start, start + len(secret)))
            start = text.find(secret, start + 1)
    if not spans:
        return text
    spans.sort()
    parts: list[str] = []
    pos = 0
    end = -1
    for span_start, span_end in spans:
        if span_start > end:  # a new, disjoint span: close the previous one
            if end >= 0:
                parts.append(REDACTED)
            parts.append(text[pos:span_start])
            end = span_end
        else:  # overlapping or adjacent secrets collapse into one marker
            end = max(end, span_end)
        pos = end
    parts.append(REDACTED)
    parts.append(text[pos:])
    return "".join(parts)


def redact(value: JSONValue, secrets: Collection[str]) -> JSONValue:
    """``value`` with every occurrence of a non-empty secret in its strings (at any depth,
    dictionary values and list items) replaced by ``REDACTED``. Tuples become lists."""
    secrets = [s for s in secrets if s]
    return _redact(value, secrets) if secrets else value


def _redact(value: JSONValue, secrets: Collection[str]) -> JSONValue:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(item, secrets) for item in value]
    return value


def describe_exception(exc: BaseException) -> str:
    """``<type>: <text>`` for a log message. The text may hold secrets or untrusted data: the
    writer redacts configured secrets, but callers must not log text that could carry prompts,
    summaries or reasons."""
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def timestamp(now: datetime | None = None) -> str:
    """UTC RFC 3339 timestamp with milliseconds, e.g. ``2026-10-30T12:00:00.123Z``."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_record(path: str, record: dict[str, JSONValue], secrets: Collection[str]) -> None:
    """Redact ``record`` and append it to the JSONL file ``path`` as one line (ASCII JSON, so
    control characters are always escaped and a record never spans lines)."""
    line = json.dumps(redact(record, secrets), separators=(",", ":")) + "\n"
    fsutil.append_private_line(path, line.encode("ascii"))


def _truncate(message: str) -> str:
    if len(message) <= MESSAGE_MAX_CHARS:
        return message
    return message[: MESSAGE_MAX_CHARS - len(TRUNCATED)] + TRUNCATED


class EventLog:
    """Writer for one ``log.jsonl``.

    ``min_level`` drops records below it; ``secrets`` are the values redacted from every
    record (the current run's secret and the values of ``secret_env`` variables).
    """

    # TODO(#3): take from config: the secret_env names (via secret_values) and, if one is
    # added, a log threshold (interface.md names no log-level key yet).
    def __init__(self, path: str, *, min_level: str = "info", secrets: Iterable[str] = ()) -> None:
        if min_level not in LEVELS:
            raise ValueError(f"unknown log level: {min_level!r}")
        self.path = path
        self.min_level = min_level
        self.secrets = frozenset(s for s in secrets if s)

    def write(
        self,
        event: str,
        *,
        level: str = "info",
        csession_id: str | None = None,
        run: int | None = None,
        pid: int | None = None,
        signal: str | None = None,
        decision_ids: Iterable[str] | None = None,
        reason: str | None = None,
        message: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Append one record; return False when ``level`` is below ``min_level``.

        Unset (``None``) fields are omitted. Raises ``ValueError`` for an unknown event or
        level and ``OSError`` when the file cannot be written.
        """
        if event not in EVENTS:
            raise ValueError(f"unknown log event: {event!r}")
        if level not in LEVELS:
            raise ValueError(f"unknown log level: {level!r}")
        if LEVELS.index(level) < LEVELS.index(self.min_level):
            return False
        record: dict[str, JSONValue] = {"ts": timestamp(now), "level": level, "event": event}
        optional: dict[str, JSONValue] = {
            "csession_id": csession_id,
            "run": run,
            "pid": pid,
            "signal": signal,
            "decision_ids": None if decision_ids is None else list(decision_ids),
            "reason": reason,
        }
        record.update((key, value) for key, value in optional.items() if value is not None)
        if message is not None:
            # Redact before truncating, so a cut can never leave part of a secret behind.
            record["message"] = _truncate(_redact_text(message, self.secrets))
        append_record(self.path, record, self.secrets)
        return True
