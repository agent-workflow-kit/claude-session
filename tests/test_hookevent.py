"""hook-event: argv, payloads, the state written to state.db, the backstop and SessionStart
output (design.md, Hook-event fast path; interface.md, State transitions and Internal
commands)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from claude_session import __main__ as entry
from claude_session import hookevent, ids, store
from claude_session.fsutil import write_private_file
from claude_session.hookevent import HookEventError
from claude_session.store import Store

SID = "cs-abcdefgh"
CLAUDE_ID = "0b6f7a52-3c1d-4e5f-9a8b-7c6d5e4f3a21"
OTHER_CLAUDE_ID = "1c7e8b63-4d2e-4f60-8b9c-8d7e6f5a4b32"
T0 = "2026-10-30T00:00:00.000000Z"


class Env:
    """A state directory with one session (``SID``) and its current run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = str(root / "state.db")
        self.csession_dir = str(root / "sessions" / SID)
        self.private_dir = str(root / "private" / SID)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db, isolation_level=None)

    def add(
        self,
        *,
        mode: str = "headless",
        state: str = "starting",
        activity: str | None = None,
        reason: str | None = None,
        started: bool = False,
        report_status: str | None = None,
        reported_at: str | None = None,
        wrapper: bool = True,
        ended: bool = False,
        forgotten: bool = False,
        run_no: int = 1,
        inputs: list[str] | None = None,
        labels: tuple[tuple[str, str], ...] = (("kind", "implement"),),
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (csession_id, created_at, launch_kind, csession_dir, "
                "private_dir, current_run, state, activity, needs_input_reason, updated_at, "
                "forgotten_at) VALUES (?, ?, 'implement', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    SID,
                    T0,
                    self.csession_dir,
                    self.private_dir,
                    run_no,
                    state,
                    activity,
                    reason,
                    T0,
                    T0 if forgotten else None,
                ),
            )
            conn.execute(
                "INSERT INTO runs (csession_id, run_no, created_at, run_kind, mode, "
                "session_started_at, report_status, reported_at, wrapper_pid, wrapper_start, "
                "ended_at) VALUES (?, ?, ?, 'implement', ?, ?, ?, ?, ?, ?, ?)",
                (
                    SID,
                    run_no,
                    T0,
                    mode,
                    T0 if started else None,
                    report_status,
                    reported_at,
                    os.getpid() if wrapper else None,
                    "x" if wrapper else None,
                    T0 if ended else None,
                ),
            )
            for key, value in labels:
                conn.execute(
                    "INSERT INTO labels (csession_id, key, value) VALUES (?, ?, ?)",
                    (SID, key, value),
                )
        self.write_hookconf(run_no, inputs or [])

    def write_hookconf(self, run_no: int, inputs: list[str]) -> None:
        run_dir = Path(self.private_dir) / "runs" / str(run_no)
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private_file(str(run_dir / "hookconf.json"), hookevent.hookconf_bytes(inputs))

    def session(self) -> dict[str, Any]:
        return self._row("SELECT * FROM sessions WHERE csession_id = ?", (SID,))

    def run(self, run_no: int = 1) -> dict[str, Any]:
        return self._row("SELECT * FROM runs WHERE csession_id = ? AND run_no = ?", (SID, run_no))

    def events(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM events ORDER BY id", ())

    def claude_ids(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM claude_ids ORDER BY seen_at, claude_session_id", ())

    def state(self) -> tuple[str, str | None, str | None]:
        s = self.session()
        return s["state"], s["needs_input_reason"], s["activity"]

    def _rows(self, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        with self.connect() as conn:
            cursor = conn.execute(sql, args)
            names = [c[0] for c in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def _row(self, sql: str, args: tuple[Any, ...]) -> dict[str, Any]:
        (row,) = self._rows(sql, args)
        return row

    def hook(
        self,
        event: str,
        payload: Any = None,
        *,
        raw: bytes | None = None,
        csession_id: str | None = SID,
    ) -> str:
        """Run hookevent.main in-process; returns what it wrote to stdout."""
        data = raw if raw is not None else json.dumps(payload or {}).encode()
        out = io.StringIO()
        environ = {} if csession_id is None else {"CSESSION_ID": csession_id}
        with redirect_stdout(out):
            code = hookevent.main(
                ["--state-dir", str(self.root), event],
                stdin=io.BytesIO(data),
                environ=environ,
            )
        assert code == 0
        return out.getvalue()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    yield Env(root)


def common(event: str, **fields: Any) -> dict[str, Any]:
    """Claude Code's common hook fields, then the event's own."""
    return {
        "session_id": CLAUDE_ID,
        "transcript_path": f"/home/u/.claude/projects/p/{CLAUDE_ID}.jsonl",
        "cwd": "/work",
        "permission_mode": "default",
        "hook_event_name": event,
        **fields,
    }


def start(env: Env, source: str = "startup", **fields: Any) -> str:
    return env.hook("SessionStart", common("SessionStart", source=source, **fields))


def stop(env: Env, tasks: list[Any] | None = None, **fields: Any) -> str:
    payload = common("Stop", **{"stop_hook_active": False, **fields})
    if tasks is not None:
        payload["background_tasks"] = tasks
    return env.hook("Stop", payload)


def context_of(output: str) -> str:
    parsed = json.loads(output)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert set(parsed) == {"hookSpecificOutput"}
    return parsed["hookSpecificOutput"]["additionalContext"]


# Shared constants


def test_events_match_the_entry_point() -> None:
    assert hookevent.EVENTS == entry.HOOK_EVENTS


def test_session_id_grammar_matches_ids() -> None:
    assert hookevent.SESSION_ID.pattern == ids.SESSION_ID.pattern


# SessionStart


@pytest.mark.parametrize(("mode", "activity"), [("headless", "working"), ("interactive", "idle")])
def test_session_start_moves_starting_to_the_mode_activity(
    env: Env, mode: str, activity: str
) -> None:
    env.add(mode=mode)
    start(env)
    assert env.state() == (activity, None, activity)
    (event,) = env.events()
    assert (event["from_state"], event["to_state"]) == ("starting", activity)
    assert (event["from_activity"], event["to_activity"]) == (None, activity)
    assert (event["cause"], event["final"], event["run_no"]) == ("SessionStart", 0, 1)
    assert json.loads(event["labels_json"]) == ["kind=implement"]
    session = env.session()
    assert session["state_since"] == session["updated_at"] == event["event_ts"]


def test_session_start_records_ids_sources_and_transcript_paths(env: Env) -> None:
    env.add()
    start(env)
    (row,) = env.claude_ids()
    assert row["claude_session_id"] == CLAUDE_ID
    assert (row["run_no"], row["source"]) == (1, "startup")
    assert row["transcript_path"] == f"/home/u/.claude/projects/p/{CLAUDE_ID}.jsonl"
    assert env.session()["claude_session_id"] == CLAUDE_ID
    assert env.run()["session_started_at"] == row["seen_at"]


def test_session_start_clear_records_the_new_id_and_keeps_the_first_start(env: Env) -> None:
    env.add()
    start(env)
    first_started = env.run()["session_started_at"]
    before = env.state()
    other_path = f"/home/u/.claude/projects/p/{OTHER_CLAUDE_ID}.jsonl"
    start(env, "clear", session_id=OTHER_CLAUDE_ID, transcript_path=other_path)
    assert env.state() == before
    assert len(env.events()) == 1  # no state change, no event
    assert env.session()["claude_session_id"] == OTHER_CLAUDE_ID
    assert [(r["claude_session_id"], r["source"]) for r in env.claude_ids()] == [
        (CLAUDE_ID, "startup"),
        (OTHER_CLAUDE_ID, "clear"),
    ]
    assert env.run()["session_started_at"] == first_started


def test_a_repeated_claude_id_keeps_its_first_row(env: Env) -> None:
    env.add()
    start(env)
    start(env, "compact")
    (row,) = env.claude_ids()
    assert row["source"] == "startup"


@pytest.mark.parametrize("source", ["startup", "resume", "fork"])
def test_session_start_new_source_in_a_later_state_changes_nothing(env: Env, source: str) -> None:
    env.add(state="idle", activity="idle", started=True, mode="interactive")
    start(env, source)
    assert env.state() == ("idle", None, "idle")
    assert env.events() == []
    assert env.claude_ids()[0]["source"] == source


def test_session_start_without_a_transcript_path_records_null(env: Env) -> None:
    env.add()
    payload = common("SessionStart", source="startup")
    del payload["transcript_path"]
    env.hook("SessionStart", payload)
    assert env.claude_ids()[0]["transcript_path"] is None


def test_session_start_emits_the_context_line(env: Env) -> None:
    env.add(inputs=["spec.md", "data.csv"])
    output = start(env)
    assert output.endswith("\n") and output.count("\n") == 1
    assert context_of(output) == (
        f"claude-session directory: {env.csession_dir}; run 1; inputs: spec.md, data.csv"
    )


def test_context_line_without_inputs_says_none(env: Env) -> None:
    env.add()
    assert context_of(start(env)).endswith("; run 1; inputs: none")


@pytest.mark.parametrize("source", ["startup", "resume", "fork", "clear", "compact"])
def test_every_session_start_source_emits_the_context_line(env: Env, source: str) -> None:
    env.add(run_no=3, state="working", activity="working", started=True)
    assert "; run 3; inputs: none" in context_of(start(env, source))


def test_session_start_with_a_missing_hookconf_is_an_error_after_the_state_is_stored(
    env: Env,
) -> None:
    env.add()
    os.unlink(Path(env.private_dir) / "runs" / "1" / "hookconf.json")
    with pytest.raises(FileNotFoundError):
        start(env)
    assert env.state() == ("working", None, "working")


def test_a_symlinked_hookconf_is_refused(env: Env, tmp_path: Path) -> None:
    env.add()
    target = Path(env.private_dir) / "runs" / "1" / "hookconf.json"
    decoy = tmp_path / "decoy.json"
    decoy.write_bytes(hookevent.hookconf_bytes(["evil"]))
    target.unlink()
    target.symlink_to(decoy)
    with pytest.raises(OSError):
        start(env)


def test_a_symlinked_run_directory_is_refused(env: Env, tmp_path: Path) -> None:
    env.add()
    real = tmp_path / "elsewhere"
    (Path(env.private_dir) / "runs" / "1").rename(real)
    (Path(env.private_dir) / "runs" / "1").symlink_to(real)
    with pytest.raises(OSError):
        start(env)


def test_a_hard_linked_hookconf_is_refused(env: Env, tmp_path: Path) -> None:
    env.add()
    os.link(Path(env.private_dir) / "runs" / "1" / "hookconf.json", tmp_path / "link")
    with pytest.raises(HookEventError):
        start(env)


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"v": 2, "input_files": []}',
        b'{"v": 1}',
        b'{"v": 1, "input_files": [], "extra": 1}',
        b'{"v": 1, "input_files": ["../x"]}',
        b'{"v": 1, "input_files": [".."]}',
        b'{"v": 1, "input_files": ["a; run 9"]}',
        b'{"v": 1, "input_files": ["a\\nb"]}',
        b'{"v": 1, "input_files": ["a", "a"]}',
        b'{"v": 1, "input_files": [1]}',
        b"\xff\xfe",
    ],
)
def test_an_invalid_hookconf_is_refused(env: Env, content: bytes) -> None:
    env.add()
    write_private_file(str(Path(env.private_dir) / "runs" / "1" / "hookconf.json"), content)
    with pytest.raises(HookEventError):
        start(env)


def test_a_hookconf_readable_by_others_is_refused(env: Env) -> None:
    env.add()
    (Path(env.private_dir) / "runs" / "1" / "hookconf.json").chmod(0o640)
    with pytest.raises(HookEventError):
        start(env)


def test_an_oversized_hookconf_is_refused(env: Env) -> None:
    env.add()
    big = b'{"v": 1, "input_files": [], "pad": "' + b"x" * hookevent.HOOKCONF_MAX_BYTES + b'"}'
    write_private_file(str(Path(env.private_dir) / "runs" / "1" / "hookconf.json"), big)
    with pytest.raises(HookEventError, match="private regular file"):
        start(env)


def test_hookconf_bytes_refuses_invalid_names() -> None:
    with pytest.raises(HookEventError):
        hookevent.hookconf_bytes(["a/b"])


# Stop and the backstop


def test_backstop_blocks_once_per_run(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    output = stop(env, [])
    assert json.loads(output) == {"decision": "block", "reason": hookevent.BACKSTOP_REASON}
    assert env.state() == ("working", None, "working")  # activity unchanged
    assert env.events() == []  # no event
    assert env.run()["backstop_used"] == 1
    assert stop(env, [], stop_hook_active=True) == ""
    assert env.state() == ("idle", None, "idle")
    assert len(env.events()) == 1


def test_backstop_text_is_the_pinned_one() -> None:
    assert hookevent.backstop_output() == {
        "decision": "block",
        "reason": "claude-session: no status report has been filed for this run. File exactly "
        "one report now with claude-session report, as described in your instructions (done, "
        "blocked, failed or needs-input), then stop.",
    }


def test_backstop_applies_to_a_stop_without_the_background_tasks_field(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    assert json.loads(stop(env))["decision"] == "block"


def test_no_backstop_for_interactive_runs(env: Env) -> None:
    env.add(mode="interactive", state="working", activity="working", started=True)
    assert stop(env, []) == ""
    assert env.state() == ("idle", None, "idle")
    assert env.run()["backstop_used"] == 0


def test_no_backstop_once_a_report_exists(env: Env) -> None:
    env.add(
        state="reported", activity="working", started=True, report_status="done", reported_at=T0
    )
    assert stop(env, []) == ""
    assert env.state() == ("reported", None, "idle")


def test_no_backstop_before_session_start(env: Env) -> None:
    env.add()
    assert stop(env, []) == ""
    assert env.state() == ("starting", None, None)
    assert env.run()["backstop_used"] == 0


def test_stop_with_background_tasks_keeps_working_and_spares_the_backstop(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    assert stop(env, [{"id": "task-1"}]) == ""
    assert env.state() == ("working", None, "working")
    assert env.run()["backstop_used"] == 0
    assert json.loads(stop(env, []))["decision"] == "block"


def test_stop_with_background_tasks_after_idle_returns_to_working(env: Env) -> None:
    env.add(mode="interactive", state="idle", activity="idle", started=True)
    stop(env, ["task"])
    assert env.state() == ("working", None, "working")
    assert env.events()[0]["cause"] == "Stop"


def test_idle_prompt_notification_goes_idle_without_backstop(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    assert env.hook("Notification", common("Notification", notification_type="idle_prompt")) == ""
    assert env.state() == ("idle", None, "idle")
    assert env.run()["backstop_used"] == 0


# Prompts, escalation, PostToolUse and UserPromptSubmit


@pytest.mark.parametrize(
    ("event", "fields", "reason"),
    [
        ("PermissionRequest", {"tool_name": "Bash", "tool_input": {}}, "permission"),
        ("PermissionRequest", {"tool_name": "AskUserQuestion"}, "question"),
        ("Notification", {"notification_type": "permission_prompt"}, "permission"),
        ("Notification", {"notification_type": "elicitation_dialog"}, "elicitation"),
    ],
)
def test_prompts_set_needs_input(env: Env, event: str, fields: dict[str, Any], reason: str) -> None:
    env.add(mode="interactive", state="idle", activity="idle", started=True)
    env.hook(event, common(event, **fields))
    assert env.state() == ("needs-input", reason, "working")
    (row,) = env.events()
    assert (row["to_state"], row["needs_input_reason"], row["cause"]) == (
        "needs-input",
        reason,
        event,
    )


def test_a_reason_change_alone_appends_an_event(env: Env) -> None:
    env.add(
        mode="interactive",
        state="needs-input",
        activity="working",
        reason="permission",
        started=True,
    )
    env.hook("PermissionRequest", common("PermissionRequest", tool_name="AskUserQuestion"))
    assert env.state() == ("needs-input", "question", "working")
    assert len(env.events()) == 1


def test_an_untracked_notification_type_changes_nothing(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    env.hook("Notification", common("Notification", notification_type="auth_success"))
    assert env.state() == ("working", None, "working")
    assert env.events() == []


def test_post_tool_use_clears_a_prompt(env: Env) -> None:
    env.add(
        mode="interactive",
        state="needs-input",
        activity="working",
        reason="permission",
        started=True,
    )
    env.hook("PostToolUse", common("PostToolUse", tool_name="Bash"))
    assert env.state() == ("working", None, "working")


def test_escalation_reverts_on_post_tool_use_until_user_prompt_submit(env: Env) -> None:
    env.add(
        mode="interactive",
        state="needs-input",
        activity="working",
        reason="permission",
        started=True,
        report_status="needs-input",
        reported_at=T0,
    )
    env.hook("PostToolUse", common("PostToolUse"))
    assert env.state() == ("needs-input", "escalation", "working")
    stop(env, [])
    assert env.state() == ("needs-input", "escalation", "idle")
    env.hook("UserPromptSubmit", common("UserPromptSubmit", prompt="go on"))
    assert env.state() == ("reported", None, "working")
    env.hook("PostToolUse", common("PostToolUse"))
    assert env.state() == ("reported", None, "working")


def test_user_prompt_submit_before_the_report_keeps_the_escalation_open(env: Env) -> None:
    env.add(mode="interactive", state="idle", activity="idle", started=True)
    env.hook("UserPromptSubmit", common("UserPromptSubmit"))
    with env.connect() as conn:  # a needs-input report ingested afterwards
        conn.execute(
            "UPDATE runs SET report_status = 'needs-input', reported_at = ?",
            ("2999-01-01T00:00:00.000000Z",),
        )
    env.hook("PostToolUse", common("PostToolUse"))
    assert env.state() == ("needs-input", "escalation", "working")


def test_user_prompt_submit_payload_is_never_parsed(env: Env) -> None:
    env.add(mode="interactive", state="idle", activity="idle", started=True)
    env.hook("UserPromptSubmit", raw=b"\xff not json")
    assert env.state() == ("working", None, "working")


def test_post_tool_use_with_an_oversized_payload_still_applies(env: Env) -> None:
    env.add(mode="interactive", state="idle", activity="idle", started=True)
    env.hook("PostToolUse", raw=b"x" * (hookevent.PAYLOAD_MAX_BYTES + 10))
    assert env.state() == ("working", None, "working")


def test_events_before_session_start_change_nothing(env: Env) -> None:
    env.add()
    env.hook("PermissionRequest", common("PermissionRequest", tool_name="Bash"))
    env.hook("PostToolUse", common("PostToolUse"))
    assert env.state() == ("starting", None, None)
    assert env.events() == []


def test_a_repeated_event_appends_no_second_event(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    env.hook("PostToolUse", common("PostToolUse"))
    env.hook("PostToolUse", common("PostToolUse"))
    assert env.events() == []


def test_labels_at_event_time_are_recorded_sorted(env: Env) -> None:
    env.add(
        mode="interactive",
        state="idle",
        activity="idle",
        started=True,
        labels=(("team", "b"), ("kind", "implement"), ("team", "a")),
    )
    env.hook("UserPromptSubmit", common("UserPromptSubmit"))
    assert json.loads(env.events()[0]["labels_json"]) == ["kind=implement", "team=a", "team=b"]


# SessionEnd


def test_session_end_records_the_reason_without_a_state_change(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    env.hook("SessionEnd", common("SessionEnd", reason="clear"))
    env.hook("SessionEnd", common("SessionEnd", reason="prompt_input_exit"))
    assert env.run()["session_end_reason"] == "prompt_input_exit"
    assert env.state() == ("working", None, "working")
    assert env.events() == []


# Runs the hook leaves alone


@pytest.mark.parametrize(
    "setup",
    [
        {"wrapper": False},  # pending: not adopted
        {"ended": True},
        {"state": "exited"},
        {"state": "not-started"},
    ],
)
def test_runs_that_are_not_live_are_left_alone(env: Env, setup: dict[str, Any]) -> None:
    env.add(**setup)
    before = (env.session(), env.run())
    assert start(env) == ""
    assert stop(env, []) == ""
    env.hook("SessionEnd", common("SessionEnd", reason="other"))
    assert (env.session(), env.run()) == before
    assert env.events() == []
    assert env.claude_ids() == []


def test_an_unknown_session_is_an_error(env: Env) -> None:
    with pytest.raises(HookEventError, match="unknown session"):
        env.hook("PostToolUse", {})


def test_a_forgotten_session_is_an_error(env: Env) -> None:
    env.add(forgotten=True, state="working", activity="working", started=True)
    with pytest.raises(HookEventError):
        env.hook("PostToolUse", {})


# Malformed input: an error, and nothing is written


BAD_PAYLOADS: list[tuple[str, bytes]] = [
    ("SessionStart", b""),
    ("SessionStart", b"not json"),
    ("SessionStart", b"[1, 2]"),
    ("SessionStart", b'"text"'),
    ("SessionStart", b"\xff\xfe"),
    ("SessionStart", b"[" * 100_000),
    ("SessionStart", json.dumps({"session_id": CLAUDE_ID}).encode()),
    ("SessionStart", json.dumps({"source": "startup"}).encode()),
    ("SessionStart", json.dumps({"source": "reboot", "session_id": CLAUDE_ID}).encode()),
    ("SessionStart", json.dumps({"source": "startup", "session_id": "--help"}).encode()),
    ("SessionStart", json.dumps({"source": "startup", "session_id": CLAUDE_ID.upper()}).encode()),
    ("SessionStart", json.dumps({"source": "startup", "session_id": "ü" * 36}).encode()),
    ("SessionStart", json.dumps({"source": "startup", "session_id": 7}).encode()),
    (
        "SessionStart",
        json.dumps(
            {"source": "startup", "session_id": CLAUDE_ID, "transcript_path": "rel/x"}
        ).encode(),
    ),
    (
        "SessionStart",
        json.dumps(
            {"source": "startup", "session_id": CLAUDE_ID, "transcript_path": "/a\nb"}
        ).encode(),
    ),
    (
        "SessionStart",
        json.dumps(
            {"source": "startup", "session_id": CLAUDE_ID, "transcript_path": "/" + "a" * 5000}
        ).encode(),
    ),
    ("PermissionRequest", json.dumps({"tool_input": {}}).encode()),
    ("PermissionRequest", json.dumps({"tool_name": ["AskUserQuestion"]}).encode()),
    ("Notification", json.dumps({"message": "x"}).encode()),
    ("Stop", json.dumps({"background_tasks": "task"}).encode()),
    ("Stop", json.dumps({"background_tasks": {"a": 1}}).encode()),
    ("SessionEnd", json.dumps({}).encode()),
    ("SessionEnd", json.dumps({"reason": ""}).encode()),
    ("SessionEnd", json.dumps({"reason": "a\x1bb"}).encode()),
    ("SessionEnd", json.dumps({"reason": "x" * 300}).encode()),
]


@pytest.mark.parametrize(("event", "raw"), BAD_PAYLOADS)
def test_malformed_payloads_are_errors_and_write_nothing(env: Env, event: str, raw: bytes) -> None:
    env.add(state="working", activity="working", started=True)
    before = (env.session(), env.run())
    with pytest.raises(HookEventError):
        env.hook(event, raw=raw)
    assert (env.session(), env.run()) == before
    assert env.events() == [] and env.claude_ids() == []


def test_an_oversized_session_start_payload_is_an_error(env: Env) -> None:
    env.add()
    with pytest.raises(HookEventError, match="too large"):
        env.hook("SessionStart", raw=b" " * (hookevent.PAYLOAD_MAX_BYTES + 1))


def test_extra_payload_fields_are_ignored(env: Env) -> None:
    env.add()
    start(env, extra={"nested": ["x"]}, model="m")
    assert env.state() == ("working", None, "working")


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--state-dir"],
        ["--state-dir", "/s"],
        ["--state-dir", "relative", "Stop"],
        ["--state-dir", "", "Stop"],
        ["--state-dir=/s", "Stop"],
        ["--state-dir", "/s", "Stop", "--yes"],
        ["Stop", "--state-dir", "/s"],
        ["--state-dir", "/s", "SubagentStop"],
        ["--state-dir", "/s", "stop"],
        ["--state-dir", "/s\0x", "Stop"],
    ],
)
def test_only_the_injected_argv_is_accepted(argv: list[str]) -> None:
    with pytest.raises(HookEventError):
        hookevent.main(argv, stdin=io.BytesIO(b"{}"), environ={"CSESSION_ID": SID})


@pytest.mark.parametrize("value", [None, "", "cs-ABCDEFGH", "cs-abc", "cs-abcdefgh\n", "x"])
def test_csession_id_must_be_a_session_id(env: Env, value: str | None) -> None:
    env.add(state="working", activity="working", started=True)
    with pytest.raises(HookEventError, match="CSESSION_ID"):
        env.hook("PostToolUse", {}, csession_id=value)


def test_a_missing_database_is_an_error_and_is_not_created(tmp_path: Path) -> None:
    with pytest.raises(Exception):  # noqa: B017 (store raises FailedError)
        hookevent.main(
            ["--state-dir", str(tmp_path), "PostToolUse"],
            stdin=io.BytesIO(b"{}"),
            environ={"CSESSION_ID": SID},
        )
    assert list(tmp_path.iterdir()) == []


def test_a_newer_minor_schema_is_used_without_migrating(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    with env.connect() as conn:
        conn.execute("ALTER TABLE runs ADD COLUMN future_column TEXT")
        conn.execute("UPDATE meta SET value = '1.7' WHERE key = 'schema_version'")
    env.hook("Stop", common("Stop", background_tasks=["t"]))
    stop(env, [])  # backstop
    stop(env, [])
    assert env.state() == ("idle", None, "idle")
    with env.connect() as conn:
        assert conn.execute("SELECT value FROM meta").fetchone() == ("1.7",)


def test_another_major_schema_is_refused(env: Env) -> None:
    env.add(state="working", activity="working", started=True)
    with env.connect() as conn:
        conn.execute("UPDATE meta SET value = '2.0' WHERE key = 'schema_version'")
    with pytest.raises(store.FailedError):
        env.hook("PostToolUse", {})


def test_a_busy_database_raises_after_the_busy_timeout(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.add(mode="interactive", state="idle", activity="idle", started=True)
    monkeypatch.setattr(hookevent, "BUSY_TIMEOUT", 0.05)
    blocker = env.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            env.hook("UserPromptSubmit", {})
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert env.state() == ("idle", None, "idle")


# read_payload


def test_read_payload_reads_to_the_end_even_past_the_cap() -> None:
    stream = io.BytesIO(b"x" * (hookevent.PAYLOAD_MAX_BYTES + 5))
    assert hookevent.read_payload(stream) is None
    assert stream.read() == b""


def test_read_payload_keeps_a_payload_at_the_cap() -> None:
    data = b"y" * hookevent.PAYLOAD_MAX_BYTES
    assert hookevent.read_payload(io.BytesIO(data)) == data
