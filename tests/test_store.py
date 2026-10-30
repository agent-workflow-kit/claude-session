"""state.db: WAL, migrations, leases and the schema-version check (design.md, Data model)."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_session import store
from claude_session.errors import SchemaNewerError
from claude_session.store import LEASE_TTL, Holder, Migration, Store

T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
ME = Holder(pid=1000, start="100")
OTHER = Holder(pid=2000, start="200")
DESIGN_TABLES = {
    "sessions",
    "runs",
    "claude_ids",
    "labels",
    "events",
    "requests",
    "deleted_ids",
    "leases",
    "meta",
}


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeProcesses:
    """Liveness by exact (pid, start time): a reused PID with another start time is dead."""

    def __init__(self, *alive: Holder) -> None:
        self.alive = {(h.pid, h.start) for h in alive}
        self.calls: list[tuple[int, str]] = []

    def __call__(self, pid: int, start: str) -> bool:
        self.calls.append((pid, start))
        return (pid, start) in self.alive


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "state" / "state.db")


def open_as(
    path: str,
    holder: Holder,
    *,
    alive: FakeProcesses | None = None,
    clock: FakeClock | None = None,
    **kwargs: object,
) -> Store:
    return Store.open(
        path,
        holder=holder,
        liveness=alive or FakeProcesses(ME, OTHER),
        clock=clock or FakeClock(),
        **kwargs,  # type: ignore[arg-type]
    )


def tables(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {name for (name,) in rows if not name.startswith("sqlite_")}


def set_version(path: str, version: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (version,))
        conn.commit()
    finally:
        conn.close()


# State directory


def test_state_dir_follows_xdg_state_home() -> None:
    assert store.state_dir({"XDG_STATE_HOME": "/x/state"}) == "/x/state/claude-session"


@pytest.mark.parametrize("value", ["", "relative/state"])
def test_state_dir_ignores_an_empty_or_relative_xdg_state_home(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/home/u")
    assert store.state_dir({"XDG_STATE_HOME": value}) == "/home/u/.local/state/claude-session"


def test_state_dir_defaults_to_local_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/u")
    assert store.state_dir({}) == "/home/u/.local/state/claude-session"


# Open, WAL and modes


def test_open_creates_a_private_wal_database(db: str) -> None:
    old = os.umask(0)
    try:
        open_as(db, ME).close()
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(os.path.dirname(db)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()


def test_open_keeps_the_mode_of_an_existing_state_dir(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir(mode=0o750)
    os.chmod(root, 0o750)
    open_as(str(root / "state.db"), ME).close()
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o750


def test_fresh_database_has_the_design_tables_and_current_version(db: str) -> None:
    with open_as(db, ME) as s:
        assert s.schema_version() == store.code_version()
    assert tables(db) == DESIGN_TABLES


def test_migrate_lease_is_released_after_migration(db: str) -> None:
    with open_as(db, ME) as s:
        assert s.lease_holder(store.MIGRATE_LEASE) is None


def test_open_without_migrate_does_not_create_or_migrate(tmp_path: Path) -> None:
    path = str(tmp_path / "state.db")
    open_as(path, ME).close()
    set_version(path, "99.0")
    with open_as(path, ME, migrate=False) as s:  # hook-event: never migrates, never refuses
        assert s.schema_version() == (99, 0)


def test_open_without_migrate_never_creates_a_database(tmp_path: Path) -> None:
    path = tmp_path / "dir with space%" / "state.db"
    path.parent.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        open_as(str(path), ME, migrate=False)
    assert not path.exists()
    open_as(str(path), ME).close()
    open_as(str(path), ME, migrate=False).close()


def test_list_sessions_on_an_empty_store(db: str) -> None:
    with open_as(db, ME) as s:
        assert s.list_sessions() == []


def test_list_sessions_excludes_forgotten_newest_first(db: str) -> None:
    with open_as(db, ME) as s:
        with s.transaction() as conn:
            for sid, created, forgotten in [
                ("cs-aaaaaaaa", "2026-01-01T00:00:00.000000Z", None),
                ("cs-bbbbbbbb", "2026-01-02T00:00:00.000000Z", None),
                ("cs-cccccccc", "2026-01-03T00:00:00.000000Z", "2026-01-04T00:00:00.000000Z"),
            ]:
                conn.execute(
                    "INSERT INTO sessions (csession_id, created_at, launch_kind, current_run, "
                    "state, updated_at, forgotten_at) VALUES (?, ?, 'k', 1, 'idle', ?, ?)",
                    (sid, created, created, forgotten),
                )
        assert [r["csession_id"] for r in s.list_sessions()] == ["cs-bbbbbbbb", "cs-aaaaaaaa"]


# Migrations


def make_migrations(
    log: list[tuple[str, object]], versions: list[tuple[int, int]]
) -> list[Migration]:
    def step(version: tuple[int, int]) -> Migration:
        def apply(conn: sqlite3.Connection) -> None:
            lease = conn.execute(
                "SELECT holder_pid, holder_start FROM leases WHERE name = 'migrate'"
            ).fetchone()
            log.append((store.format_version(version), lease))
            conn.execute(f"CREATE TABLE t_{version[0]}_{version[1]} (x)")

        return Migration(version, apply)

    return [step(v) for v in versions]


def test_migrations_apply_in_order_under_the_migrate_lease(db: str) -> None:
    log: list[tuple[str, object]] = []
    migrations = make_migrations(log, [(1, 0), (1, 1), (2, 0)])
    with open_as(db, ME, migrations=migrations) as s:
        assert s.schema_version() == (2, 0)
    assert log == [
        ("1.0", (ME.pid, ME.start)),
        ("1.1", (ME.pid, ME.start)),
        ("2.0", (ME.pid, ME.start)),
    ]


def test_only_pending_migrations_run_on_an_older_database(db: str) -> None:
    log: list[tuple[str, object]] = []
    open_as(db, ME, migrations=make_migrations(log, [(1, 0)])).close()
    log.clear()
    with open_as(db, ME, migrations=make_migrations(log, [(1, 0), (1, 1), (1, 2)])) as s:
        assert s.schema_version() == (1, 2)
    assert [version for version, _ in log] == ["1.1", "1.2"]


def test_a_failed_migration_keeps_the_earlier_steps_and_releases_the_lease(db: str) -> None:
    log: list[tuple[str, object]] = []

    def broken(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("boom")

    migrations = [*make_migrations(log, [(1, 0)]), Migration((1, 1), broken)]
    with pytest.raises(RuntimeError):
        open_as(db, ME, migrations=migrations)
    with open_as(db, ME, migrations=make_migrations(log, [(1, 0)])) as s:
        assert s.schema_version() == (1, 0)
        assert s.lease_holder(store.MIGRATE_LEASE) is None
    assert "half_done" not in tables(db)


def test_migration_waits_for_a_live_holder_of_the_migrate_lease(db: str) -> None:
    log: list[tuple[str, object]] = []
    open_as(db, ME, migrations=make_migrations(log, [(1, 0)])).close()
    with open_as(db, OTHER, migrations=make_migrations(log, [(1, 0)])) as other:
        assert other.take_lease(store.MIGRATE_LEASE)
    with pytest.raises(Exception, match="timed out waiting"):
        open_as(db, ME, migrations=make_migrations(log, [(1, 0), (1, 1)]), migrate_wait=0.2)
    assert [version for version, _ in log] == ["1.0"]


def test_migration_takes_over_a_dead_holders_migrate_lease(db: str) -> None:
    log: list[tuple[str, object]] = []
    open_as(db, ME, migrations=make_migrations(log, [(1, 0)])).close()
    with open_as(db, OTHER, migrations=make_migrations(log, [(1, 0)])) as other:
        assert other.take_lease(store.MIGRATE_LEASE)
    with open_as(
        db, ME, alive=FakeProcesses(ME), migrations=make_migrations(log, [(1, 0), (1, 1)])
    ) as s:
        assert s.schema_version() == (1, 1)


@pytest.mark.parametrize("bad", [[], [(0, 0)], [(1, 1), (1, 0)], [(1, 0), (1, 0)]])
def test_migration_list_must_increase(db: str, bad: list[tuple[int, int]]) -> None:
    with pytest.raises(ValueError):
        open_as(db, ME, migrations=make_migrations([], bad))


# Schema-version check


@pytest.mark.parametrize("newer", ["1.1", "2.0", "99.7"])
def test_newer_schema_is_refused_without_writing(db: str, newer: str) -> None:
    open_as(db, ME, migrations=make_migrations([], [(1, 0)])).close()
    set_version(db, newer)
    before = Path(db).read_bytes()
    with pytest.raises(SchemaNewerError) as exc:
        open_as(db, ME, migrations=make_migrations([], [(1, 0)]))
    assert exc.value.code == "schema_newer"
    assert exc.value.exit_code == 12
    assert newer in exc.value.message
    assert Path(db).read_bytes() == before


def test_same_schema_opens_without_taking_the_migrate_lease(db: str) -> None:
    open_as(db, ME).close()
    with open_as(db, OTHER) as other:
        assert other.take_lease(store.MIGRATE_LEASE)
    # A live holder of `migrate` does not block an up-to-date open.
    open_as(db, ME, migrate_wait=0).close()


def test_invalid_schema_version_is_an_internal_error(db: str) -> None:
    open_as(db, ME).close()
    set_version(db, "one")
    with pytest.raises(Exception, match="invalid schema_version"):
        open_as(db, ME)


# Leases


def test_take_a_free_lease_and_expiry_is_ttl_from_now(db: str) -> None:
    with open_as(db, ME) as s:
        assert s.take_lease("reconcile")
        assert s.lease_holder("reconcile") == (ME, T0 + LEASE_TTL)


def test_a_live_holder_keeps_its_lease(db: str) -> None:
    with open_as(db, ME) as me, open_as(db, OTHER) as other:
        assert other.take_lease("reconcile")
        assert not me.take_lease("reconcile")
        assert me.lease_holder("reconcile") == (OTHER, T0 + LEASE_TTL)


def test_taking_your_own_lease_again_renews_it(db: str) -> None:
    clock = FakeClock()
    with open_as(db, ME, clock=clock) as s:
        assert s.take_lease("reconcile")
        clock.now = T0 + timedelta(seconds=30)
        assert s.take_lease("reconcile")
        assert s.lease_holder("reconcile") == (ME, clock.now + LEASE_TTL)


def test_renew_extends_the_lease(db: str) -> None:
    clock = FakeClock()
    with open_as(db, ME, clock=clock) as s:
        s.take_lease("delivery:cs-aaaaaaaa")
        clock.now = T0 + store.LEASE_RENEW_INTERVAL
        assert s.renew_lease("delivery:cs-aaaaaaaa")
        assert s.lease_holder("delivery:cs-aaaaaaaa") == (ME, clock.now + LEASE_TTL)


def test_renew_fails_after_a_takeover(db: str) -> None:
    clock = FakeClock()
    with open_as(db, ME, clock=clock) as me, open_as(db, OTHER, clock=clock) as other:
        assert me.take_lease("reconcile")
        clock.now = T0 + LEASE_TTL
        assert other.take_lease("reconcile")
        assert not me.renew_lease("reconcile")
        assert me.lease_holder("reconcile") == (OTHER, clock.now + LEASE_TTL)


def test_renew_fails_for_a_lease_never_taken(db: str) -> None:
    with open_as(db, ME) as s:
        assert not s.renew_lease("reconcile")


def test_an_expired_lease_of_a_live_holder_can_be_taken(db: str) -> None:
    clock = FakeClock()
    with open_as(db, ME, clock=clock) as me, open_as(db, OTHER, clock=clock) as other:
        assert other.take_lease("reconcile")
        clock.now = T0 + LEASE_TTL - timedelta(microseconds=1)
        assert not me.take_lease("reconcile")
        clock.now = T0 + LEASE_TTL
        assert me.take_lease("reconcile")


def test_a_dead_holders_lease_is_taken_over_at_once(db: str) -> None:
    alive = FakeProcesses(ME)  # OTHER is dead
    with open_as(db, OTHER) as other, open_as(db, ME, alive=alive) as me:
        assert other.take_lease("reconcile")
        assert me.take_lease("reconcile")
        assert alive.calls == [(OTHER.pid, OTHER.start)]
        assert me.lease_holder("reconcile") == (ME, T0 + LEASE_TTL)


def test_a_reused_pid_with_another_start_time_counts_as_dead(db: str) -> None:
    reused = Holder(pid=OTHER.pid, start="999")
    alive = FakeProcesses(ME, reused)  # same PID, different process
    with open_as(db, OTHER) as other, open_as(db, ME, alive=alive) as me:
        assert other.take_lease("reconcile")
        assert me.take_lease("reconcile")


def test_release_frees_only_your_own_lease(db: str) -> None:
    with open_as(db, ME) as me, open_as(db, OTHER) as other:
        assert other.take_lease("reconcile")
        me.release_lease("reconcile")
        assert me.lease_holder("reconcile") is not None
        other.release_lease("reconcile")
        assert me.lease_holder("reconcile") is None
        assert me.take_lease("reconcile")


def test_leases_are_independent_by_name(db: str) -> None:
    with open_as(db, ME) as me, open_as(db, OTHER) as other:
        assert other.take_lease("delivery:cs-aaaaaaaa")
        assert me.take_lease("delivery:cs-bbbbbbbb")


def test_default_liveness_checks_the_pid() -> None:
    assert store.pid_alive(os.getpid(), "")
    assert not store.pid_alive(0, "")
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert not store.pid_alive(int(proc.stdout), "")


def test_default_holder_is_this_process() -> None:
    assert store.current_holder().pid == os.getpid()


# Concurrency: BEGIN IMMEDIATE with a busy timeout


def make_counter(db: str) -> None:
    with open_as(db, ME) as s, s.transaction() as conn:
        conn.execute("CREATE TABLE counter (n INTEGER NOT NULL)")
        conn.execute("INSERT INTO counter VALUES (0)")


def counter(db: str) -> int:
    with open_as(db, ME) as s:
        return s.conn.execute("SELECT n FROM counter").fetchone()[0]


WRITER = """
import sys, time
from claude_session.store import Store
path, rounds = sys.argv[1], int(sys.argv[2])
with Store.open(path) as s:
    for _ in range(rounds):
        with s.transaction() as conn:
            (n,) = conn.execute("SELECT n FROM counter").fetchone()
            time.sleep(0.001)  # widen the read-modify-write window
            conn.execute("UPDATE counter SET n = ?", (n + 1,))
"""


def test_concurrent_writer_processes_lose_no_updates(db: str) -> None:
    make_counter(db)
    writers, rounds = 4, 40
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, db, str(rounds)], stderr=subprocess.PIPE, text=True
        )
        for _ in range(writers)
    ]
    errors = [p.communicate(timeout=120)[1] for p in procs]
    assert [p.returncode for p in procs] == [0] * writers, errors
    assert counter(db) == writers * rounds


def test_a_writer_waits_for_the_lock_within_the_busy_timeout(db: str) -> None:
    make_counter(db)
    holding = threading.Event()

    def hold() -> None:
        with open_as(db, OTHER) as other, other.transaction() as conn:
            conn.execute("UPDATE counter SET n = n + 1")
            holding.set()
            time.sleep(0.5)

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(10)
    with open_as(db, ME, busy_timeout=5) as me:
        started = time.monotonic()
        with me.transaction() as conn:
            conn.execute("UPDATE counter SET n = n + 1")
        waited = time.monotonic() - started
    thread.join()
    assert waited >= 0.3
    assert counter(db) == 2


def test_a_writer_fails_after_the_busy_timeout(db: str) -> None:
    make_counter(db)
    holding, done = threading.Event(), threading.Event()

    def hold() -> None:
        with open_as(db, OTHER) as other, other.transaction():
            holding.set()
            done.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(10)
    try:
        with open_as(db, ME, busy_timeout=0.2) as me:
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                with me.transaction():
                    pass  # pragma: no cover
            assert time.monotonic() - started >= 0.15
    finally:
        done.set()
        thread.join()


def test_a_failed_transaction_rolls_back(db: str) -> None:
    make_counter(db)
    with open_as(db, ME) as s:
        with pytest.raises(RuntimeError):
            with s.transaction() as conn:
                conn.execute("UPDATE counter SET n = 5")
                raise RuntimeError("boom")
    assert counter(db) == 0


OPENER = """
import sys
from claude_session.store import Store
with Store.open(sys.argv[1]) as s:
    print(".".join(map(str, s.schema_version())))
"""


def test_concurrent_first_opens_migrate_once(db: str) -> None:
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", OPENER, db],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    results = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0] * 6, [err for _, err in results]
    current = store.format_version(store.code_version())
    assert {out.strip() for out, _ in results} == {current}
    assert tables(db) == DESIGN_TABLES
