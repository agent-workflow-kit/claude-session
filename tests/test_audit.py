"""audit.jsonl writer (interface.md, Audit and log)."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from claude_session import audit

from .schemas import registry, validate

AUDIT_SCHEMA = "claude-session/audit.json"
SECRET = "s3cr3t-Value-0123456789"
SID = "cs-abcd2345"


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


def write(path: Path, **kwargs: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "actor": "human:tester",
        "target": SID,
        "action": "stop",
        "result": "ok",
    }
    args.update(kwargs)
    return audit.append(str(path), **args)


def test_minimal_record_validates(audit_path: Path) -> None:
    returned = write(audit_path)
    [record] = records(audit_path)
    validate(record, AUDIT_SCHEMA)
    assert record == returned
    assert record["decision_id"] == []
    assert record["inputs"] is None


def test_full_record_validates(audit_path: Path) -> None:
    inputs = {
        "run_kind": "review",
        "kind": "implement",
        "run": 3,
        "forked_from": "cs-zzzz2222",
        "labels": ["a=b", "kind=review"],
        "tty": False,
        "overridden_hooks": ["check-a"],
        "sandbox_from": "required",
        "sandbox_to": "off",
        "grace": 10.0,
    }
    now = datetime(2026, 10, 30, 1, 2, 3, tzinfo=UTC)
    write(
        audit_path,
        action="launch_escalation_override",
        decision_ids=["d1", "d2"],
        inputs=inputs,
        now=now,
    )
    [record] = records(audit_path)
    validate(record, AUDIT_SCHEMA)
    assert record == {
        "ts": "2026-10-30T01:02:03.000Z",
        "actor": "human:tester",
        "decision_id": ["d1", "d2"],
        "target": SID,
        "action": "launch_escalation_override",
        "inputs": inputs,
        "result": "ok",
    }


@pytest.mark.parametrize("action", sorted(audit.ACTIONS))
@pytest.mark.parametrize("result", sorted(audit.RESULTS))
def test_every_action_and_result_validates(action: str, result: str, audit_path: Path) -> None:
    write(audit_path, action=action, result=result)
    validate(records(audit_path)[0], AUDIT_SCHEMA)


def test_enums_match_the_schema() -> None:
    schema = registry().contents(AUDIT_SCHEMA)
    assert set(audit.ACTIONS) == set(schema["properties"]["action"]["enum"])
    assert set(audit.RESULTS) == set(schema["properties"]["result"]["enum"])
    input_schema = schema["properties"]["inputs"]["oneOf"][0]
    assert set(audit.INPUT_KEYS) == set(input_schema["properties"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "launch"},
        {"result": "maybe"},
        {"inputs": {"unknown": 1}},
        {"target": "cs-ABCD2345"},
        {"target": "abcd2345"},
        {"actor": 3},
        {"decision_ids": "d1"},
        {"decision_ids": [1]},
        {"inputs": ["run"]},
        {"inputs": {"sandbox_from": "maybe"}},
        {"inputs": {"sandbox_to": "on"}},
        {"inputs": {"labels": ["Bad Label"]}},
        {"inputs": {"labels": ["a=b c"]}},
        {"inputs": {"labels": {"a": "b"}}},
        {"inputs": {"labels": "a=b"}},
        {"inputs": {"labels": [1]}},
        {"inputs": {"run": "1"}},
        {"inputs": {"run": True}},
        {"inputs": {"kind": 3}},
        {"inputs": {"run_kind": ["review"]}},
        {"inputs": {"kind": "Bad Kind"}},
        {"inputs": {"kind": "-review"}},
        {"inputs": {"kind": "k" * 65}},
        {"inputs": {"run_kind": "review!"}},
        {"inputs": {"run_kind": ""}},
        {"inputs": {"forked_from": "parent"}},
        {"inputs": {"tty": 1}},
        {"inputs": {"overridden_hooks": "h1"}},
        {"inputs": {"overridden_hooks": [1]}},
        {"inputs": {"grace": "10"}},
        {"inputs": {"grace": True}},
        {"inputs": {"grace": float("nan")}},
        {"inputs": {"grace": float("inf")}},
        {"now": datetime(2026, 10, 30, 1, 2, 3)},
    ],
)
def test_invalid_records_are_rejected(kwargs: dict[str, Any], audit_path: Path) -> None:
    with pytest.raises(ValueError):
        write(audit_path, **kwargs)
    assert not audit_path.exists()


def test_bulk_forget_appends_one_record_per_session(audit_path: Path) -> None:
    for sid in ("cs-aaaa2222", "cs-bbbb3333"):
        write(audit_path, action="forget", target=sid)
    assert [r["target"] for r in records(audit_path)] == ["cs-aaaa2222", "cs-bbbb3333"]


def test_file_mode_is_0600(audit_path: Path) -> None:
    write(audit_path)
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600


def test_write_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        write(tmp_path / "missing" / "audit.jsonl")
    (tmp_path / "audit.jsonl").mkdir()
    with pytest.raises(OSError):
        write(tmp_path / "audit.jsonl")


def test_secrets_never_appear_in_records(audit_path: Path) -> None:
    write(
        audit_path,
        actor=f"auto:{SECRET}",
        decision_ids=[SECRET],
        inputs={
            "overridden_hooks": [f"h{SECRET}"],
            "labels": [f"token={SECRET}", "plain=1", f"{SECRET.lower()}=v"],
        },
        secrets=[SECRET, SECRET.lower()],
    )
    text = audit_path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert SECRET.lower() not in text
    [record] = records(audit_path)
    validate(record, AUDIT_SCHEMA)
    assert record["inputs"]["labels"] == ["token=redacted", "plain=1"]
    assert record["decision_id"] == ["[redacted]"]
    assert record["inputs"]["overridden_hooks"] == ["h[redacted]"]


@pytest.mark.parametrize("secret", ["stop", "ok", "human", "cs-abcd", "a", "2026"])
def test_short_secrets_are_ignored(secret: str, audit_path: Path) -> None:
    write(audit_path, secrets=[secret], inputs={"labels": [f"k={secret}"]})
    [record] = records(audit_path)
    validate(record, AUDIT_SCHEMA)
    assert record["action"] == "stop"
    assert record["result"] == "ok"
    assert record["actor"] == "human:tester"
    assert record["inputs"]["labels"] == [f"k={secret}"]


@pytest.mark.parametrize(
    "secret", ["launch_escalation_override", SID, "cs-zzzz2222", "required", "2026-10-30"]
)
def test_constrained_fields_are_never_redacted(secret: str, audit_path: Path) -> None:
    now = datetime(2026, 10, 30, 1, 2, 3, tzinfo=UTC)
    inputs = {"forked_from": "cs-zzzz2222", "sandbox_from": "required", "sandbox_to": "off"}
    write(
        audit_path,
        action="launch_escalation_override",
        inputs=inputs,
        actor=f"auto:{secret}",
        secrets=[secret],
        now=now,
    )
    [record] = records(audit_path)
    validate(record, AUDIT_SCHEMA)
    assert record["action"] == "launch_escalation_override"
    assert record["target"] == SID
    assert record["ts"] == "2026-10-30T01:02:03.000Z"
    assert record["inputs"] == inputs
    assert record["actor"] == "auto:[redacted]"


def test_label_whose_redacted_form_would_leak_is_dropped(audit_path: Path) -> None:
    write(audit_path, inputs={"labels": ["key=abcdefgh"]}, secrets=["abcdefgh", "y=redacted"])
    [record] = records(audit_path)
    validate(record, AUDIT_SCHEMA)
    assert record["inputs"]["labels"] == []


def test_secret_formed_across_a_marker_is_not_written(audit_path: Path) -> None:
    write(audit_path, actor="auto:AAAAAAAABBBBBB", secrets=["AAAAAAAA", "ted]BBBBBB"])
    assert "ted]BBBBBB" not in audit_path.read_text(encoding="utf-8")
    assert records(audit_path)[0]["actor"] == "[redacted]"


def test_returned_record_is_the_redacted_one(audit_path: Path) -> None:
    returned = write(audit_path, actor=f"auto:{SECRET}", secrets=[SECRET])
    assert SECRET not in json.dumps(returned)


@pytest.mark.parametrize("secret", ["gate-check", "draft-ticket"])
def test_kind_names_are_validated_not_redacted(secret: str, audit_path: Path) -> None:
    inputs = {"kind": secret, "run_kind": secret}
    write(audit_path, inputs=inputs, actor=f"auto:{secret}", secrets=[secret])
    [record] = records(audit_path)
    validate(record, AUDIT_SCHEMA)
    assert record["inputs"] == inputs
    assert record["actor"] == "auto:[redacted]"


LEAK = "s3cr3tVALUE"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": LEAK},
        {"result": LEAK},
        {"target": f"cs-{LEAK}"},
        {"inputs": {f"x{LEAK}": 1}},
        {"inputs": {"forked_from": LEAK}},
        {"inputs": {"labels": [f"Bad {LEAK}"]}},
        {"inputs": {"kind": f"Bad {LEAK}"}},
        {"inputs": {"sandbox_from": LEAK}},
    ],
)
def test_rejection_messages_never_echo_the_value(kwargs: dict[str, Any], audit_path: Path) -> None:
    with pytest.raises(ValueError) as info:
        write(audit_path, **kwargs)
    assert LEAK not in str(info.value)
