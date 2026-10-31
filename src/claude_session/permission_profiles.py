"""Permission profiles: selection, profile files, validation, variable expansion and snapshots
(interface.md, Permission profiles; design.md, Generated settings).

A profile is a Claude Code settings fragment ``{permissions: {...}, sandbox: {...}}``, named in
config (``permission_profiles``) or read from a file given with ``--permission-profile``. The
launch path uses this module in this order:

1. ``select``: the run's profile, unexpanded and validated for the run's isolation (shape and
   refused keys). A file is read once, here, at the launch that names it.
2. ``expand``: ``${CSESSION_DIR}``, ``${CWD}``, ``${HOME}`` and ``${GIT_DIR}`` replaced; a
   variable that expands empty fails. The result goes to the settings builder.
3. ``write_snapshot`` (file profiles only): the canonical JSON of the unexpanded profile to
   ``private/<id>/runs/<n>/permission-profile.json``. The run stores ``Profile.label``
   (``file:<sha256 prefix>``) and the snapshot path; a later run carries the snapshot path as
   its ``--permission-profile`` value, never the original path, so the file is never re-read.
   A carried snapshot is read with ``expected_label`` (the stored label): an edited snapshot
   fails.

Every failure raises ``InvalidPermissionProfile`` (end reason ``invalid_permission_profile``).
Messages name key paths and file paths, never values from the profile.

Permission rules are evadable; the sandbox is the boundary. This module only keeps a profile
from undoing the generated isolation keys, which the settings builder applies on top anyway.

Never imported by the hook path: it needs ``config``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, fsutil, kinds
from .errors import InvalidPermissionProfile, UsageError

VARIABLES = ("CSESSION_DIR", "CWD", "HOME", "GIT_DIR")
SNAPSHOT_NAME = "permission-profile.json"
HASH_PREFIX = 16  # hex characters of the sha256 in ``file:<prefix>`` (the schemas allow 12-64)
MAX_FILE_BYTES = config.MAX_FILE_BYTES
SECTIONS = ("permissions", "sandbox")
# The only sandbox keys an isolated run's profile may set (interface.md, Permission profiles):
# an allowlist, since any other key may be a sandbox escape (host sockets, local listeners,
# proxies, a weaker or disabled sandbox, ignored violations) or a generated isolation key.
# A mapping value lists the allowed keys below it; None: a leaf.
ISOLATED_SANDBOX_KEYS: dict[str, frozenset[str] | None] = {
    "filesystem": frozenset({"allowRead", "denyRead", "allowWrite", "denyWrite"}),
    "network": frozenset({"allowedDomains", "deniedDomains"}),
    "autoAllowBashIfSandboxed": None,
}
# Permission keys an isolated run's profile may not set at all.
REFUSED_ISOLATED = (("permissions", "additionalDirectories"),)
# ... and this one only as false (the generated value is false).
AUTO_ALLOW = ("sandbox", "autoAllowBashIfSandboxed")
# Values the settings builder merges with generated rules: lists of strings when present.
STRING_LISTS = (
    ("permissions", "allow"),
    ("permissions", "deny"),
    ("permissions", "ask"),
    ("permissions", "additionalDirectories"),
    ("sandbox", "excludedCommands"),
    ("sandbox", "filesystem", "allowRead"),
    ("sandbox", "filesystem", "denyRead"),
    ("sandbox", "filesystem", "allowWrite"),
    ("sandbox", "filesystem", "denyWrite"),
    ("sandbox", "network", "allowedDomains"),
    ("sandbox", "network", "deniedDomains"),
)
MAPPINGS = (("sandbox", "filesystem"), ("sandbox", "network"))
STRINGS = (("permissions", "defaultMode"),)

# Every `${`: a well-formed reference is `${NAME}`; anything else fails.
_REFERENCE = re.compile(r"\$\{([^}]*)\}")
# Variable values are normalized absolute paths; glob and escape characters would change the
# meaning of the rules they are put into, so they fail (as do control characters).
_UNSAFE_VALUE = re.compile(r"[*?\[\]{}\\\x00-\x1f\x7f-\x9f]")
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


@dataclass(frozen=True)
class Profile:
    """A selected permission profile. ``content`` is unexpanded; ``name`` is the config name
    (None for a file); ``snapshot`` is the canonical JSON of a file profile, fixed when the
    file is read, so the snapshot, ``sha256`` and ``label`` agree even if ``content`` is
    changed later."""

    name: str | None
    content: dict[str, Any]
    snapshot: bytes | None = None

    @property
    def is_file(self) -> bool:
        return self.snapshot is not None

    @property
    def sha256(self) -> str | None:
        """The hash of the snapshot (file profiles only)."""
        return None if self.snapshot is None else hashlib.sha256(self.snapshot).hexdigest()

    @property
    def label(self) -> str:
        """The name, or ``file:<sha256 prefix>`` (status, dry run, pre-launch payloads)."""
        digest = self.sha256
        if digest is None:
            assert self.name is not None
            return self.name
        return "file:" + digest[:HASH_PREFIX]

    def snapshot_bytes(self) -> bytes:
        """The snapshot of a file profile: canonical JSON of the unexpanded content, as read."""
        if self.snapshot is None:
            raise ValueError("only file profiles are snapshotted")
        return self.snapshot


def select(
    cfg: config.Config,
    resolution: kinds.Resolution,
    *,
    session_writable: Callable[[str], bool] | None,
    expected_label: str | None = None,
) -> Profile:
    """The run's profile for ``resolution``: the file in
    ``permission_profile_file`` when set (a flag given now, or the previous run's snapshot),
    else the config profile ``profile.permission_profile``. Validated for the run's isolation.
    ``session_writable`` and ``expected_label`` are passed to ``read_file`` (required for
    isolated runs; None only for trusted ones)."""
    isolated = resolution.isolated()
    _require_check(isolated, session_writable)
    if resolution.permission_profile_file is not None:
        return read_file(
            resolution.permission_profile_file,
            isolated=isolated,
            session_writable=session_writable,
            expected_label=expected_label,
        )
    name = resolution.profile.permission_profile
    configured = cfg.permission_profiles.get(name)
    if configured is None:  # kinds.resolve and config.load refuse this first
        raise InvalidPermissionProfile(
            "unknown permission profile",
            detail=f"{name}: names no built-in or configured permission profile",
        )
    # Only the unset sections are left out; values inside them (including nulls) are kept.
    content = {
        key: copy.deepcopy(value)
        for key in SECTIONS
        if (value := getattr(configured, key)) is not None
    }
    validate(content, isolated=isolated, source=f"permission profile {name}")
    return Profile(name=name, content=content)


def read_file(
    value: str,
    *,
    isolated: bool,
    session_writable: Callable[[str], bool] | None,
    expected_label: str | None = None,
) -> Profile:
    """Read, parse and validate the profile file ``value`` (relative to the current directory).

    ``.json`` files are strict JSON; anything else is YAML with the config-file rules. For
    isolated runs, a file whose real path ``session_writable`` accepts is refused before it is
    read (the check is required for them: None raises ``ValueError``); the file is then
    opened at that real path without following symlinks, so a swap after the check fails. At
    most ``MAX_FILE_BYTES``; regular files only. With ``expected_label`` (a carried snapshot's
    recorded ``file:<prefix>``), a file whose label differs fails."""
    # The check is pathrules.session_writable(...) (launch.select_profile passes it).
    _require_check(isolated, session_writable)
    problem: str | None = None
    try:
        path = os.path.realpath(value)
    except (ValueError, OSError):  # NUL in the path, or an unresolvable cwd
        path = ""
        problem = "invalid path"
    if problem is not None:
        raise InvalidPermissionProfile("cannot read permission profile file", detail=problem)
    if isolated and session_writable is not None and session_writable(path):
        raise InvalidPermissionProfile(
            "permission profile file is under a session-writable path", detail=path
        )
    raw = _read_bytes(path)
    data = _parse(raw, path, json_file=value.endswith(".json"))
    validate(data, isolated=isolated, source=path)
    try:
        snapshot = _canonical(data)
    except ValueError:  # e.g. a YAML integer over the int-to-str digit limit; not quoted
        snapshot = None
    if snapshot is None:
        raise InvalidPermissionProfile(
            "invalid permission profile", detail=f"{path}: value cannot be snapshotted"
        )
    profile = Profile(name=None, content=data, snapshot=snapshot)
    if expected_label is not None and profile.label != expected_label:
        raise InvalidPermissionProfile(
            "invalid permission profile",
            detail=f"{path}: snapshot does not match its recorded hash",
        )
    return profile


def _require_check(isolated: bool, session_writable: Callable[[str], bool] | None) -> None:
    if isolated and session_writable is None:
        raise ValueError("isolated runs need the session_writable path check")


def validate(content: Any, *, isolated: bool, source: str = "permission profile") -> None:
    """Check ``content``: a mapping of ``permissions`` and ``sandbox`` mappings whose known
    rule lists are lists of strings and whose numbers are finite. For isolated runs, also the
    sandbox key allowlist and the refused keys. Raises ``InvalidPermissionProfile`` listing
    every problem (key paths only)."""
    # The paths in the profile are checked after expansion by pathrules.PathRules
    # .check_profile: refused paths (state root, /tmp/csession-<uid>) for every run; for
    # isolated runs also unsafe write targets and the credential paths for sandbox allowRead.
    problems = _shape(content)
    if not problems and isolated:
        problems = _refused(content)
    if problems:
        raise InvalidPermissionProfile(
            "invalid permission profile", detail=f"{'; '.join(problems)} (in {source})"
        )


def expand(content: Mapping[str, Any], variables: Mapping[str, str | None]) -> dict[str, Any]:
    """A copy of ``content`` with ``${NAME}`` replaced in every string value (not in keys).
    ``NAME`` is one of ``VARIABLES``; a missing, empty or ``None`` value fails, as do unknown
    names, malformed references and unsafe values (relative, or with glob, escape or control
    characters). Unused variables are not checked."""

    def walk(node: Any, where: str) -> Any:
        if isinstance(node, dict):
            return {key: walk(value, _join(where, key)) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value, f"{where}.{index}") for index, value in enumerate(node)]
        if isinstance(node, str) and "${" in node:
            return _expand_string(node, where, variables)
        return copy.deepcopy(node)

    return walk(content, "")


def write_snapshot(
    profile: Profile,
    path: str,
    *,
    write: Callable[[str, bytes], None] = fsutil.write_private_file,
) -> None:
    """Write the snapshot of a file profile to ``path`` (absolute; normally
    ``private/<id>/runs/<n>/permission-profile.json``) with ``write`` (default: atomic, mode
    0600, no-follow)."""
    # exec writes the snapshot with SessionDirs.write_private_run_file (launch.prepare) and
    # records the label and snapshot path in the run row; this helper writes to a path.
    # start reads the file (a relative path resolves against start's cwd, never at the
    # adopting exec), snapshots it and freezes the profile in its launch spec (lifecycle).
    # TODO(#29): a resume stores the snapshot path as the --permission-profile flag value.
    write(path, profile.snapshot_bytes())


# Reading and parsing


def _read_bytes(path: str) -> bytes:
    def fail(problem: str) -> InvalidPermissionProfile:
        return InvalidPermissionProfile(
            "cannot read permission profile file", detail=f"{path}: {problem}"
        )

    parent, name = os.path.split(path)
    error: str | None = None
    fd = -1
    try:
        dir_fd = fsutil.open_dir_nofollow(parent)
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        error = exc.strerror or "cannot open"
    if error is not None:
        raise fail(error)
    chunks: list[bytes] = []
    size = 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise fail("not a regular file")
        while size <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        error = exc.strerror or "cannot read"
    finally:
        os.close(fd)
    if error is not None:
        raise fail(error)
    if size > MAX_FILE_BYTES:
        raise fail("larger than 1 MiB")
    return b"".join(chunks)


def _parse(raw: bytes, path: str, *, json_file: bool) -> Any:
    def invalid(problem: str) -> InvalidPermissionProfile:
        return InvalidPermissionProfile("invalid permission profile file", detail=problem)

    if not json_file:
        detail: str | None = None
        try:
            return config.parse_yaml(raw, Path(path), empty=False)
        except UsageError as exc:  # its detail names the file and problem, never the content
            detail = exc.detail
        raise invalid(detail or path)
    problem: str | None = None
    data: Any = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        problem = f"{path}: not UTF-8 ({exc.reason})"
    else:
        try:
            data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        except _Duplicate:
            problem = f"{path}: duplicate key"
        except (ValueError, RecursionError):  # JSONDecodeError quotes nothing we show
            problem = f"{path}: invalid JSON"
    # Raised outside the handlers: the original exceptions may quote the content.
    if problem is not None:
        raise invalid(problem)
    if not isinstance(data, dict):
        raise invalid(f"top level: expected a mapping (in {path})")
    if not _encodable(data):  # a lone surrogate (\ud800) is valid JSON but not UTF-8
        raise invalid(f"{path}: invalid string")
    try:
        config.check_tree(data, Path(path))  # the config node and depth limits
    except UsageError as exc:
        problem = exc.detail
    if problem is not None:
        raise invalid(problem)
    return data


class _Duplicate(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Duplicate
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise ValueError("NaN and Infinity are not JSON")


def _encodable(node: Any) -> bool:
    """Whether every string (key or value) in ``node`` encodes as UTF-8. Iterative: it runs
    before the depth limit (``config.check_tree`` details quote keys), so it must not recurse."""
    pending = [node]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                return False
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return True


def _canonical(content: Mapping[str, Any]) -> bytes:
    text = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return text.encode("utf-8")


# Validation


def _shape(content: Any) -> list[str]:
    if not isinstance(content, dict):
        return ["top level: expected a mapping"]
    problems: list[str] = []
    for key, value in content.items():
        if key not in SECTIONS:
            problems.append(f"{_safe(key)}: not allowed (only permissions and sandbox)")
        elif not isinstance(value, dict):
            problems.append(f"{key}: expected a mapping")
    if problems:
        return problems
    problems.extend(_non_finite(content, ""))
    for loc in MAPPINGS:
        found, value = _get(content, loc)
        if found and not isinstance(value, dict):
            problems.append(f"{'.'.join(loc)}: expected a mapping")
    for loc in STRINGS:
        found, value = _get(content, loc)
        if found and not isinstance(value, str):
            problems.append(f"{'.'.join(loc)}: expected a string")
    for loc in STRING_LISTS:
        found, value = _get(content, loc)
        if not found:
            continue
        where = ".".join(loc)
        if not isinstance(value, list):
            problems.append(f"{where}: expected a list of strings")
            continue
        problems.extend(
            f"{where}.{index}: expected a string"
            for index, item in enumerate(value)
            if not isinstance(item, str)
        )
    return problems


def _non_finite(node: Any, where: str) -> list[str]:
    if isinstance(node, float) and not math.isfinite(node):
        return [f"{where}: not a finite number"]
    if isinstance(node, dict):
        return [p for key, value in node.items() for p in _non_finite(value, _join(where, key))]
    if isinstance(node, list):
        return [
            p for index, value in enumerate(node) for p in _non_finite(value, f"{where}.{index}")
        ]
    return []


def _refused(content: dict[str, Any]) -> list[str]:
    problems = [
        f"{'.'.join(loc)}: not allowed for isolated runs"
        for loc in REFUSED_ISOLATED
        if _get(content, loc)[0]
    ]
    sandbox = content.get("sandbox") or {}
    for key, value in sandbox.items():
        if key not in ISOLATED_SANDBOX_KEYS:
            problems.append(f"sandbox.{_safe(key)}: not allowed for isolated runs")
            continue
        allowed = ISOLATED_SANDBOX_KEYS[key]
        if allowed is None or not isinstance(value, dict):
            continue
        problems.extend(
            f"sandbox.{key}.{_safe(sub)}: not allowed for isolated runs"
            for sub in value
            if sub not in allowed
        )
    found, value = _get(content, AUTO_ALLOW)
    if found and value is not False:
        problems.append(f"{'.'.join(AUTO_ALLOW)}: only false is allowed for isolated runs")
    return problems


def _get(content: Mapping[str, Any], loc: tuple[str, ...]) -> tuple[bool, Any]:
    node: Any = content
    for part in loc:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _safe(key: Any) -> str:
    """A key from a profile, shown printable and short."""
    text = str(key)
    shown = "".join(char if char.isprintable() else "?" for char in text[:64])
    return shown + ("..." if len(text) > 64 else "")


def _join(where: str, key: Any) -> str:
    return f"{where}.{_safe(key)}" if where else _safe(key)


# Expansion


def _expand_string(text: str, where: str, variables: Mapping[str, str | None]) -> str:
    def fail(problem: str) -> InvalidPermissionProfile:
        return InvalidPermissionProfile("invalid permission profile", detail=f"{where}: {problem}")

    if "${" in _REFERENCE.sub("", text):
        raise fail("malformed ${...} reference")
    for match in _REFERENCE.finditer(text):
        name = match.group(1)
        if name not in VARIABLES:
            raise fail(f"unknown variable (expected one of {', '.join(VARIABLES)})")
        value = variables.get(name)
        if not value:
            raise fail(f"${{{name}}} expands empty")
        if not _normalized_absolute(value) or _UNSAFE_VALUE.search(value):
            raise fail(
                f"${{{name}}} is not a normalized absolute path free of glob, escape and"
                " control characters"
            )
    return _REFERENCE.sub(lambda m: str(variables[m.group(1)]), text)


def _normalized_absolute(value: str) -> bool:
    """An absolute path with no ``.`` or ``..`` segment, no ``//`` and no trailing ``/``
    (``/`` itself is allowed here; whether it is a safe rule target is a path rule)."""
    return os.path.isabs(value) and os.path.normpath(value) == value and not value.startswith("//")
