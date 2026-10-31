"""``export`` (interface.md, export): the records of ``audit.jsonl``, oldest first, filtered by
``--since`` and ``--action`` (applied while reading, so only matching records are held).

The file is opened relative to the state directory, walked as the writer walks it
(``fsutil.open_dir_nofollow`` after one ``realpath``), with ``O_NOFOLLOW`` and ``O_NONBLOCK``,
and refused unless it is a regular file with one link owned by the user, the checks
``fsutil.append_private_line`` makes before it appends. Only the bytes present when it is
opened are read (a busy writer cannot keep export reading), without taking the writers' lock
(a long export must not make an audited action time out waiting for it). A line is read only
up to ``MAX_LINE_BYTES``, the writer's own limit; a longer one is discarded as it streams past.

Every line that is not a record schemas/audit.json allows (a torn line, a line edited by
hand, anything ``audit.parse_record`` refuses) is skipped and counted, never shown. A final
line without its newline that is not a record is neither shown nor counted: it may be an
append in progress (the writer terminates a torn tail before its next append, and from then
on it is counted). Records are printed as written: the writer redacted their text fields, and
export adds no value of its own (no environment, no config).
"""

from __future__ import annotations

import errno
import os
import stat
import unicodedata
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import audit, fsutil
from .errors import FailedError
from .terminal import display, no_surrogates

AUDIT_NAME = "audit.jsonl"
# Longest line read (newline excluded): the longest record the writer writes
# (audit.MAX_RECORD_BYTES, newline included), so every record it wrote is read.
MAX_LINE_BYTES = audit.MAX_RECORD_BYTES - 1
CHUNK_BYTES = 64 * 1024
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# Characters escaped in a human field value besides display()'s own (and every whitespace
# character): the space separates fields, the comma list items and the double quote spells
# the empty string, so no value can pass for another field, item or value.
_FIELD_ESCAPED = ' ,"'
_SPACE_CATEGORIES = frozenset({"Zs", "Zl", "Zp"})


@dataclass(frozen=True)
class AuditRead:
    """The matching records of one read, oldest first, and the number of lines skipped."""

    records: list[dict[str, Any]]
    skipped: int


def read_records(
    path: str, *, since: datetime | None = None, actions: Collection[str] = ()
) -> AuditRead:
    """The records of the audit file ``path`` (absolute) at or after ``since`` and, when
    ``actions`` is not empty, with one of them, oldest first by ``ts`` (a stable sort: records
    of the same instant keep file order, also when a clock stepped back between writes). The
    filter is applied while reading: only matching records are held. A missing file (or state
    directory) has none. Raises ``FailedError`` for a file that is not safe to read or cannot
    be read."""
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
        chosen: list[tuple[datetime, dict[str, Any]]] = []
        skipped = 0
        for line, terminated in _lines(fd, size, path):
            record = None if line is None else audit.parse_record(line)
            if record is None:
                skipped += terminated  # an unterminated last line may be an append in progress
                continue
            when = audit.parse_ts(record["ts"])
            if (since is None or when >= since) and (not actions or record["action"] in actions):
                chosen.append((when, record))
    finally:
        os.close(fd)
    chosen.sort(key=lambda pair: pair[0])
    return AuditRead([record for _, record in chosen], skipped)


def _check_file(fd: int, path: str) -> int:
    """The size of the opened audit file; ``FailedError`` unless it is a regular file with
    one link owned by the user."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _unsafe(path, "not a regular file")
    if info.st_nlink != 1:
        raise _unsafe(path, f"file has {info.st_nlink} links")
    if info.st_uid != os.getuid():
        raise _unsafe(path, f"owned by uid {info.st_uid}")
    return info.st_size


def _unsafe(path: str, cause: str) -> FailedError:
    return FailedError("unsafe audit file", detail=f"{path}: {cause}")


def _failed(path: str, exc: OSError) -> FailedError:
    if exc.errno == errno.ELOOP:
        return _unsafe(path, "a symlink")
    cause = exc.strerror or type(exc).__name__
    return FailedError("cannot read the audit file", detail=f"{path}: {cause}")


def _lines(fd: int, size: int, path: str) -> Iterator[tuple[bytes | None, bool]]:
    """The lines of the first ``size`` bytes of ``fd`` (newline excluded; a last line
    without one included), each ``None`` when longer than ``MAX_LINE_BYTES``, and whether it
    ended with a newline. At most ``MAX_LINE_BYTES`` plus one chunk is held at once."""
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
            yield None if overlong or len(pending) > MAX_LINE_BYTES else bytes(pending), True
            pending.clear()
            overlong = False
            start = end + 1
        if not overlong:
            pending += chunk[start:]
            if len(pending) > MAX_LINE_BYTES:
                pending.clear()
                overlong = True
    if overlong or pending:
        yield None if overlong else bytes(pending), False


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
    Every value goes through ``display`` with spaces, commas, double quotes and every other
    whitespace character escaped, so each field is one inert token. A list is joined with
    commas; null and an empty list are ``-``, the string ``-`` is ``\\x2d`` and the empty
    string ``""``, so no two values look alike."""
    fields = [
        _text(record["ts"]),
        _text(record["action"]),
        _text(record["target"]),
        _text(record["result"]),
        f"actor={_value(record['actor'])}",
        f"decision_id={_value(record['decision_id'])}",
    ]
    inputs = record["inputs"] or {}
    fields += [f"{_text(key)}={_value(value)}" for key, value in inputs.items()]
    return " ".join(fields)


def _value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(_value(item) for item in value) or "-"
    if isinstance(value, str):
        return _text(value)
    return display(value)  # a number


def _text(text: str) -> str:
    """A string field value: ``display`` (whose escapes are ASCII without whitespace, so the
    second pass leaves them alone), then the field separators and every whitespace character
    escaped; ``-`` and the empty string spelled so they cannot pass for null or an empty
    list."""
    if text == "":
        return '""'
    if text == "-":
        return "\\x2d"
    return "".join(_escape_field_char(char) for char in display(text))


def _escape_field_char(char: str) -> str:
    if char in _FIELD_ESCAPED or char.isspace() or unicodedata.category(char) in _SPACE_CATEGORIES:
        code = ord(char)
        return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"
    return char


def encodable(text: str, encoding: str | None) -> str:
    """``text`` with every character ``encoding`` (stdout's) cannot encode written as a
    backslash escape, so a non-UTF-8 terminal gets an escape, not a crash."""
    if not encoding:
        return text
    return text.encode(encoding, "backslashreplace").decode(encoding)
