"""The private temporary directory (design.md, Session directories): ``/tmp/csession-<uid>/``
sits in a world-writable parent, so every test here assumes another local user is hostile."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from claude_session import fsutil, tmpdir
from claude_session.errors import LaunchValidationError
from claude_session.tmpdir import TmpdirUnsafe

SID = "cs-abcd2345"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """A stand-in for ``/tmp``: world-writable with the sticky bit, like the real one."""
    path = tmp_path / "tmp"
    path.mkdir()
    path.chmod(0o1777)
    return path


def root_of(base: Path) -> Path:
    return base / f"csession-{os.geteuid()}"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# Paths


def test_root_and_run_paths_name_the_effective_uid(base: Path) -> None:
    assert tmpdir.root_path(str(base)) == str(root_of(base))
    assert tmpdir.run_path(SID, str(base)) == str(root_of(base) / SID)


def test_paths_resolve_a_symlinked_base_first(tmp_path: Path, base: Path) -> None:
    # macOS: /tmp is a symlink to /private/tmp and is resolved before use.
    link = tmp_path / "tmp-link"
    link.symlink_to(base)
    assert tmpdir.root_path(str(link)) == str(root_of(base))
    created = tmpdir.create(SID, base=str(link))
    assert created == str(root_of(base) / SID)
    assert Path(created).is_dir()


@pytest.mark.parametrize(
    "bad", ["", ".", "..", "../cs-abcd2345", "cs-ABCD2345", "cs-abcd23", "x/y"]
)
def test_session_id_must_match_the_grammar(base: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        tmpdir.run_path(bad, str(base))
    with pytest.raises(ValueError):
        tmpdir.create(bad, base=str(base))
    with pytest.raises(ValueError):
        tmpdir.remove(bad, base=str(base))
    assert not root_of(base).exists()


def test_relative_base_is_refused() -> None:
    with pytest.raises(ValueError):
        tmpdir.root_path("tmp")


# create


def test_create_makes_root_and_run_dir_0700(base: Path) -> None:
    created = tmpdir.create(SID, base=str(base))
    assert created == str(root_of(base) / SID)
    assert mode(root_of(base)) == 0o700
    assert mode(Path(created)) == 0o700
    assert root_of(base).lstat().st_uid == os.geteuid()


@pytest.mark.parametrize("umask", [0o777, 0o277, 0o022, 0o000])
def test_create_modes_do_not_depend_on_the_umask(base: Path, umask: int) -> None:
    old = os.umask(umask)
    try:
        created = tmpdir.create(SID, base=str(base))
    finally:
        os.umask(old)
    assert mode(root_of(base)) == 0o700
    assert mode(Path(created)) == 0o700


def test_create_reuses_a_safe_existing_root(base: Path) -> None:
    root = root_of(base)
    root.mkdir(mode=0o700)
    (root / "cs-other222").mkdir(mode=0o700)
    created = tmpdir.create(SID, base=str(base))
    assert Path(created).is_dir()
    assert (root / "cs-other222").is_dir()  # other sessions' tmpdirs are left alone


def test_create_replaces_a_leftover_run_dir_with_an_empty_one(base: Path) -> None:
    first = Path(tmpdir.create(SID, base=str(base)))
    (first / "stale.sock").write_text("x")
    (first / "sub").mkdir()
    (first / "sub" / "deep").write_text("x")
    second = Path(tmpdir.create(SID, base=str(base)))
    assert second == first
    assert list(second.iterdir()) == []
    assert mode(second) == 0o700


def test_create_never_follows_symlinks_in_a_leftover_run_dir(tmp_path: Path, base: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("keep")
    run = Path(tmpdir.create(SID, base=str(base)))
    (run / "link").symlink_to(victim)
    (run / "file-link").symlink_to(victim / "keep")
    tmpdir.create(SID, base=str(base))
    assert (victim / "keep").read_text() == "keep"


def test_create_replaces_a_run_dir_symlink_without_following_it(tmp_path: Path, base: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("keep")
    root = root_of(base)
    root.mkdir(mode=0o700)
    (root / SID).symlink_to(victim)
    created = Path(tmpdir.create(SID, base=str(base)))
    assert not created.is_symlink()
    assert created.is_dir()
    assert (victim / "keep").read_text() == "keep"


def test_create_replaces_a_run_dir_that_is_a_file(base: Path) -> None:
    root = root_of(base)
    root.mkdir(mode=0o700)
    (root / SID).write_text("squat")
    created = Path(tmpdir.create(SID, base=str(base)))
    assert created.is_dir()


def test_tmpdir_unsafe_is_a_launch_validation_error(base: Path) -> None:
    root_of(base).mkdir(mode=0o755)
    with pytest.raises(LaunchValidationError) as info:
        tmpdir.create(SID, base=str(base))
    assert info.value.end_reason == "tmpdir_unsafe"
    assert info.value.exit_code == 12
    assert info.value.hint is not None


# Squatted, symlinked or wrong roots: refused, never repaired or followed


def test_root_symlink_to_own_dir_is_refused(tmp_path: Path, base: Path) -> None:
    target = tmp_path / "attacker-chosen"
    target.mkdir(mode=0o700)
    root_of(base).symlink_to(target)
    with pytest.raises(TmpdirUnsafe):
        tmpdir.create(SID, base=str(base))
    assert list(target.iterdir()) == []


def test_dangling_root_symlink_is_refused_and_not_created(tmp_path: Path, base: Path) -> None:
    target = tmp_path / "would-be-created"
    root_of(base).symlink_to(target)
    with pytest.raises(TmpdirUnsafe):
        tmpdir.create(SID, base=str(base))
    assert not target.exists()


def test_root_that_is_a_regular_file_is_refused(base: Path) -> None:
    root_of(base).write_text("squat")
    with pytest.raises(TmpdirUnsafe):
        tmpdir.create(SID, base=str(base))


def test_root_that_is_a_fifo_is_refused_without_blocking(base: Path) -> None:
    os.mkfifo(root_of(base))
    with pytest.raises(TmpdirUnsafe):
        tmpdir.create(SID, base=str(base))


@pytest.mark.parametrize("bad_mode", [0o755, 0o750, 0o711, 0o770, 0o777, 0o1700, 0o2700, 0o500])
def test_root_with_another_mode_is_refused_and_not_repaired(base: Path, bad_mode: int) -> None:
    root = root_of(base)
    root.mkdir()
    root.chmod(bad_mode)
    try:
        with pytest.raises(TmpdirUnsafe, match="mode"):
            tmpdir.create(SID, base=str(base))
        assert mode(root) == bad_mode
        assert not (root / SID).exists()
    finally:
        root.chmod(0o700)


def test_root_owned_by_another_user_is_refused(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_uid = os.geteuid()
    (base / "csession-4242").mkdir(mode=0o700)  # owned by real_uid, "another user" below
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    # The base stand-in is owned by real_uid, which is neither root nor 4242: it is refused
    # first. Make its owner check pass so the root's owner check is what fails.
    monkeypatch.setattr(tmpdir, "_TRUSTED_BASE_OWNERS", (0, real_uid))
    with pytest.raises(TmpdirUnsafe, match="owned by uid"):
        tmpdir.create(SID, base=str(base))


def test_squatted_root_created_between_mkdir_and_open_is_refused(
    tmp_path: Path, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another user wins the race: the name exists (as a symlink) when we try to create it.
    target = tmp_path / "attacker-chosen"
    target.mkdir(mode=0o700)
    real_mkdir = os.mkdir

    def racing_mkdir(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if path == f"csession-{os.geteuid()}":
            os.symlink(str(target), path, dir_fd=dir_fd)
            raise FileExistsError(path)
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    with pytest.raises(TmpdirUnsafe):
        tmpdir.create(SID, base=str(base))
    assert list(target.iterdir()) == []


# The base (/tmp)


def test_world_writable_base_without_sticky_bit_is_refused(base: Path) -> None:
    base.chmod(0o777)
    with pytest.raises(TmpdirUnsafe, match="sticky"):
        tmpdir.create(SID, base=str(base))
    assert not root_of(base).exists()


def test_group_writable_base_without_sticky_bit_is_refused(base: Path) -> None:
    base.chmod(0o770)
    with pytest.raises(TmpdirUnsafe, match="sticky"):
        tmpdir.create(SID, base=str(base))


def test_private_base_without_sticky_bit_is_accepted(base: Path) -> None:
    base.chmod(0o700)
    assert Path(tmpdir.create(SID, base=str(base))).is_dir()


def test_base_owned_by_another_user_is_refused(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    with pytest.raises(TmpdirUnsafe, match="owned by uid"):
        tmpdir.create(SID, base=str(base))


def test_missing_base_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TmpdirUnsafe):
        tmpdir.create(SID, base=str(tmp_path / "missing"))


def test_base_that_is_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    with pytest.raises(TmpdirUnsafe):
        tmpdir.create(SID, base=str(tmp_path / "file"))


# check: the same validation, with no side effects (dry run)


def test_check_creates_nothing(base: Path) -> None:
    assert tmpdir.check(SID, base=str(base)) == str(root_of(base) / SID)
    assert not root_of(base).exists()


def test_check_without_a_session_id_creates_nothing(base: Path) -> None:
    tmpdir.check(None, base=str(base))
    assert not root_of(base).exists()


def test_check_refuses_an_unsafe_existing_root(base: Path) -> None:
    root_of(base).mkdir(mode=0o755)
    with pytest.raises(TmpdirUnsafe):
        tmpdir.check(SID, base=str(base))


def test_check_refuses_an_unsafe_base(base: Path) -> None:
    base.chmod(0o777)
    with pytest.raises(TmpdirUnsafe):
        tmpdir.check(SID, base=str(base))


def test_check_does_not_touch_an_existing_run_dir(base: Path) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    (run / "x").write_text("x")
    tmpdir.check(SID, base=str(base))
    assert (run / "x").read_text() == "x"


# remove


def test_remove_deletes_the_run_dir_only(base: Path) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    other = Path(tmpdir.create("cs-other222", base=str(base)))
    (run / "sub").mkdir()
    (run / "sub" / "f").write_text("x")
    assert tmpdir.remove(SID, base=str(base)) is True
    assert not run.exists()
    assert other.is_dir()
    assert root_of(base).is_dir()


def test_remove_is_idempotent(base: Path) -> None:
    assert tmpdir.remove(SID, base=str(base)) is False  # no root
    assert not root_of(base).exists()
    tmpdir.create(SID, base=str(base))
    assert tmpdir.remove(SID, base=str(base)) is True
    assert tmpdir.remove(SID, base=str(base)) is False


def test_remove_never_follows_symlinks(tmp_path: Path, base: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("keep")
    run = Path(tmpdir.create(SID, base=str(base)))
    (run / "dir-link").symlink_to(victim)
    (run / "file-link").symlink_to(victim / "keep")
    tmpdir.remove(SID, base=str(base))
    assert (victim / "keep").read_text() == "keep"


def test_remove_of_a_run_dir_symlink_unlinks_only_the_link(tmp_path: Path, base: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("keep")
    root = root_of(base)
    root.mkdir(mode=0o700)
    (root / SID).symlink_to(victim)
    assert tmpdir.remove(SID, base=str(base)) is True
    assert not (root / SID).is_symlink()
    assert (victim / "keep").read_text() == "keep"


def test_remove_repairs_unreadable_subdirectories(base: Path) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    locked = run / "locked"
    locked.mkdir()
    (locked / "f").write_text("x")
    locked.chmod(0o000)
    assert tmpdir.remove(SID, base=str(base)) is True
    assert not run.exists()


def test_remove_refuses_a_symlinked_root(tmp_path: Path, base: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (target / SID).mkdir()
    root_of(base).symlink_to(target)
    with pytest.raises(fsutil.UnsafePathError):
        tmpdir.remove(SID, base=str(base))
    assert (target / SID).is_dir()


def test_remove_refuses_a_root_with_a_wrong_mode(base: Path) -> None:
    root = root_of(base)
    root.mkdir(mode=0o700)
    (root / SID).mkdir()
    root.chmod(0o777)
    try:
        with pytest.raises(fsutil.UnsafePathError, match="mode"):
            tmpdir.remove(SID, base=str(base))
        assert (root / SID).is_dir()
    finally:
        root.chmod(0o700)
