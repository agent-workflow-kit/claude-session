"""File writes that never follow a planted symlink. Standard library only.

``write_private_file`` is used for ``--outcome-file`` (interface.md, Outcome file),
``write_private_file_at`` and the ``*_private_dir_at`` helpers for the session-directory
writes (design.md, Session directories), which work relative to pinned directory
descriptors, and ``append_private_line`` for the ``log.jsonl`` and ``audit.jsonl`` appends.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import sys
import time

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Seconds append_private_line waits for the append lock before giving up: a process holding
# it must not hang hook-event or an audited command.
LOCK_TIMEOUT = 1.0
_LOCK_RETRY_INTERVAL = 0.005
# O_NONBLOCK: opening a FIFO planted in place of a directory must fail, not wait for a writer.
_SUBDIR_FLAGS = _DIR_FLAGS | os.O_NONBLOCK


class UnsafePathError(OSError):
    """A directory that is not a real directory owned by this user."""


def token_hex(nbytes: int) -> str:
    """``nbytes`` random bytes as hex, as ``secrets.token_hex`` makes them and from the same
    source: this module is imported on every hook, and importing ``secrets`` costs
    milliseconds there."""
    return os.urandom(nbytes).hex()


def _check_segment(name: str) -> None:
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise ValueError(f"not a single path segment: {name!r}")


def check_private_dir_fd(fd: int, label: str) -> None:
    """``fd`` must be a directory owned by the effective user; raise ``UnsafePathError``."""
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafePathError(f"{label}: not a directory")
    if info.st_uid != os.geteuid():
        raise UnsafePathError(f"{label}: owned by uid {info.st_uid}")


def open_private_dir_at(dir_fd: int, name: str) -> int:
    """Open the existing directory ``name`` relative to ``dir_fd`` without following a
    symlink, check that it is ours and set it to 0700 (also when its mode denies us reading
    it). Returns a descriptor the caller must close; raises ``OSError``
    (``FileNotFoundError`` when it is missing)."""
    _check_segment(name)
    try:
        fd = os.open(name, _SUBDIR_FLAGS, dir_fd=dir_fd)
    except PermissionError:
        # Our own directory with a mode that denies reading (a umask without owner bits,
        # or mode 000): repair the mode without following a symlink, then open it again.
        repaired = _repair_dir_mode_at(dir_fd, name)
        fd = os.open(name, _SUBDIR_FLAGS, dir_fd=dir_fd)
        try:
            if _identity(fd) != repaired:
                raise UnsafePathError(f"{name}: replaced while its mode was repaired")
        except BaseException:
            os.close(fd)
            raise
    try:
        check_private_dir_fd(fd, name)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def ensure_private_dir_at(dir_fd: int, name: str) -> int:
    """Like ``open_private_dir_at``, creating ``name`` (mode 0700, whatever the umask) when
    it is missing. A symlink, even a dangling one, is refused and never followed."""
    _check_segment(name)
    try:
        os.mkdir(name, 0o700, dir_fd=dir_fd)
    except FileExistsError:
        pass
    return open_private_dir_at(dir_fd, name)


def create_private_dir_at(dir_fd: int, name: str) -> int:
    """Create the directory ``name`` (``FileExistsError`` when any entry has that name) and
    open it like ``open_private_dir_at``, so it is 0700 whatever the umask."""
    _check_segment(name)
    os.mkdir(name, 0o700, dir_fd=dir_fd)
    return open_private_dir_at(dir_fd, name)


def _identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _repair_dir_mode_at(dir_fd: int, name: str) -> tuple[int, int]:
    """Set the directory ``name`` to 0700 without ever following a symlink and return its
    ``(st_dev, st_ino)``. It must be a directory owned by the effective user.

    Linux: open it ``O_PATH`` (needs no read permission), check it through that descriptor
    and chmod it through ``/proc/self/fd``. Elsewhere (macOS): a no-follow ``chmod`` relative
    to ``dir_fd``, after an ``lstat`` check; without one the repair fails."""
    o_path = os.O_PATH if sys.platform.startswith("linux") else 0
    if o_path:
        flags = o_path | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(name, flags, dir_fd=dir_fd)
        try:
            check_private_dir_fd(fd, name)
            os.chmod(f"/proc/self/fd/{fd}", 0o700)
            return _identity(fd)
        finally:
            os.close(fd)
    if os.chmod not in os.supports_follow_symlinks:
        raise UnsafePathError(f"{name}: unreadable and no no-follow chmod on this platform")
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafePathError(f"{name}: not a directory")
    if info.st_uid != os.geteuid():
        raise UnsafePathError(f"{name}: owned by uid {info.st_uid}")
    os.chmod(name, 0o700, dir_fd=dir_fd, follow_symlinks=False)
    return info.st_dev, info.st_ino


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
    if name in ("", ".", ".."):
        raise IsADirectoryError(path)
    dir_fd = open_dir_nofollow(os.path.realpath(parent))
    try:
        write_private_file_at(dir_fd, name, data)
    finally:
        os.close(dir_fd)


def write_private_file_at(
    dir_fd: int, name: str, data: bytes, *, tmp_dir_fd: int | None = None
) -> None:
    """Atomically replace ``name`` in the directory ``dir_fd`` with ``data``, mode 0600,
    durably: a temporary file created with ``O_EXCL|O_NOFOLLOW``, fsynced and renamed over
    ``name`` (a symlink at ``name`` is replaced, never followed), then the directory is
    fsynced. The temporary file is created in ``tmp_dir_fd`` when given (a directory on the
    same file system that others cannot see, so no temporary name ever appears in
    ``dir_fd``), else in ``dir_fd``. Raises ``OSError`` on failure (the temporary file is
    removed) and ``ValueError`` when ``name`` is not one path segment."""
    _check_segment(name)
    tmp_fd = dir_fd if tmp_dir_fd is None else tmp_dir_fd
    tmp_name = f".{token_hex(4)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(tmp_name, flags, 0o600, dir_fd=tmp_fd)
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp_name, name, src_dir_fd=tmp_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name, dir_fd=tmp_fd)
        except OSError:
            pass  # the original error matters more than a leftover temporary file
        raise
    os.fsync(dir_fd)


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


# Deeper trees are refused by ``remove_tree_at`` (bounded recursion and open descriptors).
MAX_TREE_DEPTH = 256
# Descriptors left free below RLIMIT_NOFILE while a tree is removed (one is open per level).
FD_MARGIN = 64


def tree_depth_cap() -> int:
    """The depth ``remove_tree_at`` refuses beyond: ``MAX_TREE_DEPTH``, lowered to the soft
    ``RLIMIT_NOFILE`` minus ``FD_MARGIN`` (at least 1), so a deep tree fails cleanly with
    ``UnsafePathError`` instead of ``EMFILE`` partway down."""
    import resource

    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft < 0:
        return MAX_TREE_DEPTH
    return max(1, min(MAX_TREE_DEPTH, soft - FD_MARGIN))


def remove_tree_at(dir_fd: int, name: str) -> None:
    """Remove ``name`` in the directory ``dir_fd`` and everything below it, relative to
    directory descriptors and never following a symlink: a symlink (anywhere, including
    ``name`` itself) is unlinked, never followed. Directories must be owned by the effective
    user; their modes are repaired (0700) so contents can be listed and removed. A tree
    deeper than ``tree_depth_cap()`` is refused (``UnsafePathError``). Raises ``OSError``; on
    failure part of the tree may already be gone."""
    _check_segment(name)
    _remove_at(dir_fd, name, 0, tree_depth_cap())


def _remove_at(dir_fd: int, name: str, depth: int, cap: int) -> None:
    info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=dir_fd)
        return
    if depth >= cap:
        raise UnsafePathError(f"{name}: directory tree deeper than {cap} levels")
    fd = open_private_dir_at(dir_fd, name)  # no-follow, ours, mode repaired to 0700
    try:
        if _identity(fd) != (info.st_dev, info.st_ino):
            raise UnsafePathError(f"{name}: replaced while it was being removed")
        for child in os.listdir(fd):
            _remove_at(fd, child, depth + 1, cap)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=dir_fd)
