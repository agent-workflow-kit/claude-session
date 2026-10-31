"""The private temporary directory (design.md, Session directories): ``/tmp/csession-<uid>/``
sits in a world-writable parent, so every test here assumes another local user is hostile."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from claude_session import fsutil, ids, pathrules, tmpdir
from claude_session.errors import LaunchValidationError
from claude_session.tmpdir import TmpdirLeftover, TmpdirUnsafe

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


def deep_tree(top: Path, levels: int) -> None:
    path = top
    for _ in range(levels):
        path = path / "d"
        path.mkdir()
    (path / "f").write_text("x")


def stale_entries(base: Path) -> list[Path]:
    return sorted(root_of(base).glob(f"{tmpdir.STALE_PREFIX}*"))


@pytest.fixture
def low_depth_cap(monkeypatch: pytest.MonkeyPatch) -> int:
    """``fsutil.remove_tree_at`` refuses trees deeper than 3 levels (instead of 256)."""
    monkeypatch.setattr(fsutil, "tree_depth_cap", lambda: 3)
    return 3


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
    root = base / "csession-4242"
    with pytest.raises(TmpdirUnsafe) as info:
        tmpdir.create(SID, base=str(base))
    assert str(info.value) == (
        f"private temporary directory unsafe: {root}: owned by uid {real_uid}"
    )
    assert info.value.hint is not None
    assert f"another user created {root}" in info.value.hint
    assert "administrator" in info.value.hint and "sandbox to off" in info.value.hint
    assert "rm -rf" not in info.value.hint


def test_squatted_root_names_its_owner_even_when_it_cannot_be_opened(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another user's 0700 root: opening it fails with EACCES, which would hide the owner.
    real_uid = os.geteuid()
    (base / "csession-4242").mkdir(mode=0o700)
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    monkeypatch.setattr(tmpdir, "_TRUSTED_BASE_OWNERS", (0, real_uid))
    real_open = os.open

    def denied_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == "csession-4242":
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", denied_open)
    for call in (tmpdir.check, tmpdir.create):
        with pytest.raises(TmpdirUnsafe, match=f"owned by uid {real_uid}") as info:
            call(SID, base=str(base))
        assert info.value.hint is not None and "another user created" in info.value.hint


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
    with pytest.raises(TmpdirUnsafe, match="sticky") as info:
        tmpdir.create(SID, base=str(base))
    assert not root_of(base).exists()
    assert str(info.value).startswith(f"private temporary directory unsafe: {base}: ")
    hint = info.value.hint
    assert hint is not None
    assert f"system temporary directory {base} is misconfigured" in hint
    assert "administrator" in hint and "sticky bit" in hint and "sandbox to off" in hint


def test_group_writable_base_without_sticky_bit_is_refused(base: Path) -> None:
    base.chmod(0o770)
    with pytest.raises(TmpdirUnsafe, match="sticky"):
        tmpdir.create(SID, base=str(base))


def test_private_base_without_sticky_bit_is_accepted(base: Path) -> None:
    base.chmod(0o700)
    assert Path(tmpdir.create(SID, base=str(base))).is_dir()


def test_base_owned_by_another_user_is_refused(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    with pytest.raises(TmpdirUnsafe, match=f"owned by uid {real_uid}"):
        tmpdir.create(SID, base=str(base))


def test_base_owned_by_another_uid_explains_user_namespaces(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inside a user namespace /tmp's owner is often unmapped (the overflow uid 65534): still
    # refused, with a hint naming that likely cause and the remedy.
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    with pytest.raises(TmpdirUnsafe) as info:
        tmpdir.create(SID, base=str(base))
    assert str(info.value).startswith(f"private temporary directory unsafe: {base}: owned by")
    hint = info.value.hint
    assert hint is not None
    assert "user namespace" in hint and "65534" in hint
    assert "sandbox to off" in hint and "owned by root or you" in hint


def test_missing_base_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TmpdirUnsafe):
        tmpdir.create(SID, base=str(tmp_path / "missing"))


def test_base_that_is_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    with pytest.raises(TmpdirUnsafe):
        tmpdir.create(SID, base=str(tmp_path / "file"))


# Messages name the failing entry; check and create report the same text


@pytest.mark.parametrize("setup", ["mode", "symlink", "file"])
def test_bad_own_root_names_the_root_and_says_remove_it(
    tmp_path: Path, base: Path, setup: str
) -> None:
    root = root_of(base)
    if setup == "mode":
        root.mkdir(mode=0o755)
    elif setup == "symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        root.symlink_to(tmp_path / "elsewhere")
    else:
        root.write_text("x")
    errors = []
    for call in (tmpdir.check, tmpdir.create):
        with pytest.raises(TmpdirUnsafe) as info:
            call(SID, base=str(base))
        errors.append((str(info.value), info.value.hint))
    assert errors[0] == errors[1]
    message, hint = errors[0]
    assert message.startswith(f"private temporary directory unsafe: {root}: ")
    assert hint == f"remove it (rm -rf {root}) and run again: it is recreated with mode 0700"


def test_unsafe_base_gives_the_same_text_from_check_and_create(base: Path) -> None:
    base.chmod(0o777)
    errors = []
    for call in (tmpdir.check, tmpdir.create):
        with pytest.raises(TmpdirUnsafe) as info:
            call(SID, base=str(base))
        errors.append((str(info.value), info.value.hint))
    assert errors[0] == errors[1]


# Leftovers that cannot be removed (a sandboxed agent can build a tree deeper than the cap)


def test_stale_names_are_never_session_ids() -> None:
    assert tmpdir.STALE_PREFIX.startswith(".")
    assert not ids.is_session_id(f"{tmpdir.STALE_PREFIX}{SID}-0123456789abcdef")


def test_create_moves_an_unremovable_leftover_aside(base: Path, low_depth_cap: int) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    deep_tree(run, low_depth_cap + 3)
    assert Path(tmpdir.create(SID, base=str(base))) == run
    assert list(run.iterdir()) == []
    assert mode(run) == 0o700
    (stale,) = stale_entries(base)
    assert stale.name.startswith(f"{tmpdir.STALE_PREFIX}{SID}-")
    assert not ids.is_session_id(stale.name)
    assert (stale / "d").is_dir()  # the second removal failed too: left for reconcile


def test_create_reporting_names_a_leftover_left_aside(base: Path, low_depth_cap: int) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    deep_tree(run, low_depth_cap + 3)
    path, stale_path = tmpdir.create_reporting(SID, base=str(base))
    assert path == str(run)
    (stale,) = stale_entries(base)
    assert stale_path == str(stale)


def test_create_reporting_reports_nothing_without_a_stale_entry(base: Path) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    (run / "x").write_text("x")
    assert tmpdir.create_reporting(SID, base=str(base)) == (str(run), None)
    assert tmpdir.create_reporting("cs-bbbb2345", base=str(base)) == (
        str(root_of(base) / "cs-bbbb2345"),
        None,
    )


def test_root_name_is_the_single_root_definition() -> None:
    assert tmpdir.root_name(1000) == "csession-1000"
    assert pathrules.tmp_parent(1000) == os.path.join(pathrules.TMP_ROOT, tmpdir.root_name(1000))
    assert pathrules.tmp_parent() == os.path.join(
        pathrules.TMP_ROOT, tmpdir.root_name(os.geteuid())
    )
    assert tmpdir.root_path() == os.path.join(
        os.path.realpath(pathrules.TMP_ROOT), tmpdir.root_name(os.geteuid())
    )


def test_create_removes_the_moved_aside_leftover_when_it_can(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    (run / "x").write_text("x")
    real_remove = fsutil.remove_tree_at
    calls: list[str] = []

    def fail_once(dir_fd: int, name: str) -> None:
        calls.append(name)
        if len(calls) == 1:
            raise fsutil.UnsafePathError(f"{name}: directory tree deeper than 3 levels")
        real_remove(dir_fd, name)

    monkeypatch.setattr(fsutil, "remove_tree_at", fail_once)
    tmpdir.create(SID, base=str(base))
    assert list(run.iterdir()) == []
    assert stale_entries(base) == []
    assert calls[0] == SID and calls[1].startswith(tmpdir.STALE_PREFIX)


def test_create_refuses_a_leftover_it_can_neither_remove_nor_move(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))

    def cannot_remove(dir_fd: int, name: str) -> None:
        raise PermissionError(1, "Operation not permitted")

    def cannot_rename(*args: object, **kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(fsutil, "remove_tree_at", cannot_remove)
    monkeypatch.setattr(os, "rename", cannot_rename)
    with pytest.raises(TmpdirUnsafe) as info:
        tmpdir.create(SID, base=str(base))
    assert str(info.value).startswith(f"private temporary directory unsafe: {run}: ")
    assert "leftover" in str(info.value)
    assert info.value.hint == f"remove it: rm -rf {run}"


def test_remove_moves_an_unremovable_tree_aside(base: Path, low_depth_cap: int) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    deep_tree(run, low_depth_cap + 3)
    with pytest.raises(TmpdirLeftover) as info:
        tmpdir.remove(SID, base=str(base))
    assert isinstance(info.value, OSError)
    assert not run.exists() and not run.is_symlink()  # the session's path is free
    (stale,) = stale_entries(base)
    assert info.value.path == str(run)
    assert info.value.stale_path == str(stale)
    assert str(stale) in str(info.value)
    assert info.value.hint == f"remove it: rm -rf {stale}"
    # A later run of the session gets a fresh tmpdir.
    assert Path(tmpdir.create(SID, base=str(base))).is_dir()


def test_remove_that_cannot_move_aside_names_the_run_path(
    base: Path, low_depth_cap: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Path(tmpdir.create(SID, base=str(base)))
    deep_tree(run, low_depth_cap + 3)

    def cannot_rename(*args: object, **kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "rename", cannot_rename)
    with pytest.raises(TmpdirLeftover) as info:
        tmpdir.remove(SID, base=str(base))
    assert info.value.stale_path is None
    assert info.value.hint == f"remove it: rm -rf {run}"
    assert run.is_dir()


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


def test_a_late_remove_never_deletes_the_next_runs_tmpdir(base: Path) -> None:
    # Run n created its tmpdir; run n + 1 of the same session created its own (clearing run
    # n's) before run n's late cleanup: that removal leaves run n + 1's directory alone.
    first = tmpdir.create_pinned(SID, base=str(base))
    second = tmpdir.create_pinned(SID, base=str(base))
    try:
        assert first.path == second.path
        (Path(second.path) / "f").write_text("run n + 1")
        assert tmpdir.remove(SID, base=str(base), pin=first.pin) is False
        assert (Path(second.path) / "f").read_text() == "run n + 1"
        assert tmpdir.remove(SID, base=str(base), pin=second.pin) is True
        assert not Path(second.path).exists()
    finally:
        os.close(first.pin)
        os.close(second.pin)


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
