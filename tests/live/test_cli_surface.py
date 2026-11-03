"""Zero-cost checks of the ``claude`` binary itself: no API call, no login needed."""

from __future__ import annotations

import pytest

from claude_session import claude_version, defaults

from .harness import LiveEnv

# The options the launch argv, the fake claude's strict list and the live tests pass.
FLAGS_USED = (
    "--settings",
    "--setting-sources",
    "--model",
    "--add-dir",
    "--plugin-dir",
    "--system-prompt-snapshot",
    "--resume",
    "--session-id",
    "--output-format",
    "--fork-session",
    "--strict-mcp-config",
    "--verbose",
    "--print",
    "--permission-mode",
    "--allowedTools",
    "--max-budget-usd",
    "--debug",
    "--mcp-config",
    "--tools",
)


@pytest.mark.live
def test_claude_cli_reports_a_version_at_least_the_minimum(cheap: LiveEnv) -> None:
    proc = cheap.claude_run(["--version"], auth=False, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert claude_version.at_least(proc.stdout, defaults.BUILTIN["min_claude_version"]) is True
    cheap.observe("claude --version", proc.stdout.strip())


@pytest.mark.live
@pytest.mark.parametrize("flag", FLAGS_USED)
def test_help_lists_the_options_the_launch_argv_uses(cheap: LiveEnv, flag: str) -> None:
    proc = cheap.claude_run(["--help"], auth=False, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert flag in proc.stdout
