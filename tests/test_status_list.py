"""``status`` and ``list`` (interface.md, status and list): JSON per schema (golden files),
filters, id resolution and the safety of human and JSON output for any stored value."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from claude_session import cli, store, terminal, views
from claude_session.cli import main
from claude_session.outbox import Frozen
from claude_session.store import Final, NewRun, Store

from .golden import assert_golden
from .schemas import validate

T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
STATUS_SCHEMA = "claude-session/cli/status.json"
LIST_SCHEMA = "claude-session/cli/list.json"
ERROR_SCHEMA = "claude-session/cli/error.json"
SHA = "ab" * 32
HOSTILE = (
    "evil\x1b]0;pwned\x07\x1b[2J\r\nfake: line\x9b31m\u202eetc\u2028x\x00\x7f"
    "\x1b]8;;https://example.org/\x1b\\link\x1b]8;;\x1b\\"  # OSC 8 hyperlink
    "\x1b]52;c;ZXZpbA==\x07"  # OSC 52 clipboard write
    "zero\u200bwidth\ufeffbom\u00adshy\u2066iso\u2069\u2029para"
    "\\x1b fake escape"  # a literal backslash: must not pass for an escape
)
# A JSON-escaped lone surrogate: json.loads yields a str that cannot be encoded as UTF-8.
SURROGATE_JSON = '["w \\ud800 x"]'


class Clock:
    """A settable clock for the seeding store."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float = 1.0) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def alive(monkeypatch: pytest.MonkeyPatch) -> set[tuple[int, str]]:
    """(PID, start-time token) pairs the liveness checks report alive (every other process,
    including a reused PID with another token, is gone)."""
    pairs: set[tuple[int, str]] = set()
    monkeypatch.setattr(store, "pid_alive", lambda pid, start: (pid, start) in pairs)
    monkeypatch.setattr(store, "pid_known_alive", lambda pid, start: (pid, start) in pairs)
    return pairs


@pytest.fixture
def st(clock: Clock, alive: set[tuple[int, str]]) -> Iterator[Store]:
    with store.open_state(clock=clock) as s:
        yield s


def new_run(**fields: Any) -> NewRun:
    base: dict[str, Any] = {
        "state_root": "/state",
        "kind": "implement",
        "reads_third_party": False,
        "mode": "headless",
        "repo": "example.org/group/project",
        "model": None,
        "skill": None,
        "has_prompt": True,
        "spec": {},
        "resolved": {"kind": "implement", "profile": {"sandbox": "off", "implicit_done": True}},
        "config_hash": None,
        "invocation": {"input_files": ["notes.md"], "decision_ids": []},
        "hook_interpreter": "/usr/bin/env",
        "config_path": None,
        "wrapper_pid": 4242,
        "wrapper_start": "boot:1",
        "labels": ("team=core",),
    }
    return NewRun(**{**base, **fields})


def allocate(s: Store, sid: str, clock: Clock, **fields: Any) -> str:
    clock.tick()
    return s.allocate(new_run(**fields), new_id=lambda used: sid)


def sql(s: Store, statement: str, *params: Any) -> None:
    with s.transaction() as conn:
        conn.execute(statement, params)


def to_working(s: Store, sid: str, clock: Clock, *, child: int = 5151) -> None:
    clock.tick()
    s.update_run(
        sid, 1, cwd="/work/project", phase="session", child_pid=child, started_at=_ts(clock)
    )
    s.update_run(sid, 1, child_start="boot:2", sandbox_active=0, permission_profile="default")
    clock.tick()
    with s.transaction():
        run = s.hook_run(sid)
        assert run is not None
        s.record_session_start(
            run,
            claude_session_id="c0ffee00-0000-4000-8000-000000000001",
            source="startup",
            transcript_path="/home/u/.claude/projects/p/c0ffee.jsonl",
        )
        s.apply_state(
            run, state="working", activity="working", needs_input_reason=None, cause="SessionStart"
        )


def report_done(s: Store, sid: str, clock: Clock, summary: str = "All tests pass.") -> None:
    clock.tick()
    with s.transaction():
        run = s.hook_run(sid)
        assert run is not None
        s.set_report(
            sid,
            1,
            status="done",
            summary=summary,
            reason=None,
            artifact=Frozen(
                name="result.md",
                path=f"/state/private/{sid}/runs/1/artifact/1/result.md",
                sha256=SHA,
                size=12,
                ingest_seq=1,
            ),
        )
        s.apply_state(
            run,
            state="reported",
            activity="idle",
            needs_input_reason=None,
            cause="report",
            always=True,
        )


def finalize(s: Store, sid: str, clock: Clock, final: Final) -> None:
    clock.tick()
    assert s.finalize_run(sid, 1, final)


DONE = Final(
    phase="session",
    outcome="done",
    end_reason="normal",
    exit_code=0,
    state="exited",
    cause="exit",
    cost_usd=0.125,
    num_turns=3,
    is_error=False,
)
NOT_STARTED = Final(
    phase="launch",
    outcome="not-started",
    end_reason="claude_not_found",
    exit_code=12,
    state="not-started",
    cause="exit",
    message="claude not found on PATH",
)


def _ts(clock: Clock) -> str:
    return store.format_ts(clock())


def run_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def seed(st: Store, clock: Clock, alive: set[tuple[int, str]]) -> None:
    """Four sessions: working (live), done (final, reported), not started (launch failure),
    pending (start, not adopted)."""
    allocate(st, "cs-aaaaaaaa", clock, labels=("team=core", "prio=high"))
    to_working(st, "cs-aaaaaaaa", clock)
    alive.update({(4242, "boot:1"), (5151, "boot:2")})
    allocate(
        st,
        "cs-bbbbbbbb",
        clock,
        kind="review",
        wrapper_pid=4343,
        reads_third_party=True,
        mode="headless",
        labels=("team=web",),
    )
    to_working(st, "cs-bbbbbbbb", clock, child=6161)
    report_done(st, "cs-bbbbbbbb", clock)
    finalize(st, "cs-bbbbbbbb", clock, DONE)
    # Its wrapper's PID now belongs to another process (cs-aaaaaaaa's): not alive.
    allocate(
        st,
        "cs-cccccccc",
        clock,
        mode="interactive",
        repo=None,
        labels=(),
        wrapper_start="boot:0",
    )
    finalize(st, "cs-cccccccc", clock, NOT_STARTED)
    allocate(st, "cs-dddddddd", clock, labels=("team=core",))
    sql(
        st,
        "UPDATE runs SET wrapper_pid = NULL, wrapper_start = NULL, expires_at = ? "
        "WHERE csession_id = 'cs-dddddddd'",
        store.format_ts(T0 + timedelta(minutes=10)),
    )
    sql(st, "UPDATE sessions SET forked_from = 'cs-bbbbbbbb' WHERE csession_id = 'cs-dddddddd'")


@pytest.fixture
def seeded(st: Store, clock: Clock, alive: set[tuple[int, str]]) -> Store:
    seed(st, clock, alive)
    return st


# status --json: golden files


@pytest.mark.parametrize(
    ("sid", "case"),
    [
        ("cs-aaaaaaaa", "working"),
        ("cs-bbbbbbbb", "done"),
        ("cs-cccccccc", "not-started"),
        ("cs-dddddddd", "pending"),
    ],
)
def test_status_json_matches_the_schema_and_golden(
    seeded: Store, sid: str, case: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, obj = run_json(["status", sid], capsys)
    assert code == 0
    assert_golden(obj, f"status-{case}", normalize=_normalize_status)


def _normalize_status(obj: dict[str, Any]) -> dict[str, Any]:
    obj = json.loads(json.dumps(obj))
    root = str(Path(store.state_dir()))
    obj["csession_dir"] = obj["csession_dir"].replace(root, "<state>")
    return obj


def test_status_of_a_live_working_run(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    _, obj = run_json(["status", "cs-aaaaaaaa"], capsys)
    run = obj["run"]
    assert (obj["state"], obj["activity"], obj["live"], obj["pending"]) == (
        "working",
        "working",
        True,
        False,
    )
    assert (run["wrapper_alive"], run["child_alive"]) == (True, True)
    assert (run["outcome"], run["ended_at"], run["exit_code"]) == (None, None, None)
    assert obj["labels"] == ["kind=implement", "prio=high", "team=core"]
    assert run["result_path"] == "/state/private/cs-aaaaaaaa/runs/1/result.json"
    assert run["input_files"] == ["notes.md"] and run["implicit_done"] is True
    assert [e["cause"] for e in obj["recent_events"]] == ["launch", "SessionStart"]
    assert obj["claude_session_ids"][0]["source"] == "startup"


def test_status_of_a_final_reported_run(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    _, obj = run_json(["status", "cs-bbbbbbbb"], capsys)
    run = obj["run"]
    assert (obj["state"], obj["live"], run["state"]) == ("exited", False, "exited")
    assert run["report"]["summary"] == "All tests pass."
    assert run["report"]["artifact"]["sha256"] == SHA
    assert (run["outcome"], run["exit_code"], run["cost_usd"]) == ("done", 0, 0.125)
    assert (run["wrapper_alive"], run["child_alive"]) == (False, False)
    assert obj["tainted"] is True and obj["tainted_by_run"] == 1
    assert obj["recent_events"][-1]["final"] is True
    assert obj["recent_events"][-1]["delivered"] is False


def test_status_of_a_launch_failure_has_no_cost_and_a_message(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    _, obj = run_json(["status", "cs-cccccccc"], capsys)
    run = obj["run"]
    assert (run["phase"], run["state"], run["outcome"]) == ("launch", "not-started", "not-started")
    assert run["message"] == "claude not found on PATH"
    assert run["cost_usd"] is None and run["result_path"] is None  # interactive


def test_status_of_a_pending_run(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    _, obj = run_json(["status", "cs-dddddddd"], capsys)
    assert obj["pending"] is True and obj["run"]["pending"] is True
    assert obj["expires_at"] == obj["run"]["expires_at"] is not None
    assert (obj["run"]["phase"], obj["run"]["state"], obj["run"]["started_at"]) == (
        "launch",
        "starting",
        None,
    )
    assert obj["forked_from"] == "cs-bbbbbbbb"


def test_status_accepts_a_prefix_any_case_with_or_without_cs(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    for text in ("cs-aaaa", "AAAAA", "Cs-AaAaAaAa"):
        code, obj = run_json(["status", text], capsys)
        assert (code, obj["csession_id"]) == (0, "cs-aaaaaaaa")


def test_status_unknown_session_exits_10(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    code, envelope = run_json(["status", "cs-zzzz"], capsys)
    assert code == 10
    validate(envelope, ERROR_SCHEMA)
    assert envelope["error"]["code"] == "not_found"


def test_status_malformed_id_is_unknown(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    for text in ("cs-aaaa'; DROP TABLE sessions; --", "cs-aaaa%", "cs-aaa_", "cs-aaaa\x00"):
        code, envelope = run_json(["status", text], capsys)
        assert code == 10, text
    assert seeded.conn.execute("SELECT COUNT(*) FROM sessions").fetchone() == (4,)


def test_status_short_prefix_exits_2(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    code, envelope = run_json(["status", "aaa"], capsys)
    assert (code, envelope["error"]["code"]) == (2, "usage")


def test_status_ambiguous_prefix_exits_21_with_matches(
    st: Store, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    allocate(st, "cs-abcdaaaa", clock)
    allocate(st, "cs-abcdbbbb", clock)
    code, envelope = run_json(["status", "abcd"], capsys)
    assert code == 21
    validate(envelope, ERROR_SCHEMA)
    assert envelope["error"]["matches"] == ["cs-abcdaaaa", "cs-abcdbbbb"]


def test_status_forgotten_session_is_unknown(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        seeded,
        "UPDATE sessions SET forgotten_at = ? WHERE csession_id = 'cs-aaaaaaaa'",
        "2026-10-30T13:00:00.000000Z",
    )
    code, _ = run_json(["status", "cs-aaaaaaaa"], capsys)
    assert code == 10


@pytest.mark.parametrize("run", ["2", "0", "-1", "99999999999999999999999"])
def test_status_run_out_of_range_exits_10(
    seeded: Store, run: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, envelope = run_json(["status", "cs-aaaaaaaa", "--run", run], capsys)
    assert code == 10
    assert envelope["error"]["code"] == "not_found"


def test_status_run_selects_an_earlier_run(
    seeded: Store, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    # A second run of cs-bbbbbbbb (resume is #18; inserted directly here).
    sql(
        seeded,
        "INSERT INTO runs (csession_id, run_no, created_at, run_kind, mode, phase, "
        "wrapper_pid, wrapper_start, cwd) VALUES ('cs-bbbbbbbb', 2, ?, 'implement', 'headless', "
        "'launch', 4242, 'boot:1', '/work')",
        _ts(clock),
    )
    sql(
        seeded,
        "UPDATE sessions SET current_run = 2, state = 'starting', activity = NULL "
        "WHERE csession_id = 'cs-bbbbbbbb'",
    )
    code, obj = run_json(["status", "cs-bbbbbbbb", "--run", "1"], capsys)
    assert code == 0
    validate(obj, STATUS_SCHEMA)
    assert (obj["current_run"], obj["runs"], obj["run_kind"], obj["live"]) == (
        2,
        2,
        "implement",
        True,
    )
    assert (obj["run"]["run"], obj["run"]["run_kind"], obj["run"]["state"]) == (
        1,
        "review",
        "exited",
    )
    assert obj["run"]["state_since"] == obj["run"]["ended_at"]
    _, current = run_json(["status", "cs-bbbbbbbb"], capsys)
    assert (current["run"]["run"], current["run"]["state"]) == (2, "starting")


def test_status_shows_the_last_20_events_oldest_first(
    st: Store, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    allocate(st, "cs-aaaaaaaa", clock)
    to_working(st, "cs-aaaaaaaa", clock)
    for index in range(30):
        clock.tick()
        with st.transaction():
            run = st.hook_run("cs-aaaaaaaa")
            assert run is not None
            activity = "idle" if index % 2 == 0 else "working"
            st.apply_state(
                run,
                state=activity,
                activity=activity,
                needs_input_reason=None,
                cause="Stop" if activity == "idle" else "UserPromptSubmit",
            )
    _, obj = run_json(["status", "cs-aaaaaaaa"], capsys)
    ids = [event["event_id"] for event in obj["recent_events"]]
    assert len(ids) == 20 and ids == sorted(ids)
    (latest,) = st.conn.execute("SELECT MAX(id) FROM events").fetchone()
    assert ids[-1] == latest


def test_status_warns_when_a_live_runs_hook_interpreter_is_gone(
    seeded: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gone = str(tmp_path / "old-venv" / "python3")
    sql(seeded, "UPDATE runs SET hook_interpreter = ? WHERE csession_id = 'cs-aaaaaaaa'", gone)
    sql(seeded, "UPDATE runs SET hook_interpreter = ? WHERE csession_id = 'cs-bbbbbbbb'", gone)
    _, live = run_json(["status", "cs-aaaaaaaa"], capsys)
    assert len(live["warnings"]) == 1 and gone in live["warnings"][0]
    _, final = run_json(["status", "cs-bbbbbbbb"], capsys)
    assert final["warnings"] == []  # only live runs still run hooks


def test_status_human_output(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "cs-bbbbbbbb"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "session:        cs-bbbbbbbb" in captured.out
    assert "  outcome:      done (normal), exit 0" in captured.out
    assert "    summary:    All tests pass." in captured.out


# Untrusted stored values


def poison(s: Store, sid: str) -> None:
    """Hostile text in every agent-influenced column of ``sid``'s run 1."""
    sql(
        s,
        "UPDATE runs SET report_status = 'done', report_implicit = 0, report_summary = ?, "
        "report_reason = ?, artifact_name = ?, artifact_path = ?, artifact_sha256 = ?, "
        "artifact_size = 1, reported_at = ?, message = ?, warnings_json = ?, cwd = ? "
        "WHERE csession_id = ? AND run_no = 1",
        HOSTILE,
        HOSTILE,
        HOSTILE,
        HOSTILE,
        SHA,
        "2026-10-30T12:00:00.000000Z",
        HOSTILE,
        json.dumps([HOSTILE, "w \ud800 x"]),
        HOSTILE,
        sid,
    )
    sql(s, "INSERT INTO labels (csession_id, key, value) VALUES (?, 'x', ?)", sid, HOSTILE)


def _unsafe_char(char: str) -> bool:
    """Whether ``char`` must never reach a terminal from a stored value (a newline only
    separates output lines, so callers check it by line)."""
    code = ord(char)
    return (
        (code < 0x20 and char != "\n")
        or 0x7F <= code <= 0x9F
        or 0xD800 <= code <= 0xDFFF
        or char in "\u2028\u2029\u034f\u115f\u1160\u3164\uffa0\u180e"
        or unicodedata.category(char) == "Cf"
    )


def assert_inert(text: str) -> None:
    bad = sorted({repr(char) for char in text if _unsafe_char(char)})
    assert not bad, bad
    assert "\nfake: line" not in text
    text.encode("utf-8")  # strict: no lone surrogate


def test_status_human_output_escapes_control_and_escape_sequences(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    poison(seeded, "cs-aaaaaaaa")
    assert main(["status", "cs-aaaaaaaa"]) == 0
    out = capsys.readouterr().out
    assert_inert(out)
    assert "evil\\x1b]0;pwned\\x07\\x1b[2J\\x0d\\x0afake: line\\x9b31m\\u202eetc" in out
    assert "zero\\u200bwidth\\ufeffbom\\u00adshy\\u2066iso\\u2069\\u2029para" in out
    assert "\\\\x1b fake escape" in out  # the backslash itself is escaped
    assert "w \ufffd x" in out  # a lone surrogate from JSON is U+FFFD


def test_list_human_output_escapes_labels(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    poison(seeded, "cs-aaaaaaaa")
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert_inert(out)
    assert "\\x1b]8;;https://example.org/\\x1b\\\\link" in out


def test_status_human_output_with_a_lone_surrogate_exits_0(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        seeded,
        "UPDATE runs SET warnings_json = ? WHERE csession_id = 'cs-aaaaaaaa'",
        SURROGATE_JSON,
    )
    assert main(["status", "cs-aaaaaaaa"]) == 0
    out = capsys.readouterr().out
    out.encode("utf-8")
    assert "  warning:      w \ufffd x" in out


def test_error_messages_escape_hostile_input(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status", "cs-aaaa\u202e\x1b]52;c;eA==\x07\\x41\u200b"]) == 10
    err = capsys.readouterr().err
    assert_inert(err)
    assert "cs-aaaa\\u202e\\x1b]52;c;eA==\\x07\\\\x41\\u200b" in err


def test_error_message_names_the_stored_id_escaped(
    st: Store, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    allocate(st, "cs-aaaaaaaa", clock)
    sql(st, "UPDATE sessions SET current_run = 3 WHERE csession_id = 'cs-aaaaaaaa'")
    assert main(["status", "cs-aaaa"]) == 10
    err = capsys.readouterr().err
    assert_inert(err)
    assert err == "claude-session: error: session cs-aaaaaaaa has no current run record\n"


def test_verbose_traceback_is_escaped_line_by_line(
    seeded: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args: object) -> None:
        raise RuntimeError(HOSTILE)

    monkeypatch.setattr(views, "status_object", boom)
    assert main(["status", "cs-aaaa", "--verbose"]) == 1
    err = capsys.readouterr().err
    assert_inert(err)
    lines = err.splitlines()
    assert lines[0] == "Traceback (most recent call last):"
    assert any(line.startswith("RuntimeError: evil\\x1b]0;pwned") for line in lines)
    assert lines[-1] == "claude-session: error: internal error: RuntimeError"


def test_json_output_carries_hostile_text_escaped(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    poison(seeded, "cs-aaaaaaaa")
    assert main(["status", "cs-aaaaaaaa", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.isascii() and "\x1b" not in out
    obj = json.loads(out)
    assert obj["run"]["report"]["summary"] == HOSTILE
    assert obj["run"]["warnings"] == [HOSTILE, "w \ufffd x"]
    assert main(["list", "--json"]) == 0
    sessions = json.loads(capsys.readouterr().out)["sessions"]
    poisoned = next(item for item in sessions if item["csession_id"] == "cs-aaaaaaaa")
    assert f"x={HOSTILE}" in poisoned["labels"]


def test_json_output_never_carries_a_lone_surrogate(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        seeded,
        "UPDATE runs SET warnings_json = ?, decision_ids_json = ?, invocation_json = ? "
        "WHERE csession_id = 'cs-aaaaaaaa'",
        SURROGATE_JSON,
        SURROGATE_JSON,
        json.dumps({"input_files": ["\udfff.md"]}),
    )
    assert main(["status", "cs-aaaaaaaa", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    validate(obj, STATUS_SCHEMA)
    assert obj["run"]["warnings"] == obj["run"]["decision_ids"] == ["w \ufffd x"]
    assert obj["run"]["input_files"] == ["\ufffd.md"]
    json.dumps(obj, ensure_ascii=False).encode("utf-8")


def test_json_error_envelope_never_carries_a_lone_surrogate(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    # An argv byte that is not UTF-8 arrives surrogate-escaped.
    text = b"cs-aaaa\xff".decode("utf-8", errors="surrogateescape")
    assert main(["status", text, "--json"]) == 10
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    validate(envelope, ERROR_SCHEMA)
    assert envelope["error"]["message"] == "unknown session: cs-aaaa\ufffd"
    captured.err.encode("utf-8")
    assert "cs-aaaa\\udcff" in captured.err


def test_json_stays_valid_for_bytes_that_are_not_utf8(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = b"bad \xff\xfe utf8 \xc3"
    # Stored as TEXT (cast) and as a BLOB: both are read without an error.
    sql(
        seeded,
        "UPDATE runs SET report_status = 'done', report_implicit = 0, "
        "report_summary = CAST(? AS TEXT), report_reason = ?, reported_at = ?, "
        "message = CAST(? AS TEXT) WHERE csession_id = 'cs-aaaaaaaa'",
        raw,
        raw,
        "2026-10-30T12:00:00.000000Z",
        raw,
    )
    sql(
        seeded,
        "INSERT INTO labels (csession_id, key, value) VALUES ('cs-aaaaaaaa', 'k', CAST(? AS TEXT))",
        raw,
    )
    assert main(["status", "cs-aaaaaaaa", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["run"]["report"]["summary"] == "bad \ufffd\ufffd utf8 \ufffd"
    assert obj["run"]["report"]["reason"] == "bad \ufffd\ufffd utf8 \ufffd"
    assert main(["list", "--json"]) == 0
    json.loads(capsys.readouterr().out)
    assert main(["status", "cs-aaaaaaaa"]) == 0
    assert main(["list"]) == 0
    # The shared connection's text decoding is restored afterwards.
    assert seeded.conn.text_factory is str


def test_non_finite_cost_is_null(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    sql(seeded, "UPDATE runs SET cost_usd = 9e999 WHERE csession_id = 'cs-bbbbbbbb'")
    code, obj = run_json(["status", "cs-bbbbbbbb"], capsys)
    assert (code, obj["run"]["cost_usd"]) == (0, None)


def test_corrupt_json_columns_do_not_fail(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        seeded,
        "UPDATE runs SET warnings_json = '{', decision_ids_json = '[1, null, \"d\"]', "
        "invocation_json = 'null', resolved_json = '[]' WHERE csession_id = 'cs-aaaaaaaa'",
    )
    code, obj = run_json(["status", "cs-aaaaaaaa"], capsys)
    assert code == 0
    validate(obj, STATUS_SCHEMA)
    assert obj["run"]["warnings"] == [] and obj["run"]["decision_ids"] == ["1", "d"]
    assert obj["run"]["input_files"] == [] and obj["run"]["sandbox"] == "off"


def test_display_escapes_controls_and_keeps_text() -> None:
    assert terminal.display("héllo wörld ✓ 審査") == "héllo wörld ✓ 審査"
    assert terminal.display("a\tb\x85c\u200fd") == "a\\x09b\\x85c\\u200fd"
    assert terminal.display(None) == "-"
    assert terminal.display("a\\x1b") == "a\\\\x1b"
    assert terminal.display("\ud800\u2028\u00ad\U000e0001\u3164") == (
        "\\ud800\\u2028\\u00ad\\U000e0001\\u3164"
    )
    assert terminal.display("a b,c", extra=" ,") == "a\\x20b\\x2cc"


def test_display_width_counts_wide_characters_twice() -> None:
    assert terminal.display_width("ab") == 2
    assert terminal.display_width("審査") == 4
    assert terminal.display_width("e\u0301") == 1


# list --json


def test_list_json_matches_the_schema_and_golden(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    code, obj = run_json(["list"], capsys)
    assert code == 0
    assert_golden(obj, "list-sessions")


def test_list_is_most_recently_updated_first_and_hides_forgotten(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(seeded, "UPDATE sessions SET forgotten_at = updated_at WHERE csession_id = 'cs-cccccccc'")
    _, obj = run_json(["list"], capsys)
    assert [s["csession_id"] for s in obj["sessions"]] == [
        "cs-dddddddd",
        "cs-bbbbbbbb",
        "cs-aaaaaaaa",
    ]


def listed(argv: list[str], capsys: pytest.CaptureFixture[str]) -> list[str]:
    code, obj = run_json(["list", *argv], capsys)
    assert code == 0, obj
    validate(obj, LIST_SCHEMA)
    return [item["csession_id"] for item in obj["sessions"]]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--label", "team=core"], ["cs-dddddddd", "cs-aaaaaaaa"]),
        (["--label", "team=core", "--label", "prio=high"], ["cs-aaaaaaaa"]),
        (["--label", "team=core", "--label", "team=web"], []),
        (["--label", "kind=review"], ["cs-bbbbbbbb"]),
        (["--label", "team="], []),
        (["--state", "working"], ["cs-aaaaaaaa"]),
        (["--state", "working", "--state", "exited"], ["cs-bbbbbbbb", "cs-aaaaaaaa"]),
        (["--state", "needs-input"], []),
        (["--kind", "review"], ["cs-bbbbbbbb"]),
        (["--launch-kind", "implement"], ["cs-dddddddd", "cs-cccccccc", "cs-aaaaaaaa"]),
        (["--run-kind", "review"], ["cs-bbbbbbbb"]),
        (["--mode", "interactive"], ["cs-cccccccc"]),
        (["--mode", "headless"], ["cs-dddddddd", "cs-bbbbbbbb", "cs-aaaaaaaa"]),
        (["--needs-input"], []),
        (["--pending"], ["cs-dddddddd"]),
        (["--repo", "example.org/group/project"], ["cs-dddddddd", "cs-bbbbbbbb", "cs-aaaaaaaa"]),
        (["--repo", "example.org/group"], []),
        (["--limit", "2"], ["cs-dddddddd", "cs-cccccccc"]),
        (["--limit", "1", "--state", "exited"], ["cs-bbbbbbbb"]),
        (["--forked-from", "cs-bbbb"], ["cs-dddddddd"]),
        (["--forked-from", "aaaaaaaa"], []),
        (["--report-status", "done"], ["cs-bbbbbbbb"]),
        (["--report-status", "none"], ["cs-dddddddd", "cs-cccccccc", "cs-aaaaaaaa"]),
        (["--report-status", "failed"], []),
        (["--report-status", "failed", "--report-status", "done"], ["cs-bbbbbbbb"]),
        (
            ["--report-status", "done", "--report-status", "none"],
            ["cs-dddddddd", "cs-cccccccc", "cs-bbbbbbbb", "cs-aaaaaaaa"],
        ),
        (["--report-status", "blocked", "--report-status", "needs-input"], []),
        (["--live"], ["cs-dddddddd", "cs-aaaaaaaa"]),
        (["--tainted"], ["cs-bbbbbbbb"]),
        (["--live", "--tainted"], []),
        (["--pending", "--live", "--label", "team=core"], ["cs-dddddddd"]),
    ],
)
def test_list_filters(
    seeded: Store, argv: list[str], expected: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert listed(argv, capsys) == expected


def test_list_kind_matches_any_run_kind(
    seeded: Store, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        seeded,
        "INSERT INTO runs (csession_id, run_no, created_at, run_kind, mode, phase, "
        "wrapper_pid, wrapper_start) VALUES ('cs-cccccccc', 2, ?, 'respond', 'headless', "
        "'launch', 4242, 'boot:1')",
        _ts(clock),
    )
    sql(seeded, "UPDATE sessions SET current_run = 2 WHERE csession_id = 'cs-cccccccc'")
    sql(
        seeded,
        "INSERT INTO runs (csession_id, run_no, created_at, run_kind, mode, phase) "
        "VALUES ('cs-aaaaaaaa', 2, ?, 'respond', 'headless', 'launch')",
        _ts(clock),
    )
    assert listed(["--kind", "respond"], capsys) == ["cs-cccccccc", "cs-aaaaaaaa"]
    assert listed(["--kind", "implement"], capsys) == ["cs-dddddddd", "cs-cccccccc", "cs-aaaaaaaa"]
    assert listed(["--run-kind", "respond"], capsys) == ["cs-cccccccc"]
    assert listed(["--launch-kind", "respond"], capsys) == []


def test_list_since_matches_updated_at(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    rows = dict(seeded.conn.execute("SELECT csession_id, updated_at FROM sessions"))
    cut = rows["cs-cccccccc"]
    assert listed(["--since", cut], capsys) == ["cs-dddddddd", "cs-cccccccc"]
    assert listed(["--since", cut.replace("Z", "+00:00")], capsys) == ["cs-dddddddd", "cs-cccccccc"]
    # Naive times are UTC; an offset is honored.
    naive = cut.removesuffix("Z")
    assert listed(["--since", naive], capsys) == ["cs-dddddddd", "cs-cccccccc"]
    later = (store.parse_ts(cut) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f+01:00")
    assert listed(["--since", later], capsys) == ["cs-dddddddd", "cs-cccccccc"]
    assert listed(["--since", "2999-01-01"], capsys) == []
    # A duration: seeded times are T0-relative, far in the past of the real clock.
    assert listed(["--since", "1h"], capsys) == []
    assert listed(["--since", "36500d"], capsys) == [
        "cs-dddddddd",
        "cs-cccccccc",
        "cs-bbbbbbbb",
        "cs-aaaaaaaa",
    ]


@pytest.mark.parametrize(
    "since",
    [
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
        "0001-01-01",
        "0999-01-01",
        "0999-12-31T23:59:59-01:00",
    ],
)
def test_list_since_at_the_edges_of_time(
    seeded: Store, since: str, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = (
        []
        if since.startswith("9999")
        else ["cs-dddddddd", "cs-cccccccc", "cs-bbbbbbbb", "cs-aaaaaaaa"]
    )
    assert listed(["--since", since], capsys) == expected


def test_list_forked_from_unknown_and_ambiguous(
    seeded: Store, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    code, envelope = run_json(["list", "--forked-from", "cs-zzzz"], capsys)
    assert (code, envelope["error"]["code"]) == (10, "not_found")
    allocate(seeded, "cs-aaaabbbb", clock)
    code, envelope = run_json(["list", "--forked-from", "cs-aaaa"], capsys)
    assert (code, envelope["error"]["code"]) == (21, "ambiguous")


def test_list_items_carry_the_current_runs_outcome(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    _, obj = run_json(["list"], capsys)
    items = {item["csession_id"]: item for item in obj["sessions"]}
    assert (items["cs-bbbbbbbb"]["last_outcome"], items["cs-bbbbbbbb"]["exit_code"]) == ("done", 0)
    assert items["cs-bbbbbbbb"]["report_status"] == "done"
    assert (items["cs-cccccccc"]["last_end_reason"], items["cs-cccccccc"]["phase"]) == (
        "claude_not_found",
        "launch",
    )
    assert (items["cs-aaaaaaaa"]["last_outcome"], items["cs-aaaaaaaa"]["live"]) == (None, True)
    assert (items["cs-dddddddd"]["pending"], items["cs-dddddddd"]["state"]) == (True, "starting")


def test_list_human_output(seeded: Store, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["ID", "STATE", "KIND", "RUN", "MODE", "UPDATED", "LABELS"]
    assert [line.split()[0] for line in lines[1:]] == [
        "cs-dddddddd",
        "cs-cccccccc",
        "cs-bbbbbbbb",
        "cs-aaaaaaaa",
    ]
    assert "kind=implement,prio=high,team=core" in lines[-1]


def test_list_human_output_with_filters_matching_nothing(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["list", "--needs-input"]) == 0
    assert capsys.readouterr().out == "No sessions.\n"


def test_status_and_list_run_the_liveness_hook_and_never_write(
    seeded: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(views, "check_liveness", lambda st, sid: seen.append(sid))
    monkeypatch.setattr(views, "check_liveness_all", lambda st: seen.append("*"))
    before = seeded.conn.execute("SELECT * FROM sessions ORDER BY csession_id").fetchall()
    events = seeded.conn.execute("SELECT COUNT(*) FROM events").fetchone()
    assert main(["status", "cs-aaaa", "--json"]) == 0
    assert main(["list", "--json"]) == 0
    assert seen == ["cs-aaaaaaaa", "*"]
    assert seeded.conn.execute("SELECT * FROM sessions ORDER BY csession_id").fetchall() == before
    assert seeded.conn.execute("SELECT COUNT(*) FROM events").fetchone() == events


def test_store_open_failure_exits_12(
    _private_state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _private_state_dir / "claude-session"
    root.mkdir(parents=True)
    (root / "state.db").write_bytes(b"not a database" * 100)
    for argv in (["status", "cs-aaaa"], ["list"]):
        code, envelope = run_json(argv, capsys)
        assert (code, envelope["error"]["code"]) == (12, "failed")


def test_status_record_missing_run_row_is_not_found(
    st: Store, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    allocate(st, "cs-aaaaaaaa", clock)
    sql(st, "UPDATE sessions SET current_run = 3 WHERE csession_id = 'cs-aaaaaaaa'")
    code, _ = run_json(["status", "cs-aaaaaaaa"], capsys)
    assert code == 10
    # list skips it, as status does not find it.
    allocate(st, "cs-bbbbbbbb", clock)
    assert listed([], capsys) == ["cs-bbbbbbbb"]


def test_store_status_record_unknown_session(st: Store) -> None:
    assert st.status_record("cs-zzzzzzzz") is None


def test_list_on_a_large_store(st: Store, clock: Clock, capsys: pytest.CaptureFixture[str]) -> None:
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    for index in range(300):
        sid = "cs-" + "".join(alphabet[(index >> (5 * k)) % 32] for k in range(8))
        allocate(st, sid, clock)
    assert len(listed([], capsys)) == 300
    assert len(listed(["--limit", "7"], capsys)) == 7


def test_cli_handlers_are_wired() -> None:
    assert cli.HANDLERS["status"] is cli._status
    assert cli.HANDLERS["list"] is cli._list


def test_status_does_not_lose_the_db_on_corrupt_row_types(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        seeded,
        "UPDATE runs SET exit_code = 'x', wrapper_pid = 'y', is_error = 2, "
        "outcome = 'bogus', ended_at = '2026' WHERE csession_id = 'cs-bbbbbbbb'",
    )
    code, obj = run_json(["status", "cs-bbbbbbbb"], capsys)
    assert code == 0
    assert obj["run"]["exit_code"] is None and obj["run"]["wrapper_pid"] is None
    assert obj["run"]["is_error"] is True and obj["run"]["state"] == "exited"


def test_expires_at_is_null_once_the_run_is_not_pending(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        seeded,
        "UPDATE runs SET expires_at = '2026-10-30T12:10:00.000000Z' "
        "WHERE csession_id = 'cs-aaaaaaaa'",
    )
    _, obj = run_json(["status", "cs-aaaaaaaa", "--run", "1"], capsys)
    assert (obj["expires_at"], obj["run"]["expires_at"]) == (None, None)
    _, listing = run_json(["list", "--live"], capsys)
    assert [item["expires_at"] is None for item in listing["sessions"]] == [False, True]


# Liveness of the run's processes


def test_a_reused_pid_of_a_live_run_is_not_alive(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    # PID 4242 exists, but with another start-time token.
    sql(seeded, "UPDATE runs SET wrapper_start = 'boot:7' WHERE csession_id = 'cs-aaaaaaaa'")
    _, obj = run_json(["status", "cs-aaaaaaaa"], capsys)
    assert (obj["run"]["wrapper_alive"], obj["run"]["child_alive"]) == (False, True)


def test_an_ended_run_with_no_start_token_is_not_alive(
    seeded: Store, alive: set[tuple[int, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    sql(seeded, "UPDATE runs SET wrapper_start = '' WHERE csession_id = 'cs-bbbbbbbb'")
    alive.add((4343, ""))  # some process has PID 4343
    _, obj = run_json(["status", "cs-bbbbbbbb"], capsys)
    assert obj["run"]["wrapper_alive"] is False
    # A live run with no token still falls back to the PID alone.
    sql(seeded, "UPDATE runs SET wrapper_start = '' WHERE csession_id = 'cs-aaaaaaaa'")
    alive.add((4242, ""))
    _, obj = run_json(["status", "cs-aaaaaaaa"], capsys)
    assert obj["run"]["wrapper_alive"] is True


def test_an_ended_run_is_not_alive_when_procinfo_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_session.procinfo import ProcInfoError

    class Broken:
        def is_alive(self, pid: int, start: str) -> bool:
            raise ProcInfoError("cannot tell")

    monkeypatch.setattr(store, "_procinfo", Broken())
    assert store.pid_alive(12345, "x:1")  # a live run: assumed alive
    assert not store.pid_known_alive(12345, "x:1")  # display of an ended run


@pytest.mark.parametrize("pid", [2**31, 2**40, 2**63 - 1, -5])
def test_an_out_of_range_pid_is_not_alive(
    seeded: Store, alive: set[tuple[int, str]], pid: int, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        seeded,
        "UPDATE runs SET wrapper_pid = ?, child_pid = ? WHERE csession_id = 'cs-aaaaaaaa'",
        pid,
        pid,
    )
    alive.update({(pid, "boot:1"), (pid, "boot:2")})
    code, obj = run_json(["status", "cs-aaaaaaaa"], capsys)
    assert code == 0
    assert (obj["run"]["wrapper_alive"], obj["run"]["child_alive"]) == (False, False)


def test_pid_exists_is_false_for_an_out_of_range_pid() -> None:
    assert not store.pid_exists(2**31)
    assert not store.pid_exists(2**64)


# Corrupt and odd stored values


def test_deeply_nested_json_columns_do_not_fail(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    deep = "[" * 100_000 + "]" * 100_000
    sql(
        seeded,
        "UPDATE runs SET warnings_json = ?, resolved_json = ?, invocation_json = ? "
        "WHERE csession_id = 'cs-aaaaaaaa'",
        deep,
        deep,
        deep,
    )
    code, obj = run_json(["status", "cs-aaaaaaaa"], capsys)
    assert code == 0
    validate(obj, STATUS_SCHEMA)
    assert obj["run"]["warnings"] == [] and obj["run"]["input_files"] == []


def copy_session(s: Store, source: str, new_id: object) -> None:
    """A copy of session ``source``'s row under the raw id ``new_id`` (TEXT, any bytes)."""
    sql(s, "CREATE TEMP TABLE copied AS SELECT * FROM sessions WHERE csession_id = ?", source)
    sql(s, "UPDATE copied SET csession_id = CAST(? AS TEXT)", new_id)
    sql(s, "INSERT INTO sessions SELECT * FROM copied")
    sql(s, "DROP TABLE copied")


def test_a_corrupt_session_id_does_not_break_id_lookup(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    copy_session(seeded, "cs-aaaaaaaa", b"cs-aaaaa\xff\xfe")  # not UTF-8
    copy_session(seeded, "cs-bbbbbbbb", "cs-bbbbbbbbz")  # not a valid id
    for text in ("cs-aaaa", "cs-aaaaaaaa", "aaaaa"):
        code, obj = run_json(["status", text], capsys)
        assert (code, obj["csession_id"]) == (0, "cs-aaaaaaaa"), text
    assert run_json(["status", "cs-bbbb"], capsys)[0] == 0
    assert listed(["--forked-from", "cs-bbbb"], capsys) == ["cs-dddddddd"]
    assert seeded.session_id_candidates("cs-aaaa") == [("cs-aaaaaaaa", False)]


def test_hook_interpreter_warning_skips_a_pending_run(
    seeded: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gone = str(tmp_path / "old-venv" / "python3")
    sql(seeded, "UPDATE runs SET hook_interpreter = ? WHERE csession_id = 'cs-dddddddd'", gone)
    _, obj = run_json(["status", "cs-dddddddd"], capsys)
    assert obj["pending"] is True and obj["warnings"] == []


@pytest.mark.parametrize(("active", "expected"), [(1, "required"), (0, "off")])
def test_sandbox_fallback_never_claims_less_isolation_than_observed(
    seeded: Store, active: int, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(
        seeded,
        "UPDATE runs SET resolved_json = '{', sandbox_active = ? WHERE csession_id = 'cs-aaaaaaaa'",
        active,
    )
    _, obj = run_json(["status", "cs-aaaaaaaa"], capsys)
    assert (obj["run"]["sandbox"], obj["run"]["sandbox_active"]) == (expected, bool(active))


def test_a_run_without_a_resolved_cwd_shows_none(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    for sid in ("cs-cccccccc", "cs-dddddddd"):
        code, obj = run_json(["status", sid], capsys)
        assert (code, obj["run"]["cwd"]) == (0, None)
    assert main(["status", "cs-dddddddd"]) == 0
    assert "  cwd:          -\n" in capsys.readouterr().out


# list: needs-input and human-output layout


def test_list_needs_input_filters(
    seeded: Store, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    allocate(seeded, "cs-eeeeeeee", clock)
    to_working(seeded, "cs-eeeeeeee", clock, child=7171)
    clock.tick()
    with seeded.transaction():
        run = seeded.hook_run("cs-eeeeeeee")
        assert run is not None
        seeded.apply_state(
            run,
            state="needs-input",
            activity="idle",
            needs_input_reason="permission",
            cause="Notification",
        )
    assert listed(["--needs-input"], capsys) == ["cs-eeeeeeee"]
    assert listed(["--state", "needs-input"], capsys) == ["cs-eeeeeeee"]
    assert listed(["--state", "needs-input", "--state", "working"], capsys) == [
        "cs-eeeeeeee",
        "cs-aaaaaaaa",
    ]
    assert listed(["--needs-input", "--state", "working"], capsys) == []


def test_list_human_cells_cannot_fake_columns_or_labels(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(seeded, "UPDATE runs SET run_kind = 'a b' WHERE csession_id = 'cs-aaaaaaaa'")
    sql(
        seeded,
        "INSERT INTO labels (csession_id, key, value) VALUES ('cs-aaaaaaaa', 'x', 'a,team=prod')",
    )
    assert main(["list"]) == 0
    line = capsys.readouterr().out.splitlines()[-1]
    assert line.split()[:3] == ["cs-aaaaaaaa", "working", "a\\x20b"]
    assert line.endswith("kind=implement,prio=high,team=core,x=a\\x2cteam=prod")
    assert main(["status", "cs-aaaaaaaa"]) == 0
    out = capsys.readouterr().out
    assert "labels:         kind=implement,prio=high,team=core,x=a\\x2cteam=prod\n" in out


def test_list_human_columns_align_with_wide_characters(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    sql(seeded, "UPDATE runs SET run_kind = '審査' WHERE csession_id = 'cs-aaaaaaaa'")
    assert main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    column = lines[0].index("RUN")
    for line in lines[1:]:
        # Cells hold no spaces, so the fourth word is the RUN cell.
        kind, run = line.split()[2:4]
        at = line.index(f" {run} ", line.index(kind) + len(kind)) + 1
        assert terminal.display_width(line[:at]) == column, line
