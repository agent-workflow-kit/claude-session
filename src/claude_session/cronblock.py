"""``cron install`` / ``cron uninstall`` (interface.md, cron install / cron uninstall).

Owns one block of the user's crontab, delimited by two exact marker lines::

    # claude-session managed
    */10 * * * * /bin/sh -c '... reconcile ...'
    # claude-session managed end

Everything outside the block is passed through byte for byte. The block is read with
``crontab -l`` and written with ``crontab -`` (or removed with ``crontab -r`` when nothing else
is left), both through the gateway, so the real crontab is never touched by tests.

Safety rules:

- ``crontab -l`` failing is "no crontab" only when its error says so; any other failure ends
  the command (exit 12) rather than treating the crontab as empty and overwriting it.
- ``crontab -l`` output is read as bytes and decoded strictly: a crontab that is not valid
  UTF-8 cannot be rewritten faithfully, so it is refused. Lines are split on ``\n`` only, so
  carriage returns (lone or in CRLF line ends) stay where they are in every foreign line.
- A malformed marker structure (an unterminated or repeated block, a stray end marker) is
  refused with a message; the user fixes it by hand. A marker line may end in spaces, tabs
  and a carriage return (a crontab saved with CRLF line ends), nothing else.
- Read-modify-write is not atomic across processes and ``crontab`` has no lock: the crontab
  is read again right before the write and the edit recomputed when it changed
  (``ATTEMPTS`` times), and after the write the block (only the block) is read back and
  checked. This catches an edit made between two back-to-back reads, not one made between
  the last read and the write: such an edit is lost.
- Every value that reaches the crontab line is shell-quoted and ``%`` is escaped for cron;
  values holding a newline, carriage return, NUL or backslash are refused (cron cannot
  represent a backslash before ``%``). The command runs under an explicit ``/bin/sh -c`` so
  a ``SHELL=`` line elsewhere in the crontab cannot change its meaning, and its ``PATH`` is
  set in the command, never as a crontab assignment (which would change the environment of
  the user's own entries below the block). The ``--env-file`` is sourced after that, so it
  may extend ``PATH``; the interpreter runs with ``-E -P`` (``PYTHON*`` variables ignored,
  the cwd kept off ``sys.path``) but keeps the user site, where a ``pip install --user``
  puts claude-session.
- ``--env-file`` is run as the user by cron, so it and every directory above it must be
  safe from other users (``check_env_file``).
"""

from __future__ import annotations

import os
import pwd
import shlex
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import FailedError, UsageError
from .gateway import GatewayTimeout, sanitize_path

if TYPE_CHECKING:
    from .gateway import Gateway

HEADER = "# claude-session managed"
FOOTER = "# claude-session managed end"
DEFAULT_INTERVAL = 10
ATTEMPTS = 3
CRONTAB_TIMEOUT = 30.0
MAX_ERROR_TEXT = 200
_BAD_CHARS = ("\n", "\r", "\x00", "\\")  # a backslash before % cannot be written for cron
# Interpreter flags: -E ignores PYTHON* variables, -P keeps the cwd (HOME under cron) off
# sys.path; not -I, which would also drop the user site (a ``pip install --user``).
PYTHON_FLAGS = ("-E", "-P")


def _clean(text: str) -> str:
    """``text`` for an error message: control characters escaped, cut to a sane length."""
    shown = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in text.strip())
    return shown[:MAX_ERROR_TEXT]


def _has_bad_char(text: str) -> bool:
    return any(c in text for c in _BAD_CHARS)


# The block


def check_env_file(path: str) -> str:
    """The real path of ``--env-file`` once it passed the checks, else ``UsageError``: a
    regular file, owned by the user, readable by the user and not group- or
    world-writable, in a directory that is not group- or world-writable, below directories
    that are not group- or world-writable unless sticky, every one of them owned by the user
    or root (anyone who can replace the file runs code as the user). Symlinks are followed
    and the target is what is checked and written, so re-pointing a link later changes
    nothing."""
    if not path or _has_bad_char(path):
        raise UsageError("--env-file: not a usable path (newline, carriage return or backslash)")
    real = os.path.realpath(os.path.abspath(path))
    try:
        info = os.stat(real)
    except OSError as exc:
        raise UsageError(f"--env-file: cannot read {_clean(path)!r}: {exc.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise UsageError(f"--env-file: {_clean(path)!r} is not a regular file")
    if info.st_uid != os.geteuid():
        raise UsageError(f"--env-file: {_clean(path)!r} is not owned by you")
    if info.st_mode & 0o022:
        raise UsageError(f"--env-file: {_clean(path)!r} is group- or world-writable")
    if not os.access(real, os.R_OK):
        raise UsageError(f"--env-file: {_clean(path)!r} is not readable by you")
    if _has_bad_char(real):
        raise UsageError("--env-file: not a usable path (newline, carriage return or backslash)")
    _check_env_file_directories(real)
    return real


def _check_env_file_directories(real: str) -> None:
    """Refuse when a directory above ``real`` would let another user replace it (see
    ``check_env_file``). The sticky bit does not excuse the file's own directory: a file
    deleted there (``/tmp``) can be recreated by anyone."""
    uid = os.geteuid()
    directory = os.path.dirname(real)
    immediate = True
    while True:
        try:
            info = os.stat(directory)
        except OSError as exc:
            raise UsageError(
                f"--env-file: cannot check directory {_clean(directory)!r}: {exc.strerror}"
            ) from None
        if info.st_uid not in (uid, 0):
            raise UsageError(
                f"--env-file: directory {_clean(directory)!r} is owned by neither you nor root"
            )
        if info.st_mode & 0o022 and (immediate or not info.st_mode & stat.S_ISVTX):
            raise UsageError(
                f"--env-file: directory {_clean(directory)!r} is group- or world-writable"
                + ("" if immediate else " without the sticky bit")
            )
        parent = os.path.dirname(directory)
        if parent == directory:
            return
        directory, immediate = parent, False


def _usable_home(home: str) -> bool:
    return os.path.isabs(home) and not _has_bad_char(home) and ":" not in home


def _home(environ: Mapping[str, str]) -> str:
    """``HOME``, or the passwd entry's home when ``HOME`` is unset, empty or relative."""
    home = environ.get("HOME", "")
    if not os.path.isabs(home):
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            home = ""
    if not _usable_home(home):
        raise FailedError("cannot determine a usable home directory for the cron PATH")
    return home.rstrip("/") or "/"


def build_block(*, interval: int, env_file: str | None, python: str, home: str) -> str:
    """The managed block (no trailing newline) for ``reconcile`` every ``interval``
    minutes. ``env_file`` is an already checked real path."""
    if _has_bad_char(python) or not os.path.isabs(python):
        raise FailedError("cannot determine a usable absolute path of this Python interpreter")
    if not _usable_home(home):
        raise FailedError("cannot determine a usable home directory for the cron PATH")
    path = f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin" if home != "/" else "/usr/bin:/bin"
    # PATH first, then the env file (which may extend it). ``.`` is a special builtin: a
    # missing file aborts the non-interactive shell; its last command's status does not.
    script = f"PATH={shlex.quote(path)}; export PATH; "
    if env_file is not None:
        script += f". {shlex.quote(env_file)}; "
    flags = " ".join(PYTHON_FLAGS)
    script += f"exec {shlex.quote(python)} {flags} -m claude_session reconcile >/dev/null"
    command = f"/bin/sh -c {shlex.quote(script)}".replace("%", "\\%")
    return "\n".join([HEADER, f"*/{interval} * * * * {command}", FOOTER])


@dataclass(frozen=True)
class Split:
    """A crontab around its managed block: the lines before and after (``block`` is the
    block's lines, markers included; None when there is none)."""

    before: list[str]
    block: list[str] | None
    after: list[str]


def _lines(text: str) -> list[str]:
    """``text`` as lines, split on ``\\n`` only (``splitlines`` would also split on
    characters a crontab entry may hold). The final newline makes no empty last line."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def split_block(text: str) -> Split:
    lines = _lines(text)
    heads = [i for i, line in enumerate(lines) if line.rstrip(" \t\r") == HEADER]
    feet = [i for i, line in enumerate(lines) if line.rstrip(" \t\r") == FOOTER]
    if not heads and not feet:
        return Split(lines, None, [])
    if len(heads) != 1 or len(feet) != 1 or feet[0] < heads[0]:
        raise FailedError(
            "the crontab has a malformed claude-session block "
            f"(expected one {HEADER!r} line followed by one {FOOTER!r} line)",
            detail="fix or remove the marker lines by hand, then retry",
        )
    return Split(lines[: heads[0]], lines[heads[0] : feet[0] + 1], lines[feet[0] + 1 :])


def _join(lines: list[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


# The crontab program


class Crontab:
    """The ``crontab`` program, run through the gateway."""

    def __init__(
        self, gateway: Gateway, environ: Mapping[str, str], program: str | None = None
    ) -> None:
        """``program``: the absolute path of ``crontab``; by default it is looked up on
        ``environ``'s ``PATH`` (absolute entries only)."""
        self.gateway = gateway
        self.program = self._find(environ) if program is None else program

    @staticmethod
    def _find(environ: Mapping[str, str]) -> str:
        for entry in sanitize_path(environ.get("PATH", "")).split(os.pathsep):
            candidate = os.path.join(entry, "crontab")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        raise FailedError("the crontab command was not found on PATH")

    def _run(self, *args: str, stdin: str | None = None) -> Any:
        try:
            return self.gateway.run(
                [self.program, *args], stdin=stdin, timeout=CRONTAB_TIMEOUT
            )
        except GatewayTimeout:
            raise FailedError(f"crontab {' '.join(args)} timed out") from None
        except OSError as exc:
            raise FailedError(f"cannot run crontab: {exc.strerror or type(exc).__name__}") from None

    def read(self) -> str:
        """The current crontab text ('' when the user has none)."""
        result = self._run("-l")
        if result.returncode != 0:
            if "no crontab" in result.stderr.lower():
                return ""
            raise FailedError(
                f"crontab -l failed (exit {result.returncode})", detail=_clean(result.stderr)
            )
        raw = result.stdout_bytes
        try:
            # Strict, and without newline translation: carriage returns stay as they are.
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise FailedError(
                "the crontab is not valid UTF-8 and cannot be rewritten safely",
                detail="nothing was changed",
            ) from None

    def write(self, text: str) -> None:
        """Replace the crontab by ``text`` (``crontab -r`` for the empty text)."""
        result = self._run("-r") if text == "" else self._run("-", stdin=text)
        if result.returncode != 0:
            raise FailedError(
                f"crontab {'-r' if text == '' else '-'} failed (exit {result.returncode})",
                detail=_clean(result.stderr),
            )


Plan = tuple[str, list[str] | None, str | None, str | None]


def _edit(
    crontab: Crontab, compute: Callable[[Split], Plan], *, dry_run: bool
) -> tuple[str, str | None]:
    """Read, compute and write the crontab. ``compute(split)`` returns ``(action, new
    split lines or None for no change, expected block text or None)``; the result is
    ``(action, block)`` as the commands report them."""
    for _ in range(ATTEMPTS):
        current = crontab.read()
        action, new_lines, shown, expected = compute(split_block(current))
        if new_lines is None or dry_run:
            return action, shown
        if crontab.read() != current:
            continue  # edited since we read it: start over on the new text
        crontab.write(_join(new_lines))
        after = split_block(crontab.read())
        found = None if after.block is None else "\n".join(after.block)
        if found != expected:
            raise FailedError("the crontab does not hold the expected block after the write")
        return action, shown
    raise FailedError("the crontab keeps changing; gave up", detail="retry")


def install(
    gateway: Gateway,
    environ: Mapping[str, str],
    *,
    interval: int | None,
    env_file: str | None,
    dry_run: bool,
    python: str | None = None,
    program: str | None = None,
) -> dict[str, Any]:
    interval = DEFAULT_INTERVAL if interval is None else interval
    if not 1 <= interval <= 59:
        raise UsageError("--interval must be between 1 and 59 minutes")
    real_env = None if env_file is None else check_env_file(env_file)
    block = build_block(
        interval=interval,
        env_file=real_env,
        python=sys.executable if python is None else python,
        home=_home(environ),
    )
    crontab = Crontab(gateway, environ, program)

    def compute(split: Split) -> Plan:
        wanted = block.split("\n")
        if split.block is None:
            return "installed", [*split.before, *wanted], block, block
        if split.block == wanted:
            return "unchanged", None, block, block
        return "updated", [*split.before, *wanted, *split.after], block, block

    action, shown = _edit(crontab, compute, dry_run=dry_run)
    assert shown is not None
    return {
        "action": action,
        "block": shown,
        "dry_run": dry_run,
        "interval_minutes": interval,
        "env_file": real_env,
    }


def uninstall(
    gateway: Gateway, environ: Mapping[str, str], *, dry_run: bool, program: str | None = None
) -> dict[str, Any]:
    crontab = Crontab(gateway, environ, program)

    def compute(split: Split) -> Plan:
        if split.block is None:
            return "unchanged", None, None, None
        return "removed", [*split.before, *split.after], "\n".join(split.block), None

    action, shown = _edit(crontab, compute, dry_run=dry_run)
    return {"action": action, "block": shown, "dry_run": dry_run}
