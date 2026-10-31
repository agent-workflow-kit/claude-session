"""``claude --version`` and the ``min_claude_version`` comparison (interface.md, kinds and
Launch order step 5; design.md, Platform notes).

``kinds`` reports the version and ``claude_version_ok``. TODO(#40): the launch check
(``claude_too_old``) at the version slot of step 5 reuses ``probe`` and ``at_least``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .gateway import Gateway, GatewayTimeout
from .pathrules import ResolvedTool

PROBE_TIMEOUT = 10.0
# The longest version text kept (the first line of the output).
MAX_TEXT = 200
# A version at the start of the output: ``2.1.312 (Claude Code)``.
_LEADING = re.compile(r"([0-9]+(?:\.[0-9]+)*)(?:\s|$)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def probe(claude: ResolvedTool, environ: Mapping[str, str], gw: Gateway) -> str | None:
    """The first line of ``claude --version`` (stripped, control characters removed, at most
    ``MAX_TEXT`` characters), run from the resolved realpath with ``argv[0]`` the tool's
    name; None when it cannot run, times out, exits non-zero or prints nothing."""
    try:
        result = gw.run(
            [claude.name, "--version"],
            env=dict(environ),
            timeout=PROBE_TIMEOUT,
            executable=claude.path,
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


def parse(text: str) -> tuple[int, ...] | None:
    """The version at the start of ``text`` (``2.1.312 (Claude Code)`` -> ``(2, 1, 312)``),
    or None when it does not start with one."""
    match = _LEADING.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def at_least(text: str | None, minimum: str) -> bool | None:
    """Whether the version in ``text`` is ``minimum`` or newer (missing trailing parts count
    as 0); None when ``text`` is None or holds no version."""
    if text is None:
        return None
    version = parse(text)
    required = parse(minimum)
    if version is None or required is None:
        return None
    width = max(len(version), len(required))
    return version + (0,) * (width - len(version)) >= required + (0,) * (width - len(required))
