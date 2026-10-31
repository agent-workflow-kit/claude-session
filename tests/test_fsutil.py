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
    write_private_file(os.path.join(str(tmp_path), name), b"x")
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


# Descriptor-relative helpers (design.md, Session directories: writes relative to directory
# descriptors, subdirectories verified before use)


@pytest.fixture
def dir_fd(tmp_path: Path):  # type: ignore[no-untyped-def]
    fd = fsutil.open_dir_nofollow(str(tmp_path))
    yield fd
    os.close(fd)


def test_write_at_writes_relative_to_the_descriptor(tmp_path: Path, dir_fd: int) -> None:
    old_umask = os.umask(0o777)
    try:
        fsutil.write_private_file_at(dir_fd, "f", b"data")
    finally:
        os.umask(old_umask)
    assert (tmp_path / "f").read_bytes() == b"data"
    assert stat.S_IMODE((tmp_path / "f").stat().st_mode) == 0o600


def test_write_at_follows_the_pinned_directory_after_a_rename(tmp_path: Path, dir_fd: int) -> None:
    moved = tmp_path.parent / (tmp_path.name + "-moved")
    tmp_path.rename(moved)
    try:
        tmp_path.mkdir()  # a new directory at the old path is never written
        fsutil.write_private_file_at(dir_fd, "f", b"x")
        assert (moved / "f").read_bytes() == b"x"
        assert list(tmp_path.iterdir()) == []
    finally:
        tmp_path.rmdir()
        moved.rename(tmp_path)


def test_write_at_replaces_a_planted_symlink(tmp_path: Path, dir_fd: int) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"untouched")
    (tmp_path / "f").symlink_to(victim)
    fsutil.write_private_file_at(dir_fd, "f", b"new")
    assert not (tmp_path / "f").is_symlink()
    assert victim.read_bytes() == b"untouched"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "/abs", "a\0b"])
def test_write_at_refuses_names_that_are_not_one_segment(dir_fd: int, name: str) -> None:
    with pytest.raises(ValueError):
        fsutil.write_private_file_at(dir_fd, name, b"x")


def test_ensure_dir_creates_0700_whatever_the_umask(tmp_path: Path, dir_fd: int) -> None:
    old_umask = os.umask(0o777)
    try:
        fd = fsutil.ensure_private_dir_at(dir_fd, "sub")
    finally:
        os.umask(old_umask)
    os.close(fd)
    assert stat.S_IMODE((tmp_path / "sub").stat().st_mode) == 0o700


def test_ensure_dir_tightens_an_existing_loose_directory(tmp_path: Path, dir_fd: int) -> None:
    (tmp_path / "sub").mkdir(mode=0o755)
    (tmp_path / "sub").chmod(0o777)
    os.close(fsutil.ensure_private_dir_at(dir_fd, "sub"))
    assert stat.S_IMODE((tmp_path / "sub").stat().st_mode) == 0o700


def test_ensure_dir_refuses_a_symlink_to_a_directory(tmp_path: Path, dir_fd: int) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "sub").symlink_to(tmp_path / "real")
    with pytest.raises(OSError):
        fsutil.ensure_private_dir_at(dir_fd, "sub")


def test_ensure_dir_refuses_a_dangling_symlink_without_creating_its_target(
    tmp_path: Path, dir_fd: int
) -> None:
    (tmp_path / "sub").symlink_to(tmp_path / "target")
    with pytest.raises(OSError):
        fsutil.ensure_private_dir_at(dir_fd, "sub")
    assert not (tmp_path / "target").exists()


def test_ensure_dir_refuses_a_regular_file(tmp_path: Path, dir_fd: int) -> None:
    (tmp_path / "sub").write_bytes(b"x")
    with pytest.raises(OSError):
        fsutil.ensure_private_dir_at(dir_fd, "sub")


def test_ensure_dir_refuses_a_fifo_without_blocking(tmp_path: Path, dir_fd: int) -> None:
    os.mkfifo(tmp_path / "sub")
    with pytest.raises(OSError):
        fsutil.ensure_private_dir_at(dir_fd, "sub")


def test_check_private_dir_refuses_another_owner(
    dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fsutil.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(fsutil.UnsafePathError, match="owned by uid"):
        fsutil.check_private_dir_fd(dir_fd, "x")


def test_open_dir_at_does_not_create(tmp_path: Path, dir_fd: int) -> None:
    with pytest.raises(FileNotFoundError):
        fsutil.open_private_dir_at(dir_fd, "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b"])
def test_ensure_dir_refuses_names_that_are_not_one_segment(dir_fd: int, name: str) -> None:
    with pytest.raises(ValueError):
        fsutil.ensure_private_dir_at(dir_fd, name)


# Temporary files staged in another directory (review R1-1)


def test_write_at_stages_the_temporary_file_in_tmp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "target").mkdir()
    (tmp_path / "private").mkdir()
    target_fd = fsutil.open_dir_nofollow(str(tmp_path / "target"))
    tmp_fd = fsutil.open_dir_nofollow(str(tmp_path / "private"))
    seen: list[list[str]] = []
    real_rename = os.rename

    def spy(*args: object, **kwargs: object) -> None:
        seen.append(sorted(os.listdir(tmp_path / "target")))
        real_rename(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fsutil.os, "rename", spy)
    try:
        fsutil.write_private_file_at(target_fd, "f", b"data", tmp_dir_fd=tmp_fd)
    finally:
        os.close(target_fd)
        os.close(tmp_fd)
    assert seen == [[]]
    assert (tmp_path / "target" / "f").read_bytes() == b"data"
    assert list((tmp_path / "private").iterdir()) == []


def test_write_at_failure_removes_the_temporary_file_from_tmp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "target").mkdir()
    (tmp_path / "private").mkdir()
    target_fd = fsutil.open_dir_nofollow(str(tmp_path / "target"))
    tmp_fd = fsutil.open_dir_nofollow(str(tmp_path / "private"))

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(fsutil.os, "rename", boom)
    try:
        with pytest.raises(OSError, match="rename failed"):
            fsutil.write_private_file_at(target_fd, "f", b"data", tmp_dir_fd=tmp_fd)
    finally:
        os.close(target_fd)
        os.close(tmp_fd)
    assert list((tmp_path / "private").iterdir()) == []
    assert list((tmp_path / "target").iterdir()) == []


# Mode repair never follows a symlink (review R1-5, R1-6)


def test_ensure_dir_mode_repair_never_chmods_a_swapped_in_symlink(
    tmp_path: Path, dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"x")
    victim.chmod(0o644)
    real_open = os.open
    swapped: list[bool] = []

    def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        # The first read open of the new directory: swap it for a symlink to the victim and
        # report the permission error a umask without owner bits causes.
        if path == "sub" and not swapped and not flags & getattr(os, "O_PATH", 0):
            swapped.append(True)
            (tmp_path / "sub").rmdir()
            (tmp_path / "sub").symlink_to(victim)
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fsutil.os, "open", racing_open)
    with pytest.raises(OSError):
        fsutil.ensure_private_dir_at(dir_fd, "sub")
    assert swapped
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644


def test_ensure_dir_under_umask_0o477_gives_0700(tmp_path: Path, dir_fd: int) -> None:
    old_umask = os.umask(0o477)
    try:
        fd = fsutil.ensure_private_dir_at(dir_fd, "sub")
    finally:
        os.umask(old_umask)
    os.close(fd)
    assert stat.S_IMODE((tmp_path / "sub").stat().st_mode) == 0o700


def test_open_dir_at_repairs_an_owned_mode_000_directory(tmp_path: Path, dir_fd: int) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub").chmod(0o000)
    os.close(fsutil.open_private_dir_at(dir_fd, "sub"))
    assert stat.S_IMODE((tmp_path / "sub").stat().st_mode) == 0o700


def test_create_dir_at_refuses_an_existing_entry(tmp_path: Path, dir_fd: int) -> None:
    (tmp_path / "sub").mkdir()
    with pytest.raises(FileExistsError):
        fsutil.create_private_dir_at(dir_fd, "sub")


def test_create_dir_at_under_umask_0o477_gives_0700(tmp_path: Path, dir_fd: int) -> None:
    old_umask = os.umask(0o477)
    try:
        fd = fsutil.create_private_dir_at(dir_fd, "sub")
    finally:
        os.umask(old_umask)
    os.close(fd)
    assert stat.S_IMODE((tmp_path / "sub").stat().st_mode) == 0o700


@pytest.mark.parametrize("name", [".", ".."])
def test_write_private_file_dot_names_are_a_directory_error(tmp_path: Path, name: str) -> None:
    with pytest.raises(IsADirectoryError):
        write_private_file(os.path.join(str(tmp_path), name), b"x")


# remove_tree_at


def test_remove_tree_at_removes_a_nested_tree(tmp_path: Path, dir_fd: int) -> None:
    tree = tmp_path / "t"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "f").write_text("x")
    (tree / "g").write_text("y")
    (tree / "locked").mkdir(mode=0o000)
    (tree / "a" / "ro").mkdir()
    (tree / "a" / "ro" / "f").write_text("z")
    (tree / "a" / "ro").chmod(0o500)
    fsutil.remove_tree_at(dir_fd, "t")
    assert not tree.exists()


def test_remove_tree_at_unlinks_symlinks_without_following_them(
    tmp_path: Path, dir_fd: int
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "link").symlink_to(outside)
    (tree / "dangling").symlink_to(tmp_path / "missing")
    fsutil.remove_tree_at(dir_fd, "t")
    assert not tree.exists()
    assert (outside / "keep").read_text() == "keep"


def test_remove_tree_at_removes_a_top_level_symlink_only(tmp_path: Path, dir_fd: int) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (tmp_path / "t").symlink_to(outside)
    fsutil.remove_tree_at(dir_fd, "t")
    assert not os.path.lexists(tmp_path / "t")
    assert (outside / "keep").read_text() == "keep"


def test_remove_tree_at_removes_a_file_and_a_fifo(tmp_path: Path, dir_fd: int) -> None:
    (tmp_path / "f").write_text("x")
    os.mkfifo(tmp_path / "p")
    fsutil.remove_tree_at(dir_fd, "f")
    fsutil.remove_tree_at(dir_fd, "p")
    assert not (tmp_path / "f").exists() and not os.path.lexists(tmp_path / "p")


def test_remove_tree_at_refuses_a_tree_deeper_than_the_limit(
    tmp_path: Path, dir_fd: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fsutil, "MAX_TREE_DEPTH", 3)
    (tmp_path / "t" / "1" / "2" / "3" / "4").mkdir(parents=True)
    with pytest.raises(OSError, match="deeper than"):
        fsutil.remove_tree_at(dir_fd, "t")


@pytest.mark.parametrize("name", ["", ".", "..", "a/b"])
def test_remove_tree_at_refuses_names_that_are_not_one_segment(dir_fd: int, name: str) -> None:
    with pytest.raises(ValueError):
        fsutil.remove_tree_at(dir_fd, name)
