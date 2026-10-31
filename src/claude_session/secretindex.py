"""``contains_secret``: the per-run keyed q-gram index of the secret values (design.md, Outbox
and ingestion; interface.md, ``report``).

``exec`` builds the index at launch from the run's secret values (``envbuild.secret_values``:
the passed ``claude_auth_env`` values, the ``secret_env`` values of its own environment and the
run secret, 8 characters or more) and writes it to ``private/<id>/runs/<n>/secret-index``
(mode 0600). It never holds plaintext: a fresh random key, then for each value its length and
a keyed BLAKE2b hash of the whole value, and a keyed BLAKE2b hash (8 bytes) of every 8-byte
q-gram of every value with the value and offset it comes from. The key sits in the file: the
file's protection is the private directory, which the agent can neither read nor write; the
hashes only keep the values out of plaintext (a reader with the file could still brute-force
short q-grams, so it must stay private).

Ingestion (the hook path) reads the index and scans the summary, the reason and the artifact
copy: q-grams are sampled at absolute offsets that are multiples of the stride
``min(8, L - 7)`` (L: the shortest recorded length), so every occurrence of every value
contains at least one sampled q-gram; a sampled q-gram found in the index names candidate
windows (value, start), each confirmed by hashing the whole window. Scanning is streamed: the
artifact copy is fed chunk by chunk and a value split across chunks is still found.

Only literal values are caught; a value transformed in any way (letter case, encodings,
inserted characters, splitting across lines) passes (interface.md, Residual risks). The run
secret alone is also refused in any letter case, by ``outbox`` itself.

Standard library only, plus ``fsutil``: ``hook-event`` imports this module.

TODO(#15): exec writes the index at launch (``write_at`` in the private run directory, values
from ``envbuild.secret_values``), gives ``read_at`` to its final drain and salvage, and checks
an implicit-done ``result.md`` with ``SecretIndex.contains`` before accepting it
(``result_rejected`` on a hit).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import fsutil

INDEX_NAME = "secret-index"
MAGIC = b"csidx-1\n"
KEY_BYTES = 32
Q = 8
# Values shorter than this are not secrets to match (logs.MIN_SECRET_CHARS, tested equal).
MIN_SECRET_CHARS = 8
GRAM_DIGEST = 8
VALUE_DIGEST = 32
VALUE_ENTRY = 4 + VALUE_DIGEST  # length, keyed hash of the whole value
GRAM_ENTRY = GRAM_DIGEST + 4 + 4  # keyed hash of the q-gram, value number, offset
# A Linux environment holds a few MiB at most; anything larger is not an index exec wrote.
INDEX_MAX_BYTES = 64 * 1024 * 1024
# Seconds the scan of an artifact at the artifact cap (10 MiB) may take: a third of the
# drain budget (outbox.DRAIN_BUDGET, half of the 60 s PostToolUse timeout). Measured (tests,
# one core of a desktop CPU): about 0.6 s at stride 8, 5 s at stride 1 (an 8-byte value).
SCAN_BUDGET = 10.0
_GRAM_PERSON = b"cs-qgram"
_VALUE_PERSON = b"cs-value"
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class SecretIndexError(Exception):
    """The secret index is not one ``exec`` wrote: the drain fails and nothing is ingested."""


def _value_hash(key: bytes, data: bytes | memoryview) -> bytes:
    return hashlib.blake2b(data, key=key, digest_size=VALUE_DIGEST, person=_VALUE_PERSON).digest()


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "little")


def build(values: Iterable[str], *, key: bytes | None = None) -> bytes:
    """The index file's content for ``values`` (the environment bytes of each value of
    ``MIN_SECRET_CHARS`` or more characters, duplicates once) under ``key`` (fresh random
    bytes when None). Raises ``ValueError`` when no value is left (exec always indexes the run
    secret) or the index would be over ``INDEX_MAX_BYTES``."""
    if key is None:
        key = os.urandom(KEY_BYTES)
    if len(key) != KEY_BYTES:
        raise ValueError(f"the index key must be {KEY_BYTES} bytes")
    raws = sorted({os.fsencode(v) for v in values if len(v) >= MIN_SECRET_CHARS})
    if not raws:
        raise ValueError("no secret value to index")
    grams = sum(len(raw) - Q + 1 for raw in raws)
    size = len(MAGIC) + KEY_BYTES + 4 + len(raws) * VALUE_ENTRY + 4 + grams * GRAM_ENTRY
    if size > INDEX_MAX_BYTES:
        raise ValueError("the secret values make the index too large")
    parts = [MAGIC, key, _u32(len(raws))]
    for raw in raws:
        parts += [_u32(len(raw)), _value_hash(key, raw)]
    parts.append(_u32(grams))
    gram_hash = hashlib.blake2b(key=key, digest_size=GRAM_DIGEST, person=_GRAM_PERSON)
    for number, raw in enumerate(raws):
        for offset in range(len(raw) - Q + 1):
            h = gram_hash.copy()
            h.update(raw[offset : offset + Q])
            parts += [h.digest(), _u32(number), _u32(offset)]
    return b"".join(parts)


def write_at(run_fd: int, values: Iterable[str]) -> None:
    """Write ``secret-index`` (mode 0600, atomically) into the private run directory."""
    fsutil.write_private_file_at(run_fd, INDEX_NAME, build(values))


def read_at(run_fd: int) -> SecretIndex:
    """Read ``secret-index`` from the private run directory ``run_fd``: a regular single-link
    file of the user's own that no one else can access, at most ``INDEX_MAX_BYTES``. Raises
    ``OSError`` (``FileNotFoundError`` when missing) or ``SecretIndexError``."""
    fd = os.open(INDEX_NAME, _FILE_FLAGS, dir_fd=run_fd)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > INDEX_MAX_BYTES
        ):
            raise SecretIndexError("secret-index is not a private regular file")
        chunks = []
        remaining = info.st_size + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) != info.st_size:
        raise SecretIndexError("secret-index changed while it was read")
    return parse(data)


def parse(data: bytes) -> SecretIndex:
    """Validate an index file's content. Every q-gram of every value must be listed exactly
    once (the stride relies on it)."""
    view = memoryview(data)
    at = len(MAGIC)
    if bytes(view[:at]) != MAGIC:
        raise SecretIndexError("secret-index has an unknown format")

    def take(n: int) -> memoryview:
        nonlocal at
        if at + n > len(view):
            raise SecretIndexError("secret-index is truncated")
        piece = view[at : at + n]
        at += n
        return piece

    def u32() -> int:
        return int.from_bytes(take(4), "little")

    key = bytes(take(KEY_BYTES))
    count = u32()
    if count == 0:
        raise SecretIndexError("secret-index has no values")
    lengths: list[int] = []
    digests: list[bytes] = []
    for _ in range(count):
        length = u32()
        if length < Q:
            raise SecretIndexError("secret-index has a value shorter than a q-gram")
        lengths.append(length)
        digests.append(bytes(take(VALUE_DIGEST)))
    grams = u32()
    if grams != sum(length - Q + 1 for length in lengths):
        raise SecretIndexError("secret-index does not list every q-gram")
    if len(view) - at != grams * GRAM_ENTRY:
        raise SecretIndexError("secret-index has the wrong size")
    table: dict[bytes, list[tuple[int, int]]] = {}
    seen: set[tuple[int, int]] = set()
    for _ in range(grams):
        digest = bytes(take(GRAM_DIGEST))
        number, offset = u32(), u32()
        if number >= count or offset > lengths[number] - Q or (number, offset) in seen:
            raise SecretIndexError("secret-index has an invalid q-gram entry")
        seen.add((number, offset))
        table.setdefault(digest, []).append((number, offset))
    return SecretIndex(
        key=key,
        lengths_by_value=tuple(lengths),
        digests=tuple(digests),
        grams={digest: tuple(entries) for digest, entries in table.items()},
    )


@dataclass(frozen=True)
class SecretIndex:
    """A loaded index. ``scan()`` starts a streamed scan; ``contains(data)`` scans one blob."""

    key: bytes = field(repr=False)
    lengths_by_value: tuple[int, ...]
    digests: tuple[bytes, ...] = field(repr=False)
    grams: dict[bytes, tuple[tuple[int, int], ...]] = field(repr=False)

    @property
    def lengths(self) -> tuple[int, ...]:
        """The distinct recorded value lengths (bytes), ascending."""
        return tuple(sorted(set(self.lengths_by_value)))

    @property
    def stride(self) -> int:
        return min(Q, min(self.lengths_by_value) - Q + 1)

    def scan(self) -> Scan:
        return Scan(self)

    def contains(self, data: bytes) -> bool:
        return self.scan().update(data)


class Scan:
    """One streamed scan: feed the bytes in order with ``update``; it returns True once a
    recorded value has occurred anywhere in the bytes fed so far (across chunk boundaries).

    State: ``_buf`` holds the bytes from absolute offset ``_base`` on; ``_next`` is the next
    absolute offset to sample; ``_pending`` are candidate windows ``(start, value)`` whose end
    has not arrived yet. Only bytes a pending or future window can need are kept: a window
    found at sample ``p`` starts no earlier than ``p - (longest - Q)``."""

    def __init__(self, index: SecretIndex) -> None:
        self._index = index
        self._stride = index.stride
        self._reach = max(index.lengths_by_value) - Q
        self._gram_hash = hashlib.blake2b(
            key=index.key, digest_size=GRAM_DIGEST, person=_GRAM_PERSON
        )
        self._buf = b""
        self._base = 0
        self._next = 0
        self._pending: list[tuple[int, int]] = []
        self.found = False

    @property
    def retained(self) -> int:
        """Bytes kept between updates (bounded by the longest value plus one chunk)."""
        return len(self._buf)

    def _confirm(self, buf: bytes, start: int, number: int) -> bool:
        at = start - self._base
        window = memoryview(buf)[at : at + self._index.lengths_by_value[number]]
        return hmac.compare_digest(
            _value_hash(self._index.key, window), self._index.digests[number]
        )

    def update(self, data: bytes) -> bool:
        if self.found or not data:
            return self.found
        buf = self._buf + data
        base = self._base
        end = base + len(buf)
        lengths = self._index.lengths_by_value
        waiting: list[tuple[int, int]] = []
        for start, number in self._pending:
            if start + lengths[number] > end:
                waiting.append((start, number))
            elif self._confirm(buf, start, number):
                self.found = True
                return True
        lookup = self._index.grams.get
        copy = self._gram_hash.copy
        position = self._next
        stride = self._stride
        while position + Q <= end:
            at = position - base
            h = copy()
            h.update(buf[at : at + Q])
            hits = lookup(h.digest())
            if hits is not None:
                for number, offset in hits:
                    start = position - offset
                    if start < 0:
                        continue
                    if start + lengths[number] > end:
                        waiting.append((start, number))
                    elif self._confirm(buf, start, number):
                        self.found = True
                        return True
            position += stride
        self._next = position
        self._pending = waiting
        keep = max(base, position - self._reach)
        if waiting:
            keep = min(keep, min(start for start, _ in waiting))
        self._buf = buf[keep - base :]
        self._base = keep
        return False
