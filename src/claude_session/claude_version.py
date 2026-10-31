"""``claude --version`` and the ``min_claude_version`` comparison (interface.md, kinds and
Launch order step 5; design.md, Platform notes).

``kinds`` reports the version and ``claude_version_ok``; the launch (``launch.checks``, right
after the tools are resolved, so a dry run makes the same check) calls ``require``:
``claude_too_old`` below ``min_claude_version`` or when the output holds no version, and
``claude_not_found`` when ``claude --version`` cannot run. One probe per launch, bounded in
time (``PROBE_TIMEOUT`` plus ``KILL_GRACE``) and in memory (``MAX_OUTPUT``); nothing is
cached: the version is read again at every launch, so an upgraded or downgraded ``claude``
is seen at once.

Versions are compared part by part as numbers of any length (leading zeros ignored, missing
trailing parts count as 0) without converting them to ``int``, so no output can make the
comparison fail. Only ASCII digits form a version.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from . import pathrules
from .errors import LaunchValidationError
from .gateway import Gateway, GatewayTimeout
from .pathrules import ResolvedTool

PROBE_TIMEOUT = 10.0
# SIGTERM-to-SIGKILL delay for the probe's process group on a timeout.
KILL_GRACE = 2.0
# The only variables the probe gets: nothing secret (auth, secret_env, env.set) reaches it.
PROBE_NAMES = ("PATH", "HOME", "LANG", "LANGUAGE")
# The longest version text kept (the first line of the output).
MAX_TEXT = 200
# The most output read into memory, per stream (the rest is read and dropped).
MAX_OUTPUT = 64 * 1024
TOO_OLD_HINT = "upgrade Claude Code (claude update), or set min_claude_version in the config"
# A version at the start of the output: ``2.1.312 (Claude Code)``.
_LEADING = re.compile(r"([0-9]+(?:\.[0-9]+)*)(?:\s|$)")
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


def probe(
    claude: ResolvedTool,
    environ: Mapping[str, str],
    gw: Gateway,
    *,
    writable: Callable[[str], bool],
    timeout: float = PROBE_TIMEOUT,
) -> str | None:
    """The first line of ``claude --version`` (stripped, control characters removed, at most
    ``MAX_TEXT`` characters), run from the resolved realpath with ``argv[0]`` the tool's
    name, in ``/`` with the minimal environment (``probe_env``: no ``PATH`` entry under
    ``writable``) and in its own process group, which a timeout ends as a whole and which
    is SIGKILLed when ``claude`` exits, so no descendant outlives the probe; None when it
    cannot run (also when no ``PATH`` entry is left), times out, exits non-zero or prints
    nothing. The caller never passes a binary a launch would refuse."""
    env = probe_env(environ, writable)
    if not env["PATH"]:
        return None
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
            max_output=MAX_OUTPUT,
        )
    except (OSError, ValueError, GatewayTimeout):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    text = _CONTROL.sub("", lines[0]).strip()[:MAX_TEXT]
    return text or None


def _parts(text: str) -> tuple[str, ...] | None:
    """The digit strings of the version at the start of ``text``, or None."""
    match = _LEADING.match(text.strip())
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
    """The launch's version check: ``probe`` (``writable``: the run's session-writable
    paths, which no ``PATH`` entry of the probe may be under), then ``at_least``. Returns the
    version text. Raises ``LaunchValidationError``: ``claude_not_found`` when
    ``claude --version`` cannot run (it fails to start, exits non-zero, prints nothing, times
    out, or no ``PATH`` entry is left to run it with), ``claude_too_old`` when the version
    is older than ``minimum`` or the output holds no version (fail closed: it is not known
    to be new enough)."""
    if not probe_path(environ, writable):
        raise LaunchValidationError(
            f"could not run {claude.path!r} --version: no PATH entry outside the "
            "session-writable paths",
            end_reason="claude_not_found",
        )
    text = probe(claude, environ, gw, writable=writable)
    if text is None:
        raise LaunchValidationError(
            f"could not run {claude.path!r} --version (it failed, printed nothing or timed out)",
            end_reason="claude_not_found",
        )
    ok = at_least(text, minimum)
    if ok is None:
        raise LaunchValidationError(
            f"{claude.path!r} --version printed no version: {text!r}; "
            f"min_claude_version is {minimum}",
            end_reason="claude_too_old",
            hint=TOO_OLD_HINT,
        )
    if not ok:
        raise LaunchValidationError(
            f"Claude Code {text!r} ({claude.path!r}) is older than min_claude_version {minimum}",
            end_reason="claude_too_old",
            hint=TOO_OLD_HINT,
        )
    return text
