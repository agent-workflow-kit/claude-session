"""log.jsonl writer (interface.md, Audit and log)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
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
    logs.EventLog(str(log_path)).write("adopt")
    [record] = records(log_path)
    validate(record, LOG_SCHEMA)
    assert record["event"] == "adopt"
    assert record["level"] == "info"


def test_full_record_validates(log_path: Path) -> None:
    log = logs.EventLog(str(log_path))
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


def test_events_levels_and_reasons_match_the_schema() -> None:
    schema = registry().contents(LOG_SCHEMA)
    assert set(logs.EVENTS) == set(schema["properties"]["event"]["enum"])
    assert list(logs.LEVELS) == schema["properties"]["level"]["enum"]
    assert set(logs.REASONS) | {None} == set(schema["properties"]["reason"]["enum"])


@pytest.mark.parametrize("reason", sorted(logs.REASONS))
def test_every_reason_validates(reason: str, log_path: Path) -> None:
    logs.EventLog(str(log_path)).write("report_rejected", reason=reason)
    validate(records(log_path)[0], LOG_SCHEMA)


def test_timestamp_is_utc_iso_8601(log_path: Path) -> None:
    now = datetime(2026, 10, 30, 12, 0, 0, 123456, tzinfo=UTC)
    logs.EventLog(str(log_path)).write("adopt", now=now)
    assert records(log_path)[0]["ts"] == "2026-10-30T12:00:00.123Z"


def test_naive_timestamp_is_rejected(log_path: Path) -> None:
    with pytest.raises(ValueError):
        logs.EventLog(str(log_path)).write("adopt", now=datetime(2026, 10, 30, 12, 0, 0))
    assert not log_path.exists()


def test_aware_non_utc_timestamp_is_converted(log_path: Path) -> None:
    now = datetime(2026, 10, 30, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    logs.EventLog(str(log_path)).write("adopt", now=now)
    assert records(log_path)[0]["ts"] == "2026-10-30T12:00:00.000Z"


def test_default_timestamp_is_now(log_path: Path) -> None:
    logs.EventLog(str(log_path)).write("adopt")
    ts = datetime.fromisoformat(records(log_path)[0]["ts"])
    assert ts.tzinfo is not None
    assert abs((datetime.now(UTC) - ts).total_seconds()) < 60


def test_every_level_is_written(log_path: Path) -> None:
    log = logs.EventLog(str(log_path))
    for level in logs.LEVELS:
        log.write("adopt", level=level)
    assert [r["level"] for r in records(log_path)] == list(logs.LEVELS)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event": "no_such_event"},
        {"event": "adopt", "level": "loud"},
        {"event": "report_rejected", "reason": "the agent said so"},
        {"event": "adopt", "csession_id": "cs-ABCD2345"},
        {"event": "adopt", "csession_id": "abcd2345"},
        {"event": "adopt", "csession_id": "cs-abcd23451"},
        {"event": "adopt", "run": True},
        {"event": "adopt", "run": "1"},
        {"event": "adopt", "pid": 1.5},
        {"event": "adopt", "pid": False},
        {"event": "adopt", "signal": 15},
        {"event": "adopt", "message": 3},
        {"event": "launch", "decision_ids": "d1"},
        {"event": "launch", "decision_ids": [1]},
    ],
)
def test_values_the_schema_rejects_are_rejected(kwargs: dict[str, Any], log_path: Path) -> None:
    with pytest.raises(ValueError):
        logs.EventLog(str(log_path)).write(**kwargs)
    assert not log_path.exists()


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


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (SID, True),
        ("cs-abcd2345a", False),  # too long, allowed characters only
        ("cs-abcd234", False),  # too short, allowed characters only
        ("cs-abcd23451", False),
        ("cs-ABCD2345", False),
        ("xs-abcd2345", False),
        (None, False),
    ],
)
def test_is_csession_id(value: object, valid: bool) -> None:
    assert logs.is_csession_id(value) is valid


LEAK = "s3cr3tVALUE"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event": f"x{LEAK}"},
        {"event": "adopt", "level": LEAK},
        {"event": "report_rejected", "reason": LEAK},
        {"event": "adopt", "csession_id": f"cs-{LEAK}"},
    ],
)
def test_rejection_messages_never_echo_the_value(kwargs: dict[str, Any], log_path: Path) -> None:
    with pytest.raises(ValueError) as info:
        logs.EventLog(str(log_path)).write(**kwargs)
    assert LEAK not in str(info.value)


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


def test_secret_in_every_text_field_is_redacted(log_path: Path) -> None:
    log = logs.EventLog(str(log_path), secrets=[SECRET])
    log.write(
        "launch",
        signal=SECRET,
        decision_ids=["ok", f"pre-{SECRET}-post"],
        message=SECRET,
    )
    assert SECRET not in raw(log_path)
    [record] = records(log_path)
    validate(record, LOG_SCHEMA)
    assert record["decision_ids"] == ["ok", f"pre-{logs.REDACTED}-post"]
    assert record["signal"] == logs.REDACTED


@pytest.mark.parametrize(
    "secret", ["launch", "info", "stop", "a", "o", "2026", "cs-", "abcd", "stale_r"]
)
def test_short_secrets_are_ignored(secret: str, log_path: Path) -> None:
    logs.EventLog(str(log_path), secrets=[secret]).write(
        "launch",
        level="info",
        csession_id=SID,
        run=2026,
        reason="stale_run",
        signal="stop",
        message=f"plain {secret} text",
    )
    [record] = records(log_path)
    validate(record, LOG_SCHEMA)
    assert record["message"] == f"plain {secret} text"
    assert record["csession_id"] == SID


def test_seven_characters_are_ignored_and_eight_redacted(log_path: Path) -> None:
    log = logs.EventLog(str(log_path), secrets=["abcdefg", "12345678"])
    log.write("adopt", message="abcdefg 12345678")
    assert records(log_path)[0]["message"] == f"abcdefg {logs.REDACTED}"


@pytest.mark.parametrize("secret", ["stale_run", "hook_event_failed", SID, "2026-10-30"])
def test_constrained_fields_are_never_redacted(secret: str, log_path: Path) -> None:
    now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
    logs.EventLog(str(log_path), secrets=[secret]).write(
        "hook_event_failed",
        level="error",
        csession_id=SID,
        reason="stale_run",
        message=f"x {secret} y",
        now=now,
    )
    [record] = records(log_path)
    validate(record, LOG_SCHEMA)
    assert record["event"] == "hook_event_failed"
    assert record["reason"] == "stale_run"
    assert record["csession_id"] == SID
    assert record["ts"] == "2026-10-30T12:00:00.000Z"
    assert record["message"] == f"x {logs.REDACTED} y"


def test_secret_spanning_the_truncation_point_is_redacted_first(log_path: Path) -> None:
    # The secret starts 5 characters before the cut: truncating first would keep SECRET[:5].
    padding = "x" * (logs.MESSAGE_MAX_CHARS - len(logs.TRUNCATED) - 5)
    logs.EventLog(str(log_path), secrets=[SECRET]).write("adopt", message=padding + SECRET)
    assert SECRET[:5] not in raw(log_path)


def test_secret_straddling_the_pre_cut_is_not_partly_kept(log_path: Path) -> None:
    # Long secrets collapse into one short marker, so text near the pre-cut ends up inside
    # the final cut: a secret cut in half there must not survive in part.
    long_secret = "L" * 200
    tail_secret = "QWERTYUIOPASDFGHJKLZ"
    limit = 2 * logs.MESSAGE_MAX_CHARS + len(long_secret)
    head = long_secret * 30
    message = head + "x" * (limit - 10 - len(head)) + tail_secret + "y" * 100
    log = logs.EventLog(str(log_path), secrets=[long_secret, tail_secret])
    log.write("adopt", message=message)
    text = records(log_path)[0]["message"]
    assert "QWERTYUIOP" not in text
    assert "Q" not in text
    assert text.endswith(logs.TRUNCATED)
    assert len(text) <= logs.MESSAGE_MAX_CHARS


def test_full_secret_before_a_pre_cut_secret_prefix_stays_redacted(log_path: Path) -> None:
    # The pre-cut text ends in a whole secret A whose end is a prefix of secret B: dropping
    # that prefix must not leave part of A unredacted.
    a, b = "AAAAAAAAzzz", "zzzBBBBBBBB"
    limit = 2 * logs.MESSAGE_MAX_CHARS + len(b)
    message = "x" * (limit - len(a)) + a + "w" * 50
    logs.EventLog(str(log_path), secrets=[a, b]).write("adopt", message=message)
    text = records(log_path)[0]["message"]
    assert "AAAA" not in text
    assert text.endswith(logs.TRUNCATED)


def run_bounded(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter with a subprocess timeout, so a performance
    regression fails the test instead of hanging the suite."""
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


MANY_OVERLAPPING = """
import sys
from claude_session import logs
secrets = ["a" * n for n in range(8, 200)]
logs.EventLog(sys.argv[1], secrets=secrets).write("adopt", message="a" * 2_000_000)
"""


def test_many_overlapping_secrets_in_a_large_message_finish_quickly(log_path: Path) -> None:
    proc = run_bounded(MANY_OVERLAPPING, str(log_path))
    assert (proc.returncode, proc.stderr) == (0, "")
    assert records(log_path)[0]["message"] == logs.REDACTED + logs.TRUNCATED


LONG_OVERLAPPING = """
import sys
from claude_session import logs
log = logs.EventLog(sys.argv[1], secrets=["a" * 50_000])
log.write("signal_sent", signal="a" * 1_000_000)
"""


def test_long_overlapping_secret_in_a_long_text_field_finishes_quickly(log_path: Path) -> None:
    # Overlapping occurrences of one long secret in a field without a pre-cut (signal).
    proc = run_bounded(LONG_OVERLAPPING, str(log_path))
    assert (proc.returncode, proc.stderr) == (0, "")
    assert records(log_path)[0]["signal"] == logs.REDACTED


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("aaaaaaaaaaaa", "aaaaaaaa"),
        ("xabababababababy", "abababab"),
        ("abcabcabXabcabcab", "abcabcab"),
        ("zzabcdefghzzabcdefgh", "abcdefgh"),
        ("abaabaabaabaab", "abaabaab"),
    ],
)
def test_overlapping_occurrences_redact_their_union(text: str, secret: str) -> None:
    covered = bytearray(len(text))
    for start in range(len(text)):
        if text.startswith(secret, start):
            covered[start : start + len(secret)] = b"\x01" * len(secret)
    assert logs._secret_mask(text, [secret]) == covered


def test_whole_field_replaced_after_the_pre_cut_has_no_truncation_marker(log_path: Path) -> None:
    secrets = ["AAAAAAAA", "ted]BBBBBB"]
    limit = 2 * logs.MESSAGE_MAX_CHARS + max(map(len, secrets))
    message = "xAAAAAAAABBBBBBy" + "z" * limit
    logs.EventLog(str(log_path), secrets=secrets).write("adopt", message=message)
    assert records(log_path)[0]["message"] == logs.REDACTED


def test_csession_id_pattern_matches_the_schemas() -> None:
    log_schema = registry().contents(LOG_SCHEMA)
    assert log_schema["properties"]["csession_id"]["pattern"] == logs.CSESSION_ID_PATTERN


@pytest.mark.parametrize(
    "value",
    ["cs-abcd2345", "cs-ABCD2345", "cs-abcd1345", "cs-abcd234", "cs-abcd23456", "xs-abcd2345"],
)
def test_is_csession_id_agrees_with_the_pattern(value: str) -> None:
    import re

    assert logs.is_csession_id(value) == bool(re.fullmatch(logs.CSESSION_ID_PATTERN, value))


def test_truncation_never_splits_a_marker(log_path: Path) -> None:
    keep = logs.MESSAGE_MAX_CHARS - len(logs.TRUNCATED)
    message = "x" * (keep - 3) + SECRET + "y" * 100
    logs.EventLog(str(log_path), secrets=[SECRET]).write("adopt", message=message)
    assert records(log_path)[0]["message"] == "x" * (keep - 3) + logs.TRUNCATED


def test_overlapping_secrets_are_both_redacted(log_path: Path) -> None:
    secrets = ["abcdefgh", "efghijkl"]
    logs.EventLog(str(log_path), secrets=secrets).write("adopt", message="xxabcdefghijklyy")
    assert records(log_path)[0]["message"] == f"xx{logs.REDACTED}yy"


def test_adjacent_secrets_collapse_into_one_marker(log_path: Path) -> None:
    secrets = ["abcdefgh", "ijklmnop"]
    logs.EventLog(str(log_path), secrets=secrets).write("adopt", message="xxabcdefghijklmnopyy")
    assert records(log_path)[0]["message"] == f"xx{logs.REDACTED}yy"


def test_secret_formed_across_a_marker_redacts_the_whole_field(log_path: Path) -> None:
    secrets = ["AAAAAAAA", "ted]BBBBBB"]
    logs.EventLog(str(log_path), secrets=secrets).write("adopt", message="xAAAAAAAABBBBBBy")
    assert records(log_path)[0]["message"] == logs.REDACTED
    assert "ted]BBBBBB" not in raw(log_path)


def test_secret_formed_with_the_truncation_marker_is_redacted(log_path: Path) -> None:
    secret = "zz[truncated]"
    message = "x" * (logs.MESSAGE_MAX_CHARS - len(logs.TRUNCATED) - 2) + "z" * 20
    logs.EventLog(str(log_path), secrets=[secret]).write("adopt", message=message)
    assert records(log_path)[0]["message"] == logs.REDACTED


@pytest.mark.parametrize("text", ["redacted", "[redacted]", "a redacted b redacted", "xredactedy"])
def test_secret_equal_to_the_marker_text_terminates(text: str) -> None:
    out = logs.redact_text(text, ["redacted"])
    assert not logs.leaks_secret(out, ["redacted"])
    assert "redacted" not in out.replace(logs.REDACTED, "")


def test_secret_needing_json_escapes_is_redacted(log_path: Path) -> None:
    secret = 'quote"back\\slashé'
    logs.EventLog(str(log_path), secrets=[secret]).write("adopt", message=f"v={secret}")
    assert records(log_path)[0]["message"] == f"v={logs.REDACTED}"
    assert json.dumps(secret)[1:-1] not in raw(log_path)


def test_empty_secret_is_ignored(log_path: Path) -> None:
    logs.EventLog(str(log_path), secrets=["", SECRET]).write("adopt", message="plain")
    assert records(log_path)[0]["message"] == "plain"


def test_secret_values_from_env() -> None:
    environ = {"API_TOKEN": SECRET, "EMPTY": "", "SHORT": "1234567", "OTHER": "visible"}
    names = ["API_TOKEN", "EMPTY", "SHORT", "MISSING"]
    assert logs.secret_values(environ, secret_env=names) == frozenset({SECRET})


def test_secret_values_include_claude_auth_env_and_the_run_secret() -> None:
    environ = {"ANTHROPIC_API_KEY": "sk-auth-value-123", "API_TOKEN": SECRET}
    run_secret = "0" * 64
    values = logs.secret_values(
        environ,
        secret_env=["API_TOKEN"],
        claude_auth_env=["ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"],
        run_secret=run_secret,
    )
    assert values == frozenset({SECRET, "sk-auth-value-123", run_secret})


def test_claude_auth_env_value_is_redacted(log_path: Path) -> None:
    secrets = logs.secret_values(
        {"ANTHROPIC_API_KEY": "sk-auth-value-123"}, claude_auth_env=["ANTHROPIC_API_KEY"]
    )
    logs.EventLog(str(log_path), secrets=secrets).write("adopt", message="k=sk-auth-value-123")
    assert "sk-auth-value-123" not in raw(log_path)


def test_nan_is_rejected(log_path: Path) -> None:
    with pytest.raises(ValueError):
        logs.append_record(str(log_path), {"ts": "x", "n": float("nan")})
    assert not log_path.exists()


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
