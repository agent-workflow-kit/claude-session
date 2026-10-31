"""``claude --version`` and the ``min_claude_version`` comparison (interface.md, kinds and
Launch order step 5; design.md, Platform notes).

``kinds`` reports the version and ``claude_version_ok``; the launch (``launch.checks``, right
after the tools are resolved, so a dry run makes the same check) calls ``require``:
``claude_too_old`` below ``min_claude_version`` or when the output holds no version, and
``claude_not_found`` when ``claude --version`` cannot run. One probe per launch, bounded in
time (``PROBE_TIMEOUT`` plus ``KILL_GRACE``) and in memory (``MAX_OUTPUT``); nothing is
cached: the version is read again at every launch, so an upgraded or downgraded ``claude``
is seen at once. The version is read from the first line of the output exactly as printed
(``Version.line``); messages show it with control characters replaced (``Version.text``).
``check_interpreter`` keeps a script ``claude``'s ``#!`` interpreter out of the
session-writable paths (isolated launches, dry runs and ``kinds``).

Versions are compared part by part as numbers of any length (leading zeros ignored, missing
trailing parts count as 0) without converting them to ``int``, so no output can make the
comparison fail. Only ASCII digits form a version.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from . import pathrules
from .errors import LaunchValidationError
from .gateway import Gateway, GatewayTimeout
from .pathrules import ResolvedTool

PROBE_TIMEOUT = 10.0
# SIGTERM-to-SIGKILL delay for the probe's process group on a timeout.
KILL_GRACE = 2.0
# The only variables the probe gets: nothing secret (auth, secret_env, env.set) reaches it.
PROBE_NAMES = ("PATH", "HOME", "LANG", "LANGUAGE")
# The most of claude's first line read for its #! interpreter (the kernel reads as much).
SHEBANG_MAX = 256
# The longest version text kept (the first line of the output).
MAX_TEXT = 200
# The most output read into memory, per stream (the rest is read and dropped).
MAX_OUTPUT = 64 * 1024
TOO_OLD_HINT = "upgrade Claude Code (claude update), or set min_claude_version in the config"
NO_VERSION_HINT = (
    "check that claude_bin is Claude Code; its `--version` output must start with a version"
)
NOT_RUN_HINT = (
    "claude_bin must be a working Claude Code; `claude --version` runs with PATH entries "
    "outside the session-writable paths, so its interpreter must be found there"
)
# A version at the start of the output: ``2.1.312 (Claude Code)``.
# The version is followed by a space, a tab or the end of the line; only ASCII digits count.
_LEADING = re.compile(r"([0-9]+(?:\.[0-9]+)*)(?=[ \t\n]|\Z)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def probe_path(environ: Mapping[str, str], writable: Callable[[str], bool]) -> list[str]:
    """The probe's ``PATH`` entries: the caller's, relative and empty entries dropped, and
    every entry that is session-writable at any step of resolving it
    (``pathrules.writable_hop`` against ``writable``) removed. ``claude`` may be a script
    (``#!/usr/bin/env node``) whose interpreter is looked up on this ``PATH``."""
    return [
        entry
        for entry in pathrules.sanitize_path(environ.get("PATH", ""))
        if pathrules.writable_hop(entry, writable) is None
    ]


def _unknown(claude: ResolvedTool, why: str) -> LaunchValidationError:
    return LaunchValidationError(
        f"claude's #! line cannot be checked ({why}): {claude.path!r}", end_reason="unsafe_path"
    )


def check_interpreter(
    claude: ResolvedTool, path_entries: Sequence[str], writable: Callable[[str], bool]
) -> None:
    """A script ``claude``'s interpreter is not session-writable: the first line of the
    resolved realpath (at most ``SHEBANG_MAX`` bytes, as the kernel reads it) is read; when
    it starts with ``#!``, an absolute interpreter is checked like a tool, every step of
    resolving it (``pathrules.writable_hop``), and ``/usr/bin/env NAME`` (one argument, no
    option or assignment) is checked as ``NAME`` resolved on ``path_entries``
    (``pathrules.resolve_tool``: the file and each step of resolving it). One level only: the
    interpreter's own ``#!`` line is not followed. Anything else (``env -S``, an option or
    more than one argument to ``env``, a relative interpreter, a line longer than
    ``SHEBANG_MAX`` or not UTF-8, a ``claude`` that cannot be read) cannot be checked and is
    refused (fail closed). An interpreter that is not found passes: running fails then.
    Raises ``LaunchValidationError`` (``unsafe_path``)."""
    try:
        with open(claude.path, "rb") as file:
            head = file.read(SHEBANG_MAX)
    except OSError as error:
        raise _unknown(claude, f"cannot read it: {error.strerror or error}") from None
    if not head.startswith(b"#!"):
        return
    line, newline, _ = head[2:].partition(b"\n")
    if not newline:
        raise _unknown(claude, f"no end of line within {SHEBANG_MAX} bytes")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise _unknown(claude, "not UTF-8") from None
    # As the kernel splits it: the interpreter, then at most one argument (the rest).
    interpreter, _, rest = text.strip(" \t").replace("\t", " ").partition(" ")
    argument = rest.strip(" ")
    if not os.path.isabs(interpreter):
        raise _unknown(claude, "not an absolute interpreter")
    hop = pathrules.writable_hop(interpreter, writable)
    if hop is not None:
        raise LaunchValidationError(
            f"claude's #! interpreter is inside a session-writable path: {shown(interpreter)!r}"
            + ("" if hop == interpreter else f" (through {shown(hop)!r})"),
            end_reason="unsafe_path",
        )
    if "env" not in (
        os.path.basename(interpreter),
        os.path.basename(os.path.realpath(interpreter)),
    ):
        return
    if not argument or argument.startswith("-") or "=" in argument or " " in argument:
        raise _unknown(claude, "env with options, assignments, or not exactly one name")
    try:
        pathrules.resolve_tool(argument, path_entries, writable)
    except LaunchValidationError as error:
        raise LaunchValidationError(
            f"claude's #! interpreter {shown(argument)!r}: {error.message}",
            end_reason="unsafe_path",
        ) from None


def probe_env(environ: Mapping[str, str], writable: Callable[[str], bool]) -> dict[str, str]:
    """The probe's minimal environment: ``PATH`` (``probe_path``), ``HOME`` and the locale
    variables (``LANG``, ``LANGUAGE``, ``LC_*``) of ``environ``."""
    env = {
        name: value
        for name, value in environ.items()
        if (name in PROBE_NAMES or name.startswith("LC_")) and name != "PATH"
    }
    env["PATH"] = ":".join(probe_path(environ, writable))
    return env


class Version(NamedTuple):
    """A ``claude --version`` answer: ``line`` is the first line of its output as printed
    (compared), ``text`` the same line for display (``shown``)."""

    line: str
    text: str


class NotRun(Exception):
    """``claude --version`` did not answer; ``reason`` says why (``run``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def shown(text: str) -> str:
    """``text`` for a message: each control character replaced with a space (so a tab never
    joins two words into another version), stripped, at most ``MAX_TEXT`` characters."""
    return _CONTROL.sub(" ", text).strip()[:MAX_TEXT]


def _first_line(text: str) -> str:
    """The first line of ``text`` as printed (a trailing ``\\r`` dropped)."""
    return text.split("\n", 1)[0].removesuffix("\r")


def _with_stderr(reason: str, stderr: str) -> str:
    line = shown(_first_line(stderr.lstrip()))
    return f"{reason}; stderr: {line!r}" if line else reason


def run(
    claude: ResolvedTool,
    environ: Mapping[str, str],
    gw: Gateway,
    *,
    writable: Callable[[str], bool],
    timeout: float = PROBE_TIMEOUT,
) -> Version:
    """``claude --version``, run from the resolved realpath with ``argv[0]`` the tool's
    name, in ``/`` with the minimal environment (``probe_env``: no ``PATH`` entry under
    ``writable``) and in its own process group, which a timeout ends as a whole and which
    is SIGKILLed when ``claude`` exits, so no descendant outlives the probe. Raises
    ``NotRun`` naming why when it cannot start, does not finish within ``timeout``, exits
    non-zero (with the first line of its stderr), prints nothing but whitespace, or when no
    ``PATH`` entry is left (then nothing runs). The caller never passes a binary a launch
    would refuse."""
    env = probe_env(environ, writable)
    if not env["PATH"]:
        raise NotRun("no PATH entry outside the session-writable paths")
    try:
        result = gw.run(
            [claude.name, "--version"],
            env=env,
            cwd="/",
            timeout=timeout,
            new_process_group=True,
            kill_grace=KILL_GRACE,
            kill_group_on_exit=True,
            executable=claude.path,
            output_limit=MAX_OUTPUT,
        )
    except GatewayTimeout:
        raise NotRun(f"it did not finish within {timeout:g} s") from None
    except OSError as error:
        raise NotRun(f"it could not start: {shown(error.strerror or str(error))}") from None
    except ValueError as error:
        raise NotRun(f"it could not start: {shown(str(error))}") from None
    if result.returncode != 0:
        raise NotRun(_with_stderr(f"it exited with status {result.returncode}", result.stderr))
    if not result.stdout.strip():
        raise NotRun(_with_stderr("it printed nothing", result.stderr))
    line = _first_line(result.stdout)
    return Version(line=line, text=shown(line))


def _parts(text: str) -> tuple[str, ...] | None:
    """The digit strings of the version at the very start of ``text`` (nothing stripped),
    or None."""
    match = _LEADING.match(text)
    if match is None:
        return None
    return tuple(match.group(1).split("."))


def parse(text: str) -> tuple[int, ...] | None:
    """The version at the start of ``text`` (``2.1.312 (Claude Code)`` -> ``(2, 1, 312)``),
    or None when it does not start with one (or a part is too long for ``int``)."""
    parts = _parts(text)
    if parts is None:
        return None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:  # more digits than int() converts
        return None


def _key(parts: tuple[str, ...], width: int) -> tuple[tuple[int, str], ...]:
    """A sort key comparing each part as a number of any length: (digit count without
    leading zeros, those digits); padded with zero parts to ``width``."""
    keys = []
    for part in parts + ("0",) * (width - len(parts)):
        digits = part.lstrip("0")
        keys.append((len(digits), digits))
    return tuple(keys)


def at_least(text: str | None, minimum: str) -> bool | None:
    """Whether the version in ``text`` is ``minimum`` or newer (missing trailing parts count
    as 0); None when ``text`` is None or either holds no version."""
    if text is None:
        return None
    version = _parts(text)
    required = _parts(minimum)
    if version is None or required is None:
        return None
    width = max(len(version), len(required))
    return _key(version, width) >= _key(required, width)


def require(
    claude: ResolvedTool,
    environ: Mapping[str, str],
    gw: Gateway,
    minimum: str,
    *,
    writable: Callable[[str], bool],
) -> str:
    """The launch's version check: ``run`` (``writable``: the run's session-writable
    paths, which no ``PATH`` entry of the probe may be under), then ``at_least`` on the raw
    first line. Returns the version text (for display). Raises ``LaunchValidationError``:
    ``claude_not_found`` when ``claude --version`` cannot run (``NotRun``: it fails to
    start, exits non-zero, prints nothing, times out, or no ``PATH`` entry is left to run it
    with; the message says which, ``NOT_RUN_HINT``), ``claude_too_old`` when the version is
    older than ``minimum`` (``TOO_OLD_HINT``) or the first line does not start with a version
    (``NO_VERSION_HINT``; fail closed: it is not known to be new enough)."""
    try:
        version = run(claude, environ, gw, writable=writable)
    except NotRun as error:
        raise LaunchValidationError(
            f"could not run {claude.path!r} --version: {error.reason}",
            end_reason="claude_not_found",
            hint=NOT_RUN_HINT,
        ) from None
    text = version.text
    ok = at_least(version.line, minimum)
    if ok is None:
        raise LaunchValidationError(
            f"{claude.path!r} --version printed no version: {text!r}",
            end_reason="claude_too_old",
            hint=NO_VERSION_HINT,
        )
    if not ok:
        raise LaunchValidationError(
            f"Claude Code {text!r} ({claude.path!r}) is older than min_claude_version {minimum}",
            end_reason="claude_too_old",
            hint=TOO_OLD_HINT,
        )
    return text
