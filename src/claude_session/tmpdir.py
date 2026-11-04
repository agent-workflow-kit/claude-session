"""The private temporary directory of sandboxed runs (interface.md, Isolation; design.md,
Session directories). No third-party dependencies.

Layout: ``<base>/<root_name(euid)>/<csession_id>/`` (``root_name``: ``csession-<euid>``) with
``<base>`` the realpath of ``/tmp`` (macOS: ``/private/tmp``), or, when that root cannot be
established, the fallback ``<realpath($XDG_RUNTIME_DIR)>/csession/<csession_id>/`` (its parent
and every ancestor checked, ``_open_runtime_dir``). A launch chooses the root once
(``select_root``, through ``run_path``) and creates only there (``create_pinned(root=...)``).
The base is world-writable, so another local user is assumed hostile: they may pre-create
(squat) the per-uid root, plant a symlink or FIFO in its place, or create it with a mode that
lets them in.

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
  it is retried once and it is left when that fails too: ``create_reporting`` returns its
  path (``selfcheck.self_check`` turns it into a warning with the ``rm -rf`` command), and
  ``remove`` raises ``TmpdirLeftover`` naming it.
- A refusal names the failing entry (the base, the per-uid root or the run directory) by its
  full path and carries a hint for that entry; ``check`` and ``create`` report the same text.
  Before the per-uid root is opened, an ``lstat`` relative to the base learns its owner and
  type for the message only (a root another user created is reported as such even when it
  cannot be opened); the no-follow open and ``fstat`` checks stay authoritative.

``exec`` (``launch.check_tmpdir``) calls ``check`` at the temporary-directory slot of launch
step 5 and ``create`` (through ``selfcheck.self_check`` in ``launch.check_sandbox``, the last
action of step 5),
passes the path as ``Isolation.tmpdir`` and ``envbuild``'s ``tmpdir``, and calls ``remove``
when the run ends (``supervisor.finish``; a not-started run's in ``launch.end_launch``).
``start`` removes the tmpdir its self-check created; the adopting ``exec`` calls ``check``
and ``create`` again (``launch.checks``).
A resume's self-check creates the tmpdir again (``create`` clears a leftover of an earlier
run first).
Removal is identity-checked: ``create_pinned`` returns an open descriptor of the created
directory (the **pin**; it keeps the inode from being reused while it is open) and a run's
``remove`` passes it, so a late removal by run n (it is finalized, then cleans up) never
deletes the directory run n + 1 of the same session created in between: an entry that is not
the pinned directory is left alone. The removal renames the entry aside before it compares
it with the pin and removes it under the aside name, so run n + 1 creating its directory at
any moment in between is never affected; an entry that is not the pinned one is renamed
back when the session's name is still free (else left aside, like other ``.stale-*``
entries). The caller closes the pin after the removal.
``reconcile`` (and the liveness check, for a run it finalizes) removes the tmpdir of a session
whose current run is final, or of a deleted session, with ``remove_checked`` (no pin: the entry
is moved aside, then the store is read again before it is removed; every root is tried), and
the ``.stale-*`` entries of such sessions (``remove_stale``); entries of ids this state
directory does not know (another state directory's) are never touched. Besides the roots the
current environment names, lookups take ``extra_roots``: the fallback roots of the recorded
tmpdir paths (``recorded_root``), so a run made under ``$XDG_RUNTIME_DIR`` is found when the
variable is not set (cron, ssh); an existing fallback root that is unsafe is skipped and
reported (``session_entries(problems=...)``), never touched.
"""

from __future__ import annotations

import errno
import os
import secrets
import shlex
import stat
from collections.abc import Callable, Iterable
from typing import NamedTuple

from . import fsutil, ids, pathrules
from .errors import LaunchValidationError

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


class Created(NamedTuple):
    """What ``create_pinned`` made: the path, a leftover renamed aside and left there (None
    when nothing was left) and the pin, an open descriptor of the new directory (for
    ``remove``; the caller closes it)."""

    path: str
    stale_path: str | None
    pin: int


class _Refusal(fsutil.UnsafePathError):
    """An unsafe entry: ``path`` (full path), what is wrong with it and the hint for it."""

    def __init__(self, path: str, reason: str, hint: str, *, external: bool = False) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason
        self.hint = hint
        # Caused by something outside our own entries (another user's squat, a misconfigured
        # base): the fallback root is a remedy. A root of ours with a bad mode is not.
        self.external = external


def root_name(uid: int) -> str:
    """The name of the per-uid root of ``uid`` in the base: ``csession-<uid>``, as
    ``pathrules.tmp_parent`` names it (the single definition)."""
    return pathrules.tmp_root_name(uid)


def root_path(base: str | None = None) -> str:
    """``<realpath(base)>/<root_name(euid)>``; ``base`` must be absolute. ``base`` defaults to
    ``pathrules.TMP_ROOT``, read at call time: the one seam the test suite patches to keep its
    runs out of the real per-uid root (nothing else, and no environment variable, moves it)."""
    if base is None:
        base = pathrules.TMP_ROOT
    if not os.path.isabs(base):
        raise ValueError(f"base must be absolute: {base!r}")
    return os.path.join(os.path.realpath(base), root_name(os.geteuid()))


def runtime_root_path() -> str | None:
    """``<realpath($XDG_RUNTIME_DIR)>/csession``, the fallback root, or None when the variable
    is unset or unusable (``pathrules.runtime_tmp_parent``). Read at call time."""
    given = pathrules.runtime_tmp_parent()
    if given is None:
        return None
    return os.path.join(os.path.realpath(os.path.dirname(given)), os.path.basename(given))


def run_path(csession_id: str, base: str | None = None) -> str:
    """The private temporary directory of session ``csession_id`` in the root a launch uses
    now: ``select_root`` (nothing is created; an unsafe root is not reported here: ``check``
    and ``create`` do)."""
    _check_id(csession_id)
    return os.path.join(select_root(base), csession_id)


class _Candidate(NamedTuple):
    root: str  # full path of the root
    private_parent: bool  # the parent must be ours alone (the runtime directory)


def _candidates(base: str | None, extra_roots: Iterable[str] = ()) -> list[_Candidate]:
    """The roots in precedence order: the per-uid root in the base, then the runtime-dir
    root when ``$XDG_RUNTIME_DIR`` is usable, then (lookups only) ``extra_roots``: fallback
    roots found from recorded tmpdir paths (``recorded_root``), checked like the
    runtime-dir root."""
    found = [_Candidate(root_path(base), False)]
    runtime = runtime_root_path()
    if runtime is not None:
        found.append(_Candidate(runtime, True))
    for root in extra_roots:
        if all(candidate.root != root for candidate in found):
            found.append(_Candidate(root, True))
    return found


def recorded_root(tmp_dir: str) -> str | None:
    """The fallback root a recorded tmpdir path (the store's ``tmp_dir``) lives in, when it
    has the form ``<P>/csession/<csession_id>`` with ``P`` a usable runtime directory
    (``pathrules.usable_runtime_dir``); None otherwise (a path in the per-uid root is found
    through the base anyway). Lexical only: opening the root checks ``P``, its ancestors
    and the root like the ``$XDG_RUNTIME_DIR`` root (``_open_runtime_dir``)."""
    root, name = os.path.split(tmp_dir)
    parent, root_name_ = os.path.split(root)
    if (
        not ids.is_session_id(name)
        or root_name_ != pathrules.RUNTIME_ROOT_NAME
        or not pathrules.usable_runtime_dir(parent)
    ):
        return None
    return root


def select_root(base: str | None = None) -> str:
    """The root a launch uses: the first candidate that can be established (it exists and is
    safe, or is missing and could be created), else the per-uid root (the failure is
    reported by ``check``/``create``). Creates and changes nothing."""
    try:
        root, fd = _open_first(base, create=False)
    except _Refusal:
        return root_path(base)
    if fd is not None:
        os.close(fd)
    return root


def _open_first(base: str | None, *, create: bool) -> tuple[str, int | None]:
    """Open the first candidate root that is safe: ``(root, descriptor)``; the descriptor is
    None for a missing root that is not created (``create`` false) whose parent is safe.
    Raises ``_Refusal`` (``refusals``: every candidate's) when none is."""
    failures: list[_Refusal] = []
    for candidate in _candidates(base):
        try:
            return candidate.root, _open_candidate(candidate, create=create, probe=not create)
        except _Refusal as exc:
            failures.append(exc)
    raise _combine(failures, runtime_unusable=runtime_root_path() is None)


def _open_candidate(candidate: _Candidate, *, create: bool, probe: bool = False) -> int | None:
    """Open the candidate's parent and root (``_open_root``; ``probe``: a missing root whose
    parent we cannot write to fails, so a launch selects the next candidate up front)."""
    parent = os.path.dirname(candidate.root)
    parent_fd = _open_runtime_dir(parent) if candidate.private_parent else _open_base(parent)
    try:
        return _open_root(parent_fd, candidate.root, create=create, probe=probe)
    finally:
        os.close(parent_fd)


def _open_existing(
    base: str | None, extra_roots: Iterable[str] = (), problems: list[str] | None = None
) -> list[tuple[str, int]]:
    """Every candidate root that exists and is safe, opened (the caller closes them), for
    the operations that look a run up (removal, listing): a run can live in the fallback root
    after the per-uid root became usable again, or the reverse, and in a fallback root that
    ``extra_roots`` names (found from recorded tmpdir paths) when ``$XDG_RUNTIME_DIR`` is not
    set now. Raises ``_Refusal`` only when no root could be opened and the per-uid root is
    unsafe (as it did before the fallback existed): an unsafe fallback root is skipped,
    never touched, and, when it exists, described in ``problems`` (when given)."""
    opened: list[tuple[str, int]] = []
    primary_failure: _Refusal | None = None
    for position, candidate in enumerate(_candidates(base, extra_roots)):
        try:
            fd = _open_candidate(candidate, create=False)
        except _Refusal as exc:
            if position == 0:
                primary_failure = exc
            elif problems is not None and os.path.lexists(candidate.root):
                problems.append(
                    f"the private temporary directory root {candidate.root} is unsafe and was"
                    f" skipped: {exc.path}: {exc.reason}"
                )
            continue
        if fd is not None:
            opened.append((candidate.root, fd))
    if not opened and primary_failure is not None:
        raise primary_failure
    return opened


def _close_all(opened: list[tuple[str, int]]) -> None:
    for _, fd in opened:
        os.close(fd)


def check(
    csession_id: str | None, *, base: str | None = None, root: str | None = None
) -> str | None:
    """Validate the base and, when it exists, the per-uid root, creating and changing nothing
    (``--dry-run``, and the temporary-directory slot of launch step 5); returns the tmpdir
    path (None without a session id). ``root``: the root the launch chose (``select_root``,
    through ``run_path``); a different root being the one usable now is refused. Raises
    ``TmpdirUnsafe``."""
    if csession_id is not None:
        _check_id(csession_id)
    try:
        found, fd = _open_first(base, create=False)
    except _Refusal as exc:
        raise _unsafe(exc) from exc
    if fd is not None:
        os.close(fd)
    if root is not None and found != root:
        raise _unsafe(_changed(root, f"{found} is the usable root now"))
    return None if csession_id is None else os.path.join(found, csession_id)


def create(csession_id: str, *, base: str | None = None) -> str:
    """Create the session's private temporary directory, empty and 0700, under a checked
    per-uid root (created when missing); a leftover directory (or any entry) of that name is
    removed first without following symlinks, or renamed aside when it cannot be removed.
    Returns its path. Raises ``TmpdirUnsafe``."""
    return create_reporting(csession_id, base=base)[0]


def create_reporting(csession_id: str, *, base: str | None = None) -> tuple[str, str | None]:
    """``create``, also returning the full path of a leftover that was renamed aside and could
    not be removed under its new name either (None when nothing was left)."""
    created = create_pinned(csession_id, base=base)
    os.close(created.pin)
    return created.path, created.stale_path


def create_pinned(csession_id: str, *, base: str | None = None, root: str | None = None) -> Created:
    """``create_reporting``, also returning the pin: an open descriptor of the created
    directory, which the run's ``remove`` passes so that it never removes a directory
    another run created. The caller closes it. ``root``: the root the launch chose at its
    check (``select_root``); only that root is tried, and when it can no longer be
    established there the creation is refused (``TmpdirUnsafe``), never moved to the other
    root. None: the first candidate that can be established."""
    stale_path: str | None = None
    _check_id(csession_id)
    try:
        if root is None:
            root, root_fd = _open_first(base, create=True)
        else:
            root_fd = _open_chosen(base, root)
        assert root_fd is not None
        path = os.path.join(root, csession_id)
        try:
            if _exists_at(root_fd, csession_id):
                stale_path = _clear_leftover(root_fd, root, csession_id)
            try:
                run_fd = fsutil.create_private_dir_at(root_fd, csession_id)
            except OSError as exc:
                raise _refusal(path, exc, _run_hint(path)) from exc
        finally:
            os.close(root_fd)
    except _Refusal as exc:
        raise _unsafe(exc) from exc
    return Created(path, stale_path, run_fd)


def _open_chosen(base: str | None, root: str) -> int:
    """Open (creating when missing) the chosen root ``root`` only. Raises ``_Refusal`` when
    it is no longer a candidate or cannot be established."""
    for candidate in _candidates(base):
        if candidate.root == root:
            try:
                fd = _open_candidate(candidate, create=True)
            except _Refusal as exc:
                raise _changed(exc.path, exc.reason, exc.hint) from exc
            assert fd is not None
            return fd
    raise _changed(root, "no longer a candidate root")


def _changed(path: str, reason: str, hint: str | None = None) -> _Refusal:
    """The refusal when the root chosen at the launch's check cannot be used at creation."""
    return _Refusal(
        path,
        f"{reason} (the root was chosen when the launch was checked; no other root is tried)",
        hint or "run again: the root is chosen anew at the next launch",
    )


def remove(
    csession_id: str,
    *,
    base: str | None = None,
    pin: int | None = None,
    extra_roots: Iterable[str] = (),
) -> bool:
    """Remove the session's private temporary directory and everything in it, never
    following a symlink. Returns False when there was nothing to remove (idempotent; a
    missing root is not created). With ``pin`` (``create_pinned``), the entry is first
    renamed aside and the renamed entry compared with the pin, so a directory another run of
    the session creates meanwhile is never removed: an entry that is not the pinned
    directory is renamed back when its name is still free (else left aside as a
    ``.stale-*`` entry) and False returned. Raises ``OSError``:
    ``fsutil.UnsafePathError`` for an unsafe base or root, which is left untouched;
    ``TmpdirLeftover`` when the directory could not be removed (it is renamed aside first,
    so the session's path is free)."""
    _check_id(csession_id)
    opened = _open_existing(base, extra_roots)
    try:
        for root, root_fd in opened:
            if _remove_in(root, root_fd, csession_id, pin):
                return True
    finally:
        _close_all(opened)
    return False


def _remove_in(root: str, root_fd: int, csession_id: str, pin: int | None) -> bool:
    """``remove`` in one root (``root_fd`` stays open for the caller)."""
    path = os.path.join(root, csession_id)
    name = csession_id
    if pin is not None:
        # Move the entry aside first and check what was moved: run n + 1 may create its
        # own directory under the session's name at any moment, so the removal below
        # must never go by that name once the identity is checked.
        try:
            name = _rename_aside(root_fd, csession_id)
        except FileNotFoundError:
            return False
        pinned = os.fstat(pin)
        if _identity_at(root_fd, name) != (pinned.st_dev, pinned.st_ino):
            _put_back(root_fd, name, csession_id)
            return False
    elif _identity_at(root_fd, csession_id) is None:
        return False
    try:
        fsutil.remove_tree_at(root_fd, name)
    except OSError as exc:
        reason = _reason(exc)
        if name == csession_id:
            try:
                name = _rename_aside(root_fd, csession_id)
            except OSError:
                raise TmpdirLeftover(
                    f"private temporary directory {path} could not be removed: {reason}",
                    path=path,
                    stale_path=None,
                    hint=_leftover_hint(path),
                ) from exc
        stale_path = os.path.join(root, name)
        raise TmpdirLeftover(
            f"private temporary directory {path} could not be removed ({reason});"
            f" it was moved aside to {stale_path}",
            path=path,
            stale_path=stale_path,
            hint=_leftover_hint(stale_path),
        ) from exc
    return True


def remove_checked(
    csession_id: str,
    still_removable: Callable[[], bool],
    *,
    base: str | None = None,
    extra_roots: Iterable[str] = (),
) -> bool:
    """Remove the session's private temporary directory without a pin (``reconcile`` and the
    liveness check, for a session whose current run is final), never one a next run of the
    session made: the entry is renamed aside first, then ``still_removable()`` (the store
    read again: the same current run, still final) decides; when it says no, the entry is
    renamed back (when its name is free; else it stays aside, a ``.stale-*`` entry). A new
    run creates its directory only after it is allocated, so an entry moved aside before a
    read that still sees no new run cannot be that run's. Returns whether it was removed.
    Raises ``OSError`` as ``remove`` does (``TmpdirLeftover`` when it could not be removed:
    it stays aside for ``remove_stale``). Every root is tried (``extra_roots`` as for
    ``remove``): the same session can have an entry in each."""
    _check_id(csession_id)
    opened = _open_existing(base, extra_roots)
    removed = False
    try:
        for root, root_fd in opened:
            if _remove_checked_in(root, root_fd, csession_id, still_removable):
                removed = True
    finally:
        _close_all(opened)
    return removed


def _remove_checked_in(
    root: str, root_fd: int, csession_id: str, still_removable: Callable[[], bool]
) -> bool:
    path = os.path.join(root, csession_id)
    try:
        name = _rename_aside(root_fd, csession_id)
    except FileNotFoundError:
        return False
    if not still_removable():
        _put_back(root_fd, name, csession_id)
        return False
    try:
        fsutil.remove_tree_at(root_fd, name)
    except OSError as exc:
        stale_path = os.path.join(root, name)
        raise TmpdirLeftover(
            f"private temporary directory {path} could not be removed ({_reason(exc)});"
            f" it was moved aside to {stale_path}",
            path=path,
            stale_path=stale_path,
            hint=_leftover_hint(stale_path),
        ) from exc
    return True


def session_entries(
    *,
    base: str | None = None,
    extra_roots: Iterable[str] = (),
    problems: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """The names in every usable root (the per-uid root, the ``$XDG_RUNTIME_DIR`` root and
    ``extra_roots``; none when missing): the entries named like a session id, and the
    ``.stale-*`` entries. Read only. An existing fallback root that is unsafe is skipped and
    described in ``problems`` (when given). Raises ``OSError`` for an unsafe base or per-uid
    root when no root could be opened."""
    opened = _open_existing(base, extra_roots, problems)
    try:
        names = sorted({name for _, fd in opened for name in os.listdir(fd)})
    finally:
        _close_all(opened)
    sessions = [name for name in names if ids.is_session_id(name)]
    stale = [name for name in names if stale_session(name) is not None]
    return sessions, stale


def stale_session(name: str) -> str | None:
    """The session id of a ``.stale-<id>-<hex>`` entry name, or None for any other name."""
    if not name.startswith(STALE_PREFIX):
        return None
    sid, sep, token = name[len(STALE_PREFIX) :].rpartition("-")
    if (
        not sep
        or not ids.is_session_id(sid)
        or not token
        or not all(c in "0123456789abcdef" for c in token)
    ):
        return None
    return sid


def remove_stale(name: str, *, base: str | None = None, extra_roots: Iterable[str] = ()) -> bool:
    """Remove the ``.stale-*`` entry ``name`` from the root that holds it (the roots of
    ``remove``; fd-based, never following a symlink). The caller decides it is abandoned
    (``reconcile``: its session's current run was final after the entry was listed).
    Returns False when it is gone already. Raises ``OSError`` when it cannot be removed."""
    if stale_session(name) is None:
        raise ValueError(f"not a stale entry: {name!r}")
    opened = _open_existing(base, extra_roots)
    try:
        for _, root_fd in opened:
            if _identity_at(root_fd, name) is not None:
                fsutil.remove_tree_at(root_fd, name)
                return True
    finally:
        _close_all(opened)
    return False


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
        raise _external(_refusal(base, exc, _base_hint(base))) from exc
    try:
        info = os.fstat(fd)
        euid = os.geteuid()
        owners = {euid if uid is None else uid for uid in _TRUSTED_BASE_OWNERS}
        if info.st_uid not in owners:
            raise _Refusal(
                base,
                f"owned by uid {info.st_uid}, neither root nor you",
                _base_owner_hint(base, info.st_uid),
                external=True,
            )
        writable = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable and not info.st_mode & stat.S_ISVTX:
            raise _Refusal(
                base,
                "writable by others and without the sticky bit",
                _base_hint(base),
                external=True,
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_runtime_dir(parent: str) -> int:
    """Open the resolved ``$XDG_RUNTIME_DIR`` without following a symlink and check it: a
    directory owned by us that neither group nor others can write (a sticky bit is not
    enough here: the fallback exists because the shared base cannot be trusted), whose
    every ancestor is owned by root or us and, when group- or other-writable, sticky (so
    nobody else can swap a component of the path, which is used by name in ``TMPDIR`` and
    the sandbox's ``allowWrite``)."""
    _check_runtime_ancestors(parent)
    try:
        fd = fsutil.open_dir_nofollow(parent)
    except OSError as exc:
        raise _refusal(parent, exc, _runtime_hint(parent)) from exc
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid():
            raise _Refusal(parent, f"owned by uid {info.st_uid}, not you", _runtime_hint(parent))
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _Refusal(
                parent,
                f"mode {stat.S_IMODE(info.st_mode):04o} is writable by others",
                _runtime_hint(parent),
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _check_runtime_ancestors(parent: str) -> None:
    euid = os.geteuid()
    ancestor = os.path.dirname(parent)
    while True:
        try:
            info = os.stat(ancestor, follow_symlinks=False)
        except OSError as exc:
            raise _refusal(ancestor, exc, _runtime_hint(parent)) from exc
        if info.st_uid not in (0, euid):
            raise _Refusal(
                parent,
                f"its ancestor {ancestor} is owned by uid {info.st_uid}, neither root nor you",
                _runtime_hint(parent),
            )
        writable = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable and not info.st_mode & stat.S_ISVTX:
            raise _Refusal(
                parent,
                f"its ancestor {ancestor} is writable by others and without the sticky bit",
                _runtime_hint(parent),
            )
        if ancestor == os.path.dirname(ancestor):
            return
        ancestor = os.path.dirname(ancestor)


def _combine(failures: list[_Refusal], *, runtime_unusable: bool) -> _Refusal:
    """The refusal when no candidate root is safe: the per-uid root's (the first), with the
    fallback's appended when there was one, or why there was none."""
    primary = failures[0]
    reason, hint = primary.reason, primary.hint
    if len(failures) > 1:
        fallback = failures[1]
        reason = f"{reason}; fallback {fallback.path}: {fallback.reason}"
        hint = f"{hint}; fallback: {fallback.hint}"
    elif os.environ.get("XDG_RUNTIME_DIR"):
        reason = (
            f"{reason}; $XDG_RUNTIME_DIR is unusable (relative, not normalized, /, or holding"
            " control characters), so no fallback"
        )
    if runtime_unusable and primary.external:
        hint = f"{hint}; or set $XDG_RUNTIME_DIR to a directory you own (mode 0700), the fallback"
    return _Refusal(primary.path, reason, hint)


def _open_root(base_fd: int, root: str, *, create: bool, probe: bool = False) -> int | None:
    """Open the per-uid root ``root`` (full path) in the base. ``create``: make it when
    missing (0700 whatever the umask). An existing root must be a real directory owned by us
    with mode exactly 0700; it is never repaired. Returns None when it is missing and not
    created, unless ``probe`` and we cannot write to its parent (refused: it could not be
    created). Raises ``_Refusal``."""
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
        parent = os.path.dirname(root)
        if probe and not _can_create_in(parent):
            # It could not be created at the launch: select the next candidate now.
            raise _Refusal(
                root,
                f"missing, and {parent} is not writable by you",
                _base_hint(parent),
                external=True,
            ) from exc
        return None
    except OSError as exc:
        raise _refusal(root, exc, _root_hint(root)) from exc
    try:
        _check_root(fd, root)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _can_create_in(parent: str) -> bool:
    effective = os.access in os.supports_effective_ids
    return os.access(parent, os.W_OK | os.X_OK, effective_ids=effective)


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
        raise _Refusal(root, f"owned by uid {info.st_uid}{kind}", _squat_hint(root), external=True)


def _check_root(fd: int, root: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise _Refusal(root, "not a directory", _root_hint(root))
    if info.st_uid != os.geteuid():
        raise _Refusal(root, f"owned by uid {info.st_uid}", _squat_hint(root), external=True)
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise _Refusal(root, f"mode {stat.S_IMODE(info.st_mode):04o}, not 0700", _root_hint(root))


def _clear_leftover(root_fd: int, root: str, name: str) -> str | None:
    """Remove the leftover entry ``name`` in the root; when that fails, rename it aside and
    try once to remove it under its new name. Returns the full path of what is left there
    when that fails too, else None."""
    try:
        fsutil.remove_tree_at(root_fd, name)
        return None
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
    # reconcile removes what is left (remove_stale).
    try:
        fsutil.remove_tree_at(root_fd, stale)
    except OSError:
        return os.path.join(root, stale)  # the session's path is free; reported by the caller
    return None


def _rename_aside(root_fd: int, name: str) -> str:
    """Rename ``name`` in the root to a fresh ``.stale-<name>-<hex>``, fd-relative; returns
    the new name."""
    stale = f"{STALE_PREFIX}{name}-{secrets.token_hex(8)}"
    os.rename(name, stale, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    return stale


def _put_back(root_fd: int, stale: str, name: str) -> None:
    """Rename the entry ``stale`` that ``remove`` moved aside but did not create back to
    ``name`` when that name is still free; otherwise (or when the rename fails) it stays
    aside, a ``.stale-*`` entry like any other (``reconcile`` removes those)."""
    if _exists_at(root_fd, name):
        return
    try:
        os.rename(stale, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    except OSError:
        pass


def _exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _identity_at(dir_fd: int, name: str) -> tuple[int, int] | None:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


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


def _external(refusal: _Refusal) -> _Refusal:
    refusal.external = True
    return refusal


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


def _runtime_hint(parent: str) -> str:
    return (
        f"$XDG_RUNTIME_DIR ({parent}) must be a directory you own that others cannot write"
        f" (mode 0700): fix it, unset it, or {_SANDBOX_OFF}"
    )


def _root_hint(root: str) -> str:
    return f"remove it (rm -rf {shlex.quote(root)}) and run again: it is recreated with mode 0700"


def _squat_hint(root: str) -> str:
    return (
        f"another user created {root}: ask an administrator to remove it, or"
        f" {_SANDBOX_OFF} until then"
    )


def _run_hint(path: str) -> str:
    return f"remove it (rm -rf {shlex.quote(path)}) and run again"


def _leftover_hint(path: str) -> str:
    return f"remove it: rm -rf {shlex.quote(path)}"
