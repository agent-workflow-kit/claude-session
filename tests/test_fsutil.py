from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from claude_session.fsutil import write_private_file


def test_writes_content_with_mode_0600(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    old_umask = os.umask(0)
    try:
        write_private_file(str(target), b"data\n")
    finally:
        os.umask(old_umask)
    assert target.read_bytes() == b"data\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    target.write_bytes(b"old")
    target.chmod(0o644)
    write_private_file(str(target), b"new")
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_replaces_a_symlink_instead_of_following_it(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    target = tmp_path / "out.json"
    target.symlink_to(victim)
    write_private_file(str(target), b"new")
    assert not target.is_symlink()
    assert target.read_bytes() == b"new"
    assert victim.read_bytes() == b"untouched"


def test_refuses_a_symlinked_parent_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(OSError):
        write_private_file(str(link / "out.json"), b"x")
    assert list(real.iterdir()) == []


def test_failure_leaves_no_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "taken"
    target.mkdir()
    (target / "child").write_bytes(b"x")
    with pytest.raises(OSError):
        write_private_file(str(target), b"x")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["taken"]


def test_requires_an_absolute_path() -> None:
    with pytest.raises(ValueError):
        write_private_file("relative.json", b"x")
