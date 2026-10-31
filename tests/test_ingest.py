"""Outbox ingestion in the injected hooks (design.md, Outbox and ingestion; interface.md,
report and State transitions): claim by rename, secret check, artifact copy-then-scan and
freeze, rejection feedback through PostToolUse, drain caps, latest valid report wins."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from claude_session import hookevent, outbox, secretindex
from claude_session.store import Store

from .outbox_env import ARTIFACT_MAX, SUMMARY_MAX, Run, listing, rejection
from .schemas import validate
from .test_hookevent import SID


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    yield Run(root).setup()


def results(run: Run) -> list[str]:
    return [r["result"] for r in run.requests()]


def log_events(run: Run) -> list[tuple[str, str | None]]:
    for record in run.log():
        validate(record, "claude-session/log.json")
    return [(r["event"], r.get("reason")) for r in run.log()]


def malformed_counts(run: Run) -> list[int]:
    """The ``count`` of each aggregate ``report_rejected`` ``malformed`` record."""
    log_events(run)  # validates every record
    return [
        r["count"]
        for r in run.log()
        if r["event"] == "report_rejected" and r.get("reason") == "malformed"
    ]


def assert_rejected(run: Run, code: str, output: str) -> None:
    assert rejection(output) == code
    assert run.run()["report_status"] is None
    assert results(run) == [code]
    assert listing(run.outbox) == [] and listing(run.claimed) == []
    assert ("report_rejected", code) in log_events(run)


# Ingestion and the state


def test_a_done_report_is_ingested_by_post_tool_use(run: Run) -> None:
    queued = run.report(summary="All tests pass.")
    assert run.post_tool_use() == ""
    r = run.run()
    assert (r["report_status"], r["report_summary"], r["report_reason"]) == (
        "done",
        "All tests pass.",
        None,
    )
    assert r["report_implicit"] == 0 and r["reported_at"] is not None
    assert r["artifact_name"] is None and r["artifact_path"] is None
    assert run.state() == ("reported", None, "working")
    assert [(q["request_id"], q["result"], q["run_no"]) for q in run.requests()] == [
        (queued.request_id, "ingested", 1)
    ]
    assert listing(run.outbox) == [] and listing(run.claimed) == []
    assert log_events(run) == [("report_ingested", None)]
    assert run.session()["updated_at"] != "2026-10-30T00:00:00.000000Z"


def test_the_report_row_follows_the_hooks_own_row(tmp_path: Path) -> None:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(state="idle", activity="idle")
    run.report()
    run.post_tool_use()
    events = [(e["cause"], e["from_state"], e["to_state"], e["to_activity"]) for e in run.events()]
    assert events == [
        ("PostToolUse", "idle", "working", "working"),
        ("report", "working", "reported", "working"),
    ]


def test_a_report_only_change_still_writes_a_report_event(run: Run) -> None:
    run.report(summary="first")
    run.post_tool_use()
    run.report(summary="second")
    run.post_tool_use()
    assert run.state() == ("reported", None, "working")
    assert len(run.report_events()) == 2
    assert run.run()["report_summary"] == "second"


def test_a_needs_input_report_escalates_until_the_next_prompt(run: Run) -> None:
    reason = run.write_artifact("reason.md", b"Which database?")
    run.report("needs-input", reason_file=reason)
    run.post_tool_use()
    assert run.state() == ("needs-input", "escalation", "working")
    run.stop_hook()
    assert run.state() == ("needs-input", "escalation", "idle")
    run.hook("UserPromptSubmit", {})
    assert run.state() == ("reported", None, "working")
    run.post_tool_use()
    assert run.state() == ("reported", None, "working")
    # A new escalation after the prompt opens again (it relies on the report event).
    run.report("needs-input", reason_file=reason)
    run.post_tool_use()
    assert run.state() == ("needs-input", "escalation", "working")
    run.post_tool_use()
    assert run.state() == ("needs-input", "escalation", "working")


def test_a_final_report_closes_an_escalation(run: Run) -> None:
    reason = run.write_artifact("reason.md", b"?")
    run.report("needs-input", reason_file=reason)
    run.post_tool_use()
    run.report("done")
    run.post_tool_use()
    assert run.state() == ("reported", None, "working")


def test_a_waiting_prompt_takes_precedence_over_the_report(run: Run) -> None:
    """Drained by Stop while a permission prompt waits: needs-input(permission) stays, and
    the report event is still written."""
    run.hook("PermissionRequest", {"tool_name": "Bash"})
    run.report()
    run.stop_hook()
    assert run.state() == ("needs-input", "permission", "idle")
    assert run.run()["report_status"] == "done"
    assert len(run.report_events()) == 1


def test_stop_drains_and_the_backstop_sees_the_report(run: Run) -> None:
    run.report()
    assert run.stop_hook() == ""
    assert run.run()["report_status"] == "done"
    assert run.run()["backstop_used"] == 0
    assert run.state() == ("reported", None, "idle")


def test_a_rejection_found_by_stop_is_only_logged_and_the_backstop_still_blocks(run: Run) -> None:
    run.plant(run.request(secret="0" * 64))
    output = json.loads(run.stop_hook())
    assert output == hookevent.backstop_output()
    assert results(run) == ["bad_secret"]
    assert run.run()["report_status"] is None


def test_a_rejection_found_by_an_interactive_stop_prints_nothing(tmp_path: Path) -> None:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(mode="interactive")
    run.plant(run.request(secret="0" * 64))
    assert run.stop_hook() == ""
    assert results(run) == ["bad_secret"]


@pytest.mark.parametrize(
    "event",
    ["UserPromptSubmit", "PermissionRequest", "Notification", "SessionEnd"],
)
def test_other_hooks_never_drain(run: Run, event: str) -> None:
    payload = {"tool_name": "Bash", "notification_type": "idle_prompt", "reason": "other"}
    run.report()
    run.hook(event, payload)
    assert len(listing(run.outbox)) == 1
    assert run.requests() == []


def test_a_post_tool_use_without_requests_prints_nothing_and_writes_no_log(run: Run) -> None:
    assert run.post_tool_use() == ""
    assert run.requests() == [] and run.log() == []


# Artifacts


def test_the_artifact_is_frozen_with_its_hash(run: Run) -> None:
    data = b"# Result\nok\n"
    run.write_artifact("result.md", data)
    run.report(artifact="result.md")
    run.post_tool_use()
    r = run.run()
    frozen = run.private_run / "artifact" / "1" / "result.md"
    assert r["artifact_name"] == "result.md"
    assert r["artifact_path"] == str(frozen)
    assert r["artifact_sha256"] == hashlib.sha256(data).hexdigest()
    assert r["artifact_size"] == len(data)
    assert r["artifact_ingest_seq"] == 1
    assert frozen.read_bytes() == data
    assert oct(frozen.stat().st_mode & 0o777) == oct(0o600)
    assert listing(run.private_run / "scan") == []
    # The agent changing its working copy does not change the frozen one.
    run.write_artifact("result.md", b"changed")
    assert frozen.read_bytes() == data


def test_later_reports_never_overwrite_earlier_frozen_files(run: Run) -> None:
    run.write_artifact("result.md", b"one")
    run.report(artifact="result.md")
    run.post_tool_use()
    run.report()  # no artifact: the run's artifact becomes null
    run.post_tool_use()
    assert run.run()["artifact_name"] is None and run.run()["artifact_path"] is None
    run.write_artifact("result.md", b"three")
    run.report(artifact="result.md")
    run.post_tool_use()
    r = run.run()
    assert r["artifact_ingest_seq"] == 3
    assert (run.private_run / "artifact" / "1" / "result.md").read_bytes() == b"one"
    assert (run.private_run / "artifact" / "3" / "result.md").read_bytes() == b"three"


def test_an_artifact_changed_after_report_is_rejected(run: Run) -> None:
    run.write_artifact("result.md", b"reviewed content")
    run.report(artifact="result.md")
    run.write_artifact("result.md", b"swapped content")
    assert_rejected(run, "artifact_changed", run.post_tool_use())
    assert listing(run.private_run / "scan") == []
    assert not (run.private_run / "artifact").exists()


def test_an_artifact_removed_after_report_is_rejected(run: Run) -> None:
    path = run.write_artifact("result.md", b"x")
    run.report(artifact="result.md")
    os.unlink(path)
    assert_rejected(run, "artifact_missing", run.post_tool_use())


def test_an_artifact_swapped_for_a_symlink_is_rejected(run: Run, tmp_path: Path) -> None:
    path = run.write_artifact("result.md", b"x")
    run.report(artifact="result.md")
    os.unlink(path)
    target = tmp_path / "private-key"
    target.write_bytes(b"x")  # even the same bytes
    os.symlink(target, path)
    assert_rejected(run, "not_regular_file", run.post_tool_use())


def test_an_artifact_swapped_for_a_hard_link_is_rejected(run: Run, tmp_path: Path) -> None:
    path = run.write_artifact("result.md", b"x")
    run.report(artifact="result.md")
    os.link(path, tmp_path / "second-link")
    assert_rejected(run, "not_regular_file", run.post_tool_use())


def test_an_artifact_swapped_for_a_fifo_is_rejected_without_waiting(run: Run) -> None:
    path = run.write_artifact("result.md", b"x")
    run.report(artifact="result.md")
    os.unlink(path)
    os.mkfifo(path)
    assert_rejected(run, "not_regular_file", run.post_tool_use())


def test_an_artifact_grown_past_the_cap_is_rejected(run: Run) -> None:
    path = run.write_artifact("result.md", b"x")
    run.report(artifact="result.md")
    Path(path).write_bytes(b"x" * (ARTIFACT_MAX + 1))
    assert_rejected(run, "too_large", run.post_tool_use())


def test_a_replaced_artifacts_directory_is_rejected(run: Run) -> None:
    run.write_artifact("result.md", b"x")
    run.report(artifact="result.md")
    run.artifacts.rename(Path(run.csession_dir) / "artifacts.old")
    run.artifacts.mkdir()
    (run.artifacts / "result.md").write_bytes(b"x")
    assert_rejected(run, "outside_dir", run.post_tool_use())


def test_a_report_without_an_artifact_ignores_a_replaced_artifacts_directory(run: Run) -> None:
    run.report()
    run.artifacts.rename(Path(run.csession_dir) / "artifacts.old")
    run.artifacts.mkdir()
    run.post_tool_use()
    assert results(run) == ["ingested"]


# Rejections of the request itself


def test_a_forged_secret_is_rejected(run: Run) -> None:
    run.plant(run.request(secret="f" * 64))
    assert_rejected(run, "bad_secret", run.post_tool_use())


def test_a_request_for_another_run_is_stale(run: Run) -> None:
    run.plant(run.request(run=2))
    assert_rejected(run, "stale_run", run.post_tool_use())


@pytest.mark.parametrize(
    "change",
    [
        {"v": 2},
        {"v": True},
        {"v": 1.0},
        {"run": 0},
        {"run": True},
        {"secret": "F" * 64},
        {"secret": 5},
        {"status": "finished"},
        {"status": "done", "summary": None},
        {"status": "done", "summary": "   "},
        {"status": "blocked", "reason": None},
        {"summary": 7},
        {"summary": "nul\u0000"},
        {"summary": "\ud800 lone surrogate"},
        {"artifact": "../x", "artifact_sha256": "0" * 64},
        {"artifact": "a", "artifact_sha256": None},
        {"artifact": None, "artifact_sha256": "0" * 64},
        {"artifact": "a", "artifact_sha256": "XYZ"},
        {"created": "yesterday"},
        {"created": None},
        {"extra": 1},
    ],
)
def test_a_malformed_request_is_rejected(run: Run, change: dict[str, Any]) -> None:
    run.plant(run.request(**change))
    assert_rejected(run, "malformed", run.post_tool_use())


def test_a_request_with_a_missing_field_is_rejected(run: Run) -> None:
    doc = run.request()
    del doc["created"]
    run.plant(doc)
    assert_rejected(run, "malformed", run.post_tool_use())


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b"\xff\xfe",
        b'{"v": NaN}',
        b"[" * 10_000,  # deep nesting: RecursionError
    ],
)
def test_garbage_is_rejected_malformed(run: Run, raw: bytes) -> None:
    run.plant(raw=raw)
    assert_rejected(run, "malformed", run.post_tool_use())


def test_duplicate_keys_are_rejected(run: Run) -> None:
    good = json.dumps(run.request())
    raw = good[:-1] + ', "status": "failed"}'
    run.plant(raw=raw.encode())
    assert_rejected(run, "malformed", run.post_tool_use())


def test_an_oversized_request_is_rejected_unread(run: Run) -> None:
    run.plant(raw=b" " * (4 * SUMMARY_MAX + outbox.REQUEST_OVERHEAD + 1))
    assert_rejected(run, "too_large", run.post_tool_use())


def test_an_oversized_summary_is_rejected(run: Run) -> None:
    run.plant(run.request(summary="x" * (SUMMARY_MAX + 1)))
    assert_rejected(run, "too_large", run.post_tool_use())


def test_a_symlinked_request_is_rejected_and_never_followed(run: Run, tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps(run.request()))
    rid = outbox.new_request_id()
    os.symlink(target, run.outbox / f"{rid}.json")
    assert_rejected(run, "not_regular_file", run.post_tool_use())
    assert target.exists()  # only the link was removed


def test_a_hard_linked_request_is_rejected(run: Run, tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps(run.request()))
    os.link(target, run.outbox / f"{outbox.new_request_id()}.json")
    assert_rejected(run, "not_regular_file", run.post_tool_use())
    assert target.exists()


def test_a_fifo_request_is_rejected_without_waiting(run: Run) -> None:
    os.mkfifo(run.outbox / f"{outbox.new_request_id()}.json")
    assert_rejected(run, "not_regular_file", run.post_tool_use())


def test_a_directory_with_a_request_name_is_removed_never_claimed(run: Run) -> None:
    rid = outbox.new_request_id()
    (run.outbox / f"{rid}.json").mkdir()
    (run.outbox / f"{rid}.json" / "inner").write_text("x")
    assert run.post_tool_use() == ""
    assert listing(run.claimed) == []
    assert run.requests() == []
    assert ("report_rejected", "malformed") in log_events(run)


@pytest.mark.parametrize("name", ["junk.txt", ".hidden", "0190b6b2.json", "x" * 200])
def test_other_names_are_removed_as_malformed(run: Run, name: str) -> None:
    (run.outbox / name).write_text("x")
    assert run.post_tool_use() == ""  # no request to give feedback on
    assert listing(run.outbox) == []
    assert log_events(run) == [("report_rejected", "malformed")]


def test_junk_directories_are_removed_empty_or_not(run: Run) -> None:
    (run.outbox / "empty").mkdir()
    (run.outbox / "full" / "deeper").mkdir(parents=True)
    (run.outbox / "full" / "deeper" / "x").write_text("x")
    run.post_tool_use()
    assert listing(run.outbox) == []
    assert malformed_counts(run) == [2]


def test_junk_never_starves_a_request(run: Run) -> None:
    """Junk does not count against the claim cap: one drain claims the request behind 200
    non-empty junk directories, removes them all and logs one aggregate record."""
    for n in range(200):
        (run.outbox / f"junk-{n:03d}").mkdir()
        (run.outbox / f"junk-{n:03d}" / "x").write_text("x")
    run.report(summary="behind the junk")
    run.post_tool_use()
    assert results(run) == ["ingested"]
    assert listing(run.outbox) == []
    assert malformed_counts(run) == [200]
    run.post_tool_use()
    assert malformed_counts(run) == [200]  # nothing left to log


def test_junk_removal_stops_at_the_drain_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junk tree whose removal crosses the deadline is left partly removed for the next
    drain; the drain returns and the hook's own event still applies."""
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(state="idle", activity="idle")
    junk = run.outbox / "junk"
    (junk / "deeper").mkdir(parents=True)
    for n in range(100):
        (junk / f"f{n:03d}").write_text("x")
        (junk / "deeper" / f"f{n:03d}").write_text("x")
    ticks = iter(range(10_000))  # every clock reading is one second later
    real = outbox.Drain.prepare

    def with_clock(cls: type[outbox.Drain], **kwargs: Any) -> outbox.Drain:
        return real(**kwargs, clock=lambda: float(next(ticks)))

    monkeypatch.setattr(outbox.Drain, "prepare", classmethod(with_clock))
    assert run.post_tool_use() == ""
    assert run.state() == ("working", None, "working")
    left = sum(len(files) for _, _, files in os.walk(junk))
    assert 0 < left < 200
    assert malformed_counts(run) == []  # counted once it is removed
    monkeypatch.undo()
    run.post_tool_use()
    assert listing(run.outbox) == []
    assert malformed_counts(run) == [1]


def test_a_fresh_report_temp_file_is_left_alone_and_an_old_one_removed(run: Run) -> None:
    """report's temporary file lives in outbox/ under a reserved name: never claimed, never
    logged, removed only once it is older than a canceled report could leave it."""
    fresh, old = outbox.temp_name(), outbox.temp_name()
    (run.outbox / fresh).write_bytes(b"partial")
    (run.outbox / old).write_bytes(b"partial")
    os.utime(run.outbox / old, (0, 0))
    run.report()
    run.post_tool_use()
    assert listing(run.outbox) == [fresh]
    assert results(run) == ["ingested"]
    assert [event for event, _ in log_events(run)] == ["report_ingested"]


def test_temp_names_do_not_count_against_the_claim_cap(run: Run) -> None:
    for _ in range(outbox.DRAIN_MAX_ENTRIES + 10):
        (run.outbox / outbox.temp_name()).write_bytes(b"partial")
    run.report()
    run.post_tool_use()
    assert results(run) == ["ingested"]


def test_the_log_never_contains_agent_text(run: Run) -> None:
    marker = "AGENT-TEXT-MARKER"
    (run.outbox / f"{marker}.json").write_text(marker)
    run.plant(run.request(summary=marker, secret="0" * 64))
    run.report(summary=marker)
    run.post_tool_use()
    assert marker not in (run.root / "log.jsonl").read_text()


# Secrets


# Configured values (made up; lengths of real credentials).
AUTH_VALUE = "test-auth-" + "Qw3rTy9" * 6  # a claude_auth_env value
SECRET_ENV_VALUE = "tok_" + "x7Yk2" * 7 + "Pq"  # a secret_env value


@pytest.fixture
def secret_run(tmp_path: Path) -> Iterator[Run]:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    yield Run(root).setup(secrets=(AUTH_VALUE, SECRET_ENV_VALUE))


def text_request(run: Run, field: str, text: str) -> dict[str, Any]:
    if field == "summary":
        return run.request(summary=text)
    return run.request(status="blocked", summary=None, reason=text)


def artifact_request(run: Run, data: bytes) -> dict[str, Any]:
    run.write_artifact("a.md", data)
    return run.request(artifact="a.md", artifact_sha256=hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("value", [AUTH_VALUE, SECRET_ENV_VALUE])
@pytest.mark.parametrize("field", ["summary", "reason"])
def test_a_configured_value_in_the_text_is_rejected(
    secret_run: Run, field: str, value: str
) -> None:
    secret_run.plant(text_request(secret_run, field, f"here it is: {value}\nbye"))
    assert_rejected(secret_run, "contains_secret", secret_run.post_tool_use())
    assert value not in (secret_run.root / "log.jsonl").read_text()


@pytest.mark.parametrize("value", [AUTH_VALUE, SECRET_ENV_VALUE])
@pytest.mark.parametrize("offset", [0, 9, 15, 16, 31])
def test_a_configured_value_split_across_artifact_chunks_is_rejected(
    secret_run: Run, value: str, offset: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy is scanned chunk by chunk as it is written; a value spanning chunks is
    found."""
    monkeypatch.setattr(outbox, "_READ_CHUNK", 16)
    data = b"y" * offset + value.encode() + b"z" * 40
    secret_run.plant(artifact_request(secret_run, data))
    assert_rejected(secret_run, "contains_secret", secret_run.post_tool_use())
    assert listing(secret_run.private_run / "scan") == []
    assert listing(secret_run.private_run / "artifact") == []


@pytest.mark.parametrize("value", [AUTH_VALUE, SECRET_ENV_VALUE])
def test_a_configured_value_as_the_artifact_name_is_rejected(secret_run: Run, value: str) -> None:
    """The artifact name reaches status JSON: it is scanned like the texts."""
    name = f"out-{value}.md"
    data = b"harmless"
    secret_run.write_artifact(name, data)
    secret_run.plant(
        secret_run.request(artifact=name, artifact_sha256=hashlib.sha256(data).hexdigest())
    )
    assert_rejected(secret_run, "contains_secret", secret_run.post_tool_use())
    assert listing(secret_run.private_run / "scan") == []
    assert value not in (secret_run.root / "log.jsonl").read_text()


def test_a_configured_value_found_by_stop_is_logged(secret_run: Run) -> None:
    secret_run.plant(text_request(secret_run, "summary", AUTH_VALUE))
    assert secret_run.stop_hook() != ""  # no report ingested: the backstop blocks
    assert results(secret_run) == ["contains_secret"]
    assert ("report_rejected", "contains_secret") in log_events(secret_run)


def test_clean_reports_pass_the_scan(secret_run: Run) -> None:
    data = b"report body\n" * 200 + AUTH_VALUE.encode()[:-1]  # all but the last character
    secret_run.write_artifact("a.md", data)
    secret_run.report(summary=f"ok {SECRET_ENV_VALUE[1:]}", artifact="a.md")
    assert secret_run.post_tool_use() == ""
    assert results(secret_run) == ["ingested"]


def test_only_literal_values_are_refused(secret_run: Run) -> None:
    """interface.md, Residual risks: a transformed configured value is not caught."""
    secret_run.report(summary=f"leak {SECRET_ENV_VALUE.upper()}")
    assert secret_run.post_tool_use() == ""
    assert results(secret_run) == ["ingested"]


class Recording:
    """A scanner that records what each scan was fed."""

    def __init__(self) -> None:
        self.scans: list[list[bytes]] = []

    def scan(self, check: Callable[[], None] | None = None) -> Recording.Feed:
        feed = Recording.Feed()
        self.scans.append(feed.chunks)
        return feed

    class Feed:
        def __init__(self) -> None:
            self.chunks: list[bytes] = []

        def update(self, data: bytes) -> bool:
            self.chunks.append(data)
            return False


def test_the_scanner_is_fed_the_copy_in_chunks(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    """The artifact is never read back whole: the scan sees exactly the copied bytes, chunk
    by chunk, and the summary in its own scan."""
    monkeypatch.setattr(outbox, "_READ_CHUNK", 64)
    data = os.urandom(1000)
    run.write_artifact("a.md", data)
    run.report(summary="see the artifact", artifact="a.md")
    recording = Recording()
    drain = prepared(run, scanner=recording)
    try:
        assert drain.winner is not None
    finally:
        drain.finish()
    summary_scan, name_scan, artifact_scan = recording.scans
    assert summary_scan == [b"see the artifact"]
    assert name_scan == [b"a.md"]
    assert b"".join(artifact_scan) == data
    assert max(len(chunk) for chunk in artifact_scan) <= 64


@pytest.mark.parametrize("damage", ["missing", "corrupt", "readable-by-others"])
def test_a_drain_without_a_usable_secret_index_ingests_nothing(run: Run, damage: str) -> None:
    """Fail closed: the drain fails (logged with the exception type only), the request stays
    claimed, and the next drain with a usable index ingests it."""
    index = run.private_run / "secret-index"
    if damage == "missing":
        index.unlink()
    elif damage == "corrupt":
        index.write_bytes(b"not an index")
    else:
        index.chmod(0o644)
    queued = run.report(summary="done")
    assert run.post_tool_use() == ""
    assert run.requests() == []
    assert listing(run.claimed) == [f"{queued.request_id}.json"]
    failures = [r for r in run.log() if r["event"] == "hook_event_failed"]
    assert len(failures) == 1
    assert failures[0]["message"].startswith("PostToolUse: outbox drain: secret_index_unusable (")
    run.write_index(1, (run.secret,))
    run.post_tool_use()
    assert results(run) == ["ingested"]


def test_a_stop_does_not_spend_the_backstop_while_a_report_waits_for_the_index(
    run: Run,
) -> None:
    """A filed report the drain could not check (no usable secret-index) is pending: the
    Stop does not tell the agent no report was filed, and the backstop stays unused."""
    (run.private_run / "secret-index").unlink()
    run.report(summary="done")
    assert run.stop_hook() == ""
    assert run.run()["backstop_used"] == 0
    assert len(listing(run.claimed)) == 1
    (failure,) = [r for r in run.log() if r["event"] == "hook_event_failed"]
    assert failure["message"] == "Stop: outbox drain: secret_index_unusable (FileNotFoundError)"


def test_a_stop_without_a_pending_report_still_blocks_without_an_index(run: Run) -> None:
    """Nothing filed: the index is never read, and the backstop applies as usual."""
    (run.private_run / "secret-index").unlink()
    assert json.loads(run.stop_hook()) == hookevent.backstop_output()
    assert run.run()["backstop_used"] == 1


def test_secret_comparison_uses_the_stored_digest_only(run: Run) -> None:
    """The plaintext secret is never stored; a request with the digest as its secret fails."""
    with Store.open(run.db) as store:
        hook_run = store.hook_run(SID)
    assert hook_run is not None and hook_run.secret_sha256 is not None
    assert hook_run.secret_sha256 != run.secret
    run.plant(run.request(secret=hook_run.secret_sha256))
    assert_rejected(run, "bad_secret", run.post_tool_use())


# Drain caps and ordering


def test_the_newest_valid_request_wins(run: Run) -> None:
    first = run.report(summary="first")
    second = run.report(summary="second")
    third = run.report(summary="third")
    assert run.post_tool_use() == ""
    assert run.run()["report_summary"] == "third"
    assert {r["request_id"]: r["result"] for r in run.requests()} == {
        first.request_id: "discarded",
        second.request_id: "discarded",
        third.request_id: "ingested",
    }
    assert len(run.report_events()) == 1
    assert log_events(run).count(("outbox_discarded", None)) == 2


def test_a_newer_rejected_request_does_not_beat_an_older_valid_one(run: Run) -> None:
    run.report(summary="valid")
    run.plant(run.request(secret="0" * 64))
    output = run.post_tool_use()
    assert run.run()["report_summary"] == "valid"
    assert rejection(output) == "bad_secret"  # the agent's latest attempt failed


def test_an_older_rejection_after_a_newer_valid_request_is_not_fed_back(run: Run) -> None:
    run.plant(run.request(secret="0" * 64))
    run.report(summary="valid")
    assert run.post_tool_use() == ""
    assert sorted(results(run)) == ["discarded", "ingested"]  # never even checked


def test_at_most_sixteen_requests_are_processed(run: Run) -> None:
    ids = [run.plant(run.request(summary=f"report {n}"), rid=None) for n in range(20)]
    run.post_tool_use()
    by_id = {r["request_id"]: r["result"] for r in run.requests()}
    assert by_id[ids[-1]] == "ingested"
    assert [by_id[i] for i in ids[:-1]] == ["discarded"] * 19
    assert run.run()["report_summary"] == "report 19"
    messages = [r.get("message", "") for r in run.log() if r["event"] == "outbox_discarded"]
    assert sum("over the drain cap" in m for m in messages) == 4


def test_a_drain_claims_at_most_64_requests(run: Run) -> None:
    for _ in range(outbox.DRAIN_MAX_ENTRIES + 6):
        run.plant(run.request())
    run.post_tool_use()
    assert len(run.requests()) == outbox.DRAIN_MAX_ENTRIES
    assert len(listing(run.outbox)) == 6
    run.post_tool_use()
    assert listing(run.outbox) == []


def test_a_drain_reads_a_bounded_number_of_outbox_entries(run: Run) -> None:
    for n in range(outbox.DRAIN_MAX_READ + 5):
        (run.outbox / f"junk-{n:05d}").write_text("x")
    run.post_tool_use()
    assert len(listing(run.outbox)) == 5
    assert malformed_counts(run) == [outbox.DRAIN_MAX_READ]


def test_requests_claimed_by_a_canceled_drain_are_processed_by_the_next(run: Run) -> None:
    queued = run.report(summary="claimed earlier")
    run.claimed.mkdir(mode=0o700, exist_ok=True)
    os.rename(run.outbox / f"{queued.request_id}.json", run.claimed / f"{queued.request_id}.json")
    run.post_tool_use()
    assert results(run) == ["ingested"]
    assert listing(run.claimed) == []


def test_a_failed_recording_leaves_the_claimed_request_for_the_next_drain(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    run.write_artifact("a.md", b"a")
    run.report(artifact="a.md")

    def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Store, "set_report", broken)
    assert run.post_tool_use() == ""
    assert run.requests() == [] and run.run()["report_status"] is None
    assert [r["message"] for r in run.log()] == ["PostToolUse: outbox drain: OperationalError"]
    assert len(listing(run.claimed)) == 1
    assert listing(run.private_run / "scan") == []
    monkeypatch.undo()
    run.post_tool_use()
    assert results(run) == ["ingested"]
    assert run.run()["artifact_ingest_seq"] == 1
    assert (run.private_run / "artifact" / "1" / "a.md").read_bytes() == b"a"


def test_a_request_is_ingested_at_most_once(run: Run) -> None:
    """A replayed request id (same name planted again) is a duplicate, never re-ingested."""
    queued = run.report(summary="once")
    content = (run.outbox / f"{queued.request_id}.json").read_bytes()
    run.post_tool_use()
    (run.outbox / f"{queued.request_id}.json").write_bytes(content)
    before = run.run()["reported_at"]
    assert run.post_tool_use() == ""
    assert results(run) == ["ingested"]
    assert run.run()["reported_at"] == before
    assert len(run.report_events()) == 1
    assert listing(run.claimed) == []
    assert ("outbox_discarded", None) in log_events(run)


def prepared(
    run: Run,
    *,
    scanner: outbox.Scanner | None = None,
    clock: Callable[[], float] | None = None,
) -> outbox.Drain:
    """A drain prepared as hook-event does; ``scanner`` replaces the run's secret index."""
    with Store.open(run.db) as store:
        hook_run = store.hook_run(SID)
    assert hook_run is not None
    fd = hookevent.open_private_run_dir(run.private_dir, 1)
    try:
        return outbox.Drain.prepare(
            csession_id=SID,
            run_no=1,
            csession_dir=run.csession_dir,
            private_run_fd=fd,
            private_run_dir=str(run.private_run),
            outbox_dev_ino=hook_run.outbox_dev_ino or "",
            artifacts_dev_ino=hook_run.artifacts_dev_ino or "",
            secret_sha256=hook_run.secret_sha256 or "",
            limits=lambda: outbox.Limits(SUMMARY_MAX, ARTIFACT_MAX),
            scanner=(lambda: secretindex.read_at(fd)) if scanner is None else lambda: scanner,
            **({} if clock is None else {"clock": clock}),
        )
    finally:
        os.close(fd)


def test_two_concurrent_drains_ingest_a_request_once(run: Run) -> None:
    """Both drains see the same claimed request; the requests primary key lets only one
    ingest it."""
    run.write_artifact("a.md", b"a")
    run.report(artifact="a.md")
    first = prepared(run)  # claims it
    second = prepared(run)  # picks it up from claimed/
    assert first.winner is not None and second.winner is not None
    with Store.open(run.db) as store:
        for drain in (first, second):
            with store.transaction():
                drain.record(store, run_no=1, live=True)
            drain.finish(committed=True)
    assert results(run) == ["ingested"]
    assert second.items[0].duplicate
    assert run.run()["artifact_ingest_seq"] == 1
    assert listing(run.private_run / "scan") == []
    assert listing(run.private_run / "artifact" / "1") == ["a.md"]


def test_the_frozen_copy_is_what_was_checked(run: Run) -> None:
    """Changing the artifact after the copy (between pre-check and commit) changes nothing."""
    run.write_artifact("a.md", b"checked")
    run.report(artifact="a.md")
    drain = prepared(run)
    run.write_artifact("a.md", b"swapped after the copy")
    with Store.open(run.db) as store:
        with store.transaction():
            drain.record(store, run_no=1, live=True)
        drain.finish(committed=True)
    assert (run.private_run / "artifact" / "1" / "a.md").read_bytes() == b"checked"


def test_record_rejects_a_request_once_the_run_changed(run: Run) -> None:
    run.report()
    drain = prepared(run)
    with Store.open(run.db) as store:
        with store.transaction():
            assert drain.record(store, run_no=2, live=True) is None
        drain.finish(committed=True)
    assert [(r["result"], r["run_no"]) for r in run.requests()] == [("stale_run", 1)]


def test_record_rejects_when_the_run_is_not_live(run: Run) -> None:
    run.report()
    drain = prepared(run)
    with Store.open(run.db) as store:
        with store.transaction():
            assert drain.record(store, run_no=1, live=False) is None
        drain.finish(committed=True)
    assert results(run) == ["not_live"]
    assert drain.feedback() == "not_live"


def test_old_scan_leftovers_are_removed(run: Run) -> None:
    scan = run.private_run / "scan"
    scan.mkdir(mode=0o700)
    old, fresh = scan / "old.tmp", scan / "fresh.tmp"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (0, 0))
    run.report()
    run.post_tool_use()
    assert listing(scan) == ["fresh.tmp"]


# When the outbox is not drained


def test_a_replaced_outbox_is_never_drained(run: Run) -> None:
    run.outbox.rename(Path(run.csession_dir) / "outbox.old")
    run.outbox.mkdir()
    run.plant(run.request())
    assert run.post_tool_use() == ""
    assert len(listing(run.outbox)) == 1 and run.requests() == []
    assert log_events(run) == [("report_rejected", "outside_dir")]
    assert run.state() == ("working", None, "working")


def test_a_symlinked_outbox_is_never_drained(run: Run, tmp_path: Path) -> None:
    real = Path(run.csession_dir) / "outbox.real"
    run.outbox.rename(real)
    run.outbox.symlink_to(real)
    run.plant(run.request())
    run.post_tool_use()
    assert run.requests() == []
    assert log_events(run) == [("report_rejected", "outside_dir")]


def test_a_symlinked_session_directory_is_never_drained(run: Run, tmp_path: Path) -> None:
    real = tmp_path / "moved"
    Path(run.csession_dir).rename(real)
    Path(run.csession_dir).symlink_to(real)
    (real / "outbox" / f"{outbox.new_request_id()}.json").write_text(json.dumps(run.request()))
    run.post_tool_use()
    assert run.requests() == []
    assert log_events(run) == [("report_rejected", "outside_dir")]


def test_nothing_is_drained_before_exec_records_the_run_files(tmp_path: Path) -> None:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(record=False)
    run.report()
    run.post_tool_use()
    assert len(listing(run.outbox)) == 1 and run.requests() == []


@pytest.mark.parametrize(
    "setup",
    [
        {"wrapper": False},
        {"ended": True},
        {"forgotten": True},
        {"state": "exited", "activity": None},
    ],
)
def test_runs_that_are_not_live_are_not_drained(tmp_path: Path, setup: dict[str, Any]) -> None:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(**setup)
    run.report()
    assert run.post_tool_use() == ""
    assert len(listing(run.outbox)) == 1 and run.requests() == []


def test_a_drain_that_cannot_run_still_applies_the_hooks_own_event(tmp_path: Path) -> None:
    """A missing hookconf.json stops the drain, not the hook: the claimed request stays for
    the next drain and the failure is logged with the exception type only."""
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(state="idle", activity="idle")
    run.report()
    os.unlink(run.private_run / "hookconf.json")
    assert run.post_tool_use() == ""
    assert run.state() == ("working", None, "working")
    assert len(listing(run.claimed)) == 1
    assert run.requests() == []
    (record,) = run.log()
    assert record["event"] == "hook_event_failed"
    assert record["message"] == "PostToolUse: outbox drain: hookconf_unusable (FileNotFoundError)"
    run.write_conf(1, summary_max=SUMMARY_MAX, artifact_max=ARTIFACT_MAX)
    run.post_tool_use()
    assert results(run) == ["ingested"]


def test_a_previous_runs_leftover_request_is_stale(tmp_path: Path) -> None:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup()
    run.report(summary="for run 1")
    with sqlite3.connect(run.db) as conn:
        conn.execute("UPDATE runs SET ended_at = 'x' WHERE run_no = 1")
        conn.execute(
            "INSERT INTO runs (csession_id, run_no, created_at, run_kind, mode, "
            "session_started_at, wrapper_pid, wrapper_start) "
            "VALUES (?, 2, 'x', 'implement', 'headless', 'x', ?, 'x')",
            (SID, os.getpid()),
        )
        conn.execute("UPDATE sessions SET current_run = 2")
    run.prepare(2, summary_max=SUMMARY_MAX, artifact_max=ARTIFACT_MAX)
    run.run_no = 2
    output = run.post_tool_use()
    assert rejection(output) == "stale_run"
    assert [(r["result"], r["run_no"]) for r in run.requests()] == [("stale_run", 2)]


# Secrets refused by ingestion itself (independent of the secret index)


@pytest.mark.parametrize("field", ["summary", "reason", "artifact", "artifact-name"])
@pytest.mark.parametrize("case", ["lower", "upper"])
def test_ingestion_refuses_the_run_secret(
    run: Run, field: str, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outbox, "_READ_CHUNK", 16)  # the secret spans read chunks
    value = run.secret if case == "lower" else run.secret.upper()
    if field == "summary":
        doc = run.request(summary=f"leak {value}")
    elif field == "reason":
        doc = run.request(status="blocked", summary=None, reason=f"leak {value}")
    elif field == "artifact":
        data = b"x" * 10 + value.encode() + b"y"
        run.write_artifact("a.md", data)
        digest = hashlib.sha256(data).hexdigest()
        doc = run.request(artifact="a.md", artifact_sha256=digest)
    else:
        run.write_artifact(value, b"harmless")
        doc = run.request(artifact=value, artifact_sha256=hashlib.sha256(b"harmless").hexdigest())
    run.plant(doc)
    assert_rejected(run, "contains_secret", run.post_tool_use())
    assert run.secret not in (run.root / "log.jsonl").read_text().lower()
    assert listing(run.private_run / "scan") == []


# One winner, a time budget


def test_only_the_winners_artifact_is_copied(run: Run) -> None:
    """Requests older than the winner are discarded unread: their artifacts are never opened
    (a removed one is not artifact_missing) or copied."""
    run.write_artifact("old.md", b"old")
    older = run.report(artifact="old.md")
    (run.artifacts / "old.md").unlink()
    run.write_artifact("new.md", b"new")
    newer = run.report(artifact="new.md")
    drain = prepared(run)
    try:
        assert [i.request_id for i in drain.items if i.copy_name] == [newer.request_id]
        assert len(listing(run.private_run / "scan")) == 1
    finally:
        drain.finish()
    run.post_tool_use()
    assert {r["request_id"]: r["result"] for r in run.requests()} == {
        older.request_id: "discarded",
        newer.request_id: "ingested",
    }
    messages = [r.get("message", "") for r in run.log() if r["event"] == "outbox_discarded"]
    assert messages == [f"request {older.request_id}: superseded by a newer request"]


def test_a_drain_stops_checking_past_its_deadline(run: Run) -> None:
    """Past half the hook budget the drain stops; the unchecked requests stay claimed for the
    next drain."""
    ids = [run.report(summary=f"report {n}").request_id for n in range(3)]
    now = [0.0]

    class SlowScan:
        def update(self, data: bytes) -> bool:
            now[0] += outbox.DRAIN_BUDGET + 1  # the first check uses up the budget
            return True

    class SlowScanner:
        def scan(self, check: Callable[[], None] | None = None) -> SlowScan:
            return SlowScan()

    drain = prepared(run, scanner=SlowScanner(), clock=lambda: now[0])
    with Store.open(run.db) as store:
        with store.transaction():
            drain.record(store, run_no=1, live=True)
        drain.finish(committed=True)
    assert {r["request_id"]: r["result"] for r in run.requests()} == {ids[2]: "contains_secret"}
    assert listing(run.claimed) == sorted(f"{rid}.json" for rid in ids[:2])
    run.post_tool_use()
    assert {r["request_id"]: r["result"] for r in run.requests()} == {
        ids[0]: "discarded",
        ids[1]: "ingested",
        ids[2]: "contains_secret",
    }


def test_a_drain_stops_claiming_past_its_deadline(run: Run) -> None:
    run.report()
    ticks = iter([0.0] + [outbox.DRAIN_BUDGET + 1] * 10)
    drain = prepared(run, clock=lambda: next(ticks))
    try:
        assert drain.items == []
    finally:
        drain.finish()
    assert len(listing(run.outbox)) == 1


class ClockedScanner:
    """A scanner whose scans spend ``cost`` seconds of the fake clock per update; with
    ``inner`` the scan also calls the drain's check inside the update (as secretindex.Scan
    does at its work interval)."""

    def __init__(self, now: list[float], cost: float, *, inner: bool = False) -> None:
        self.now = now
        self.cost = cost
        self.inner = inner

    def scan(self, check: Callable[[], None] | None = None) -> ClockedScanner.Feed:
        return ClockedScanner.Feed(self, check)

    class Feed:
        def __init__(self, owner: ClockedScanner, check: Callable[[], None] | None) -> None:
            self.owner = owner
            self.check = check

        def update(self, data: bytes) -> bool:
            self.owner.now[0] += self.owner.cost
            if self.owner.inner and self.check is not None:
                self.check()
            return False


def _drain_with(run: Run, scanner: outbox.Scanner, now: list[float]) -> outbox.Drain:
    drain = prepared(run, scanner=scanner, clock=lambda: now[0])
    assert listing(run.private_run / "scan") == []  # removed at once, not only by finish
    with Store.open(run.db) as store:
        with store.transaction():
            drain.record(store, run_no=1, live=True)
        drain.finish(committed=True)
    return drain


@pytest.mark.parametrize("inner", [False, True], ids=["between-chunks", "inside-a-chunk"])
def test_a_first_request_whose_scan_outlasts_the_drain_is_too_large(
    run: Run, monkeypatch: pytest.MonkeyPatch, inner: bool
) -> None:
    """The first request checked had the whole budget: a scan still running at the deadline
    rejects it too_large (a definite outcome: it is not retried forever), and its partial
    copy is removed."""
    monkeypatch.setattr(outbox, "_READ_CHUNK", 64)
    run.write_artifact("a.md", os.urandom(1000))
    run.report(artifact="a.md")
    now = [0.0]
    cost = outbox.DRAIN_BUDGET + 1 if inner else outbox.DRAIN_BUDGET / 4
    _drain_with(run, ClockedScanner(now, cost, inner=inner), now)
    assert results(run) == ["too_large"]
    assert listing(run.claimed) == []
    assert listing(run.private_run / "scan") == []
    assert listing(run.private_run / "artifact") == []


def test_a_later_request_whose_scan_outlasts_the_drain_is_deferred(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request checked after another one did not have the whole budget: it stays claimed
    (its copy removed) and the next drain gives it the full budget."""
    monkeypatch.setattr(outbox, "_READ_CHUNK", 64)
    run.write_artifact("a.md", os.urandom(1000))
    older = run.report(artifact="a.md")
    newer = run.plant(run.request(secret="0" * 64))  # checked first: bad_secret
    now = [0.0]
    _drain_with(run, ClockedScanner(now, outbox.DRAIN_BUDGET / 4), now)
    assert {r["request_id"]: r["result"] for r in run.requests()} == {newer: "bad_secret"}
    assert listing(run.claimed) == [f"{older.request_id}.json"]
    assert listing(run.private_run / "scan") == []
    run.post_tool_use()
    assert {r["request_id"]: r["result"] for r in run.requests()} == {
        newer: "bad_secret",
        older.request_id: "ingested",
    }


def test_a_first_request_that_started_late_in_the_drain_is_deferred(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup before checking used up most of the budget: the first request checked did
    not have the whole budget, so a scan that outlasts the drain defers it (its copy
    removed) rather than rejecting it too_large; the next drain ingests it."""
    monkeypatch.setattr(outbox, "_READ_CHUNK", 64)
    run.write_artifact("a.md", os.urandom(1000))
    request = run.report(artifact="a.md")
    now = [0.0]
    real_remove_leftovers = outbox._remove_leftovers

    def slow_remove_leftovers(scan_fd: int, stop: Callable[[], bool]) -> None:
        now[0] += outbox.DRAIN_BUDGET * 0.8
        real_remove_leftovers(scan_fd, stop)

    monkeypatch.setattr(outbox, "_remove_leftovers", slow_remove_leftovers)
    drain = _drain_with(run, ClockedScanner(now, outbox.DRAIN_BUDGET / 4), now)
    assert [item.deferred for item in drain.items] == [True]
    assert results(run) == []
    assert listing(run.claimed) == [f"{request.request_id}.json"]
    assert listing(run.private_run / "scan") == []
    assert listing(run.private_run / "artifact") == []
    monkeypatch.setattr(outbox, "_remove_leftovers", real_remove_leftovers)
    run.post_tool_use()
    assert results(run) == ["ingested"]


def test_a_text_scan_outlasting_the_drain_is_too_large(run: Run) -> None:
    run.report(summary="done")
    now = [0.0]
    _drain_with(run, ClockedScanner(now, outbox.DRAIN_BUDGET + 1, inner=True), now)
    assert results(run) == ["too_large"]


def test_the_real_scan_stops_at_the_drain_deadline(
    secret_run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """secretindex's own interval check, with the drain's deadline: one artifact read as a
    single chunk, a clock that passes the deadline on the first check inside the scan."""
    monkeypatch.setattr(outbox, "_READ_CHUNK", ARTIFACT_MAX + 1)
    monkeypatch.setattr(secretindex, "CHECK_EVERY", 64)
    secret_run.plant(artifact_request(secret_run, os.urandom(ARTIFACT_MAX)))
    calls = [0]
    deadline_calls: list[int] = []

    def clock() -> float:
        calls[0] += 1
        if deadline_calls and calls[0] > deadline_calls[0]:
            return outbox.DRAIN_BUDGET + 1
        return 0.0

    original = secretindex.Scan.update

    stopped_inside: list[bool] = []

    def update(self: secretindex.Scan, data: bytes) -> bool:
        if len(data) > 1000:
            deadline_calls.append(calls[0] + 1)  # the check at the start of update passes
        try:
            return original(self, data)
        except outbox.ScanTimeout:
            stopped_inside.append(True)
            raise

    monkeypatch.setattr(secretindex.Scan, "update", update)
    drain = prepared(secret_run, clock=clock)
    with Store.open(secret_run.db) as store:
        with store.transaction():
            drain.record(store, run_no=1, live=True)
        drain.finish(committed=True)
    assert stopped_inside == [True]
    assert results(secret_run) == ["too_large"]
    assert listing(secret_run.private_run / "scan") == []


# Concurrency and ordering of recorded reports


def test_a_stop_blocks_when_a_concurrent_drain_rejected_its_winner(run: Run) -> None:
    """Both drains pre-check the same request; the other one records it rejected first, so
    this Stop ingests nothing and the backstop must still block."""
    run.report()
    mine, other = prepared(run), prepared(run)
    assert mine.winner is not None and other.winner is not None
    with Store.open(run.db) as store:
        with store.transaction():
            other.record(store, run_no=1, live=False)
        other.finish(committed=True)
        hook = hookevent.classify("Stop", json.dumps({"stop_hook_active": False}).encode())
        assert hook is not None
        applied = hookevent.apply_hook(store, SID, hook, mine)
        mine.finish(committed=applied.drained)
    assert applied.output == hookevent.backstop_output()
    assert results(run) == ["not_live"]
    assert run.run()["report_status"] is None and run.run()["backstop_used"] == 1
    assert run.state() == ("working", None, "working")  # a blocked Stop changes nothing


def test_an_older_request_never_replaces_a_newer_ingested_report(run: Run) -> None:
    t = 1_800_000_000_000_000_000
    newer = outbox.new_request_id(now_ns=lambda: t + 1_000_000)
    older = outbox.new_request_id(now_ns=lambda: t)
    run.plant(run.request(summary="newer"), rid=newer)
    run.post_tool_use()
    run.plant(run.request(summary="older"), rid=older)
    assert run.post_tool_use() == ""
    assert run.run()["report_summary"] == "newer"
    assert {r["request_id"]: r["result"] for r in run.requests()} == {
        newer: "ingested",
        older: "discarded",
    }
    assert len(run.report_events()) == 1


def test_a_freeze_failure_still_applies_the_hooks_own_event(tmp_path: Path) -> None:
    root = tmp_path / "state"
    Store.open(str(root / "state.db")).close()
    run = Run(root).setup(state="idle", activity="idle")
    run.write_artifact("a.md", b"a")
    run.report(artifact="a.md")
    (run.private_run / "artifact").write_text("not a directory")
    assert run.post_tool_use() == ""
    assert run.state() == ("working", None, "working")
    assert run.requests() == [] and run.run()["report_status"] is None
    assert len(listing(run.claimed)) == 1
    assert listing(run.private_run / "scan") == []
    (record,) = run.log()
    assert record["event"] == "hook_event_failed"
    assert record["message"].startswith("PostToolUse: outbox drain: ")
    (run.private_run / "artifact").unlink()
    run.post_tool_use()
    assert results(run) == ["ingested"]


def test_a_freeze_failure_in_a_headless_stop_blocks(run: Run) -> None:
    run.write_artifact("a.md", b"a")
    run.report(artifact="a.md")
    (run.private_run / "artifact").write_text("not a directory")
    assert json.loads(run.stop_hook()) == hookevent.backstop_output()
    assert run.run()["report_status"] is None
    assert len(listing(run.claimed)) == 1


def test_the_drain_budget_is_half_the_draining_hooks_timeout() -> None:
    from claude_session import settings_builder

    timeouts = {
        event: t for event, _, t in settings_builder.HOOKS if event in ("PostToolUse", "Stop")
    }
    assert set(timeouts.values()) == {outbox.DRAIN_BUDGET * 2}


def test_a_drain_auto_rollback_fails_the_whole_hook(
    run: Run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SQLite rolled the transaction back inside the drain: nothing may run in autocommit,
    so the Stop fails as a whole through the contained-error path (no backstop taken)."""
    from claude_session import __main__ as entry

    def auto_rolled_back(self: outbox.Drain, store: Store, **kwargs: Any) -> None:
        store.conn.execute("ROLLBACK")
        raise sqlite3.OperationalError("database or disk is full")

    real_main = hookevent.main

    def hook_main(argv: list[str]) -> int:
        payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False}).encode()
        return real_main(argv, stdin=io.BytesIO(payload), environ=run.environ())

    run.report()
    monkeypatch.setattr(outbox.Drain, "record", auto_rolled_back)
    monkeypatch.setattr(hookevent, "main", hook_main)
    monkeypatch.setattr(entry, "_silence_output", entry._restore_output)
    monkeypatch.setenv("CSESSION_ID", SID)
    assert entry.main(["hook-event", "--state-dir", str(run.root), "Stop"]) == 0
    assert capsys.readouterr().out == ""
    r = run.run()
    assert r["backstop_used"] == 0 and r["report_status"] is None
    assert run.state() == ("working", None, "working")
    assert run.events() == [] and run.requests() == []
    assert len(listing(run.claimed)) == 1  # left for the next drain
    assert [(e["event"], e["level"]) for e in run.log()] == [("hook_event_failed", "error")]
    assert run.log()[0]["message"] == "Stop: OperationalError"


def test_a_backstop_is_never_taken_after_an_auto_rollback(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = hookevent._apply_own_row

    def auto_rolled_back(store: Store, *args: Any) -> None:
        real(store, *args)
        store.conn.execute("ROLLBACK")

    monkeypatch.setattr(hookevent, "_apply_own_row", auto_rolled_back)
    with pytest.raises(hookevent._Backstop):
        run.stop_hook()
    assert run.run()["backstop_used"] == 0
    assert run.events() == []


def _deep_tree(parent: Path, name: str, depth: int) -> None:
    """A directory chain ``depth`` levels deep, made relative to directory fds so that no
    path ever exceeds ``PATH_MAX``."""
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir(name, dir_fd=fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
            name = "d"
        os.close(os.open("leaf", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
    finally:
        os.close(fd)


def test_a_junk_tree_deeper_than_the_descriptor_limit_is_removed(run: Run) -> None:
    """Removing a junk tree holds a bounded number of descriptors whatever its depth: a
    tree nested deeper than ``RLIMIT_NOFILE`` neither fails the drain nor stays behind."""
    resource = pytest.importorskip("resource")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    in_use = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else 32
    limit = in_use + 32
    if hard != resource.RLIM_INFINITY and limit > hard:
        pytest.skip("descriptor limit too low")
    _deep_tree(run.outbox, "junk", limit + 150)
    run.report(summary="beside a deep tree")
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
    try:
        assert run.post_tool_use() == ""
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert results(run) == ["ingested"]
    assert listing(run.outbox) == []
    assert malformed_counts(run) == [1]


def test_an_error_removing_one_junk_entry_does_not_fail_the_drain(
    run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``OSError`` while removing one junk tree leaves it for the next drain; the drain
    still removes the other junk and ingests the request."""
    (run.outbox / "bad" / "inner").mkdir(parents=True)
    (run.outbox / "good" / "inner").mkdir(parents=True)
    bad = os.stat(run.outbox / "bad" / "inner").st_ino
    real = os.scandir

    def failing(path: Any = ".") -> Any:
        if isinstance(path, int) and os.fstat(path).st_ino == bad:
            raise OSError(5, "injected")
        return real(path)

    run.report(summary="beside a broken tree")
    monkeypatch.setattr(os, "scandir", failing)
    assert run.post_tool_use() == ""
    monkeypatch.undo()
    assert results(run) == ["ingested"]
    assert listing(run.outbox) == ["bad"]
    assert malformed_counts(run) == [1]
    run.post_tool_use()
    assert listing(run.outbox) == []
    assert malformed_counts(run) == [1, 1]
