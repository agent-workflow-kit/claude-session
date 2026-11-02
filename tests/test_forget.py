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

from claude_session import config, ids, liveness, sessiondir, store, tmpdir
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


# review round 1


def dead_wrapper(st: Store, root: str, procs: Procs, **fields: Any) -> str:  # noqa: F811
    """An old session whose run 1 is open with a wrapper that is gone (crash-left)."""
    pid, token = procs.dead()
    sid = make(st, root, final=False, **fields)
    st.conn.execute(
        "UPDATE runs SET wrapper_pid = ?, wrapper_start = ? WHERE csession_id = ?",
        (pid, token, sid),
    )
    st.conn.execute("UPDATE sessions SET updated_at = ? WHERE csession_id = ?", (LONG_AGO, sid))
    return sid


def test_bulk_forgets_a_crash_left_session_and_the_dry_run_agrees(
    st: Store,
    root: str,
    capsys: pytest.CaptureFixture[str],
    procs: Procs,  # noqa: F811
) -> None:
    sid = dead_wrapper(st, root, procs)
    code, dry, _ = forget(capsys, "--older-than", "1d", "--dry-run")
    assert code == 0
    code, real, _ = forget(capsys, "--older-than", "1d")
    assert code == 0
    assert [e["csession_id"] for e in dry["forgotten"]] == [sid]
    assert [e["csession_id"] for e in real["forgotten"]] == [sid]
    assert dry["skipped"] == real["skipped"] == []
    assert forgotten_at(st, sid) is not None
    assert [(r["target"], r["result"]) for r in audit_records(root)] == [(sid, "ok")]


def test_bulk_state_filter_matches_the_state_after_the_check(
    st: Store,
    root: str,
    capsys: pytest.CaptureFixture[str],
    procs: Procs,  # noqa: F811
) -> None:
    sid = dead_wrapper(st, root, procs)
    (before,) = st.conn.execute(
        "SELECT state FROM sessions WHERE csession_id = ?", (sid,)
    ).fetchone()
    checked = liveness.check_session(st, config.load(None), sid, cause="forget", dry_run=True)
    (after,) = {c.to_state for c in checked.changes if c.to_state is not None}
    assert after != before
    _, out, _ = forget(capsys, "--older-than", "1d", "--state", before, "--dry-run")
    assert out["forgotten"] == []
    _, out, _ = forget(capsys, "--older-than", "1d", "--state", after, "--dry-run")
    assert [e["csession_id"] for e in out["forgotten"]] == [sid]
    _, out, _ = forget(capsys, "--older-than", "1d", "--state", after)
    assert [e["csession_id"] for e in out["forgotten"]] == [sid]
    assert forgotten_at(st, sid) is not None


def test_bulk_never_checks_sessions_its_filters_exclude(
    st: Store,
    root: str,
    capsys: pytest.CaptureFixture[str],
    procs: Procs,  # noqa: F811
) -> None:
    sid = dead_wrapper(st, root, procs, kind="review", labels=("team=y",))
    events = st.conn.execute("SELECT count(*) FROM events WHERE csession_id = ?", (sid,)).fetchone()
    for argv in (("--kind", "other"), ("--label", "team=x")):
        _, out, _ = forget(capsys, "--older-than", "1d", *argv)
        assert out == {"dry_run": False, "forgotten": [], "skipped": []}
    assert st.has_open_run(sid)
    assert (
        st.conn.execute("SELECT count(*) FROM events WHERE csession_id = ?", (sid,)).fetchone()
        == events
    )


def test_a_run_started_after_the_check_is_refused_and_audited(
    st: Store, root: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = make(st, root)
    monkeypatch.setattr(Store, "forget_session", lambda self, *a, **k: "live")
    code, out, _ = forget(capsys, sid)
    assert code == 20 and out["error"]["reason"] == "live"
    assert [(r["target"], r["result"]) for r in audit_records(root)] == [(sid, "refused")]


def test_a_refusal_that_cannot_be_audited_exits_1(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = make(st, root, final=False)
    (Path(root) / "audit.jsonl").mkdir()
    code, out, _ = forget(capsys, sid)
    assert code == 1 and out["error"]["code"] == "internal"
    assert forgotten_at(st, sid) is None


def test_bulk_skips_are_not_audited(
    st: Store, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    live = make(st, root, final=False)
    _, out, _ = forget(capsys, "--older-than", "1d")
    assert out["skipped"] == [{"csession_id": live, "reason": "live"}]
    assert audit_records(root) == []


def test_an_interrupt_after_the_ok_record_writes_the_error_record(
    st: Store, root: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_session import audit, forgetting
    from claude_session.gateway import SystemGateway

    sid = make(st, root)
    original = audit.append
    calls = {"n": 0}

    def interrupted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        record = original(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return record

    monkeypatch.setattr(audit, "append", interrupted)
    with pytest.raises(KeyboardInterrupt):
        forgetting.forget_session(
            st,
            config.load(None),
            sid,
            gateway=SystemGateway(),  # nothing to deliver: never runs a subprocess
            environ={},
            actor="auto:test",
        )
    assert forgotten_at(st, sid) is None
    assert [r["result"] for r in audit_records(root)] == ["ok", "error"]


def test_an_interrupt_after_the_commit_writes_no_error_record(
    st: Store, root: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_session import forgetting
    from claude_session.gateway import SystemGateway

    sid = make(st, root)
    original = Store.forget_session

    def interrupted(self: Store, *args: Any, **kwargs: Any) -> Any:
        original(self, *args, **kwargs)
        raise KeyboardInterrupt  # after the transaction committed

    monkeypatch.setattr(Store, "forget_session", interrupted)
    with pytest.raises(KeyboardInterrupt):
        forgetting.forget_session(
            st,
            config.load(None),
            sid,
            gateway=SystemGateway(),  # nothing to deliver: never runs a subprocess
            environ={},
            actor="auto:test",
        )
    assert forgotten_at(st, sid) is not None
    assert [r["result"] for r in audit_records(root)] == ["ok"]


def test_retention_keeps_the_taint_of_a_deleted_source_on_its_unforked_forks(
    st: Store, root: str, capsys: pytest.CaptureFixture[str], cfg: str
) -> None:
    from claude_session.errors import RefusedError

    src = make(st, root)
    fork = st.allocate(
        new_run(
            state_root=root, wrapper_pid=os.getpid(), wrapper_start=store.start_time(os.getpid())
        ),
        new_id=ids.new_session_id,
        forked_from=src,
    )
    captured = st.allocate(
        new_run(
            state_root=root, wrapper_pid=os.getpid(), wrapper_start=store.start_time(os.getpid())
        ),
        new_id=ids.new_session_id,
        forked_from=src,
    )
    st.conn.execute(
        "UPDATE sessions SET claude_session_id = 'c-1' WHERE csession_id = ?", (captured,)
    )
    st.conn.execute(
        "UPDATE runs SET ended_at = ?, outcome = 'exited', end_reason = 'exit' "
        "WHERE csession_id IN (?, ?)",
        (LONG_AGO, fork, captured),
    )
    st.conn.execute("UPDATE events SET delivered_at = ?", (LONG_AGO,))
    st.conn.execute(
        "UPDATE sessions SET tainted = 1, tainted_at = '2021-01-01T00:00:00.000000Z' "
        "WHERE csession_id = ?",
        (src,),
    )
    assert st.forget_session(src) == "forgotten"
    age_forget(st, src, 8)
    out = reconcile(capsys, cfg)
    assert [c["csession_id"] for c in out["changes"] if c["action"] == "deleted"] == [src]
    row = st.conn.execute(
        "SELECT tainted, tainted_at, tainted_by_run FROM sessions WHERE csession_id = ?", (fork,)
    ).fetchone()
    assert tuple(row) == (1, "2021-01-01T00:00:00.000000Z", None)
    with pytest.raises(RefusedError):
        st.recheck_fork_taint(fork, 1, src)
    assert st.fork_source_taints(fork)  # the adoption dry run agrees
    # A fork that captured its own Claude id never consults its source again.
    assert (
        st.conn.execute(
            "SELECT tainted FROM sessions WHERE csession_id = ?", (captured,)
        ).fetchone()[0]
        == 0
    )


def stuck(st: Store, root: str, days: float) -> str:
    """A forgotten session past retention whose directory tree cannot be removed (deeper
    than the patched depth cap)."""
    sid = make(st, root)
    with sessiondir.SessionDirs.open(root, sid):
        pass
    deep = Path(sessiondir.session_dir(root, sid))
    for _ in range(10):
        deep = deep / "d"
        deep.mkdir()
    st.forget_session(sid)
    age_forget(st, sid, days)
    return sid


def test_sessions_that_cannot_be_deleted_never_starve_the_others(
    st: Store,
    root: str,
    capsys: pytest.CaptureFixture[str],
    cfg: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import random

    from claude_session import fsutil
    from claude_session import reconcile as reconciling

    monkeypatch.setattr(reconciling, "DELETE_MAX", 2)
    monkeypatch.setattr(reconciling, "_rng", random.Random(7))
    monkeypatch.setattr(fsutil, "tree_depth_cap", lambda: 5)
    blocked = [stuck(st, root, 30), stuck(st, root, 29)]
    clean = make(st, root)
    st.forget_session(clean)
    age_forget(st, clean, 8)
    for _ in range(20):
        out = reconcile(capsys, cfg)
        assert any("could not be deleted" in w for w in out["warnings"])
        if forgotten_at(st, clean) is None:
            break
    else:
        pytest.fail("the deletable session was starved")
    for sid in blocked:
        assert forgotten_at(st, sid) is not None  # kept, warned about every pass


def test_sessions_with_events_to_deliver_take_no_deletion_slot(
    st: Store,
    root: str,
    capsys: pytest.CaptureFixture[str],
    cfg: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_session import reconcile as reconciling

    monkeypatch.setattr(reconciling, "DELETE_MAX", 1)
    held = make(st, root)
    st.forget_session(held)
    age_forget(st, held, 30)
    st.conn.execute("UPDATE events SET delivered_at = NULL WHERE csession_id = ?", (held,))
    clean = make(st, root)
    st.forget_session(clean)
    age_forget(st, clean, 8)
    assert st.retention_due(st.clock() - timedelta(days=7)) == [clean]
    monkeypatch.setattr(reconciling, "_deliver", lambda *a, **k: None)
    out = reconcile(capsys, cfg)
    assert [c["csession_id"] for c in out["changes"] if c["action"] == "deleted"] == [clean]
    assert any("left to deliver" in w for w in out["warnings"])
    assert forgotten_at(st, held) is not None
