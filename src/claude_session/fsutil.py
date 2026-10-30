"""File writes that never follow a planted symlink. Standard library only.

``write_private_file`` is used for ``--outcome-file`` (interface.md, Outcome file) and
``append_private_line`` for the ``log.jsonl`` and ``audit.jsonl`` appends; both are reusable
for the session-directory writes (design.md, File handling).
"""

from __future__ import annotations

import errno
import fcntl
import os
import secrets
import stat
import time

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Seconds append_private_line waits for the append lock before giving up: a process holding
# it must not hang hook-event or an audited command.
LOCK_TIMEOUT = 1.0
_LOCK_RETRY_INTERVAL = 0.005


def open_dir_nofollow(path: str) -> int:
    """Open the absolute, already resolved directory ``path`` one component at a time from
    ``/`` with ``O_NOFOLLOW``, so a component that is (or became) a symlink fails with
    ``OSError``. Returns a directory descriptor the caller must close."""
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in path.split("/"):
            if not part:
                continue
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_private_file(path: str, data: bytes) -> None:
    """Atomically replace ``path`` with ``data``, mode 0600, durably.

    The parent directory is resolved once with ``realpath`` (so symlinks that exist beforehand,
    such as macOS ``/tmp``, work) and then opened component by component without following
    symlinks; a component swapped to a symlink after resolution makes the write fail. The
    temporary file is created relative to that descriptor with ``O_EXCL|O_NOFOLLOW``, fsynced,
    and renamed over ``path`` (a symlink at ``path`` is replaced, never followed); then the
    directory is fsynced. ``path`` must be absolute and every directory on the resolved parent
    path must be readable (directories are opened ``O_RDONLY``). Raises ``OSError`` on failure;
    the temporary file is removed.
    """
    if not os.path.isabs(path):
        raise ValueError(f"path must be absolute: {path!r}")
    parent, name = os.path.split(path)
    if not name:
        raise IsADirectoryError(path)
    dir_fd = open_dir_nofollow(os.path.realpath(parent))
    try:
        tmp_name = f".{secrets.token_hex(4)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
        try:
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass  # the original error matters more than a leftover temporary file
            raise
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def append_private_line(path: str, line: bytes) -> None:
    """Append one newline-terminated ``line`` to ``path`` with a single ``os.write``.

    The file is opened ``O_APPEND`` (created mode 0600, and tightened to 0600 if it was
    looser), so lines from concurrent writers never interleave. The parent directory is
    resolved and walked as in ``write_private_file``; the file itself is opened with
    ``O_NOFOLLOW`` (a symlink fails) and ``O_NONBLOCK`` (a FIFO fails instead of blocking), and
    anything but a regular file with exactly one link, owned by the current user, is refused
    before anything is written or chmodded. The checks run after the descriptor is locked, and
    the name must still refer to the opened file (same device and inode), so a hard link
    renamed away after the open is refused too. Under an exclusive ``flock`` (held by every
    writer, so a concurrent write in progress is never mistaken for a torn line), if the file
    does not end in a newline (an earlier short write left a torn line), the line is prefixed
    with one, so it stays a line of its own. The lock is waited for at most ``LOCK_TIMEOUT``
    seconds. An existing file the user cannot read (such as mode 0200) is checked the same way
    (without the lock), tightened to 0600 and reopened; a missing file in a directory the user
    cannot write raises the ``PermissionError`` of the create. The parent directory is never
    created. Raises ``OSError`` on failure, including a short write and a lock still held at
    the deadline.
    """
    if not os.path.isabs(path):
        raise ValueError(f"path must be absolute: {path!r}")
    if not line.endswith(b"\n") or b"\n" in line[:-1]:
        raise ValueError("line must contain exactly one newline, at its end")
    parent, name = os.path.split(path)
    if not name:
        raise IsADirectoryError(path)
    dir_fd = open_dir_nofollow(os.path.realpath(parent))
    try:
        fd = _open_for_append(name, dir_fd, path)
        try:
            _lock(fd, path)  # released when fd is closed
            _check_and_tighten(fd, name, dir_fd, path)
            size = os.fstat(fd).st_size
            if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(f"short write to {path}: {written} of {len(line)} bytes")
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


# O_RDWR, not O_WRONLY: the last byte is read back to detect a torn line.
_APPEND_FLAGS = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _open_for_append(name: str, dir_fd: int, path: str) -> int:
    try:
        return os.open(name, _APPEND_FLAGS | os.O_CREAT, 0o600, dir_fd=dir_fd)
    except PermissionError as denied:
        # An existing file without read permission: check it through a write-only descriptor,
        # tighten it to 0600, then open it once more for reading and appending. A missing file
        # (a directory the user cannot write) keeps the original permission error.
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            raise denied from None
        try:
            _check_and_tighten(fd, name, dir_fd, path)
        finally:
            os.close(fd)
        return os.open(name, _APPEND_FLAGS, dir_fd=dir_fd)


def _check_and_tighten(fd: int, name: str, dir_fd: int, path: str) -> None:
    """Refuse anything but a regular file with one link owned by the user that ``name`` (in
    ``dir_fd``) still names; set mode 0600.

    The name is checked after the descriptor: a hard link renamed away after the open leaves
    the opened file with one link, but ``name`` then no longer names it.
    """
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"not a regular file: {path}")
    if st.st_nlink != 1:
        raise OSError(f"file has {st.st_nlink} links: {path}")
    if st.st_uid != os.getuid():
        raise OSError(f"file is owned by another user: {path}")
    named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino):
        raise OSError(f"file was replaced while it was opened: {path}")
    if stat.S_IMODE(st.st_mode) != 0o600:
        os.fchmod(fd, 0o600)


def _lock(fd: int, path: str) -> None:
    """Take an exclusive ``flock`` on ``fd``, waiting at most ``LOCK_TIMEOUT`` seconds."""
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                message = f"timed out waiting for the lock: {path}"
                raise OSError(errno.EWOULDBLOCK, message) from None
            time.sleep(_LOCK_RETRY_INTERVAL)
