"""The end mapping: how a run's end becomes an outcome and an end reason (interface.md, Outcome
mapping; design.md, State machine and Supervision).

Pure functions over (pending cause, report, headless result, exit status, SessionStart seen),
so ``exec``, the inline liveness check and ``reconcile`` map the same way. Standard library
only.

**Headless result.** ``result.json`` holds Claude Code's ``stream-json`` lines. Every line that
is a JSON object counts; other lines are ignored. The **final result** is the last object with
``type: result``; ``cost_usd`` is the sum of ``total_cost_usd`` and ``num_turns`` the sum of
``num_turns`` over all ``result`` objects (values that are not finite non-negative numbers,
or not non-negative integers, are left out). The result is **complete** when there is at least
one ``result`` object and the last JSON object is one, and the file does not end in a
truncated line: a non-empty tail without its newline (a writer killed mid-line) makes it
incomplete even when the tail happens to parse.

**Interactive table.** ``map_interactive``, first match: the cause and signal rows;
``no_session_start`` when no SessionStart was recorded (``claude`` gone before it, e.g. the
trust dialog quit, whatever its exit code); ``user_exit`` when the last process-ending
SessionEnd reason is ``prompt_input_exit``; then ``normal`` for exit 0 and ``exit_<code>``
otherwise. The run stores only its latest SessionEnd reason, verbatim. That is enough: a
reason that does not end the process (``clear``) never matches, and an earlier
``prompt_input_exit`` followed by more SessionEnds would not have ended the process either.
The reason is compared exactly (no case folding or trimming).
"""

from __future__ import annotations

import json
import math
import os
import signal
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

RESULT_FILE = "result.json"
_READ_CHUNK = 1 << 16
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

REPORT_OUTCOMES = frozenset({"done", "blocked", "needs-input", "failed"})
# Outcome -> exit code (interface.md, Outcome -> exit code).
EXIT_CODES: dict[str, int] = {
    "done": 0,
    "blocked": 13,
    "needs-input": 13,
    "failed": 18,
    "exited-unreported": 14,
    "killed": 15,
    "timeout": 16,
    "lost": 17,
    "not-started": 12,
    "vetoed": 11,
    "escalated": 13,
}
# The cause table: pending_cause -> (outcome without a report, end_reason). ``auto_exit`` is
# set only after a done report, so its outcome is always the report's.
CAUSES: dict[str, tuple[str | None, str]] = {
    "timeout": ("timeout", "timeout"),
    "stopped": ("killed", "stopped"),
    "forwarded_term": ("killed", "forwarded_term"),
    "forwarded_hup": ("killed", "forwarded_hup"),
    "forwarded_int": ("killed", "forwarded_int"),
    "internal_error": ("killed", "internal_error"),
    "auto_exit": (None, "auto_exit"),
}


def final_state(outcome: str) -> str:
    """The final state of an outcome (interface.md, Outcome object: final state per
    outcome)."""
    if outcome in REPORT_OUTCOMES:
        return "exited"
    if outcome in ("not-started", "vetoed", "escalated"):
        return "not-started"
    if outcome in EXIT_CODES:
        return outcome
    raise ValueError(f"unknown outcome {outcome!r}")


@dataclass(frozen=True)
class HeadlessResult:
    """The parsed ``result.json``: ``final`` is the last ``result`` object (None: none)."""

    complete: bool = False
    final: dict[str, Any] | None = field(default=None, compare=False)
    result_count: int = 0
    cost_usd: float | None = None
    num_turns: int | None = None


def _cost(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _turns(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _object(raw: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


class _Parser:
    def __init__(self) -> None:
        self.final: dict[str, Any] | None = None
        self.last_is_result = False
        self.count = 0
        self.cost: float | None = None
        self.turns: int | None = None

    def line(self, raw: bytes) -> None:
        obj = _object(raw.rstrip(b"\r"))
        if obj is None:
            return
        self.last_is_result = obj.get("type") == "result"
        if not self.last_is_result:
            return
        self.final = obj
        self.count += 1
        cost = _cost(obj.get("total_cost_usd"))
        if cost is not None:
            self.cost = (self.cost or 0.0) + cost
        turns = _turns(obj.get("num_turns"))
        if turns is not None:
            self.turns = (self.turns or 0) + turns


def parse_stream_json(chunks: Iterable[bytes]) -> HeadlessResult:
    """Parse ``stream-json`` output given in chunks of any size (see the module docs). Only
    each new chunk is split; the partial line is kept in parts, so a long line costs linear
    time. A summed cost that is not finite is unknown (None)."""
    parser = _Parser()
    partial: list[bytes] = []
    for chunk in chunks:
        *lines, rest = chunk.split(b"\n")
        if lines:
            partial.append(lines[0])
            parser.line(b"".join(partial))
            for raw in lines[1:]:
                parser.line(raw)
            partial = []
        if rest:
            partial.append(rest)
    truncated = b"".join(partial).strip() != b""
    complete = parser.count > 0 and parser.last_is_result and not truncated
    if parser.count == 0:
        return HeadlessResult()
    cost = parser.cost
    return HeadlessResult(
        complete=complete,
        final=parser.final,
        result_count=parser.count,
        cost_usd=cost if cost is None or math.isfinite(cost) else None,
        num_turns=parser.turns,
    )


def read_result_file(run_fd: int, name: str = RESULT_FILE) -> HeadlessResult:
    """Parse the private run directory's ``result.json`` (``run_fd``), read through one
    no-follow descriptor; a missing file is an empty result. Anything but a regular file
    raises ``OSError``."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=run_fd)
    except FileNotFoundError:
        return HeadlessResult()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{name} is not a regular file")

        def chunks() -> Iterable[bytes]:
            while chunk := os.read(fd, _READ_CHUNK):
                yield chunk

        return parse_stream_json(chunks())
    finally:
        os.close(fd)


SIGNAL_MAX = 64  # the highest signal number an exit status 128+n is read as


def signal_end_reason(returncode: int) -> str | None:
    """``signal_<name>`` for a ``claude`` killed by a signal (a negative return code, or exit
    128+n for a signal n of 1 to 64), else None. A signal without a Python name (a real-time
    signal such as 35) is ``signal_<n>``."""
    if returncode < 0:
        number = -returncode
    elif 128 < returncode <= 128 + SIGNAL_MAX:
        number = returncode - 128
    else:
        return None
    try:
        name = signal.Signals(number).name.removeprefix("SIG").lower()
    except ValueError:
        name = str(number)
    return "signal_" + name


@dataclass(frozen=True)
class Mapped:
    """A mapped end: ``implicit`` marks an implicit done the caller must still accept (the
    ``result.md`` ingestion checks; a failure is ``exited-unreported``, ``result_rejected``)."""

    outcome: str
    end_reason: str
    implicit: bool = False


def _with_report(report_status: str | None, outcome: str, end_reason: str) -> Mapped:
    """A report filed in the run gives the report's outcome; the end reason stays."""
    if report_status is not None:
        if report_status not in REPORT_OUTCOMES:
            raise ValueError(f"unknown report status {report_status!r}")
        return Mapped(report_status, end_reason)
    return Mapped(outcome, end_reason)


def _cause(pending_cause: str, report_status: str | None) -> Mapped:
    if pending_cause not in CAUSES:
        raise ValueError(f"unknown pending cause {pending_cause!r}")
    outcome, end_reason = CAUSES[pending_cause]
    if outcome is None:
        # Set only after a done report; a report ingested after that (the final drain) wins.
        if report_status is None:
            raise ValueError("auto_exit is set only after a done report")
        return _with_report(report_status, "done", end_reason)
    return _with_report(report_status, outcome, end_reason)


def map_headless(
    *,
    pending_cause: str | None,
    report_status: str | None,
    result: HeadlessResult,
    returncode: int | None,
    session_started: bool,
    implicit_done: bool,
) -> Mapped:
    """The headless table, first match (interface.md, Outcome mapping: Headless). With a
    report the outcome is the report's and only the end reason comes from the table.
    ``returncode`` None: the exit status is unknown and never used (salvage); the rows that
    need it are skipped and a complete result counts as a clean exit."""
    if pending_cause is not None:
        return _cause(pending_cause, report_status)
    if returncode is not None:
        signalled = signal_end_reason(returncode)
        if signalled is not None:
            return _with_report(report_status, "killed", signalled)
    final = result.final if result.complete else None
    if final is not None:
        if final.get("subtype") == "error_max_turns" or final.get("terminal_reason") == "max_turns":
            return _with_report(report_status, "timeout", "max_turns")
        if final.get("is_error") is True:
            return _with_report(report_status, "exited-unreported", "api_error")
        if returncode in (0, None):
            if report_status is not None:
                return _with_report(report_status, "exited-unreported", "normal")
            if implicit_done:
                return Mapped("done", "normal", implicit=True)
            return Mapped("exited-unreported", "no_report")
    if returncode not in (0, None):
        return _with_report(report_status, "exited-unreported", f"exit_{returncode}")
    reason = "no_result" if session_started else "no_session_start"
    return _with_report(report_status, "exited-unreported", reason)


USER_EXIT_REASON = "prompt_input_exit"  # Claude Code's SessionEnd reason for /exit


def map_interactive(
    *,
    pending_cause: str | None,
    report_status: str | None,
    returncode: int,
    session_started: bool,
    session_end_reason: str | None,
) -> Mapped:
    """The interactive table, first match (interface.md, Outcome mapping: Interactive). With a
    report the outcome is the report's and only the end reason comes from the table.
    ``session_end_reason`` is the run's latest recorded SessionEnd reason (see the module
    docs)."""
    if pending_cause is not None:
        return _cause(pending_cause, report_status)
    signalled = signal_end_reason(returncode)
    if signalled is not None:
        return _with_report(report_status, "killed", signalled)
    if not session_started:
        return _with_report(report_status, "exited-unreported", "no_session_start")
    if session_end_reason == USER_EXIT_REASON:
        return _with_report(report_status, "exited-unreported", "user_exit")
    if returncode == 0:
        return _with_report(report_status, "exited-unreported", "normal")
    return _with_report(report_status, "exited-unreported", f"exit_{returncode}")


def map_wrapper_lost(
    *,
    pending_cause: str | None,
    report_status: str | None,
    headless_result: HeadlessResult | None,
    session_started: bool,
    implicit_done: bool,
) -> Mapped:
    """The wrapper-lost rules (interface.md, Outcome mapping: Wrapper lost): the cause table;
    else a headless run with a complete result maps by the headless table (never using
    claude's exit status); else the report's outcome with ``wrapper_lost``; else ``lost``.
    ``headless_result`` is None for interactive runs.

    TODO(#33): the liveness check and reconcile call this after draining the run's outbox
    and ``claimed/`` (``finalized_from_cause`` / ``finalized_from_result``)."""
    if pending_cause is not None:
        return _cause(pending_cause, report_status)
    if headless_result is not None and headless_result.complete:
        return map_headless(
            pending_cause=None,
            report_status=report_status,
            result=headless_result,
            returncode=None,
            session_started=session_started,
            implicit_done=implicit_done,
        )
    if report_status is not None:
        return _with_report(report_status, "lost", "wrapper_lost")
    return Mapped("lost", "lost")
