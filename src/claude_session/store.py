"""``state.db`` access: WAL mode, numbered migrations, leases and the schema-version check.

All SQL of claude-session lives in this module (design.md, Data model). Standard library only:
the hook path may import it (design.md, Hook-event fast path), but never migrates
(``Store.open(..., migrate=False)``).

- Every write is one ``BEGIN IMMEDIATE`` transaction with a busy timeout (5 s by default).
- ``meta.schema_version`` is ``<major>.<minor>``. Migrations are numbered, forward only and
  applied in order at open, under the lease ``migrate``; each one commits together with the
  version it brings the database to, so an interrupted migration resumes where it stopped.
- A database whose schema is newer than this code is refused with ``schema_newer`` (exit 12)
  before claude-session writes any data to it (interface.md, Launch order and Upgrades).
- Leases (``migrate``, ``reconcile``, ``delivery:<id>``) record the holder's PID, process
  start time and a per-``Store`` token, and expire ``LEASE_TTL`` after their last renewal; a
  dead holder's lease may be taken over at once.
- Open refuses, with ``FailedError`` (exit 12), a state directory or ``state.db`` that is
  unsafe (a symlink, not ours, hard-linked, not a regular file), unusable or not a
  claude-session database (``PRAGMA application_id``); claude-session writes no data to such
  a database (it may only tighten the modes of the state directory and ``state.db``, which it
  already checked are the user's own; SQLite's own hot-journal recovery may still run).
- Open never opens a file descriptor of its own on an existing ``state.db``: closing it
  would drop every POSIX lock this process holds on the file, SQLite's included. Paths are
  checked with ``lstat``; only a missing file is created (``O_EXCL``), and closed before
  any thread of the process connects. Only ``migrate`` opens tighten modes; the hook path
  only checks.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .errors import FailedError, InternalError, SchemaNewerError

if TYPE_CHECKING:
    from .outbox import Frozen

DB_NAME = "state.db"
BUSY_TIMEOUT = 5.0  # seconds (design.md, Data model: Concurrency)
LEASE_TTL = timedelta(seconds=60)
LEASE_RENEW_INTERVAL = timedelta(seconds=20)
MIGRATE_LEASE = "migrate"
# How long ``open`` waits for another process's migration before giving up.
MIGRATE_WAIT = 2 * LEASE_TTL.total_seconds()
MIGRATE_POLL = 0.05  # seconds
# ``PRAGMA application_id`` of a claude-session database ("CSes"; design.md, Data model).
APPLICATION_ID = 0x43536573
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# A session id or a prefix of one (ids.SESSION_ID is the full grammar).
_SESSION_ID_PREFIX = re.compile(r"cs-[a-z2-7]{1,8}")
_SESSION_ID = re.compile(r"cs-[a-z2-7]{8}")
# Held while state.db is checked and, when missing, created: closing the create descriptor
# drops this process's POSIX locks, so no other thread may connect before that close. Any
# thread that takes it after the creator finds the file and closed descriptor; it is never
# held across a database wait.
# The guarantee assumes state.db is not deleted from outside while the process runs.
_OPEN_LOCK = threading.Lock()


def _reset_open_lock() -> None:
    """In a forked child, replace the lock: another thread may have held it at the fork."""
    global _OPEN_LOCK
    _OPEN_LOCK = threading.Lock()


os.register_at_fork(after_in_child=_reset_open_lock)

Version = tuple[int, int]
Clock = Callable[[], datetime]
# (pid, process start time) -> whether that exact process is still alive.
Liveness = Callable[[int, str], bool]
# ``finalize_run``'s ``report_status`` when the report status is not compared.
ANY: Any = object()


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


def _make_private_dirs(path: str) -> None:
    """Create ``path`` and its missing parents, each with mode 0700 whatever the umask."""
    missing = []
    while not os.path.lexists(path):
        missing.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            continue
        os.chmod(directory, 0o700)


def _check_private_dir(path: str, *, tighten: bool) -> None:
    """``path`` must be a real directory (not a symlink) owned by us; with ``tighten`` it
    is made 0700."""
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe(path, "not a directory (or a symbolic link)")
    if info.st_uid != os.geteuid():
        raise _unsafe(path, f"owned by uid {info.st_uid}")
    if tighten and stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)


def _check_db_file(path: str, info: os.stat_result) -> None:
    """``info`` (from ``lstat``) must be a regular file owned by us with a single link."""
    if stat.S_ISLNK(info.st_mode):
        raise _unsafe(path, "a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise _unsafe(path, "not a regular file")
    if info.st_uid != os.geteuid():
        raise _unsafe(path, f"owned by uid {info.st_uid}")
    if info.st_nlink != 1:
        raise _unsafe(path, f"has {info.st_nlink} hard links")


def _prepare_db_file(path: str, *, migrate: bool) -> None:
    """Check ``path`` without opening it; with ``migrate``, create it (0600) when missing and
    tighten it (and an existing ``-wal``/``-shm``): group and other bits are cleared, owner
    bits never added, so a mode is only ever tightened.

    No descriptor is ever held on an existing database: POSIX ``close`` drops all of this
    process's ``fcntl`` locks on the file, including those of live SQLite connections. A
    file this call creates is closed before anything connects to it."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not migrate:
            raise
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open(path, flags, 0o600)
        except FileExistsError:  # created concurrently: check it like any other
            info = os.lstat(path)
        else:
            try:
                os.fchmod(fd, 0o600)  # whatever the umask
            finally:
                os.close(fd)
            return
    _check_db_file(path, info)
    if not migrate:
        return
    _tighten(path, info)
    for sidecar in (path + "-wal", path + "-shm"):
        try:
            side = os.lstat(sidecar)
        except FileNotFoundError:
            continue
        # Anything else is skipped: SQLite refuses a symlinked -wal/-shm itself.
        if stat.S_ISREG(side.st_mode) and side.st_uid == os.geteuid():
            _tighten(sidecar, side)


def _tighten(path: str, info: os.stat_result) -> None:
    """Clear the group and other bits of ``path`` (never adds a bit)."""
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        os.chmod(path, mode & ~0o077)


def _unsafe(path: str, cause: str) -> FailedError:
    return FailedError("unsafe state path", detail=f"{path}: {cause}")


def _open_failed(path: str, exc: OSError | sqlite3.Error) -> FailedError:
    cause = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
    where = exc.filename if isinstance(exc, OSError) and exc.filename else path
    return FailedError("cannot open the state database", detail=f"{where}: {cause}")


def _db_uri(path: str) -> str:
    """A ``file:`` URI for ``path`` (``mode=rw``). The absolute path starts with exactly one
    slash, so it can never be read as a URI authority (``//host``)."""
    absolute = "/" + os.path.abspath(path).lstrip("/")
    return f"file:{urllib.parse.quote(absolute)}?mode=rw"


def _connect(path: str, busy_timeout: float) -> sqlite3.Connection:
    """Connect to an existing file (``mode=rw``: SQLite never creates it). isolation_level
    None: no implicit transactions; writes use BEGIN IMMEDIATE."""
    return sqlite3.connect(
        _db_uri(path),
        timeout=busy_timeout,
        isolation_level=None,
        uri=True,
    )


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """Whether ``exc`` is SQLITE_BUSY or SQLITE_LOCKED (any extended code)."""
    return exc.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


# Time and process identity


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_ts(moment: datetime) -> str:
    """UTC ISO 8601 with microseconds and ``Z``; fixed width, so text order is time order."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


_procinfo: Any = None


def system_procinfo() -> Any:
    """The process's ``procinfo.ProcInfo`` over the real gateway, made on first use (imported
    lazily: the hook path opens a store but never checks liveness)."""
    global _procinfo
    if _procinfo is None:
        from .gateway import SystemGateway
        from .procinfo import ProcInfo

        _procinfo = ProcInfo(SystemGateway())
    return _procinfo


def start_time(pid: int, *, zombie: bool = False) -> str:
    """The procinfo start-time token of ``pid`` (``""``: gone, a zombie unless ``zombie``,
    or unknown). Stored as data: changing the token format would make every live run look
    dead."""
    from .gateway import GatewayTimeout
    from .procinfo import ProcInfoError

    try:
        return system_procinfo().start_time(pid, zombie=zombie) or ""
    except (ProcInfoError, OSError, ValueError, GatewayTimeout):
        return ""


def current_holder() -> Holder:
    """This process as a lease holder, with its procinfo start time (``""`` when unknown:
    see ``Store.take_lease``)."""
    return Holder(pid=os.getpid(), start=start_time(os.getpid()))


# The largest PID the kernel's pid_t can hold; a larger stored value names no process.
MAX_PID = 2**31 - 1


def pid_exists(pid: int) -> bool:
    """Whether a process with ``pid`` exists (one we may not signal counts). A PID outside
    1..``MAX_PID`` names none."""
    if not 0 < pid <= MAX_PID:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        return True
    return True


def pid_alive(pid: int, start: str) -> bool:
    """The default liveness check: whether the exact process ``pid`` with the procinfo
    start-time token ``start`` still runs (not a zombie). An unknown start (``""``) falls back
    to whether ``pid`` exists. When the platform cannot answer (procinfo error), the process
    counts as alive: a live process must never be finalized or have its lease taken (a dead
    one is then only noticed later, which is safe). Cheap on Linux (one ``/proc`` read):
    it runs inside ``BEGIN IMMEDIATE``."""
    if start == "":
        return pid_exists(pid)
    from .gateway import GatewayTimeout
    from .procinfo import ProcInfoError

    try:
        return bool(system_procinfo().is_alive(pid, start))
    except (ProcInfoError, OSError, GatewayTimeout):
        return True
    except ValueError:  # not a valid PID
        return False


def pid_known_alive(pid: int, start: str) -> bool:
    """Whether the exact process ``pid`` with the start-time token ``start`` is known to run:
    for showing the processes of a run that has ended. Unlike ``pid_alive``, an unknown start
    (``""``) or a check the platform cannot answer counts as gone: nothing is decided by it,
    and a reused PID must not show an ended run's wrapper as alive."""
    if start == "" or not 0 < pid <= MAX_PID:
        return False
    from .gateway import GatewayTimeout
    from .procinfo import ProcInfoError

    try:
        return bool(system_procinfo().is_alive(pid, start))
    except (ProcInfoError, OSError, GatewayTimeout, ValueError):
        return False


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
    """``<major>.<minor>``; ``ValueError`` for anything else."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+", text):
        raise ValueError(f"invalid schema_version {text!r}")
    major, _, minor = text.partition(".")
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


def _check_identity(conn: sqlite3.Connection, path: str, *, allow_new: bool) -> None:
    """Refuse a database that is not ours, before claude-session writes any data. With
    ``allow_new``, an empty database (application_id 0, no schema) is accepted."""
    # One statement, one snapshot: a concurrent first open may bootstrap in between.
    app_id, objects = conn.execute(
        "SELECT (SELECT application_id FROM pragma_application_id), "
        "(SELECT count(*) FROM sqlite_master)"
    ).fetchone()
    if app_id == APPLICATION_ID:
        return
    if allow_new and app_id == 0 and objects == 0:
        return
    cause = "empty, never migrated" if app_id == 0 and objects == 0 else f"application_id {app_id}"
    raise FailedError("not a claude-session database", detail=f"{path}: {cause}")


def _enable_wal(conn: sqlite3.Connection, busy_timeout: float) -> None:
    """Switch to WAL (persistent). SQLite calls the busy handler for this switch, but when
    several connections switch the same new database at once it may return SQLITE_BUSY at
    once (waiting could deadlock); only that error is retried, up to the busy timeout."""
    deadline = time.monotonic() + busy_timeout
    while True:
        try:
            (mode,) = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            break
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(MIGRATE_POLL)
    if mode != "wal":
        raise InternalError(f"state.db could not be switched to WAL mode (got {mode!r})")


def _open_connection(path: str, *, migrate: bool, busy_timeout: float) -> sqlite3.Connection:
    """Check (with ``migrate``: create) ``path`` and connect to it; with ``migrate`` the
    database is switched to WAL. Unsafe, unusable or foreign files raise ``FailedError``."""
    root = os.path.dirname(os.path.abspath(path))
    try:
        if migrate:
            _make_private_dirs(root)
        _check_private_dir(root, tighten=migrate)
        with _OPEN_LOCK:
            _prepare_db_file(path, migrate=migrate)
    except OSError as exc:
        raise _open_failed(path, exc) from exc
    try:
        conn = _connect(path, busy_timeout)
    except sqlite3.DatabaseError as exc:
        raise _open_failed(path, exc) from exc
    try:
        _check_identity(conn, path, allow_new=migrate)
        if migrate:
            _enable_wal(conn, busy_timeout)
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise _open_failed(path, exc) from exc
    except BaseException:
        conn.close()
        raise
    return conn


def _log_migration(db: str, version: Version) -> None:
    """The ``migration`` record in ``log.jsonl`` next to ``state.db`` (best effort: a
    migration that committed is never undone because its record could not be written)."""
    try:
        from .logs import EventLog

        EventLog(os.path.join(os.path.dirname(db), "log.jsonl")).write(
            "migration", message=f"state.db schema migrated to {format_version(version)}"
        )
    except (OSError, ValueError):
        pass


# Store


class Store:
    """An open ``state.db``. Use ``Store.open``; close it (or use it as a context manager)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        path: str,
        holder: Holder | None,
        liveness: Liveness,
        clock: Clock,
        known_liveness: Liveness = pid_known_alive,
    ) -> None:
        self.conn = conn
        self.path = path
        # Computed on first use (leases): the hook path never needs this process's start time.
        self._holder = None if holder is None else Holder(pid=holder.pid, start=holder.start or "")
        # Lease ownership is (pid, start, token): two stores of one process never share one.
        self.token = uuid.uuid4().hex
        self.liveness = liveness
        # For display only (an ended run's processes): "cannot tell" counts as gone.
        self.known_liveness = known_liveness
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
        known_liveness: Liveness | None = None,
    ) -> Store:
        """Open (creating it, mode 0600 in a 0700 directory) the database at ``path``.

        With ``migrate`` (every command but ``hook-event``): refuse a newer schema with
        ``SchemaNewerError`` before writing any data, and migrate an older one under the
        ``migrate`` lease. Without it (the hook path), the database must exist and have the
        code's schema major version; it is used as it is, never created or migrated.

        An unsafe, unusable or foreign state directory or database, and a migration lease
        not obtained within ``migrate_wait`` or a migration that cannot complete (lease
        lost, database locked), raise ``FailedError`` naming the path.
        """
        migrations = MIGRATIONS if migrations is None else migrations
        _check_migrations(migrations)
        conn = _open_connection(path, migrate=migrate, busy_timeout=busy_timeout)
        store = cls(
            conn,
            path=path,
            holder=holder,
            liveness=liveness or pid_alive,
            clock=clock or utc_now,
            known_liveness=known_liveness or pid_known_alive,
        )
        try:
            if migrate:
                store._migrate(migrations, migrate_wait, path)
            else:
                store._check_hook_schema(code_version(migrations), path)
        except BaseException:
            conn.close()
            raise
        return store

    @property
    def holder(self) -> Holder:
        """This store's lease holder (default: this process, ``current_holder``)."""
        if self._holder is None:
            self._holder = current_holder()
        return self._holder

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
            self.conn.execute("COMMIT")
        except BaseException:
            # SQLite may have rolled back already (e.g. INSERT OR ROLLBACK); a failed
            # ROLLBACK must not mask the original error, which is re-raised.
            if self.conn.in_transaction:
                with suppress(sqlite3.Error):
                    self.conn.execute("ROLLBACK")
            raise

    @contextmanager
    def savepoint(self, name: str) -> Iterator[None]:
        """A ``SAVEPOINT`` inside the current transaction: released on success, rolled back
        to (and released) on error, which is re-raised. When SQLite already rolled the whole
        transaction back, the error is re-raised as is and the caller must not continue in it
        (``conn.in_transaction`` is then false). ``name`` is a fixed identifier."""
        if not name.isidentifier():
            raise ValueError("savepoint names are identifiers")
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            if self.conn.in_transaction:
                with suppress(sqlite3.Error):
                    self.conn.execute(f"ROLLBACK TO {name}")
                    self.conn.execute(f"RELEASE {name}")
            raise
        self.conn.execute(f"RELEASE {name}")

    # Schema version

    def schema_version(self) -> Version:
        """``meta.schema_version``; ``(0, 0)`` for a database never migrated."""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
        ).fetchone()
        if row is None:
            return (0, 0)
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            return (0, 0)
        try:
            return parse_version(row[0])
        except ValueError as exc:
            raise FailedError(
                "cannot open the state database", detail=f"{self.path}: {exc}"
            ) from exc

    def _check_hook_schema(self, code: Version, path: str) -> None:
        """The hook path uses a database of the code's major version as it is (a newer
        minor too); anything else is refused."""
        found = self.schema_version()
        if found == (0, 0) or found[0] != code[0]:
            raise FailedError(
                f"state.db schema {format_version(found)} is not usable by this "
                f"claude-session (schema {format_version(code)})",
                detail=path,
            )

    def _bootstrap(self) -> None:
        """Mark the database as ours and create the tables migrations themselves need
        (``meta`` and ``leases``), in one transaction."""
        with self.transaction() as conn:
            conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leases (
                    name TEXT PRIMARY KEY,
                    holder_pid INTEGER NOT NULL,
                    holder_start TEXT NOT NULL,
                    holder_token TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL
                )
                """
            )

    def _wait_for_migrate_lease(self, wait: float, path: str) -> None:
        """Bootstrap and take the ``migrate`` lease, retrying while another process holds
        it or the database is busy (e.g. inside a migration step), for up to ``wait``."""
        deadline = time.monotonic() + wait
        (configured,) = self.conn.execute("PRAGMA busy_timeout").fetchone()

        def bound_busy_timeout() -> None:
            # Before each blocking step: never wait for the lock past the deadline.
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            self.conn.execute(f"PRAGMA busy_timeout = {min(configured, remaining)}")

        try:
            while True:
                try:
                    bound_busy_timeout()
                    self._bootstrap()
                    bound_busy_timeout()
                    if self.take_lease(MIGRATE_LEASE):
                        return
                except sqlite3.OperationalError as exc:
                    if not _is_busy(exc):
                        raise
                if time.monotonic() >= deadline:
                    raise FailedError(
                        "timed out waiting for another process to migrate state.db",
                        detail=f"{path}: migrate lease not obtained within {wait:g} s",
                    )
                time.sleep(MIGRATE_POLL)
        finally:
            self.conn.execute(f"PRAGMA busy_timeout = {int(configured)}")

    def _migrate(self, migrations: Sequence[Migration], wait: float, path: str) -> None:
        code = code_version(migrations)
        found = self.schema_version()
        if found > code:
            raise _schema_newer(found, code)
        if found == code:
            return
        self._wait_for_migrate_lease(wait, path)
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
                _log_migration(path, migration.version)
                if not self.renew_lease(MIGRATE_LEASE):
                    raise FailedError(
                        "lost the migrate lease while migrating state.db",
                        detail=f"{path}: migrate lease taken over",
                    )
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                raise
            raise FailedError(
                "state.db stayed locked while migrating", detail=f"{path}: {exc}"
            ) from exc
        finally:
            # Must not mask the error above; a dead process's lease is taken over at once.
            with suppress(sqlite3.Error):
                self.release_lease(MIGRATE_LEASE)

    # Leases

    def take_lease(self, name: str) -> bool:
        """Take lease ``name`` for this store. Succeeds when the lease is free, expired
        (including an expiry more than ``2 * LEASE_TTL`` ahead: clock skew), held by a dead
        process or already held by this store (then it is renewed). Returns whether this
        store now holds it.

        The clock is read inside the transaction: read before waiting for the lock, a
        renewal committed meanwhile would look like an expiry far in the future."""
        me = self.holder
        with self.transaction() as conn:
            now = self.clock()
            row = conn.execute(
                "SELECT holder_pid, holder_start, holder_token, expires_at "
                "FROM leases WHERE name = ?",
                (name,),
            ).fetchone()
            if row is not None:
                pid, start, token, expires_at = row
                mine = (pid, start, token) == (me.pid, me.start, self.token)
                expiry = parse_ts(expires_at)
                expired = expiry <= now or expiry > now + 2 * LEASE_TTL
                if not mine and not expired and self._holder_alive(pid, start):
                    return False
            conn.execute(
                "INSERT INTO leases (name, holder_pid, holder_start, holder_token, expires_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET "
                "holder_pid = excluded.holder_pid, holder_start = excluded.holder_start, "
                "holder_token = excluded.holder_token, expires_at = excluded.expires_at",
                (name, me.pid, me.start, self.token, format_ts(now + LEASE_TTL)),
            )
        return True

    def _holder_alive(self, pid: int, start: str) -> bool:
        """A holder with an unknown start time (``""``) is checked by PID only."""
        return pid_exists(pid) if start == "" else self.liveness(pid, start)

    def renew_lease(self, name: str) -> bool:
        """Extend lease ``name`` by ``LEASE_TTL`` from now. Returns False if this store no
        longer holds it (it was taken over or released)."""
        with self.transaction() as conn:
            expires_at = format_ts(self.clock() + LEASE_TTL)
            cursor = conn.execute(
                "UPDATE leases SET expires_at = ? WHERE name = ? AND holder_pid = ? "
                "AND holder_start = ? AND holder_token = ?",
                (expires_at, name, self.holder.pid, self.holder.start, self.token),
            )
            return cursor.rowcount == 1

    def release_lease(self, name: str) -> None:
        """Release lease ``name`` if this store holds it."""
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM leases WHERE name = ? AND holder_pid = ? AND holder_start = ? "
                "AND holder_token = ?",
                (name, self.holder.pid, self.holder.start, self.token),
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

    def session_id_used(self, csession_id: str) -> bool:
        """Whether ``csession_id`` belongs to a session (live or forgotten) or to a deleted one
        (``deleted_ids``), so allocation never reuses it. Call it inside the transaction that
        inserts the new session, so the check and the insert are one."""
        row = self.conn.execute(
            "SELECT 1 FROM sessions WHERE csession_id = ? "
            "UNION ALL SELECT 1 FROM deleted_ids WHERE csession_id = ? LIMIT 1",
            (csession_id, csession_id),
        ).fetchone()
        return row is not None

    def session_id_candidates(self, prefix: str) -> list[tuple[str, bool]]:
        """``(csession_id, forgotten)`` of every session whose id starts with ``prefix``.

        A range comparison on the primary key (``prefix <= id < upper``, where ``upper`` is
        ``prefix`` with its last character incremented), never ``LIKE``. Callers normalize
        and validate the prefix first (``ids.lookup_session_id``); a prefix outside
        ``cs-[a-z2-7]{1,8}`` raises ``ValueError``. Stored ids are decoded leniently and a
        row whose id is not a valid session id (corrupt) is skipped, so it never breaks the
        lookup of the others.
        """
        if _SESSION_ID_PREFIX.fullmatch(prefix) is None:
            raise ValueError(f"not a session id prefix: {prefix!r}")
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        with self._lenient_decoding():
            rows = self.conn.execute(
                "SELECT csession_id, forgotten_at IS NOT NULL FROM sessions "
                "WHERE csession_id >= ? AND csession_id < ? ORDER BY csession_id",
                (prefix, upper),
            ).fetchall()
        return [
            (sid, bool(forgotten))
            for sid, forgotten in rows
            if isinstance(sid, str) and _SESSION_ID.fullmatch(sid) is not None
        ]

    def list_sessions(self) -> list[dict[str, Any]]:
        """Sessions that are not forgotten, most recently updated first (raw ``sessions``
        rows). ``list_records`` adds each one's current run, labels and run kinds."""
        cursor = self.conn.execute(
            "SELECT * FROM sessions WHERE forgotten_at IS NULL "
            "ORDER BY updated_at DESC, csession_id DESC"
        )
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    # Read views (status, list). One read transaction each, so a view is one snapshot; text
    # is decoded leniently (``_lenient_text``) so stored bytes that are not UTF-8 never fail.

    @contextmanager
    def _lenient_decoding(self) -> Iterator[None]:
        """Lenient text decoding on the shared connection (restored afterwards); usable
        inside a caller's transaction."""
        previous = self.conn.text_factory
        self.conn.text_factory = _lenient_text
        try:
            yield
        finally:
            self.conn.text_factory = previous

    @contextmanager
    def _read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """A deferred read transaction with lenient text decoding (restored afterwards)."""
        with self._lenient_decoding():
            self.conn.execute("BEGIN")
            try:
                yield self.conn
            finally:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")  # nothing was written

    def status_record(self, csession_id: str, run_no: int | None = None) -> StatusRecord | None:
        """The rows ``status`` shows: the session (forgotten or not; the caller resolved the
        id), run ``run_no`` (default: the current run; None when it does not exist), the run
        count, labels, Claude session ids and the last ``STATUS_EVENTS`` events (oldest
        first). None when the session does not exist."""
        with self._read_snapshot() as conn:
            session = _one_dict(conn, "SELECT * FROM sessions WHERE csession_id = ?", csession_id)
            if session is None:
                return None
            wanted = session["current_run"] if run_no is None else run_no
            run = None
            if isinstance(wanted, int) and 1 <= wanted <= MAX_SQLITE_INTEGER:
                run = _one_dict(
                    conn,
                    "SELECT * FROM runs WHERE csession_id = ? AND run_no = ?",
                    csession_id,
                    wanted,
                )
            current = session["current_run"]
            current_run = run
            if run_no is not None and run_no != current:
                current_run = _one_dict(
                    conn,
                    "SELECT * FROM runs WHERE csession_id = ? AND run_no = ?",
                    csession_id,
                    current,
                )
            (runs,) = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE csession_id = ?", (csession_id,)
            ).fetchone()
            labels = _labels(conn, csession_id)
            claude_ids = _all_dicts(
                conn,
                "SELECT claude_session_id, run_no, source, transcript_path, seen_at "
                "FROM claude_ids WHERE csession_id = ? ORDER BY seen_at, claude_session_id",
                csession_id,
            )
            events = _all_dicts(
                conn,
                "SELECT * FROM (SELECT id, run_no, event_ts, from_state, to_state, "
                "from_activity, to_activity, needs_input_reason, cause, final, delivered_at "
                "FROM events WHERE csession_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                csession_id,
                STATUS_EVENTS,
            )
        return StatusRecord(
            session=session,
            run=run,
            current_run=current_run,
            runs=int(runs),
            labels=labels,
            claude_ids=claude_ids,
            events=events,
        )

    def list_records(self) -> list[ListRecord]:
        """Every session that is not forgotten, in ``list_sessions`` order, with its current
        run, labels and the kinds of all its runs. A session whose current-run row is
        missing (corrupt) is skipped, as ``status`` does not find it."""
        with self._read_snapshot() as conn:
            sessions = self.list_sessions()
            runs = _all_dicts(
                conn,
                "SELECT r.* FROM runs r JOIN sessions s ON s.csession_id = r.csession_id "
                "AND r.run_no = s.current_run WHERE s.forgotten_at IS NULL",
            )
            labels: dict[str, list[str]] = {}
            for sid, key, value in conn.execute(
                "SELECT l.csession_id, l.key, l.value FROM labels l JOIN sessions s "
                "ON s.csession_id = l.csession_id WHERE s.forgotten_at IS NULL "
                "ORDER BY l.csession_id, l.key, l.value"
            ):
                labels.setdefault(sid, []).append(f"{key}={value}")
            kinds: dict[str, set[str]] = {}
            for sid, kind in conn.execute(
                "SELECT DISTINCT r.csession_id, r.run_kind FROM runs r JOIN sessions s "
                "ON s.csession_id = r.csession_id WHERE s.forgotten_at IS NULL"
            ):
                kinds.setdefault(sid, set()).add(kind)
        current = {row["csession_id"]: row for row in runs}
        return [
            ListRecord(
                session=session,
                run=current[session["csession_id"]],
                labels=labels.get(session["csession_id"], []),
                run_kinds=frozenset(kinds.get(session["csession_id"], set())),
            )
            for session in sessions
            if session["csession_id"] in current
        ]

    # Runs

    def record_run_files(
        self,
        csession_id: str,
        run_no: int,
        *,
        secret_sha256: str,
        outbox_dev_ino: str,
        artifacts_dev_ino: str,
    ) -> None:
        """Record the run's secret digest (never the secret) and the ``<st_dev>:<st_ino>`` of
        the ``outbox/`` and ``artifacts/`` directories ``exec`` prepared (design.md, Session
        directories). ``InternalError`` when the run row does not exist."""
        if _SHA256_HEX.fullmatch(secret_sha256) is None:
            raise ValueError("secret_sha256 must be 64 lowercase hex characters")
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE runs SET secret_sha256 = ?, outbox_dev_ino = ?, artifacts_dev_ino = ? "
                "WHERE csession_id = ? AND run_no = ?",
                (secret_sha256, outbox_dev_ino, artifacts_dev_ino, csession_id, run_no),
            )
            if cursor.rowcount != 1:
                raise InternalError(f"no run {run_no} of session {csession_id}")

    # Launch and supervision (exec). Each method is one transaction.

    def allocate(self, run: NewRun, *, new_id: Callable[[Callable[[str], bool]], str]) -> str:
        """Launch step 4: allocate a session id (``new_id`` is ``ids.new_session_id``, given
        ``session_id_used`` so the check and the insert share this transaction) and insert the
        session (state ``starting``), its run 1 (phase ``launch``, the wrapper already set:
        plain ``exec`` performs every step itself), its labels and the ``launch`` event. A
        third-party-reading run taints the session here (interface.md, Taint). Returns the
        session id."""
        with self.transaction() as conn:
            csession_id = new_id(self.session_id_used)
            now = format_ts(self.clock())
            conn.execute(
                "INSERT INTO sessions (csession_id, created_at, launch_kind, csession_dir, "
                "private_dir, tainted, tainted_at, tainted_by_run, current_run, state, "
                "state_since, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'starting', ?, ?)",
                (
                    csession_id,
                    now,
                    run.kind,
                    os.path.join(run.state_root, "sessions", csession_id),
                    os.path.join(run.state_root, "private", csession_id),
                    int(run.reads_third_party),
                    now if run.reads_third_party else None,
                    1 if run.reads_third_party else None,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO runs (csession_id, run_no, created_at, run_kind, reads_third_party, "
                "mode, repo, model, skill, has_prompt, spec_json, resolved_json, config_hash, "
                "invocation_json, decision_ids_json, warnings_json, hook_interpreter, "
                "config_path, wrapper_pid, wrapper_start, outcome_file, phase) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'launch')",
                (
                    csession_id,
                    now,
                    run.kind,
                    int(run.reads_third_party),
                    run.mode,
                    run.repo,
                    run.model,
                    run.skill,
                    int(run.has_prompt),
                    json.dumps(run.spec, ensure_ascii=False, sort_keys=True),
                    json.dumps(run.resolved, ensure_ascii=False, sort_keys=True),
                    run.config_hash,
                    json.dumps(run.invocation, ensure_ascii=False, sort_keys=True),
                    json.dumps(list(run.decision_ids), ensure_ascii=False),
                    json.dumps(list(run.warnings), ensure_ascii=False),
                    run.hook_interpreter,
                    run.config_path,
                    run.wrapper_pid,
                    run.wrapper_start,
                    run.outcome_file,
                ),
            )
            labels = sorted({f"kind={run.kind}", *run.labels})
            for label in labels:
                key, _, value = label.partition("=")
                conn.execute(
                    "INSERT OR IGNORE INTO labels (csession_id, key, value) VALUES (?, ?, ?)",
                    (csession_id, key, value),
                )
            conn.execute(
                "INSERT INTO events (csession_id, run_no, event_ts, from_state, to_state, "
                "from_activity, to_activity, needs_input_reason, cause, final, labels_json) "
                "VALUES (?, 1, ?, NULL, 'starting', NULL, NULL, NULL, 'launch', 0, ?)",
                (csession_id, now, json.dumps(labels, ensure_ascii=False)),
            )
        return csession_id

    def update_run(self, csession_id: str, run_no: int, **fields: Any) -> None:
        """Set columns of a run row (only ``RUN_FIELDS``). ``InternalError`` when the run row
        does not exist."""
        unknown = sorted(set(fields) - RUN_FIELDS)
        if unknown or not fields:
            raise ValueError(f"not settable run fields: {unknown}")
        columns = sorted(fields)
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE runs SET {assignments} WHERE csession_id = ? AND run_no = ?",
                (*(fields[name] for name in columns), csession_id, run_no),
            )
            if cursor.rowcount != 1:
                raise InternalError(f"no run {run_no} of session {csession_id}")

    def update_session(self, csession_id: str, **fields: Any) -> None:
        """Set columns of a session row (only ``SESSION_FIELDS``)."""
        unknown = sorted(set(fields) - SESSION_FIELDS)
        if unknown or not fields:
            raise ValueError(f"not settable session fields: {unknown}")
        columns = sorted(fields)
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE sessions SET {assignments} WHERE csession_id = ?",
                (*(fields[name] for name in columns), csession_id),
            )
            if cursor.rowcount != 1:
                raise InternalError(f"no session {csession_id}")

    def add_run_warnings(self, csession_id: str, run_no: int, warnings: Sequence[str]) -> None:
        """Append ``warnings`` to the run's ``warnings_json`` (duplicates kept once)."""
        if not warnings:
            return
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT warnings_json FROM runs WHERE csession_id = ? AND run_no = ?",
                (csession_id, run_no),
            ).fetchone()
            if row is None:
                raise InternalError(f"no run {run_no} of session {csession_id}")
            current = json.loads(row[0] or "[]")
            merged = list(dict.fromkeys([*current, *warnings]))
            conn.execute(
                "UPDATE runs SET warnings_json = ? WHERE csession_id = ? AND run_no = ?",
                (json.dumps(merged, ensure_ascii=False), csession_id, run_no),
            )

    def set_implicit_report(self, csession_id: str, run_no: int, artifact: Frozen) -> bool:
        """Store an implicit done (headless ``result`` accepted as done): status ``done``,
        ``report_implicit`` 1, no summary or reason, the frozen ``result.md`` as artifact.
        Only while the run has no report and is not final; returns whether it was stored."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE runs SET report_status = 'done', report_implicit = 1, "
                "report_summary = NULL, report_reason = NULL, artifact_name = ?, "
                "artifact_path = ?, artifact_sha256 = ?, artifact_size = ?, "
                "artifact_ingest_seq = ?, reported_at = ? "
                "WHERE csession_id = ? AND run_no = ? AND report_status IS NULL "
                "AND ended_at IS NULL",
                (
                    artifact.name,
                    artifact.path,
                    artifact.sha256,
                    artifact.size,
                    artifact.ingest_seq,
                    format_ts(self.clock()),
                    csession_id,
                    run_no,
                ),
            )
            return cursor.rowcount == 1

    def finalize_run(
        self, csession_id: str, run_no: int, final: Final, *, report_status: Any = ANY
    ) -> bool:
        """The finalization compare-and-set (design.md, Data model): on ``ended_at IS NULL``
        (and, unless ``report_status`` is ``ANY``, the run's report status still being the
        one the end was mapped from, None for none), record the outcome, set the session's
        final state (``exited`` when the run has a report, else the outcome's state) and
        append the one final event. Returns whether this call won (False: the run was
        already final or its report status differs, nothing changed)."""
        report_check = "" if report_status is ANY else " AND report_status IS ?"
        report_param = () if report_status is ANY else (report_status,)
        with self.transaction() as conn:
            now = format_ts(self.clock())
            cursor = conn.execute(
                "UPDATE runs SET ended_at = ?, phase = ?, outcome = ?, end_reason = ?, "
                "exit_code = ?, cost_usd = ?, num_turns = ?, is_error = ?, "
                "message = COALESCE(?, message) "
                "WHERE csession_id = ? AND run_no = ? AND ended_at IS NULL" + report_check,
                (
                    now,
                    final.phase,
                    final.outcome,
                    final.end_reason,
                    final.exit_code,
                    final.cost_usd,
                    final.num_turns,
                    None if final.is_error is None else int(final.is_error),
                    final.message,
                    csession_id,
                    run_no,
                    *report_param,
                ),
            )
            if cursor.rowcount != 1:
                return False
            row = conn.execute(
                "SELECT s.state, s.activity, r.report_status FROM sessions s JOIN runs r "
                "ON r.csession_id = s.csession_id AND r.run_no = ? WHERE s.csession_id = ?",
                (run_no, csession_id),
            ).fetchone()
            if row is None:
                raise InternalError(f"no run {run_no} of session {csession_id}")
            before_state, before_activity, report_status = row
            state = "exited" if report_status is not None else final.state
            conn.execute(
                "UPDATE sessions SET state = ?, activity = NULL, needs_input_reason = NULL, "
                "state_since = CASE WHEN state IS ? THEN state_since ELSE ? END, updated_at = ? "
                "WHERE csession_id = ? AND current_run = ?",
                (state, state, now, now, csession_id, run_no),
            )
            labels = [
                f"{key}={value}"
                for key, value in conn.execute(
                    "SELECT key, value FROM labels WHERE csession_id = ? ORDER BY key, value",
                    (csession_id,),
                )
            ]
            conn.execute(
                "INSERT INTO events (csession_id, run_no, event_ts, from_state, to_state, "
                "from_activity, to_activity, needs_input_reason, cause, final, labels_json) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, 1, ?)",
                (
                    csession_id,
                    run_no,
                    now,
                    before_state,
                    state,
                    before_activity,
                    final.cause,
                    json.dumps(labels, ensure_ascii=False),
                ),
            )
        return True

    def run_record(self, csession_id: str, run_no: int) -> dict[str, Any]:
        """The run row joined with its session's ``forked_from``, ``claude_session_id`` and
        ``state`` (for the outcome object). ``InternalError`` when it does not exist."""
        cursor = self.conn.execute(
            "SELECT r.*, s.forked_from AS session_forked_from, "
            "s.claude_session_id AS session_claude_session_id, s.state AS session_state "
            "FROM runs r JOIN sessions s ON s.csession_id = r.csession_id "
            "WHERE r.csession_id = ? AND r.run_no = ?",
            (csession_id, run_no),
        )
        row = cursor.fetchone()
        if row is None:
            raise InternalError(f"no run {run_no} of session {csession_id}")
        names = [column[0] for column in cursor.description]
        return dict(zip(names, row, strict=True))

    # The hook path (hookevent). Call these inside one ``transaction()``; they read and write
    # only columns of schema 1.0, so a newer additive schema keeps working (design.md, Data
    # model: hook-event never migrates).

    def hook_run(self, csession_id: str) -> HookRun | None:
        """The session's current run as the hook path sees it (``forgotten`` set for a
        forgotten session), or None when the session is unknown or its current run row is
        missing."""
        row = self.conn.execute(
            "SELECT s.csession_id, r.run_no, s.state, s.activity, s.needs_input_reason, "
            "r.mode, r.session_started_at, r.report_status, r.backstop_used, "
            "r.ended_at IS NULL AND r.wrapper_pid IS NOT NULL, s.csession_dir, s.private_dir, "
            "s.forgotten_at IS NOT NULL, r.secret_sha256, r.outbox_dev_ino, r.artifacts_dev_ino "
            "FROM sessions s JOIN runs r "
            "ON r.csession_id = s.csession_id AND r.run_no = s.current_run "
            "WHERE s.csession_id = ?",
            (csession_id,),
        ).fetchone()
        if row is None:
            return None
        return HookRun(
            csession_id=row[0],
            run_no=row[1],
            state=row[2],
            activity=row[3],
            needs_input_reason=row[4],
            mode=row[5],
            session_started_at=row[6],
            report_status=row[7],
            backstop_used=bool(row[8]),
            adopted_and_open=bool(row[9]),
            csession_dir=row[10],
            private_dir=row[11],
            forgotten=bool(row[12]),
            secret_sha256=row[13],
            outbox_dev_ino=row[14],
            artifacts_dev_ino=row[15],
        )

    def prompt_submitted_after_report(self, run: HookRun) -> bool:
        """Whether the run has a ``UserPromptSubmit`` event with an id greater than its latest
        ``report`` event (any ``UserPromptSubmit`` event when it has none), which closes an
        escalation (design.md, State machine). Ordered by event id, not by timestamp, so a
        clock step cannot keep an escalation open."""
        row = self.conn.execute(
            "SELECT 1 FROM events WHERE csession_id = ? AND run_no = ? "
            "AND cause = 'UserPromptSubmit' AND id > COALESCE(("
            "SELECT MAX(id) FROM events WHERE csession_id = ? AND run_no = ? "
            "AND cause = 'report'), 0) LIMIT 1",
            (run.csession_id, run.run_no, run.csession_id, run.run_no),
        ).fetchone()
        return row is not None

    def record_session_start(
        self,
        run: HookRun,
        *,
        claude_session_id: str,
        source: str,
        transcript_path: str | None,
    ) -> None:
        """Record a SessionStart: a ``claude_ids`` row (an id already recorded keeps its first
        row), the session's current Claude id and, on the run's first SessionStart,
        ``session_started_at``."""
        now = format_ts(self.clock())
        self.conn.execute(
            "INSERT INTO claude_ids (csession_id, claude_session_id, run_no, source, "
            "transcript_path, seen_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (csession_id, claude_session_id) DO NOTHING",
            (run.csession_id, claude_session_id, run.run_no, source, transcript_path, now),
        )
        self.conn.execute(
            "UPDATE sessions SET claude_session_id = ? WHERE csession_id = ?",
            (claude_session_id, run.csession_id),
        )
        self.conn.execute(
            "UPDATE runs SET session_started_at = ? "
            "WHERE csession_id = ? AND run_no = ? AND session_started_at IS NULL",
            (now, run.csession_id, run.run_no),
        )

    def record_session_end_reason(self, run: HookRun, reason: str) -> None:
        """Record the reason of the run's latest SessionEnd, verbatim."""
        self.conn.execute(
            "UPDATE runs SET session_end_reason = ? WHERE csession_id = ? AND run_no = ?",
            (reason, run.csession_id, run.run_no),
        )

    def apply_state(
        self,
        run: HookRun,
        *,
        state: str,
        activity: str | None,
        needs_input_reason: str | None,
        cause: str,
        always: bool = False,
    ) -> int | None:
        """Store the run's new state. When ``state``, ``activity`` or ``needs_input_reason``
        changed, or with ``always``, also bump ``updated_at`` (and ``state_since`` for a new
        state) and append an ``events`` row carrying the session's labels; returns its id,
        else None.

        Report ingestion passes ``always``: its ``report`` event is written whenever the
        report status or ``reported_at`` changes (every ingestion), even when state, activity
        and reason do not (interface.md, State transitions); the escalation check
        (``prompt_submitted_after_report``) and post-state-change rely on it.
        """
        before = (run.state, run.activity, run.needs_input_reason)
        if (state, activity, needs_input_reason) == before and not always:
            return None
        now = format_ts(self.clock())
        self.conn.execute(
            "UPDATE sessions SET state = ?, activity = ?, needs_input_reason = ?, "
            "state_since = CASE WHEN state IS ? THEN state_since ELSE ? END, updated_at = ? "
            "WHERE csession_id = ?",
            (state, activity, needs_input_reason, state, now, now, run.csession_id),
        )
        labels = [
            f"{key}={value}"
            for key, value in self.conn.execute(
                "SELECT key, value FROM labels WHERE csession_id = ? ORDER BY key, value",
                (run.csession_id,),
            )
        ]
        cursor = self.conn.execute(
            "INSERT INTO events (csession_id, run_no, event_ts, from_state, to_state, "
            "from_activity, to_activity, needs_input_reason, cause, final, labels_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                run.csession_id,
                run.run_no,
                now,
                run.state,
                state,
                run.activity,
                activity,
                needs_input_reason,
                cause,
                json.dumps(labels, ensure_ascii=False),
            ),
        )
        return cursor.lastrowid

    def record_request(self, csession_id: str, run_no: int, request_id: str, result: str) -> bool:
        """Record a claimed outbox request and its result (``ingested``, a rejection code or
        ``discarded``). Returns False when ``request_id`` is already recorded: a request is
        processed at most once (design.md, Claim)."""
        cursor = self.conn.execute(
            "INSERT INTO requests (request_id, csession_id, run_no, result, ingested_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (request_id) DO NOTHING",
            (request_id, csession_id, run_no, result, format_ts(self.clock())),
        )
        return cursor.rowcount == 1

    def ingested_count(self, csession_id: str, run_no: int) -> int:
        """How many requests of the run were ingested (the latest one's ``<ingest_seq>``)."""
        (count,) = self.conn.execute(
            "SELECT COUNT(*) FROM requests WHERE csession_id = ? AND run_no = ? "
            "AND result = 'ingested'",
            (csession_id, run_no),
        ).fetchone()
        return int(count)

    def latest_ingested(self, csession_id: str, run_no: int) -> str | None:
        """The newest (largest UUIDv7) ``request_id`` of the run that was ingested, or None:
        an older request never replaces a newer report."""
        (latest,) = self.conn.execute(
            "SELECT MAX(request_id) FROM requests WHERE csession_id = ? AND run_no = ? "
            "AND result = 'ingested'",
            (csession_id, run_no),
        ).fetchone()
        return latest

    def set_report(
        self,
        csession_id: str,
        run_no: int,
        *,
        status: str,
        summary: str | None,
        reason: str | None,
        artifact: Frozen | None,
    ) -> None:
        """Store an ingested report as the run's report (the latest one wins); the run's
        artifact becomes this report's, or null. ``reported_at`` is now."""
        now = format_ts(self.clock())
        cursor = self.conn.execute(
            "UPDATE runs SET report_status = ?, report_implicit = 0, report_summary = ?, "
            "report_reason = ?, artifact_name = ?, artifact_path = ?, artifact_sha256 = ?, "
            "artifact_size = ?, artifact_ingest_seq = ?, reported_at = ? "
            "WHERE csession_id = ? AND run_no = ?",
            (
                status,
                summary,
                reason,
                None if artifact is None else artifact.name,
                None if artifact is None else artifact.path,
                None if artifact is None else artifact.sha256,
                None if artifact is None else artifact.size,
                None if artifact is None else artifact.ingest_seq,
                now,
                csession_id,
                run_no,
            ),
        )
        if cursor.rowcount != 1:
            raise InternalError(f"no run {run_no} of session {csession_id}")

    def take_backstop(self, run: HookRun) -> bool:
        """Mark the run's Stop backstop used, once per run, while it has no report. Returns
        whether this call took it."""
        cursor = self.conn.execute(
            "UPDATE runs SET backstop_used = 1 WHERE csession_id = ? AND run_no = ? "
            "AND backstop_used = 0 AND report_status IS NULL",
            (run.csession_id, run.run_no),
        )
        return cursor.rowcount == 1


# Run and session columns ``exec`` sets after allocation (``update_run``, ``update_session``).
RUN_FIELDS = frozenset(
    {
        "cwd",
        "phase",
        "permission_profile",
        "permission_profile_snapshot",
        "sandbox_active",
        "started_at",
        "child_pid",
        "child_start",
        "backstop_used",
        "pending_cause",
        "message",
    }
)
SESSION_FIELDS = frozenset({"tmp_dir", "git_dir"})


@dataclass(frozen=True)
class NewRun:
    """What launch step 4 records for a new session's run 1 (``Store.allocate``). JSON fields
    hold no secret values and no prompt text (design.md, Data model)."""

    state_root: str
    kind: str
    reads_third_party: bool
    mode: str
    repo: str | None
    model: str | None
    skill: str | None
    has_prompt: bool
    spec: dict[str, Any]
    resolved: dict[str, Any]
    config_hash: str | None
    invocation: dict[str, Any]
    hook_interpreter: str
    config_path: str | None
    wrapper_pid: int
    wrapper_start: str
    labels: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    outcome_file: str | None = None


@dataclass(frozen=True)
class Final:
    """A run's final record (``Store.finalize_run``). ``state`` is the outcome's final state;
    the session's state becomes ``exited`` instead when the run has a report."""

    phase: str
    outcome: str
    end_reason: str
    exit_code: int
    state: str
    cause: str
    cost_usd: float | None = None
    num_turns: int | None = None
    is_error: bool | None = None
    message: str | None = None


@dataclass(frozen=True)
class HookRun:
    """The fields of a session and its current run that ``hook-event`` reads."""

    csession_id: str
    run_no: int
    state: str
    activity: str | None
    needs_input_reason: str | None
    mode: str | None
    session_started_at: str | None
    report_status: str | None
    backstop_used: bool
    # Adopted (wrapper set: claude can have been spawned) and not ended.
    adopted_and_open: bool
    csession_dir: str | None
    private_dir: str | None
    forgotten: bool
    # What outbox ingestion checks requests against (NULL until exec records them).
    secret_sha256: str | None = None
    outbox_dev_ino: str | None = None
    artifacts_dev_ino: str | None = None


@dataclass(frozen=True)
class StatusRecord:
    """``Store.status_record``: raw rows (column name -> value) for ``status``."""

    session: dict[str, Any]
    # The selected run (None: no such run) and the session's current run.
    run: dict[str, Any] | None
    current_run: dict[str, Any] | None
    runs: int
    labels: list[str]
    claude_ids: list[dict[str, Any]]
    events: list[dict[str, Any]]


@dataclass(frozen=True)
class ListRecord:
    """``Store.list_records``: one session for ``list``."""

    session: dict[str, Any]
    run: dict[str, Any] | None
    labels: list[str]
    run_kinds: frozenset[str]


# ``status`` shows this many of a session's latest events (design.md, Data model).
STATUS_EVENTS = 20
# The largest value SQLite stores as an INTEGER; larger Python ints cannot be bound.
MAX_SQLITE_INTEGER = 2**63 - 1


def _lenient_text(raw: bytes) -> str:
    """TEXT values for the read views: bytes that are not UTF-8 become U+FFFD, never an
    error, so status and list print any stored value."""
    return raw.decode("utf-8", errors="replace")


def _one_dict(conn: sqlite3.Connection, sql: str, *params: Any) -> dict[str, Any] | None:
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([column[0] for column in cursor.description], row, strict=True))


def _all_dicts(conn: sqlite3.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _labels(conn: sqlite3.Connection, csession_id: str) -> list[str]:
    return [
        f"{key}={value}"
        for key, value in conn.execute(
            "SELECT key, value FROM labels WHERE csession_id = ? ORDER BY key, value",
            (csession_id,),
        )
    ]


def open_state(**kwargs: Any) -> Store:
    """Open ``state.db`` in the default state directory (see ``Store.open``)."""
    return Store.open(db_path(state_dir()), **kwargs)
