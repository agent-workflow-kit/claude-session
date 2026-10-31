"""The private temporary directory of sandboxed runs (interface.md, Isolation; design.md,
Session directories). Standard library only.

Layout: ``<base>/csession-<euid>/<csession_id>/`` with ``<base>`` the realpath of ``/tmp``
(macOS: ``/private/tmp``). The base is world-writable, so another local user is assumed
hostile: they may pre-create (squat) the per-uid root, plant a symlink or FIFO in its place,
or create it with a mode that lets them in.

- The base must be a directory owned by root or by us; when group- or other-writable it must
  have the sticky bit, so nobody else can rename or delete our entries in it (that is what
  makes "create, then use by path" race-free).
- The per-uid root is created 0700 (whatever the umask) or, when it exists, opened without
  following a symlink (``O_NOFOLLOW | O_DIRECTORY | O_NONBLOCK``: a symlink, file or FIFO
  fails) and checked through that descriptor: a directory, owned by the effective uid, mode
  exactly 0700. An existing root that fails is refused (``tmpdir_unsafe``), never repaired:
  a root that was ever open to others may hold their planted entries.
- The per-run directory is created fresh (a leftover is removed first) relative to the root's
  descriptor, and removed with ``fsutil.remove_tree_at``, which never follows a symlink.

TODO(#15): exec calls ``create`` (through ``selfcheck.run``) at launch and adoption, passes
the path as ``Isolation.tmpdir`` and ``envbuild``'s ``tmpdir``, and calls ``remove`` when the
run ends (also not-started runs, after the run is final).
TODO(#29): reconcile calls ``remove`` for leftover tmpdirs of final runs and of deleted
sessions.
"""

from __future__ import annotations

import errno
import os
import stat

from . import fsutil, ids
from .errors import LaunchValidationError

DEFAULT_BASE = "/tmp"
ROOT_PREFIX = "csession-"
# Owners the base directory may have besides nobody: root, or the effective user itself.
# A tuple of uids; ``None`` stands for the effective uid at call time.
_TRUSTED_BASE_OWNERS: tuple[int | None, ...] = (0, None)
# O_NONBLOCK: a FIFO squatting the name must fail to open, not wait for a writer.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_HINT = (
    "remove or fix it: it must be a directory owned by you with mode 0700 (and the"
    " temporary directory's parent must have the sticky bit)"
)


class TmpdirUnsafe(LaunchValidationError):
    """The private temporary directory, its per-uid root or its base is unsafe."""

    end_reason = "tmpdir_unsafe"


def root_path(base: str = DEFAULT_BASE) -> str:
    """``<realpath(base)>/csession-<euid>``; ``base`` must be absolute."""
    if not os.path.isabs(base):
        raise ValueError(f"base must be absolute: {base!r}")
    return os.path.join(os.path.realpath(base), f"{ROOT_PREFIX}{os.geteuid()}")


def run_path(csession_id: str, base: str = DEFAULT_BASE) -> str:
    """The private temporary directory of session ``csession_id`` (not checked or created)."""
    _check_id(csession_id)
    return os.path.join(root_path(base), csession_id)


def check(csession_id: str | None, *, base: str = DEFAULT_BASE) -> str | None:
    """Validate the base and, when it exists, the per-uid root, creating and changing nothing
    (``--dry-run``); returns the tmpdir path (None without a session id). Raises
    ``TmpdirUnsafe``."""
    path = None if csession_id is None else run_path(csession_id, base)
    root = root_path(base)
    try:
        base_fd = _open_base(os.path.dirname(root))
        try:
            root_fd = _open_root(base_fd, os.path.basename(root), create=False)
            if root_fd is not None:
                os.close(root_fd)
        finally:
            os.close(base_fd)
    except OSError as exc:
        raise _unsafe(root, exc) from exc
    return path


def create(csession_id: str, *, base: str = DEFAULT_BASE) -> str:
    """Create the session's private temporary directory, empty and 0700, under a checked
    per-uid root (created when missing); a leftover directory (or any entry) of that name is
    removed first without following symlinks. Returns its path. Raises ``TmpdirUnsafe``."""
    path = run_path(csession_id, base)
    root = os.path.dirname(path)
    try:
        base_fd = _open_base(os.path.dirname(root))
        try:
            root_fd = _open_root(base_fd, os.path.basename(root), create=True)
        finally:
            os.close(base_fd)
        assert root_fd is not None
        try:
            if _exists_at(root_fd, csession_id):
                fsutil.remove_tree_at(root_fd, csession_id)
            os.close(fsutil.create_private_dir_at(root_fd, csession_id))
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise _unsafe(path, exc) from exc
    return path


def remove(csession_id: str, *, base: str = DEFAULT_BASE) -> bool:
    """Remove the session's private temporary directory and everything in it, never
    following a symlink. Returns False when there was nothing to remove (idempotent; a
    missing root is not created). Raises ``OSError`` (``fsutil.UnsafePathError`` for an
    unsafe base or root, which is left untouched)."""
    root = os.path.dirname(run_path(csession_id, base))
    base_fd = _open_base(os.path.dirname(root))
    try:
        root_fd = _open_root(base_fd, os.path.basename(root), create=False)
    finally:
        os.close(base_fd)
    if root_fd is None:
        return False
    try:
        if not _exists_at(root_fd, csession_id):
            return False
        fsutil.remove_tree_at(root_fd, csession_id)
    finally:
        os.close(root_fd)
    return True


def _check_id(csession_id: str) -> None:
    # The id becomes a path segment: only the session id grammar (no "/", "..", NUL).
    if not ids.is_session_id(csession_id):
        raise ValueError(f"not a session id: {csession_id!r}")


def _open_base(base: str) -> int:
    """Open the resolved base without following symlinks and check it: a directory owned by
    root or us; group- or other-writable only with the sticky bit."""
    fd = fsutil.open_dir_nofollow(base)
    try:
        info = os.fstat(fd)
        euid = os.geteuid()
        owners = {euid if uid is None else uid for uid in _TRUSTED_BASE_OWNERS}
        if info.st_uid not in owners:
            raise fsutil.UnsafePathError(f"{base}: owned by uid {info.st_uid}")
        writable = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable and not info.st_mode & stat.S_ISVTX:
            raise fsutil.UnsafePathError(f"{base}: writable by others and without the sticky bit")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_root(base_fd: int, name: str, *, create: bool) -> int | None:
    """Open the per-uid root ``name`` in the base. ``create``: make it when missing (0700
    whatever the umask). An existing root must be a real directory owned by us with mode
    exactly 0700; it is never repaired. Returns None when it is missing and not created."""
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=base_fd)
        except FileExistsError:
            pass
        else:
            # Ours, just made, in a sticky (or private) base nobody else can rename in: only
            # the umask may have narrowed the mode. open_private_dir_at opens it no-follow,
            # checks the owner and sets 0700.
            return fsutil.open_private_dir_at(base_fd, name)
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=base_fd)
    except FileNotFoundError:
        if create:
            raise
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise fsutil.UnsafePathError(f"{name}: a symlink or not a directory") from exc
        raise
    try:
        _check_root(fd, name)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _check_root(fd: int, name: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise fsutil.UnsafePathError(f"{name}: not a directory")
    if info.st_uid != os.geteuid():
        raise fsutil.UnsafePathError(f"{name}: owned by uid {info.st_uid}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise fsutil.UnsafePathError(f"{name}: mode {stat.S_IMODE(info.st_mode):04o}, not 0700")


def _exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _unsafe(path: str, exc: OSError) -> TmpdirUnsafe:
    reason = exc.strerror if exc.strerror else str(exc)
    if isinstance(exc, fsutil.UnsafePathError):
        reason = str(exc)
    elif exc.errno in (errno.ELOOP, errno.ENOTDIR):
        reason = f"{reason} (a symlink or not a directory)"
    return TmpdirUnsafe(f"private temporary directory {path} is unsafe: {reason}", hint=_HINT)
