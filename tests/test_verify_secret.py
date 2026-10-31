"""``verify-secret`` (interface.md, Internal commands): full id only, an adopted and live
current run only, trailing whitespace stripped, constant-time compare, exit 0 or 11 and never
an oracle (an unknown session answers exactly like a wrong secret)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from claude_session import cli, secretcheck, store
from claude_session.cli import main
from claude_session.store import NewRun, Store

from .golden import assert_golden
from .schemas import validate

SCHEMA = "claude-session/cli/verify-secret.json"
SID = "cs-abcd2345"
OTHER = "cs-wxyz7777"
SECRET = "0123456789abcdef" * 4
SECRET_SHA = hashlib.sha256(SECRET.encode("ascii")).hexdigest()
WRAPPER: tuple[int, str] = (4242, "boot:1")
CHILD: tuple[int, str] = (4343, "boot:2")
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def alive(monkeypatch: pytest.MonkeyPatch) -> set[tuple[int, str]]:
    """(PID, start-time token) pairs the liveness checks report alive."""
    pairs: set[tuple[int, str]] = {WRAPPER}
    monkeypatch.setattr(store, "pid_alive", lambda pid, start: (pid, start) in pairs)
    monkeypatch.setattr(store, "pid_known_alive", lambda pid, start: (pid, start) in pairs)
    return pairs


@pytest.fixture
def st(alive: set[tuple[int, str]]) -> Iterator[Store]:
    with store.open_state(clock=lambda: T0) as s:
        yield s


def new_run(**fields: Any) -> NewRun:
    base: dict[str, Any] = {
        "state_root": "/state",
        "kind": "implement",
        "reads_third_party": False,
        "mode": "headless",
        "repo": None,
        "model": None,
        "skill": None,
        "has_prompt": True,
        "spec": {},
        "resolved": {},
        "config_hash": None,
        "invocation": {},
        "hook_interpreter": "/usr/bin/env",
        "config_path": None,
        "wrapper_pid": WRAPPER[0],
        "wrapper_start": WRAPPER[1],
    }
    return NewRun(**{**base, **fields})


def adopted(s: Store, sid: str = SID, *, digest: str | None = SECRET_SHA) -> str:
    s.allocate(new_run(), new_id=lambda used: sid)
    if digest is not None:
        s.record_run_files(
            sid, 1, secret_sha256=digest, outbox_dev_ino="1:2", artifacts_dev_ino="1:3"
        )
    return sid


def sql(s: Store, statement: str, *params: Any) -> None:
    with s.transaction() as conn:
        conn.execute(statement, params)


class Stdin:
    """A stand-in for ``sys.stdin`` whose ``buffer`` holds the given bytes."""

    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def with_child(s: Store, child: tuple[int, str] | None = CHILD, sid: str = SID) -> None:
    """Record the run's child (``None``: no child recorded)."""
    pid, start = child if child is not None else (None, None)
    sql(s, "UPDATE runs SET child_pid = ?, child_start = ? WHERE csession_id = ?", pid, start, sid)


def verify(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: bytes,
    *argv: str,
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", Stdin(stdin))
    code = main(["verify-secret", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def run_verify(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Any:
    def go(stdin: bytes, *argv: str) -> tuple[int, str, str]:
        return verify(monkeypatch, capsys, stdin, *argv)

    return go


def assert_no_match(result: tuple[int, str, str]) -> None:
    code, out, err = result
    assert code == 11
    obj = json.loads(out)
    validate(obj, SCHEMA)
    assert obj == {"match": False}
    assert SECRET not in out + err


# A match


def test_the_current_runs_secret_matches(st: Store, run_verify: Any) -> None:
    adopted(st)
    code, out, err = run_verify(SECRET.encode(), SID, "--json")
    assert code == 0
    obj = json.loads(out)
    assert_golden(obj, "verify-secret-match")
    assert SECRET not in out + err


@pytest.mark.parametrize("suffix", [b"\n", b"\r\n", b" \t\n", b"\n\n\n", b"\x0b\x0c "])
def test_trailing_whitespace_is_stripped(st: Store, run_verify: Any, suffix: bytes) -> None:
    adopted(st)
    assert run_verify(SECRET.encode() + suffix, SID, "--json")[0] == 0


def test_without_json_the_exit_code_is_the_answer(st: Store, run_verify: Any) -> None:
    adopted(st)
    assert run_verify(SECRET.encode(), SID) == (0, "", "")
    assert run_verify(b"f" * 64, SID) == (11, "", "")


def test_a_mismatch_golden(st: Store, run_verify: Any) -> None:
    adopted(st)
    code, out, _ = run_verify(b"f" * 64, SID, "--json")
    assert code == 11
    assert_golden(json.loads(out), "verify-secret-mismatch")


# What is presented


@pytest.mark.parametrize(
    "stdin",
    [
        b"",
        b"\n",
        b" " + SECRET.encode(),  # leading whitespace is not stripped
        SECRET.upper().encode(),
        SECRET[:-1].encode(),
        SECRET.encode() + b"0",
        SECRET.encode() + b"\x00",
        SECRET.encode() + b" x",
        SECRET.encode()[:32] + b"\n" + SECRET.encode()[32:],
        SECRET_SHA.encode(),  # the stored digest is not the secret
        "é".encode() + SECRET.encode(),
        b"\xff" * 64,
        b"0" * 64,
        SECRET.encode() + b"\n" + b" " * 10_000,  # stripped but oversized
        SECRET.encode() * 1000,
        b"'; DROP TABLE runs; --",
    ],
)
def test_anything_but_the_secret_is_a_mismatch(st: Store, run_verify: Any, stdin: bytes) -> None:
    adopted(st)
    assert_no_match(run_verify(stdin, SID, "--json"))


def test_stdin_is_read_with_a_bound(st: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    sizes: list[int] = []

    class Bounded(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            sizes.append(-1 if size is None else size)
            return super().read(size)

    stream = Bounded(SECRET.encode() + b"\n")
    assert secretcheck.read_secret(stream) == SECRET.encode()
    assert sizes and all(0 < size <= secretcheck.MAX_INPUT_BYTES + 1 for size in sizes)


# Which sessions and runs can match


@pytest.mark.parametrize(
    "text",
    [
        "abcd2345",  # cs- is required
        "ABCD2345",
        "cs-abcd",  # no prefix matching
        "cs-abcd234",
        "cs-abcd23455",
        "abcd",
        "",
        "cs-abcd2345 ",
        "cs-abcd2345\n",
        "cs-abcd%",
        "-",
        "cs-abcd2345".replace("c", "\u0441"),  # Cyrillic look-alikes
        "cs-\u212aabc2345",
    ],
)
def test_only_the_exact_full_id_matches(st: Store, run_verify: Any, text: str) -> None:
    adopted(st)
    assert_no_match(run_verify(SECRET.encode(), "--json", "--", text))


@pytest.mark.parametrize("text", ["CS-ABCD2345", "cs-ABCD2345", "Cs-AbCd2345"])
def test_the_full_id_is_case_insensitive(st: Store, run_verify: Any, text: str) -> None:
    adopted(st)
    assert run_verify(SECRET.encode(), text, "--json")[0] == 0


def test_a_non_ascii_look_alike_is_not_folded(st: Store, run_verify: Any) -> None:
    """``"\u212a".lower()`` is ``"k"``: only ASCII is folded."""
    adopted(st, "cs-abck2345")
    assert_no_match(run_verify(SECRET.encode(), "cs-abc\u212a2345", "--json"))
    assert run_verify(SECRET.encode(), "CS-ABCK2345", "--json")[0] == 0


def test_an_unknown_session_answers_like_a_wrong_secret(st: Store, run_verify: Any) -> None:
    adopted(st)
    unknown = run_verify(SECRET.encode(), OTHER, "--json")
    wrong = run_verify(b"f" * 64, SID, "--json")
    assert_no_match(unknown)
    assert unknown == wrong


def test_no_database_rows_still_answer_eleven(run_verify: Any) -> None:
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_another_sessions_secret_does_not_match(st: Store, run_verify: Any) -> None:
    adopted(st)
    other_secret = "f" * 64
    adopted(st, OTHER, digest=hashlib.sha256(other_secret.encode()).hexdigest())
    assert_no_match(run_verify(other_secret.encode(), SID, "--json"))
    assert run_verify(other_secret.encode(), OTHER, "--json")[0] == 0


def test_a_forgotten_session_never_matches(st: Store, run_verify: Any) -> None:
    adopted(st)
    sql(st, "UPDATE sessions SET forgotten_at = '2026-10-30T12:00:01.000Z'")
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_an_ended_run_never_matches(st: Store, run_verify: Any) -> None:
    adopted(st)
    sql(st, "UPDATE runs SET ended_at = '2026-10-30T12:00:01.000Z'")
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_a_pending_run_never_matches(st: Store, run_verify: Any) -> None:
    adopted(st)
    sql(st, "UPDATE runs SET wrapper_pid = NULL, wrapper_start = NULL")
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_a_run_whose_wrapper_is_dead_never_matches(
    st: Store, run_verify: Any, alive: set[tuple[int, str]]
) -> None:
    adopted(st)
    alive.clear()
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_a_reused_wrapper_pid_never_matches(
    st: Store, run_verify: Any, alive: set[tuple[int, str]]
) -> None:
    adopted(st)
    alive.clear()
    alive.add((WRAPPER[0], "boot:999"))
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_a_run_whose_child_alone_is_alive_matches(
    st: Store, run_verify: Any, alive: set[tuple[int, str]]
) -> None:
    """An orphaned child (wrapper dead, child alive) keeps the run live (interface.md,
    Liveness)."""
    adopted(st)
    with_child(st)
    alive.clear()
    alive.add(CHILD)
    assert run_verify(SECRET.encode(), SID, "--json")[0] == 0


def test_a_run_whose_wrapper_and_child_are_dead_never_matches(
    st: Store, run_verify: Any, alive: set[tuple[int, str]]
) -> None:
    adopted(st)
    with_child(st)
    alive.clear()
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_a_wrapper_without_a_start_token_with_a_live_child_matches(
    st: Store, run_verify: Any, alive: set[tuple[int, str]]
) -> None:
    adopted(st)
    with_child(st)
    sql(st, "UPDATE runs SET wrapper_start = ''")
    alive.clear()
    alive.update({(WRAPPER[0], ""), CHILD})
    assert run_verify(SECRET.encode(), SID, "--json")[0] == 0
    alive.discard(CHILD)  # an empty start token never counts as alive
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_a_run_whose_liveness_cannot_be_told_never_matches(
    st: Store, run_verify: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    adopted(st)
    with_child(st)
    monkeypatch.setattr(store, "pid_alive", lambda pid, start: True)
    monkeypatch.setattr(store, "pid_known_alive", lambda pid, start: False)
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_a_wrapper_whose_liveness_cannot_be_told_never_matches(
    st: Store, run_verify: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pid_alive`` counts "cannot tell" as alive (never finalize a live run); a secret check
    fails closed instead, so it uses the known-alive check."""
    adopted(st)
    monkeypatch.setattr(store, "pid_alive", lambda pid, start: True)
    monkeypatch.setattr(store, "pid_known_alive", lambda pid, start: False)
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_a_run_without_a_recorded_secret_never_matches(st: Store, run_verify: Any) -> None:
    adopted(st, digest=None)
    assert_no_match(run_verify(b"", SID, "--json"))
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


@pytest.mark.parametrize("digest", ["", "x" * 64, SECRET_SHA.upper(), SECRET_SHA[:-1]])
def test_a_malformed_stored_digest_never_matches(st: Store, run_verify: Any, digest: str) -> None:
    adopted(st)
    sql(st, "UPDATE runs SET secret_sha256 = ?", digest)
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))


def test_only_the_current_runs_secret_matches(st: Store, run_verify: Any) -> None:
    adopted(st)
    new_secret = "e" * 64
    sql(
        st,
        "INSERT INTO runs (csession_id, run_no, created_at, run_kind, wrapper_pid, "
        "wrapper_start, secret_sha256) VALUES (?, 2, '2026-10-30T12:00:02.000Z', 'implement', "
        "?, ?, ?)",
        SID,
        *WRAPPER,
        hashlib.sha256(new_secret.encode()).hexdigest(),
    )
    sql(st, "UPDATE sessions SET current_run = 2")
    assert_no_match(run_verify(SECRET.encode(), SID, "--json"))
    assert run_verify(new_secret.encode(), SID, "--json")[0] == 0


# Constant time and no oracle


def test_the_compare_runs_for_every_answer(
    st: Store, run_verify: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digest compare is ``hmac.compare_digest`` and runs once for every request, also for
    an unknown session, a malformed id and malformed input (no early exit)."""
    adopted(st)
    calls: list[tuple[str, str]] = []
    real = secretcheck.hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(secretcheck.hmac, "compare_digest", spy)
    for stdin, sid in [
        (SECRET.encode(), SID),
        (SECRET.encode(), OTHER),
        (SECRET.encode(), "x"),
        (b"\xff", SID),
        (b"", "cs-abcd"),
    ]:
        calls.clear()
        run_verify(stdin, sid, "--json")
        assert len(calls) == 1
        assert all(len(side) == 64 for side in calls[0])


class RecordingProcInfo:
    """A ``procinfo.ProcInfo`` stand-in that records every liveness probe."""

    def __init__(self, alive: set[tuple[int, str]]) -> None:
        self.alive = alive
        self.calls: list[tuple[int, str]] = []

    def is_alive(self, pid: int, start: str) -> bool:
        self.calls.append((pid, start))
        return (pid, start) in self.alive


def test_every_answer_does_the_same_work(run_verify: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown, forgotten, pending and ended runs, a malformed id, no recorded digest, dead
    processes, a wrong and the right secret: each does one lookup, one compare and the same
    two liveness probes through the real ``pid_known_alive`` and procinfo path, so timing does
    not tell whether a session exists or what state its run is in."""
    fake = RecordingProcInfo({WRAPPER})
    lookups = {"procinfo": 0, "secret_run": 0, "compare": 0}

    def procinfo() -> RecordingProcInfo:
        lookups["procinfo"] += 1
        return fake

    real_secret_run = Store.secret_run
    real_compare = secretcheck.hmac.compare_digest

    def secret_run(self: Store, csession_id: str) -> Any:
        lookups["secret_run"] += 1
        return real_secret_run(self, csession_id)

    def compare(a: Any, b: Any) -> bool:
        lookups["compare"] += 1
        return real_compare(a, b)

    with store.open_state(clock=lambda: T0) as s:
        adopted(s)  # live, wrapper only
        adopted(s, "cs-aaaa2222")  # forgotten
        sql(s, "UPDATE sessions SET forgotten_at = created_at WHERE csession_id = 'cs-aaaa2222'")
        adopted(s, "cs-bbbb2222")  # pending
        sql(
            s,
            "UPDATE runs SET wrapper_pid = NULL, wrapper_start = NULL "
            "WHERE csession_id = 'cs-bbbb2222'",
        )
        adopted(s, "cs-cccc2222")  # ended
        sql(s, "UPDATE runs SET ended_at = created_at WHERE csession_id = 'cs-cccc2222'")
        adopted(s, "cs-dddd2222", digest=None)  # no recorded secret
        adopted(s, "cs-eeee2222")  # wrapper and child dead
        with_child(s, (4444, "boot:4"), sid="cs-eeee2222")
        sql(s, "UPDATE runs SET wrapper_pid = 4545 WHERE csession_id = 'cs-eeee2222'")
        adopted(s, "cs-ffff2222")  # live child, wrapper without a start token
        with_child(s, CHILD, sid="cs-ffff2222")
        sql(s, "UPDATE runs SET wrapper_start = '' WHERE csession_id = 'cs-ffff2222'")
    fake.alive.add(CHILD)
    monkeypatch.setattr(store, "system_procinfo", procinfo)
    monkeypatch.setattr(Store, "secret_run", secret_run)
    monkeypatch.setattr(secretcheck.hmac, "compare_digest", compare)
    cases = [
        (OTHER, SECRET, 11),  # unknown
        ("cs-aaaa2222", SECRET, 11),
        ("cs-bbbb2222", SECRET, 11),
        ("cs-cccc2222", SECRET, 11),
        ("cs-dddd2222", SECRET, 11),
        ("cs-eeee2222", SECRET, 11),
        ("x", SECRET, 11),  # malformed id
        (SID, "f" * 64, 11),  # live, wrong secret
        (SID, SECRET, 0),  # live, right secret
        ("cs-ffff2222", SECRET, 0),  # live through the child
    ]
    for sid, secret, code in cases:
        fake.calls.clear()
        for key in lookups:
            lookups[key] = 0
        assert run_verify(secret.encode(), sid, "--json")[0] == code, sid
        assert lookups == {"procinfo": 2, "secret_run": 1, "compare": 1}, sid
        assert len(fake.calls) == 2, sid
        assert all(start != "" for _, start in fake.calls), sid


def test_the_secret_is_never_printed(st: Store, run_verify: Any) -> None:
    adopted(st)
    near = SECRET[:-1] + "0"
    for stdin, argv in [
        (SECRET.encode(), [SID, "--json"]),
        (SECRET.encode(), [SID]),
        (near.encode(), [SID, "--json"]),
        (near.encode(), [OTHER]),
    ]:
        _, out, err = run_verify(stdin, *argv)
        assert SECRET not in out + err and near not in out + err
        assert SECRET_SHA not in out + err


# argv


@pytest.mark.parametrize(
    "flag",
    [
        "--dry-run",
        "--secret=x",
        "--run=1",
        # Global flags do not apply to internal commands (interface.md, Global flags).
        "--yes",
        "--quiet",
        "--verbose",
        "--version",
        "--config=/dev/null",
    ],
)
def test_unknown_flags_are_usage_errors(flag: str, run_verify: Any) -> None:
    code, out, err = run_verify(SECRET.encode(), SID, "--json", flag)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage"
    assert SECRET not in out + err


def test_config_with_a_value_is_a_usage_error(run_verify: Any) -> None:
    code, out, _ = run_verify(SECRET.encode(), SID, "--json", "--config", "/dev/null")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage"


@pytest.mark.parametrize(
    "argv",
    [
        [SID, SECRET],
        [SID, "--json", SECRET],
        [SID, "--" + SECRET],
        [SID, "--json=" + SECRET],
        [SID, "--json", "--", SECRET],
        [SID, "-" + SECRET],
    ],
)
@pytest.mark.parametrize("json_flag", [True, False])
def test_a_secret_on_argv_is_never_echoed(
    run_verify: Any, argv: list[str], json_flag: bool
) -> None:
    if not json_flag:
        argv = [arg for arg in argv if arg != "--json"]
    code, out, err = run_verify(b"", *argv)
    assert code == 2
    assert SECRET not in out + err
    if "--json" in argv:
        assert json.loads(out)["error"]["code"] == "usage"


def test_a_terminal_on_stdin_is_a_usage_error_without_reading(
    st: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adopted(st)

    class Terminal(io.BytesIO):
        def isatty(self) -> bool:
            return True

        def read(self, size: int | None = -1) -> bytes:
            raise AssertionError("read from a terminal")

    class TerminalStdin:
        buffer = Terminal()

    monkeypatch.setattr(sys, "stdin", TerminalStdin())
    for argv in ([SID, "--json"], [OTHER, "--json"], ["x", "--json"]):
        code = main(["verify-secret", *argv])
        out = capsys.readouterr().out
        assert code == 2
        error = json.loads(out)["error"]
        assert error["code"] == "usage"
        assert "terminal" in error["message"]


def test_the_id_is_required(run_verify: Any) -> None:
    assert run_verify(SECRET.encode(), "--json")[0] == 2


def test_verify_secret_is_hidden_from_help() -> None:
    assert "verify-secret" not in cli.build_parser().format_help()


def test_a_read_error_is_an_error_without_the_secret(
    st: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adopted(st)

    class Broken:
        def isatty(self) -> bool:
            return False

        def read(self, size: int = -1) -> bytes:
            raise OSError(5, "Input/output error")

    class BrokenStdin:
        buffer = Broken()

    monkeypatch.setattr(sys, "stdin", BrokenStdin())
    code = main(["verify-secret", SID, "--json"])
    out = capsys.readouterr().out
    assert code == 11
    assert json.loads(out) == {"match": False}
