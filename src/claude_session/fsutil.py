"""File writes that never follow a planted symlink. Standard library only.

Used for ``--outcome-file`` (interface.md, Outcome file) and reusable for the session-directory
writes (design.md, File handling).
"""

from __future__ import annotations

import os
import secrets


def write_private_file(path: str, data: bytes) -> None:
    """Atomically replace ``path`` with ``data``, mode 0600.

    The parent directory is opened with ``O_NOFOLLOW`` and both the temporary file and the
    rename are relative to that descriptor; the temporary file is created with
    ``O_EXCL|O_NOFOLLOW``, and the rename replaces a symlink at ``path`` instead of following
    it. ``path`` must be absolute. Raises ``OSError`` on failure; no temporary file is left.
    """
    if not os.path.isabs(path):
        raise ValueError(f"path must be absolute: {path!r}")
    parent, name = os.path.split(path)
    if not name:
        raise IsADirectoryError(path)
    dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        tmp_name = f".{name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
        try:
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            finally:
                os.close(fd)
            os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(dir_fd)
