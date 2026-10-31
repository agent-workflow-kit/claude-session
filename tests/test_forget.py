"""``forget`` (single and bulk) and the retention deletion of ``reconcile`` (interface.md,
forget and reconcile; design.md, Reconcile, salvage and retention)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from claude_session import ids, sessiondir, store, tmpdir
from claude_session.cli import main
from claude_session.store import Store

from .schemas import validate
from .test_liveness import Procs, procs, wait_token  # noqa: F401  (the procs fixture)
from .test_store_runs import new_run

SCHEMA = "claude-session/cli/forget.json"
RECONCILE = "claude-session/cli/reconcile.json"
LONG_AGO = "2020-01-01T00:00:00.000000Z"


@pytest.fixture
def st() -> Iterator[Store]:
    with store.open_state() as s:
        yield s


@pytest.fixture
def root(st: Store) -> str:
    return os.path.dirname(st.path)


@pytest.fixture
def cfg(tmp_path: Path) -> str:
    path = tmp_path / "forget-config.yaml"
    path.write_text("retention_days: 7\n", encoding="utf-8")
    return str(path)


def make(st: Store, root: str, *, final: bool = True, old: bool = True, **fields: Any) -> str:
    """A session with run 1, final (exited) or open (this process as wrapper), updated long ago."""
    sid = st.allocate(
        new_run(
            state_root=root,
            wrapper_pid=os.getpid(),
            wrapper_start=store.start_time(os.getpid()),
            **fields,
        ),
        new_id=ids.new_session_id,
    )
    if final:
        st.conn.execute(
            "UPDATE runs SET ended_at = ?, outcome = 'exited', end_reason = 'exit' "
            "WHERE csession_id = ?",
            (LONG_AGO, sid),
        )
        st.conn.execute("UPDATE sessions SET state = 'exited' WHERE csession_id = ?", (sid,))
        st.conn.execute("UPDATE events SET delivered_at = ? WHERE csession_id = ?", (LONG_AGO, sid))
    if old:
        st.conn.execute("UPDATE sessions SET updated_at = ? WHERE csession_id = ?", (LONG_AGO, sid))
    return sid


def forget(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    code = main(["forget", "--json", "--actor", "auto:test", *argv])
    out = capsys.readouterr()
    text = out.out
    return code, (json.loads(text) if text.strip() else None), out.err


def audit_records(root: str) -> list[dict[str, Any]]:
    path = Path(root) / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def forgotten_at(st: Store, sid: str) -> str | None:
    row = st.conn.execute("SELECT forgotten_at FROM sessions WHERE csession_id = ?", (sid,))
    found = row.fetchone()
    return None if found is None else found[0]


# forget <id>


def test_forget_hides_the_session_audits_it_and_validates(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = make(st, root, labels=("team=x",))
    code, out, _ = forget(capsys, sid, "--decision-id", "d1")
    assert code == 0
    validate(out, SCHEMA)
    assert [e["csession_id"] for e in out["forgotten"]] == [sid] and out["skipped"] == []
    assert out["forgotten"][0]["forgotten_at"] == forgotten_at(st, sid)
    (rec,) = audit_records(root)
    assert (rec["action"], rec["target"], rec["result"], rec["actor"]) == (
        "forget",
        sid,
        "ok",
        "auto:test",
    )
    assert rec["decision_id"] == ["d1"]
    # hidden from prefix matching and every lookup: exit 10
    code, out, _ = forget(capsys, sid[:8])
    assert code == 10 and out["error"]["code"] == "not_found"


def test_delete_after_is_retention_days_later(
    st: Store, root: str, capsys: pytest.CaptureFixture[str], cfg: str
) -> None:
    sid = make(st, root)
    _, out, _ = forget(capsys, sid, "--config", cfg)
    entry = out["forgotten"][0]
    from claude_session.forgetting import delete_after

    assert delete_after(entry["forgotten_at"], 7) == entry["delete_after"]
    assert store.parse_ts(entry["delete_after"]) - store.parse_ts(entry["forgotten_at"]) == (
        timedelta(days=7)
    )


def test_a_live_session_is_refused_and_not_forgotten(
    st: Store,
    root: str,
    capsys: pytest.CaptureFixture[str],
    procs: Procs,  # noqa: F811
) -> None:
    pid, token = procs.live()
    sid = make(st, root, final=False)
    st.conn.execute(
        "UPDATE runs SET wrapper_pid = ?, wrapper_start = ? WHERE csession_id = ?",
        (pid, token, sid),
    )
    code, out, _ = forget(capsys, sid)
    assert code == 20 and out["error"]["reason"] == "live"
    assert forgotten_at(st, sid) is None
    assert procs.running(pid)  # never signaled
    assert [r["result"] for r in audit_records(root)] == ["refused"]


def test_a_pending_new_session_record_is_refused(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = make(st, root, final=False)
    st.conn.execute(
        "UPDATE runs SET wrapper_pid = NULL, wrapper_start = NULL, expires_at = ? "
        "WHERE csession_id = ?",
        (store.format_ts(st.clock() + timedelta(hours=1)), sid),
    )
    code, out, _ = forget(capsys, sid)
    assert code == 20 and out["error"]["reason"] == "live"
    assert forgotten_at(st, sid) is None


def test_a_run_whose_wrapper_is_gone_is_finalized_by_the_check_then_forgotten(
    st: Store,
    root: str,
    capsys: pytest.CaptureFixture[str],
    procs: Procs,  # noqa: F811
) -> None:
    pid, token = procs.dead()
    sid = make(st, root, final=False)
    st.conn.execute(
        "UPDATE runs SET wrapper_pid = ?, wrapper_start = ? WHERE csession_id = ?",
        (pid, token, sid),
    )
    code, out, _ = forget(capsys, sid)
    assert code == 0 and out["forgotten"][0]["csession_id"] == sid
    causes = st.conn.execute(
        "SELECT cause FROM events WHERE csession_id = ? AND final = 1", (sid,)
    ).fetchall()
    assert [c[0] for c in causes] == ["forget"]


def test_unknown_and_ambiguous_ids(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = forget(capsys, "cs-zzzzzzzz")
    assert code == 10
    code, out, _ = forget(capsys, "cs-aaaa")
    assert code == 10


def test_forgetting_twice_is_not_found(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = make(st, root)
    assert forget(capsys, sid)[0] == 0
    assert forget(capsys, sid)[0] == 10
    assert len(audit_records(root)) == 1


def test_a_failing_audit_forgets_nothing_and_exits_1(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = make(st, root)
    (Path(root) / "audit.jsonl").mkdir()  # cannot be appended to
    code, out, _ = forget(capsys, sid)
    assert code == 1
    assert forgotten_at(st, sid) is None


def test_a_failing_audit_stops_a_bulk_run(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = make(st, root), make(st, root)
    (Path(root) / "audit.jsonl").mkdir()
    code, _, _ = forget(capsys, "--older-than", "1d")
    assert code == 1
    assert forgotten_at(st, a) is None and forgotten_at(st, b) is None


def test_the_audit_inputs_never_leak_a_secret_actor(
    st: Store,
    root: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "s.yaml"
    cfg_path.write_text("secret_env: [MY_TOKEN]\n", encoding="utf-8")
    monkeypatch.setenv("MY_TOKEN", "supersecretvalue123")
    sid = make(st, root)
    code = main(
        ["forget", sid, "--json", "--config", str(cfg_path), "--actor", "auto:supersecretvalue123"]
    )
    capsys.readouterr()
    assert code == 0
    assert "supersecretvalue123" not in (Path(root) / "audit.jsonl").read_text()


def test_dry_run_writes_nothing(st: Store, root: str, capsys: pytest.CaptureFixture[str]) -> None:
    sid = make(st, root)
    code, out, _ = forget(capsys, sid, "--dry-run")
    assert code == 0 and out["dry_run"] is True
    validate(out, SCHEMA)
    assert out["forgotten"][0]["csession_id"] == sid
    assert forgotten_at(st, sid) is None and audit_records(root) == []


# bulk


def test_bulk_filters_live_and_young_sessions(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    old = make(st, root, labels=("team=x", "k=v"))
    other_label = make(st, root, labels=("team=y",))
    young = make(st, root, old=False)
    live = make(st, root, final=False, labels=("z=z",))
    code, out, _ = forget(capsys, "--older-than", "1d", "--label", "team=x")
    assert code == 0
    validate(out, SCHEMA)
    assert [e["csession_id"] for e in out["forgotten"]] == [old]
    assert out["skipped"] == []  # live is not a match of the label filter
    assert forgotten_at(st, other_label) is None and forgotten_at(st, young) is None
    code, out, _ = forget(capsys, "--older-than", "1d")
    ids_forgotten = {e["csession_id"] for e in out["forgotten"]}
    assert ids_forgotten == {other_label}
    assert out["skipped"] == [{"csession_id": live, "reason": "live"}]
    assert forgotten_at(st, live) is None
    assert {r["target"] for r in audit_records(root)} == {old, other_label}


def test_bulk_state_and_kind_filters(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    a = make(st, root, kind="review")
    b = make(st, root, kind="implement")
    st.conn.execute("UPDATE sessions SET state = 'lost' WHERE csession_id = ?", (b,))
    _, out, _ = forget(capsys, "--older-than", "1d", "--state", "lost", "--state", "killed")
    assert [e["csession_id"] for e in out["forgotten"]] == [b]
    _, out, _ = forget(capsys, "--older-than", "1d", "--kind", "implement")
    assert out["forgotten"] == []
    _, out, _ = forget(capsys, "--older-than", "1d", "--kind", "review")
    assert [e["csession_id"] for e in out["forgotten"]] == [a]


def test_bulk_dry_run_and_empty(st: Store, root: str, capsys: pytest.CaptureFixture[str]) -> None:
    sid = make(st, root)
    _, out, _ = forget(capsys, "--older-than", "1d", "--dry-run")
    assert [e["csession_id"] for e in out["forgotten"]] == [sid]
    assert forgotten_at(st, sid) is None and audit_records(root) == []
    _, out, _ = forget(capsys, "--older-than", "36500d")
    assert out == {"dry_run": False, "forgotten": [], "skipped": []}


def test_a_session_that_changed_since_selection_is_left_alone(st: Store, root: str) -> None:
    sid = make(st, root)
    seen = st.forget_candidates(st.clock())[0]
    st.conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE csession_id = ?",
        (store.format_ts(st.clock()), sid),
    )
    assert st.forget_session(seen[0], updated_at=seen[1]) == "changed"
    assert forgotten_at(st, sid) is None


def test_store_refuses_an_open_run_and_a_forgotten_session(st: Store, root: str) -> None:
    live = make(st, root, final=False)
    done = make(st, root)
    assert st.forget_session(live) == "live"
    assert st.forget_session(done) == "forgotten"
    assert st.forget_session(done) == "gone"
    assert st.forget_session("cs-aaaaaaaa") == "gone"


# retention


def reconcile(capsys: pytest.CaptureFixture[str], cfg: str, *extra: str) -> dict[str, Any]:
    assert main(["reconcile", "--json", "--config", cfg, *extra]) == 0
    out = json.loads(capsys.readouterr().out)
    validate(out, RECONCILE)
    return out


def age_forget(st: Store, sid: str, days: float) -> None:
    st.conn.execute(
        "UPDATE sessions SET forgotten_at = ? WHERE csession_id = ?",
        (store.format_ts(st.clock() - timedelta(days=days)), sid),
    )


def test_reconcile_deletes_expired_forgotten_sessions_and_keeps_the_rest(
    st: Store, root: str, capsys: pytest.CaptureFixture[str], cfg: str
) -> None:
    gone, fresh, kept = make(st, root), make(st, root), make(st, root)
    for sid in (gone, fresh, kept):
        with sessiondir.SessionDirs.open(root, sid) as dirs:
            dirs.write_instructions("x")
    assert st.forget_session(gone) == st.forget_session(fresh) == "forgotten"
    age_forget(st, gone, 8)
    age_forget(st, fresh, 6)
    out = reconcile(capsys, cfg, "--dry-run")
    assert [
        (c["csession_id"], c["action"]) for c in out["changes"] if c["action"] == "deleted"
    ] == [(gone, "deleted")]
    assert forgotten_at(st, gone) is not None  # a dry run deletes nothing
    out = reconcile(capsys, cfg)
    deleted = [c for c in out["changes"] if c["action"] == "deleted"]
    assert [c["csession_id"] for c in deleted] == [gone]
    assert not Path(sessiondir.session_dir(root, gone)).exists()
    assert not Path(sessiondir.private_dir(root, gone)).exists()
    for sid in (fresh, kept):
        assert Path(sessiondir.session_dir(root, sid)).is_dir()
    assert (
        st.conn.execute("SELECT count(*) FROM sessions WHERE csession_id = ?", (gone,)).fetchone()[
            0
        ]
        == 0
    )
    assert (
        st.conn.execute("SELECT count(*) FROM runs WHERE csession_id = ?", (gone,)).fetchone()[0]
        == 0
    )
    assert (
        st.conn.execute("SELECT count(*) FROM events WHERE csession_id = ?", (gone,)).fetchone()[0]
        == 0
    )
    assert (
        st.conn.execute("SELECT count(*) FROM labels WHERE csession_id = ?", (gone,)).fetchone()[0]
        == 0
    )
    assert st.session_id_used(gone)  # kept in deleted_ids: never reused
    assert reconcile(capsys, cfg)["changes"] == []  # idempotent


def test_retention_never_follows_a_planted_symlink(
    st: Store, root: str, capsys: pytest.CaptureFixture[str], cfg: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "precious").write_text("keep")
    sid = make(st, root)
    with sessiondir.SessionDirs.open(root, sid) as dirs:
        dirs.write_instructions("x")
    session = Path(sessiondir.session_dir(root, sid))
    os.symlink(outside, session / "link-to-dir")
    os.symlink(outside / "sub" / "precious", session / "link-to-file")
    (session / "work").mkdir()
    os.symlink(outside, session / "work" / "deep")
    os.symlink(outside, Path(sessiondir.private_dir(root, sid)) / "pl")
    st.forget_session(sid)
    age_forget(st, sid, 8)
    reconcile(capsys, cfg)
    assert not session.exists() and not Path(sessiondir.private_dir(root, sid)).exists()
    assert (outside / "sub" / "precious").read_text() == "keep"


def test_retention_unlinks_a_session_dir_that_is_a_symlink(
    st: Store, root: str, capsys: pytest.CaptureFixture[str], cfg: str, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep")
    sid = make(st, root)
    session = Path(sessiondir.session_dir(root, sid))
    session.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, session)
    st.forget_session(sid)
    age_forget(st, sid, 8)
    reconcile(capsys, cfg)
    assert not os.path.lexists(session)
    assert (outside / "precious").read_text() == "keep"
    assert forgotten_at(st, sid) is None  # the row is gone too


def test_a_failed_directory_removal_keeps_the_rows_for_the_next_pass(
    st: Store, root: str, capsys: pytest.CaptureFixture[str], cfg: str
) -> None:
    sid = make(st, root)
    with sessiondir.SessionDirs.open(root, sid):
        pass
    deep = Path(sessiondir.session_dir(root, sid))
    for _ in range(40):
        deep = deep / "d"
        deep.mkdir()
    st.forget_session(sid)
    age_forget(st, sid, 8)
    from claude_session import fsutil

    original = fsutil.tree_depth_cap
    fsutil.tree_depth_cap = lambda: 5  # type: ignore[assignment]
    try:
        out = reconcile(capsys, cfg)
    finally:
        fsutil.tree_depth_cap = original  # type: ignore[assignment]
    assert out["warnings"] and not [c for c in out["changes"] if c["action"] == "deleted"]
    assert forgotten_at(st, sid) is not None
    out = reconcile(capsys, cfg)
    assert [c["csession_id"] for c in out["changes"] if c["action"] == "deleted"] == [sid]


def test_retention_removes_the_tmpdir(
    st: Store, root: str, capsys: pytest.CaptureFixture[str], cfg: str
) -> None:
    sid = make(st, root)
    path = Path(tmpdir.create(sid))
    (path / "f").write_text("x")
    st.forget_session(sid)
    age_forget(st, sid, 8)
    reconcile(capsys, cfg)
    assert not path.exists()
    assert forgotten_at(st, sid) is None


def test_rows_with_an_event_left_to_deliver_are_kept(st: Store, root: str) -> None:
    sid = make(st, root)
    st.forget_session(sid)
    st.conn.execute("UPDATE events SET delivered_at = NULL WHERE csession_id = ?", (sid,))
    assert st.delete_session_rows(sid, st.clock()) is False
    st.conn.execute("UPDATE events SET delivered_at = ? WHERE csession_id = ?", (LONG_AGO, sid))
    assert st.delete_session_rows(sid, st.clock()) is True


def test_rows_not_yet_past_retention_are_kept(st: Store, root: str) -> None:
    sid = make(st, root)
    st.forget_session(sid)
    assert st.delete_session_rows(sid, st.clock() - timedelta(days=1)) is False


def test_a_session_with_an_open_run_is_never_deleted(st: Store, root: str) -> None:
    sid = make(st, root, final=False)
    st.conn.execute("UPDATE sessions SET forgotten_at = ? WHERE csession_id = ?", (LONG_AGO, sid))
    assert st.retention_due(st.clock()) == []
    assert st.delete_session_rows(sid, st.clock()) is False
