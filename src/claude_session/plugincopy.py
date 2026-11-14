"""The per-run copy of a ``--plugin-dir`` directory for sandboxed runs (interface.md,
Isolation, Plugin directories).

Claude Code's sandbox masks ``.claude/``, ``.mcp.json``, ``CLAUDE.md`` and ``CLAUDE.local.md``
inside every readable ``--add-dir`` directory, and every sandboxed Bash call fails when a
mask target does not exist in a read-only directory. A sandboxed run therefore gets a copy
of each plugin directory in its session directory, ``plugins/<run>/<n>/<name>/``, with the
four entries added when the plugin does not ship them; the caller's directory is never
written. The copy is the directory the run passes as ``--plugin-dir`` and ``--add-dir`` and
makes read-only (sandbox ``denyWrite`` and ``Edit``/``Write`` denies).

The source is read through directory descriptors opened without following symlinks. A
symlink, a special file (FIFO, socket, device) or an unreadable entry is refused
(``unsafe_plugin_dir``), as is a tree past the bounds below; nothing is followed out of the
plugin directory. Directories are written 0700 and files 0600 (0700 when the source has an
executable bit: plugin hooks are scripts), whatever the umask. ``scan`` makes the same
checks reading only (the launch validations and the dry run); ``copy`` writes.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Final

from . import fsutil
from .errors import LaunchValidationError

# Bounds on a plugin tree: entries (files and directories), total file bytes and depth.
MAX_ENTRIES: Final = 10_000
MAX_BYTES: Final = 64 * 1024 * 1024
MAX_DEPTH: Final = 32

PLUGINS = "plugins"
END_REASON = "unsafe_plugin_dir"

# Name, kind and content of each entry Claude Code's sandbox masks. The ``.mcp.json`` is
# valid, empty MCP configuration: Claude Code loads a plugin's ``.mcp.json`` itself.
_DIR: Final = None
PLACEHOLDERS: Final[tuple[tuple[str, bytes | None], ...]] = (
    (".claude", _DIR),
    (".mcp.json", b'{"mcpServers": {}}\n'),
    ("CLAUDE.md", b""),
    ("CLAUDE.local.md", b""),
)

_CHUNK = 1 << 16
_OPEN = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass(frozen=True)
class Report:
    """What a scan or copy found: the placeholders added (names), the source entries and the
    file bytes counted."""

    added: tuple[str, ...]
    entries: int
    size: int


@dataclass(frozen=True)
class Planned:
    """One plugin directory of a sandboxed run: the caller's ``source``, the ``effective``
    directory the run uses (its copy) and the placeholders the copy adds."""

    source: str
    effective: str
    added: tuple[str, ...]


def effective_path(session_path: str, run: int, index: int, source: str) -> str:
    """Where run ``run`` copies the plugin directory ``source``, the ``index``-th of the
    run's: the directory name is kept, since Claude Code names a plugin without a manifest
    after it."""
    name = os.path.basename(source.rstrip("/")) or "plugin"
    return os.path.join(session_path, PLUGINS, str(run), str(index), name)


def _refuse(source: str, why: str) -> LaunchValidationError:
    shown = source.encode("utf-8", "backslashreplace").decode("utf-8")
    return LaunchValidationError(
        f"plugin directory {shown!r} cannot be copied: {why}", end_reason=END_REASON
    )


class _Walk:
    def __init__(self, source: str, max_entries: int, max_bytes: int, max_depth: int) -> None:
        self.source = source
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_depth = max_depth
        self.entries = 0
        self.size = 0

    def _count_entry(self, rel: str) -> None:
        self.entries += 1
        if self.entries > self.max_entries:
            raise _refuse(self.source, f"more than {self.max_entries} entries (at {rel!r})")

    def _count_bytes(self, rel: str, size: int) -> None:
        self.size += size
        if self.size > self.max_bytes:
            raise _refuse(self.source, f"more than {self.max_bytes} bytes (at {rel!r})")

    def directory(self, src_fd: int, dst_fd: int | None, rel: str, depth: int) -> tuple[str, ...]:
        """Visit the directory ``src_fd`` (copying into ``dst_fd`` when given); return the
        names of the placeholders missing at the top level."""
        if depth > self.max_depth:
            raise _refuse(self.source, f"deeper than {self.max_depth} levels (at {rel!r})")
        try:
            names = sorted(os.listdir(src_fd))
        except OSError as exc:
            raise _refuse(self.source, f"{rel or '.'!r} cannot be listed ({exc.strerror})") from exc
        for name in names:
            self.entry(src_fd, dst_fd, name, f"{rel}/{name}" if rel else name, depth)
        if depth > 0:
            return ()
        present = set(names)
        return tuple(name for name, _ in PLACEHOLDERS if name not in present)

    def entry(self, src_fd: int, dst_fd: int | None, name: str, rel: str, depth: int) -> None:
        self._count_entry(rel)
        try:
            info = os.stat(name, dir_fd=src_fd, follow_symlinks=False)
        except OSError as exc:
            raise _refuse(self.source, f"{rel!r} cannot be inspected ({exc.strerror})") from exc
        if stat.S_ISLNK(info.st_mode):
            raise _refuse(self.source, f"{rel!r} is a symlink")
        if stat.S_ISDIR(info.st_mode):
            self.subdirectory(src_fd, dst_fd, name, rel, depth, info)
        elif stat.S_ISREG(info.st_mode):
            self.file(src_fd, dst_fd, name, rel, info)
        else:
            raise _refuse(self.source, f"{rel!r} is not a regular file or directory")

    def subdirectory(
        self, src_fd: int, dst_fd: int | None, name: str, rel: str, depth: int, info: os.stat_result
    ) -> None:
        try:
            fd = os.open(name, _OPEN | os.O_DIRECTORY, dir_fd=src_fd)
        except OSError as exc:
            raise _refuse(self.source, f"{rel!r} cannot be opened ({exc.strerror})") from exc
        out_fd = None
        try:
            if not _same(fd, info):
                raise _refuse(self.source, f"{rel!r} changed while it was being read")
            if dst_fd is not None:
                out_fd = fsutil.create_private_dir_at(dst_fd, name)
            self.directory(fd, out_fd, rel, depth + 1)
        finally:
            os.close(fd)
            if out_fd is not None:
                os.close(out_fd)

    def file(
        self, src_fd: int, dst_fd: int | None, name: str, rel: str, info: os.stat_result
    ) -> None:
        self._count_bytes(rel, info.st_size)
        try:
            fd = os.open(name, _OPEN, dir_fd=src_fd)
        except OSError as exc:
            raise _refuse(self.source, f"{rel!r} cannot be read ({exc.strerror})") from exc
        try:
            if not _same(fd, info):
                raise _refuse(self.source, f"{rel!r} changed while it was being read")
            if dst_fd is not None:
                self.write(fd, dst_fd, name, rel, info)
        finally:
            os.close(fd)

    def write(self, fd: int, dst_fd: int, name: str, rel: str, info: os.stat_result) -> None:
        mode = 0o700 if info.st_mode & 0o111 else 0o600
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        out = os.open(name, flags, mode, dir_fd=dst_fd)
        try:
            os.fchmod(out, mode)
            read = 0
            while True:
                try:
                    chunk = os.read(fd, _CHUNK)
                except OSError as exc:
                    raise _refuse(self.source, f"{rel!r} cannot be read ({exc.strerror})") from exc
                if not chunk:
                    break
                read += len(chunk)
                if read > info.st_size:  # grew since it was counted
                    self._count_bytes(rel, len(chunk))
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view) :]
        finally:
            os.close(out)


def _same(fd: int, info: os.stat_result) -> bool:
    now = os.fstat(fd)
    return (now.st_dev, now.st_ino) == (info.st_dev, info.st_ino)


def _open_source(source: str) -> int:
    try:
        return fsutil.open_dir_nofollow(source)
    except OSError as exc:
        raise _refuse(source, f"cannot be opened ({exc.strerror})") from exc


def scan(
    source: str,
    *,
    max_entries: int = MAX_ENTRIES,
    max_bytes: int = MAX_BYTES,
    max_depth: int = MAX_DEPTH,
) -> Report:
    """Check the resolved plugin directory ``source`` as ``copy`` does, writing nothing.
    Raises ``LaunchValidationError`` (``unsafe_plugin_dir``)."""
    walk = _Walk(source, max_entries, max_bytes, max_depth)
    fd = _open_source(source)
    try:
        added = walk.directory(fd, None, "", 0)
    finally:
        os.close(fd)
    return Report(added, walk.entries, walk.size)


def copy(
    source: str,
    dst_parent_fd: int,
    name: str,
    *,
    max_entries: int = MAX_ENTRIES,
    max_bytes: int = MAX_BYTES,
    max_depth: int = MAX_DEPTH,
) -> Report:
    """Copy the resolved plugin directory ``source`` to ``name`` (which must not exist) in
    the directory ``dst_parent_fd``, then add the
    placeholders the plugin lacks. Raises ``LaunchValidationError`` (``unsafe_plugin_dir``),
    ``FileExistsError`` and other ``OSError`` from writing; a failed copy may be partial."""
    walk = _Walk(source, max_entries, max_bytes, max_depth)
    src_fd = _open_source(source)
    out_fd = None
    try:
        out_fd = fsutil.create_private_dir_at(dst_parent_fd, name)
        added = walk.directory(src_fd, out_fd, "", 0)
        for placeholder, content in PLACEHOLDERS:
            if placeholder in added:
                _add(out_fd, placeholder, content)
    finally:
        os.close(src_fd)
        if out_fd is not None:
            os.close(out_fd)
    return Report(added, walk.entries, walk.size)


def _add(dir_fd: int, name: str, content: bytes | None) -> None:
    if content is None:
        os.close(fsutil.create_private_dir_at(dir_fd, name))
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(content)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)
