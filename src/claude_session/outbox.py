"""The ``report`` command (write side) and outbox ingestion (design.md, Outbox and ingestion;
interface.md, ``report``).

Everything the agent controls is hostile: the outbox files, their names, the summary, reason
and artifact bytes, and any symlink, hard link, FIFO or directory planted in ``outbox/`` or
``artifacts/``. Both sides open directories with ``O_DIRECTORY | O_NOFOLLOW`` and files with
``O_NOFOLLOW | O_NONBLOCK``, require regular files with one link, size-check with ``fstat``
before reading, and read through one descriptor, so what is checked is what is used.

- ``file_report`` (the ``report`` command): validates the report, reads the run's facts from
  the protected ``run.json`` (never ``state.db``) and writes ``outbox/<uuidv7>.json``
  atomically.
- ``Drain`` (ingestion, on the hook path): claims requests by renaming them into the run's
  private ``claimed/`` directory, pre-checks them outside any transaction (schema, size,
  secret, artifact copy, hash and scan), then records them inside the caller's transaction
  (``Drain.record``) and cleans up after the commit (``Drain.finish``).

Standard library only, plus ``errors`` and ``fsutil``: ``hook-event`` imports this module
(design.md, Hook-event fast path), so it never imports ``ids``, ``sessiondir`` or ``config``;
the grammars it needs are restated here and tested equal.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from . import fsutil
from .errors import FailedError, UsageError

STATUSES = ("done", "blocked", "failed", "needs-input")
REQUEST_VERSION = 1
REQUEST_FIELDS = frozenset(
    {
        "v",
        "run",
        "secret",
        "status",
        "summary",
        "reason",
        "artifact",
        "artifact_sha256",
        "created",
    }
)
# The request file is at most 4 x summary_max_bytes + this (interface.md, report).
REQUEST_OVERHEAD = 16 * 1024
# A drain reads at most DRAIN_MAX_READ directory entries of outbox/ (and of claimed/),
# claims at most DRAIN_MAX_ENTRIES requests (junk and temporary names do not count) and
# processes at most DRAIN_MAX_REQUESTS of them (design.md, Claim).
DRAIN_MAX_READ = 4096
DRAIN_MAX_ENTRIES = 64
DRAIN_MAX_REQUESTS = 16
# Seconds a drain may spend claiming and checking: half the 60 s timeout of the hooks that
# drain (settings_builder.HOOKS, tested equal); the rest waits for the next drain.
DRAIN_BUDGET = 30.0
# run.json is written by exec; anything larger is not one.
RUN_JSON_MAX_BYTES = 1024 * 1024
SECRET_CHARS = 64
# Scan copies and report temporary files older than this are leftovers of a canceled hook
# or report (hook timeouts are 60 s).
SCAN_LEFTOVER_AGE = 600.0

# Names (sessiondir's constants; sessiondir is not on the hook path, so they are restated
# here and tested equal).
RUN_JSON = "run.json"
SECRET = "secret"
OUTBOX = "outbox"
ARTIFACTS = "artifacts"
SESSIONS = "sessions"
# Private run directory entries (design.md, Outbox and ingestion; interface.md, Concepts).
CLAIMED = "claimed"
SCAN = "scan"
ARTIFACT = "artifact"

# interface.md, Identifiers (ids.ARTIFACT_NAME and ids.SESSION_ID, tested equal).
ARTIFACT_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
SESSION_ID = re.compile(r"cs-[a-z2-7]{8}")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# A UUIDv7 in its canonical lowercase form; request files are named ``<uuid>.json``.
UUID7 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
REQUEST_SUFFIX = ".json"
# report's temporary file in outbox/: a reserved name a drain never claims (design.md,
# Claim).
TEMP_NAME = re.compile(r"\.tmp-[0-9a-f]{16}")
CREATED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")

# Rejection reasons (interface.md, report) and the fixed, trusted sentence the PostToolUse
# hook gives the agent for each (design.md, Hook output).
REJECTION_DETAILS: Mapping[str, str] = {
    "bad_secret": "The request does not carry this run's secret.",
    "not_live": "The run is no longer live.",
    "stale_run": "The request is for another run of this session.",
    "artifact_changed": "The artifact changed after claude-session report read it.",
    "artifact_missing": "The artifact does not exist in the artifacts directory.",
    "outside_dir": (
        "The outbox or artifacts directory is not the one claude-session created for this run."
    ),
    "not_regular_file": (
        "The request or the artifact is not a regular file with a single link (symbolic "
        "links, hard links, directories and special files are refused)."
    ),
    "too_large": "The request, the summary, the reason or the artifact is over its size limit.",
    "contains_secret": (
        "The summary, the reason or the artifact contains a secret value; remove it."
    ),
    "malformed": "The request is not a valid report request; file it with claude-session report.",
}
REJECTIONS = frozenset(REJECTION_DETAILS)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_CHUNK = 1 << 16

# ``contains_secret`` over some bytes (summary, reason or the artifact copy).
Scanner = Callable[[bytes], bool]


def rejection_output(code: str) -> dict[str, Any]:
    """The PostToolUse output for a rejected request (design.md, Hook output)."""
    detail = REJECTION_DETAILS[code]
    reason = (
        f"claude-session report rejected ({code}): {detail} Fix the problem and file the "
        "report again."
    )
    return {"decision": "block", "reason": reason}


# Shared helpers


def new_request_id(
    now_ns: Callable[[], int] = time.time_ns,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> str:
    """A UUIDv7 (RFC 9562): 48-bit Unix milliseconds, then 12 bits of sub-millisecond time
    (method 3), so names made by one clock sort in creation order, then 62 random bits."""
    ns = now_ns()
    ms = ns // 1_000_000
    sub_ms = (ns % 1_000_000) * 4096 // 1_000_000
    rand = int.from_bytes(random_bytes(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | sub_ms << 64 | 0b10 << 62 | rand
    return str(uuid.UUID(int=value))


def is_request_name(name: str) -> bool:
    """Whether ``name`` is ``<uuidv7>.json``."""
    stem, suffix = name[: -len(REQUEST_SUFFIX)], name[-len(REQUEST_SUFFIX) :]
    return suffix == REQUEST_SUFFIX and UUID7.fullmatch(stem) is not None


def temp_name() -> str:
    """A fresh reserved temporary name for ``report``'s request file in ``outbox/``."""
    return f".tmp-{secrets.token_hex(8)}"


def is_temp_name(name: str) -> bool:
    return TEMP_NAME.fullmatch(name) is not None


def is_artifact_name(name: str) -> bool:
    return ARTIFACT_NAME.fullmatch(name) is not None and name not in (".", "..")


def _has_secret(data: bytes, secret: str) -> bool:
    """Whether the run secret (lowercase hex) occurs in ``data`` in any letter case."""
    return secret.encode("ascii") in data.lower()


def secret_sha256(value: str) -> str:
    """SHA-256 of the secret as presented (sessiondir.secret_sha256, tested equal)."""
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _text_problem(text: str) -> str | None:
    """Why ``text`` cannot be a summary or reason: not encodable as UTF-8 (a lone surrogate)
    or containing NUL. None when it can."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "is not valid UTF-8"
    if "\0" in text:
        return "contains a NUL character"
    return None


def _open_dir_at(dir_fd: int, name: str) -> int:
    """Open the directory ``name`` in ``dir_fd`` without following a symlink; it must be a
    directory owned by the effective user (``OSError`` otherwise)."""
    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    try:
        fsutil.check_private_dir_fd(fd, name)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _dir_id(fd: int) -> str:
    info = os.fstat(fd)
    return f"{info.st_dev}:{info.st_ino}"


class _BadFile(Exception):
    """A file that is not a regular single-link file of ours, or over its cap."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # not_regular_file or too_large


def _open_regular(dir_fd: int, name: str, cap: int) -> int:
    """Open ``name`` in ``dir_fd`` for reading: without following a symlink and without
    waiting on a FIFO, a regular file with one link owned by us, at most ``cap`` bytes by
    ``fstat`` (the caller checks the size again while reading). ``FileNotFoundError`` when
    missing, ``_BadFile`` for the rest."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _BadFile("not_regular_file") from exc  # ELOOP (a symlink), ENXIO, EACCES, ...
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise _BadFile("not_regular_file")
        if info.st_size > cap:
            raise _BadFile("too_large")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_regular(dir_fd: int, name: str, cap: int) -> bytes:
    """Read ``name`` in ``dir_fd`` (``_open_regular``), at most ``cap`` bytes."""
    fd = _open_regular(dir_fd, name, cap)
    try:
        chunks: list[bytes] = []
        size = 0
        while size <= cap:
            chunk = os.read(fd, min(_READ_CHUNK, cap + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > cap:
            raise _BadFile("too_large")  # it grew after the fstat
        return b"".join(chunks)
    finally:
        os.close(fd)


# The report command (write side)


@dataclass(frozen=True)
class RunInfo:
    """The facts ``report`` reads from ``run.json``."""

    csession_id: str
    run: int
    state_root: str
    summary_max_bytes: int
    artifact_max_bytes: int
    outbox: str  # "<dev>:<ino>"
    artifacts: str


@dataclass(frozen=True)
class Queued:
    """A request written to the outbox."""

    request_id: str
    status: str
    artifact: str | None

    def as_json(self) -> dict[str, Any]:
        """schemas/cli/report.json."""
        return {
            "queued": True,
            "request_id": self.request_id,
            "status": self.status,
            "artifact": self.artifact,
        }


def _usage(message: str) -> UsageError:
    return UsageError(f"report: {message}")


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _dir_identity(value: object) -> str | None:
    if not isinstance(value, dict) or set(value) != {"dev", "ino"}:
        return None
    dev, ino = value["dev"], value["ino"]
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (dev, ino)):
        return None
    return f"{dev}:{ino}"


def parse_run_json(data: bytes) -> RunInfo:
    """Validate ``run.json`` as ``sessiondir.build_run_json`` writes it (``UsageError``)."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _usage("run.json is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise _usage("run.json is not a JSON object")
    sid = doc.get("csession_id")
    root = doc.get("state_root")
    outbox, artifacts = _dir_identity(doc.get("outbox")), _dir_identity(doc.get("artifacts"))
    if (
        not isinstance(sid, str)
        or SESSION_ID.fullmatch(sid) is None
        or not _positive_int(doc.get("run"))
        or not isinstance(root, str)
        or not os.path.isabs(root)
        or "\0" in root
        or not _positive_int(doc.get("summary_max_bytes"))
        or not _positive_int(doc.get("artifact_max_bytes"))
        or outbox is None
        or artifacts is None
    ):
        raise _usage("run.json does not describe a claude-session run")
    return RunInfo(
        csession_id=sid,
        run=doc["run"],
        state_root=root,
        summary_max_bytes=doc["summary_max_bytes"],
        artifact_max_bytes=doc["artifact_max_bytes"],
        outbox=outbox,
        artifacts=artifacts,
    )


class Readable(Protocol):
    def read(self, size: int = ..., /) -> bytes | None: ...


def _read_stream(stream: Readable, cap: int, flag: str) -> bytes:
    """Read ``stream`` (stdin) to its end, at most ``cap`` bytes."""
    chunks: list[bytes] = []
    size = 0
    while size <= cap:
        chunk = stream.read(min(_READ_CHUNK, cap + 1 - size))
        if chunk is None:  # a non-blocking stream with no data yet
            time.sleep(0.01)
            continue
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > cap:
        raise _usage(f"{flag} -: larger than summary_max_bytes ({cap} bytes)")
    return b"".join(chunks)


def _read_text_file(path: str, cap: int, flag: str, stdin: Readable) -> str:
    """``--summary-file``/``--reason-file``: ``-`` (stdin) or a readable regular file, opened
    with ``O_NOFOLLOW`` (a symlink, FIFO, device or directory is refused), at most ``cap``
    bytes, valid UTF-8."""
    if path == "-":
        data = _read_stream(stdin, cap, flag)
    else:
        try:
            fd = os.open(path, _FILE_FLAGS)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise _usage(f"{flag} {path}: is a symbolic link; pass the file itself") from exc
            raise _usage(f"{flag} {path}: {exc.strerror}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _usage(f"{flag} {path}: not a regular file")
            if info.st_size > cap:
                raise _usage(f"{flag} {path}: larger than summary_max_bytes ({cap} bytes)")
            data = b""
            while len(data) <= cap:
                chunk = os.read(fd, min(_READ_CHUNK, cap + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            if len(data) > cap:
                raise _usage(f"{flag} {path}: larger than summary_max_bytes ({cap} bytes)")
        except OSError as exc:
            raise _usage(f"{flag} {path}: {exc.strerror}") from exc
        finally:
            os.close(fd)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _usage(f"{flag}: the text is not valid UTF-8") from exc


def _check_text(text: str, what: str, cap: int) -> None:
    problem = _text_problem(text)
    if problem is not None:
        raise _usage(f"the {what} {problem}")
    if len(text.encode("utf-8")) > cap:
        raise _usage(f"the {what} is larger than summary_max_bytes ({cap} bytes)")
    if not text.strip():
        raise _usage(f"the {what} is empty")


def _artifact_name(value: str, csession_dir: str) -> str:
    """``--artifact``: a bare name, or a path whose lexical parent (no symlink resolution;
    repeated slashes collapsed) is exactly ``$CSESSION_DIR/artifacts``. Only the name is used
    afterwards; the file is opened relative to the verified ``artifacts/`` descriptor."""
    if "/" not in value:
        name = value
    else:
        parent, name = os.path.split(value)
        if any(part in (".", "..") for part in parent.split("/")):
            raise _usage(f"--artifact {value}: the path must be $CSESSION_DIR/artifacts/<name>")
        if os.path.normpath(parent) != os.path.normpath(os.path.join(csession_dir, ARTIFACTS)):
            raise _usage(f"--artifact {value}: the path must be $CSESSION_DIR/artifacts/<name>")
    if not is_artifact_name(name):
        raise _usage(
            f"--artifact {value}: the name must match ^{ARTIFACT_NAME.pattern}$, not . or .."
        )
    return name


def _open_checked_dir(session_fd: int, name: str, expected: str) -> int:
    """``outbox/`` or ``artifacts/`` for ``report``: no symlink, ours, and the device and
    inode recorded in ``run.json`` (``UsageError``)."""
    try:
        fd = _open_dir_at(session_fd, name)
    except OSError as exc:
        raise _usage(f"$CSESSION_DIR/{name} is not the directory claude-session created") from exc
    if _dir_id(fd) != expected:
        os.close(fd)
        raise _usage(f"$CSESSION_DIR/{name} is not the directory claude-session created")
    return fd


def _read_session_file(session_fd: int, name: str, cap: int) -> bytes:
    try:
        return _read_regular(session_fd, name, cap)
    except FileNotFoundError as exc:
        raise _usage(f"$CSESSION_DIR/{name} is missing: not inside a claude-session run") from exc
    except (_BadFile, OSError) as exc:
        raise _usage(f"$CSESSION_DIR/{name} is not a regular single-link file") from exc


def file_report(
    *,
    status: str,
    summary: str | None,
    summary_file: str | None,
    reason_file: str | None,
    artifact: str | None,
    environ: Mapping[str, str],
    stdin: Readable,
    clock: Callable[[], datetime] | None = None,
    request_id: Callable[[], str] = new_request_id,
) -> Queued:
    """Validate a report and queue it in the outbox (interface.md, ``report``). Never opens
    ``state.db``. Validation failures raise ``UsageError`` (exit 2); a failed write raises
    ``FailedError``."""
    if status not in STATUSES:
        raise _usage(f"unknown status {status!r}")
    sid = environ.get("CSESSION_ID", "")
    csession_dir = environ.get("CSESSION_DIR", "")
    if SESSION_ID.fullmatch(sid) is None or not os.path.isabs(csession_dir):
        raise _usage("must run inside a claude-session session (CSESSION_ID and CSESSION_DIR)")
    if "\0" in csession_dir:
        raise _usage("CSESSION_DIR is not a path")
    if summary_file == "-" and reason_file == "-":
        raise _usage("--summary-file and --reason-file cannot both read stdin")
    real_dir = os.path.realpath(csession_dir)
    try:
        session_fd = fsutil.open_dir_nofollow(real_dir)
    except OSError as exc:
        raise _usage(f"CSESSION_DIR {csession_dir} cannot be opened") from exc
    fds = [session_fd]
    try:
        try:
            fsutil.check_private_dir_fd(session_fd, real_dir)
        except OSError as exc:
            raise _usage(f"CSESSION_DIR {csession_dir} is not ours") from exc
        info = parse_run_json(_read_session_file(session_fd, RUN_JSON, RUN_JSON_MAX_BYTES))
        expected = os.path.join(os.path.normpath(info.state_root), SESSIONS, sid)
        if info.csession_id != sid or real_dir != expected:
            raise _usage("CSESSION_DIR does not match CSESSION_ID")
        secret = _read_session_file(session_fd, SECRET, SECRET_CHARS + 1).decode("ascii", "replace")
        if SHA256_HEX.fullmatch(secret) is None:
            raise _usage("$CSESSION_DIR/secret is not a run secret")
        outbox_fd = _open_checked_dir(session_fd, OUTBOX, info.outbox)
        fds.append(outbox_fd)
        artifacts_fd = _open_checked_dir(session_fd, ARTIFACTS, info.artifacts)
        fds.append(artifacts_fd)

        cap = info.summary_max_bytes
        summary_text = summary
        if summary_file is not None:
            summary_text = _read_text_file(summary_file, cap, "--summary-file", stdin)
        reason_text = None
        if reason_file is not None:
            reason_text = _read_text_file(reason_file, cap, "--reason-file", stdin)
        if summary_text is not None:
            _check_text(summary_text, "summary", cap)
        if reason_text is not None:
            _check_text(reason_text, "reason", cap)
        if status == "done" and summary_text is None:
            raise _usage("report done requires a non-empty --summary or --summary-file")
        if status != "done" and reason_text is None:
            raise _usage(f"report {status} requires --reason-file")

        name = digest = None
        data = b""
        if artifact is not None:
            name = _artifact_name(artifact, csession_dir)
            try:
                data = _read_regular(artifacts_fd, name, info.artifact_max_bytes)
            except FileNotFoundError as exc:
                missing = f"--artifact {artifact}: no such file in $CSESSION_DIR/artifacts"
                raise _usage(missing) from exc
            except _BadFile as exc:
                if exc.reason == "too_large":
                    raise _usage(
                        f"--artifact {artifact}: larger than artifact_max_bytes "
                        f"({info.artifact_max_bytes} bytes)"
                    ) from exc
                raise _usage(
                    f"--artifact {artifact}: not a regular file with a single link "
                    "(symbolic links and hard links are refused)"
                ) from exc
            digest = hashlib.sha256(data).hexdigest()
        # The run secret is the one secret value report can see (refused in any letter case,
        # as ingestion does); the full contains_secret scan (every configured value) runs at
        # ingestion.
        for what, blob in (
            ("summary", (summary_text or "").encode("utf-8")),
            ("reason", (reason_text or "").encode("utf-8")),
            ("artifact", data),
        ):
            if _has_secret(blob, secret):
                raise _usage(f"the {what} contains a secret value (the run secret); remove it")

        created = (clock or (lambda: datetime.now(UTC)))()
        request = {
            "v": REQUEST_VERSION,
            "run": info.run,
            "secret": secret,
            "status": status,
            "summary": summary_text,
            "reason": reason_text,
            "artifact": name,
            "artifact_sha256": digest,
            "created": created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 4 * cap + REQUEST_OVERHEAD:
            raise _usage("the report is larger than the request size limit")
        rid = request_id()
        _write_request(outbox_fd, rid, encoded)
        return Queued(request_id=rid, status=status, artifact=name)
    finally:
        for fd in fds:
            os.close(fd)


def _write_request(outbox_fd: int, rid: str, data: bytes) -> None:
    """Write ``outbox/<rid>.json`` atomically: the temporary file is created in ``outbox/``
    itself under a reserved name a drain never claims, then renamed within ``outbox/``. (In
    the sandbox ``outbox/`` and ``artifacts/`` are separate bind mounts, so a rename between
    them fails with ``EXDEV``.)"""
    tmp_dir_fd = outbox_fd
    tmp_name = temp_name()
    final = rid + REQUEST_SUFFIX
    try:
        fd = os.open(tmp_name, _CREATE_FLAGS, 0o600, dir_fd=tmp_dir_fd)
    except OSError as exc:
        raise FailedError("report: cannot write the request", detail=str(exc.strerror)) from exc
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_name, final, src_dir_fd=tmp_dir_fd, dst_dir_fd=outbox_fd)
        os.fsync(outbox_fd)
    except OSError as exc:
        try:
            os.unlink(tmp_name, dir_fd=tmp_dir_fd)
        except OSError:
            pass
        raise FailedError("report: cannot write the request", detail=str(exc.strerror)) from exc


# Ingestion (the hook path; exec's final drain reuses it)


@dataclass(frozen=True)
class Limits:
    """The run's caps, from the private ``hookconf.json`` (never from an agent-writable
    file)."""

    summary_max_bytes: int
    artifact_max_bytes: int


@dataclass(frozen=True)
class Request:
    """A request that passed the schema and size checks."""

    run: int
    secret: str = field(repr=False)
    status: str
    summary: str | None = field(repr=False)
    reason: str | None = field(repr=False)
    artifact: str | None
    artifact_sha256: str | None


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise ValueError(f"invalid JSON constant {name}")


def parse_request(data: bytes, limits: Limits) -> Request | str:
    """Validate a request file's bytes (design.md, Request schema). Returns the request or a
    rejection code (``too_large`` or ``malformed``)."""
    cap = limits.summary_max_bytes
    if len(data) > 4 * cap + REQUEST_OVERHEAD:
        return "too_large"
    try:
        doc = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_no_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "malformed"
    if not isinstance(doc, dict) or set(doc) != REQUEST_FIELDS:
        return "malformed"
    version, run, secret = doc["v"], doc["run"], doc["secret"]
    status, summary, reason = doc["status"], doc["summary"], doc["reason"]
    artifact, digest, created = doc["artifact"], doc["artifact_sha256"], doc["created"]
    if (
        type(version) is not int
        or version != REQUEST_VERSION
        or not _positive_int(run)
        or not isinstance(secret, str)
        or SHA256_HEX.fullmatch(secret) is None
        or status not in STATUSES
        or not isinstance(created, str)
        or CREATED.fullmatch(created) is None
    ):
        return "malformed"
    for text in (summary, reason):
        if text is None:
            continue
        if not isinstance(text, str) or _text_problem(text) is not None or not text.strip():
            return "malformed"
    if (status == "done" and summary is None) or (status != "done" and reason is None):
        return "malformed"
    if artifact is None:
        if digest is not None:
            return "malformed"
    elif (
        not isinstance(artifact, str)
        or not is_artifact_name(artifact)
        or not isinstance(digest, str)
        or SHA256_HEX.fullmatch(digest) is None
    ):
        return "malformed"
    for text in (summary, reason):
        if text is not None and len(text.encode("utf-8")) > cap:
            return "too_large"
    return Request(
        run=run,
        secret=secret,
        status=status,
        summary=summary,
        reason=reason,
        artifact=artifact,
        artifact_sha256=digest,
    )


@dataclass
class Item:
    """One request of a drain, oldest to newest by name."""

    request_id: str
    rejection: str | None = None  # a rejection code found before the transaction
    request: Request | None = None
    unchecked: bool = False  # over the drain cap: discarded without being processed
    superseded: bool = False  # older than the winner: discarded without being read
    deferred: bool = False  # past the drain's deadline: left in claimed/ for the next drain
    copy_name: str | None = None  # the artifact copy in scan/
    artifact_size: int | None = None
    frozen: bool = False
    result: str | None = None  # what was recorded: ingested, a rejection code, discarded
    duplicate: bool = False  # request_id already recorded (ingested at most once)
    gone: bool = False  # removed by a concurrent drain before it could be read

    @property
    def name(self) -> str:
        return self.request_id + REQUEST_SUFFIX

    @property
    def processed(self) -> bool:
        """Read and checked by this drain (whatever the outcome)."""
        return not (self.unchecked or self.superseded or self.deferred or self.gone)


@dataclass(frozen=True)
class Frozen:
    """A frozen artifact (``runs.artifact_*``)."""

    name: str
    path: str
    sha256: str
    size: int
    ingest_seq: int


class IngestStore(Protocol):
    """What ``Drain.record`` needs from ``store.Store`` (inside its transaction)."""

    def record_request(
        self, csession_id: str, run_no: int, request_id: str, result: str
    ) -> bool: ...

    def ingested_count(self, csession_id: str, run_no: int) -> int: ...

    def latest_ingested(self, csession_id: str, run_no: int) -> str | None: ...

    def set_report(
        self,
        csession_id: str,
        run_no: int,
        *,
        status: str,
        summary: str | None,
        reason: str | None,
        artifact: Frozen | None,
    ) -> None: ...


class OutboxDirError(Exception):
    """``outbox/`` is missing or not the directory ``exec`` created (``outside_dir``)."""


@dataclass
class Drain:
    """One drain of a run's outbox: requests claimed and pre-checked, waiting to be recorded
    in the caller's transaction. Always call ``finish`` (or use it as a context manager)."""

    csession_id: str
    run_no: int
    private_run_dir: str
    items: list[Item]
    removed_malformed: int = 0
    # Why recording failed (an exception type name), set by the caller that rolled it back.
    failure: str | None = None
    _fds: dict[str, int] = field(default_factory=dict)
    recorded: bool = False
    _deadline: float = float("inf")
    _clock: Callable[[], float] = time.monotonic

    # Preparation (before any transaction)

    @classmethod
    def prepare(
        cls,
        *,
        csession_id: str,
        run_no: int,
        csession_dir: str,
        private_run_fd: int,
        private_run_dir: str,
        outbox_dev_ino: str,
        artifacts_dev_ino: str,
        secret_sha256: str,
        limits: Callable[[], Limits],
        scanner: Scanner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Drain:
        """Claim the run's outbox requests (and pick up those claimed earlier but never
        recorded) and pre-check them, newest first, until one passes every check (the
        winner). ``private_run_fd`` is the verified private run directory; it stays owned by
        the caller. ``limits`` is called only when there is a request to check. ``scanner``
        is ``contains_secret``. Claiming and checking stop ``DRAIN_BUDGET`` seconds (by
        ``clock``) after the start; what is left waits for the next drain.

        Raises ``OutboxDirError`` when ``outbox/`` cannot be trusted (nothing is claimed) and
        ``OSError`` for an unusable private directory."""
        drain = cls(csession_id, run_no, private_run_dir, [])
        drain._clock = clock
        drain._deadline = clock() + DRAIN_BUDGET
        try:
            drain._prepare(
                csession_dir=csession_dir,
                private_run_fd=private_run_fd,
                outbox_dev_ino=outbox_dev_ino,
                artifacts_dev_ino=artifacts_dev_ino,
                secret_sha256=secret_sha256,
                limits=limits,
                scanner=scanner,
            )
        except BaseException:
            drain.finish()
            raise
        return drain

    def _past_deadline(self) -> bool:
        return self._clock() > self._deadline

    def _prepare(
        self,
        *,
        csession_dir: str,
        private_run_fd: int,
        outbox_dev_ino: str,
        artifacts_dev_ino: str,
        secret_sha256: str,
        limits: Callable[[], Limits],
        scanner: Scanner | None,
    ) -> None:
        session_fd = self._open_session_dir(csession_dir)
        self._fds["session"] = session_fd
        try:
            outbox_fd = _open_dir_at(session_fd, OUTBOX)
        except OSError as exc:
            raise OutboxDirError("outbox/ cannot be opened safely") from exc
        self._fds["outbox"] = outbox_fd
        if _dir_id(outbox_fd) != outbox_dev_ino:
            raise OutboxDirError("outbox/ is not the directory exec created")
        claimed_fd = fsutil.ensure_private_dir_at(private_run_fd, CLAIMED)
        self._fds["claimed"] = claimed_fd
        names = self._claim(outbox_fd, claimed_fd)
        if not names:
            return
        names = sorted(names)  # UUIDv7 names sort in creation order
        unchecked = names[:-DRAIN_MAX_REQUESTS]
        self.items = [Item(name[: -len(REQUEST_SUFFIX)], unchecked=True) for name in unchecked]
        checked = [Item(name[: -len(REQUEST_SUFFIX)]) for name in names[-DRAIN_MAX_REQUESTS:]]
        self.items += checked
        caps = limits()
        scan_fd = fsutil.ensure_private_dir_at(private_run_fd, SCAN)
        self._fds["scan"] = scan_fd
        _remove_leftovers(scan_fd, self._past_deadline)
        self._fds["private_run"] = os.dup(private_run_fd)
        winner_found = False
        for item in reversed(checked):  # newest first: the first valid one wins
            if winner_found:
                item.superseded = True
            elif self._past_deadline():
                item.deferred = True
            else:
                self._check(item, caps, secret_sha256, artifacts_dev_ino, scanner)
                winner_found = item.request is not None

    @staticmethod
    def _open_session_dir(csession_dir: str) -> int:
        """``<state>/sessions/<id>``: the trusted parent is resolved once, the session
        directory itself is opened without following a symlink and must be ours."""
        normalized = os.path.normpath(csession_dir)
        name = os.path.basename(normalized)
        if not os.path.isabs(normalized) or SESSION_ID.fullmatch(name) is None:
            raise OutboxDirError("the session directory is not a session path")
        try:
            parent_fd = fsutil.open_dir_nofollow(os.path.realpath(os.path.dirname(normalized)))
        except OSError as exc:
            raise OutboxDirError("the sessions directory cannot be opened") from exc
        try:
            return _open_dir_at(parent_fd, name)
        except OSError as exc:
            raise OutboxDirError("the session directory cannot be opened safely") from exc
        finally:
            os.close(parent_fd)

    def _claim(self, outbox_fd: int, claimed_fd: int) -> list[str]:
        """Rename up to ``DRAIN_MAX_ENTRIES`` outbox requests into ``claimed/`` (only the
        process whose rename succeeds owns a request) and return every request name now in
        ``claimed/``. Of at most ``DRAIN_MAX_READ`` entries read, other names (and
        directories) are malformed and removed, without counting against the claim cap;
        ``report``'s temporary names are left alone until they are leftovers. Past the
        deadline nothing more is claimed or removed (a junk tree is left partly removed);
        the rest waits for the next drain."""
        claims = 0
        now = time.time()
        stop = self._past_deadline
        for name, is_dir in _entries(outbox_fd):
            if stop():
                break
            if is_temp_name(name):
                _remove_leftover(outbox_fd, name, now, stop)
                continue
            if not is_request_name(name) or is_dir:
                if _remove_entry(outbox_fd, name, stop):
                    self.removed_malformed += 1
                continue
            if claims >= DRAIN_MAX_ENTRIES:
                continue
            claims += 1
            try:
                os.rename(name, name, src_dir_fd=outbox_fd, dst_dir_fd=claimed_fd)
            except FileNotFoundError:
                continue  # claimed by a concurrent drain
        names = []
        for name, is_dir in _entries(claimed_fd):
            if is_dir:
                # A directory swapped in between the check and the rename: never processed.
                if _remove_tree(claimed_fd, name, stop):
                    self.removed_malformed += 1
            elif is_request_name(name):
                names.append(name)
            else:
                _remove_entry(claimed_fd, name, stop)
        return names

    def _check(
        self,
        item: Item,
        caps: Limits,
        secret_sha256_hex: str,
        artifacts_dev_ino: str,
        scanner: Scanner | None,
    ) -> None:
        """The pre-checks of one claimed request (design.md, Ingestion). Sets
        ``item.rejection`` or ``item.request`` (and the artifact copy)."""
        cap = 4 * caps.summary_max_bytes + REQUEST_OVERHEAD
        try:
            data = _read_regular(self._fds["claimed"], item.name, cap)
        except FileNotFoundError:
            item.gone = True  # recorded and removed by a concurrent drain
            return
        except _BadFile as exc:
            item.rejection = exc.reason
            return
        parsed = parse_request(data, caps)
        if isinstance(parsed, str):
            item.rejection = parsed
            return
        if parsed.run != self.run_no:
            item.rejection = "stale_run"
            return
        if not hmac.compare_digest(secret_sha256(parsed.secret), secret_sha256_hex):
            item.rejection = "bad_secret"
            return
        # The secret matched, so parsed.secret is the run secret: refused in any letter case
        # in the texts and the artifact, independently of the scanner.
        for text in (parsed.summary, parsed.reason):
            if text is None:
                continue
            blob = text.encode("utf-8")
            if _has_secret(blob, parsed.secret) or _scan(scanner, blob):
                item.rejection = "contains_secret"
                return
        if parsed.artifact is not None:
            rejection = self._copy_artifact(item, parsed, caps, artifacts_dev_ino, scanner)
            if rejection is not None:
                item.rejection = rejection
                return
        item.request = parsed

    def _artifacts_fd(self, expected: str) -> int | None:
        if "artifacts" not in self._fds:
            try:
                fd = _open_dir_at(self._fds["session"], ARTIFACTS)
            except OSError:
                return None
            self._fds["artifacts"] = fd
        fd = self._fds["artifacts"]
        return fd if _dir_id(fd) == expected else None

    def _copy_artifact(
        self,
        item: Item,
        request: Request,
        caps: Limits,
        expected_dir: str,
        scanner: Scanner | None,
    ) -> str | None:
        """Copy the artifact in chunks through one ``O_NOFOLLOW`` descriptor into a private
        file in ``scan/``, hashing it and looking for the run secret as it goes; only that
        copy is scanned and later frozen. Returns a rejection code, or None."""
        assert request.artifact is not None
        artifacts_fd = self._artifacts_fd(expected_dir)
        if artifacts_fd is None:
            return "outside_dir"
        cap = caps.artifact_max_bytes
        try:
            src = _open_regular(artifacts_fd, request.artifact, cap)
        except FileNotFoundError:
            return "artifact_missing"
        except _BadFile as exc:
            return exc.reason
        copy_name = f"{item.request_id}.{secrets.token_hex(8)}.tmp"
        scan_fd = self._fds["scan"]
        try:
            fd = os.open(copy_name, _CREATE_FLAGS, 0o600, dir_fd=scan_fd)
            item.copy_name = copy_name
            try:
                os.fchmod(fd, 0o600)
                size, digest, found = _copy_stream(src, fd, cap, request.secret)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(src)
        if size > cap:
            return "too_large"  # it grew after the fstat
        # The copy holds exactly the bytes hashed and searched: the agent can neither read
        # nor change it, so checking these bytes is checking the copy.
        if not hmac.compare_digest(digest, request.artifact_sha256 or ""):
            return "artifact_changed"
        if found:
            return "contains_secret"
        if scanner is not None:
            # TODO(#26): give the scanner the copy in chunks; until then it reads the copy
            # (at most artifact_max_bytes, and only the winner's artifact is copied).
            if _scan(scanner, _read_regular(scan_fd, copy_name, cap)):
                return "contains_secret"
        item.artifact_size = size
        return None

    # Recording (inside the caller's transaction)

    @property
    def winner(self) -> Item | None:
        """The newest request that passed every pre-check: the one that is ingested."""
        for item in reversed(self.items):
            if item.request is not None:
                return item
        return None

    def record(self, store: IngestStore, *, run_no: int, live: bool) -> str | None:
        """Record every claimed request this drain decided on in ``requests``
        (``request_id`` is the primary key, so a request is recorded at most once) and
        ingest the winner unless a newer request of the run was already ingested: freeze its
        artifact as ``artifact/<ingest_seq>/<name>`` and store the report. Call inside the
        transaction, after the hook's own row; ``run_no`` and ``live`` describe the run as
        that transaction sees it (requests are recorded under the run they were drained
        for). Returns the ingested report's status, or None."""
        winner = self.winner
        status = None
        for item in self.items:
            if item.gone or item.deferred:
                continue  # deferred requests stay in claimed/ for the next drain
            if item.unchecked or item.superseded:
                result = "discarded"
            elif item.rejection is not None:
                result = item.rejection
            elif self.run_no != run_no:
                result = "stale_run"
            elif not live:
                result = "not_live"
            else:
                assert item is winner
                latest = store.latest_ingested(self.csession_id, self.run_no)
                # A newer report already ingested (by a concurrent or later drain) wins.
                result = "ingested" if latest is None or item.request_id > latest else "discarded"
            if not store.record_request(self.csession_id, self.run_no, item.request_id, result):
                item.duplicate = True
                continue
            item.result = result
            if result == "ingested":
                assert item.request is not None
                frozen = None
                if item.request.artifact is not None:
                    seq = store.ingested_count(self.csession_id, self.run_no)
                    frozen = self._freeze(item, seq)
                store.set_report(
                    self.csession_id,
                    self.run_no,
                    status=item.request.status,
                    summary=item.request.summary,
                    reason=item.request.reason,
                    artifact=frozen,
                )
                status = item.request.status
        return status

    def rolled_back(self, failure: str | None = None) -> None:
        """Forget what ``record`` decided (its savepoint was rolled back): the requests stay
        in ``claimed/`` unless recorded again. ``failure`` names why, when it failed."""
        if failure is not None:
            self.failure = failure
        for item in self.items:
            item.result = None
            item.duplicate = False

    def _freeze(self, item: Item, seq: int) -> Frozen:
        """Rename the private copy to ``artifact/<seq>/<name>``. A directory left by an
        earlier attempt that never committed is reused (its file is replaced)."""
        assert item.request is not None and item.request.artifact is not None
        assert item.copy_name is not None and item.artifact_size is not None
        name = item.request.artifact
        artifact_fd = fsutil.ensure_private_dir_at(self._fds["private_run"], ARTIFACT)
        try:
            seq_fd = fsutil.ensure_private_dir_at(artifact_fd, str(seq))
            try:
                os.rename(item.copy_name, name, src_dir_fd=self._fds["scan"], dst_dir_fd=seq_fd)
                os.fsync(seq_fd)
            finally:
                os.close(seq_fd)
        finally:
            os.close(artifact_fd)
        item.frozen = True
        return Frozen(
            name=name,
            path=os.path.join(self.private_run_dir, ARTIFACT, str(seq), name),
            sha256=item.request.artifact_sha256 or "",
            size=item.artifact_size,
            ingest_seq=seq,
        )

    # After the transaction

    def feedback(self) -> str | None:
        """The rejection to give the agent: that of the newest processed request, when it was
        rejected (an older request's rejection no longer matters)."""
        processed = [item for item in self.items if item.processed and not item.duplicate]
        if not processed:
            return None
        newest = processed[-1]
        return newest.result if newest.result in REJECTIONS else None

    def finish(self, *, committed: bool = False) -> None:
        """Clean up. With ``committed`` (the transaction that ran ``record`` committed), the
        recorded requests are removed from ``claimed/``; otherwise they stay there for the
        next drain. Copies that were not frozen are always removed."""
        self.recorded = committed
        scan_fd = self._fds.get("scan")
        claimed_fd = self._fds.get("claimed")
        for item in self.items:
            if item.copy_name is not None and not (committed and item.frozen):
                if scan_fd is not None:
                    _unlink_quietly(scan_fd, item.copy_name)
            if committed and claimed_fd is not None and (item.result or item.duplicate):
                _unlink_quietly(claimed_fd, item.name)
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()

    def __enter__(self) -> Drain:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fds:
            self.finish()


def _copy_stream(src: int, dst: int, cap: int, secret: str) -> tuple[int, str, bool]:
    """Copy ``src`` to ``dst`` in chunks, reading at most ``cap + 1`` bytes. Returns the size
    read, the SHA-256 of the bytes written and whether the run secret occurs in them in any
    letter case (searched across chunk boundaries)."""
    token = secret.encode("ascii")
    keep = len(token) - 1
    hasher = hashlib.sha256()
    size = 0
    tail = b""
    found = False
    while size <= cap:
        chunk = os.read(src, min(_READ_CHUNK, cap + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > cap:
            break
        view = memoryview(chunk)
        while view:
            view = view[os.write(dst, view) :]
        hasher.update(chunk)
        window = tail + chunk.lower()
        found = found or token in window
        tail = window[-keep:] if keep else b""
    return size, hasher.hexdigest(), found


def _scan(scanner: Scanner | None, data: bytes) -> bool:
    # TODO(#26): the hook path passes the scanner built from the run's secret-index
    # (keyed q-gram index); until then no scanner is given and nothing is found.
    return scanner is not None and scanner(data)


def _entries(dir_fd: int) -> list[tuple[str, bool]]:
    """Up to ``DRAIN_MAX_READ`` entries of ``dir_fd``: ``(name, is a directory)`` (from
    the directory entry, never following a symlink)."""
    found: list[tuple[str, bool]] = []
    with os.scandir(dir_fd) as scan:
        for entry in scan:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            found.append((entry.name, is_dir))
            if len(found) >= DRAIN_MAX_READ:
                break
    return found


def _unlink_quietly(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _remove_entry(dir_fd: int, name: str, stop: Callable[[], bool]) -> bool:
    """Remove a malformed entry: a file, symlink or FIFO is unlinked, a directory removed
    with its contents (``_remove_tree``). False when ``stop`` cut a tree removal short."""
    try:
        os.unlink(name, dir_fd=dir_fd)
    except (IsADirectoryError, PermissionError):
        return _remove_tree(dir_fd, name, stop)
    except OSError:
        pass
    return True


_TREE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _remove_tree(dir_fd: int, name: str, stop: Callable[[], bool]) -> bool:
    """Remove a directory tree (fd-based, never following a symlink), checking ``stop``
    before each entry: once it is true the walk ends and the rest of the tree stays for the
    next drain (returns False). Iterative, so a deep tree cannot exhaust the Python stack;
    an entry that cannot be removed is skipped."""
    try:
        top = os.open(name, _TREE_FLAGS, dir_fd=dir_fd)
    except OSError:
        return True
    # Each frame: (parent fd, name in the parent, its fd, names that could not be removed).
    stack: list[tuple[int, str, int, set[str]]] = [(dir_fd, name, top, set())]
    try:
        while stack:
            _, _, fd, skip = stack[-1]
            child = None
            with os.scandir(fd) as scan:
                for entry in scan:
                    if stop():
                        return False
                    if entry.name in skip:
                        continue
                    try:
                        os.unlink(entry.name, dir_fd=fd)
                    except (IsADirectoryError, PermissionError):
                        child = entry.name
                        break
                    except OSError:
                        skip.add(entry.name)
            if child is not None:
                try:
                    child_fd = os.open(child, _TREE_FLAGS, dir_fd=fd)
                except OSError:
                    skip.add(child)
                    continue
                stack.append((fd, child, child_fd, set()))
                continue
            parent_fd, own_name, fd, _ = stack.pop()
            os.close(fd)
            try:
                os.rmdir(own_name, dir_fd=parent_fd)
            except OSError:
                if stack:
                    stack[-1][3].add(own_name)
        return True
    finally:
        for _, _, fd, _ in stack:
            os.close(fd)


def _remove_leftover(dir_fd: int, name: str, now: float, stop: Callable[[], bool]) -> None:
    """Remove ``name`` when it is older than ``SCAN_LEFTOVER_AGE`` (left by a canceled drain
    or ``report``); a fresh one may still be in use and is kept."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return
    if now - info.st_mtime > SCAN_LEFTOVER_AGE:
        _remove_entry(dir_fd, name, stop)


def _remove_leftovers(scan_fd: int, stop: Callable[[], bool]) -> None:
    """Remove copies left by a canceled drain; a concurrent drain's fresh copies are kept.
    Stops once ``stop`` is true."""
    now = time.time()
    for name, _ in _entries(scan_fd):
        if stop():
            return
        _remove_leftover(scan_fd, name, now, stop)
