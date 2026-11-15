"""A sandboxed run launches ``claude`` with per-run copies of its plugin directories that hold
the sandbox's mask placeholders (interface.md, Isolation, Plugin directories); a run without a
sandbox uses the directories as given. End to end through the fake ``claude``."""

from __future__ import annotations

import errno
import json
import os
import subprocess
from pathlib import Path

import pytest

from .test_exec_headless import (
    FAST,
    START,
    STOP,
    Run,
    claude_bin,
    claude_session_cmd,
    private_home,
    read_jsonl,
    result_step,
    run_exec,
    write_json,
)

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


def test_a_relative_symlink_inside_the_plugin_is_copied_as_a_link(
    tmp_path: Path, state: Path, work: Path, plugin: Path
) -> None:
    (plugin / "alias").symlink_to("skills/s")
    run = launch(tmp_path, state, work, plugin, "--sandbox", "preferred")
    assert run.code == 0, run.proc.stderr
    copy = state / "sessions" / run.sid / "plugins" / "1" / "0" / "kit"
    assert os.readlink(copy / "alias") == "skills/s"


@pytest.mark.parametrize("target", ["ABSOLUTE", "../../x"])
def test_an_absolute_or_escaping_symlink_ends_the_run_not_started(
    tmp_path: Path, state: Path, work: Path, plugin: Path, target: str
) -> None:
    (plugin / "out").symlink_to(str(tmp_path) if target == "ABSOLUTE" else target)
    run = launch(tmp_path, state, work, plugin, "--sandbox", "preferred")
    assert run.code == 12
    assert (run.outcome["outcome"], run.outcome["end_reason"]) == (
        "not-started",
        "unsafe_plugin_dir",
    )
    assert "out" in run.outcome["message"]
    assert not run.log.exists() or not read_jsonl(run.log)


def test_a_resume_without_copies_removes_the_earlier_runs_copies(
    tmp_path: Path, state: Path, work: Path, plugin: Path
) -> None:
    first = launch(tmp_path, state, work, plugin, "--sandbox", "preferred")
    assert first.code == 0, first.proc.stderr
    plugins = state / "sessions" / first.sid / "plugins"
    assert (plugins / "1").is_dir()
    prompt = tmp_path / "again.md"
    prompt.write_text("Again.\n")
    proc = subprocess.run(
        [
            *claude_session_cmd(),
            "resume",
            first.sid,
            "--json",
            "--config",
            str(tmp_path / "config.yaml"),
            "--sandbox",
            "off",
            "--prompt-file",
            str(prompt),
        ],
        cwd=work,
        env={**os.environ, **private_home(tmp_path), "PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not plugins.exists()


def test_a_copy_that_fails_ends_not_started_and_leaves_no_run_directory(
    tmp_path: Path,
    state: Path,
    work: Path,
    plugin: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from claude_session import cli, plugincopy

    def failing(*_args: object, **_kwargs: object) -> object:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(plugincopy, "copy", failing)
    log = tmp_path / "fake.jsonl"
    scenario = write_json(tmp_path / "scenario.json", {"steps": [START, STOP, result_step()]})
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {claude_bin(tmp_path / 'fakebin', scenario, log)}\n{FAST}")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Go.\n")
    monkeypatch.chdir(work)
    for key, value in {**private_home(tmp_path), "PATH": "/usr/bin:/bin"}.items():
        monkeypatch.setenv(key, value)
    capsys.readouterr()
    code = cli.main(
        ["exec", "--mode", "headless", "--json", "--config", str(cfg), "--prompt-file", str(prompt)]
        + ["--plugin-dir", str(plugin), "--sandbox", "preferred"]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 12 and out["outcome"] == "not-started"
    assert not list((state / "sessions").glob("*/plugins/*"))
    assert not log.exists() or not read_jsonl(log)
