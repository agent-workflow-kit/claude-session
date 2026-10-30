from __future__ import annotations

import fcntl
import json
import os
import stat
import time
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


def test_append_after_a_torn_line_starts_a_new_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "log.jsonl"
    fsutil.append_private_line(str(target), b'{"a":1}\n')
    real_write = os.write
    with monkeypatch.context() as patch:
        patch.setattr(fsutil.os, "write", lambda fd, data: real_write(fd, data[:4]))
        with pytest.raises(OSError, match="short write"):
            fsutil.append_private_line(str(target), b'{"b":2}\n')
    fsutil.append_private_line(str(target), b'{"c":3}\n')
    assert target.read_bytes() == b'{"a":1}\n{"b"\n{"c":3}\n'
    assert json.loads(target.read_text(encoding="utf-8").splitlines()[-1]) == {"c": 3}


def test_append_to_a_file_ending_in_a_newline_adds_no_blank_line(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_bytes(b"one\n")
    target.chmod(0o600)
    fsutil.append_private_line(str(target), b"two\n")
    assert target.read_bytes() == b"one\ntwo\n"


def test_append_refuses_a_hard_linked_file(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_bytes(b"one\n")
    target.chmod(0o644)
    os.link(target, tmp_path / "other")
    with pytest.raises(OSError, match="links"):
        fsutil.append_private_line(str(target), b"two\n")
    assert target.read_bytes() == b"one\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_append_refuses_a_file_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "log.jsonl"
    target.write_bytes(b"one\n")
    target.chmod(0o644)
    monkeypatch.setattr(fsutil.os, "getuid", lambda: os.stat(target).st_uid + 1)
    with pytest.raises(OSError, match="another user"):
        fsutil.append_private_line(str(target), b"two\n")
    assert target.read_bytes() == b"one\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


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


def test_append_gives_up_on_a_held_lock_within_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "log.jsonl"
    fsutil.append_private_line(str(target), b"one\n")
    monkeypatch.setattr(fsutil, "LOCK_TIMEOUT", 0.2)
    # flock locks belong to the open file description: a second open conflicts in-process.
    holder = os.open(target, os.O_RDONLY)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(OSError, match="lock"):
            fsutil.append_private_line(str(target), b"two\n")
        assert time.monotonic() - started < 5
    finally:
        os.close(holder)
    assert target.read_bytes() == b"one\n"


def test_lock_deadline_is_short() -> None:
    assert 0 < fsutil.LOCK_TIMEOUT <= 2


def test_append_waits_for_a_lock_released_before_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "log.jsonl"
    fsutil.append_private_line(str(target), b"one\n")
    holder = os.open(target, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_EX)
    real_sleep = time.sleep

    def release_then_sleep(seconds: float) -> None:
        os.close(holder)  # the first retry finds the lock free
        monkeypatch.setattr(fsutil.time, "sleep", real_sleep)
        real_sleep(seconds)

    monkeypatch.setattr(fsutil.time, "sleep", release_then_sleep)
    fsutil.append_private_line(str(target), b"two\n")
    assert target.read_bytes() == b"one\ntwo\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can open a mode-0200 file for reading")
def test_append_to_a_write_only_file_tightens_it_and_appends(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_bytes(b"one\n")
    target.chmod(0o200)
    fsutil.append_private_line(str(target), b"two\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_bytes() == b"one\ntwo\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can open a mode-0200 file for reading")
def test_append_to_a_write_only_hard_linked_file_is_refused_untouched(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    target.write_bytes(b"one\n")
    target.chmod(0o200)
    os.link(target, tmp_path / "other")
    with pytest.raises(OSError, match="links"):
        fsutil.append_private_line(str(target), b"two\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o200
