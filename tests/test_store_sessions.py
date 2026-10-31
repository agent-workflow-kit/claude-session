"""Session id allocation and prefix lookup wired to state.db, and the per-run file facts
(design.md, Identifiers and Data model)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_session import ids
from claude_session.errors import AmbiguousError, InternalError, NotFoundError, UsageError
from claude_session.store import Store

NOW = "2026-10-30T00:00:00.000000Z"


@pytest.fixture
def state(tmp_path: Path) -> Iterator[Store]:
    with Store.open(str(tmp_path / "state" / "state.db")) as s:
        yield s


def add_session(s: Store, sid: str, *, forgotten: bool = False) -> None:
    with s.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (csession_id, created_at, launch_kind, current_run, state, "
            "updated_at, forgotten_at) VALUES (?, ?, 'k', 1, 'idle', ?, ?)",
            (sid, NOW, NOW, NOW if forgotten else None),
        )


def add_deleted(s: Store, sid: str) -> None:
    with s.transaction() as conn:
        conn.execute("INSERT INTO deleted_ids (csession_id, deleted_at) VALUES (?, ?)", (sid, NOW))


def add_run(s: Store, sid: str, run_no: int) -> None:
    with s.transaction() as conn:
        conn.execute(
            "INSERT INTO runs (csession_id, run_no, created_at, run_kind) VALUES (?, ?, ?, 'k')",
            (sid, run_no, NOW),
        )


def record_sql(s: Store) -> list[str]:
    statements: list[str] = []
    s.conn.set_trace_callback(statements.append)
    return statements


# Allocation


def test_live_forgotten_and_deleted_ids_are_used(state: Store) -> None:
    add_session(state, "cs-aaaaaaaa")
    add_session(state, "cs-bbbbbbbb", forgotten=True)
    add_deleted(state, "cs-cccccccc")
    assert state.session_id_used("cs-aaaaaaaa")
    assert state.session_id_used("cs-bbbbbbbb")
    assert state.session_id_used("cs-cccccccc")
    assert not state.session_id_used("cs-dddddddd")


def test_allocation_skips_live_and_deleted_ids(state: Store) -> None:
    add_session(state, "cs-aaaaaaaa")
    add_deleted(state, "cs-bbbbbbbb")
    draws = iter([b"\x00" * 5, b"\x08\x42\x10\x84\x21", b"\x10\x84\x21\x08\x42"])
    new = ids.new_session_id(state.session_id_used, random_bytes=lambda _: next(draws))
    assert new == "cs-cccccccc"  # the first two draws are taken


def test_allocation_fails_when_every_draw_is_taken(state: Store) -> None:
    add_deleted(state, "cs-aaaaaaaa")
    with pytest.raises(InternalError):
        ids.new_session_id(
            state.session_id_used, random_bytes=lambda _: b"\x00" * 5, max_attempts=3
        )


# Prefix lookup


def test_candidates_are_the_ids_starting_with_the_prefix(state: Store) -> None:
    for sid in ("cs-abcdaaaa", "cs-abcdzzzz", "cs-abcd7777", "cs-abceaaaa", "cs-abccaaaa"):
        add_session(state, sid)
    add_session(state, "cs-abcdbbbb", forgotten=True)
    assert sorted(state.session_id_candidates("cs-abcd")) == [
        ("cs-abcd7777", False),
        ("cs-abcdaaaa", False),
        ("cs-abcdbbbb", True),
        ("cs-abcdzzzz", False),
    ]


@pytest.mark.parametrize("prefix", ["cs-abcz", "cs-abc7"])
def test_candidates_at_the_alphabet_edges(state: Store, prefix: str) -> None:
    add_session(state, prefix + "aaaa")
    add_session(state, prefix + "zzzz")
    # The first value past the range (never a valid id): excluded.
    add_session(state, prefix[:-1] + chr(ord(prefix[-1]) + 1) + "aaaa")
    assert sorted(sid for sid, _ in state.session_id_candidates(prefix)) == [
        prefix + "aaaa",
        prefix + "zzzz",
    ]


def test_a_full_id_is_its_own_only_candidate(state: Store) -> None:
    add_session(state, "cs-abcdaaaa")
    add_session(state, "cs-abcdaaab")
    assert state.session_id_candidates("cs-abcdaaaa") == [("cs-abcdaaaa", False)]


@pytest.mark.parametrize("prefix", ["cs-a%", "cs-a_cd", "cs-%", "%", "_"])
def test_like_wildcards_match_literally(state: Store, prefix: str) -> None:
    add_session(state, "cs-abcdaaaa")
    assert state.session_id_candidates(prefix) == []


def test_prefix_lookup_never_uses_like(state: Store) -> None:
    add_session(state, "cs-abcdaaaa")
    statements = record_sql(state)
    state.session_id_candidates("cs-abcd")
    assert statements
    assert not any("LIKE" in sql.upper() or "GLOB" in sql.upper() for sql in statements)


def test_an_empty_prefix_is_refused(state: Store) -> None:
    with pytest.raises(ValueError):
        state.session_id_candidates("")


# Resolution through the store


def test_lookup_resolves_a_unique_prefix(state: Store) -> None:
    add_session(state, "cs-abcdaaaa")
    add_session(state, "cs-bcdeaaaa")
    assert ids.lookup_session_id("ABCD", state.session_id_candidates) == "cs-abcdaaaa"
    assert ids.lookup_session_id("cs-abcdaaaa", state.session_id_candidates) == "cs-abcdaaaa"


def test_lookup_reports_ambiguity_with_sorted_matches(state: Store) -> None:
    add_session(state, "cs-abcdbbbb")
    add_session(state, "cs-abcdaaaa")
    with pytest.raises(AmbiguousError) as exc:
        ids.lookup_session_id("cs-abcd", state.session_id_candidates)
    assert exc.value.fields()["matches"] == ["cs-abcdaaaa", "cs-abcdbbbb"]


def test_lookup_excludes_forgotten_sessions(state: Store) -> None:
    add_session(state, "cs-abcdaaaa", forgotten=True)
    add_session(state, "cs-abcdbbbb")
    assert ids.lookup_session_id("abcd", state.session_id_candidates) == "cs-abcdbbbb"
    with pytest.raises(NotFoundError):
        ids.lookup_session_id("cs-abcdaaaa", state.session_id_candidates)


def test_lookup_of_an_unknown_id(state: Store) -> None:
    with pytest.raises(NotFoundError):
        ids.lookup_session_id("cs-abcdaaaa", state.session_id_candidates)


def test_lookup_of_a_short_prefix_is_a_usage_error_without_a_query(state: Store) -> None:
    statements = record_sql(state)
    with pytest.raises(UsageError):
        ids.lookup_session_id("abc", state.session_id_candidates)
    assert statements == []


@pytest.mark.parametrize(
    "text", ["cs-ab%d", "abc_", "cs-abcd'--", "cs-abcd\n", "cs-abcd0", "ab cd"]
)
def test_lookup_of_malformed_input_is_unknown_without_a_query(state: Store, text: str) -> None:
    add_session(state, "cs-abcdaaaa")
    statements = record_sql(state)
    with pytest.raises(NotFoundError):
        ids.lookup_session_id(text, state.session_id_candidates)
    assert statements == []


# Per-run file facts


def test_record_run_files(state: Store) -> None:
    add_session(state, "cs-abcdaaaa")
    add_run(state, "cs-abcdaaaa", 1)
    add_run(state, "cs-abcdaaaa", 2)
    state.record_run_files(
        "cs-abcdaaaa",
        2,
        secret_sha256="ab" * 32,
        outbox_dev_ino="1:2",
        artifacts_dev_ino="1:3",
    )
    rows = state.conn.execute(
        "SELECT run_no, secret_sha256, outbox_dev_ino, artifacts_dev_ino FROM runs ORDER BY run_no"
    ).fetchall()
    assert rows == [(1, None, None, None), (2, "ab" * 32, "1:2", "1:3")]


def test_record_run_files_for_a_missing_run_fails(state: Store) -> None:
    with pytest.raises(InternalError):
        state.record_run_files(
            "cs-abcdaaaa", 1, secret_sha256="ab" * 32, outbox_dev_ino="1:2", artifacts_dev_ino="1:3"
        )


def test_record_run_files_never_stores_a_non_digest(state: Store) -> None:
    add_session(state, "cs-abcdaaaa")
    add_run(state, "cs-abcdaaaa", 1)
    for bad in ("x" * 64, "AB" * 32, "ab" * 31, "ab" * 32 + "\n"):
        with pytest.raises(ValueError):
            state.record_run_files(
                "cs-abcdaaaa", 1, secret_sha256=bad, outbox_dev_ino="1:2", artifacts_dev_ino="1:3"
            )
    assert state.conn.execute("SELECT secret_sha256 FROM runs").fetchone() == (None,)
