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
    o_path = getattr(os, "O_PATH", 0)
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
    tmp_name = f".{secrets.token_hex(4)}.tmp"
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
