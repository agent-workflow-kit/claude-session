"""Entry point for ``python -m claude_session`` and the ``claude-session`` console script.

``hook-event`` is dispatched here, before ``cli`` (and argparse) is imported, so the injected
hooks stay fast (design.md, Hook-event fast path). Keep this module standard library only.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence


def _hook_event(argv: list[str]) -> int:
    """Run ``hook-event``; it never fails a session, so any error exits 0 with no output."""
    try:
        from . import hookevent

        return hookevent.main(argv)
    except BaseException:  # containment: hook-event never fails a session
        # TODO(#7): log at error
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["hook-event"]:
        return _hook_event(args[1:])
    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
