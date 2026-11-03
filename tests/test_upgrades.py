"""Upgrade warnings: a live run's recorded hook interpreter path that no longer exists
(interface.md, Upgrades)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from claude_session import settings_builder, upgrades


def interpreter(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_the_current_interpreter_gets_no_warning() -> None:
    assert upgrades.hook_interpreter_warning(settings_builder.current_interpreter()) is None
    assert upgrades.hook_interpreter_warning(sys.executable) is None


def test_nothing_recorded_gets_no_warning() -> None:
    assert upgrades.hook_interpreter_warning(None) is None


def test_a_removed_interpreter_is_warned_about_by_path(tmp_path: Path) -> None:
    path = tmp_path / "venv" / "bin" / "python"
    warning = upgrades.hook_interpreter_warning(str(path))
    assert warning is not None
    assert str(path) in warning and "no longer exists" in warning


def test_a_venv_python_whose_base_interpreter_is_gone_is_missing(tmp_path: Path) -> None:
    """A venv's ``bin/python`` is a symlink to the base interpreter: the recorded path
    exists as a link, but the hooks cannot run."""
    base = interpreter(tmp_path / "base" / "python3")
    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(base)
    assert upgrades.hook_interpreter_warning(str(link)) is None
    base.unlink()
    assert upgrades.hook_interpreter_warning(str(link)) is not None


@pytest.mark.skipif(os.geteuid() == 0, reason="root may execute any file")
def test_an_interpreter_that_is_no_longer_executable_is_missing(tmp_path: Path) -> None:
    path = interpreter(tmp_path / "python")
    path.chmod(0o644)
    assert upgrades.hook_interpreter_missing(str(path))


def test_a_directory_is_missing(tmp_path: Path) -> None:
    assert upgrades.hook_interpreter_missing(str(tmp_path))


@pytest.mark.parametrize("path", ["", "python3", "bin/python", "/usr/bin/py\x00thon"])
def test_a_path_a_launch_never_records_counts_as_missing(path: str) -> None:
    assert upgrades.hook_interpreter_missing(path)
    warning = upgrades.hook_interpreter_warning(path)
    assert warning is not None and "\x00" not in warning


def test_the_warning_escapes_control_characters(tmp_path: Path) -> None:
    path = str(tmp_path / "gone\x1b[31m\n")
    warning = upgrades.hook_interpreter_warning(path)
    assert warning is not None
    assert "\x1b" not in warning and "\n" not in warning
