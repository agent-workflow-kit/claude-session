from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from claude_session import fsutil
from claude_session.fsutil import write_private_file


def test_mode_is_0600_even_under_a_restrictive_umask(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    old_umask = os.umask(0o777)
    try:
        write_private_file(str(target), b"data\n")
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_bytes() == b"data\n"


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


def test_symlinked_parent_set_up_beforehand_works(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    write_private_file(str(link / "out.json"), b"x")
    assert (real / "out.json").read_bytes() == b"x"


def test_symlink_earlier_in_the_path_is_resolved_before_the_walk(tmp_path: Path) -> None:
    (tmp_path / "real" / "sub").mkdir(parents=True)
    (tmp_path / "linkdir").symlink_to(tmp_path / "real")
    write_private_file(str(tmp_path / "linkdir" / "sub" / "out.json"), b"x")
    assert (tmp_path / "real" / "sub" / "out.json").read_bytes() == b"x"


def test_component_swapped_to_a_symlink_after_resolution_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "real" / "sub").mkdir(parents=True)
    swapped = tmp_path / "swapped"
    swapped.symlink_to(tmp_path / "real")
    # realpath ran while `swapped` was still a directory; it is a symlink by the walk.
    monkeypatch.setattr(fsutil.os.path, "realpath", lambda path: path)
    with pytest.raises(OSError):
        write_private_file(str(swapped / "sub" / "out.json"), b"x")
    assert list((tmp_path / "real" / "sub").iterdir()) == []


def test_long_final_name(tmp_path: Path) -> None:
    name = "n" * 250
    write_private_file(str(tmp_path / name), b"x")
    assert (tmp_path / name).read_bytes() == b"x"


def test_file_and_directory_are_fsynced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[str] = []
    real_fsync = os.fsync

    def record(fd: int) -> None:
        synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(fsutil.os, "fsync", record)
    write_private_file(str(tmp_path / "out.json"), b"x")
    assert synced == ["file", "dir"]


def test_failure_leaves_no_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "taken"
    target.mkdir()
    (target / "child").write_bytes(b"x")
    with pytest.raises(OSError):
        write_private_file(str(target), b"x")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["taken"]


def test_cleanup_error_does_not_mask_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "taken"
    target.mkdir()
    (target / "child").write_bytes(b"x")

    def failing_unlink(*args: object, **kwargs: object) -> None:
        raise PermissionError("cleanup failed")

    monkeypatch.setattr(fsutil.os, "unlink", failing_unlink)
    with pytest.raises(OSError) as exc:
        write_private_file(str(target), b"x")
    assert not isinstance(exc.value, PermissionError)


def test_requires_an_absolute_path() -> None:
    with pytest.raises(ValueError):
        write_private_file("relative.json", b"x")


# append_private_line


def test_append_creates_a_0600_file_under_a_restrictive_umask(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    old_umask = os.umask(0o777)
    try:
        fsutil.append_private_line(str(target), b"one\n")
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_bytes() == b"one\n"


def test_append_keeps_existing_lines_and_tightens_the_mode(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_bytes(b"one\n")
    target.chmod(0o644)
    fsutil.append_private_line(str(target), b"two\n")
    assert target.read_bytes() == b"one\ntwo\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_append_uses_a_single_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[bytes] = []
    real_write = os.write

    def record(fd: int, data: bytes) -> int:
        writes.append(bytes(data))
        return real_write(fd, data)

    monkeypatch.setattr(fsutil.os, "write", record)
    fsutil.append_private_line(str(tmp_path / "log.jsonl"), b"x" * 100_000 + b"\n")
    assert len(writes) == 1


def test_append_short_write_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write
    monkeypatch.setattr(fsutil.os, "write", lambda fd, data: real_write(fd, data[:1]))
    with pytest.raises(OSError, match="short write"):
        fsutil.append_private_line(str(tmp_path / "log.jsonl"), b"abc\n")


def test_append_refuses_a_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    target = tmp_path / "log.jsonl"
    target.symlink_to(victim)
    with pytest.raises(OSError):
        fsutil.append_private_line(str(target), b"x\n")
    assert victim.read_bytes() == b"untouched"


def test_append_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    os.mkfifo(target, 0o600)
    with pytest.raises(OSError):
        fsutil.append_private_line(str(target), b"x\n")


def test_append_refuses_a_directory(tmp_path: Path) -> None:
    (tmp_path / "log.jsonl").mkdir()
    with pytest.raises(OSError):
        fsutil.append_private_line(str(tmp_path / "log.jsonl"), b"x\n")


def test_append_does_not_create_the_parent(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fsutil.append_private_line(str(tmp_path / "missing" / "log.jsonl"), b"x\n")


def test_append_requires_a_newline_terminated_line(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        fsutil.append_private_line(str(tmp_path / "log.jsonl"), b"x")
    with pytest.raises(ValueError):
        fsutil.append_private_line(str(tmp_path / "log.jsonl"), b"x\ny\n")


def test_append_requires_an_absolute_path() -> None:
    with pytest.raises(ValueError):
        fsutil.append_private_line("relative/log.jsonl", b"x\n")
