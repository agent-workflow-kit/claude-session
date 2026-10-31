"""``status`` and ``list`` (interface.md, status and list): JSON per schema (golden files),
filters, id resolution and the safety of human and JSON output for any stored value."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from claude_session import cli, store, views
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
HOSTILE = "evil\x1b]0;pwned\x07\x1b[2J\r\nfake: line\x9b31m\u202eetc\u2028x\x00\x7f"


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
def alive(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """PIDs the liveness check reports alive (every other process is gone)."""
    pids: set[int] = set()
    monkeypatch.setattr(store, "pid_alive", lambda pid, start: pid in pids)
    return pids


@pytest.fixture
def st(clock: Clock, alive: set[int]) -> Iterator[Store]:
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


def seed(st: Store, clock: Clock, alive: set[int]) -> None:
    """Four sessions: working (live), done (final, reported), not started (launch failure),
    pending (start, not adopted)."""
    allocate(st, "cs-aaaaaaaa", clock, labels=("team=core", "prio=high"))
    to_working(st, "cs-aaaaaaaa", clock)
    alive.update({4242, 5151})
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
    allocate(st, "cs-cccccccc", clock, mode="interactive", repo=None, labels=())
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
def seeded(st: Store, clock: Clock, alive: set[int]) -> Store:
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
        json.dumps([HOSTILE]),
        HOSTILE,
        sid,
    )
    sql(s, "INSERT INTO labels (csession_id, key, value) VALUES (?, 'x', ?)", sid, HOSTILE)


def assert_inert(text: str) -> None:
    for bad in ("\x1b", "\x07", "\x9b", "\r", "\x00", "\x7f", "\u202e", "\u2028"):
        assert bad not in text, repr(bad)
    assert "\nfake: line" not in text


def test_status_human_output_escapes_control_and_escape_sequences(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    poison(seeded, "cs-aaaaaaaa")
    assert main(["status", "cs-aaaaaaaa"]) == 0
    out = capsys.readouterr().out
    assert_inert(out)
    assert "evil\\x1b]0;pwned\\x07\\x1b[2J\\x0d\\x0afake: line\\x9b31m\\u202eetc" in out


def test_list_human_output_escapes_labels(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    poison(seeded, "cs-aaaaaaaa")
    assert main(["list"]) == 0
    assert_inert(capsys.readouterr().out)


def test_json_output_carries_hostile_text_escaped(
    seeded: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    poison(seeded, "cs-aaaaaaaa")
    assert main(["status", "cs-aaaaaaaa", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.isascii() and "\x1b" not in out
    obj = json.loads(out)
    assert obj["run"]["report"]["summary"] == HOSTILE
    assert main(["list", "--json"]) == 0
    sessions = json.loads(capsys.readouterr().out)["sessions"]
    poisoned = next(item for item in sessions if item["csession_id"] == "cs-aaaaaaaa")
    assert f"x={HOSTILE}" in poisoned["labels"]


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


def test_display_text_escapes_controls_and_keeps_text() -> None:
    assert views.display_text("héllo wörld ✓") == "héllo wörld ✓"
    assert views.display_text("a\tb\x85c\u200fd") == "a\\x09b\\x85c\\u200fd"
    assert views.display_text(None) == "-"


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
    "since", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00", "0001-01-01"]
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
    # list still shows it (its run fields fall back to null and launch).
    code, obj = run_json(["list"], capsys)
    assert code == 0 and obj["sessions"][0]["run_kind"] is None


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
