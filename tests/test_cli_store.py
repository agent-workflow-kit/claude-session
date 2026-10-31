"""The CLI's use of state.db: the schema check before parsing, and `list` on an empty store."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from pathlib import Path

import pytest

from claude_session import __main__ as entry
from claude_session import cli, store
from claude_session.cli import main
from claude_session.errors import CliError

from .schemas import validate
from .test_cli import FULL_INVOCATIONS

GOLDEN = Path(__file__).resolve().parent / "golden"
ERROR_SCHEMA = "claude-session/cli/error.json"
LIST_SCHEMA = "claude-session/cli/list.json"


def db() -> str:
    return store.db_path(store.state_dir())


def make_newer() -> str:
    """A state.db whose schema is newer than this claude-session; returns its version."""
    store.open_state().close()
    major, minor = store.code_version()
    newer = f"{major}.{minor + 1}"
    conn = sqlite3.connect(db())
    try:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (newer,))
        conn.commit()
    finally:
        conn.close()
    return newer


def db_bytes() -> bytes:
    return Path(db()).read_bytes()


# list


def test_list_json_on_an_empty_store(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"sessions": []}
    validate(json.loads(out), LIST_SCHEMA)
    assert json.loads(out) == json.loads((GOLDEN / "list-empty.json").read_text())


def test_list_json_creates_the_store_in_the_state_dir(
    _private_state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["list", "--json"]) == 0
    assert (_private_state_dir / "claude-session" / "state.db").is_file()


def test_list_json_with_filters_on_an_empty_store(capsys: pytest.CaptureFixture[str]) -> None:
    argv = FULL_INVOCATIONS["list"]
    at = argv.index("--forked-from")
    assert main(argv[:at] + argv[at + 2 :]) == 0
    assert json.loads(capsys.readouterr().out) == {"sessions": []}


def test_list_forked_from_an_unknown_session_exits_10(capsys: pytest.CaptureFixture[str]) -> None:
    # --forked-from takes session id input, which resolves like any id (interface.md,
    # Identifiers): an unknown id exits 10.
    assert main(FULL_INVOCATIONS["list"]) == 10
    envelope = json.loads(capsys.readouterr().out)
    validate(envelope, ERROR_SCHEMA)
    assert envelope["error"]["code"] == "not_found"


def test_list_human_on_an_empty_store(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("No sessions.\n", "")


# Schema check (interface.md, Launch order step 1)


def test_schema_newer_is_refused_with_exit_12(capsys: pytest.CaptureFixture[str]) -> None:
    newer = make_newer()
    before = db_bytes()
    assert main(["list", "--json"]) == 12
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    validate(envelope, ERROR_SCHEMA)
    assert envelope["error"]["code"] == "schema_newer"
    assert newer in envelope["error"]["message"]
    assert "schema_newer" not in captured.err and newer in captured.err
    assert db_bytes() == before


def test_schema_newer_golden(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "MIGRATIONS", (store.Migration((1, 0), store._migration_1_0),))
    store.open_state().close()
    conn = sqlite3.connect(db())
    try:
        conn.execute("UPDATE meta SET value = '1.1' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    assert main(["list", "--json"]) == 12
    envelope = json.loads(capsys.readouterr().out)
    golden = json.loads((GOLDEN / "error-schema-newer.json").read_text())
    validate(golden, ERROR_SCHEMA)
    assert envelope == golden


def capture_errors(monkeypatch: pytest.MonkeyPatch) -> list[CliError]:
    seen: list[CliError] = []
    monkeypatch.setattr(cli, "_emit_error", lambda error, json_mode: seen.append(error))
    return seen


@pytest.mark.parametrize("name", sorted(n for n in FULL_INVOCATIONS if not n.startswith("report")))
def test_every_command_but_report_checks_the_schema(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_newer()
    seen = capture_errors(monkeypatch)
    assert main(FULL_INVOCATIONS[name]) == 12
    assert [error.code for error in seen] == ["schema_newer"]
    validate(seen[0].envelope(), ERROR_SCHEMA)


def test_schema_check_comes_before_usage_checks(capsys: pytest.CaptureFixture[str]) -> None:
    make_newer()
    assert main(["list", "--bogus", "--json"]) == 12
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "schema_newer"
    assert main(["exec", "--verbose", "--quiet", "--json"]) == 12
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "schema_newer"


def test_schema_newer_is_not_written_to_the_outcome_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_newer()
    outcome = tmp_path / "o.json"
    assert main(["wait", "cs-abcd2345", "--outcome-file", str(outcome)]) == 12
    assert not outcome.exists()


def test_an_unsafe_store_is_failed(_private_state_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.db"
    root = _private_state_dir / "claude-session"
    root.mkdir(parents=True, mode=0o700)
    (root / "state.db").symlink_to(target)
    outcome = tmp_path / "o.json"
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert main(["wait", "cs-abcd2345", "--outcome-file", str(outcome), "--json"]) == 12
    envelope = json.loads(stdout.getvalue())
    validate(envelope, ERROR_SCHEMA)
    assert envelope["error"]["code"] == "failed"
    assert str(root / "state.db") in envelope["error"]["detail"]
    assert not target.exists()
    assert not outcome.exists()  # the schema check precedes parsing


def test_an_invalid_schema_version_is_failed(capsys: pytest.CaptureFixture[str]) -> None:
    store.open_state().close()
    conn = sqlite3.connect(db())
    try:
        conn.execute("UPDATE meta SET value = 'one' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    assert main(["list", "--json"]) == 12
    envelope = json.loads(capsys.readouterr().out)
    validate(envelope, ERROR_SCHEMA)
    assert envelope["error"]["code"] == "failed"
    assert envelope["error"]["detail"] == f"{db()}: invalid schema_version 'one'"


def test_report_never_opens_the_store(
    _private_state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "report", lambda args: 77)
    assert main(["report", "done", "--summary", "x"]) == 77
    assert not _private_state_dir.exists()
    make_newer()
    assert main(["report", "done", "--summary", "x"]) == 77


@pytest.mark.parametrize(
    "argv", [["--help"], ["-h"], ["--version"], ["list", "--help"], ["list", "--json", "--version"]]
)
def test_help_and_version_skip_the_schema_check(
    argv: list[str], _private_state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert entry.main(argv) == 0
    assert not _private_state_dir.exists()
    make_newer()
    assert entry.main(argv) == 0


@pytest.mark.parametrize("argv", [[], ["nonsense", "--json"], ["--json", "list"]])
def test_no_or_unknown_command_is_a_usage_error_without_the_store(
    argv: list[str], _private_state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    assert not _private_state_dir.exists()


def test_help_after_double_dash_does_not_skip_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_newer()
    seen = capture_errors(monkeypatch)
    assert main(["label", "add", "cs-abcd2345", "--", "--help"]) == 12
    assert [error.code for error in seen] == ["schema_newer"]


def test_an_older_store_is_migrated_by_a_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    applied: list[str] = []
    migrations = (
        store.Migration((1, 0), store._migration_1_0),
        store.Migration((1, 1), lambda conn: applied.append("1.1")),
    )
    monkeypatch.setattr(store, "MIGRATIONS", migrations[:1])
    store.open_state(migrations=migrations[:1]).close()
    monkeypatch.setattr(store, "MIGRATIONS", migrations)
    assert main(["list", "--json"]) == 0
    assert applied == ["1.1"]
    with store.open_state(migrate=False) as s:
        assert s.schema_version() == (1, 1)


def test_hook_event_works_with_a_newer_additive_schema_and_never_migrates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    newer = make_newer()
    now = "2026-10-30T00:00:00.000000Z"
    conn = sqlite3.connect(db())
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN added_later TEXT")
        conn.execute(
            "INSERT INTO sessions (csession_id, created_at, launch_kind, current_run, state, "
            "activity, updated_at) VALUES ('cs-abcdefgh', ?, 'k', 1, 'idle', 'idle', ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO runs (csession_id, run_no, created_at, run_kind, mode, "
            "session_started_at, wrapper_pid) VALUES ('cs-abcdefgh', 1, ?, 'k', "
            "'interactive', ?, 1)",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("CSESSION_ID", "cs-abcdefgh")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    state_root = store.state_dir()
    assert entry.main(["hook-event", "--state-dir", state_root, "UserPromptSubmit"]) == 0
    assert capsys.readouterr() == ("", "")
    with store.open_state(migrate=False) as s:
        assert store.format_version(s.schema_version()) == newer
        row = s.conn.execute("SELECT state, activity FROM sessions").fetchone()
    assert row == ("working", "working")
    log = Path(state_root, "log.jsonl")
    assert "hook_event_failed" not in (log.read_text() if log.exists() else "")
