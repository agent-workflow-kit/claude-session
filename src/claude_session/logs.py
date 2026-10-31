"""``log.jsonl`` writer (interface.md, Audit and log; schemas/log.json).

Standard library only and import-light: ``__main__`` logs contained ``hook-event`` errors
through it (design.md, Hook-event fast path), so it imports no third-party modules (and no
direct ``re`` import; ``json`` loads ``re`` itself). Text fields are redacted against the
secret values before a record is written; identifiers, enums and timestamps are validated
instead. Records are appended with one ``os.write`` each.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Iterator, Mapping
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
        "session_start_source_unknown",
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
        "session_dir_unsafe",
        "unsafe_claude_args",
        "unsafe_setting_sources",
        "claude_too_old",
    }
)
REDACTED = "[redacted]"
TRUNCATED = "[truncated]"
MESSAGE_MAX_CHARS = 4096
# Shorter values are not redacted: the same threshold as contains_secret (interface.md,
# report), so a short value cannot rewrite unrelated text.
MIN_SECRET_CHARS = 8

# interface.md, Identifiers; schemas/log.json and schemas/audit.json use the same pattern.
# A plain string: is_csession_id checks it without ``re``.
CSESSION_ID_PATTERN = "^cs-[a-z2-7]{8}$"
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


def _failure(secret: str) -> list[int]:
    """The KMP prefix function of ``secret``: entry ``i`` is the length of the longest strict
    prefix of ``secret[: i + 1]`` that is also its suffix."""
    failure = [0] * len(secret)
    k = 0
    for i in range(1, len(secret)):
        while k and secret[i] != secret[k]:
            k = failure[k - 1]
        if secret[i] == secret[k]:
            k += 1
        failure[i] = k
    return failure


def _occurrences(text: str, secret: str) -> Iterator[int]:
    """The start of every occurrence of ``secret`` in ``text``, overlapping ones included, in
    increasing order, in time linear in ``len(text) + len(secret)``.

    KMP, except that while no partial match is in progress the next occurrence is found with
    ``str.find`` (an occurrence never starts before the current position then); the
    character-by-character scan runs only inside a match of a secret that overlaps itself.
    An empty ``secret`` has no occurrences."""
    if not secret:
        return
    size = len(secret)
    failure = _failure(secret)
    border = failure[-1]  # how much of a match the next overlapping occurrence can reuse
    pos, matched = 0, 0
    while pos < len(text):
        if not matched:
            start = text.find(secret, pos)
            if start == -1:
                return
            yield start
            pos, matched = start + size, border
            continue
        char = text[pos]
        while matched and char != secret[matched]:
            matched = failure[matched - 1]
        if char == secret[matched]:
            matched += 1
        pos += 1
        if matched == size:
            yield pos - size
            matched = border


def _secret_mask(text: str, secrets: Collection[str]) -> bytearray:
    """One byte per character of ``text``: 1 where a secret occurs. Occurrences (overlapping
    ones included) are found in linear time, and each character is marked at most once per
    secret, so the work is ``O(len(text) + len(secret))`` per secret."""
    mask = bytearray(len(text))
    for secret in secrets:
        covered = 0  # characters before this index are already marked for this secret
        for start in _occurrences(text, secret):
            end = start + len(secret)
            begin = max(start, covered)
            mask[begin:end] = b"\x01" * (end - begin)
            covered = end
    return mask


def first_secret_index(text: str, secrets: Collection[str]) -> int:
    """The index where the first secret occurrence in ``text`` starts, or -1."""
    return _secret_mask(text, secrets).find(1)


def _apply_mask(text: str, mask: bytearray, end: int) -> str:
    """``text[:end]`` with every marked run replaced by one ``REDACTED`` (a run that crosses
    ``end`` still becomes a marker)."""
    parts: list[str] = []
    pos = 0
    while pos < end:
        start = mask.find(1, pos, end)
        if start == -1:
            parts.append(text[pos:end])
            break
        parts.append(text[pos:start])
        parts.append(REDACTED)
        stop = mask.find(0, start)
        pos = len(text) if stop == -1 else stop
    return "".join(parts)


def _replace_secrets(text: str, secrets: Collection[str], end: int | None = None) -> str:
    """One pass over ``text[:end]``: every occurrence of a secret (found in the whole ``text``)
    replaced by ``REDACTED``; overlapping or adjacent occurrences collapse into one marker."""
    mask = _secret_mask(text, secrets)
    end = len(text) if end is None else end
    if 1 not in mask:
        return text[:end]
    return _apply_mask(text, mask, end)


def leaks_secret(text: str, secrets: Collection[str]) -> bool:
    """Whether a secret occurs in ``text`` outside the ``REDACTED`` markers (a secret that is
    itself part of the marker, such as ``redacted``, is looked for between markers only)."""
    pieces = text.split(REDACTED)
    for secret in secrets:
        if not secret:
            continue
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
    if result != text and leaks_secret(result, secrets):
        return REDACTED
    return result


def _prefix_at_end(text: str, secret: str) -> int:
    """The length of the longest strict prefix of ``secret`` that ``text`` ends with (KMP,
    linear in ``len(secret)``)."""
    failure = _failure(secret)
    matched = 0
    for char in text[-(len(secret) - 1) :] if len(secret) > 1 else "":
        while matched and char != secret[matched]:
            matched = failure[matched - 1]
        if char == secret[matched]:
            matched += 1
        if matched == len(secret):  # a whole occurrence: keep only its longest strict border
            matched = failure[matched - 1]
    return matched


def _end_before_secret_prefix(text: str, secrets: Collection[str]) -> int:
    """The length of ``text`` without its longest tail that is a strict prefix of a secret
    (a secret cut in half at the end of ``text``)."""
    return len(text) - max((_prefix_at_end(text, s) for s in secrets), default=0)


def redact_message(message: str, secrets: Collection[str]) -> str:
    """``message`` redacted and truncated to ``MESSAGE_MAX_CHARS``, with bounded work.

    The message is first cut to ``2 * MESSAGE_MAX_CHARS`` plus the longest secret, and any
    tail of that cut which is the start of a secret is dropped, so no part of a secret
    survives the cut even when earlier redactions shorten the text. Redaction happens before
    the final cut, which never splits a ``REDACTED`` marker. When the whole message is
    replaced (a secret formed across a marker, or completed by the ``TRUNCATED`` marker) the
    result is exactly ``REDACTED``, without ``TRUNCATED``. ``secrets`` must already be
    filtered with ``usable_secrets``.
    """
    limit = 2 * MESSAGE_MAX_CHARS + max(map(len, secrets), default=0)
    precut = len(message) > limit
    if precut:
        text = message[:limit]
        result = _replace_secrets(text, secrets, _end_before_secret_prefix(text, secrets))
        if leaks_secret(result, secrets):
            return REDACTED  # the whole field is replaced: nothing was kept to truncate
    else:
        result = redact_text(message, secrets)
    result = _truncate(result, force=precut)
    return REDACTED if leaks_secret(result, secrets) else result


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


def _truncate(message: str, *, force: bool = False) -> str:
    """``message`` cut to ``MESSAGE_MAX_CHARS`` with a final ``TRUNCATED`` (also when
    ``force``, for a message cut earlier). A cut inside a ``REDACTED`` marker moves back to
    the marker's start."""
    if len(message) <= MESSAGE_MAX_CHARS and not force:
        return message
    cut = min(len(message), MESSAGE_MAX_CHARS - len(TRUNCATED))
    inside = message.find(REDACTED, max(0, cut - len(REDACTED) + 1), cut + len(REDACTED) - 1)
    if inside != -1 and inside < cut:
        cut = inside
    return message[:cut] + TRUNCATED


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

    # Callers pass the values of the configured secret_env and claude_auth_env variables
    # (envbuild.secret_values; exec's launch and supervisor do) and the run secret where they
    # have one.
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
        count: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """Append one record. Unset (``None``) fields are omitted.

        Raises ``ValueError`` for a value the schema does not allow (unknown event, level or
        reason, a malformed csession_id, a non-integer run or pid, a naive ``now``) and
        ``OSError`` when the file cannot be written.
        """
        if event not in EVENTS:
            raise ValueError("unknown log event")
        if level not in LEVELS:
            raise ValueError("unknown log level")
        if reason is not None and reason not in REASONS:
            raise ValueError("unknown log reason")
        if csession_id is not None and not is_csession_id(csession_id):
            raise ValueError("malformed csession_id")
        for name, number in (("run", run), ("pid", pid), ("count", count)):
            if number is not None and not is_int(number):
                raise ValueError(f"{name} must be an integer")
        if count is not None and count < 1:
            raise ValueError("count must be at least 1")
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
            "count": count,
        }
        record.update((key, value) for key, value in optional.items() if value is not None)
        if message is not None:
            record["message"] = redact_message(message, self.secrets)
        append_record(self.path, record)
