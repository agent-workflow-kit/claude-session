"""The per-run copy of a plugin directory with the sandbox mask placeholders (interface.md,
Isolation, Plugin directories): bounds, symlinks and special files refused, placeholders
added only when missing, modes, and the copy never written through a planted symlink."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from claude_session import fsutil, plugincopy
from claude_session.errors import LaunchValidationError

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


def test_a_symlink_to_a_file_is_refused(plugin: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("s")
    (plugin / "link").symlink_to(secret)
    assert "link" in refused(plugin).message


def test_a_symlink_to_a_directory_is_refused(plugin: Path, tmp_path: Path) -> None:
    (plugin / "skills" / "out").symlink_to(tmp_path)
    assert "symlink" in refused(plugin).message


def test_a_dangling_symlink_placeholder_is_refused_not_replaced(plugin: Path) -> None:
    (plugin / "CLAUDE.md").symlink_to("/nonexistent/elsewhere")
    assert "CLAUDE.md" in refused(plugin).message


def test_a_symlink_never_leaks_its_target_into_the_copy(
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
