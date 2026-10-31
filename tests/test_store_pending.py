"""Pending runs and the adoption compare-and-set in the store (interface.md, Launch order;
design.md, Launch, adoption and pending runs). The races run in real, concurrent processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from claude_session import ids
from claude_session.store import Holder, NewRun, Store, format_ts

ME = Holder(pid=1000, start="100")
T0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


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
        "spec": {},
        "resolved": {"kind": "implement"},
        "config_hash": "ab" * 32,
        "invocation": {"actor": "auto:unknown"},
        "hook_interpreter": "/usr/bin/python3",
        "config_path": "/cfg/config.yaml",
        "wrapper_pid": 7,
        "wrapper_start": "s:7",
    }
    return NewRun(**{**base, **fields})


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "state" / "state.db")


@pytest.fixture
def st(db: str, clock: Clock) -> Any:
    with Store.open(db, holder=ME, clock=clock) as s:
        yield s


def pending(st: Store, pending_for: float = 60.0, **fields: Any) -> str:
    """A run as start leaves it: allocated with start as its wrapper, then released."""
    sid = st.allocate(new_run(**fields), new_id=ids.new_session_id)
    released = st.release_pending(
        sid, 1, wrapper_pid=7, wrapper_start="s:7", pending_for=pending_for
    )
    assert released is not None
    return sid


def test_start_allocates_its_run_with_itself_as_the_wrapper_and_no_deadline(st: Store) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    row = st.run_record(sid, 1)
    assert (row["wrapper_pid"], row["wrapper_start"], row["expires_at"]) == (7, "s:7", None)
    assert row["ended_at"] is None and row["phase"] == "launch"
    assert (row["config_hash"], row["config_path"]) == ("ab" * 32, "/cfg/config.yaml")
    assert row["session_state"] == "starting"
    # Not adoptable before it is released.
    assert st.adopt(sid, wrapper_pid=11, wrapper_start="b:1").outcome == "already_adopted"


def test_release_makes_the_run_pending_with_its_deadline(st: Store, clock: Clock) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    clock.now = T0 + timedelta(seconds=5)
    expires = st.release_pending(sid, 1, wrapper_pid=7, wrapper_start="s:7", pending_for=60.0)
    assert expires == format_ts(T0 + timedelta(seconds=65))
    row = st.run_record(sid, 1)
    assert (row["wrapper_pid"], row["wrapper_start"], row["expires_at"]) == (None, None, expires)


@pytest.mark.parametrize(
    ("pid", "token", "final"),
    [(8, "s:7", False), (7, "s:8", False), (7, "s:7", True)],
)
def test_release_matches_only_starts_own_open_run(
    st: Store, pid: int, token: str, final: bool
) -> None:
    sid = st.allocate(new_run(), new_id=ids.new_session_id)
    if final:
        with st.transaction() as conn:
            conn.execute("UPDATE runs SET ended_at = 'x'")
    assert st.release_pending(sid, 1, wrapper_pid=pid, wrapper_start=token, pending_for=60) is None
    row = st.run_record(sid, 1)
    assert (row["wrapper_pid"], row["expires_at"]) == (7, None)


@pytest.mark.parametrize("fields", [{"wrapper_pid": None}, {"wrapper_start": None}])
def test_a_run_is_allocated_with_its_wrapper(st: Store, fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        st.allocate(new_run(**fields), new_id=ids.new_session_id)


def test_adoption_sets_the_wrapper_once(st: Store) -> None:
    sid = pending(st)
    first = st.adopt(sid, wrapper_pid=11, wrapper_start="b:1")
    assert (first.outcome, first.run_no, first.expired_now) == ("adopted", 1, False)
    row = st.run_record(sid, 1)
    assert (row["wrapper_pid"], row["wrapper_start"]) == (11, "b:1")
    second = st.adopt(sid, wrapper_pid=12, wrapper_start="b:2")
    assert second.outcome == "already_adopted"
    assert st.run_record(sid, 1)["wrapper_pid"] == 11
    # Adoption is no state change: no event besides the launch event.
    assert st.conn.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)


def test_adoption_overrides_the_outcome_file_only_when_given(st: Store) -> None:
    sid = pending(st, outcome_file="/a/out.json")
    st.adopt(sid, wrapper_pid=11, wrapper_start="b:1")
    assert st.run_record(sid, 1)["outcome_file"] == "/a/out.json"
    other = pending(st, outcome_file="/a/out.json")
    st.adopt(other, wrapper_pid=11, wrapper_start="b:1", outcome_file="/b/out.json")
    assert st.run_record(other, 1)["outcome_file"] == "/b/out.json"


def test_a_pending_run_past_its_deadline_is_finalized_never_adopted_and_refused(
    st: Store, clock: Clock
) -> None:
    sid = pending(st)
    clock.now = T0 + timedelta(seconds=60)  # exactly at expires_at: expired (now < expires_at)
    result = st.adopt(sid, wrapper_pid=11, wrapper_start="b:1")
    assert (result.outcome, result.expired_now) == ("expired", True)
    row = st.run_record(sid, 1)
    assert row["wrapper_pid"] is None
    assert (row["outcome"], row["end_reason"], row["exit_code"], row["phase"]) == (
        "not-started",
        "never_adopted",
        12,
        "launch",
    )
    assert row["session_state"] == "not-started"
    finals = st.conn.execute("SELECT cause, to_state FROM events WHERE final = 1").fetchall()
    assert finals == [("adopt", "not-started")]
    # Later attempts: final, so expired, and nothing changes.
    again = st.adopt(sid, wrapper_pid=12, wrapper_start="b:2")
    assert (again.outcome, again.expired_now) == ("expired", False)
    assert st.conn.execute("SELECT COUNT(*) FROM events WHERE final = 1").fetchone() == (1,)


def test_just_before_the_deadline_adoption_wins(st: Store, clock: Clock) -> None:
    sid = pending(st)
    clock.now = T0 + timedelta(seconds=59.999)
    assert st.adopt(sid, wrapper_pid=11, wrapper_start="b:1").outcome == "adopted"


@pytest.mark.parametrize("expires", [None, "not a time", b"2999-01-01T00:00:00.000Z", 5])
def test_a_pending_run_without_a_readable_deadline_is_never_adoptable(
    st: Store, expires: object
) -> None:
    sid = pending(st)
    with st.transaction() as conn:
        conn.execute("UPDATE runs SET expires_at = ?", (expires,))
    assert st.adopt(sid, wrapper_pid=11, wrapper_start="b:1").outcome == "expired"


def test_expire_pending_finalizes_a_run_whose_deadline_is_a_blob(st: Store) -> None:
    sid = pending(st)
    with st.transaction() as conn:
        conn.execute("UPDATE runs SET expires_at = ?", (b"2999-01-01T00:00:00.000Z",))
    assert st.expire_pending(sid, 1)
    assert st.run_record(sid, 1)["end_reason"] == "never_adopted"


def test_a_final_run_is_refused_expired(st: Store) -> None:
    sid = pending(st)
    with st.transaction() as conn:
        conn.execute("UPDATE runs SET ended_at = 'x', end_reason = 'canceled'")
    result = st.adopt(sid, wrapper_pid=11, wrapper_start="b:1")
    assert (result.outcome, result.expired_now) == ("expired", False)


def test_a_forgotten_or_unknown_session_is_not_found(st: Store) -> None:
    sid = pending(st)
    with st.transaction() as conn:
        conn.execute("UPDATE sessions SET forgotten_at = 'x'")
    assert st.adopt(sid, wrapper_pid=11, wrapper_start="b:1").outcome == "not_found"
    assert st.run_record(sid, 1)["wrapper_pid"] is None
    assert st.adopt("cs-zzzzzzzz", wrapper_pid=11, wrapper_start="b").outcome == "not_found"


def test_expire_pending_only_finalizes_a_pending_run_past_its_deadline(
    st: Store, clock: Clock
) -> None:
    sid = pending(st)
    assert not st.expire_pending(sid, 1)
    clock.now = T0 + timedelta(seconds=61)
    assert st.expire_pending(sid, 1, cause="liveness")
    assert not st.expire_pending(sid, 1)
    assert st.conn.execute("SELECT cause FROM events WHERE final = 1").fetchall() == [("liveness",)]
    clock.now = T0
    adopted = pending(st)
    assert st.adopt(adopted, wrapper_pid=1, wrapper_start="x").outcome == "adopted"
    clock.now = T0 + timedelta(seconds=600)
    assert not st.expire_pending(adopted, 1)  # adopted: never expired


def test_current_run_reads_the_session_columns(st: Store) -> None:
    sid = pending(st)
    with st.transaction() as conn:
        conn.execute("UPDATE sessions SET git_dir = 'none'")
    record = st.current_run(sid)
    assert record is not None
    assert (record["run_no"], record["session_git_dir"], record["session_forgotten_at"]) == (
        1,
        "none",
        None,
    )
    assert st.current_run("cs-zzzzzzzz") is None


# Races between real processes

RACER = """
import json, os, sys, time
from claude_session.store import Store
db, sid, go, action = sys.argv[1:5]
st = Store.open(db)
while not os.path.exists(go):
    time.sleep(0.001)
if action == "adopt":
    result = st.adopt(sid, wrapper_pid=os.getpid(), wrapper_start="t")
    print(json.dumps([result.outcome, result.expired_now]))
elif action == "expire":
    print(json.dumps(["finalized" if st.expire_pending(sid, 1, cause="liveness") else "no", False]))
elif action == "forget":
    with st.transaction() as conn:
        conn.execute("UPDATE sessions SET forgotten_at = 'x' WHERE csession_id = ?", (sid,))
    print(json.dumps(["forgot", False]))
"""


def race(tmp_path: Path, db: str, sid: str, actions: list[str]) -> list[list[Any]]:
    go = tmp_path / "go"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", RACER, db, sid, str(go), action],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for action in actions
    ]
    time.sleep(0.3)  # every racer has opened the store and is spinning on the go file
    go.write_text("")
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        results.append(json.loads(out))
    return results


def test_concurrent_adopters_have_exactly_one_winner(tmp_path: Path, db: str, st: Store) -> None:
    st.close()
    with Store.open(db) as real:
        sid = pending(real)
    results = race(tmp_path, db, sid, ["adopt"] * 8)
    outcomes = sorted(r[0] for r in results)
    assert outcomes == ["adopted"] + ["already_adopted"] * 7
    with Store.open(db) as real:
        row = real.run_record(sid, 1)
    assert row["wrapper_pid"] is not None and row["ended_at"] is None


def test_adoption_racing_expiry_has_one_consistent_result(tmp_path: Path, db: str) -> None:
    # The deadline has passed for both: whichever goes first finalizes, and nobody adopts.
    with Store.open(db) as real:
        sid = pending(real, pending_for=0.0)
    results = race(tmp_path, db, sid, ["adopt", "expire", "adopt", "expire"])
    assert all(r[0] != "adopted" for r in results)
    with Store.open(db) as real:
        row = real.run_record(sid, 1)
        finals = real.conn.execute("SELECT COUNT(*) FROM events WHERE final = 1").fetchone()
    assert row["wrapper_pid"] is None and row["end_reason"] == "never_adopted"
    assert finals == (1,)
    # Exactly one process finalized it: an adopter (expired_now) or an expirer.
    finalizers = [r for r in results if r == ["expired", True] or r[0] == "finalized"]
    assert len(finalizers) == 1


def test_adoption_racing_forget_never_adopts_a_forgotten_session(tmp_path: Path, db: str) -> None:
    with Store.open(db) as real:
        sid = pending(real)
    results = race(tmp_path, db, sid, ["forget", "adopt", "adopt"])
    with Store.open(db) as real:
        row = real.run_record(sid, 1)
    adopted = [r for r in results if r[0] == "adopted"]
    assert len(adopted) <= 1
    if adopted:
        assert row["wrapper_pid"] is not None  # adoption won before forget
    else:
        assert row["wrapper_pid"] is None
        assert all(r[0] in ("not_found", "forgot") for r in results)
    assert os.path.exists(db)
