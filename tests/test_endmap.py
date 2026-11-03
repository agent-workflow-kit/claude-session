"""The end mapping (interface.md, Outcome mapping; design.md, State machine and Supervision)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from claude_session import endmap
from claude_session.endmap import HeadlessResult, Mapped


def line(obj: Any) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def result(**fields: Any) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False, "result": "ok", **fields}


def parse(*parts: bytes) -> HeadlessResult:
    return endmap.parse_stream_json(parts)


# Parsing stream-json


def test_the_last_result_object_is_the_final_result_and_costs_are_summed() -> None:
    parsed = parse(
        line({"type": "system", "subtype": "init"}),
        line(result(result="first", total_cost_usd=0.5, num_turns=2)),
        line({"type": "assistant"}),
        line(result(result="second", total_cost_usd=0.25, num_turns=3)),
    )
    assert parsed.complete
    assert parsed.final is not None and parsed.final["result"] == "second"
    assert parsed.result_count == 2
    assert parsed.cost_usd == pytest.approx(0.75)
    assert parsed.num_turns == 5


def test_a_result_followed_by_another_object_is_not_complete() -> None:
    parsed = parse(line(result()), line({"type": "assistant"}))
    assert not parsed.complete
    assert parsed.final is not None  # still the last result object (cost is still summed)


@pytest.mark.parametrize(
    "tail",
    [
        b'{"type": "result", "is_er',  # cut mid-object
        json.dumps(result()).encode(),  # a whole object but no newline: cut before it
        '{"type": "result", "result": "café'.encode()[:-1],  # cut inside a character
    ],
)
def test_a_truncated_tail_is_not_complete(tail: bytes) -> None:
    parsed = parse(line(result(total_cost_usd=1.0, num_turns=1)), tail)
    assert not parsed.complete
    assert parsed.cost_usd == 1.0


@pytest.mark.parametrize(
    "noise",
    [b"not json\n", b"[1, 2]\n", b"\n", b'"text"\n', b"\xff\xfe\n", b"{bad json}\n", b"null\n"],
)
def test_lines_that_are_not_json_objects_are_ignored(noise: bytes) -> None:
    parsed = parse(line(result()), noise)
    assert parsed.complete
    assert parsed.result_count == 1


def test_no_result_object_is_incomplete_with_unknown_cost() -> None:
    parsed = parse(line({"type": "system"}))
    assert not parsed.complete and parsed.final is None
    assert parsed.cost_usd is None and parsed.num_turns is None
    assert not parse().complete


@pytest.mark.parametrize(
    "bad",
    [
        {"total_cost_usd": "1.0", "num_turns": "2"},
        {"total_cost_usd": True, "num_turns": False},
        {"total_cost_usd": -1.0, "num_turns": -2},
        {"total_cost_usd": None, "num_turns": 1.5},
        {"total_cost_usd": 1e400},
    ],
)
def test_bad_cost_and_turn_values_are_not_counted(bad: dict[str, Any]) -> None:
    parsed = parse(line(result(total_cost_usd=0.5, num_turns=1)), line(result(**bad)))
    assert parsed.cost_usd == 0.5
    assert parsed.num_turns == 1


def test_infinity_and_nan_are_not_numbers_here() -> None:
    parsed = parse(b'{"type": "result", "total_cost_usd": NaN, "num_turns": Infinity}\n')
    # NaN/Infinity are not JSON: the line is ignored.
    assert parsed.result_count == 0


def test_lines_split_across_chunks_are_joined() -> None:
    data = line({"type": "system"}) + line(result(total_cost_usd=0.1, num_turns=1))
    chunks = [data[i : i + 7] for i in range(0, len(data), 7)]
    parsed = endmap.parse_stream_json(chunks)
    assert parsed.complete and parsed.cost_usd == pytest.approx(0.1)


def test_crlf_lines_parse() -> None:
    parsed = parse(json.dumps(result()).encode() + b"\r\n")
    assert parsed.complete


def test_read_result_file_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(line(result()))
    (tmp_path / "result.json").symlink_to(target)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            endmap.read_result_file(fd)
    finally:
        os.close(fd)


def test_read_result_file_missing_is_empty(tmp_path: Path) -> None:
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert endmap.read_result_file(fd) == HeadlessResult()
        (tmp_path / "result.json").write_bytes(line(result(num_turns=4)))
        assert endmap.read_result_file(fd).num_turns == 4
    finally:
        os.close(fd)


# Signals and exit codes


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (-15, "signal_term"),
        (-9, "signal_kill"),
        (143, "signal_term"),
        (137, "signal_kill"),
        (129, "signal_hup"),
        (0, None),
        (1, None),
        (128, None),
        (255, None),  # 128 + 127: not a signal
        (-35, "signal_35"),  # a real-time signal: no Python name
        (163, "signal_35"),  # 128 + 35
        (192, "signal_rtmax"),  # 128 + 64
        (193, None),  # 128 + 65: not a signal
        (-200, "signal_200"),
    ],
)
def test_signal_end_reason(code: int, reason: str | None) -> None:
    assert endmap.signal_end_reason(code) == reason


def test_a_cost_too_large_for_a_float_is_not_counted() -> None:
    parsed = parse(line(result(total_cost_usd=10**400)), line(result(total_cost_usd=0.5)))
    assert parsed.complete and parsed.cost_usd == 0.5


def test_a_cost_sum_that_overflows_is_unknown() -> None:
    parsed = parse(line(result(total_cost_usd=1.5e308)), line(result(total_cost_usd=1.5e308)))
    assert parsed.complete and parsed.cost_usd is None


def test_a_long_single_line_parses_in_linear_time() -> None:
    import time

    text = "x" * (32 << 20)
    data = line(result(result=text))
    chunks = [data[i : i + (1 << 16)] for i in range(0, len(data), 1 << 16)]
    start = time.monotonic()
    parsed = parse(*chunks)
    assert time.monotonic() - start < 3.0
    assert parsed.complete and parsed.final is not None and len(parsed.final["result"]) == len(text)


def test_outcome_exit_codes_and_states() -> None:
    assert endmap.EXIT_CODES == {
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
    for outcome in ("done", "blocked", "needs-input", "failed"):
        assert endmap.final_state(outcome) == "exited"
    for outcome in ("not-started", "vetoed", "escalated"):
        assert endmap.final_state(outcome) == "not-started"
    assert endmap.final_state("killed") == "killed"


# The headless table, first match


def headless(
    *,
    cause: str | None = None,
    report: str | None = None,
    parsed: HeadlessResult | None = None,
    code: int = 0,
    started: bool = True,
    implicit_done: bool = True,
) -> Mapped:
    return endmap.map_headless(
        pending_cause=cause,
        report_status=report,
        result=parse(line(result())) if parsed is None else parsed,
        returncode=code,
        session_started=started,
        implicit_done=implicit_done,
    )


MAX_TURNS = parse(line(result(subtype="error_max_turns", is_error=True)))
TERMINAL_MAX_TURNS = parse(line(result(terminal_reason="max_turns")))
API_ERROR = parse(line(result(is_error=True, subtype="success")))
INCOMPLETE = parse(line(result()), b"{")
EMPTY = parse()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # the cause table
        ({"cause": "timeout"}, Mapped("timeout", "timeout")),
        ({"cause": "stopped"}, Mapped("killed", "stopped")),
        ({"cause": "forwarded_term"}, Mapped("killed", "forwarded_term")),
        ({"cause": "forwarded_hup"}, Mapped("killed", "forwarded_hup")),
        ({"cause": "forwarded_int"}, Mapped("killed", "forwarded_int")),
        ({"cause": "internal_error"}, Mapped("killed", "internal_error")),
        ({"cause": "auto_exit", "report": "done"}, Mapped("done", "auto_exit")),
        # killed by a signal exec did not send
        ({"code": -9}, Mapped("killed", "signal_kill")),
        ({"code": 143, "parsed": MAX_TURNS}, Mapped("killed", "signal_term")),
        # the cause wins over a signal exit
        ({"cause": "stopped", "code": -15}, Mapped("killed", "stopped")),
        # max turns, on the final result object
        ({"parsed": MAX_TURNS, "code": 1}, Mapped("timeout", "max_turns")),
        ({"parsed": TERMINAL_MAX_TURNS}, Mapped("timeout", "max_turns")),
        # is_error
        ({"parsed": API_ERROR}, Mapped("exited-unreported", "api_error")),
        ({"parsed": API_ERROR, "code": 1}, Mapped("exited-unreported", "api_error")),
        # clean exit with a result
        ({}, Mapped("done", "normal", implicit=True)),
        ({"implicit_done": False}, Mapped("exited-unreported", "no_report")),
        # non-zero exit
        ({"code": 1}, Mapped("exited-unreported", "exit_1")),
        ({"code": 2, "parsed": INCOMPLETE}, Mapped("exited-unreported", "exit_2")),
        # no complete result
        ({"parsed": INCOMPLETE}, Mapped("exited-unreported", "no_result")),
        ({"parsed": EMPTY}, Mapped("exited-unreported", "no_result")),
        ({"parsed": EMPTY, "started": False}, Mapped("exited-unreported", "no_session_start")),
    ],
)
def test_headless_rows_without_a_report(kwargs: dict[str, Any], expected: Mapped) -> None:
    assert headless(**kwargs) == expected


@pytest.mark.parametrize(
    ("kwargs", "end_reason"),
    [
        ({}, "normal"),
        ({"implicit_done": False}, "normal"),
        ({"parsed": API_ERROR}, "api_error"),
        ({"parsed": EMPTY}, "no_result"),
        ({"code": 3}, "exit_3"),
        ({"cause": "stopped"}, "stopped"),
        ({"code": -15}, "signal_term"),
        ({"parsed": MAX_TURNS}, "max_turns"),
    ],
)
@pytest.mark.parametrize("status", ["done", "blocked", "needs-input", "failed"])
def test_with_a_report_the_outcome_is_the_reports(
    kwargs: dict[str, Any], end_reason: str, status: str
) -> None:
    assert headless(report=status, **kwargs) == Mapped(status, end_reason)


def test_unknown_cause_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        headless(cause="nonsense")


def test_auto_exit_without_a_done_report_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        headless(cause="auto_exit")


def test_wrapper_lost_rules() -> None:
    complete = parse(line(result()))
    assert endmap.map_wrapper_lost(
        pending_cause="stopped",
        report_status=None,
        headless_result=complete,
        session_started=True,
        implicit_done=True,
    ) == Mapped("killed", "stopped")
    # A complete headless result: the headless table, claude's exit status never used.
    assert endmap.map_wrapper_lost(
        pending_cause=None,
        report_status=None,
        headless_result=complete,
        session_started=True,
        implicit_done=True,
    ) == Mapped("done", "normal", implicit=True)
    assert endmap.map_wrapper_lost(
        pending_cause=None,
        report_status="blocked",
        headless_result=INCOMPLETE,
        session_started=True,
        implicit_done=True,
    ) == Mapped("blocked", "wrapper_lost")
    assert endmap.map_wrapper_lost(
        pending_cause=None,
        report_status=None,
        headless_result=None,
        session_started=True,
        implicit_done=True,
    ) == Mapped("lost", "lost")
