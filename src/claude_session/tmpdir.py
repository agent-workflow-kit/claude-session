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
  descriptor, and removed with ``fsutil.remove_tree_at``, which never follows a symlink. A
  leftover that cannot be removed (a tree deeper than ``fsutil.tree_depth_cap()``, which a
  sandboxed agent can build in its own TMPDIR) is renamed aside in the root to
  ``.stale-<id>-<hex>`` (never a session id), so the session's path is free again; removing
  it is retried once and it is left when that fails too.
- A refusal names the failing entry (the base, the per-uid root or the run directory) by its
  full path and carries a hint for that entry; ``check`` and ``create`` report the same text.
  Before the per-uid root is opened, an ``lstat`` relative to the base learns its owner and
  type for the message only (a root another user created is reported as such even when it
  cannot be opened); the no-follow open and ``fstat`` checks stay authoritative.

TODO(#15): exec calls ``check`` at the temporary-directory slot of launch step 5 and
``create`` (through ``selfcheck.self_check``, after the ``claude_args`` check) at launch and
again at adoption, passes the path as ``Isolation.tmpdir`` and ``envbuild``'s ``tmpdir``,
and calls ``remove`` when the run ends (also not-started runs, after the run is final).
TODO(#29): reconcile calls ``remove`` for leftover tmpdirs of final runs and of deleted
sessions.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat

from . import fsutil, ids
from .errors import LaunchValidationError

DEFAULT_BASE = "/tmp"
ROOT_PREFIX = "csession-"
# Leftover run directories that cannot be removed are renamed to ".stale-<id>-<hex>": the
# leading dot keeps the name outside the session id grammar.
STALE_PREFIX = ".stale-"
# Owners the base directory may have besides nobody: root, or the effective user itself.
# A tuple of uids; ``None`` stands for the effective uid at call time.
_TRUSTED_BASE_OWNERS: tuple[int | None, ...] = (0, None)
# O_NONBLOCK: a FIFO squatting the name must fail to open, not wait for a writer.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_SANDBOX_OFF = "set the kind's sandbox to off"


class TmpdirUnsafe(LaunchValidationError):
    """The private temporary directory, its per-uid root or its base is unsafe."""

    end_reason = "tmpdir_unsafe"


class TmpdirLeftover(OSError):
    """``remove`` could not remove the run directory. It was renamed aside to ``stale_path``
    (the session's path is free) unless ``stale_path`` is None; ``hint`` names the exact
    command that removes what is left."""

    def __init__(self, message: str, *, path: str, stale_path: str | None, hint: str) -> None:
        super().__init__(message)
        self.path = path
        self.stale_path = stale_path
        self.hint = hint


class _Refusal(fsutil.UnsafePathError):
    """An unsafe entry: ``path`` (full path), what is wrong with it and the hint for it."""

    def __init__(self, path: str, reason: str, hint: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason
        self.hint = hint


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
    (``--dry-run``, and the temporary-directory slot of launch step 5); returns the tmpdir
    path (None without a session id). Raises ``TmpdirUnsafe``."""
    path = None if csession_id is None else run_path(csession_id, base)
    root = root_path(base)
    try:
        base_fd = _open_base(os.path.dirname(root))
        try:
            root_fd = _open_root(base_fd, root, create=False)
            if root_fd is not None:
                os.close(root_fd)
        finally:
            os.close(base_fd)
    except _Refusal as exc:
        raise _unsafe(exc) from exc
    return path


def create(csession_id: str, *, base: str = DEFAULT_BASE) -> str:
    """Create the session's private temporary directory, empty and 0700, under a checked
    per-uid root (created when missing); a leftover directory (or any entry) of that name is
    removed first without following symlinks, or renamed aside when it cannot be removed.
    Returns its path. Raises ``TmpdirUnsafe``."""
    path = run_path(csession_id, base)
    root = os.path.dirname(path)
    try:
        base_fd = _open_base(os.path.dirname(root))
        try:
            root_fd = _open_root(base_fd, root, create=True)
        finally:
            os.close(base_fd)
        assert root_fd is not None
        try:
            if _exists_at(root_fd, csession_id):
                _clear_leftover(root_fd, root, csession_id)
            try:
                os.close(fsutil.create_private_dir_at(root_fd, csession_id))
            except OSError as exc:
                raise _refusal(path, exc, _run_hint(path)) from exc
        finally:
            os.close(root_fd)
    except _Refusal as exc:
        raise _unsafe(exc) from exc
    return path


def remove(csession_id: str, *, base: str = DEFAULT_BASE) -> bool:
    """Remove the session's private temporary directory and everything in it, never
    following a symlink. Returns False when there was nothing to remove (idempotent; a
    missing root is not created). Raises ``OSError``: ``fsutil.UnsafePathError`` for an
    unsafe base or root, which is left untouched; ``TmpdirLeftover`` when the directory
    could not be removed (it is renamed aside first, so the session's path is free)."""
    path = run_path(csession_id, base)
    root = os.path.dirname(path)
    base_fd = _open_base(os.path.dirname(root))
    try:
        root_fd = _open_root(base_fd, root, create=False)
    finally:
        os.close(base_fd)
    if root_fd is None:
        return False
    try:
        if not _exists_at(root_fd, csession_id):
            return False
        try:
            fsutil.remove_tree_at(root_fd, csession_id)
        except OSError as exc:
            reason = _reason(exc)
            try:
                stale = _rename_aside(root_fd, csession_id)
            except OSError:
                raise TmpdirLeftover(
                    f"private temporary directory {path} could not be removed: {reason}",
                    path=path,
                    stale_path=None,
                    hint=_leftover_hint(path),
                ) from exc
            stale_path = os.path.join(root, stale)
            raise TmpdirLeftover(
                f"private temporary directory {path} could not be removed ({reason});"
                f" it was moved aside to {stale_path}",
                path=path,
                stale_path=stale_path,
                hint=_leftover_hint(stale_path),
            ) from exc
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
    try:
        fd = fsutil.open_dir_nofollow(base)
    except OSError as exc:
        raise _refusal(base, exc, _base_hint(base)) from exc
    try:
        info = os.fstat(fd)
        euid = os.geteuid()
        owners = {euid if uid is None else uid for uid in _TRUSTED_BASE_OWNERS}
        if info.st_uid not in owners:
            raise _Refusal(
                base,
                f"owned by uid {info.st_uid}, neither root nor you",
                _base_owner_hint(base, info.st_uid),
            )
        writable = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable and not info.st_mode & stat.S_ISVTX:
            raise _Refusal(base, "writable by others and without the sticky bit", _base_hint(base))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_root(base_fd: int, root: str, *, create: bool) -> int | None:
    """Open the per-uid root ``root`` (full path) in the base. ``create``: make it when
    missing (0700 whatever the umask). An existing root must be a real directory owned by us
    with mode exactly 0700; it is never repaired. Returns None when it is missing and not
    created. Raises ``_Refusal``."""
    name = os.path.basename(root)
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=base_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _refusal(root, exc, _base_hint(os.path.dirname(root))) from exc
        else:
            # Ours, just made, in a sticky (or private) base nobody else can rename in: only
            # the umask may have narrowed the mode. open_private_dir_at opens it no-follow,
            # checks the owner and sets 0700.
            try:
                return fsutil.open_private_dir_at(base_fd, name)
            except OSError as exc:
                raise _refusal(root, exc, _root_hint(root)) from exc
    _diagnose_root(base_fd, root)
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=base_fd)
    except FileNotFoundError as exc:
        if create:
            raise _refusal(root, exc, _root_hint(root)) from exc
        return None
    except OSError as exc:
        raise _refusal(root, exc, _root_hint(root)) from exc
    try:
        _check_root(fd, root)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _diagnose_root(base_fd: int, root: str) -> None:
    """Diagnostics only (the descriptor checks in ``_open_root`` stay authoritative): refuse
    a root another user owns with the squat message and hint, also when its mode would make
    the open fail with ``EACCES`` and hide the owner."""
    try:
        info = os.stat(os.path.basename(root), dir_fd=base_fd, follow_symlinks=False)
    except OSError:
        return  # the open below reports it
    if info.st_uid != os.geteuid():
        kind = "" if stat.S_ISDIR(info.st_mode) else " (not a directory)"
        raise _Refusal(root, f"owned by uid {info.st_uid}{kind}", _squat_hint(root))


def _check_root(fd: int, root: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise _Refusal(root, "not a directory", _root_hint(root))
    if info.st_uid != os.geteuid():
        raise _Refusal(root, f"owned by uid {info.st_uid}", _squat_hint(root))
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise _Refusal(root, f"mode {stat.S_IMODE(info.st_mode):04o}, not 0700", _root_hint(root))


def _clear_leftover(root_fd: int, root: str, name: str) -> None:
    """Remove the leftover entry ``name`` in the root; when that fails, rename it aside and
    try once to remove it under its new name (left there when that fails too)."""
    try:
        fsutil.remove_tree_at(root_fd, name)
        return
    except OSError as exc:
        removal_error = exc
    path = os.path.join(root, name)
    try:
        stale = _rename_aside(root_fd, name)
    except OSError as rename_error:
        raise _Refusal(
            path,
            f"a leftover that could not be removed ({_reason(removal_error)}) or moved"
            f" aside ({_reason(rename_error)})",
            _leftover_hint(path),
        ) from removal_error
    # TODO(#29): reconcile removes .stale-* entries under the per-uid root.
    try:
        fsutil.remove_tree_at(root_fd, stale)
    except OSError:
        pass  # left for reconcile (#29); the session's path is free


def _rename_aside(root_fd: int, name: str) -> str:
    """Rename ``name`` in the root to a fresh ``.stale-<name>-<hex>``, fd-relative; returns
    the new name."""
    stale = f"{STALE_PREFIX}{name}-{secrets.token_hex(8)}"
    os.rename(name, stale, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    return stale


def _exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _reason(exc: OSError) -> str:
    if isinstance(exc, _Refusal):
        return exc.reason
    if isinstance(exc, fsutil.UnsafePathError):
        return str(exc)
    reason = exc.strerror if exc.strerror else str(exc)
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        reason = f"{reason} (a symlink or not a directory)"
    return reason


def _refusal(path: str, exc: OSError, hint: str) -> _Refusal:
    if isinstance(exc, _Refusal):
        return exc
    return _Refusal(path, _reason(exc), hint)


def _unsafe(refusal: _Refusal) -> TmpdirUnsafe:
    return TmpdirUnsafe(
        f"private temporary directory unsafe: {refusal.path}: {refusal.reason}",
        hint=refusal.hint,
    )


# Hints, one per failing entry (interface.md, Private temporary directory).


def _base_hint(base: str) -> str:
    return (
        f"the system temporary directory {base} is misconfigured: ask an administrator to"
        " make it a directory owned by root (or you) with the sticky bit (mode 1777), or"
        f" {_SANDBOX_OFF}"
    )


def _base_owner_hint(base: str, uid: int) -> str:
    return (
        f"{base} is owned by uid {uid}, neither root nor you; inside a user namespace or"
        " container this usually means its owner is unmapped there (shown as the overflow"
        f" uid, often 65534): {_SANDBOX_OFF}, or give the run a {base} owned by root or you"
        " (with the sticky bit when others can write it)"
    )


def _root_hint(root: str) -> str:
    return f"remove it (rm -rf {root}) and run again: it is recreated with mode 0700"


def _squat_hint(root: str) -> str:
    return (
        f"another user created {root}: ask an administrator to remove it, or"
        f" {_SANDBOX_OFF} until then"
    )


def _run_hint(path: str) -> str:
    return f"remove it (rm -rf {path}) and run again"


def _leftover_hint(path: str) -> str:
    return f"remove it: rm -rf {path}"
