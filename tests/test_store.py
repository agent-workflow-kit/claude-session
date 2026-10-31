"""state.db: WAL, migrations, leases and the schema-version check (design.md, Data model)."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_session import store
from claude_session.errors import FailedError, SchemaNewerError
from claude_session.store import LEASE_TTL, Holder, Migration, Store

T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
ME = Holder(pid=1000, start="100")
OTHER = Holder(pid=2000, start="200")
# design.md, Data model: every table and its columns.
DESIGN_COLUMNS = {
    "sessions": (
        "csession_id created_at launch_kind csession_dir private_dir tmp_dir forked_from "
        "tainted tainted_at tainted_by_run current_run claude_session_id state state_since "
        "activity needs_input_reason git_dir updated_at forgotten_at"
    ),
    "runs": (
        "csession_id run_no created_at run_kind reads_third_party mode cwd repo model skill "
        "has_prompt spec_json resolved_json config_hash invocation_json permission_profile "
        "permission_profile_snapshot sandbox_active decision_ids_json escalated_hooks "
        "warnings_json message hook_interpreter config_path expires_at started_at "
        "session_started_at ended_at stop_requested_at pending_cause wrapper_pid "
        "wrapper_start child_pid child_start outbox_dev_ino artifacts_dev_ino secret_sha256 "
        "backstop_used report_status report_implicit report_summary report_reason "
        "artifact_name artifact_path artifact_sha256 artifact_size artifact_ingest_seq "
        "reported_at phase outcome end_reason exit_code cost_usd num_turns is_error "
        "session_end_reason outcome_file outcome_file_written_at"
    ),
    "claude_ids": "csession_id claude_session_id run_no source transcript_path seen_at",
    "labels": "csession_id key value",
    "events": (
        "id csession_id run_no event_ts from_state to_state from_activity to_activity "
        "needs_input_reason cause final labels_json delivered_at"
    ),
    "requests": "request_id csession_id run_no result ingested_at",
    "deleted_ids": "csession_id deleted_at",
    "leases": "name holder_pid holder_start holder_token expires_at",
    "meta": "key value",
}
DESIGN_TABLES = set(DESIGN_COLUMNS)


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
    migrate: bool = True,
    busy_timeout: float = store.BUSY_TIMEOUT,
    migrations: list[Migration] | None = None,
    migrate_wait: float = store.MIGRATE_WAIT,
) -> Store:
    return Store.open(
        path,
        holder=holder,
        liveness=alive or FakeProcesses(ME, OTHER),
        clock=clock or FakeClock(),
        migrate=migrate,
        busy_timeout=busy_timeout,
        migrations=migrations,
        migrate_wait=migrate_wait,
    )


def tables(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {name for (name,) in rows if not name.startswith("sqlite_")}


def columns(path: str, table: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def journal_mode(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


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


def mode(path: str | Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_open_creates_a_private_wal_database(db: str) -> None:
    old = os.umask(0o177)  # mkdir(0o700) alone would give 0o600
    try:
        open_as(db, ME).close()
    finally:
        os.umask(old)
    assert mode(os.path.dirname(db)) == 0o700
    assert mode(db) == 0o600
    assert journal_mode(db) == "wal"


def test_missing_parents_are_created_private_whatever_the_umask(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "claude-session" / "state.db"
    old = os.umask(0)
    try:
        open_as(str(path), ME).close()
    finally:
        os.umask(old)
    for directory in (tmp_path / "a", tmp_path / "a" / "b", path.parent):
        assert mode(directory) == 0o700, directory


def test_a_loose_existing_state_dir_is_tightened(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    os.chmod(root, 0o777)
    open_as(str(root / "state.db"), ME).close()
    assert mode(root) == 0o700


def test_a_symlinked_state_dir_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "state").symlink_to(target)
    with pytest.raises(FailedError) as exc:
        open_as(str(tmp_path / "state" / "state.db"), ME)
    assert exc.value.exit_code == 12
    assert str(tmp_path / "state") in (exc.value.detail or "")
    assert list(target.iterdir()) == []


def test_a_dangling_symlink_db_is_refused_and_its_target_not_created(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / "target.db"
    (root / "state.db").symlink_to(target)
    with pytest.raises(FailedError) as exc:
        open_as(str(root / "state.db"), ME)
    assert "symbolic link" in (exc.value.detail or "")
    assert not target.exists()


def test_a_symlink_to_an_existing_db_is_refused_and_its_target_unchanged(
    tmp_path: Path,
) -> None:
    target = str(tmp_path / "real" / "state.db")
    open_as(target, ME).close()
    before = Path(target).read_bytes()
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    (root / "state.db").symlink_to(target)
    with pytest.raises(FailedError):
        open_as(str(root / "state.db"), ME)
    with pytest.raises(FailedError):
        open_as(str(root / "state.db"), ME, migrate=False)
    assert Path(target).read_bytes() == before


def test_a_hard_linked_db_is_refused(db: str, tmp_path: Path) -> None:
    open_as(db, ME).close()
    os.link(db, tmp_path / "elsewhere.db")
    with pytest.raises(FailedError, match="unsafe") as exc:
        open_as(db, ME)
    assert "hard links" in (exc.value.detail or "")
    with pytest.raises(FailedError):
        open_as(db, ME, migrate=False)


def test_a_loose_db_is_tightened_to_0600(db: str) -> None:
    open_as(db, ME).close()
    os.chmod(db, 0o644)
    open_as(db, ME).close()
    assert mode(db) == 0o600


def test_loose_wal_and_shm_files_are_tightened_to_0600(db: str) -> None:
    open_as(db, ME).close()
    reader = sqlite3.connect(db, isolation_level=None)  # keeps -wal and -shm in place
    try:
        reader.execute("SELECT * FROM meta").fetchall()
        for sidecar in (db + "-wal", db + "-shm"):
            os.chmod(sidecar, 0o644)
        open_as(db, ME).close()
        assert (mode(db + "-wal"), mode(db + "-shm")) == (0o600, 0o600)
    finally:
        reader.close()


def test_the_hook_path_checks_but_never_changes_modes(db: str) -> None:
    open_as(db, ME).close()
    root = os.path.dirname(db)
    os.chmod(root, 0o755)
    os.chmod(db, 0o644)
    open_as(db, ME, migrate=False).close()
    assert (mode(root), mode(db)) == (0o755, 0o644)


def test_a_fifo_in_place_of_the_db_is_refused_without_blocking(db: str) -> None:
    os.makedirs(os.path.dirname(db), mode=0o700)
    os.mkfifo(db, 0o600)
    errors: list[BaseException] = []

    def attempt() -> None:
        for migrate in (True, False):
            try:
                open_as(db, ME, migrate=migrate).close()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "open blocked on a FIFO"
    assert [type(e) for e in errors] == [FailedError, FailedError]
    assert all("not a regular file" in (e.detail or "") for e in errors)  # type: ignore[attr-defined]


def test_a_state_root_with_two_leading_slashes_opens(tmp_path: Path) -> None:
    path = "/" + str(tmp_path / "state" / "state.db")  # "//tmp/...": not a URI authority
    assert path.startswith("//")
    open_as(path, ME).close()
    with open_as(path, ME, migrate=False) as s:
        assert s.schema_version() == store.code_version()


@pytest.mark.parametrize("name", ["a?mode=ro", "c%41%2F", "b#frag"])
def test_a_state_root_with_uri_special_characters_opens(tmp_path: Path, name: str) -> None:
    path = str(tmp_path / name / "state.db")
    open_as(path, ME).close()
    with open_as(path, ME, migrate=False) as s:
        assert s.schema_version() == store.code_version()
    assert os.listdir(tmp_path) == [name]  # no file created under a misread path
    assert os.path.isfile(path)


OPEN_AND_CLOSE = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("SELECT count(*) FROM t").fetchone()
conn.close()
"""
INSERT_ROWS = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
for i in range(int(sys.argv[2])):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO t VALUES (?)", (1000 + i,))
    conn.execute("COMMIT")
conn.close()
"""


def test_opening_again_keeps_the_sqlite_locks_of_live_connections(db: str) -> None:
    """POSIX drops all of a process's locks on a file when any fd on it is closed; open
    must not do that, or another process deletes the WAL under live connections."""
    with open_as(db, ME) as s, s.transaction() as conn:
        conn.execute("CREATE TABLE t (n INTEGER)")
    first = open_as(db, ME)
    raw = sqlite3.connect(db, isolation_level=None)  # e.g. a reader elsewhere in the process
    try:
        raw.execute("SELECT count(*) FROM t").fetchone()
        open_as(db, ME).close()
        open_as(db, ME, migrate=False).close()
        subprocess.run([sys.executable, "-c", OPEN_AND_CLOSE, db], check=True, timeout=60)
        assert os.path.exists(db + "-wal"), "another process deleted the live WAL"
        rows = 50
        writer = subprocess.Popen([sys.executable, "-c", INSERT_ROWS, db, str(rows)])
        for i in range(rows):
            with first.transaction() as conn:
                conn.execute("INSERT INTO t VALUES (?)", (i,))
        assert writer.wait(timeout=60) == 0
    finally:
        raw.close()
        first.close()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM t").fetchone() == (2 * rows,)
    finally:
        conn.close()


def test_a_thread_cannot_connect_while_another_holds_the_create_descriptor(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the descriptor that created state.db drops every POSIX lock this process
    holds on it; no other thread may connect to the new database before that close."""
    created, go, second_done = threading.Event(), threading.Event(), threading.Event()
    errors: list[BaseException] = []
    fchmod = os.fchmod

    def paused_fchmod(fd: int, mode: int) -> None:
        if threading.current_thread().name == "creator":
            created.set()
            go.wait(10)
        fchmod(fd, mode)

    def run(done: threading.Event | None) -> None:
        try:
            open_as(db, ME).close()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            if done is not None:
                done.set()

    monkeypatch.setattr(os, "fchmod", paused_fchmod)
    creator = threading.Thread(target=run, args=(None,), name="creator")
    second = threading.Thread(target=run, args=(second_done,), name="second")
    creator.start()
    try:
        assert created.wait(10)
        second.start()
        connected_early = second_done.wait(0.5)
    finally:
        go.set()
        creator.join(10)
        if second.ident is not None:
            second.join(10)
    assert not connected_early, "opened while the create descriptor was open"
    assert not creator.is_alive() and not second.is_alive()
    assert errors == []


def test_a_slow_open_of_one_database_does_not_stall_an_open_of_another(tmp_path: Path) -> None:
    """The process-wide open lock covers only creating the file, not the database waits."""
    slow_db, other_db = str(tmp_path / "a" / "state.db"), str(tmp_path / "b" / "state.db")
    open_as(slow_db, ME).close()
    open_as(other_db, ME).close()
    setup = sqlite3.connect(slow_db, isolation_level=None)
    setup.execute("PRAGMA journal_mode=DELETE")  # an exclusive lock then blocks readers
    setup.close()
    locker = sqlite3.connect(slow_db, isolation_level=None, check_same_thread=False)
    locker.execute("BEGIN EXCLUSIVE")
    waiting = threading.Event()

    def slow_open() -> None:
        waiting.set()
        with suppress(FailedError):
            open_as(slow_db, ME, busy_timeout=2.0).close()

    thread = threading.Thread(target=slow_open)
    try:
        thread.start()
        assert waiting.wait(10)
        time.sleep(0.2)  # let it reach the busy wait on the slow database
        started = time.monotonic()
        open_as(other_db, ME, busy_timeout=2.0).close()
        elapsed = time.monotonic() - started
    finally:
        locker.execute("ROLLBACK")
        locker.close()
        thread.join(10)
    assert not thread.is_alive()
    assert elapsed < 1.0, f"the unrelated open took {elapsed:.2f} s"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_child_forked_while_the_open_lock_is_held_can_open(tmp_path: Path) -> None:
    """A fork while another thread holds the open lock must not leave the child's copy
    locked forever."""
    child_db = str(tmp_path / "child" / "state.db")
    store._OPEN_LOCK.acquire()  # pyright: ignore[reportPrivateUsage]
    try:
        pid = os.fork()
        if pid == 0:  # child: never return into pytest
            status = 1
            try:
                signal.alarm(5)  # a hang ends the child by SIGALRM
                Store.open(child_db).close()
                status = 0
            finally:
                os._exit(status)
    finally:
        store._OPEN_LOCK.release()  # pyright: ignore[reportPrivateUsage]
    deadline = time.monotonic() + 15
    while True:
        done, wait_status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)  # the child forked above, still unreaped
            os.waitpid(pid, 0)
            pytest.fail("the forked child did not finish")
        time.sleep(0.05)
    assert not os.WIFSIGNALED(wait_status), "the child hung on the open lock"
    assert os.WEXITSTATUS(wait_status) == 0


def test_a_db_that_cannot_be_opened_is_failed(db: str) -> None:
    os.makedirs(db)  # a directory where state.db should be
    with pytest.raises(FailedError) as exc:
        open_as(db, ME)
    assert db in (exc.value.detail or "")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 0 file")
def test_an_unreadable_db_is_failed_on_the_hook_path(db: str) -> None:
    open_as(db, ME).close()
    os.chmod(db, 0)
    try:
        with pytest.raises(FailedError) as exc:
            open_as(db, ME, migrate=False)
        assert db in (exc.value.detail or "")
    finally:
        os.chmod(db, 0o600)


def test_a_corrupt_db_is_failed(db: str) -> None:
    os.makedirs(os.path.dirname(db), mode=0o700)
    Path(db).write_bytes(b"this is not a database at all" * 200)
    os.chmod(db, 0o600)
    with pytest.raises(FailedError) as exc:
        open_as(db, ME)
    assert db in (exc.value.detail or "")


def test_a_foreign_database_is_refused_and_left_untouched(db: str) -> None:
    os.makedirs(os.path.dirname(db), mode=0o700)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    os.chmod(db, 0o600)
    before = Path(db).read_bytes()
    with pytest.raises(FailedError, match="not a claude-session database"):
        open_as(db, ME)
    with pytest.raises(FailedError, match="not a claude-session database"):
        open_as(db, ME, migrate=False)
    assert Path(db).read_bytes() == before
    assert journal_mode(db) == "delete"


def test_an_empty_file_is_initialized(db: str) -> None:
    os.makedirs(os.path.dirname(db), mode=0o700)
    Path(db).touch(mode=0o600)
    with open_as(db, ME) as s:
        assert s.schema_version() == store.code_version()
        assert s.conn.execute("PRAGMA application_id").fetchone() == (store.APPLICATION_ID,)


def test_wal_switch_waits_for_a_reader_within_the_busy_timeout(db: str) -> None:
    open_as(db, ME).close()
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    reading = threading.Event()

    def read() -> None:
        reader = sqlite3.connect(db, isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM meta").fetchall()
        reading.set()
        time.sleep(0.3)
        reader.execute("COMMIT")
        reader.close()

    conn.close()
    thread = threading.Thread(target=read)
    thread.start()
    reading.wait(10)
    open_as(db, ME, busy_timeout=5).close()
    thread.join()
    assert journal_mode(db) == "wal"


def test_fresh_database_has_the_design_tables_and_current_version(db: str) -> None:
    with open_as(db, ME) as s:
        assert s.schema_version() == store.code_version()
    assert tables(db) == DESIGN_TABLES


@pytest.mark.parametrize("table", sorted(DESIGN_COLUMNS))
def test_fresh_database_tables_have_the_design_columns(db: str, table: str) -> None:
    open_as(db, ME).close()
    assert columns(db, table) == set(DESIGN_COLUMNS[table].split())


def test_migrate_lease_is_released_after_migration(db: str) -> None:
    with open_as(db, ME) as s:
        assert s.lease_holder(store.MIGRATE_LEASE) is None


def test_open_without_migrate_accepts_a_newer_minor_schema(db: str) -> None:
    open_as(db, ME).close()
    major, minor = store.code_version()
    set_version(db, f"{major}.{minor + 3}")
    with open_as(db, ME, migrate=False) as s:  # hook-event: never migrates
        assert s.schema_version() == (major, minor + 3)


@pytest.mark.parametrize("version", ["99.0", "0.5"])
def test_open_without_migrate_refuses_another_major_schema(db: str, version: str) -> None:
    open_as(db, ME).close()
    set_version(db, version)
    with pytest.raises(FailedError, match="schema"):
        open_as(db, ME, migrate=False)


def test_open_without_migrate_refuses_an_unmigrated_database(db: str) -> None:
    os.makedirs(os.path.dirname(db), mode=0o700)
    Path(db).touch(mode=0o600)
    with pytest.raises(FailedError):
        open_as(db, ME, migrate=False)
    assert Path(db).read_bytes() == b""


def test_open_without_migrate_never_creates_a_database(tmp_path: Path) -> None:
    path = tmp_path / "dir with space%" / "state.db"
    path.parent.mkdir(mode=0o700)
    with pytest.raises(FailedError):
        open_as(str(path), ME, migrate=False)
    assert not path.exists()
    open_as(str(path), ME).close()
    open_as(str(path), ME, migrate=False).close()


def test_open_without_migrate_refuses_a_bootstrapped_but_unmigrated_database(db: str) -> None:
    """A crash between the bootstrap and the first migration leaves application_id set and
    schema 0.0; the hook path must not use it (a 0.x code version included)."""

    def crash(conn: sqlite3.Connection) -> None:
        raise RuntimeError("crash")

    migrations = [Migration((0, 1), crash)]
    with pytest.raises(RuntimeError):
        open_as(db, ME, migrations=migrations)
    with pytest.raises(FailedError, match="schema 0.0"):
        open_as(db, ME, migrate=False, migrations=migrations)


def test_list_sessions_on_an_empty_store(db: str) -> None:
    with open_as(db, ME) as s:
        assert s.list_sessions() == []


def test_list_sessions_excludes_forgotten_most_recently_updated_first(db: str) -> None:
    day = "2026-01-0{}T00:00:00.000000Z".format
    with open_as(db, ME) as s:
        with s.transaction() as conn:
            # cs-dddddddd first: only the csession_id tie-break puts it before cs-bbbbbbbb.
            for sid, created, updated, forgotten in [
                ("cs-aaaaaaaa", day(1), day(5), None),
                ("cs-dddddddd", day(4), day(3), None),
                ("cs-bbbbbbbb", day(2), day(3), None),
                ("cs-cccccccc", day(3), day(9), day(9)),
            ]:
                conn.execute(
                    "INSERT INTO sessions (csession_id, created_at, launch_kind, current_run, "
                    "state, updated_at, forgotten_at) VALUES (?, ?, 'k', 1, 'idle', ?, ?)",
                    (sid, created, updated, forgotten),
                )
        assert [r["csession_id"] for r in s.list_sessions()] == [
            "cs-aaaaaaaa",
            "cs-dddddddd",
            "cs-bbbbbbbb",
        ]


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
    with pytest.raises(FailedError, match="timed out waiting") as exc:
        open_as(db, ME, migrations=make_migrations(log, [(1, 0), (1, 1)]), migrate_wait=0.2)
    assert db in (exc.value.detail or "")
    assert [version for version, _ in log] == ["1.0"]


def hold_write_lock(db: str, seconds: float) -> tuple[threading.Thread, threading.Event]:
    """Hold a write transaction on ``db`` in a thread for ``seconds``; returns the thread
    and an event set once the lock is held."""
    holding = threading.Event()

    def hold() -> None:
        conn = sqlite3.connect(db, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        holding.set()
        time.sleep(seconds)
        conn.execute("ROLLBACK")
        conn.close()

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(10)
    return thread, holding


def test_migration_wait_retries_while_the_database_is_busy(db: str) -> None:
    log: list[tuple[str, object]] = []
    open_as(db, ME, migrations=make_migrations(log, [(1, 0)])).close()
    thread, _ = hold_write_lock(db, 0.4)
    try:
        with open_as(
            db,
            ME,
            migrations=make_migrations(log, [(1, 0), (1, 1)]),
            busy_timeout=0.05,
            migrate_wait=10,
        ) as s:
            assert s.schema_version() == (1, 1)
    finally:
        thread.join()


def test_migration_wait_on_a_busy_database_times_out_as_failed(db: str) -> None:
    log: list[tuple[str, object]] = []
    open_as(db, ME, migrations=make_migrations(log, [(1, 0)])).close()
    thread, _ = hold_write_lock(db, 1.0)
    try:
        with pytest.raises(FailedError, match="timed out waiting"):
            open_as(
                db,
                ME,
                migrations=make_migrations(log, [(1, 0), (1, 1)]),
                busy_timeout=0.05,
                migrate_wait=0.3,
            )
    finally:
        thread.join()


def test_a_migrator_that_loses_its_lease_stops_before_the_next_step(db: str) -> None:
    log: list[str] = []

    def stolen(conn: sqlite3.Connection) -> None:
        log.append("1.0")
        conn.execute(
            "UPDATE leases SET holder_pid = ?, holder_start = ?, holder_token = 'x' "
            "WHERE name = 'migrate'",
            (OTHER.pid, OTHER.start),
        )

    migrations = [Migration((1, 0), stolen), Migration((1, 1), lambda c: log.append("1.1"))]
    with pytest.raises(FailedError, match="lost the migrate lease") as exc:
        open_as(db, ME, migrations=migrations)
    assert exc.value.detail == f"{db}: migrate lease taken over"
    assert log == ["1.0"]


def grab_write_lock(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")
    return conn


@pytest.mark.parametrize("when", ["before_renew", "after_renew"])
def test_a_busy_database_between_migration_steps_is_failed(
    db: str, when: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_as(db, ME, migrations=make_migrations([], [(1, 0)])).close()
    locks: list[sqlite3.Connection] = []
    renew = Store.renew_lease

    def renew_then_lock(self: Store, name: str) -> bool:
        if when == "before_renew":
            locks.append(grab_write_lock(db))
        renewed = renew(self, name)
        if when == "after_renew":
            locks.append(grab_write_lock(db))
        return renewed

    monkeypatch.setattr(Store, "renew_lease", renew_then_lock)
    try:
        with pytest.raises(FailedError) as exc:
            open_as(
                db, ME, migrations=make_migrations([], [(1, 0), (1, 1), (1, 2)]), busy_timeout=0.05
            )
        assert exc.value.detail is not None
        assert exc.value.detail.startswith(f"{db}: ")
        assert "locked" in exc.value.detail
    finally:
        for lock in locks:
            lock.close()


def test_a_failing_release_does_not_mask_the_migration_error(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(conn: sqlite3.Connection) -> None:
        raise RuntimeError("boom")

    def release(self: Store, name: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "release_lease", release)
    with pytest.raises(RuntimeError, match="boom"):
        open_as(db, ME, migrations=[Migration((1, 0), broken)])


def test_the_migration_wait_does_not_overshoot_by_a_busy_timeout(db: str) -> None:
    open_as(db, ME, migrations=make_migrations([], [(1, 0)])).close()
    lock = grab_write_lock(db)
    try:
        started = time.monotonic()
        with pytest.raises(FailedError, match="timed out waiting"):
            open_as(
                db,
                ME,
                migrations=make_migrations([], [(1, 0), (1, 1)]),
                busy_timeout=2,
                migrate_wait=0.5,
            )
        assert time.monotonic() - started < 1.5
    finally:
        lock.close()


def test_the_lease_step_of_the_migration_wait_gets_only_the_time_left(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrap uses up most of the wait; taking the lease must not wait a full
    ``migrate_wait`` again."""
    open_as(db, ME, migrations=make_migrations([], [(1, 0)])).close()
    locks: list[sqlite3.Connection] = []
    bootstrap = Store._bootstrap

    def slow_bootstrap(self: Store) -> None:
        if not locks:
            time.sleep(0.9)  # e.g. waiting for another writer
            bootstrap(self)
            locks.append(grab_write_lock(db))  # the next writer holds the lock throughout
        else:
            bootstrap(self)

    monkeypatch.setattr(Store, "_bootstrap", slow_bootstrap)
    try:
        started = time.monotonic()
        with pytest.raises(FailedError, match="timed out waiting"):
            open_as(
                db,
                ME,
                migrations=make_migrations([], [(1, 0), (1, 1)]),
                busy_timeout=5,
                migrate_wait=1.0,
            )
        assert time.monotonic() - started < 1.5
    finally:
        for lock in locks:
            lock.close()


def test_the_busy_timeout_is_restored_after_the_migration_wait(db: str) -> None:
    open_as(db, ME, migrations=make_migrations([], [(1, 0)])).close()
    with open_as(
        db, ME, migrations=make_migrations([], [(1, 0), (1, 1)]), busy_timeout=3, migrate_wait=1
    ) as s:
        assert s.conn.execute("PRAGMA busy_timeout").fetchone() == (3000,)


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


@pytest.mark.parametrize("version", ["one", "1.\u00b2", "1.0\n", " 1.0"])
@pytest.mark.parametrize("migrate", [True, False])
def test_an_invalid_schema_version_is_failed(db: str, version: str, migrate: bool) -> None:
    open_as(db, ME).close()
    set_version(db, version)
    with pytest.raises(FailedError, match="cannot open the state database") as exc:
        open_as(db, ME, migrate=migrate)
    assert exc.value.detail == f"{db}: invalid schema_version {version!r}"


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


def test_two_stores_in_one_process_do_not_share_a_lease(db: str) -> None:
    with open_as(db, ME) as a, open_as(db, ME) as b:
        assert a.take_lease("reconcile")
        assert not b.take_lease("reconcile")
        assert not b.renew_lease("reconcile")
        b.release_lease("reconcile")
        assert a.renew_lease("reconcile")
        a.release_lease("reconcile")
        assert b.take_lease("reconcile")


def test_a_lease_far_in_the_future_counts_as_expired(db: str) -> None:
    skewed = FakeClock(T0 + timedelta(hours=1))
    with open_as(db, OTHER, clock=skewed) as other, open_as(db, ME) as me:
        assert other.take_lease("reconcile")
        assert me.take_lease("reconcile")
        assert me.lease_holder("reconcile") == (ME, T0 + LEASE_TTL)


def test_a_lease_less_than_two_ttls_ahead_is_not_far_in_the_future(db: str) -> None:
    ahead = FakeClock(T0 + LEASE_TTL)  # expiry T0 + 2 TTL: a renewal racing a take
    with open_as(db, OTHER, clock=ahead) as other, open_as(db, ME) as me:
        assert other.take_lease("reconcile")
        assert not me.take_lease("reconcile")


def test_take_lease_reads_the_clock_after_getting_the_lock(db: str) -> None:
    clock = FakeClock()
    with open_as(db, OTHER, clock=clock) as other:
        assert other.take_lease("reconcile")  # expires T0 + TTL
    opened, go = threading.Event(), threading.Event()
    taken: list[bool] = []

    def take() -> None:
        with open_as(db, ME, clock=clock) as me:
            opened.set()
            go.wait(10)
            taken.append(me.take_lease("reconcile"))

    thread = threading.Thread(target=take)
    thread.start()
    assert opened.wait(10)
    lock = grab_write_lock(db)
    try:
        go.set()
        time.sleep(0.1)  # the taker now waits for the lock
        later = T0 + LEASE_TTL + timedelta(seconds=10)
        lock.execute(
            "UPDATE leases SET expires_at = ? WHERE name = 'reconcile'",
            (store.format_ts(later + LEASE_TTL),),
        )
        clock.now = later  # the holder renewed while the taker waited
        lock.execute("COMMIT")
    finally:
        lock.close()
        thread.join(10)
    assert taken == [False]


def test_a_missing_start_time_is_stored_as_unknown(db: str) -> None:
    unknown = Holder(pid=ME.pid, start=None)  # type: ignore[arg-type]  # start_time() -> None
    with open_as(db, unknown) as s:
        assert s.take_lease("reconcile")
        row = s.conn.execute("SELECT holder_start FROM leases WHERE name = 'reconcile'")
        assert row.fetchone() == ("",)


def test_a_lease_with_an_unknown_start_time_uses_the_pid_only(db: str) -> None:
    unknown = Holder(pid=os.getpid(), start="")
    alive = FakeProcesses(ME)  # would call (getpid, "") dead
    with open_as(db, unknown) as other, open_as(db, ME, alive=alive) as me:
        assert other.take_lease("reconcile")
        assert not me.take_lease("reconcile")
        assert alive.calls == []


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root may signal pid 1")
def test_default_liveness_counts_a_pid_it_may_not_signal_as_alive() -> None:
    assert store.pid_alive(1, "")  # EPERM: alive


def test_default_holder_is_this_process() -> None:
    assert store.current_holder().pid == os.getpid()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procinfo through /proc")
def test_default_liveness_compares_the_procinfo_start_time() -> None:
    me = store.start_time(os.getpid())
    assert me and ":" in me  # <boot_id>:<ticks>
    assert store.pid_alive(os.getpid(), me)
    assert not store.pid_alive(os.getpid(), me + "0")  # a reused PID: another start time
    assert store.current_holder() == Holder(pid=os.getpid(), start=me)


def test_default_liveness_counts_an_unanswerable_check_as_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_session.procinfo import ProcInfoError

    class Broken:
        def is_alive(self, pid: int, start: str) -> bool:
            raise ProcInfoError("cannot tell")

    monkeypatch.setattr(store, "_procinfo", Broken())
    assert store.pid_alive(12345, "x:1")


def test_the_holder_is_computed_only_when_a_lease_needs_it(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = store.current_holder

    def counting() -> Holder:
        calls.append(1)
        return real()

    store.Store.open(db).close()  # created and migrated (the migrate lease needs the holder)
    monkeypatch.setattr(store, "current_holder", counting)
    with store.Store.open(db, migrate=False) as s:
        assert calls == []  # the hook path opens a store and never takes a lease
        assert s.take_lease("reconcile")
        assert calls == [1]


def test_a_migration_is_logged(db: str) -> None:
    open_as(db, ME).close()
    log = Path(db).parent / "log.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["event"] for r in records] == ["migration"]
    open_as(db, ME).close()  # nothing to migrate: nothing logged
    assert len(log.read_text().splitlines()) == 1


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


def test_a_failed_savepoint_rolls_back_only_its_own_changes(db: str) -> None:
    make_counter(db)
    with open_as(db, ME) as s:
        with s.transaction() as conn:
            conn.execute("UPDATE counter SET n = 1")
            with pytest.raises(RuntimeError):
                with s.savepoint("inner"):
                    conn.execute("UPDATE counter SET n = 5")
                    raise RuntimeError("boom")
            with s.savepoint("kept"):
                conn.execute("UPDATE counter SET n = n + 1")
    assert counter(db) == 2


def test_an_auto_rolled_back_error_surfaces_unmasked(db: str) -> None:
    make_counter(db)
    with open_as(db, ME) as s:
        with s.transaction() as conn:
            conn.execute("CREATE TABLE u (k INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO u VALUES (1)")
        with pytest.raises(sqlite3.IntegrityError):
            with s.transaction() as conn:
                conn.execute("UPDATE counter SET n = 5")
                conn.execute("INSERT OR ROLLBACK INTO u VALUES (1)")
        with s.transaction() as conn:
            conn.execute("UPDATE counter SET n = n + 1")
    assert counter(db) == 1


def test_a_failed_commit_rolls_back_and_the_store_stays_usable(db: str) -> None:
    make_counter(db)
    with open_as(db, ME) as s:
        with s.transaction() as conn:
            conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            conn.execute(
                "CREATE TABLE child (p INTEGER REFERENCES parent (id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            )
        s.conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            with s.transaction() as conn:
                conn.execute("UPDATE counter SET n = 5")
                conn.execute("INSERT INTO child VALUES (42)")  # fails only at COMMIT
        assert not s.conn.in_transaction
        with s.transaction() as conn:
            conn.execute("UPDATE counter SET n = n + 1")
    assert counter(db) == 1


def test_event_ids_are_never_reused(db: str) -> None:
    with open_as(db, ME) as s:
        insert = (
            "INSERT INTO events (csession_id, run_no, event_ts) "
            "VALUES ('cs-aaaaaaaa', 1, '2026-01-01T00:00:00.000000Z')"
        )
        with s.transaction() as conn:
            conn.execute(insert)
            conn.execute(insert)
            (last,) = conn.execute("SELECT max(id) FROM events").fetchone()
            conn.execute("DELETE FROM events WHERE id = ?", (last,))
            conn.execute(insert)
            (new,) = conn.execute("SELECT max(id) FROM events").fetchone()
            assert new > last


FIRST_OPENER = """
import sys, time
from claude_session.store import Store
start = float(sys.argv[2])
time.sleep(max(0.0, start - time.time()))  # all processes open at once
Store.open(sys.argv[1]).close()
"""


def test_many_concurrent_first_opens_all_switch_to_wal(tmp_path: Path) -> None:
    """Concurrent WAL switches of a new database may fail SQLITE_BUSY at once; open retries."""
    for trial in range(3):
        path = str(tmp_path / f"t{trial}" / "state.db")
        start = time.time() + 0.5
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", FIRST_OPENER, path, str(start)],
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(14)
        ]
        errors = [p.communicate(timeout=60)[1] for p in procs]
        assert [p.returncode for p in procs] == [0] * len(procs), errors
        assert journal_mode(path) == "wal"


class FakeWalConnection:
    """Answers ``PRAGMA journal_mode=WAL`` with the given errors, then ``wal``."""

    def __init__(self, *errors: sqlite3.OperationalError) -> None:
        self.errors = list(errors)
        self.calls = 0

    def execute(self, sql: str) -> FakeWalConnection:
        assert sql == "PRAGMA journal_mode=WAL"
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self

    def fetchone(self) -> tuple[str]:
        return ("wal",)


def sqlite_error(message: str, code: int) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code  # type: ignore[attr-defined]
    return error


def test_the_wal_switch_retries_busy_and_locked() -> None:
    fake = FakeWalConnection(
        sqlite_error("database is locked", sqlite3.SQLITE_BUSY),
        sqlite_error("database table is locked", sqlite3.SQLITE_LOCKED),
    )
    store._enable_wal(fake, 5)  # type: ignore[arg-type]
    assert fake.calls == 3


def test_the_wal_switch_does_not_retry_other_errors() -> None:
    fake = FakeWalConnection(sqlite_error("disk I/O error", sqlite3.SQLITE_IOERR))
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        store._enable_wal(fake, 5)  # type: ignore[arg-type]
    assert fake.calls == 1


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
