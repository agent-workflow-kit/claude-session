"""The store's launch and finalization methods (design.md, Data model)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claude_session import ids, store
from claude_session.errors import InternalError
from claude_session.outbox import Frozen
from claude_session.store import Final, Holder, NewRun, Store

ME = Holder(pid=1000, start="100")


@pytest.fixture
def st(tmp_path: Path) -> Any:
    with Store.open(str(tmp_path / "state" / "state.db"), holder=ME) as s:
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
        "spec": {"model": "m"},
        "resolved": {"kind": "implement"},
        "config_hash": None,
        "invocation": {"actor": "auto:unknown"},
        "hook_interpreter": "/usr/bin/python3",
        "config_path": None,
        "wrapper_pid": 4242,
        "wrapper_start": "boot:1",
        "labels": ("team=x",),
    }
    return NewRun(**{**base, **fields})


def one(st: Store, sql: str, *params: Any) -> tuple[Any, ...]:
    row = st.conn.execute(sql, params).fetchone()
    assert row is not None
    return tuple(row)


def test_allocate_inserts_the_session_its_run_labels_and_the_launch_event(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    assert ids.is_session_id(sid)
    assert one(st, "SELECT state, current_run, tainted, launch_kind FROM sessions") == (
        "starting",
        1,
        0,
        "implement",
    )
    assert one(st, "SELECT csession_dir, private_dir FROM sessions") == (
        f"/state/sessions/{sid}",
        f"/state/private/{sid}",
    )
    row = st.run_record(sid, 1)
    assert (row["phase"], row["wrapper_pid"], row["wrapper_start"]) == ("launch", 4242, "boot:1")
    assert row["ended_at"] is None and json.loads(row["spec_json"]) == {"model": "m"}
    labels = st.conn.execute("SELECT key, value FROM labels ORDER BY key").fetchall()
    assert labels == [("kind", "implement"), ("team", "x")]
    assert one(st, "SELECT cause, to_state, final FROM events") == ("launch", "starting", 0)


def test_allocate_checks_the_id_inside_its_transaction(st: Store) -> None:
    taken = st.allocate(new_run(), new_id=lambda used: "cs-aaaaaaaa")
    assert taken == "cs-aaaaaaaa"
    seen: list[bool] = []

    def new_id(used: Any) -> str:
        seen.append(used("cs-aaaaaaaa"))
        return "cs-bbbbbbbb"

    st.allocate(new_run(), new_id=new_id)
    assert seen == [True]


def test_a_third_party_reading_run_taints_the_session_at_allocation(st: Store) -> None:
    sid = st.allocate(new_run(reads_third_party=True), new_id=ids.new_session_id)
    tainted, by_run, at = one(st, "SELECT tainted, tainted_by_run, tainted_at FROM sessions")
    assert (tainted, by_run) == (1, 1) and at is not None
    assert st.run_record(sid, 1)["reads_third_party"] == 1


def test_update_run_sets_only_known_columns(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    st.update_run(sid, 1, cwd="/w", child_pid=7)
    assert st.run_record(sid, 1)["cwd"] == "/w"
    for bad in ({"outcome": "done"}, {"ended_at": "x"}, {"cwd = 1; --": 1}, {}):
        with pytest.raises(ValueError):
            st.update_run(sid, 1, **bad)
    with pytest.raises(InternalError):
        st.update_run(sid, 2, cwd="/w")
    with pytest.raises(ValueError):
        st.update_session(sid, state="exited")


def test_run_warnings_are_appended_once(st: Store) -> None:
    sid = st.allocate(new_run(warnings=("a",)), new_id=ids.new_session_id)
    st.add_run_warnings(sid, 1, ["b", "a", "b"])
    assert json.loads(st.run_record(sid, 1)["warnings_json"]) == ["a", "b"]


FINAL = Final(
    phase="session",
    outcome="exited-unreported",
    end_reason="no_report",
    exit_code=14,
    state="exited-unreported",
    cause="exit",
    cost_usd=0.5,
    num_turns=2,
    is_error=False,
)


def test_finalize_is_a_compare_and_set_with_one_final_event(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    assert st.finalize_run(sid, 1, FINAL)
    assert not st.finalize_run(
        sid, 1, Final("launch", "not-started", "canceled", 12, "not-started", "stop")
    )
    row = st.run_record(sid, 1)
    assert (row["outcome"], row["end_reason"], row["exit_code"], row["is_error"]) == (
        "exited-unreported",
        "no_report",
        14,
        0,
    )
    assert row["ended_at"] is not None and row["session_state"] == "exited-unreported"
    finals = st.conn.execute("SELECT cause, to_state FROM events WHERE final = 1").fetchall()
    assert finals == [("exit", "exited-unreported")]


def test_a_run_with_a_report_ends_exited(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    frozen = Frozen(name="result.md", path="/p/result.md", sha256="0" * 64, size=1, ingest_seq=1)
    assert st.set_implicit_report(sid, 1, frozen)
    assert not st.set_implicit_report(sid, 1, frozen)  # never over a report
    assert st.finalize_run(sid, 1, Final("session", "done", "normal", 0, "exited", "exit"))
    row = st.run_record(sid, 1)
    assert (row["report_status"], row["report_implicit"], row["report_summary"]) == (
        "done",
        1,
        None,
    )
    assert row["session_state"] == "exited"


def test_finalize_can_require_the_report_status_it_mapped_from(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    frozen = Frozen(name="result.md", path="/p/result.md", sha256="0" * 64, size=1, ingest_seq=1)
    assert st.set_implicit_report(sid, 1, frozen)
    # Mapped without a report: the report committed since must not be overwritten.
    assert not st.finalize_run(sid, 1, FINAL, report_status=None)
    assert st.run_record(sid, 1)["ended_at"] is None
    assert st.finalize_run(
        sid, 1, Final("session", "done", "normal", 0, "exited", "exit"), report_status="done"
    )
    assert st.run_record(sid, 1)["outcome"] == "done"


def test_finalize_keeps_a_message_set_earlier_when_given_none(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    st.update_run(sid, 1, message="earlier")
    st.finalize_run(sid, 1, FINAL)
    assert st.run_record(sid, 1)["message"] == "earlier"


def test_run_record_of_a_missing_run(st: Store) -> None:
    with pytest.raises(InternalError):
        st.run_record("cs-aaaaaaaa", 1)


def test_start_time_of_a_missing_process_is_empty() -> None:
    assert store.start_time(2**22 + 5) == ""


# pending_cause and the auto-exit compare-and-set (design.md, Supervision)


def add_event(st: Store, sid: str, cause: str, to_state: str, to_activity: str | None) -> int:
    with st.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO events (csession_id, run_no, event_ts, to_state, to_activity, cause) "
            "VALUES (?, 1, '2026-01-01T00:00:00Z', ?, ?, ?)",
            (sid, to_state, to_activity, cause),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_set_pending_cause_keeps_an_earlier_cause(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    assert st.set_pending_cause(sid, 1, "stopped")
    assert not st.set_pending_cause(sid, 1, "forwarded_term")
    assert st.run_record(sid, 1)["pending_cause"] == "stopped"


def test_set_pending_cause_leaves_a_final_run_alone(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    final = Final(
        phase="session",
        outcome="exited-unreported",
        end_reason="normal",
        exit_code=14,
        state="exited-unreported",
        cause="exit",
    )
    assert st.finalize_run(sid, 1, final)
    assert not st.set_pending_cause(sid, 1, "forwarded_term")
    assert st.run_record(sid, 1)["pending_cause"] is None


def test_latest_event_is_by_id(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    first = st.latest_event(sid, 1)
    assert first is not None and first["cause"] == "launch"
    eid = add_event(st, sid, "Stop", "reported", "idle")
    assert st.latest_event(sid, 1) == {
        "id": eid,
        "cause": "Stop",
        "to_state": "reported",
        "to_activity": "idle",
        "final": False,
    }
    assert st.latest_event(sid, 2) is None


def test_set_auto_exit_cause_rechecks_report_event_and_cause(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    stop = add_event(st, sid, "Stop", "reported", "idle")
    assert not st.set_auto_exit_cause(sid, 1, stop)  # no report
    st.conn.execute("UPDATE runs SET report_status = 'blocked' WHERE csession_id = ?", (sid,))
    assert not st.set_auto_exit_cause(sid, 1, stop)  # not done
    st.conn.execute("UPDATE runs SET report_status = 'done' WHERE csession_id = ?", (sid,))
    newer = add_event(st, sid, "UserPromptSubmit", "reported", "working")
    assert not st.set_auto_exit_cause(sid, 1, stop)  # a newer event
    assert st.run_record(sid, 1)["pending_cause"] is None
    assert st.set_auto_exit_cause(sid, 1, newer)
    assert st.run_record(sid, 1)["pending_cause"] == "auto_exit"
    assert not st.set_auto_exit_cause(sid, 1, newer)  # a cause already exists
