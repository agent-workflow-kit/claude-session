"""File writes that never follow a planted symlink. Standard library only.

Used for ``--outcome-file`` (interface.md, Outcome file), the ``log.jsonl`` and ``audit.jsonl``
appends, and reusable for the session-directory writes (design.md, File handling).
"""

from __future__ import annotations

import os
import secrets
import stat

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


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
    anything but a regular file is refused. The parent directory is never created. Raises
    ``OSError`` on failure, including a short write.
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
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(name, flags | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"not a regular file: {path}")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        written = os.write(fd, line)
        if written != len(line):
            raise OSError(f"short write to {path}: {written} of {len(line)} bytes")
    finally:
        os.close(fd)
