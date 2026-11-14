"""A sandboxed run launches ``claude`` with per-run copies of its plugin directories that hold
the sandbox's mask placeholders (interface.md, Isolation, Plugin directories); a run without a
sandbox uses the directories as given. End to end through the fake ``claude``."""

from __future__ import annotations

from pathlib import Path

import pytest

from .test_exec_headless import FAST, START, STOP, Run, read_jsonl, result_step, run_exec

PLACEHOLDERS = [".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md"]


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    src = tmp_path / "plugins" / "kit"
    (src / "skills" / "s").mkdir(parents=True)
    (src / "skills" / "s" / "SKILL.md").write_text("skill\n")
    return src


def plugin_flags(log: Path) -> tuple[list[str], list[str]]:
    (start,) = [r for r in read_jsonl(log) if r.get("event") == "start"]
    flags = start["flags"]
    plugins = flags["--plugin-dir"]
    return ([plugins] if isinstance(plugins, str) else plugins), flags["--add-dir"]


def launch(tmp_path: Path, state: Path, work: Path, plugin: Path, *args: str) -> Run:
    return run_exec(
        tmp_path,
        state,
        work,
        [START, STOP, result_step()],
        args=["--plugin-dir", str(plugin), *args],
        config=FAST,
        env={"PATH": "/usr/bin:/bin"},
    )


def test_a_sandboxed_run_uses_a_copy_with_the_placeholders(
    tmp_path: Path, state: Path, work: Path, plugin: Path
) -> None:
    run = launch(tmp_path, state, work, plugin, "--sandbox", "preferred")
    assert run.code == 0, run.proc.stderr
    copy = state / "sessions" / run.sid / "plugins" / "1" / "0" / "kit"
    plugins, add_dirs = plugin_flags(run.log)
    assert plugins == [str(copy)]
    assert str(copy) in add_dirs and str(plugin) not in add_dirs
    assert (copy / "skills" / "s" / "SKILL.md").read_text() == "skill\n"
    for name in PLACEHOLDERS:
        assert (copy / name).exists(), name
    assert sorted(p.name for p in plugin.iterdir()) == ["skills"]  # the source is untouched


def test_the_generated_settings_name_only_the_copy(
    tmp_path: Path, state: Path, work: Path, plugin: Path
) -> None:
    run = launch(tmp_path, state, work, plugin, "--sandbox", "preferred")
    assert run.code == 0, run.proc.stderr
    copy = state / "sessions" / run.sid / "plugins" / "1" / "0" / "kit"
    text = (run.private_run / "settings.json").read_text()
    assert str(copy) in text
    assert str(plugin) not in text


def test_a_plugin_that_ships_the_placeholders_keeps_them(
    tmp_path: Path, state: Path, work: Path, plugin: Path
) -> None:
    (plugin / ".claude").mkdir()
    (plugin / ".mcp.json").write_text('{"mcpServers": {"x": {}}}')
    (plugin / "CLAUDE.md").write_text("mine\n")
    (plugin / "CLAUDE.local.md").write_text("")
    run = launch(tmp_path, state, work, plugin, "--sandbox", "preferred")
    assert run.code == 0, run.proc.stderr
    copy = state / "sessions" / run.sid / "plugins" / "1" / "0" / "kit"
    assert (copy / "CLAUDE.md").read_text() == "mine\n"
    assert (copy / ".mcp.json").read_text() == '{"mcpServers": {"x": {}}}'


def test_a_run_without_a_sandbox_uses_the_directory_as_given(
    tmp_path: Path, state: Path, work: Path, plugin: Path
) -> None:
    run = launch(tmp_path, state, work, plugin, "--sandbox", "off")
    assert run.code == 0, run.proc.stderr
    plugins, _ = plugin_flags(run.log)
    assert plugins == [str(plugin)]
    assert not (state / "sessions" / run.sid / "plugins").exists()
    assert not (plugin / "CLAUDE.md").exists()


def test_a_symlink_in_the_plugin_ends_the_run_not_started(
    tmp_path: Path, state: Path, work: Path, plugin: Path
) -> None:
    (plugin / "out").symlink_to(tmp_path)
    run = launch(tmp_path, state, work, plugin, "--sandbox", "preferred")
    assert run.code == 12
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == (
        "not-started",
        "unsafe_plugin_dir",
    )
    assert "out" in run.outcome["message"]
    assert not run.log.exists() or not read_jsonl(run.log)
