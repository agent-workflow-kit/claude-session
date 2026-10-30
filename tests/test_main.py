"""The entry point: hook-event dispatch and containment."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from claude_session import __main__ as entry
from claude_session import cli, hookevent


@pytest.mark.parametrize("error", [RuntimeError("boom"), SystemExit(2), KeyError("x")])
def test_hook_event_errors_exit_0_silently(
    error: BaseException, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(argv: list[str]) -> int:
        raise error

    silenced: list[bool] = []
    monkeypatch.setattr(hookevent, "main", broken)
    # Redirecting fds 1 and 2 would break pytest; the subprocess tests below cover it.
    monkeypatch.setattr(entry, "_silence_output", lambda: silenced.append(True))
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


def test_hook_event_accepts_yes() -> None:
    """Placeholder until #13 parses hook-event's argv: the stub ignores its arguments."""
    assert entry.main(["hook-event", "--state-dir", "/nonexistent", "Stop", "--yes"]) == 0


def test_other_commands_go_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(cli.HANDLERS, "kinds", lambda args: 77)
    assert entry.main(["kinds"]) == 77


def test_console_script_uses_the_light_entry_point() -> None:
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"claude-session": "claude_session.__main__:main"}


def run_python(code: str, *, close_stdout_pipe: bool = False) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter; optionally with a stdout pipe nobody reads."""
    if not close_stdout_pipe:
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        return subprocess.run(
            [sys.executable, "-c", code],
            stdout=write_end,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        os.close(write_end)


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
    code = (
        "import sys\n"
        "from claude_session.__main__ import main\n"
        "sys.exit(main(['exec', '--timeout', '0', '--json']))\n"
    )
    proc = run_python(code, close_stdout_pipe=True)
    assert proc.returncode == 2
    assert "Exception ignored" not in proc.stderr
    assert "BrokenPipeError" not in proc.stderr
