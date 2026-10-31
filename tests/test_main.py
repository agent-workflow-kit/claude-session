"""The entry point: hook-event dispatch and containment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from claude_session import __main__ as entry
from claude_session import cli, hookevent

from .schemas import validate


@pytest.mark.parametrize("error", [RuntimeError("boom"), SystemExit(2), KeyError("x")])
def test_hook_event_errors_exit_0_silently(
    error: BaseException, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(argv: list[str]) -> int:
        raise error

    silenced: list[bool] = []
    monkeypatch.setattr(hookevent, "main", broken)

    # Redirecting fds 1 and 2 would break pytest; the subprocess tests below cover it.
    def silence(saved: list[int | None]) -> None:
        entry._restore_output(saved)
        silenced.append(True)

    monkeypatch.setattr(entry, "_silence_output", silence)
    assert entry.main(["hook-event", "--state-dir", "/nonexistent", "Stop"]) == 0
    assert capsys.readouterr() == ("", "")
    assert silenced == [True]


def test_hook_event_receives_its_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def record(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(hookevent, "main", record)
    assert entry.main(["hook-event", "--state-dir", "/s", "Stop"]) == 0
    assert seen == [["--state-dir", "/s", "Stop"]]


def test_hook_event_refuses_extra_arguments_and_still_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the injected argv is accepted (hookevent.parse_argv); anything else is a contained
    error: logged, no output, exit 0."""
    assert entry.main(["hook-event", "--state-dir", str(tmp_path), "Stop", "--yes"]) == 0
    assert capsys.readouterr() == ("", "")
    (record,) = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert (record["event"], record["message"]) == ("hook_event_failed", "Stop: HookEventError")


def test_other_commands_go_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(cli.HANDLERS, "kinds", lambda args: 77)
    assert entry.main(["kinds"]) == 77


def test_console_script_uses_the_light_entry_point() -> None:
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"claude-session": "claude_session.__main__:main"}


def run_python(
    code: str, *, broken: tuple[str, ...] = (), env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter. Streams named in ``broken`` ("stdout", "stderr")
    are pipes whose read end is already closed; the others are captured."""
    streams: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    write_ends: list[int] = []
    for name in broken:
        read_end, write_end = os.pipe()
        os.close(read_end)
        write_ends.append(write_end)
        streams[name] = write_end
    try:
        return subprocess.run(
            [sys.executable, "-c", code],
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, **(env or {})},
            **streams,
        )
    finally:
        for fd in write_ends:
            os.close(fd)


def entry_code(argv: list[str]) -> str:
    return f"import sys\nfrom claude_session.__main__ import main\nsys.exit(main({argv!r}))\n"


UNBUFFERED = {"PYTHONUNBUFFERED": "1"}


HOOK_PRINTS_THEN = """
import sys
from claude_session import __main__ as entry, hookevent

def hook(argv):
    print("partial output")
    {after}

hookevent.main = hook
{before}
sys.exit(entry.main(["hook-event", "--state-dir", "/nonexistent", "Stop"]))
"""


def test_hook_event_failure_after_output_leaves_both_streams_empty() -> None:
    proc = run_python(HOOK_PRINTS_THEN.format(after="raise RuntimeError('x')", before=""))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_hook_event_with_closed_stdout_exits_0_silently() -> None:
    code = HOOK_PRINTS_THEN.format(after="return 0", before="import os; os.close(1)")
    proc = run_python(code)
    assert (proc.returncode, proc.stderr) == (0, "")


def test_broken_stdout_pipe_keeps_the_exit_code() -> None:
    proc = run_python(entry_code(["exec", "--timeout", "0", "--json"]), broken=("stdout",))
    assert proc.returncode == 2
    assert "Exception ignored" not in proc.stderr
    assert "BrokenPipeError" not in proc.stderr


def test_broken_stderr_pipe_keeps_the_usage_exit_code() -> None:
    proc = run_python(entry_code(["exec", "--bogus"]), broken=("stderr",))
    assert proc.returncode == 2


def test_unbuffered_broken_stdout_keeps_the_usage_exit_code() -> None:
    proc = run_python(entry_code(["exec", "--json", "--bogus"]), broken=("stdout",), env=UNBUFFERED)
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "Exception ignored" not in proc.stderr


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["exec", "--help"]])
def test_help_and_version_to_a_broken_stdout_exit_0_quietly(argv: list[str]) -> None:
    proc = run_python(entry_code(argv), broken=("stdout",))
    assert (proc.returncode, proc.stderr) == (0, "")


@pytest.mark.parametrize(
    "output",
    [
        "print('partial output')",
        "print('x' * 65536)",
        "sys.stderr.write('noise\\n')",
    ],
    ids=["unbuffered", "64k", "stderr"],
)
def test_hook_event_output_before_a_failure_never_escapes(output: str) -> None:
    code = HOOK_OUTPUT_THEN.format(output=output, after="raise RuntimeError('x')")
    proc = run_python(code, env=UNBUFFERED)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_hook_event_stdout_is_written_and_stderr_dropped_on_success() -> None:
    code = HOOK_OUTPUT_THEN.format(
        output="print('{\"ok\": true}'); sys.stderr.write('noise\\n')", after="return 0"
    )
    proc = run_python(code, env=UNBUFFERED)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, '{"ok": true}\n', "")


HOOK_OUTPUT_THEN = """
import sys
from claude_session import __main__ as entry, hookevent

def hook(argv):
    {output}
    {after}

hookevent.main = hook
sys.exit(entry.main(["hook-event", "--state-dir", "/nonexistent", "Stop"]))
"""


# hook-event failures are logged at error (interface.md, Internal commands)

LEAKY = "prompt text with s3cr3t-Value-0123456789"


def fail_hook(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    def broken(argv: list[str]) -> int:
        raise error

    monkeypatch.setattr(hookevent, "main", broken)
    monkeypatch.setattr(entry, "_silence_output", entry._restore_output)


def log_records(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / "log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_hook_event_failure_is_logged_at_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_hook(monkeypatch, RuntimeError(LEAKY))
    monkeypatch.setenv("CSESSION_ID", "cs-abcd2345")
    assert entry.main(["hook-event", "--state-dir", str(tmp_path), "Stop"]) == 0
    [record] = log_records(tmp_path)
    validate(record, "claude-session/log.json")
    assert record["level"] == "error"
    assert record["event"] == "hook_event_failed"
    assert record["csession_id"] == "cs-abcd2345"
    assert record["message"] == "Stop: RuntimeError"


def test_hook_event_failure_log_never_carries_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_hook(monkeypatch, KeyError(LEAKY))
    entry.main(["hook-event", f"--state-dir={tmp_path}", "UserPromptSubmit"])
    assert "s3cr3t" not in (tmp_path / "log.jsonl").read_text(encoding="utf-8")
    assert log_records(tmp_path)[0]["message"] == "UserPromptSubmit: KeyError"


@pytest.mark.parametrize("value", ["", "cs-bad", "cs-ABCD2345", "x" * 300])
def test_hook_event_failure_ignores_a_malformed_session_id(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_hook(monkeypatch, RuntimeError("x"))
    monkeypatch.setenv("CSESSION_ID", value)
    entry.main(["hook-event", "--state-dir", str(tmp_path), "Stop"])
    [record] = log_records(tmp_path)
    validate(record, "claude-session/log.json")
    assert "csession_id" not in record


@pytest.mark.parametrize(
    "event", ["NotAnEvent", "x" * 300, "--state-dir", "Stop\nforged"], ids=str.__len__
)
def test_hook_event_failure_names_only_a_known_event(
    event: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_hook(monkeypatch, RuntimeError("x"))
    entry.main(["hook-event", "--state-dir", str(tmp_path), event])
    assert log_records(tmp_path)[0]["message"] == "RuntimeError"


@pytest.mark.parametrize(
    "argv",
    [
        ["Stop"],
        ["--state-dir", "relative/dir", "Stop"],
        ["--state-dir=", "Stop"],
        ["--state-dir"],
    ],
)
def test_hook_event_failure_without_an_absolute_state_dir_logs_nothing(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_hook(monkeypatch, RuntimeError("x"))
    monkeypatch.chdir(tmp_path)
    assert entry.main(["hook-event", *argv]) == 0
    assert list(tmp_path.rglob("*")) == []


def test_hook_event_failure_with_an_unwritable_log_still_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fail_hook(monkeypatch, RuntimeError("x"))
    (tmp_path / "log.jsonl").mkdir()
    assert entry.main(["hook-event", "--state-dir", str(tmp_path), "Stop"]) == 0
    assert capsys.readouterr() == ("", "")


def test_hook_event_success_writes_no_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hookevent, "main", lambda argv: 0)
    assert entry.main(["hook-event", "--state-dir", str(tmp_path), "Stop"]) == 0
    assert not (tmp_path / "log.jsonl").exists()


def test_hook_event_failure_is_logged_from_a_real_process(tmp_path: Path) -> None:
    code = HOOK_OUTPUT_THEN.format(output="pass", after="raise RuntimeError('x')").replace(
        "/nonexistent", str(tmp_path)
    )
    proc = run_python(code)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert log_records(tmp_path)[0]["event"] == "hook_event_failed"


def test_hook_event_always_exits_0() -> None:
    code = HOOK_OUTPUT_THEN.format(output="print('oops')", after="return 2")
    proc = run_python(code)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "oops\n", "")


def test_hook_event_in_process_return_value_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hookevent, "main", lambda argv: 2)
    assert entry.main(["hook-event", "--state-dir", "/nonexistent", "Stop"]) == 0


@pytest.mark.parametrize("after", ["raise RuntimeError('x')", "return 0"], ids=["fail", "ok"])
def test_hook_event_raw_fd_writes_never_escape(after: str) -> None:
    code = HOOK_OUTPUT_THEN.format(
        output="import os; os.write(1, b'raw out'); os.write(2, b'raw err')", after=after
    )
    proc = run_python(code)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_hook_event_after_a_raw_write_keeps_printed_output() -> None:
    code = HOOK_OUTPUT_THEN.format(
        output="import os; os.write(1, b'raw'); print('{}')", after="return 0"
    )
    proc = run_python(code)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "{}\n", "")


LOCKED_LOG = """
import fcntl, os, sys, time
from claude_session import __main__ as entry, fsutil, hookevent

fsutil.LOCK_TIMEOUT = 0.2
state_dir = sys.argv[1]
holder = os.open(os.path.join(state_dir, "log.jsonl"), os.O_RDONLY)
fcntl.flock(holder, fcntl.LOCK_EX)

def hook(argv):
    raise RuntimeError("x")

hookevent.main = hook
sys.exit(entry.main(["hook-event", "--state-dir", state_dir, "Stop"]))
"""


def test_hook_event_with_a_locked_log_exits_0_quickly(tmp_path: Path) -> None:
    (tmp_path / "log.jsonl").write_bytes(b"")
    (tmp_path / "log.jsonl").chmod(0o600)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", LOCKED_LOG, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert time.monotonic() - started < 30
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert (tmp_path / "log.jsonl").read_bytes() == b""


def test_hook_event_whose_saved_fds_are_closed_exits_0_silently() -> None:
    code = HOOK_OUTPUT_THEN.format(
        output="import os; print('{}'); os.closerange(3, 64)", after="return 0"
    )
    proc = run_python(code)
    assert (proc.returncode, proc.stderr) == (0, "")


def test_hook_event_writes_to_the_real_streams_never_escape() -> None:
    code = HOOK_OUTPUT_THEN.format(
        output=(
            "print('{\"ok\": true}'); sys.__stdout__.write('stray out'); "
            "sys.__stderr__.write('stray err')"
        ),
        after="return 0",
    )
    proc = run_python(code)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, '{"ok": true}\n', "")
