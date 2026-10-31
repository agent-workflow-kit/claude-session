"""Gateway: the single boundary for subprocess and HTTP calls.

Every call to an external program (or HTTP endpoint) goes through this module so that
tests can substitute fakes (see design guide §14). Commands are always run with an argv
list, never through a shell.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping


def run(
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    devnull_stdin: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell and capture its text output. ``env`` replaces the whole
    environment of the child (None: inherit this process's environment). ``stdin`` is the
    child's input; with ``devnull_stdin`` (and no ``stdin``) its stdin is ``/dev/null``
    instead of this process's stdin."""
    if devnull_stdin and stdin is None:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            env=None if env is None else dict(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    return subprocess.run(
        argv,
        input=stdin,
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
