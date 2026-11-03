"""``contains_secret``: the per-run keyed q-gram index of the secret values (design.md, Outbox
and ingestion; interface.md, ``report``).

``exec`` builds the index at launch from the run's secret values (``envbuild.secret_values``:
the passed ``claude_auth_env`` values, the ``secret_env`` values of its own environment and the
run secret, 8 characters or more) and writes it to ``private/<id>/runs/<n>/secret-index``
(mode 0600). It holds a fresh random key, then for each value its length and a keyed BLAKE2b
hash of the whole value, and a keyed BLAKE2b hash (8 bytes) of every 8-byte q-gram of every
value with the value and offset it comes from.

The index is secret-equivalent: the key sits in the file, and with the key, the offsets and
the 7-byte overlap of consecutive q-grams a reader recovers every value one byte at a time.
Its only protection is the private directory (0700, which the agent can neither read nor
write) and ``read_at``'s owner-only file checks; it must never be readable by the agent.

Ingestion (the hook path) reads the index and scans the summary, the reason, the artifact
name and the artifact copy. q-grams are sampled at absolute offsets that are multiples of the
stride ``min(8, L - 7)`` (L: the shortest recorded length), so every occurrence of every
value contains a sampled q-gram, and the first sampled q-gram of an occurrence lies at an
offset below the stride in its value. A sampled q-gram is therefore looked up among the
*anchors* only (the q-grams at offsets below the stride), which names every candidate window
(value, start) exactly once, at most stride x values candidates per sample whatever the
values repeat. A candidate is prefiltered by hashing one more of its q-grams (the value's
last) and confirmed by hashing the whole window only when that matches. Scanning is
streamed: the artifact copy is fed chunk by chunk and a value split across chunks is still
found. A ``check`` callable given to ``scan`` is called at a bounded interval of work, so a
caller's deadline (it raises) can stop a scan inside one chunk.

Only literal values are caught; a value transformed in any way (letter case, encodings,
inserted characters, splitting across lines) passes (interface.md, Residual risks). The run
secret alone is also refused in any letter case, by ``outbox`` itself.

Standard library only, plus ``fsutil``: ``hook-event`` imports this module.

``exec`` writes the index at launch step 7 (``launch.prepare``: ``build`` over
``envbuild.secret_values``, written into the private run directory; ``build``'s
``ValueError`` for a set over ``VALUES_MAX_BYTES`` fails the launch, ``config_invalid``),
before the agent can file a report; its final drain reads it (``hookevent.final_drain``),
it checks an implicit-done ``result.md`` with ``SecretIndex.contains`` (``result_rejected``
on a hit) and deletes ``secret-index`` at run end (``supervisor.finish``; the file is
secret-equivalent).
TODO(#33): salvage of a run whose wrapper is lost drains with the index, then deletes it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import struct
from collections.abc import Callable, Iterable
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
_GRAM = struct.Struct(f"<{GRAM_DIGEST}sII")
_HEADER = len(MAGIC) + KEY_BYTES + 4 + 4  # magic, key, value count, q-gram count
# The secret values of a run may total at most this many bytes (as environment bytes): a
# Linux environment holds a few MiB at most, and real credentials (tokens, PEM keys and
# bundles, service-account JSON) are a few KiB each. It bounds the index (16 bytes per
# q-gram) so the hook path parses it in well under a second (measured: about 0.2 s).
VALUES_MAX_BYTES = 256 * 1024
# The largest index ``build`` can write: anything larger is not an index exec wrote.
INDEX_MAX_BYTES = _HEADER + GRAM_ENTRY * VALUES_MAX_BYTES
# Seconds the scan of an artifact at the artifact cap (10 MiB) should take with realistic
# values: a third of the drain budget (outbox.DRAIN_BUDGET, half of the 60 s PostToolUse
# timeout). Measured (one core of a desktop CPU): about 0.6 s at stride 8, 5 s at stride 1
# (an 8-byte value). Pathological values (self-periodic ones such as "a" * 2000) can take
# longer on matching data; the caller's deadline (``check``) bounds every scan.
SCAN_BUDGET = 10.0
# Work units between two ``check`` calls: one per sampled q-gram and per waiting candidate
# window revisited, CONFIRM_COST per candidate prefilter (plus a unit per 512 bytes of a
# full-window hash). About a millisecond of work.
CHECK_EVERY = 4096
CONFIRM_COST = 8
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
    bytes when None). The q-grams are listed in order: by value, then by offset. Raises
    ``ValueError`` when no value is left (exec always indexes the run secret) or the values
    total more than ``VALUES_MAX_BYTES``."""
    if key is None:
        key = os.urandom(KEY_BYTES)
    if len(key) != KEY_BYTES:
        raise ValueError(f"the index key must be {KEY_BYTES} bytes")
    raws = sorted({os.fsencode(v) for v in values if len(v) >= MIN_SECRET_CHARS})
    if not raws:
        raise ValueError("no secret value to index")
    total = sum(len(raw) for raw in raws)
    if total > VALUES_MAX_BYTES:
        raise ValueError(
            f"the secret values total {total} bytes, over the limit of {VALUES_MAX_BYTES} "
            "bytes for the contains_secret index"
        )
    grams = sum(len(raw) - Q + 1 for raw in raws)
    parts = [MAGIC, key, _u32(len(raws))]
    for raw in raws:
        parts += [_u32(len(raw)), _value_hash(key, raw)]
    parts.append(_u32(grams))
    gram_hash = hashlib.blake2b(key=key, digest_size=GRAM_DIGEST, person=_GRAM_PERSON)
    for number, raw in enumerate(raws):
        for offset in range(len(raw) - Q + 1):
            h = gram_hash.copy()
            h.update(raw[offset : offset + Q])
            parts.append(_GRAM.pack(h.digest(), number, offset))
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
    once, in order (by value, then by offset): the stride relies on every q-gram being
    there."""
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
    stride = min(Q, min(lengths) - Q + 1)
    anchors: dict[bytes, list[tuple[int, int]]] = {}
    gram_digests: list[bytes] = []
    first_gram: list[int] = []
    entries = _GRAM.iter_unpack(view[at:])
    for number, length in enumerate(lengths):
        first_gram.append(len(gram_digests))
        for offset in range(length - Q + 1):
            digest, got_number, got_offset = next(entries)
            if (got_number, got_offset) != (number, offset):
                raise SecretIndexError("secret-index has an invalid q-gram entry")
            gram_digests.append(digest)
            if offset < stride:
                anchors.setdefault(digest, []).append((number, offset))
    return SecretIndex(
        key=key,
        lengths_by_value=tuple(lengths),
        digests=tuple(digests),
        anchors={digest: tuple(entries) for digest, entries in anchors.items()},
        gram_digests=b"".join(gram_digests),
        first_gram=tuple(first_gram),
    )


@dataclass(frozen=True)
class SecretIndex:
    """A loaded index. ``scan()`` starts a streamed scan; ``contains(data)`` scans one blob.

    ``anchors`` maps the digest of each q-gram at an offset below the stride to its
    ``(value, offset)`` entries; ``gram_digests`` holds every q-gram digest in index order
    (value ``n``'s q-gram at ``o`` is entry ``first_gram[n] + o``)."""

    key: bytes = field(repr=False)
    lengths_by_value: tuple[int, ...]
    digests: tuple[bytes, ...] = field(repr=False)
    anchors: dict[bytes, tuple[tuple[int, int], ...]] = field(repr=False)
    gram_digests: bytes = field(repr=False)
    first_gram: tuple[int, ...] = field(repr=False)

    @property
    def lengths(self) -> tuple[int, ...]:
        """The distinct recorded value lengths (bytes), ascending."""
        return tuple(sorted(set(self.lengths_by_value)))

    @property
    def stride(self) -> int:
        return min(Q, min(self.lengths_by_value) - Q + 1)

    def gram_digest(self, number: int, offset: int) -> bytes:
        at = (self.first_gram[number] + offset) * GRAM_DIGEST
        return self.gram_digests[at : at + GRAM_DIGEST]

    def scan(self, check: Callable[[], None] | None = None) -> Scan:
        """A streamed scan. ``check`` (when given) is called at least every ``CHECK_EVERY``
        work units inside ``update``; an exception it raises ends the scan (abandon it)."""
        return Scan(self, check)

    def contains(self, data: bytes) -> bool:
        return self.scan().update(data)


class Scan:
    """One streamed scan: feed the bytes in order with ``update``; it returns True once a
    recorded value has occurred anywhere in the bytes fed so far (across chunk boundaries).

    State: ``_buf`` holds the bytes from absolute offset ``_base`` on; ``_next`` is the next
    absolute offset to sample; ``_pending`` are candidate windows ``(start, value)`` whose end
    has not arrived yet. A window is named only by its first sampled q-gram (an anchor, at an
    offset below the stride), so the bytes kept are those of the pending windows and the
    ``stride - 1`` bytes before the next sample."""

    def __init__(self, index: SecretIndex, check: Callable[[], None] | None = None) -> None:
        self._index = index
        self._check = check
        self._stride = index.stride
        self._gram_hash = hashlib.blake2b(
            key=index.key, digest_size=GRAM_DIGEST, person=_GRAM_PERSON
        )
        # Per value: the offset of the prefilter q-gram (the last one) and its digest.
        self._last = tuple(length - Q for length in index.lengths_by_value)
        self._last_digest = tuple(
            index.gram_digest(number, offset) for number, offset in enumerate(self._last)
        )
        self._buf = b""
        self._base = 0
        self._next = 0
        self._pending: list[tuple[int, int, int]] = []
        self._work = 0
        self.found = False

    @property
    def retained(self) -> int:
        """Bytes kept between updates (bounded by the longest value plus one chunk)."""
        return len(self._buf)

    def _tick(self, units: int) -> None:
        self._work += units
        if self._work >= CHECK_EVERY:
            self._work = 0
            if self._check is not None:
                self._check()

    def _confirm(self, buf: bytes, at: int, number: int, offset: int) -> bool:
        """Whether the window of value ``number`` at ``buf[at:]`` (named by its q-gram at
        ``offset``) is that value: its last q-gram first, then the whole window."""
        length = self._index.lengths_by_value[number]
        self._tick(CONFIRM_COST)
        last = self._last[number]
        if last != offset:
            h = self._gram_hash.copy()
            h.update(buf[at + last : at + length])
            if h.digest() != self._last_digest[number]:
                return False
        self._tick(length >> 9)
        window = memoryview(buf)[at : at + length]
        return hmac.compare_digest(
            _value_hash(self._index.key, window), self._index.digests[number]
        )

    def update(self, data: bytes) -> bool:
        if self.found or not data:
            return self.found
        if self._check is not None:
            self._check()
        buf = self._buf + data
        base = self._base
        end = base + len(buf)
        lengths = self._index.lengths_by_value
        waiting: list[tuple[int, int, int]] = []
        for start, number, offset in self._pending:
            self._tick(1)  # every waiting window is revisited: one unit each
            if start + lengths[number] > end:
                waiting.append((start, number, offset))
            elif self._confirm(buf, start - base, number, offset):
                self.found = True
                return True
        lookup = self._index.anchors.get
        copy = self._gram_hash.copy
        position = self._next
        stride = self._stride
        limit = end - Q
        while position <= limit:
            # Tick inline (one unit per sample): the hot loop.
            self._work += 1
            if self._work >= CHECK_EVERY:
                self._tick(0)
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
                        waiting.append((start, number, offset))
                    elif self._confirm(buf, start - base, number, offset):
                        self.found = True
                        return True
            position += stride
        self._next = position
        self._pending = waiting
        keep = max(base, position - stride + 1)
        if waiting:
            keep = min(keep, min(start for start, _, _ in waiting))
        self._buf = buf[keep - base :]
        self._base = keep
        return False
