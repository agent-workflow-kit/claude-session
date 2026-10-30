"""log.jsonl writer (interface.md, Audit and log)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from claude_session import logs

from .schemas import registry, validate

LOG_SCHEMA = "claude-session/log.json"
SECRET = "s3cr3t-Value-0123456789"
SID = "cs-abcd2345"


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "log.jsonl"


def test_minimal_record_validates(log_path: Path) -> None:
    assert logs.EventLog(str(log_path)).write("adopt")
    [record] = records(log_path)
    validate(record, LOG_SCHEMA)
    assert record["event"] == "adopt"
    assert record["level"] == "info"


def test_full_record_validates(log_path: Path) -> None:
    log = logs.EventLog(str(log_path), min_level="debug")
    log.write(
        "signal_sent",
        level="debug",
        csession_id=SID,
        run=2,
        pid=4242,
        signal="SIGTERM",
        decision_ids=["d1", "d2"],
        reason="hook_timeout",
        message="sent",
    )
    [record] = records(log_path)
    validate(record, LOG_SCHEMA)
    assert record == {
        "ts": record["ts"],
        "level": "debug",
        "event": "signal_sent",
        "csession_id": SID,
        "run": 2,
        "pid": 4242,
        "signal": "SIGTERM",
        "decision_ids": ["d1", "d2"],
        "reason": "hook_timeout",
        "message": "sent",
    }


def test_unset_fields_are_omitted(log_path: Path) -> None:
    logs.EventLog(str(log_path)).write("migration", message="v2")
    [record] = records(log_path)
    assert set(record) == {"ts", "level", "event", "message"}


@pytest.mark.parametrize("event", sorted(logs.EVENTS))
def test_every_event_validates(event: str, log_path: Path) -> None:
    logs.EventLog(str(log_path)).write(event)
    validate(records(log_path)[0], LOG_SCHEMA)


def test_events_and_levels_match_the_schema() -> None:
    schema = registry().contents(LOG_SCHEMA)
    assert set(logs.EVENTS) == set(schema["properties"]["event"]["enum"])
    assert list(logs.LEVELS) == schema["properties"]["level"]["enum"]


def test_timestamp_is_utc_iso_8601(log_path: Path) -> None:
    now = datetime(2026, 10, 30, 12, 0, 0, 123456, tzinfo=UTC)
    logs.EventLog(str(log_path)).write("adopt", now=now)
    assert records(log_path)[0]["ts"] == "2026-10-30T12:00:00.123Z"


def test_default_timestamp_is_now(log_path: Path) -> None:
    logs.EventLog(str(log_path)).write("adopt")
    ts = datetime.fromisoformat(records(log_path)[0]["ts"])
    assert ts.tzinfo is not None
    assert abs((datetime.now(UTC) - ts).total_seconds()) < 60


@pytest.mark.parametrize(
    ("min_level", "written"),
    [
        ("debug", ["debug", "info", "warn", "error"]),
        ("info", ["info", "warn", "error"]),
        ("warn", ["warn", "error"]),
        ("error", ["error"]),
    ],
)
def test_records_below_the_level_are_skipped(
    min_level: str, written: list[str], log_path: Path
) -> None:
    log = logs.EventLog(str(log_path), min_level=min_level)
    results = [log.write("adopt", level=level) for level in logs.LEVELS]
    assert [r["level"] for r in records(log_path)] == written
    assert results == [level in written for level in logs.LEVELS]


def test_nothing_is_created_when_every_record_is_skipped(log_path: Path) -> None:
    logs.EventLog(str(log_path), min_level="error").write("adopt")
    assert not log_path.exists()


@pytest.mark.parametrize(
    "kwargs",
    [{"event": "no_such_event"}, {"event": "adopt", "level": "loud"}],
)
def test_unknown_event_or_level_is_rejected(kwargs: dict[str, Any], log_path: Path) -> None:
    with pytest.raises(ValueError):
        logs.EventLog(str(log_path)).write(**kwargs)
    assert not log_path.exists()


def test_unknown_min_level_is_rejected(log_path: Path) -> None:
    with pytest.raises(ValueError):
        logs.EventLog(str(log_path), min_level="loud")


def test_records_append_one_line_each(log_path: Path) -> None:
    log = logs.EventLog(str(log_path))
    log.write("adopt", message="line\nbreak")
    log.write("launch")
    lines = log_path.read_text(encoding="utf-8").split("\n")
    assert lines[-1] == ""
    assert [json.loads(line)["event"] for line in lines[:-1]] == ["adopt", "launch"]


def test_existing_content_is_kept(log_path: Path) -> None:
    log_path.write_text('{"old": 1}\n', encoding="utf-8")
    log_path.chmod(0o600)
    logs.EventLog(str(log_path)).write("adopt")
    assert log_path.read_text(encoding="utf-8").startswith('{"old": 1}\n')


def test_file_mode_is_0600(log_path: Path) -> None:
    old = os.umask(0)
    try:
        logs.EventLog(str(log_path)).write("adopt")
    finally:
        os.umask(old)
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_long_message_is_truncated(log_path: Path) -> None:
    logs.EventLog(str(log_path)).write("adopt", message="x" * (logs.MESSAGE_MAX_CHARS * 2))
    message = records(log_path)[0]["message"]
    assert len(message) <= logs.MESSAGE_MAX_CHARS
    assert message.endswith(logs.TRUNCATED)


# Secrets


def raw(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_secret_in_message_is_redacted(log_path: Path) -> None:
    log = logs.EventLog(str(log_path), secrets=[SECRET])
    log.write("hook_failed", message=f"token={SECRET} rejected")
    [record] = records(log_path)
    validate(record, LOG_SCHEMA)
    assert SECRET not in raw(log_path)
    assert record["message"] == f"token={logs.REDACTED} rejected"


def test_secret_in_every_string_field_is_redacted(log_path: Path) -> None:
    log = logs.EventLog(str(log_path), secrets=[SECRET])
    log.write(
        "launch",
        signal=SECRET,
        decision_ids=["ok", f"pre-{SECRET}-post"],
        message=SECRET,
    )
    assert SECRET not in raw(log_path)
    assert records(log_path)[0]["decision_ids"] == ["ok", f"pre-{logs.REDACTED}-post"]


def test_secret_in_exception_text_is_redacted(log_path: Path) -> None:
    log = logs.EventLog(str(log_path), secrets=[SECRET])
    try:
        raise RuntimeError(f"could not parse {{'token': '{SECRET}'}}")
    except RuntimeError as exc:
        log.write("hook_event_failed", level="error", message=logs.describe_exception(exc))
    assert SECRET not in raw(log_path)
    assert records(log_path)[0]["message"].startswith("RuntimeError: could not parse")


def test_secret_spanning_the_truncation_point_is_redacted_first(log_path: Path) -> None:
    padding = "x" * (logs.MESSAGE_MAX_CHARS - 5)
    logs.EventLog(str(log_path), secrets=[SECRET]).write("adopt", message=padding + SECRET)
    assert SECRET[:8] not in raw(log_path)


def test_overlapping_secrets_are_both_redacted(log_path: Path) -> None:
    secrets = ["abcdefgh", "efghijkl"]
    logs.EventLog(str(log_path), secrets=secrets).write("adopt", message="xxabcdefghijklyy")
    assert records(log_path)[0]["message"] == f"xx{logs.REDACTED}yy"


def test_secret_needing_json_escapes_is_redacted(log_path: Path) -> None:
    secret = 'quote"back\\slashé'
    logs.EventLog(str(log_path), secrets=[secret]).write("adopt", message=f"v={secret}")
    assert records(log_path)[0]["message"] == f"v={logs.REDACTED}"
    assert json.dumps(secret)[1:-1] not in raw(log_path)


def test_empty_secret_is_ignored(log_path: Path) -> None:
    logs.EventLog(str(log_path), secrets=["", SECRET]).write("adopt", message="plain")
    assert records(log_path)[0]["message"] == "plain"


def test_redact_walks_nested_values() -> None:
    value = {"a": [SECRET, {"b": (f"x{SECRET}", 3, None, True)}], "c": 1.5}
    assert logs.redact(value, [SECRET]) == {
        "a": [logs.REDACTED, {"b": [f"x{logs.REDACTED}", 3, None, True]}],
        "c": 1.5,
    }


def test_secret_values_from_env() -> None:
    environ = {"API_TOKEN": SECRET, "EMPTY": "", "OTHER": "visible"}
    names = ["API_TOKEN", "EMPTY", "MISSING"]
    assert logs.secret_values(names, environ) == frozenset({SECRET})


# Concurrency

WRITER = """
import sys
from claude_session import logs
log = logs.EventLog(sys.argv[1])
for i in range(int(sys.argv[2])):
    log.write("state_change", message=sys.argv[3] * 2000 + str(i))
"""


def test_concurrent_writers_never_interleave_lines(log_path: Path) -> None:
    count = 200
    procs = [
        subprocess.Popen([sys.executable, "-c", WRITER, str(log_path), str(count), tag])
        for tag in "abcd"
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    lines = records(log_path)
    assert len(lines) == 4 * count
    for record in lines:
        validate(record, LOG_SCHEMA)
        body = record["message"].rstrip("0123456789")
        assert body == body[0] * 2000
