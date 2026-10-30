"""Session ids, prefix matching and name grammars (interface.md, Identifiers)."""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from claude_session import ids
from claude_session.errors import AmbiguousError, InternalError, NotFoundError, UsageError

from .schemas import validate

GOLDEN = Path(__file__).resolve().parent / "golden"
ERROR_SCHEMA = "claude-session/cli/error.json"
SCHEMA_ID_PATTERN = re.compile(r"^cs-[a-z2-7]{8}$")  # schemas/cli/status.json, csession_id


def _source(*chunks: bytes) -> tuple[Callable[[int], bytes], list[int]]:
    """A fake of ``secrets.token_bytes`` returning ``chunks`` in order, and its requests."""
    stream: Iterator[bytes] = iter(chunks)
    requested: list[int] = []

    def random_bytes(n: int) -> bytes:
        requested.append(n)
        return next(stream)

    return random_bytes, requested


# Allocation


def test_new_id_is_cs_and_eight_lowercase_base32_chars() -> None:
    random_bytes, requested = _source(b"\x00\x00\x00\x00\x00")
    assert ids.new_session_id(lambda _: False, random_bytes=random_bytes) == "cs-aaaaaaaa"
    assert requested == [5]


def test_new_id_maps_all_bits_to_the_base32_alphabet() -> None:
    random_bytes, _ = _source(b"\xff\xff\xff\xff\xff")
    assert ids.new_session_id(lambda _: False, random_bytes=random_bytes) == "cs-77777777"


def test_new_id_with_the_default_random_source_matches_the_schema() -> None:
    allocated = {ids.new_session_id(lambda _: False) for _ in range(50)}
    assert all(SCHEMA_ID_PATTERN.fullmatch(i) for i in allocated)
    assert all(ids.is_session_id(i) for i in allocated)
    assert len(allocated) > 1


def test_new_id_retries_on_collision() -> None:
    random_bytes, requested = _source(b"\x00" * 5, b"\x00" * 5, b"\xff" * 5)
    taken = {"cs-aaaaaaaa"}
    checked: list[str] = []

    def is_used(candidate: str) -> bool:
        checked.append(candidate)
        return candidate in taken

    assert ids.new_session_id(is_used, random_bytes=random_bytes) == "cs-77777777"
    assert checked == ["cs-aaaaaaaa", "cs-aaaaaaaa", "cs-77777777"]
    assert len(requested) == 3


def test_new_id_never_reuses_a_deleted_id() -> None:
    # The store's check covers both `sessions` and `deleted_ids` (design.md, Data model).
    live: set[str] = set()
    deleted = {"cs-aaaaaaaa"}
    random_bytes, _ = _source(b"\x00" * 5, b"\xff" * 5)
    new = ids.new_session_id(lambda c: c in live or c in deleted, random_bytes=random_bytes)
    assert new == "cs-77777777"


def test_new_id_gives_up_after_max_attempts() -> None:
    random_bytes, requested = _source(*([b"\x00" * 5] * 3))
    with pytest.raises(InternalError, match="could not allocate a session id"):
        ids.new_session_id(lambda _: True, random_bytes=random_bytes, max_attempts=3)
    assert len(requested) == 3


# Full ids


@pytest.mark.parametrize("text", ["cs-k3v7q2xa", "cs-aaaaaaaa", "cs-77777777"])
def test_is_session_id_valid(text: str) -> None:
    assert ids.is_session_id(text)


@pytest.mark.parametrize(
    "text",
    [
        "k3v7q2xa",  # no cs-
        "cs-k3v7q2x",  # 7 chars
        "cs-k3v7q2xab",  # 9 chars
        "cs-K3V9Q2XA",  # uppercase
        "cs-k3v7q2x1",  # 1 is not base32
        "cs-k3v7q2x8",
        "cs-k3v7q2xa\n",
        "CS-k3v7q2xa",
        "",
    ],
)
def test_is_session_id_invalid(text: str) -> None:
    assert not ids.is_session_id(text)


# Prefix input


@pytest.mark.parametrize(
    ("text", "prefix"),
    [
        ("cs-abcd", "cs-abcd"),
        ("abcd", "cs-abcd"),
        ("abcd2345", "cs-abcd2345"),
        ("cs-abcd2345", "cs-abcd2345"),
        ("csab", "cs-csab"),  # no dash: "cs" is part of the id body
    ],
)
def test_session_id_prefix_normalizes_input(text: str, prefix: str) -> None:
    assert ids.session_id_prefix(text) == prefix


@pytest.mark.parametrize("text", ["", "a", "abc", "cs-", "cs-abc", "cs-a"])
def test_session_id_prefix_shorter_than_4_chars_is_a_usage_error(text: str) -> None:
    with pytest.raises(UsageError, match="at least 4 characters") as info:
        ids.session_id_prefix(text)
    assert info.value.exit_code == 2


SESSIONS = [
    ids.IdCandidate("cs-abcd7777"),
    ids.IdCandidate("cs-abcd2345"),
    ids.IdCandidate("cs-k3v7q2xa"),
    ids.IdCandidate("cs-zzzz2222", forgotten=True),
    ids.IdCandidate("cs-k3v7aaaa", forgotten=True),
]


@pytest.mark.parametrize(
    ("text", "resolved"),
    [
        ("cs-k3v7q2xa", "cs-k3v7q2xa"),
        ("k3v7q2xa", "cs-k3v7q2xa"),
        ("k3v7", "cs-k3v7q2xa"),  # unique once forgotten cs-k3v7aaaa is excluded
        ("cs-k3v7q", "cs-k3v7q2xa"),
        ("abcd2", "cs-abcd2345"),
        ("cs-abcd7777", "cs-abcd7777"),
    ],
)
def test_resolve_unique_prefix(text: str, resolved: str) -> None:
    assert ids.resolve_session_id(text, SESSIONS) == resolved


def test_resolve_ambiguous_prefix_lists_sorted_candidates() -> None:
    with pytest.raises(AmbiguousError) as info:
        ids.resolve_session_id("abcd", SESSIONS)
    assert info.value.exit_code == 21
    assert info.value.matches == ["cs-abcd2345", "cs-abcd7777"]


def test_resolve_ambiguous_envelope_matches_golden() -> None:
    with pytest.raises(AmbiguousError) as info:
        ids.resolve_session_id("abcd", SESSIONS)
    envelope = info.value.envelope()
    validate(envelope, ERROR_SCHEMA)
    assert envelope == json.loads((GOLDEN / "error-ambiguous.json").read_text())


@pytest.mark.parametrize(
    "text",
    [
        "cs-zzzz2222",  # forgotten, full id
        "zzzz",  # forgotten, prefix
        "cs-k3v7aa",  # forgotten, prefix that would be unique
        "qqqq",
        "cs-abcd2345a",  # longer than any id
        "ABCD",  # ids are lowercase
        "abc1",  # not base32
    ],
)
def test_resolve_unknown_or_forgotten_is_not_found(text: str) -> None:
    with pytest.raises(NotFoundError) as info:
        ids.resolve_session_id(text, SESSIONS)
    assert info.value.exit_code == 10
    validate(info.value.envelope(), ERROR_SCHEMA)


def test_resolve_not_found_envelope_matches_golden() -> None:
    with pytest.raises(NotFoundError) as info:
        ids.resolve_session_id("cs-abcd2345", [ids.IdCandidate("cs-abcd2345", forgotten=True)])
    assert info.value.envelope() == json.loads((GOLDEN / "error-not-found.json").read_text())


def test_resolve_short_prefix_is_usage_before_lookup() -> None:
    with pytest.raises(UsageError):
        ids.resolve_session_id("abc", SESSIONS)


def test_resolve_with_no_sessions_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        ids.resolve_session_id("abcd", [])


# Kind names: ^[a-z0-9][a-z0-9_-]{0,63}$


@pytest.mark.parametrize(
    "name", ["review", "implement", "a", "0", "review-respond", "my_kind", "k" * 64, "9x-_"]
)
def test_kind_name_valid(name: str) -> None:
    assert ids.is_kind_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "Review", "-review", "_review", "re view", "re.view", "k" * 65, "review\n", "rév"],
)
def test_kind_name_invalid(name: str) -> None:
    assert not ids.is_kind_name(name)


# Permission-profile names: ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$


@pytest.mark.parametrize("name", ["review", "Read-Only", "p_1", "P" * 64])
def test_permission_profile_name_valid(name: str) -> None:
    assert ids.is_permission_profile_name(name)


@pytest.mark.parametrize(
    "name", ["", "-p", "p.json", "p.yaml", "p.yml", "a/b", "P" * 65, "p\n", "p q"]
)
def test_permission_profile_name_invalid(name: str) -> None:
    assert not ids.is_permission_profile_name(name)


# Skill names: ^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$


@pytest.mark.parametrize(
    "name", ["review", "plan:make", "Plugin:Skill.v2", "a", "s_k-i.l:l", "S" * 128]
)
def test_skill_name_valid(name: str) -> None:
    assert ids.is_skill_name(name)


@pytest.mark.parametrize(
    "name", ["", ":plan", "-x", ".x", "a b", "a/b", "/review", "S" * 129, "x\n", "x;y"]
)
def test_skill_name_invalid(name: str) -> None:
    assert not ids.is_skill_name(name)


# Artifact names: one segment ^[A-Za-z0-9._-]{1,128}$, not . or ..


@pytest.mark.parametrize("name", ["out.md", "a", "...", ".hidden", "a-b_c.d", "x" * 128])
def test_artifact_name_valid(name: str) -> None:
    assert ids.is_artifact_name(name)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a b", "x" * 129, "a.md\n", "é"])
def test_artifact_name_invalid(name: str) -> None:
    assert not ids.is_artifact_name(name)


# Labels: key ^[a-z0-9][a-z0-9_.-]{0,63}$, value 0-256 of [A-Za-z0-9._:/#@+=,-]


@pytest.mark.parametrize(
    ("text", "pair"),
    [
        ("kind=review", ("kind", "review")),
        ("a=", ("a", "")),
        ("expr=a=b", ("expr", "a=b")),  # split at the first '='
        ("pr=github.com/org/repo#42", ("pr", "github.com/org/repo#42")),
        ("owner=me@example.org", ("owner", "me@example.org")),
        ("x.y_z-1=A,b+c:d", ("x.y_z-1", "A,b+c:d")),
        ("k" * 64 + "=" + "v" * 256, ("k" * 64, "v" * 256)),
        ("0=0", ("0", "0")),
    ],
)
def test_parse_label_valid(text: str, pair: tuple[str, str]) -> None:
    assert ids.parse_label(text) == pair


@pytest.mark.parametrize(
    "text",
    [
        "kind",  # no '='
        "=value",  # empty key
        "Kind=x",  # uppercase key
        "-k=x",
        ".k=x",
        "k k=x",
        "k" * 65 + "=x",
        "k=a b",  # space in value
        "k=a\tb",
        "k=a\nb",
        "k=a\x1b",
        "k=v\n",
        "k=" + "v" * 257,
        "k=a;b",
        "k=$(x)",
        "k=é",
    ],
)
def test_parse_label_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        ids.parse_label(text)


# Repo ids


@pytest.mark.parametrize(
    "repo",
    [
        "example.org/group/repo",
        "github.com/Org/Repo.git-mirror",
        "example.org:8443/a/b/c",
        "localhost/r",
        "git.example-host.org/a_b/c-d/e.f",
        "example.org/...",
    ],
)
def test_repo_id_valid(repo: str) -> None:
    assert ids.is_repo_id(repo)


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "example.org",  # no path
        "example.org/",
        "Example.org/r",  # uppercase host
        "https://example.org/r",
        "git@example.org:r",
        "example.org//r",
        "example.org/./r",
        "example.org/../r",
        "example.org/r/..",
        "example.org/r/",
        "example.org:/r",
        "example.org:0/r",
        "example.org:65536/r",
        "example.org:08/r",
        "example.org/a b",
        "example.org/r\n",
        "/r",
        "-host/r",
        "host-/r",
        "a..b/r",
    ],
)
def test_repo_id_invalid(repo: str) -> None:
    assert not ids.is_repo_id(repo)


@pytest.mark.parametrize(
    ("origin", "repo"),
    [
        ("https://github.com/Org/Repo.git", "github.com/Org/Repo"),
        ("https://GitHub.com/org/repo", "github.com/org/repo"),
        ("https://user@example.org/g/sub/r.git", "example.org/g/sub/r"),
        ("https://user:token@example.org/g/r", "example.org/g/r"),
        ("https://example.org:443/g/r", "example.org/g/r"),
        ("http://example.org:80/g/r", "example.org/g/r"),
        ("https://example.org:8443/g/r.git", "example.org:8443/g/r"),
        ("ssh://git@example.org:22/g/r.git", "example.org/g/r"),
        ("ssh://git@example.org:2222/g/r.git", "example.org:2222/g/r"),
        ("git://example.org/g/r.git", "example.org/g/r"),
        ("git://example.org:9418/g/r", "example.org/g/r"),
        ("git@github.com:org/repo.git", "github.com/org/repo"),
        ("git@GitHub.com:org/repo", "github.com/org/repo"),
        ("example.org:org/repo.git", "example.org/org/repo"),
        ("https://example.org/g/r.git/", "example.org/g/r"),
    ],
)
def test_repo_id_from_origin(origin: str, repo: str) -> None:
    assert ids.repo_id_from_origin(origin) == repo
    assert ids.is_repo_id(repo)


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "/srv/git/repo.git",
        "../repo",
        "file:///srv/git/repo.git",
        "https://example.org",
        "https://example.org/",
        "https://example.org/a b/r",
        "https://example.org/../r",
        "git@example.org:",
        "ftp://example.org/r",
        "https://[::1]/r",
    ],
)
def test_repo_id_from_origin_without_a_valid_id_is_none(origin: str) -> None:
    assert ids.repo_id_from_origin(origin) is None


# Module rules


def test_ids_imports_only_the_standard_library() -> None:
    source = Path(ids.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                assert node.module == "errors", "ids may import only .errors"
                continue
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert name.partition(".")[0] in sys.stdlib_module_names, name
