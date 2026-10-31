"""Live checks against the real Claude Code CLI (`make live`; never in CI)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from claude_session import gateway


@pytest.mark.live
def test_claude_cli_reports_a_version() -> None:
    claude = shutil.which("claude")
    assert claude is not None, "claude is not on PATH"
    proc = gateway.SystemGateway().run([claude, "--version"], timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()


@pytest.mark.live
def test_headless_stream_json_ends_its_last_line_with_a_newline(tmp_path: Path) -> None:
    """The headless parser reads a tail without its newline as truncated (interface.md,
    Headless output), so Claude Code must end its last ``stream-json`` line with one."""
    claude = shutil.which("claude")
    assert claude is not None, "claude is not on PATH"
    argv = [claude, "-p", "--output-format", "stream-json", "--verbose", "Reply with OK."]
    proc = gateway.SystemGateway().run(argv, cwd=str(tmp_path), stdin="", timeout=300)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.endswith("\n")
    last = json.loads(proc.stdout.splitlines()[-1])
    assert last["type"] == "result"
