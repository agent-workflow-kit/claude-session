"""Live checks against the real Claude Code CLI (`make live`; never in CI)."""

from __future__ import annotations

import shutil

import pytest

from claude_session import gateway


@pytest.mark.live
def test_claude_cli_reports_a_version() -> None:
    claude = shutil.which("claude")
    assert claude is not None, "claude is not on PATH"
    proc = gateway.run([claude, "--version"], timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()
