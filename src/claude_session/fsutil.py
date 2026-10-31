"""File writes that never follow a planted symlink. Standard library only.

Used for ``--outcome-file`` (interface.md, Outcome file) and for the session-directory writes
(design.md, Session directories), which work relative to pinned directory descriptors.
"""

from __future__ import annotations

import os
import secrets
import stat

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: opening a FIFO planted in place of a directory must fail, not wait for a writer.
_SUBDIR_FLAGS = _DIR_FLAGS | os.O_NONBLOCK


class UnsafePathError(OSError):
    """A directory that is not a real directory owned by this user."""


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
    symlink, check that it is ours and tighten it to 0700. Returns a descriptor the caller
    must close; raises ``OSError`` (``FileNotFoundError`` when it is missing)."""
    _check_segment(name)
    fd = os.open(name, _SUBDIR_FLAGS, dir_fd=dir_fd)
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
        return open_private_dir_at(dir_fd, name)
    try:
        return open_private_dir_at(dir_fd, name)
    except PermissionError:
        # A umask without owner bits left the new directory unreadable. Linux has no
        # no-follow chmod; the open below re-checks with O_NOFOLLOW, so a swapped-in symlink
        # still fails (and chmod can only touch our own files).
        os.chmod(name, 0o700, dir_fd=dir_fd)
        return open_private_dir_at(dir_fd, name)


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
        write_private_file_at(dir_fd, name, data)
    finally:
        os.close(dir_fd)


def write_private_file_at(dir_fd: int, name: str, data: bytes) -> None:
    """Atomically replace ``name`` in the directory ``dir_fd`` with ``data``, mode 0600,
    durably: a temporary file created with ``O_EXCL|O_NOFOLLOW`` relative to ``dir_fd``,
    fsynced and renamed over ``name`` (a symlink at ``name`` is replaced, never followed),
    then the directory is fsynced. Raises ``OSError`` on failure (the temporary file is
    removed) and ``ValueError`` when ``name`` is not one path segment."""
    _check_segment(name)
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
