"""Entry point for ``python -m claude_session`` and the ``claude-session`` console script.

``hook-event`` is dispatched here, before ``cli`` (and argparse) is imported, so the injected
hooks stay fast (design.md, Hook-event fast path). Keep this module standard library only.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence


def _silence_output() -> None:
    """Point fds 1 and 2 at /dev/null, so nothing buffered or written later reaches Claude."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:  # a closed fd 1 or 2 is reused by os.open; keep it open then
            os.close(devnull)
    except OSError:
        pass


def _hook_event(argv: list[str]) -> int:
    """Run ``hook-event``; it never fails a session, so any error exits 0 with no output."""
    try:
        from . import hookevent

        code = hookevent.main(argv)
        sys.stdout.flush()
        return code
    except BaseException:  # containment: hook-event never fails a session
        # TODO(#7): log at error
        _silence_output()
        return 0


def _cli(argv: list[str]) -> int:
    code = 1
    try:
        from .cli import main as cli_main

        code = cli_main(argv)
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader went away: keep the exit code, and stop Python's exit-time flush from
        # failing again (https://docs.python.org/3/library/signal.html#note-on-sigpipe).
        devnull = os.open(os.devnull, os.O_WRONLY)
        if devnull != 1:
            os.dup2(devnull, 1)
            os.close(devnull)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["hook-event"]:
        return _hook_event(args[1:])
    return _cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
