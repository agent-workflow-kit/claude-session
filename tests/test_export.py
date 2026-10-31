"""``export`` (interface.md, export; Audit and log): audit records as written, oldest first,
filtered by ``--since`` and ``--action``; every unreadable line skipped, never trusted; the
file read without following a symlink; human output one inert line per record."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from claude_session import audit, exporter, store, terminal
from claude_session.cli import main

from .golden import assert_golden
from .schemas import validate

EXPORT_SCHEMA = "claude-session/cli/export.json"
AUDIT_SCHEMA = "claude-session/audit.json"
T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
HOSTILE = (
    "evil\x1b]0;pwned\x07\x1b[2J\r\nfake: line\x9b31m\u202eetc\u2028x\x00\x7f"
    "\x1b]52;c;ZXZpbA==\x07zero\u200bwidth\ufeffbom\u2066iso\u2069\u2029para"
    "\\x1b fake escape result=ok"
)


@pytest.fixture
def audit_file() -> Path:
    root = Path(store.state_dir())
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / "audit.jsonl"


def write(path: Path, *, at: datetime = T0, **kwargs: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "actor": "human:tester",
        "target": "cs-aaaaaaaa",
        "action": "stop",
        "result": "ok",
    }
    args.update(kwargs)
    return audit.append(str(path), now=at, **args)


def seed(path: Path) -> list[dict[str, Any]]:
    """One record of every action, an hour apart, plus a refused and an error record."""
    return [
        write(
            path,
            at=T0,
            action="resume_tainted",
            target="cs-aaaaaaaa",
            decision_ids=["gate-1"],
            inputs={"run_kind": "implement", "kind": "review", "run": 2, "tty": True},
        ),
        write(
            path,
            at=T0 + timedelta(hours=1),
            action="launch_escalation_override",
            target="cs-bbbbbbbb",
            inputs={"run": 3, "overridden_hooks": ["4f2a"], "tty": False},
        ),
        write(
            path,
            at=T0 + timedelta(hours=2),
            action="sandbox_lowered",
            target="cs-cccccccc",
            actor="auto:queue",
            inputs={
                "run_kind": "review",
                "run": 1,
                "forked_from": "cs-aaaaaaaa",
                "labels": ["team=core", "prio=high"],
                "sandbox_from": "required",
                "sandbox_to": "off",
            },
        ),
        write(
            path,
            at=T0 + timedelta(hours=3),
            action="stop",
            target="cs-dddddddd",
            inputs={"run": 1, "grace": 10},
        ),
        write(
            path, at=T0 + timedelta(hours=4), action="stop", target="cs-dddddddd", result="error"
        ),
        write(path, at=T0 + timedelta(hours=5), action="forget", target="cs-eeeeeeee"),
        write(
            path,
            at=T0 + timedelta(hours=6),
            action="forget",
            target="cs-ffffffff",
            result="refused",
            inputs=None,
        ),
    ]


def run_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    code = main(["export", *argv, "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def exported(argv: list[str], capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    code, records, err = run_json(argv, capsys)
    assert code == 0, records
    assert err == ""
    validate(records, EXPORT_SCHEMA)
    return records


def targets(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(r["action"], r["target"]) for r in records]


# --json output


def test_no_audit_file_is_an_empty_array(capsys: pytest.CaptureFixture[str]) -> None:
    code, records, err = run_json([], capsys)
    assert (code, err) == (0, "")
    assert_golden(records, "export-empty")


def test_empty_audit_file_is_an_empty_array(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit_file.write_bytes(b"")
    assert exported([], capsys) == []


def test_every_record_as_written(audit_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    written = seed(audit_file)
    records = exported([], capsys)
    assert records == written
    assert_golden(records, "export-records")


def test_each_record_validates_against_the_audit_schema(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(audit_file)
    for record in exported([], capsys):
        validate(record, AUDIT_SCHEMA)


def test_output_is_the_file_and_nothing_more(
    audit_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No field is added or rewritten: export never re-introduces a value (a secret in the
    environment of the exporting process included)."""
    secret = "s3cr3t-Value-0123456789"
    write(audit_file, actor=f"human:{secret}", secrets=[secret], decision_ids=[secret])
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    main(["export", "--json"])
    out = capsys.readouterr().out
    assert secret not in out
    assert json.loads(out) == [json.loads(line) for line in audit_file.read_text().splitlines()]


# Filters


def test_action_filter(audit_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(audit_file)
    assert targets(exported(["--action", "stop"], capsys)) == [
        ("stop", "cs-dddddddd"),
        ("stop", "cs-dddddddd"),
    ]
    assert targets(exported(["--action", "forget", "--action", "resume_tainted"], capsys)) == [
        ("resume_tainted", "cs-aaaaaaaa"),
        ("forget", "cs-eeeeeeee"),
        ("forget", "cs-ffffffff"),
    ]
    assert targets(exported(["--action", "stop", "--action", "stop"], capsys)) == [
        ("stop", "cs-dddddddd"),
        ("stop", "cs-dddddddd"),
    ]


@pytest.mark.parametrize("action", sorted(audit.ACTIONS))
def test_action_filter_accepts_every_audit_action(
    audit_file: Path, action: str, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(audit_file)
    records = exported(["--action", action], capsys)
    assert records
    assert {r["action"] for r in records} == {action}


@pytest.mark.parametrize("action", ["launch", "STOP", "stop ", "", "stop,forget", "sto"])
def test_unknown_action_exits_2(
    audit_file: Path, action: str, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(audit_file)
    code, envelope, _ = run_json(["--action", action], capsys)
    assert code == 2
    assert envelope["error"]["code"] == "usage"


def test_since_iso_time_is_inclusive(audit_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(audit_file)
    cut = "2026-10-30T15:00:00Z"
    assert targets(exported(["--since", cut], capsys)) == [
        ("stop", "cs-dddddddd"),
        ("stop", "cs-dddddddd"),
        ("forget", "cs-eeeeeeee"),
        ("forget", "cs-ffffffff"),
    ]
    # The same instant in another offset, and without one (UTC).
    assert len(exported(["--since", "2026-10-30T16:00:00+01:00"], capsys)) == 4
    assert len(exported(["--since", "2026-10-30T15:00:00"], capsys)) == 4
    assert len(exported(["--since", "2026-10-30T15:00:00.001Z"], capsys)) == 3
    assert exported(["--since", "2999-01-01"], capsys) == []


def test_since_duration_counts_back_from_now(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = datetime.now(UTC)
    write(audit_file, at=now - timedelta(days=2), target="cs-aaaaaaaa")
    write(audit_file, at=now - timedelta(minutes=30), target="cs-bbbbbbbb")
    assert [r["target"] for r in exported(["--since", "1h"], capsys)] == ["cs-bbbbbbbb"]
    assert [r["target"] for r in exported(["--since", "3d"], capsys)] == [
        "cs-aaaaaaaa",
        "cs-bbbbbbbb",
    ]
    assert exported(["--since", "0"], capsys) == []


def test_since_and_action_combine(audit_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed(audit_file)
    records = exported(["--since", "2026-10-30T15:30:00Z", "--action", "stop"], capsys)
    assert [(r["target"], r["result"]) for r in records] == [("cs-dddddddd", "error")]


@pytest.mark.parametrize(
    "since",
    [
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
        "0001-01-01",
        "0999-12-31T23:59:59-01:00",
        "36500d",
    ],
)
def test_since_at_the_edges_of_time(
    audit_file: Path, since: str, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(audit_file)
    expected = 0 if since.startswith("9999") else 7
    assert len(exported(["--since", since], capsys)) == expected


@pytest.mark.parametrize("since", ["yesterday", "5 m", "-1h", "1w", "2026-13-01", ""])
def test_bad_since_exits_2(since: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, envelope, _ = run_json(["--since", since], capsys)
    assert code == 2
    assert envelope["error"]["code"] == "usage"


# Order


def test_oldest_first_by_timestamp_not_file_order(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clock that stepped back makes file order differ from time order; ties keep file
    order."""
    write(audit_file, at=T0 + timedelta(hours=2), target="cs-cccccccc")
    write(audit_file, at=T0, target="cs-aaaaaaaa")
    write(audit_file, at=T0 + timedelta(hours=1), target="cs-bbbbbbbb", action="forget")
    write(audit_file, at=T0 + timedelta(hours=1), target="cs-bbbbbbbb", action="stop")
    assert targets(exported([], capsys)) == [
        ("stop", "cs-aaaaaaaa"),
        ("forget", "cs-bbbbbbbb"),
        ("stop", "cs-bbbbbbbb"),
        ("stop", "cs-cccccccc"),
    ]


def test_order_compares_instants_across_offsets(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = json.dumps(write(audit_file, at=T0, target="cs-bbbbbbbb"))
    # 11:30 UTC written with a +01:00 offset: text order would put it after 12:00Z.
    early = json.loads(good) | {"ts": "2026-10-30T12:30:00.000+01:00", "target": "cs-aaaaaaaa"}
    with audit_file.open("a") as f:
        f.write(json.dumps(early) + "\n")
    assert [r["target"] for r in exported([], capsys)] == ["cs-aaaaaaaa", "cs-bbbbbbbb"]


# Unreadable lines


def good_record(**changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ts": "2026-10-30T12:00:00.000Z",
        "actor": "human:tester",
        "decision_id": [],
        "target": "cs-aaaaaaaa",
        "action": "stop",
        "inputs": None,
        "result": "ok",
    }
    record.update(changes)
    return {k: v for k, v in record.items() if v is not ...}


def line(record: dict[str, Any]) -> bytes:
    return json.dumps(record).encode() + b"\n"


BAD_LINES: dict[str, bytes] = {
    "not json": b"not json at all\n",
    "blank": b"\n",
    "whitespace": b"   \t \n",
    "array": b"[1, 2]\n",
    "string": b'"stop"\n',
    "null": b"null\n",
    "truncated": line(good_record())[:40] + b"\n",
    "invalid utf-8": line(good_record(actor="human:x"))[:-1].replace(b"x", b"\xff") + b"\n",
    "nul byte": b'{"ts": \x00}\n',
    "missing field": line(good_record(result=...)),
    "extra field": line(good_record(secret="x")),
    "unknown action": line(good_record(action="launch")),
    "unknown result": line(good_record(result="maybe")),
    "bad target": line(good_record(target="cs-AAAAAAAA")),
    "short id": line(good_record(target="cs-aaaa")),
    "target with newline": line(good_record(target="cs-aaaaaaaa\n")),
    "actor not a string": line(good_record(actor=5)),
    "decision_id string": line(good_record(decision_id="d")),
    "decision_id item": line(good_record(decision_id=["a", 1])),
    "inputs list": line(good_record(inputs=[])),
    "inputs unknown key": line(good_record(inputs={"secret": "x"})),
    "inputs bad label": line(good_record(inputs={"labels": ["Team=core"]})),
    "inputs label newline": line(good_record(inputs={"labels": ["a=b\n"]})),
    "inputs bad kind": line(good_record(inputs={"kind": "../x"})),
    "inputs run float": line(good_record(inputs={"run": 1.0})),
    "inputs run bool": line(good_record(inputs={"run": True})),
    "inputs tty int": line(good_record(inputs={"tty": 1})),
    "inputs sandbox": line(good_record(inputs={"sandbox_to": "none"})),
    "inputs bad fork": line(good_record(inputs={"forked_from": "x"})),
    "inputs grace negative": line(good_record(inputs={"grace": -1})),
    "inputs grace huge": line(good_record(inputs={"grace": 2**53 + 1})),
    "inputs grace nan": b'{"ts":"2026-10-30T12:00:00.000Z","actor":"a","decision_id":[],'
    b'"target":"cs-aaaaaaaa","action":"stop","inputs":{"grace":NaN},"result":"ok"}\n',
    "inputs grace inf": b'{"ts":"2026-10-30T12:00:00.000Z","actor":"a","decision_id":[],'
    b'"target":"cs-aaaaaaaa","action":"stop","inputs":{"grace":1e999},"result":"ok"}\n',
    "inputs hooks string": line(good_record(inputs={"overridden_hooks": "h"})),
    "ts not a string": line(good_record(ts=0)),
    "ts no zone": line(good_record(ts="2026-10-30T12:00:00.000")),
    "ts date only": line(good_record(ts="2026-10-30")),
    "ts lowercase": line(good_record(ts="2026-10-30t12:00:00z")),
    "ts space": line(good_record(ts="2026-10-30 12:00:00Z")),
    "ts month 13": line(good_record(ts="2026-13-30T12:00:00Z")),
    "ts hour 24": line(good_record(ts="2026-10-30T24:00:00Z")),
    "ts leap second": line(good_record(ts="2026-12-31T23:59:60Z")),
    "ts week date": line(good_record(ts="2026-W44-5T12:00:00Z")),
    "ts unicode digits": line(good_record(ts="２026-10-30T12:00:00Z")),
    "ts trailing": line(good_record(ts="2026-10-30T12:00:00Zjunk")),
    "duplicate key": b'{"ts":"2026-10-30T12:00:00.000Z","actor":"a","actor":"b","decision_id":[],'
    b'"target":"cs-aaaaaaaa","action":"stop","inputs":null,"result":"ok"}\n',
    "deep nesting": b'{"ts":' + b"[" * 100_000 + b"]" * 100_000 + b"}\n",
    "two records on a line": line(good_record())[:-1] + line(good_record()),
}


@pytest.mark.parametrize("name", sorted(BAD_LINES))
def test_an_unreadable_line_is_skipped_and_counted(
    audit_file: Path, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    write(audit_file, at=T0, target="cs-aaaaaaaa")
    with audit_file.open("ab") as f:
        f.write(BAD_LINES[name])
    write(audit_file, at=T0 + timedelta(seconds=1), target="cs-bbbbbbbb")
    code, records, err = run_json([], capsys)
    assert code == 0
    validate(records, EXPORT_SCHEMA)
    assert [r["target"] for r in records] == ["cs-aaaaaaaa", "cs-bbbbbbbb"]
    assert err == "claude-session: warning: skipped 1 unreadable line in audit.jsonl\n"


def test_an_unreadable_line_never_reaches_the_output(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with audit_file.open("ab") as f:
        f.write(line(good_record(actor="human:x", extra=HOSTILE)))
        f.write(line(good_record(inputs={"labels": [HOSTILE]})))
    for argv in ([], ["--json"]):
        main(["export", *argv])
        out = capsys.readouterr().out
        assert "evil" not in out


def test_a_half_written_last_line_is_skipped(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(audit_file, target="cs-aaaaaaaa")
    with audit_file.open("ab") as f:
        f.write(line(good_record(target="cs-bbbbbbbb"))[:-20])
    code, records, err = run_json([], capsys)
    assert code == 0
    assert [r["target"] for r in records] == ["cs-aaaaaaaa"]
    assert "skipped 1 unreadable line" in err


def test_a_complete_last_record_without_its_newline_is_read(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The next append ends such a line with a newline (fsutil.append_private_line), so it is
    read now as it will be read then."""
    write(audit_file, target="cs-aaaaaaaa")
    with audit_file.open("ab") as f:
        f.write(line(good_record(target="cs-bbbbbbbb"))[:-1])
    assert [r["target"] for r in exported([], capsys)] == ["cs-aaaaaaaa", "cs-bbbbbbbb"]
    write(audit_file, target="cs-cccccccc")
    assert [r["target"] for r in exported([], capsys)] == [
        "cs-aaaaaaaa",
        "cs-bbbbbbbb",
        "cs-cccccccc",
    ]


def test_a_crlf_line_ending_is_unreadable(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The writer ends lines with LF only; JSON allows the CR as whitespace, but a line the
    writer cannot have written is not trusted either way, so it is read like any record."""
    with audit_file.open("ab") as f:
        f.write(line(good_record())[:-1] + b"\r\n")
    assert len(exported([], capsys)) == 1


def test_many_unreadable_lines_are_counted(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with audit_file.open("ab") as f:
        f.write(b"x\n" * 5)
    code, records, err = run_json([], capsys)
    assert (code, records) == (0, [])
    assert err == "claude-session: warning: skipped 5 unreadable lines in audit.jsonl\n"


def test_quiet_hides_the_skip_warning(audit_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with audit_file.open("ab") as f:
        f.write(b"x\n")
    assert main(["export", "--json", "--quiet"]) == 0
    captured = capsys.readouterr()
    assert (json.loads(captured.out), captured.err) == ([], "")


def test_an_overlong_line_is_skipped_without_holding_it(
    audit_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter, "MAX_LINE_BYTES", 4096)
    monkeypatch.setattr(exporter, "CHUNK_BYTES", 1000)
    write(audit_file, target="cs-aaaaaaaa")
    write(audit_file, actor="human:" + "x" * 5000, target="cs-bbbbbbbb")  # a record, too long
    write(audit_file, actor="human:" + "y" * 3900, target="cs-cccccccc")  # within the cap
    with audit_file.open("ab") as f:
        f.write(b"z" * 20_000)  # an overlong unterminated tail
    code, records, err = run_json([], capsys)
    assert code == 0
    assert [r["target"] for r in records] == ["cs-aaaaaaaa", "cs-cccccccc"]
    assert "skipped 2 unreadable lines" in err


def test_a_line_exactly_at_the_cap_is_read(
    audit_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = line(good_record(actor="human:"))[:-1]
    size = 2048
    monkeypatch.setattr(exporter, "MAX_LINE_BYTES", size)
    monkeypatch.setattr(exporter, "CHUNK_BYTES", 7)
    actor = "human:" + "a" * (size - len(probe))  # the line, newline excluded, is `size` bytes
    assert len(line(good_record(actor=actor, target="cs-aaaaaaaa"))) == size + 1
    with audit_file.open("ab") as f:
        f.write(line(good_record(actor=actor, target="cs-aaaaaaaa")))
        f.write(line(good_record(actor=actor + "a", target="cs-bbbbbbbb")))
    code, records, err = run_json([], capsys)
    assert [r["target"] for r in records] == ["cs-aaaaaaaa"]
    assert "skipped 1 unreadable line" in err


def test_only_bytes_present_at_open_are_read(
    audit_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records appended while export reads are left for the next export, so a busy writer
    cannot keep it reading."""
    write(audit_file, target="cs-aaaaaaaa")
    real_read = os.read
    appended: list[bool] = []

    def read_then_append(fd: int, n: int) -> bytes:
        data = real_read(fd, n)
        if not appended:
            appended.append(True)
            write(audit_file, target="cs-bbbbbbbb")
        return data

    monkeypatch.setattr(exporter.os, "read", read_then_append)
    assert [r["target"] for r in exported([], capsys)] == ["cs-aaaaaaaa"]


# Unsafe files


def test_a_symlinked_audit_file_is_refused_unread(
    audit_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = tmp_path / "elsewhere.jsonl"
    write(elsewhere, target="cs-aaaaaaaa")
    audit_file.symlink_to(elsewhere)
    code, envelope, err = run_json([], capsys)
    assert code == 12
    assert envelope["error"]["code"] == "failed"
    validate(envelope, "claude-session/cli/error.json")
    assert "cs-aaaaaaaa" not in err


def test_a_dangling_symlink_is_refused(
    audit_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit_file.symlink_to(tmp_path / "missing.jsonl")
    code, envelope, _ = run_json([], capsys)
    assert (code, envelope["error"]["code"]) == (12, "failed")


def test_a_fifo_is_refused_without_blocking(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    os.mkfifo(audit_file, 0o600)
    code, envelope, _ = run_json([], capsys)
    assert (code, envelope["error"]["code"]) == (12, "failed")
    assert "not a regular file" in envelope["error"]["message"]


def test_a_directory_is_refused(audit_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    audit_file.mkdir()
    code, envelope, _ = run_json([], capsys)
    assert (code, envelope["error"]["code"]) == (12, "failed")


def test_a_hard_linked_audit_file_is_refused(
    audit_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(audit_file)
    os.link(audit_file, tmp_path / "other-name")
    code, envelope, _ = run_json([], capsys)
    assert (code, envelope["error"]["code"]) == (12, "failed")
    assert "links" in envelope["error"]["message"]


def test_an_unreadable_audit_file_fails(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    write(audit_file)
    audit_file.chmod(0o200)
    code, envelope, _ = run_json([], capsys)
    assert (code, envelope["error"]["code"]) == (12, "failed")


def test_a_symlinked_state_dir_component_is_followed_like_the_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state dir is resolved once (as the writer resolves it) and then walked without
    following symlinks, so the reader finds the file the writer appends to."""
    real = tmp_path / "real-state"
    (real / "claude-session").mkdir(parents=True, mode=0o700)
    link = tmp_path / "linked-state"
    link.symlink_to(real)
    monkeypatch.setenv("XDG_STATE_HOME", str(link))
    write(real / "claude-session" / "audit.jsonl", target="cs-aaaaaaaa")
    assert [r["target"] for r in exported([], capsys)] == ["cs-aaaaaaaa"]


# Human output


def test_human_output_is_one_line_per_record(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(audit_file)
    assert main(["export"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "2026-10-30T12:00:00.000Z resume_tainted cs-aaaaaaaa ok actor=human:tester"
        " decision_id=gate-1 run_kind=implement kind=review run=2 tty=true",
        "2026-10-30T13:00:00.000Z launch_escalation_override cs-bbbbbbbb ok"
        " actor=human:tester decision_id=- run=3 overridden_hooks=4f2a tty=false",
        "2026-10-30T14:00:00.000Z sandbox_lowered cs-cccccccc ok actor=auto:queue"
        " decision_id=- run_kind=review run=1 forked_from=cs-aaaaaaaa"
        " labels=team=core,prio=high sandbox_from=required sandbox_to=off",
        "2026-10-30T15:00:00.000Z stop cs-dddddddd ok actor=human:tester decision_id=- run=1"
        " grace=10",
        "2026-10-30T16:00:00.000Z stop cs-dddddddd error actor=human:tester decision_id=-",
        "2026-10-30T17:00:00.000Z forget cs-eeeeeeee ok actor=human:tester decision_id=-",
        "2026-10-30T18:00:00.000Z forget cs-ffffffff refused actor=human:tester decision_id=-",
    ]


def test_human_output_shows_nulls_and_empty_lists(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        audit_file,
        inputs={"run_kind": None, "run": None, "labels": [], "grace": 2.5, "sandbox_to": None},
    )
    main(["export"])
    assert capsys.readouterr().out == (
        "2026-10-30T12:00:00.000Z stop cs-aaaaaaaa ok actor=human:tester decision_id=-"
        " run_kind=- run=- labels=- grace=2.5 sandbox_to=-\n"
    )


def test_human_output_without_records_is_empty(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["export"]) == 0
    assert capsys.readouterr().out == ""


def test_human_output_makes_hostile_text_inert(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        audit_file,
        actor=HOSTILE,
        decision_ids=[HOSTILE, "a,b", "c d"],
        inputs={"overridden_hooks": [HOSTILE, "x y"]},
    )
    assert main(["export"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    text = out[:-1]
    assert all(char.isprintable() for char in text), text
    for char in "\x1b\x07\r\x00\x7f\x9b\u202e\u2028\u2029\u200b\ufeff\u2066":
        assert char not in text
    # Every field is one space-free token: a value cannot pass for another field.
    tokens = text.split(" ")
    assert tokens[:4] == ["2026-10-30T12:00:00.000Z", "stop", "cs-aaaaaaaa", "ok"]
    assert [t.partition("=")[0] for t in tokens[4:]] == [
        "actor",
        "decision_id",
        "overridden_hooks",
    ]
    assert tokens[5].endswith(",a\\x2cb,c\\x20d")
    assert tokens[6].endswith(",x\\x20y")


def test_human_output_escapes_surrogates(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with audit_file.open("ab") as f:
        f.write(line(good_record(actor="human:\udcff")))
    main(["export"])
    out = capsys.readouterr().out
    assert "actor=human:\\udcff " in out
    out.encode("utf-8")


# JSON output safety


def test_json_output_is_strict_utf8_for_a_lone_surrogate(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An actor from a non-UTF-8 argv byte is stored as a lone surrogate escape; the output
    replaces it with U+FFFD, as error envelopes do."""
    with audit_file.open("ab") as f:
        f.write(line(good_record(actor="human:\udcff", decision_id=["\ud800"])))
        f.write(line(good_record(inputs={"overridden_hooks": ["h\udfff"]})))
    code, records, _ = run_json([], capsys)
    assert code == 0
    validate(records, EXPORT_SCHEMA)
    assert records[0]["actor"] == "human:�"
    assert records[0]["decision_id"] == ["�"]
    assert records[1]["inputs"]["overridden_hooks"] == ["h�"]
    json.dumps(records, ensure_ascii=False).encode("utf-8")


def test_json_output_keeps_unicode_text(
    audit_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(audit_file, actor="human:é 山", decision_ids=["\u202eodd"])
    [record] = exported([], capsys)
    assert record["actor"] == "human:é 山"
    assert record["decision_id"] == ["\u202eodd"]


# Reader unit checks


def test_read_records_reports_the_skip_count(audit_file: Path) -> None:
    write(audit_file)
    with audit_file.open("ab") as f:
        f.write(b"junk\n{}\n")
    result = exporter.read_records(str(audit_file))
    assert len(result.records) == 1
    assert result.skipped == 2


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        ("2026-10-30T12:00:00Z", datetime(2026, 10, 30, 12, tzinfo=UTC)),
        ("2026-10-30T12:00:00.5Z", datetime(2026, 10, 30, 12, 0, 0, 500000, tzinfo=UTC)),
        ("2026-10-30T12:00:00.123456789Z", datetime(2026, 10, 30, 12, 0, 0, 123456, tzinfo=UTC)),
        ("2026-10-30T13:00:00+01:00", datetime(2026, 10, 30, 12, tzinfo=UTC)),
    ],
)
def test_parse_ts_reads_rfc3339_times(ts: str, expected: datetime) -> None:
    assert audit.parse_ts(ts) == expected
    assert audit.parse_record(line(good_record(ts=ts))[:-1]) is not None


def test_parse_record_never_returns_an_invalid_record() -> None:
    record = audit.parse_record(line(good_record())[:-1])
    assert record is not None
    validate(record, AUDIT_SCHEMA)
    assert audit.parse_record(b"") is None
    assert audit.parse_record(b"{") is None


def test_terminal_display_is_the_sanitizer(audit_file: Path) -> None:
    """The human line goes through terminal.display, the one sanitizer."""
    rec = good_record(actor="a\x1bb")
    assert terminal.display("a\x1bb", " ") in exporter.record_line(rec)
