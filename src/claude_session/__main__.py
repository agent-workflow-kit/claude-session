"""Entry point for ``python -m claude_session`` and the ``claude-session`` console script.

``hook-event`` is dispatched here, before ``cli`` (and argparse) is imported, so the injected
hooks stay fast (design.md, Hook-event fast path). Keep this module standard library only.
"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Sequence


def _redirect_to_devnull(*fds: int) -> None:
    """Point ``fds`` at /dev/null, so nothing buffered or written later reaches them."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        for fd in fds:
            if fd != devnull:
                os.dup2(devnull, fd)
        if devnull not in fds:  # a closed fd 1 or 2 is reused by os.open; keep it then
            os.close(devnull)
    except OSError:
        pass


def _silence_output() -> None:
    _redirect_to_devnull(1, 2)


def _hook_event(argv: list[str]) -> int:
    """Run ``hook-event``; it never fails a session, so any error exits 0 with no output.

    Its stdout is captured and written only after it returns normally, so a failure can never
    leak partial output to Claude Code.
    """
    real_stdout, real_stderr = sys.stdout, sys.stderr
    captured_stdout, captured_stderr = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = captured_stdout, captured_stderr
    failed = False
    code = 0
    try:
        from . import hookevent

        code = hookevent.main(argv)
    except BaseException:  # containment: hook-event never fails a session
        # TODO(#7): log at error
        failed = True
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
    if failed:
        _silence_output()
        return 0
    # captured_stderr is dropped. TODO(#7): route it to the log.
    try:
        real_stdout.write(captured_stdout.getvalue())
        real_stdout.flush()
    except BaseException:
        _silence_output()
        return 0
    return code


def _flush_or_discard() -> None:
    """Flush stdout and stderr; a stream that cannot be written is pointed at /dev/null so
    the interpreter's exit-time flush does not fail again (and change the exit code)."""
    for stream, fd in ((sys.stdout, 1), (sys.stderr, 2)):
        try:
            stream.flush()
        except OSError:
            _redirect_to_devnull(fd)


def _cli(argv: list[str]) -> int:
    code: int = 1
    try:
        from .cli import main as cli_main

        code = cli_main(argv)
    except SystemExit as exc:  # --help, --version
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BrokenPipeError:
        # A closed output stream (https://docs.python.org/3/library/signal.html#note-on-sigpipe).
        _redirect_to_devnull(1, 2)
    finally:
        _flush_or_discard()
    return code


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["hook-event"]:
        return _hook_event(args[1:])
    return _cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
