"""``transcript``: find a session's Claude Code transcripts, read them safely and render them.

The layout knowledge lives here only (design.md, Transcripts): the main file is the
``transcript_path`` a ``SessionStart`` payload recorded in ``claude_ids``; subagents are
``<dir>/<claude_session_id>/subagents/agent-<id>.jsonl`` with ``agent-<id>.meta.json``
(``agentType``, ``description``, ``toolUseId``); offloaded tool output is
``<dir>/<claude_session_id>/tool-results/``. Nothing is ever copied.

Everything here is untrusted: the recorded paths (a hook payload), the files (written by Claude
Code, steered by the agent) and their content. So:

- A recorded path is used only when it is ``<projects root>/<project>/<claude id>.jsonl`` for a
  projects root of this user (``$HOME/.claude/projects`` or ``$CLAUDE_CONFIG_DIR/projects``),
  in normal form and printable unchanged; any other is reported with ``path: null`` and a
  warning, never opened.
- Files and directories are opened one component at a time below the projects root without
  following symlinks (``O_NOFOLLOW``) and without blocking on a FIFO (``O_NONBLOCK``). A file
  must be a regular file with one link owned by the user, a directory a real directory owned
  by the user; anything else fails the command (``failed``, exit 12). A missing one is
  reported (``exists: false``), never fatal.
- A file is read only up to its size when it was opened (a live session keeps appending), a
  JSONL record longer than ``RECORD_MAX_BYTES`` is skipped, records that are not JSON objects
  (malformed, too deeply nested, invalid UTF-8 replaced) are skipped, and the whole command
  stops (``failed``) once it has spent ``READ_DEADLINE`` seconds reading.
- Rendered text passes every value through ``terminal.display`` (one line each, with control,
  bidi and invisible characters escaped); ``--raw`` copies the bytes unchanged.

The record format (``type`` ``user``/``assistant``, ``message.content`` as a string or a list
of ``text``/``tool_use``/``tool_result`` blocks) is not covered by design.md's verified
results; the renderer is tolerant, so unknown or changed records are skipped, not fatal.
"""

from __future__ import annotations

import errno
import itertools
import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from . import fsutil
from .errors import FailedError
from .terminal import display, no_surrogates

# Claude Code session ids are lowercase UUIDs (hook-event refuses anything else).
CLAUDE_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Subagent files; other names in the subagents directory are ignored.
AGENT_FILE = re.compile(r"agent-([A-Za-z0-9_-]{1,128})\.jsonl")
AGENT_META = re.compile(r"agent-([A-Za-z0-9_-]{1,128})\.meta\.json")
# Offloaded tool-result names referenced from tool-result text.
TOOL_RESULT_REF = re.compile(r"tool-results/([A-Za-z0-9._-]{1,255})")
SOURCES = frozenset({"startup", "resume", "fork", "clear", "compact", "other"})

# Read limits.
META_MAX_BYTES = 64 * 1024
RECORD_MAX_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
MAX_DIR_ENTRIES = 10_000
READ_DEADLINE = 300.0
# Render limits.
RESULT_LINES = 20
LINE_MAX_CHARS = 4000
SUMMARY_MAX_CHARS = 200
# Input fields shown in a tool call's one-line summary, the first present one wins.
SUMMARY_KEYS = (
    "command",
    "file_path",
    "notebook_path",
    "path",
    "pattern",
    "url",
    "query",
    "description",
    "prompt",
    "skill",
)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class _Missing(Exception):
    """A file or directory that does not exist (Claude Code deleted it, or never wrote it)."""


# Allowed locations


def projects_roots(environ: Mapping[str, str]) -> list[str]:
    """The directories transcripts may live under: ``$HOME/.claude/projects`` and, when set,
    ``$CLAUDE_CONFIG_DIR/projects`` (Claude Code's own relocation). Relative or empty values
    are ignored."""
    roots: list[str] = []
    home = environ.get("HOME", "")
    if os.path.isabs(home) and "\0" not in home:
        roots.append(os.path.join(os.path.normpath(home), ".claude", "projects"))
    config_dir = environ.get("CLAUDE_CONFIG_DIR", "")
    if os.path.isabs(config_dir) and "\0" not in config_dir:
        roots.append(os.path.join(os.path.normpath(config_dir), "projects"))
    return list(dict.fromkeys(roots))


@dataclass(frozen=True)
class Location:
    """Where a main transcript lives: ``real_root`` (the resolved projects root, opened
    without following symlinks), the ``project`` directory name below it, the claude id and
    the recorded ``path`` (also the base of every derived path that is printed)."""

    real_root: str
    project: str
    claude_session_id: str
    path: str

    @property
    def directory(self) -> str:
        return os.path.dirname(self.path)

    @property
    def name(self) -> str:
        return f"{self.claude_session_id}.jsonl"

    def subagent_path(self, agent_id: str) -> str:
        return os.path.join(
            self.directory, self.claude_session_id, "subagents", f"agent-{agent_id}.jsonl"
        )

    def tool_result_path(self, name: str) -> str:
        return os.path.join(self.directory, self.claude_session_id, "tool-results", name)


def locate(path: object, claude_session_id: object, roots: Sequence[str]) -> Location | None:
    """The ``Location`` of a recorded transcript path, or None when it is not
    ``<root>/<project>/<claude_session_id>.jsonl`` for one of ``roots`` (lexically, or below
    the root's realpath), in normal form and printable unchanged."""
    if not isinstance(path, str) or not isinstance(claude_session_id, str):
        return None
    if CLAUDE_SESSION_ID.fullmatch(claude_session_id) is None:
        return None
    if not os.path.isabs(path) or display(path) != path or os.path.normpath(path) != path:
        return None
    if path.startswith("//"):
        return None  # POSIX leaves a leading // implementation-defined
    parent, name = os.path.split(path)
    project_root, project = os.path.split(parent)
    if name != f"{claude_session_id}.jsonl" or project in ("", ".", ".."):
        return None
    for root in roots:
        real = os.path.realpath(root)
        if project_root in (root, real):
            return Location(real, project, claude_session_id, path)
    return None


# Safe reading


class Reader:
    """Opens files below a projects root without following symlinks, within one deadline
    for the whole command (``clock`` is ``time.monotonic`` outside tests)."""

    def __init__(
        self,
        *,
        deadline: float = READ_DEADLINE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._limit = deadline
        self._end = clock() + deadline

    def check_time(self) -> None:
        if self._clock() > self._end:
            raise FailedError(f"reading transcripts took longer than {self._limit:g} seconds")

    def open_dir(self, loc: Location, segments: Sequence[str]) -> int:
        """The directory ``<root>/<project>/<segments...>``, each component opened without
        following symlinks and checked to be a directory owned by the user. Raises
        ``_Missing`` or ``FailedError``."""
        self.check_time()
        shown = os.path.join(loc.directory, *segments)
        try:
            fd = fsutil.open_dir_nofollow(loc.real_root)
        except FileNotFoundError:
            raise _Missing(shown) from None
        except OSError as exc:
            raise _failed(shown, exc) from None
        try:
            for part in (loc.project, *segments):
                next_fd = _open_entry(fd, part, _DIR_FLAGS, shown)
                os.close(fd)
                fd = next_fd
                _check_owned_dir(fd, shown)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def open_file(self, loc: Location, segments: Sequence[str], name: str) -> int:
        """The regular file ``name`` in ``open_dir(loc, segments)``: one link, owned by the
        user. Raises ``_Missing`` or ``FailedError``."""
        shown = os.path.join(loc.directory, *segments, name)
        dir_fd = self.open_dir(loc, segments)
        try:
            fd = _open_entry(dir_fd, name, _FILE_FLAGS, shown)
        finally:
            os.close(dir_fd)
        try:
            _check_file(fd, shown)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def list_dir(self, loc: Location, segments: Sequence[str]) -> list[str]:
        """The entry names of a directory (sorted; at most ``MAX_DIR_ENTRIES``), or ``[]``
        when it is missing."""
        try:
            fd = self.open_dir(loc, segments)
        except _Missing:
            return []
        try:
            names = sorted(os.listdir(fd))
        except OSError as exc:
            raise _failed(os.path.join(loc.directory, *segments), exc) from None
        finally:
            os.close(fd)
        return names[:MAX_DIR_ENTRIES]

    def chunks(self, fd: int, shown: str = "") -> Iterator[bytes]:
        """The bytes of the open file ``fd`` (``shown`` in errors) up to its size now, in
        chunks. A read error raises ``FailedError``."""
        try:
            remaining = os.fstat(fd).st_size
        except OSError as exc:
            raise _failed(shown, exc) from None
        while remaining > 0:
            self.check_time()
            try:
                data = os.read(fd, min(CHUNK_BYTES, remaining))
            except OSError as exc:
                raise _failed(shown, exc) from None
            if not data:
                return
            remaining -= len(data)
            yield data


def _open_entry(dir_fd: int, name: str, flags: int, shown: str) -> int:
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        raise _Missing(shown) from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise FailedError(
                "unsafe transcript file", detail=f"{shown}: a symlink or not a directory"
            ) from None
        raise _failed(shown, exc) from None


def _check_owned_dir(fd: int, shown: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise FailedError(
            "unsafe transcript file", detail=f"{shown}: not a directory owned by this user"
        )


def _check_file(fd: int, shown: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise FailedError("unsafe transcript file", detail=f"{shown}: not a regular file")
    if info.st_nlink != 1:
        raise FailedError("unsafe transcript file", detail=f"{shown}: {info.st_nlink} links")
    if info.st_uid != os.geteuid():
        raise FailedError("unsafe transcript file", detail=f"{shown}: owned by another user")


def _failed(shown: str, exc: OSError) -> FailedError:
    return FailedError("cannot read transcript file", detail=f"{shown}: {exc.strerror or exc}")


# Discovery


@dataclass
class Subagent:
    agent_id: str
    agent_type: str | None
    description: str | None
    tool_use_id: str | None
    path: str
    exists: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "description": self.description,
            "tool_use_id": self.tool_use_id,
            "path": self.path,
            "exists": self.exists,
        }


@dataclass
class TranscriptFile:
    claude_session_id: str
    run: int
    source: str
    path: str | None
    exists: bool
    location: Location | None = None
    subagents: list[Subagent] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "claude_session_id": self.claude_session_id,
            "run": self.run,
            "source": self.source,
            "path": self.path,
            "exists": self.exists,
            "subagents": [sub.as_json() for sub in self.subagents],
        }


@dataclass
class Listing:
    csession_id: str
    files: list[TranscriptFile]
    warnings: list[str]

    def as_json(self) -> dict[str, Any]:
        return {"csession_id": self.csession_id, "files": [f.as_json() for f in self.files]}


def discover(
    csession_id: str,
    rows: Sequence[Mapping[str, Any]],
    roots: Sequence[str],
    reader: Reader,
    *,
    run_no: int | None = None,
) -> Listing:
    """The transcript files of ``rows`` (``claude_ids`` rows in the order recorded), those of
    run ``run_no`` only when given, each with its subagents."""
    files: list[TranscriptFile] = []
    warnings: list[str] = []
    for row in rows:
        run = row.get("run_no")
        if not isinstance(run, int) or isinstance(run, bool):
            continue
        if run_no is not None and run != run_no:
            continue
        claude_id = _text(row.get("claude_session_id")) or ""
        source = _text(row.get("source"))
        stored = row.get("transcript_path")
        loc = locate(stored, claude_id, roots)
        entry = TranscriptFile(
            claude_session_id=claude_id,
            run=run,
            source=source if source in SOURCES else "other",
            path=None if loc is None else loc.path,
            exists=False,
            location=loc,
        )
        if loc is None:
            if stored is None:
                warnings.append(f"no transcript path was recorded for Claude session {claude_id}")
            else:
                warnings.append(
                    f"the recorded transcript path of Claude session {claude_id} is not under a "
                    "Claude Code projects directory; it is not read"
                )
        else:
            entry.exists = _exists(reader, loc, (), loc.name)
            entry.subagents = _subagents(reader, loc)
        files.append(entry)
    return Listing(csession_id, files, warnings)


def _exists(reader: Reader, loc: Location, segments: Sequence[str], name: str) -> bool:
    try:
        os.close(reader.open_file(loc, segments, name))
    except _Missing:
        return False
    return True


def _subagents(reader: Reader, loc: Location) -> list[Subagent]:
    segments = (loc.claude_session_id, "subagents")
    names = reader.list_dir(loc, segments)
    agent_ids: set[str] = set()
    for name in names:
        match = AGENT_FILE.fullmatch(name) or AGENT_META.fullmatch(name)
        if match is not None:
            agent_ids.add(match.group(1))
    found: list[Subagent] = []
    for agent_id in sorted(agent_ids):
        meta = _meta(reader, loc, segments, f"agent-{agent_id}.meta.json")
        found.append(
            Subagent(
                agent_id=agent_id,
                agent_type=_meta_text(meta, "agentType"),
                description=_meta_text(meta, "description"),
                tool_use_id=_meta_text(meta, "toolUseId"),
                path=loc.subagent_path(agent_id),
                exists=_exists(reader, loc, segments, f"agent-{agent_id}.jsonl"),
            )
        )
    return found


def _meta(reader: Reader, loc: Location, segments: Sequence[str], name: str) -> dict[str, Any]:
    """A subagent's meta file as a JSON object; ``{}`` when it is missing, larger than
    ``META_MAX_BYTES`` or not a JSON object."""
    try:
        fd = reader.open_file(loc, segments, name)
    except _Missing:
        return {}
    shown = os.path.join(loc.directory, *segments, name)
    try:
        chunks = reader.chunks(fd, shown)
        data = b"".join(itertools.islice(chunks, META_MAX_BYTES // CHUNK_BYTES + 1))
    finally:
        os.close(fd)
    if len(data) > META_MAX_BYTES:
        return {}
    parsed = _parse(data)
    return parsed if parsed is not None else {}


def _meta_text(meta: Mapping[str, Any], key: str) -> str | None:
    value = meta.get(key)
    return no_surrogates(value) if isinstance(value, str) else None


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return no_surrogates(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return None


# Records


def _parse(line: bytes) -> dict[str, Any] | None:
    """One JSONL record as a dict, or None (malformed, too deeply nested, not an object).
    Invalid UTF-8 is replaced; lone surrogates from escapes are left to ``display``."""
    try:
        value = json.loads(line.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


OVERSIZED: dict[str, Any] = {"type": "__oversized__"}


def records(reader: Reader, fd: int, shown: str = "") -> Iterator[dict[str, Any] | None]:
    """The records of an open JSONL file: a dict per JSON-object line, None for any other
    line, ``OVERSIZED`` for a line longer than ``RECORD_MAX_BYTES`` (never held in memory
    whole). A last line without a newline is parsed too."""
    pending: list[bytes] = []
    pending_size = 0
    oversized = False
    for chunk in reader.chunks(fd, shown):
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            piece = chunk[start:] if end < 0 else chunk[start:end]
            if not oversized:
                pending_size += len(piece)
                if pending_size > RECORD_MAX_BYTES:
                    oversized = True
                    pending = []
                else:
                    pending.append(piece)
            if end < 0:
                break
            yield OVERSIZED if oversized else _line_record(b"".join(pending))
            pending, pending_size, oversized = [], 0, False
            start = end + 1
    if oversized:
        yield OVERSIZED
    elif pending_size:
        yield _line_record(b"".join(pending))


def _line_record(line: bytes) -> dict[str, Any] | None:
    if not line.strip():
        return None
    return _parse(line)


# Rendering


@dataclass
class Renderer:
    """Renders a listing as lines of text, every value through ``display``."""

    reader: Reader
    subagents: bool = False
    # (claude session id, agent id) of the subagents already shown inline.
    _shown: set[tuple[str, str]] = field(default_factory=set)

    def lines(self, listing: Listing) -> Iterator[str]:
        if not listing.files:
            yield "No transcripts."
            return
        for index, entry in enumerate(listing.files):
            if index:
                yield ""
            yield (
                f"=== Claude session {display(entry.claude_session_id)}"
                f" (run {entry.run}, {display(entry.source)}) ==="
            )
            loc = entry.location
            if loc is None:
                yield "file: (not available)"
                continue
            yield f"file: {display(loc.path)}"
            if not entry.exists:
                yield "(missing: Claude Code deleted it or never wrote it)"
            else:
                yield from self._file(loc, (), loc.name, entry.subagents, "")
            if self.subagents:
                yield from self._remaining(loc, entry.subagents)

    def _remaining(self, loc: Location, subs: Sequence[Subagent]) -> Iterator[str]:
        """Subagents not shown inline (no meta, or no matching tool call)."""
        for sub in subs:
            if (loc.claude_session_id, sub.agent_id) not in self._shown:
                yield from self._subagent(loc, sub, "")

    def _subagent(self, loc: Location, sub: Subagent, indent: str) -> Iterator[str]:
        self._shown.add((loc.claude_session_id, sub.agent_id))
        kind = display(sub.agent_type) if sub.agent_type is not None else "subagent"
        line = f"{indent}--- subagent {display(sub.agent_id)} ({kind})"
        if sub.description is not None:
            line += f": {_clip(display(sub.description))}"
        yield line
        inner = indent + "  | "
        if not sub.exists:
            yield f"{inner}(missing: {display(sub.path)})"
            return
        segments = (loc.claude_session_id, "subagents")
        yield from self._file(loc, segments, f"agent-{sub.agent_id}.jsonl", (), inner)

    def _file(
        self,
        loc: Location,
        segments: Sequence[str],
        name: str,
        subs: Sequence[Subagent],
        indent: str,
    ) -> Iterator[str]:
        try:
            fd = self.reader.open_file(loc, segments, name)
        except _Missing:
            yield f"{indent}(missing: deleted while reading)"
            return
        offloaded = set(self.reader.list_dir(loc, (loc.claude_session_id, "tool-results")))
        by_tool = {sub.tool_use_id: sub for sub in subs if sub.tool_use_id is not None}
        shown = os.path.join(loc.directory, *segments, name)
        try:
            for record in records(self.reader, fd, shown):
                if record is OVERSIZED:
                    yield f"{indent}[record over {RECORD_MAX_BYTES} bytes skipped]"
                    continue
                if record is None:
                    continue
                for line, tool_id in _record_lines(record):
                    yield indent + line
                    if self.subagents and tool_id is not None and tool_id in by_tool:
                        yield from self._subagent(loc, by_tool[tool_id], indent + "  ")
                for ref in _offloaded_refs(record, offloaded):
                    yield f"{indent}[offloaded] {display(loc.tool_result_path(ref))}"
        finally:
            os.close(fd)


def _record_lines(record: Mapping[str, Any]) -> Iterator[tuple[str, str | None]]:
    """The lines of one record and, on a tool call's line, its tool-use id."""
    kind = record.get("type")
    if kind not in ("user", "assistant"):
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        yield from _labeled(f"[{kind}]", content)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            yield from _labeled(f"[{kind}]", block["text"])
        elif block_type == "tool_use":
            tool_id = block.get("id") if isinstance(block.get("id"), str) else None
            yield _tool_call(block), tool_id
        elif block_type == "tool_result":
            label = "[error]" if block.get("is_error") is True else "[result]"
            text = _result_text(block.get("content"))
            yield from _labeled(label, text, max_lines=RESULT_LINES)


def _labeled(
    label: str, text: str, *, max_lines: int | None = None
) -> Iterator[tuple[str, str | None]]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        lines = [""]
    extra = 0
    if max_lines is not None and len(lines) > max_lines:
        extra = len(lines) - max_lines
        lines = lines[:max_lines]
    pad = " " * len(label)
    for index, line in enumerate(lines):
        yield f"{label if index == 0 else pad} {_clip(display(line))}".rstrip(), None
    if extra:
        yield f"{pad} ... ({extra} more lines)", None


def _tool_call(block: Mapping[str, Any]) -> str:
    name = block.get("name")
    summary = ""
    data = block.get("input")
    if isinstance(data, dict):
        for key in SUMMARY_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value:
                summary = value
                break
        else:
            try:
                summary = json.dumps(data, ensure_ascii=False, sort_keys=True)[
                    : SUMMARY_MAX_CHARS + 1
                ]
            except (ValueError, TypeError, RecursionError):
                summary = ""
    first, newline, _rest = summary.partition("\n")
    if len(first) > SUMMARY_MAX_CHARS or newline:
        first = first[:SUMMARY_MAX_CHARS] + " ..."
    tool = display(name if isinstance(name, str) else "?")
    return f"[tool {tool}] {display(first)}".rstrip()


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item.get("type"), str):
                parts.append(f"[{item['type']}]")
    return "\n".join(parts)


def _offloaded_refs(record: Mapping[str, Any], offloaded: set[str]) -> list[str]:
    """Offloaded tool-result files a record's tool results name (those that exist)."""
    if not offloaded or record.get("type") != "user":
        return []
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    found: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            for name in TOOL_RESULT_REF.findall(_result_text(block.get("content"))):
                if name in offloaded and name not in found:
                    found.append(name)
    return found


def _clip(text: str) -> str:
    if len(text) <= LINE_MAX_CHARS:
        return text
    return f"{text[:LINE_MAX_CHARS]} ... ({len(text) - LINE_MAX_CHARS} more characters)"


# Paths and raw output


def path_lines(listing: Listing) -> list[str]:
    """``--paths``: the existing files, each main file followed by its subagent files."""
    lines: list[str] = []
    for entry in listing.files:
        if entry.path is not None and entry.exists:
            lines.append(entry.path)
        lines += [sub.path for sub in entry.subagents if sub.exists]
    return lines


def missing_warnings(listing: Listing) -> list[str]:
    warnings: list[str] = []
    for entry in listing.files:
        if entry.path is not None and not entry.exists:
            warnings.append(f"transcript file missing: {entry.path}")
        warnings += [
            f"subagent transcript file missing: {sub.path}"
            for sub in entry.subagents
            if not sub.exists
        ]
    return warnings


def write_raw(listing: Listing, reader: Reader, out: BinaryIO, *, subagents: bool) -> None:
    """``--raw``: the existing files' bytes, unchanged, each main file followed by its
    subagent files when ``subagents``."""
    for entry in listing.files:
        loc = entry.location
        if loc is None:
            continue
        if entry.exists:
            _copy(reader, loc, (), loc.name, out)
        if subagents:
            for sub in entry.subagents:
                if sub.exists:
                    segments = (loc.claude_session_id, "subagents")
                    _copy(reader, loc, segments, f"agent-{sub.agent_id}.jsonl", out)


def _copy(reader: Reader, loc: Location, segments: Sequence[str], name: str, out: BinaryIO) -> None:
    try:
        fd = reader.open_file(loc, segments, name)
    except _Missing:
        return
    try:
        for chunk in reader.chunks(fd, os.path.join(loc.directory, *segments, name)):
            out.write(chunk)
    finally:
        os.close(fd)
