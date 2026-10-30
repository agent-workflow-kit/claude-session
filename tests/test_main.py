"""The entry point: hook-event dispatch and containment."""

from __future__ import annotations

import pytest

from claude_session import __main__ as entry
from claude_session import cli, hookevent


@pytest.mark.parametrize("error", [RuntimeError("boom"), SystemExit(2), KeyError("x")])
def test_hook_event_errors_exit_0_silently(
    error: BaseException, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(argv: list[str]) -> int:
        raise error

    monkeypatch.setattr(hookevent, "main", broken)
    assert entry.main(["hook-event", "--state-dir", "/nonexistent", "Stop"]) == 0
    assert capsys.readouterr() == ("", "")


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
