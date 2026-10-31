"""``export`` (interface.md, export): the records of ``audit.jsonl``, oldest first, filtered by
``--since`` and ``--action``.

The file is opened relative to the state directory, walked as the writer walks it
(``fsutil.open_dir_nofollow`` after one ``realpath``), with ``O_NOFOLLOW`` and ``O_NONBLOCK``,
and refused unless it is a regular file with one link owned by the user, the checks
``fsutil.append_private_line`` makes before it appends. Only the bytes present when it is
opened are read (a busy writer cannot keep export reading), without taking the writers' lock
(a long export must not make an audited action time out waiting for it). A line is read only
up to ``MAX_LINE_BYTES``; a longer one is discarded as it streams past.

Every line that is not a record schemas/audit.json allows (a torn or half-written line, a
line edited by hand, anything ``audit.parse_record`` refuses) is skipped and counted, never
shown. Records are printed as written: the writer redacted their text fields, and export adds
no value of its own (no environment, no config).
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import audit, fsutil
from .errors import FailedError
from .terminal import display, no_surrogates

AUDIT_NAME = "audit.jsonl"
# Longest line read (newline excluded). Every record the writer produces is far shorter:
# actor and decision ids come from argv, whose total is bounded by ARG_MAX.
MAX_LINE_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# Characters escaped in a human field value besides display()'s own: the space separates
# fields and the comma list items, so no value can pass for another field or item.
_FIELD_ESCAPED = " ,"


@dataclass(frozen=True)
class AuditRead:
    """The valid records of one read, in file order, and the number of lines skipped."""

    records: list[dict[str, Any]]
    skipped: int


def read_records(path: str) -> AuditRead:
    """Every record of the audit file ``path`` (absolute), in file order. A missing file (or
    state directory) has none. Raises ``FailedError`` for a file that is not safe to read or
    cannot be read."""
    parent, name = os.path.split(path)
    try:
        dir_fd = fsutil.open_dir_nofollow(os.path.realpath(parent))
    except FileNotFoundError:
        return AuditRead([], 0)
    except OSError as exc:
        raise _failed(path, exc) from None
    try:
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        except FileNotFoundError:
            return AuditRead([], 0)
        except OSError as exc:
            raise _failed(path, exc) from None
    finally:
        os.close(dir_fd)
    try:
        size = _check_file(fd, path)
        records: list[dict[str, Any]] = []
        skipped = 0
        for line in _lines(fd, size, path):
            record = None if line is None else audit.parse_record(line)
            if record is None:
                skipped += 1
            else:
                records.append(record)
        return AuditRead(records, skipped)
    finally:
        os.close(fd)


def _check_file(fd: int, path: str) -> int:
    """The size of the opened audit file; ``FailedError`` unless it is a regular file with
    one link owned by the user."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise FailedError(f"cannot read {path}: not a regular file")
    if info.st_nlink != 1:
        raise FailedError(f"cannot read {path}: file has {info.st_nlink} links")
    if info.st_uid != os.getuid():
        raise FailedError(f"cannot read {path}: owned by uid {info.st_uid}")
    return info.st_size


def _failed(path: str, exc: OSError) -> FailedError:
    if exc.errno == errno.ELOOP:
        return FailedError(f"cannot read {path}: a symlink")
    reason = exc.strerror or type(exc).__name__
    return FailedError(f"cannot read {path}: {reason}")


def _lines(fd: int, size: int, path: str) -> Iterator[bytes | None]:
    """The lines of the first ``size`` bytes of ``fd`` (newline excluded; a last line
    without one included), each ``None`` when longer than ``MAX_LINE_BYTES``. At most
    ``MAX_LINE_BYTES`` plus one chunk is held at once."""
    pending = bytearray()
    overlong = False
    remaining = size
    while remaining > 0:
        try:
            chunk = os.read(fd, min(CHUNK_BYTES, remaining))
        except OSError as exc:
            raise _failed(path, exc) from None
        if not chunk:
            break  # the file shrank: only a hand edit does that; read what there was
        remaining -= len(chunk)
        start = 0
        while (end := chunk.find(b"\n", start)) != -1:
            if not overlong:
                pending += chunk[start:end]
            yield None if overlong or len(pending) > MAX_LINE_BYTES else bytes(pending)
            pending.clear()
            overlong = False
            start = end + 1
        if not overlong:
            pending += chunk[start:]
            if len(pending) > MAX_LINE_BYTES:
                pending.clear()
                overlong = True
    if overlong or pending:
        yield None if overlong else bytes(pending)


def select(
    records: list[dict[str, Any]], *, since: datetime | None, actions: Collection[str]
) -> list[dict[str, Any]]:
    """``records`` at or after ``since`` and, when ``actions`` is not empty, with one of
    them; oldest first by ``ts`` (a stable sort: records of the same instant keep file
    order, also when a clock stepped back between writes)."""
    timed = [(audit.parse_ts(r["ts"]), r) for r in records]
    chosen = [
        (when, r)
        for when, r in timed
        if (since is None or when >= since) and (not actions or r["action"] in actions)
    ]
    chosen.sort(key=lambda pair: pair[0])
    return [r for _, r in chosen]


def json_record(record: dict[str, Any]) -> dict[str, Any]:
    """``record`` for ``--json`` output: every lone surrogate (an argv byte that was not
    UTF-8) replaced by U+FFFD, so the output encodes as strict UTF-8."""
    return _no_surrogates(record)


def _no_surrogates(value: Any) -> Any:
    if isinstance(value, str):
        return no_surrogates(value)
    if isinstance(value, list):
        return [_no_surrogates(item) for item in value]
    if isinstance(value, dict):
        return {key: _no_surrogates(item) for key, item in value.items()}
    return value


def record_line(record: dict[str, Any]) -> str:
    """One line of human output: ``<ts> <action> <target> <result>``, then ``actor=``,
    ``decision_id=`` and one ``<key>=<value>`` per input present, in the writer's key order.
    Every value goes through ``display`` with spaces and commas escaped, so each field is one
    inert token; a list is joined with commas, null and an empty list are ``-``."""
    fields = [
        display(record["ts"], _FIELD_ESCAPED),
        display(record["action"], _FIELD_ESCAPED),
        display(record["target"], _FIELD_ESCAPED),
        display(record["result"], _FIELD_ESCAPED),
        f"actor={_value(record['actor'])}",
        f"decision_id={_value(record['decision_id'])}",
    ]
    inputs = record["inputs"] or {}
    fields += [f"{display(key, _FIELD_ESCAPED)}={_value(value)}" for key, value in inputs.items()]
    return " ".join(fields)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(_value(item) for item in value) or "-"
    return display(value, _FIELD_ESCAPED)
