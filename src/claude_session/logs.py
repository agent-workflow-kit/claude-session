"""``log.jsonl`` writer (interface.md, Audit and log; schemas/log.json).

Standard library only and import-light: ``__main__`` logs contained ``hook-event`` errors
through it (design.md, Hook-event fast path), so it imports no third-party modules (and no
``re``). Text fields are redacted against the secret values before a record is written;
identifiers, enums and timestamps are validated instead. Records are appended with one
``os.write`` each.
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
# Machine reasons (schemas/log.json "reason"): report rejection reasons and launch-phase end
# reasons. Never agent text.
REASONS = frozenset(
    {
        "bad_secret",
        "not_live",
        "stale_run",
        "artifact_changed",
        "artifact_missing",
        "outside_dir",
        "not_regular_file",
        "too_large",
        "contains_secret",
        "malformed",
        "hook_veto",
        "hook_escalate",
        "hook_timeout",
        "canceled",
        "never_adopted",
        "launch_interrupted",
        "config_invalid",
        "claude_not_found",
        "sandbox_unavailable",
        "socket_path_too_long",
        "invalid_permission_profile",
        "unsafe_cwd",
        "unsafe_path",
        "unsafe_plugin_dir",
        "unsafe_read_dir",
        "plugin_dir_missing",
        "git_dir_changed",
        "no_git_cwd",
        "fork_cwd_unsupported",
        "no_tty",
        "tmpdir_unsafe",
        "unsafe_claude_args",
        "claude_too_old",
    }
)
REDACTED = "[redacted]"
TRUNCATED = "[truncated]"
MESSAGE_MAX_CHARS = 4096
# Shorter values are not redacted: the same threshold as contains_secret (interface.md,
# report), so a short value cannot rewrite unrelated text.
MIN_SECRET_CHARS = 8

_CSESSION_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz234567")

type JSONValue = (
    str | int | float | bool | None | list[JSONValue] | tuple[JSONValue, ...] | dict[str, JSONValue]
)


def is_csession_id(value: object) -> bool:
    """Whether ``value`` is a full csession_id, ``^cs-[a-z2-7]{8}$`` (interface.md,
    Identifiers). Plain string checks, so the hook path does not need ``re``."""
    return (
        isinstance(value, str)
        and len(value) == 11
        and value.startswith("cs-")
        and set(value[3:]) <= _CSESSION_ID_CHARS
    )


def is_int(value: object) -> bool:
    """Whether ``value`` is an integer (a bool is not)."""
    return isinstance(value, int) and not isinstance(value, bool)


def secret_values(
    environ: Mapping[str, str],
    *,
    secret_env: Iterable[str] = (),
    claude_auth_env: Iterable[str] = (),
    run_secret: str | None = None,
) -> frozenset[str]:
    """The values both writers redact: the same set ``contains_secret`` checks (interface.md,
    report): the values of the ``secret_env`` and passed ``claude_auth_env`` variables present
    in ``environ``, plus the run secret when the caller has one; only values of
    ``MIN_SECRET_CHARS`` or more characters."""
    names = [*secret_env, *claude_auth_env]
    values = [environ.get(name, "") for name in names]
    if run_secret is not None:
        values.append(run_secret)
    return frozenset(v for v in values if len(v) >= MIN_SECRET_CHARS)


def usable_secrets(secrets: Iterable[str]) -> frozenset[str]:
    """``secrets`` without the values too short to redact (fewer than ``MIN_SECRET_CHARS``)."""
    return frozenset(s for s in secrets if len(s) >= MIN_SECRET_CHARS)


def _replace_secrets(text: str, secrets: Collection[str]) -> str:
    """One pass: every occurrence of a secret replaced by ``REDACTED``; overlapping or
    adjacent occurrences collapse into one marker."""
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


def leaks_secret(text: str, secrets: Collection[str]) -> bool:
    """Whether a secret occurs in ``text`` outside the ``REDACTED`` markers (a secret that is
    itself part of the marker, such as ``redacted``, is looked for between markers only)."""
    pieces = text.split(REDACTED)
    for secret in secrets:
        if secret in REDACTED:
            if any(secret in piece for piece in pieces):
                return True
        elif secret in text:
            return True
    return False


def redact_text(text: str, secrets: Collection[str]) -> str:
    """``text`` with every secret occurrence replaced by ``REDACTED``. If the result still
    contains a secret (one formed across a marker and the following text), the whole text
    becomes ``REDACTED``, so no secret survives and redaction always terminates.
    ``secrets`` must already be filtered with ``usable_secrets``."""
    result = _replace_secrets(text, secrets)
    if result is not text and leaks_secret(result, secrets):
        return REDACTED
    return result


def redact(value: JSONValue, secrets: Iterable[str]) -> JSONValue:
    """``value`` with secrets redacted from every string at any depth: list items, dictionary
    values and dictionary keys. Integers, floats, bools and None are typed ids and counters and
    are never redacted. Secrets shorter than ``MIN_SECRET_CHARS`` are ignored. Tuples become
    lists.

    Writers call this on text fields only; identifiers, enums and timestamps are validated, not
    redacted (interface.md, Audit and log).
    """
    usable = usable_secrets(secrets)
    return _redact(value, usable) if usable else value


def _redact(value: JSONValue, secrets: Collection[str]) -> JSONValue:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {redact_text(k, secrets): _redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(item, secrets) for item in value]
    return value


def timestamp(now: datetime | None = None) -> str:
    """UTC RFC 3339 timestamp with milliseconds, e.g. ``2026-10-30T12:00:00.123Z``. A naive
    ``now`` raises ``ValueError`` (it would be read as local time)."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timestamp needs a timezone-aware datetime")
    return now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_record(path: str, record: Mapping[str, JSONValue]) -> None:
    """Append the already validated and redacted ``record`` to the JSONL file ``path`` as one
    line (ASCII JSON, so control characters are always escaped and a record never spans lines;
    NaN and infinities raise ``ValueError``)."""
    line = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
    fsutil.append_private_line(path, line.encode("ascii"))


def _truncate(message: str) -> str:
    if len(message) <= MESSAGE_MAX_CHARS:
        return message
    return message[: MESSAGE_MAX_CHARS - len(TRUNCATED)] + TRUNCATED


def _string_list(name: str, value: Iterable[str]) -> list[str]:
    if isinstance(value, str | bytes | Mapping):
        raise ValueError(f"{name} must be a list of strings")
    items = list(value)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"{name} must be a list of strings")
    return items


class EventLog:
    """Writer for one ``log.jsonl``. Every record is written (there is no level threshold).

    ``secrets`` are the values redacted from the text fields (``signal``, ``decision_ids``,
    ``message``); build them with ``secret_values``. Values shorter than
    ``MIN_SECRET_CHARS`` are ignored.
    """

    # TODO(#3): callers build secrets with secret_values from config's secret_env and
    # claude_auth_env names (and the run secret where they have one).
    def __init__(self, path: str, *, secrets: Iterable[str] = ()) -> None:
        self.path = path
        self.secrets = usable_secrets(secrets)

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
    ) -> None:
        """Append one record. Unset (``None``) fields are omitted.

        Raises ``ValueError`` for a value the schema does not allow (unknown event, level or
        reason, a malformed csession_id, a non-integer run or pid, a naive ``now``) and
        ``OSError`` when the file cannot be written.
        """
        if event not in EVENTS:
            raise ValueError(f"unknown log event: {event!r}")
        if level not in LEVELS:
            raise ValueError(f"unknown log level: {level!r}")
        if reason is not None and reason not in REASONS:
            raise ValueError(f"unknown log reason: {reason!r}")
        if csession_id is not None and not is_csession_id(csession_id):
            raise ValueError(f"malformed csession_id: {csession_id!r}")
        for name, number in (("run", run), ("pid", pid)):
            if number is not None and not is_int(number):
                raise ValueError(f"{name} must be an integer")
        if signal is not None and not isinstance(signal, str):
            raise ValueError("signal must be a string")
        if message is not None and not isinstance(message, str):
            raise ValueError("message must be a string")
        ids = None if decision_ids is None else _string_list("decision_ids", decision_ids)
        record: dict[str, JSONValue] = {"ts": timestamp(now), "level": level, "event": event}
        optional: dict[str, JSONValue] = {
            "csession_id": csession_id,
            "run": run,
            "pid": pid,
            "signal": None if signal is None else redact_text(signal, self.secrets),
            "decision_ids": None if ids is None else [redact_text(i, self.secrets) for i in ids],
            "reason": reason,
        }
        record.update((key, value) for key, value in optional.items() if value is not None)
        if message is not None:
            # Redact before truncating, so a cut can never leave part of a secret behind; the
            # truncation marker could complete a secret, so check once more.
            text = _truncate(redact_text(message, self.secrets))
            record["message"] = REDACTED if leaks_secret(text, self.secrets) else text
        append_record(self.path, record)
