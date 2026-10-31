"""Git directory checks and the repository-config credential warning (interface.md, Path rules
and Generated settings; design.md, Git calls)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from claude_session import gitinfo
from claude_session.errors import FailedError, LaunchValidationError
from claude_session.gitinfo import GitDirs

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git not installed")
TOKEN = "tok-never-echoed-0123456789"


def git(cwd: Path, *args: str) -> None:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@e",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    return path


def environ(tmp_path: Path) -> dict[str, str]:
    # Inherited variables that would point git elsewhere are dropped by kinds.git_env.
    return {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""), "GIT_DIR": "/nowhere"}


@needs_git
def test_resolve_no_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert GIT is not None
    assert gitinfo.resolve(str(plain), git=GIT, environ=environ(tmp_path)) is None


@needs_git
def test_resolve_repository_and_subdirectory(repo: Path, tmp_path: Path) -> None:
    assert GIT is not None
    (repo / "sub").mkdir()
    expected = GitDirs(common_dir=str(repo / ".git"))
    assert gitinfo.resolve(str(repo), git=GIT, environ=environ(tmp_path)) == expected
    assert gitinfo.resolve(str(repo / "sub"), git=GIT, environ=environ(tmp_path)) == expected


@needs_git
def test_resolve_linked_worktree(repo: Path, tmp_path: Path) -> None:
    assert GIT is not None
    wt = tmp_path.resolve() / "wt"
    git(repo, "worktree", "add", "-q", str(wt))
    dirs = gitinfo.resolve(str(wt), git=GIT, environ=environ(tmp_path))
    assert dirs == GitDirs(
        common_dir=str(repo / ".git"), worktree_git_dir=str(repo / ".git" / "worktrees" / "wt")
    )


def fake(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    calls: list[dict[str, Any]] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kw})
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_resolve_is_hardened_and_absolute() -> None:
    run = fake(0, ".git\n.git\n")
    dirs = gitinfo.resolve("/w/r", git="/usr/bin/git", environ={"GIT_DIR": "/x"}, run=run)
    assert dirs == GitDirs(common_dir=os.path.realpath("/w/r/.git"))
    call = run.calls[0]
    assert call["argv"][0] == "/usr/bin/git"
    assert "core.hooksPath=/dev/null" in call["argv"] and "core.fsmonitor=" in call["argv"]
    assert "GIT_DIR" not in call["env"] and call["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert call["env"]["LC_ALL"] == "C"
    assert call["timeout"] == gitinfo.GIT_TIMEOUT


@pytest.mark.parametrize(
    ("result", "error"),
    [
        ((128, "", "fatal: detected dubious ownership in repository at '/w'"), FailedError),
        ((1, "", "error"), FailedError),
        ((0, "", ""), FailedError),
        ((0, "\n", ""), FailedError),
    ],
)
def test_resolve_failures_are_never_recorded_as_no_git_dir(
    result: tuple[int, str, str], error: type[Exception]
) -> None:
    with pytest.raises(error):
        gitinfo.resolve("/w", git="/usr/bin/git", environ={}, run=fake(*result))


def test_resolve_git_that_cannot_run() -> None:
    def run(argv: list[str], **kw: Any) -> Any:
        raise FileNotFoundError(argv[0])

    with pytest.raises(FailedError):
        gitinfo.resolve("/w", git="/missing/git", environ={}, run=run)


def test_not_a_repository_is_none() -> None:
    run = fake(128, "", "fatal: not a git repository (or any of the parent directories): .git")
    assert gitinfo.resolve("/w", git="/usr/bin/git", environ={}, run=run) is None


# git_dir_changed and no_git_cwd

A = GitDirs(common_dir="/r/.git")


@pytest.mark.parametrize(
    ("stored", "current"),
    [
        (None, A),
        (None, None),
        ("/r/.git", A),
        ("/r/.git", GitDirs(common_dir="/r/.git", worktree_git_dir="/r/.git/worktrees/w")),
        (gitinfo.NO_GIT_DIR, None),
    ],
)
def test_git_dir_unchanged(stored: str | None, current: GitDirs | None) -> None:
    gitinfo.check_unchanged(stored, current)


@pytest.mark.parametrize(
    ("stored", "current"),
    [
        (gitinfo.NO_GIT_DIR, A),  # appeared where none was
        ("/r/.git", None),  # vanished
        ("/r/.git", GitDirs(common_dir="/other/.git")),
    ],
)
def test_git_dir_changed(stored: str, current: GitDirs | None) -> None:
    with pytest.raises(LaunchValidationError) as info:
        gitinfo.check_unchanged(stored, current)
    assert info.value.end_reason == "git_dir_changed"


def test_stored_value() -> None:
    assert gitinfo.stored_value(None) == "none"
    assert gitinfo.stored_value(A) == "/r/.git"


def test_respond_needs_a_git_cwd() -> None:
    with pytest.raises(LaunchValidationError) as info:
        gitinfo.check_git_cwd("respond", None)
    assert info.value.end_reason == "no_git_cwd"
    gitinfo.check_git_cwd("respond", A)
    gitinfo.check_git_cwd("review", None)


# Credential warning


def listing(*entries: tuple[str, str, str | None]) -> str:
    parts = []
    for scope, key, value in entries:
        parts += [scope, key if value is None else f"{key}\n{value}"]
    return "\x00".join(parts) + "\x00"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            ("local", "http.https://host/.extraheader", f"AUTHORIZATION: bearer {TOKEN}"),
            ["http.https://host/.extraheader"],
        ),
        (("local", "http.extraheader", "x"), ["http.extraheader"]),
        (
            ("worktree", "remote.origin.url", f"https://user:{TOKEN}@host/r.git"),
            ["remote.origin.url"],
        ),
        (
            ("local", "remote.origin.pushurl", f"https://{TOKEN}@host/r.git"),
            ["remote.origin.pushurl"],
        ),
        (
            ("local", f"url.https://u:{TOKEN}@host/.insteadof", "https://host/"),
            ["url.<redacted>.insteadof"],
        ),
        (("local", "remote.origin.url", "ssh://git@host/r.git"), []),
        (("local", "remote.origin.url", "git@host:r.git"), []),
        (("local", "remote.origin.url", "https://host/r.git"), []),
        (("global", "http.extraheader", "x"), []),  # not the repository's config
        (("command", "remote.x.url", f"https://u:{TOKEN}@h/"), []),
        (("local", "core.bare", None), []),
    ],
)
def test_credential_keys(entry: tuple[str, str, str | None], expected: list[str]) -> None:
    keys = gitinfo.credential_keys(listing(entry))
    assert keys == expected
    assert all(TOKEN not in key for key in keys)


def test_credential_keys_mask_control_characters() -> None:
    keys = gitinfo.credential_keys(listing(("local", "remote.a\x1bb.url", "https://u:p@h/")))
    assert keys == ["remote.a?b.url"]


@needs_git
def test_credential_warnings_on_a_real_repository(repo: Path, tmp_path: Path) -> None:
    assert GIT is not None
    git(repo, "config", "remote.origin.url", f"https://user:{TOKEN}@host/r.git")
    included = tmp_path / "included.cfg"
    included.write_text(f"[http]\n\textraheader = AUTHORIZATION: bearer {TOKEN}\n")
    git(repo, "config", "include.path", str(included))
    warnings = gitinfo.credential_warnings(str(repo), git=GIT, environ=environ(tmp_path))
    assert len(warnings) == 2
    assert any("remote.origin.url" in w for w in warnings)
    assert any("http.extraheader" in w for w in warnings)
    assert all(TOKEN not in w for w in warnings)


@needs_git
def test_no_credential_warning_for_a_clean_repository(repo: Path, tmp_path: Path) -> None:
    assert GIT is not None
    git(repo, "config", "remote.origin.url", "git@host:r.git")
    assert gitinfo.credential_warnings(str(repo), git=GIT, environ=environ(tmp_path)) == []


def test_credential_warning_when_git_fails() -> None:
    warnings = gitinfo.credential_warnings("/w", git="/g", environ={}, run=fake(1))
    assert warnings == ["could not read the repository git config to check it for credentials"]
