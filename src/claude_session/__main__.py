"""Entry point for ``python -m claude_session`` and the ``claude-session`` console script.

``hook-event`` is dispatched here, before ``cli`` (and argparse) is imported, so the injected
hooks stay fast (design.md, Hook-event fast path). Keep this module standard library only.
"""

from __future__ import annotations

import io
import os
import re
import sys
from collections.abc import Sequence

# The events injected hooks pass (interface.md, Internal commands); only these are logged by
# name, so argv text never reaches the log.
HOOK_EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PermissionRequest",
        "Notification",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }
)
# The csession_id grammar (interface.md, Identifiers). TODO(#5): share it with the ids module
# if that module stays importable from the hook path.
_CSESSION_ID = re.compile(r"cs-[a-z2-7]{8}")


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
    except BaseException as exc:  # containment: hook-event never fails a session
        failed = True
        _log_hook_event_failure(argv, exc)
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
    if failed:
        _silence_output()
        return 0
    # captured_stderr is dropped. TODO(#13): decide whether hook-event's own stderr reaches the
    # log; schemas/log.json has no event for it and the text may carry payload data.
    try:
        real_stdout.write(captured_stdout.getvalue())
        real_stdout.flush()
    except BaseException:
        _silence_output()
        return 0
    return code


def _state_dir_and_event(argv: list[str]) -> tuple[str | None, str | None]:
    """The ``--state-dir`` value (absolute only) and the hook event name (known names only)
    from hook-event's argv, without argparse. The first ``--state-dir`` wins."""
    state_dir: str | None = None
    event: str | None = None
    rest = iter(argv)
    for arg in rest:
        if arg == "--state-dir" or arg.startswith("--state-dir="):
            value = next(rest, "") if arg == "--state-dir" else arg.partition("=")[2]
            if state_dir is None:
                state_dir = value
        elif event is None and arg in HOOK_EVENTS:
            event = arg
    if not state_dir or not os.path.isabs(state_dir):
        state_dir = None
    return state_dir, event


def _log_hook_event_failure(argv: list[str], exc: BaseException) -> None:
    """Log a contained hook-event error at ``error`` in ``<state-dir>/log.jsonl``, best effort.

    The message is the event and the exception type only: exception text can carry payload
    data (prompt text, tool output), which the log must never contain.
    """
    try:
        state_dir, event = _state_dir_and_event(argv)
        if state_dir is None:
            return
        from . import logs

        session = os.environ.get("CSESSION_ID", "")
        name = type(exc).__name__
        logs.EventLog(os.path.join(state_dir, "log.jsonl")).write(
            "hook_event_failed",
            level="error",
            csession_id=session if _CSESSION_ID.fullmatch(session) else None,
            message=f"{event}: {name}" if event else name,
        )
    except BaseException:  # logging is best effort; hook-event still exits 0 silently
        pass


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
