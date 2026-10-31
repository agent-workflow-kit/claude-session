"""Entry point for ``python -m claude_session`` and the ``claude-session`` console script.

``hook-event`` is dispatched here, before ``cli`` (and argparse) is imported, so the injected
hooks stay fast (design.md, Hook-event fast path). Keep this module standard library only.
"""

from __future__ import annotations

import fcntl
import io
import os
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


def _divert_output() -> list[int | None]:
    """Save fds 1 and 2 aside (above 2, close-on-exec) and point both at /dev/null, so raw fd
    writes (``os.write``, child processes) during hook-event never reach Claude Code. Returns
    the saved descriptors (None for one that was closed)."""
    saved: list[int | None] = []
    for fd in (1, 2):
        try:
            saved.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3))
        except OSError:
            saved.append(None)
    _redirect_to_devnull(1, 2)
    return saved


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _restore_output(saved: list[int | None]) -> None:
    """Point fds 1 and 2 back at the descriptors ``_divert_output`` saved, and close those
    (their entries become None, so they are never closed twice). Raises ``OSError`` if a
    descriptor cannot be restored (fds 1 and 2 may then be half restored)."""
    for index, (fd, copy) in enumerate(zip((1, 2), saved, strict=True)):
        if copy is None:
            continue
        saved[index] = None
        try:
            os.dup2(copy, fd)
        finally:
            _close_quietly(copy)


def _silence_output(saved: list[int | None]) -> None:
    """Leave fds 1 and 2 at /dev/null for the rest of the process and close the saved ones."""
    for copy in saved:
        if copy is not None:
            _close_quietly(copy)
    _redirect_to_devnull(1, 2)


def _drop_stray_output(*streams: object) -> None:
    """Flush ``streams`` (and the interpreter's original stdout and stderr) while fds 1 and 2
    still point at /dev/null, so text the handler wrote to them directly is dropped."""
    for stream in {id(s): s for s in (*streams, sys.__stdout__, sys.__stderr__)}.values():
        try:
            stream.flush()  # type: ignore[union-attr]
        except BaseException:
            pass


def _hook_event(argv: list[str]) -> int:
    """Run ``hook-event``. It always exits 0 (it acts on Claude Code only through its JSON
    output) and never fails a session: any error exits 0 with no output.

    While it runs, ``sys.stdout``/``sys.stderr`` are captured and fds 1 and 2 point at
    /dev/null. Only after it returns normally are the fds restored and the captured stdout
    written; raw fd writes, text written to ``sys.__stdout__``/``sys.__stderr__`` (flushed to
    /dev/null before the restore) and the captured stderr are dropped, so a failure can never
    leak partial output to Claude Code. A failure to restore or write leaves fds 1 and 2 at
    /dev/null and still exits 0.
    """
    saved = _divert_output()
    real_stdout, real_stderr = sys.stdout, sys.stderr
    captured_stdout, captured_stderr = io.StringIO(), io.StringIO()
    sys.stdout, sys.stderr = captured_stdout, captured_stderr
    failed = False
    try:
        from . import hookevent

        hookevent.main(argv)  # the return value is ignored: hook-event always exits 0
    except BaseException as exc:  # containment: hook-event never fails a session
        failed = True
        _log_hook_event_failure(argv, exc)
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
    if failed:
        _silence_output(saved)
        return 0
    # captured_stderr is dropped, never logged: the text may carry payload data (prompt text,
    # tool output) and schemas/log.json has no event for it (design.md, Hook-event fast path).
    try:
        _drop_stray_output(real_stdout, real_stderr)
        _restore_output(saved)
        real_stdout.write(captured_stdout.getvalue())
        real_stdout.flush()
    except BaseException:  # containment: nothing reaches Claude Code, and the exit is still 0
        _silence_output(saved)
    return 0


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
            csession_id=session if logs.is_csession_id(session) else None,
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
