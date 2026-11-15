"""The per-run copy of a plugin directory with the sandbox mask placeholders (interface.md,
Isolation, Plugin directories): bounds, symlinks and special files refused, placeholders
added only when missing, modes, and the copy never written through a planted symlink."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_session import fsutil, plugincopy, sessiondir
from claude_session.errors import LaunchValidationError
from claude_session.sessiondir import SessionDirs

PLACEHOLDERS = [".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md"]


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "myplug"
    (src / "skills" / "hello").mkdir(parents=True)
    (src / "skills" / "hello" / "SKILL.md").write_text("hello\n")
    (src / "hooks").mkdir()
    script = src / "hooks" / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    return src


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    path = tmp_path / "dest"
    path.mkdir(mode=0o700)
    return path


def copy(src: Path, dest: Path, name: str = "myplug") -> plugincopy.Report:
    fd = fsutil.open_dir_nofollow(str(dest))
    try:
        return plugincopy.copy(str(src), fd, name)
    finally:
        os.close(fd)


def refused(src: Path, **limits: int) -> LaunchValidationError:
    with pytest.raises(LaunchValidationError) as info:
        plugincopy.scan(str(src), **limits)
    assert info.value.end_reason == "unsafe_plugin_dir"
    return info.value


def test_a_plugin_without_placeholders_gets_all_four(plugin: Path, dest: Path) -> None:
    report = copy(plugin, dest)
    out = dest / "myplug"
    assert report.added == tuple(PLACEHOLDERS)
    assert (out / ".claude").is_dir() and not any((out / ".claude").iterdir())
    assert (out / ".mcp.json").read_text() == '{"mcpServers": {}}\n'
    assert (out / "CLAUDE.md").read_bytes() == b""
    assert (out / "CLAUDE.local.md").read_bytes() == b""
    assert (out / "skills" / "hello" / "SKILL.md").read_text() == "hello\n"


def test_the_source_is_not_touched(plugin: Path, dest: Path) -> None:
    copy(plugin, dest)
    assert sorted(p.name for p in plugin.iterdir()) == ["hooks", "skills"]


def test_scan_reports_what_the_copy_adds_and_writes_nothing(plugin: Path) -> None:
    assert plugincopy.scan(str(plugin)).added == tuple(PLACEHOLDERS)


def test_a_plugin_with_placeholders_keeps_its_own(plugin: Path, dest: Path) -> None:
    (plugin / ".claude").mkdir()
    (plugin / ".claude" / "keep").write_text("x")
    (plugin / ".mcp.json").write_text('{"mcpServers": {"a": {}}}')
    (plugin / "CLAUDE.md").write_text("own\n")
    (plugin / "CLAUDE.local.md").write_text("")
    report = copy(plugin, dest)
    assert report.added == ()
    out = dest / "myplug"
    assert (out / ".claude" / "keep").read_text() == "x"
    assert (out / ".mcp.json").read_text() == '{"mcpServers": {"a": {}}}'
    assert (out / "CLAUDE.md").read_text() == "own\n"


def test_only_the_missing_placeholders_are_added(plugin: Path, dest: Path) -> None:
    (plugin / "CLAUDE.md").write_text("own\n")
    report = copy(plugin, dest)
    assert report.added == (".claude", ".mcp.json", "CLAUDE.local.md")
    assert (dest / "myplug" / "CLAUDE.md").read_text() == "own\n"


def test_modes_are_private_and_the_exec_bit_is_kept(plugin: Path, dest: Path) -> None:
    old = os.umask(0)
    try:
        copy(plugin, dest)
    finally:
        os.umask(old)
    out = dest / "myplug"
    assert stat.S_IMODE(os.lstat(out).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(out / "skills").st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(out / "skills" / "hello" / "SKILL.md").st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(out / "hooks" / "run.sh").st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(out / "CLAUDE.md").st_mode) == 0o600


def test_an_absolute_symlink_is_refused(plugin: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("s")
    (plugin / "link").symlink_to(secret)
    assert "link" in refused(plugin).message


def test_an_absolute_symlink_to_a_directory_is_refused(plugin: Path, tmp_path: Path) -> None:
    (plugin / "skills" / "out").symlink_to(tmp_path)
    assert "symlink" in refused(plugin).message


def test_a_relative_symlink_inside_the_plugin_is_copied_as_a_link(plugin: Path, dest: Path) -> None:
    (plugin / "bin").mkdir()
    (plugin / "bin" / "tool").write_text("t")
    (plugin / "node_modules" / ".bin").mkdir(parents=True)
    (plugin / "node_modules" / ".bin" / "tool").symlink_to("../../bin/tool")
    (plugin / "alias").symlink_to("skills/hello")
    report = copy(plugin, dest)
    out = dest / "myplug"
    assert os.readlink(out / "node_modules" / ".bin" / "tool") == "../../bin/tool"
    assert os.readlink(out / "alias") == "skills/hello"
    assert (out / "alias" / "SKILL.md").read_text() == "hello\n"
    assert report.entries == plugincopy.scan(str(plugin)).entries


def test_a_dangling_symlink_inside_the_plugin_is_ok(plugin: Path, dest: Path) -> None:
    (plugin / "later").symlink_to("not/there")
    copy(plugin, dest)
    assert os.readlink(dest / "myplug" / "later") == "not/there"


def test_symlinks_count_toward_the_entry_bound_with_no_bytes(plugin: Path) -> None:
    for i in range(3):
        (plugin / f"l{i}").symlink_to("skills")
    report = plugincopy.scan(str(plugin))
    assert report.size == len("hello\n") + len("#!/bin/sh\n")
    refused(plugin, max_entries=report.entries - 1)


@pytest.mark.parametrize(
    ("where", "target"),
    [
        ("link", "../x"),
        ("link", ".."),
        ("link", "a/../../x"),
        ("skills/link", "../../x"),
        ("skills/link", "../.."),
        ("skills/link", "a/../../../x"),
    ],
)
def test_a_symlink_escaping_the_plugin_is_refused(plugin: Path, where: str, target: str) -> None:
    (plugin / where).symlink_to(target)
    assert where in refused(plugin).message


def test_a_symlink_that_stays_inside_through_dotdot_is_ok(plugin: Path) -> None:
    (plugin / "skills" / "link").symlink_to("../hooks/../skills")
    plugincopy.scan(str(plugin))


def test_a_symlinked_plugin_root_is_refused(plugin: Path, tmp_path: Path) -> None:
    root = tmp_path / "rootlink"
    root.symlink_to(plugin)
    refused(root)


def test_an_absolute_dangling_symlink_placeholder_is_refused_not_replaced(plugin: Path) -> None:
    (plugin / "CLAUDE.md").symlink_to("/nonexistent/elsewhere")
    assert "CLAUDE.md" in refused(plugin).message


def test_an_escaping_symlink_never_leaks_its_target_into_the_copy(
    plugin: Path, dest: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("secret")
    (plugin / "evil").symlink_to(outside)
    with pytest.raises(LaunchValidationError):
        copy(plugin, dest)
    assert not list(outside.glob("**/*.md"))
    assert (outside / "f").read_text() == "secret"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_a_fifo_is_refused_without_blocking(plugin: Path) -> None:
    os.mkfifo(plugin / "pipe")
    assert "pipe" in refused(plugin).message


def test_too_many_entries_are_refused(plugin: Path) -> None:
    for i in range(10):
        (plugin / f"f{i}").write_text("")
    assert "entries" in refused(plugin, max_entries=8).message


def test_the_entry_limit_counts_directories(plugin: Path) -> None:
    for i in range(10):
        (plugin / f"d{i}").mkdir()
    refused(plugin, max_entries=8)


def test_too_many_bytes_are_refused(plugin: Path) -> None:
    (plugin / "big").write_bytes(b"x" * 5000)
    assert "bytes" in refused(plugin, max_bytes=4096).message


def test_a_tree_that_is_too_deep_is_refused(plugin: Path) -> None:
    deep = plugin
    for _ in range(6):
        deep = deep / "d"
    deep.mkdir(parents=True)
    assert "deep" in refused(plugin, max_depth=4).message


def test_the_default_bounds_are_sane() -> None:
    assert 1000 <= plugincopy.MAX_ENTRIES <= 100_000
    assert 1 << 20 <= plugincopy.MAX_BYTES <= 1 << 30
    assert 8 <= plugincopy.MAX_DEPTH <= 64


def test_the_copy_stops_when_the_bytes_grow_past_the_limit(plugin: Path, dest: Path) -> None:
    (plugin / "big").write_bytes(b"x" * 5000)
    fd = fsutil.open_dir_nofollow(str(dest))
    try:
        with pytest.raises(LaunchValidationError):
            plugincopy.copy(str(plugin), fd, "myplug", max_bytes=4096)
    finally:
        os.close(fd)


def test_growth_past_the_counted_size_counts_only_the_excess(tmp_path: Path) -> None:
    grown = tmp_path / "grown"
    grown.write_bytes(b"x" * 150)
    walk = plugincopy._Walk("src", 10, 240, 5)
    info = SimpleNamespace(st_size=100, st_mode=stat.S_IFREG | 0o644)
    walk._count_bytes("grown", info.st_size)
    src = os.open(grown, os.O_RDONLY)
    dst_dir = tmp_path / "out"
    dst_dir.mkdir()
    dst = os.open(dst_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        walk.write(src, dst, "grown", "grown", info)  # type: ignore[arg-type]
    finally:
        os.close(src)
        os.close(dst)
    assert walk.size == 150


def test_same_requires_the_same_type_and_for_files_size_and_mtime(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("abc")
    fd = os.open(path, os.O_RDONLY)
    try:
        real = os.fstat(fd)
        assert plugincopy._same(fd, real)
        as_fifo = SimpleNamespace(**{**_fields(real), "st_mode": stat.S_IFIFO | 0o600})
        assert not plugincopy._same(fd, as_fifo)  # type: ignore[arg-type]
        as_dir = SimpleNamespace(**{**_fields(real), "st_mode": stat.S_IFDIR | 0o700})
        assert not plugincopy._same(fd, as_dir)  # type: ignore[arg-type]
        resized = SimpleNamespace(**{**_fields(real), "st_size": real.st_size + 1})
        assert not plugincopy._same(fd, resized)  # type: ignore[arg-type]
        touched = SimpleNamespace(**{**_fields(real), "st_mtime_ns": real.st_mtime_ns + 1})
        assert not plugincopy._same(fd, touched)  # type: ignore[arg-type]
    finally:
        os.close(fd)


def _fields(info: os.stat_result) -> dict[str, int]:
    names = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    return {n: getattr(info, n) for n in names}


def test_an_existing_destination_is_refused(plugin: Path, dest: Path) -> None:
    (dest / "myplug").mkdir()
    with pytest.raises(FileExistsError):
        copy(plugin, dest)


def test_unicode_and_odd_names_are_copied(plugin: Path, dest: Path) -> None:
    for name in ("héllo wörld.md", "a b", "--flag", "x;y", "$(id)", "新.md"):
        (plugin / name).write_text(name)
    copy(plugin, dest)
    for name in ("héllo wörld.md", "--flag", "$(id)", "新.md"):
        assert (dest / "myplug" / name).read_text() == name


def test_an_unreadable_file_is_refused(plugin: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    bad = plugin / "bad"
    bad.write_text("x")
    bad.chmod(0)
    assert "bad" in refused(plugin).message


def test_a_hard_linked_file_is_copied_as_its_bytes(
    plugin: Path, dest: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.write_text("shared")
    os.link(other, plugin / "linked")
    copy(plugin, dest)
    copied = dest / "myplug" / "linked"
    assert copied.read_text() == "shared"
    assert os.lstat(copied).st_nlink == 1


def test_effective_path_keeps_the_directory_name(tmp_path: Path) -> None:
    path = plugincopy.effective_path("/s/sessions/cs-abcd2345", 2, 1, "/x/y/my-plug")
    assert path == "/s/sessions/cs-abcd2345/plugins/2/1/my-plug"


# SessionDirs.copy_plugins

SID = "cs-abcd2345"


@pytest.fixture
def dirs(tmp_path: Path) -> Iterator[SessionDirs]:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with SessionDirs.open(str(state), SID) as d:
        yield d


def test_copy_plugins_returns_the_effective_directories(plugin: Path, dirs: SessionDirs) -> None:
    second = plugin.parent / "other"
    second.mkdir()
    paths, warnings = dirs.copy_plugins(1, [str(plugin), str(second)])
    assert warnings == []
    assert paths == [
        plugincopy.effective_path(dirs.session_path, 1, 0, str(plugin)),
        plugincopy.effective_path(dirs.session_path, 1, 1, str(second)),
    ]
    assert (Path(paths[0]) / "skills" / "hello" / "SKILL.md").read_text() == "hello\n"
    assert (Path(paths[1]) / "CLAUDE.local.md").is_file()


def test_copy_plugins_replaces_earlier_copies(plugin: Path, dirs: SessionDirs) -> None:
    first, _ = dirs.copy_plugins(1, [str(plugin)])
    second, _ = dirs.copy_plugins(2, [str(plugin)])
    assert not Path(first[0]).exists()
    assert Path(second[0]).is_dir()
    again, _ = dirs.copy_plugins(2, [str(plugin)])  # a retried prepare of the same run
    assert again == second and Path(again[0]).is_dir()


def test_copy_plugins_never_writes_through_a_planted_symlink(
    plugin: Path, dirs: SessionDirs, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    os.symlink(outside, Path(dirs.session_path) / "plugins")
    paths, _ = dirs.copy_plugins(1, [str(plugin)])
    assert sorted(p.name for p in outside.iterdir()) == ["keep"]
    assert Path(paths[0]).is_dir()
    assert not (Path(dirs.session_path) / "plugins").is_symlink()


def test_copy_plugins_replaces_a_planted_run_directory_link(
    plugin: Path, dirs: SessionDirs, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    plugins = Path(dirs.session_path) / "plugins"
    plugins.mkdir()
    os.symlink(outside, plugins / "1")
    dirs.copy_plugins(1, [str(plugin)])
    assert list(outside.iterdir()) == []


def test_copy_plugins_with_no_sources_removes_earlier_copies_and_creates_nothing(
    plugin: Path, dirs: SessionDirs
) -> None:
    dirs.copy_plugins(1, [str(plugin)])
    paths, warnings = dirs.copy_plugins(2, [])
    assert (paths, warnings) == ([], [])
    assert not (Path(dirs.session_path) / "plugins").exists()


def test_copy_plugins_refuses_a_hostile_source(
    plugin: Path, dirs: SessionDirs, tmp_path: Path
) -> None:
    (plugin / "link").symlink_to(tmp_path)
    with pytest.raises(LaunchValidationError) as info:
        dirs.copy_plugins(1, [str(plugin)])
    assert info.value.end_reason == "unsafe_plugin_dir"


def test_copy_plugins_is_removed_with_the_session(plugin: Path, dirs: SessionDirs) -> None:
    dirs.copy_plugins(1, [str(plugin)])
    state = dirs.state_root
    dirs.close()
    sessiondir.delete_session_dirs(state, SID)
    assert not os.path.exists(dirs.session_path)
