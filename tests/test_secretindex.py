"""contains_secret's keyed q-gram index (design.md, Outbox and ingestion: ``contains_secret``)."""

from __future__ import annotations

import base64
import os
import random
import stat
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_session import logs, secretindex
from claude_session.fsutil import write_private_file
from claude_session.secretindex import SecretIndex, SecretIndexError

# The lengths of common credentials (made-up values without any real token prefix).
API_KEY = "test-api-" + "Zq8" * 33 + "A"  # 109 characters
GIT_TOKEN = "tok_" + "x7Yk2" * 7 + "Pq"  # 40 characters
AWS_ID = "KEYID" + "QX7Z3M5N8P2R4T6"  # 20 characters
AWS_SECRET = "fakeSecretKey/K7MDENG/bPxRfiCY" + "1234567890"  # 40 characters
RUN_SECRET = "0123456789abcdef" * 4  # 64 hex characters
REALISTIC = [API_KEY, GIT_TOKEN, AWS_ID, AWS_SECRET, RUN_SECRET]


def index_of(*values: str) -> SecretIndex:
    return secretindex.parse(secretindex.build(values))


def chunked(index: SecretIndex, chunks: list[bytes]) -> bool:
    scan = index.scan()
    found = False
    for chunk in chunks:
        found = scan.update(chunk)
    return found


# The index holds no plaintext


def test_the_index_holds_no_plaintext_or_q_gram_of_any_value() -> None:
    values = [*REALISTIC, "short-but-ok", "ünïcödé-sécret"]
    data = secretindex.build(values)
    for value in values:
        raw = os.fsencode(value)
        assert raw not in data
        for start in range(len(raw) - secretindex.Q + 1):
            assert raw[start : start + secretindex.Q] not in data
        assert raw.hex().encode() not in data
        assert base64.b64encode(raw) not in data


def test_each_build_draws_a_fresh_key() -> None:
    first = secretindex.build([API_KEY])
    second = secretindex.build([API_KEY])
    assert first != second
    assert secretindex.parse(first).contains(API_KEY.encode())
    assert secretindex.parse(second).contains(API_KEY.encode())


def test_a_given_key_builds_the_same_index() -> None:
    key = bytes(range(32))
    assert secretindex.build([API_KEY], key=key) == secretindex.build([API_KEY], key=key)


def test_the_key_must_be_32_bytes() -> None:
    with pytest.raises(ValueError, match="key"):
        secretindex.build([API_KEY], key=b"short")


def test_values_shorter_than_8_characters_are_not_indexed() -> None:
    index = index_of("1234567", "abcdefgh")
    assert not index.contains(b"xx1234567xx")
    assert index.contains(b"xxabcdefghxx")
    assert index.lengths == (8,)


def test_the_minimum_is_the_logs_redaction_minimum() -> None:
    assert secretindex.MIN_SECRET_CHARS == logs.MIN_SECRET_CHARS == 8


def test_the_minimum_counts_characters_not_bytes() -> None:
    """Seven characters stay unindexed even when their UTF-8 is longer (as in logs)."""
    index = index_of("éééééé", "ééééééé8")
    assert not index.contains("éééééé".encode())
    assert index.contains("x ééééééé8 y".encode())


def test_duplicate_values_are_indexed_once() -> None:
    assert index_of(API_KEY, API_KEY).lengths == (len(API_KEY),)


def test_an_empty_set_is_refused() -> None:
    """exec always indexes the run secret: an index without values is a bug."""
    with pytest.raises(ValueError, match="no secret value"):
        secretindex.build([])
    with pytest.raises(ValueError, match="no secret value"):
        secretindex.build(["short"])


# Literal matches


@pytest.mark.parametrize("value", REALISTIC)
@pytest.mark.parametrize("where", ["alone", "start", "middle", "end"])
def test_a_literal_value_is_found(value: str, where: str) -> None:
    index = index_of(*REALISTIC)
    raw = value.encode()
    data = {
        "alone": raw,
        "start": raw + b" trailing text",
        "middle": b"some text " + raw + b" more",
        "end": b"leading text: " + raw,
    }[where]
    assert index.contains(data)


def test_clean_text_is_not_flagged() -> None:
    index = index_of(*REALISTIC)
    assert not index.contains(b"")
    assert not index.contains(b"all done; see the artifact")
    assert not index.contains(os.urandom(64 * 1024))


@pytest.mark.parametrize("value", REALISTIC)
def test_a_partial_or_altered_value_is_not_a_match(value: str) -> None:
    index = index_of(*REALISTIC)
    raw = value.encode()
    assert not index.contains(raw[:-1])
    assert not index.contains(raw[1:])
    altered = raw[:5] + bytes([raw[5] ^ 1]) + raw[6:]
    assert not index.contains(altered)


@pytest.mark.parametrize("length", range(8, 25))
def test_every_offset_is_covered_whatever_the_stride(length: int) -> None:
    """The stride is min(8, L - 7) for the shortest length L: every occurrence of every value,
    at every offset, contains a sampled q-gram."""
    short = "".join(random.Random(length).choices("abcdefghijklmnop", k=length))
    index = index_of(short, API_KEY)
    assert index.stride == min(8, length - 7)
    for value in (short, API_KEY):
        for offset in range(20):
            assert index.contains(b"#" * offset + value.encode() + b"#" * 3), (value, offset)


def test_overlapping_and_repeated_values_are_found() -> None:
    index = index_of("abababababab", "aaaaaaaaaaaaaaaaaaaa")
    assert index.contains(b"xabababababababx")
    assert index.contains(b"a" * 19 + b"b" + b"a" * 20)
    assert not index.contains(b"a" * 19 + b"b" + b"a" * 19)


def test_a_multibyte_value_is_found_in_utf8_text() -> None:
    value = "pässwörd-日本語-🔑"
    index = index_of(value)
    assert index.contains(f"the value is {value}.".encode())


def test_a_value_that_is_not_utf8_is_matched_as_its_environment_bytes() -> None:
    """os.environ decodes undecodable bytes with surrogateescape; the index holds the bytes."""
    raw = b"\xff\xfe-not-utf8-secret"
    value = os.fsdecode(raw)
    index = index_of(value)
    assert index.contains(b"blob " + raw + b" blob")


def test_injection_shaped_values_are_plain_bytes() -> None:
    value = "'; DROP TABLE runs; -- \x00\n${HOME}"
    index = index_of(value)
    assert index.contains(b"x" + value.encode() + b"y")


# Chunked scanning (the artifact copy)


@pytest.mark.parametrize("value", [AWS_ID, API_KEY, "exactly8"])
def test_a_value_split_at_every_chunk_boundary_is_found(value: str) -> None:
    index = index_of(value, RUN_SECRET)
    data = b"prefix-" + value.encode() + b"-suffix"
    for cut in range(1, len(data)):
        assert chunked(index, [data[:cut], data[cut:]]), cut


def test_one_byte_chunks_and_empty_chunks() -> None:
    index = index_of(*REALISTIC)
    data = b"lead " + GIT_TOKEN.encode() + b" tail"
    pieces: list[bytes] = []
    for byte in data:
        pieces += [b"", bytes([byte])]
    assert chunked(index, pieces)


def test_a_value_spanning_many_chunks_is_found() -> None:
    long_value = "".join(random.Random(7).choices("0123456789abcdef", k=5000))
    index = index_of(long_value, AWS_ID)
    data = os.urandom(3000) + long_value.encode() + os.urandom(3000)
    assert chunked(index, [data[i : i + 97] for i in range(0, len(data), 97)])


def test_a_found_scan_stays_found() -> None:
    scan = index_of(API_KEY).scan()
    assert scan.update(API_KEY.encode())
    assert scan.update(b"clean")
    assert scan.found


def test_a_value_cut_off_by_the_end_of_the_data_is_not_found() -> None:
    index = index_of(API_KEY)
    assert not chunked(index, [b"x" * 100, API_KEY.encode()[:-1]])


def test_retained_bytes_stay_bounded() -> None:
    """The scan keeps only what a pending window can still need, not the whole stream."""
    index = index_of(API_KEY, AWS_ID)
    scan = index.scan()
    for _ in range(64):
        scan.update(os.urandom(4096))
    assert scan.retained <= len(API_KEY) + 4096


@pytest.mark.parametrize("seed", range(40))
def test_the_scan_agrees_with_a_plain_substring_search(seed: int) -> None:
    """Random small-alphabet secrets and data (many partial matches), random chunking."""
    rng = random.Random(seed)
    alphabet = "ab" if seed % 2 else "abc"
    values = [
        "".join(rng.choices(alphabet, k=rng.randint(8, 30))) for _ in range(rng.randint(1, 4))
    ]
    index = index_of(*values)
    for _ in range(20):
        data = "".join(rng.choices(alphabet, k=rng.randint(0, 200))).encode()
        expected = any(v.encode() in data for v in values)
        cuts = sorted(rng.sample(range(len(data) + 1), min(len(data) + 1, rng.randint(0, 6))))
        pieces = [data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)], strict=True)]
        assert index.contains(data) == expected, (values, data)
        assert chunked(index, pieces) == expected, (values, pieces)


def _near(rng: random.Random, value: str, alphabet: str) -> str:
    """``value`` with one character changed (a near match: every q-gram but a few match)."""
    at = rng.randrange(len(value))
    other = [c for c in alphabet if c != value[at]]
    return value[:at] + rng.choice(other) + value[at + 1 :]


@pytest.mark.parametrize("seed", range(60))
def test_the_scan_agrees_with_a_plain_substring_search_on_near_matches(seed: int) -> None:
    """Data built from near matches, repeats and planted values (candidate-heavy: many windows
    pass the anchor and the prefilter q-gram and fail the whole-window hash), lengths that
    give every stride, random chunking down to single bytes."""
    rng = random.Random(1000 + seed)
    alphabet = ["ab", "abc", "abcdefgh"][seed % 3]
    values = [
        "".join(rng.choices(alphabet, k=rng.randint(8, 24))) for _ in range(rng.randint(1, 3))
    ]
    if seed % 4 == 0:
        values.append(alphabet[0] * rng.randint(8, 40))  # self-periodic
    index = index_of(*values)
    for _ in range(15):
        parts = []
        for _ in range(rng.randint(1, 12)):
            value = rng.choice(values)
            kind = rng.random()
            if kind < 0.6:
                parts.append(_near(rng, value, alphabet))
            elif kind < 0.7:
                parts.append(value)
            elif kind < 0.85:
                parts.append(value[: rng.randint(1, len(value) - 1)])
            else:
                parts.append("".join(rng.choices(alphabet, k=rng.randint(0, 9))))
        data = "".join(parts).encode()
        expected = any(v.encode() in data for v in values)
        size = rng.choice([1, 2, 3, 7, 16, 1000])
        pieces = [data[i : i + size] for i in range(0, len(data), size)]
        assert index.contains(data) == expected, (values, data)
        assert chunked(index, pieces) == expected, (values, size, data)


def test_a_self_periodic_value_is_found_after_near_matches() -> None:
    index = index_of("a" * 2000, RUN_SECRET)
    data = (b"a" * 1999 + b"b") * 20
    assert not index.contains(data)
    assert index.contains(data + b"a" * 2000)
    assert chunked(index, [data[:1234], data[1234:] + b"a" * 1000, b"a" * 1000])


# Scan cost: candidate windows (design.md, contains_secret budget). Counted, not timed: the
# bytes given to the whole-window hash.


def _window_hash_bytes(
    monkeypatch: pytest.MonkeyPatch, index: SecretIndex, data: bytes, chunk: int = 4096
) -> int:
    hashed = [0]
    original = secretindex._value_hash

    def counting(key: bytes, window: bytes | memoryview) -> bytes:
        hashed[0] += len(window)
        return original(key, window)

    monkeypatch.setattr(secretindex, "_value_hash", counting)
    assert not chunked(index, [data[i : i + chunk] for i in range(0, len(data), chunk)])
    return hashed[0]


def _pem(label: str, rng: random.Random, size: int) -> str:
    body = base64.b64encode(rng.randbytes(size)).decode()
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n"


@pytest.mark.parametrize("unit", [b"CERTIFIC", b"-----BEGIN CERTIFICATE-----\n", b"-----"])
def test_a_value_with_repeated_q_grams_costs_no_window_hashes_on_matching_data(
    monkeypatch: pytest.MonkeyPatch, unit: bytes
) -> None:
    """A PEM bundle repeats its header q-grams; data made of them used to name the same
    windows over and over, each hashed whole."""
    rng = random.Random(5)
    bundle = _pem("PRIVATE KEY", rng, 1218) + "".join(
        _pem("CERTIFICATE", rng, 900) for _ in range(6)
    )
    index = index_of(bundle, API_KEY, RUN_SECRET)
    data = unit * (256 * 1024 // len(unit))
    assert _window_hash_bytes(monkeypatch, index, data) <= len(data)


@pytest.mark.parametrize("where", ["middle", "end"])
def test_near_matches_at_stride_1_hash_each_window_at_most_once(
    monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """Data made of copies of a value with one byte changed: each copy is one candidate (its
    anchor), never one per sampled q-gram, and the last-q-gram prefilter drops a copy whose
    end differs."""
    index = index_of("exactly8", API_KEY, RUN_SECRET)
    assert index.stride == 1
    at = len(API_KEY) // 2 if where == "middle" else len(API_KEY) - 1
    near = (API_KEY[:at] + "#" + API_KEY[at + 1 :]).encode()
    data = near * (256 * 1024 // len(near))
    hashed = _window_hash_bytes(monkeypatch, index, data)
    if where == "end":
        assert hashed == 0
    else:
        assert hashed <= len(data)


def test_each_candidate_window_is_named_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every (start, value) window reaches the confirmation at most once, whatever the value
    repeats and however the data is chunked."""
    seen: list[tuple[int, int]] = []
    original = secretindex.Scan._confirm

    def recording(self: secretindex.Scan, buf: bytes, at: int, number: int, offset: int) -> bool:
        seen.append((self._base + at, number))
        return original(self, buf, at, number, offset)

    monkeypatch.setattr(secretindex.Scan, "_confirm", recording)
    index = index_of("abababababababab" + "c", "abcabcabcabc", RUN_SECRET)
    data = b"ab" * 3000 + b"x" + b"abcabcabcabx" * 300
    assert not chunked(index, [data[i : i + 37] for i in range(0, len(data), 37)])
    assert seen and len(seen) == len(set(seen))


# The caller's deadline: ``check`` is called at a bounded interval of work


class Stop(Exception):
    pass


def _counting_check(limit: int | None = None) -> tuple[list[int], Callable[[], None]]:
    calls = [0]

    def check() -> None:
        calls[0] += 1
        if limit is not None and calls[0] >= limit:
            raise Stop

    return calls, check


def test_check_is_called_once_per_chunk() -> None:
    calls, check = _counting_check()
    scan = index_of(API_KEY).scan(check)
    for _ in range(5):
        scan.update(b"x" * 100)
    assert calls[0] == 5


def test_check_is_called_inside_one_huge_chunk() -> None:
    """One chunk of 1 MiB at stride 8 is 131072 samples: check runs every CHECK_EVERY."""
    calls, check = _counting_check()
    index = index_of(*REALISTIC)
    assert index.stride == 8
    index.scan(check).update(bytes(1024 * 1024))
    assert calls[0] >= (1024 * 1024 // 8) // secretindex.CHECK_EVERY


def test_check_is_called_while_confirming_candidates() -> None:
    """A self-periodic value on near-matching data: every sample names candidates that pass
    the prefilter; the confirmations themselves count as work."""
    calls, check = _counting_check()
    index = index_of("a" * 2000, RUN_SECRET)
    data = (b"a" * 1999 + b"b") * 32
    samples = len(data) // index.stride
    assert not index.scan(check).update(data)
    assert calls[0] > 2 * (samples // secretindex.CHECK_EVERY + 1)


def test_an_exception_from_check_ends_the_scan() -> None:
    _, check = _counting_check(limit=3)
    scan = index_of("exactly8").scan(check)
    with pytest.raises(Stop):
        scan.update(bytes(4 * secretindex.CHECK_EVERY))


# Documented residual risk: only literal values are caught


@pytest.mark.parametrize(
    "transform",
    [
        pytest.param(lambda s: s.upper(), id="case-change"),
        pytest.param(lambda s: base64.b64encode(s.encode()).decode(), id="base64"),
        pytest.param(lambda s: s.encode().hex(), id="hex"),
        pytest.param(lambda s: " ".join(s), id="whitespace-inserted"),
        pytest.param(lambda s: s[:20] + "\n" + s[20:], id="split-across-lines"),
        pytest.param(lambda s: s[::-1], id="reversed"),
    ],
)
def test_transformed_values_are_not_caught(transform: Callable[[str], str]) -> None:
    """interface.md, Residual risks: contains_secret catches only literal values of 8 or more
    characters. A configured value transformed beyond a literal match passes (the run secret
    alone is also refused in any letter case, by outbox, not by this index)."""
    index = index_of(GIT_TOKEN)
    assert not index.contains(transform(GIT_TOKEN).encode())


# Parsing and reading the file


def test_parse_reports_lengths_and_stride() -> None:
    index = index_of(*REALISTIC)
    assert index.lengths == tuple(sorted({len(v) for v in REALISTIC}))
    assert index.stride == 8
    assert index_of("exactly8").stride == 1
    assert index_of("twelve-chars").stride == 5


def _layout(data: bytes) -> tuple[int, int]:
    """(offset of the value table, number of values) of a built index."""
    header = len(secretindex.MAGIC) + secretindex.KEY_BYTES
    return header, int.from_bytes(data[header : header + 4], "little")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: b"", id="empty"),
        pytest.param(lambda d: d[:-1], id="truncated"),
        pytest.param(lambda d: d + b"\0", id="trailing-bytes"),
        pytest.param(lambda d: b"X" + d[1:], id="bad-magic"),
        pytest.param(
            lambda d: d[: _layout(d)[0]] + b"\0\0\0\0" + d[_layout(d)[0] + 4 :], id="no-values"
        ),
        pytest.param(lambda d: d[:-8] + (99).to_bytes(4, "little") + d[-4:], id="bad-value-index"),
        pytest.param(lambda d: d[:-4] + (999).to_bytes(4, "little"), id="bad-offset"),
        pytest.param(
            lambda d: d[: _layout(d)[0] + 4] + (7).to_bytes(4, "little") + d[_layout(d)[0] + 8 :],
            id="value-too-short",
        ),
    ],
)
def test_parse_refuses_a_malformed_index(mutate: Callable[[bytes], bytes]) -> None:
    data = secretindex.build(["abcdefghij"])
    with pytest.raises(SecretIndexError):
        secretindex.parse(mutate(data))


def test_parse_refuses_an_index_missing_a_q_gram() -> None:
    """Every q-gram of every value must be listed, or the stride would leave gaps."""
    data = secretindex.build(["abcdefghij"])
    header, _ = _layout(data)
    values_end = header + 4 + secretindex.VALUE_ENTRY
    count = int.from_bytes(data[values_end : values_end + 4], "little")
    assert count == 3
    entries = data[values_end + 4 :]
    short = data[:values_end] + (2).to_bytes(4, "little") + entries[secretindex.GRAM_ENTRY :]
    with pytest.raises(SecretIndexError):
        secretindex.parse(short)
    first = entries[: secretindex.GRAM_ENTRY]
    rest = entries[2 * secretindex.GRAM_ENTRY :]
    doubled = data[:values_end] + (3).to_bytes(4, "little") + first + first + rest
    with pytest.raises(SecretIndexError):
        secretindex.parse(doubled)


def test_parse_refuses_q_grams_out_of_order() -> None:
    """The q-grams are listed by value, then by offset (build's order)."""
    data = secretindex.build(["abcdefghij"])
    header, _ = _layout(data)
    values_end = header + 4 + secretindex.VALUE_ENTRY
    entries = data[values_end + 4 :]
    size = secretindex.GRAM_ENTRY
    swapped = entries[size : 2 * size] + entries[:size] + entries[2 * size :]
    with pytest.raises(SecretIndexError, match="q-gram entry"):
        secretindex.parse(data[: values_end + 4] + swapped)


def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "run"
    path.mkdir(mode=0o700)
    return path


def open_dir(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def test_write_and_read_round_trip(tmp_path: Path) -> None:
    path = run_dir(tmp_path)
    fd = open_dir(path)
    try:
        secretindex.write_at(fd, REALISTIC)
        index = secretindex.read_at(fd)
    finally:
        os.close(fd)
    info = (path / secretindex.INDEX_NAME).stat()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert index.contains(b"..." + AWS_SECRET.encode() + b"...")


def test_a_missing_index_is_an_error(tmp_path: Path) -> None:
    fd = open_dir(run_dir(tmp_path))
    try:
        with pytest.raises(FileNotFoundError):
            secretindex.read_at(fd)
    finally:
        os.close(fd)


def test_a_symlinked_index_is_refused(tmp_path: Path) -> None:
    path = run_dir(tmp_path)
    decoy = tmp_path / "decoy"
    write_private_file(str(decoy), secretindex.build(["abcdefghij"]))
    (path / secretindex.INDEX_NAME).symlink_to(decoy)
    fd = open_dir(path)
    try:
        with pytest.raises(OSError):
            secretindex.read_at(fd)
    finally:
        os.close(fd)


def test_a_hard_linked_index_is_refused(tmp_path: Path) -> None:
    path = run_dir(tmp_path)
    write_private_file(str(path / secretindex.INDEX_NAME), secretindex.build(["abcdefghij"]))
    os.link(path / secretindex.INDEX_NAME, tmp_path / "link")
    fd = open_dir(path)
    try:
        with pytest.raises(SecretIndexError, match="private regular file"):
            secretindex.read_at(fd)
    finally:
        os.close(fd)


def test_an_index_readable_by_others_is_refused(tmp_path: Path) -> None:
    path = run_dir(tmp_path)
    write_private_file(str(path / secretindex.INDEX_NAME), secretindex.build(["abcdefghij"]))
    (path / secretindex.INDEX_NAME).chmod(0o640)
    fd = open_dir(path)
    try:
        with pytest.raises(SecretIndexError, match="private regular file"):
            secretindex.read_at(fd)
    finally:
        os.close(fd)


def test_an_oversized_index_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = run_dir(tmp_path)
    write_private_file(str(path / secretindex.INDEX_NAME), secretindex.build([API_KEY]))
    monkeypatch.setattr(secretindex, "INDEX_MAX_BYTES", 100)
    fd = open_dir(path)
    try:
        with pytest.raises(SecretIndexError, match="private regular file"):
            secretindex.read_at(fd)
    finally:
        os.close(fd)


def test_build_refuses_values_over_the_total_limit() -> None:
    """The values may total VALUES_MAX_BYTES (as environment bytes); more is refused with an
    error naming the limit."""
    limit = secretindex.VALUES_MAX_BYTES
    half = limit // 2
    values = ["a" * half, "b" * (limit - half)]
    assert len(secretindex.build(values)) <= secretindex.INDEX_MAX_BYTES
    with pytest.raises(ValueError, match=f"total {limit + 1} bytes, over the limit of {limit}"):
        secretindex.build(["a" * half, "b" * (limit - half + 1)])


@pytest.mark.parametrize("shape", ["one-value", "many-values"])
def test_the_largest_index_parses_well_within_the_drain_budget(shape: str) -> None:
    """At the limit parsing takes about 0.2 s on a desktop core; the bound here is generous
    for CI."""
    limit = secretindex.VALUES_MAX_BYTES
    if shape == "one-value":
        values = ["".join(random.Random(3).choices("0123456789abcdef", k=limit))]
    else:
        values = [f"{n:08d}" + "x" * 24 for n in range(limit // 32)]
    data = secretindex.build(values)
    assert len(data) <= secretindex.INDEX_MAX_BYTES
    start = time.perf_counter()
    secretindex.parse(data)
    assert time.perf_counter() - start < secretindex.SCAN_BUDGET / 4


# Budget (design.md: a full-size artifact well within the PostToolUse hook budget)

ARTIFACT_BYTES = 10 * 1024 * 1024
CHUNK = 64 * 1024


def _scan_seconds(index: SecretIndex, data: bytes) -> float:
    start = time.perf_counter()
    scan = index.scan()
    for at in range(0, len(data), CHUNK):
        scan.update(data[at : at + CHUNK])
    elapsed = time.perf_counter() - start
    assert not scan.found
    return elapsed


def test_a_10_mib_artifact_is_scanned_within_the_budget() -> None:
    """A realistic secret set (shortest value 20 bytes: stride 8)."""
    index = index_of(*REALISTIC)
    assert index.stride == 8
    assert _scan_seconds(index, os.urandom(ARTIFACT_BYTES)) < secretindex.SCAN_BUDGET


def test_the_worst_stride_stays_within_the_budget() -> None:
    """An 8-character value forces stride 1 (eight times the hashes). Measured on 1 MiB with a
    generous bound (a tenth of the input against half the budget, for slow CI machines); the
    10 MiB figure is design.md's measurement, and the drain's deadline, not this timing, is
    what bounds a scan (test_ingest)."""
    index = index_of("exactly8", *REALISTIC)
    assert index.stride == 1
    assert _scan_seconds(index, os.urandom(ARTIFACT_BYTES // 10)) < secretindex.SCAN_BUDGET / 2
