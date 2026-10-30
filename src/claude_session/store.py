"""``state.db`` access: WAL mode, numbered migrations, leases and the schema-version check.

All SQL of claude-session lives in this module (design.md, Data model). Standard library only:
the hook path may import it (design.md, Hook-event fast path), but never migrates
(``Store.open(..., migrate=False)``).

- Every write is one ``BEGIN IMMEDIATE`` transaction with a busy timeout (5 s by default).
- ``meta.schema_version`` is ``<major>.<minor>``. Migrations are numbered, forward only and
  applied in order at open, under the lease ``migrate``; each one commits together with the
  version it brings the database to, so an interrupted migration resumes where it stopped.
- A database whose schema is newer than this code is refused with ``schema_newer`` (exit 12)
  before anything is written (interface.md, Launch order and Upgrades).
- Leases (``migrate``, ``reconcile``, ``delivery:<id>``) record the holder's PID and process
  start time and expire ``LEASE_TTL`` after their last renewal; a dead holder's lease may be
  taken over at once.
"""

from __future__ import annotations

import os
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import InternalError, SchemaNewerError

DB_NAME = "state.db"
BUSY_TIMEOUT = 5.0  # seconds (design.md, Data model: Concurrency)
LEASE_TTL = timedelta(seconds=60)
LEASE_RENEW_INTERVAL = timedelta(seconds=20)
MIGRATE_LEASE = "migrate"
# How long ``open`` waits for another process's migration before giving up.
MIGRATE_WAIT = 2 * LEASE_TTL.total_seconds()
MIGRATE_POLL = 0.05  # seconds

Version = tuple[int, int]
Clock = Callable[[], datetime]
# (pid, process start time) -> whether that exact process is still alive.
Liveness = Callable[[int, str], bool]


@dataclass(frozen=True)
class Holder:
    """A lease holder: a process, identified by its PID and its start time."""

    pid: int
    start: str


@dataclass(frozen=True)
class Migration:
    """One numbered migration: ``apply`` brings the schema to ``version``.

    ``apply`` runs inside the migration's ``BEGIN IMMEDIATE`` transaction; it must not commit.
    """

    version: Version
    apply: Callable[[sqlite3.Connection], None] = field(compare=False)


# State directory


def state_dir(environ: dict[str, str] | None = None) -> str:
    """``$XDG_STATE_HOME/claude-session`` (default ``~/.local/state/claude-session``).

    A relative or empty ``XDG_STATE_HOME`` is ignored, as the XDG base directory spec says.
    """
    env = os.environ if environ is None else environ
    base = env.get("XDG_STATE_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "claude-session")


def db_path(root: str) -> str:
    return os.path.join(root, DB_NAME)


def _ensure_private_dir(path: str) -> None:
    """Create ``path`` (and missing parents); a directory created here gets mode 0700."""
    parent = os.path.dirname(path)
    if parent and parent != path:
        os.makedirs(parent, exist_ok=True)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)  # explicit, whatever the umask


def _ensure_private_file(path: str) -> None:
    """Create ``path`` with mode 0600 unless it exists (SQLite gives ``-wal``/``-shm`` the
    database file's mode)."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


# Time and process identity


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_ts(moment: datetime) -> str:
    """UTC ISO 8601 with microseconds and ``Z``; fixed width, so text order is time order."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def current_holder() -> Holder:
    """This process as a lease holder."""
    # TODO(#6): wire procinfo liveness: record this process's real start time.
    return Holder(pid=os.getpid(), start="")


def pid_alive(pid: int, start: str) -> bool:
    """Interim liveness check: whether ``pid`` exists. It ignores ``start``, so a reused PID
    counts as alive and its lease is only taken over once it expires (safe, just slower)."""
    del start
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# Schema


def _migration_1_0(conn: sqlite3.Connection) -> None:
    """The data model of design.md (Data model). ``meta`` and ``leases`` are bootstrapped
    before any migration runs (``_bootstrap``)."""
    conn.execute(
        """
        CREATE TABLE sessions (
            csession_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            launch_kind TEXT NOT NULL,
            csession_dir TEXT,
            private_dir TEXT,
            tmp_dir TEXT,
            forked_from TEXT,
            tainted INTEGER NOT NULL DEFAULT 0,
            tainted_at TEXT,
            tainted_by_run INTEGER,
            current_run INTEGER NOT NULL,
            claude_session_id TEXT,
            state TEXT NOT NULL,
            state_since TEXT,
            activity TEXT,
            needs_input_reason TEXT,
            git_dir TEXT,
            updated_at TEXT NOT NULL,
            forgotten_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE runs (
            csession_id TEXT NOT NULL,
            run_no INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            run_kind TEXT NOT NULL,
            reads_third_party INTEGER,
            mode TEXT,
            cwd TEXT,
            repo TEXT,
            model TEXT,
            skill TEXT,
            has_prompt INTEGER,
            spec_json TEXT,
            resolved_json TEXT,
            config_hash TEXT,
            invocation_json TEXT,
            permission_profile TEXT,
            permission_profile_snapshot TEXT,
            sandbox_active INTEGER,
            decision_ids_json TEXT,
            escalated_hooks TEXT,
            warnings_json TEXT,
            message TEXT,
            hook_interpreter TEXT,
            config_path TEXT,
            expires_at TEXT,
            started_at TEXT,
            session_started_at TEXT,
            ended_at TEXT,
            stop_requested_at TEXT,
            pending_cause TEXT,
            wrapper_pid INTEGER,
            wrapper_start TEXT,
            child_pid INTEGER,
            child_start TEXT,
            outbox_dev_ino TEXT,
            artifacts_dev_ino TEXT,
            secret_sha256 TEXT,
            backstop_used INTEGER NOT NULL DEFAULT 0,
            report_status TEXT,
            report_implicit INTEGER,
            report_summary TEXT,
            report_reason TEXT,
            artifact_name TEXT,
            artifact_path TEXT,
            artifact_sha256 TEXT,
            artifact_size INTEGER,
            artifact_ingest_seq INTEGER,
            reported_at TEXT,
            phase TEXT,
            outcome TEXT,
            end_reason TEXT,
            exit_code INTEGER,
            cost_usd REAL,
            num_turns INTEGER,
            is_error INTEGER,
            session_end_reason TEXT,
            outcome_file TEXT,
            outcome_file_written_at TEXT,
            PRIMARY KEY (csession_id, run_no)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE claude_ids (
            csession_id TEXT NOT NULL,
            claude_session_id TEXT NOT NULL,
            run_no INTEGER NOT NULL,
            source TEXT,
            transcript_path TEXT,
            seen_at TEXT NOT NULL,
            PRIMARY KEY (csession_id, claude_session_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE labels (
            csession_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (csession_id, key, value)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            csession_id TEXT NOT NULL,
            run_no INTEGER NOT NULL,
            event_ts TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            from_activity TEXT,
            to_activity TEXT,
            needs_input_reason TEXT,
            cause TEXT,
            final INTEGER NOT NULL DEFAULT 0,
            labels_json TEXT,
            delivered_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE requests (
            request_id TEXT PRIMARY KEY,
            csession_id TEXT NOT NULL,
            run_no INTEGER NOT NULL,
            result TEXT NOT NULL,
            ingested_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE deleted_ids (
            csession_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX sessions_updated_at ON sessions (updated_at)")
    conn.execute("CREATE INDEX events_session_run ON events (csession_id, run_no, id)")
    conn.execute("CREATE INDEX events_undelivered ON events (delivered_at, id)")


MIGRATIONS: tuple[Migration, ...] = (Migration((1, 0), _migration_1_0),)


def code_version(migrations: Sequence[Migration] | None = None) -> Version:
    """The schema version this code brings a database to."""
    return (MIGRATIONS if migrations is None else migrations)[-1].version


def format_version(version: Version) -> str:
    return f"{version[0]}.{version[1]}"


def parse_version(text: str) -> Version:
    major, sep, minor = text.partition(".")
    if not sep or not major.isdigit() or not minor.isdigit():
        raise InternalError(f"state.db has an invalid schema_version {text!r}")
    return int(major), int(minor)


def _check_migrations(migrations: Sequence[Migration]) -> None:
    versions = [m.version for m in migrations]
    if not versions or versions[0] <= (0, 0) or versions != sorted(set(versions)):
        raise ValueError("migrations must be non-empty, above 0.0 and strictly increasing")


def _schema_newer(found: Version, code: Version) -> SchemaNewerError:
    return SchemaNewerError(
        f"state.db schema {format_version(found)} is newer than this claude-session "
        f"(schema {format_version(code)}); upgrade claude-session"
    )


def _enable_wal(conn: sqlite3.Connection, busy_timeout: float) -> None:
    """Switch to WAL (persistent). SQLite does not call the busy handler for this switch, so
    a lock held by another process opening the same new database is retried here, up to the
    busy timeout."""
    deadline = time.monotonic() + busy_timeout
    while True:
        try:
            (mode,) = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(MIGRATE_POLL)
            continue
        if mode != "wal":
            raise InternalError(f"state.db could not be switched to WAL mode (got {mode!r})")
        return


# Store


class Store:
    """An open ``state.db``. Use ``Store.open``; close it (or use it as a context manager)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        holder: Holder,
        liveness: Liveness,
        clock: Clock,
    ) -> None:
        self.conn = conn
        self.holder = holder
        self.liveness = liveness
        self.clock = clock

    @classmethod
    def open(
        cls,
        path: str,
        *,
        migrate: bool = True,
        busy_timeout: float = BUSY_TIMEOUT,
        migrations: Sequence[Migration] | None = None,
        holder: Holder | None = None,
        liveness: Liveness | None = None,
        clock: Clock | None = None,
        migrate_wait: float = MIGRATE_WAIT,
    ) -> Store:
        """Open (creating it, mode 0600 in a 0700 directory) the database at ``path``.

        With ``migrate`` (every command but ``hook-event``): refuse a newer schema with
        ``SchemaNewerError`` before writing anything, and migrate an older one under the
        ``migrate`` lease. Without it, the database must exist and its schema is used as it
        is.
        """
        migrations = MIGRATIONS if migrations is None else migrations
        _check_migrations(migrations)
        if migrate:
            _ensure_private_dir(os.path.dirname(os.path.abspath(path)))
            _ensure_private_file(path)
        # Without ``migrate`` (the hook path) a missing database is an error, never created.
        target = path if migrate else f"file:{urllib.parse.quote(path)}?mode=rw"
        # isolation_level=None: no implicit transactions; writes use BEGIN IMMEDIATE.
        conn = sqlite3.connect(target, timeout=busy_timeout, isolation_level=None, uri=not migrate)
        store = cls(
            conn,
            holder=holder or current_holder(),
            # TODO(#6): wire procinfo liveness (PID + start time) as the default.
            liveness=liveness or pid_alive,
            clock=clock or utc_now,
        )
        try:
            if migrate:
                _enable_wal(conn, busy_timeout)
                store._migrate(migrations, migrate_wait)
        except BaseException:
            conn.close()
            raise
        return store

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction: committed on success, rolled back on error.

        Waits up to the busy timeout for the write lock (``sqlite3.OperationalError`` after).
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # Schema version

    def schema_version(self) -> Version:
        """``meta.schema_version``; ``(0, 0)`` for a database never migrated."""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if row is None:
            return (0, 0)
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return (0, 0) if row is None else parse_version(row[0])

    def _bootstrap(self) -> None:
        """Create the tables migrations themselves need (``meta`` and ``leases``)."""
        with self.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leases (
                    name TEXT PRIMARY KEY,
                    holder_pid INTEGER NOT NULL,
                    holder_start TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )

    def _migrate(self, migrations: Sequence[Migration], wait: float) -> None:
        code = code_version(migrations)
        found = self.schema_version()
        if found > code:
            raise _schema_newer(found, code)
        if found == code:
            return
        self._bootstrap()
        deadline = time.monotonic() + wait
        while not self.take_lease(MIGRATE_LEASE):
            if time.monotonic() >= deadline:
                raise InternalError("timed out waiting for another process to migrate state.db")
            time.sleep(MIGRATE_POLL)
        try:
            for migration in migrations:
                # Re-checked inside each transaction: the steps stay correct even if this
                # process stalled and lost the lease to another migrator.
                with self.transaction() as conn:
                    found = self.schema_version()
                    if found > code:
                        raise _schema_newer(found, code)
                    if found >= migration.version:
                        continue
                    migration.apply(conn)
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        (format_version(migration.version),),
                    )
                # TODO(#7): log a `migration` event.
                self.renew_lease(MIGRATE_LEASE)
        finally:
            self.release_lease(MIGRATE_LEASE)

    # Leases

    def take_lease(self, name: str) -> bool:
        """Take lease ``name`` for this store's holder. Succeeds when the lease is free,
        expired, held by a dead process (PID + start time) or already held by this holder
        (then it is renewed). Returns whether this holder now holds it."""
        now = self.clock()
        me = self.holder
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT holder_pid, holder_start, expires_at FROM leases WHERE name = ?",
                (name,),
            ).fetchone()
            if row is not None:
                pid, start, expires_at = row
                mine = (pid, start) == (me.pid, me.start)
                expired = parse_ts(expires_at) <= now
                if not mine and not expired and self.liveness(pid, start):
                    return False
            conn.execute(
                "INSERT INTO leases (name, holder_pid, holder_start, expires_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET "
                "holder_pid = excluded.holder_pid, holder_start = excluded.holder_start, "
                "expires_at = excluded.expires_at",
                (name, me.pid, me.start, format_ts(now + LEASE_TTL)),
            )
        return True

    def renew_lease(self, name: str) -> bool:
        """Extend lease ``name`` by ``LEASE_TTL`` from now. Returns False if this holder no
        longer holds it (it was taken over or released)."""
        expires_at = format_ts(self.clock() + LEASE_TTL)
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE leases SET expires_at = ? "
                "WHERE name = ? AND holder_pid = ? AND holder_start = ?",
                (expires_at, name, self.holder.pid, self.holder.start),
            )
            return cursor.rowcount == 1

    def release_lease(self, name: str) -> None:
        """Release lease ``name`` if this holder holds it."""
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM leases WHERE name = ? AND holder_pid = ? AND holder_start = ?",
                (name, self.holder.pid, self.holder.start),
            )

    def lease_holder(self, name: str) -> tuple[Holder, datetime] | None:
        """The recorded holder of lease ``name`` and its expiry, or None if it is free."""
        row = self.conn.execute(
            "SELECT holder_pid, holder_start, expires_at FROM leases WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        return Holder(pid=row[0], start=row[1]), parse_ts(row[2])

    # Sessions

    def list_sessions(self) -> list[dict[str, Any]]:
        """Sessions that are not forgotten, newest first (raw ``sessions`` rows)."""
        # TODO(#16): join the current run and labels, apply the list filters.
        cursor = self.conn.execute(
            "SELECT * FROM sessions WHERE forgotten_at IS NULL "
            "ORDER BY created_at DESC, csession_id DESC"
        )
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def open_state(**kwargs: Any) -> Store:
    """Open ``state.db`` in the default state directory (see ``Store.open``)."""
    return Store.open(db_path(state_dir()), **kwargs)
