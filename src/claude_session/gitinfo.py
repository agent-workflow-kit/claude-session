"""Hardened, read-only git calls of a launch: the cwd's git directory and the repository-config
credential warning (interface.md, Path rules and Generated settings; design.md, Git calls).

Every git command runs through ``gateway`` with the absolute ``git`` resolved at launch, the
hardening options of ``kinds.GIT_HARDENING`` and the minimal environment ``kinds.git_env``
(``PATH``, ``HOME``, ``LC_ALL=C``, ``GIT_CONFIG_NOSYSTEM=1``; nothing else is inherited).

``candidate_dirs`` finds, without running git, every directory git could take for the cwd's
git directory: with a ``${GIT_DIR}`` write entry, git is resolved against them before it runs
(``launch.git_writable``).

``exec`` (``launch.check_git``) resolves the git directory at the session's first launch
(``resolve``; its ``FailedError`` ends the launch ``unsafe_cwd``), stores ``stored_value`` on
the session, calls ``check_git_cwd`` for the run kind and ``credential_warnings`` on
third-party-reading runs with a git directory. The origin lookup stays in
``kinds.git_origin_url`` (``kinds`` resolves the repo before a launch has a git directory).
TODO(#28): adoption calls ``check_unchanged`` against the stored value.
TODO(#29): every resume calls ``check_unchanged`` against the stored value.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass

from . import kinds
from .errors import FailedError, LaunchValidationError
from .gateway import Gateway, GatewayTimeout, Result, SystemGateway

# Stored on the session when the first launch found no git directory (a resolved value).
NO_GIT_DIR = "none"
# Kinds that need a git cwd (interface.md, Config: built-in kinds).
GIT_CWD_KINDS = frozenset({"respond"})
GIT_TIMEOUT = 10.0
# Config scopes that belong to the repository (local and worktree config, with includes).
REPO_SCOPES = frozenset({"local", "worktree"})
# A URL with userinfo: ``scheme://userinfo@``; the userinfo may be a credential when it has a
# password (``user:secret``) or the scheme is HTTP(S) (a token is often given as the user name).
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/@\s]*)@")
# The start of git's stderr line for a directory outside every repository (LC_ALL=C).
NOT_A_REPOSITORY = "fatal: not a git repository"
# A ``.git`` file (``gitdir: <path>``) or ``commondir`` file larger than this is not one.
GITFILE_MAX = 4096
GITFILE_PREFIX = "gitdir: "
# The longest stderr excerpt a FailedError detail carries.
STDERR_EXCERPT = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_SYSTEM_GATEWAY = SystemGateway()


@dataclass(frozen=True)
class GitDirs:
    """The cwd's git common directory and, for a linked worktree, its own gitdir (resolved)."""

    common_dir: str
    worktree_git_dir: str | None = None


def resolve(
    cwd: str,
    *,
    git: str,
    environ: Mapping[str, str],
    gw: Gateway = _SYSTEM_GATEWAY,
    git_name: str | None = None,
) -> GitDirs | None:
    """The git directories of ``cwd`` (``git rev-parse --git-common-dir --git-dir``, made
    absolute against ``cwd`` and resolved), or None when ``cwd`` is not in a repository:
    git's stderr has a line starting ``fatal: not a git repository`` and no ``.git``
    entry exists at ``cwd`` or any ancestor on the same file system (``git_entry_above``).
    Raises ``FailedError`` when git fails for any other reason (for example a repository
    git refuses as of dubious ownership, a broken or unreadable ``.git``), when its output
    is not exactly two lines, or when a printed directory does not exist: "no git
    directory" is a stored value and must never be recorded by mistake. The detail carries
    the first line of git's stderr, control characters replaced, at most
    ``STDERR_EXCERPT`` characters.

    The launch path maps ``FailedError`` to the end reason ``unsafe_cwd``."""
    argv = [git, *kinds.GIT_HARDENING, "-C", cwd, "rev-parse", "--git-common-dir", "--git-dir"]
    result = _run(gw, argv, environ, git_name)
    if result.returncode != 0:
        not_a_repository = any(
            line.startswith(NOT_A_REPOSITORY) for line in result.stderr.splitlines()
        )
        if not_a_repository and not git_entry_above(cwd):
            return None
        raise FailedError(
            "git rev-parse failed",
            detail=f"{cwd}: exit {result.returncode}: {stderr_excerpt(result.stderr)}",
        )
    lines = result.stdout[:-1].split("\n") if result.stdout.endswith("\n") else []
    if len(lines) != 2 or not lines[0] or not lines[1]:
        raise FailedError("git rev-parse printed no git directory", detail=cwd)
    common = os.path.realpath(os.path.join(cwd, lines[0]))
    own = os.path.realpath(os.path.join(cwd, lines[1]))
    if not (os.path.isdir(common) and os.path.isdir(own)):
        raise FailedError("git rev-parse printed a directory that does not exist", detail=cwd)
    return GitDirs(common_dir=common, worktree_git_dir=None if own == common else own)


def stderr_excerpt(stderr: str) -> str:
    """The first line of ``stderr``, control characters shown as ``?``, cut to
    ``STDERR_EXCERPT`` characters."""
    first = stderr.split("\n", 1)[0]
    return _CONTROL.sub("?", first)[:STDERR_EXCERPT]


def git_entry_above(cwd: str) -> bool:
    """Whether a ``.git`` entry (``lstat``: any type) exists at ``cwd`` or an ancestor on
    the same file system (git stops its discovery at a mount boundary). A ``cwd`` that
    cannot be examined counts as having one (fail closed)."""
    try:
        current = os.path.realpath(cwd)
        device = os.stat(current).st_dev
    except (OSError, ValueError):
        return True
    while True:
        try:
            os.lstat(os.path.join(current, ".git"))
            return True
        except FileNotFoundError:
            pass
        except OSError:
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        try:
            if os.stat(parent).st_dev != device:
                return False
        except OSError:
            return True
        current = parent


def candidate_dirs(cwd: str) -> list[str]:
    """Every directory git could take for the git directory of ``cwd``, found without
    running git (resolved): at ``cwd`` and every ancestor up to ``/`` (conservatively not
    stopping at the first repository or a mount boundary), a ``.git`` directory, the
    directory a ``.git`` file names (``gitdir: <path>``, relative to the file's directory),
    and the directory itself when it holds a ``HEAD`` file (a bare repository, or ``cwd``
    inside a git directory); each with the directory its ``commondir`` file names (a linked
    worktree's common directory). Raises ``LaunchValidationError`` (``unsafe_cwd``) when a
    ``.git`` entry or a ``commondir`` file cannot be read or is not one: the candidates are
    then not known, and git, which would fail on it too, is not run."""
    found: list[str] = []
    current = os.path.realpath(cwd)
    while True:
        dot_git = os.path.join(current, ".git")
        kind = _entry_type(dot_git)
        if kind == "dir":
            found += _with_common(dot_git)
        elif kind == "file":
            target = _read_small(dot_git)
            if not target.startswith(GITFILE_PREFIX) or not target[len(GITFILE_PREFIX) :].strip():
                raise _undeterminable(dot_git, "not a gitdir file")
            found += _with_common(os.path.join(current, target[len(GITFILE_PREFIX) :].strip()))
        elif kind is not None:
            raise _undeterminable(dot_git, "neither a directory nor a file")
        if _entry_type(os.path.join(current, "HEAD")) is not None:
            found += _with_common(current)
        parent = os.path.dirname(current)
        if parent == current:
            return list(dict.fromkeys(found))
        current = parent


def _entry_type(path: str) -> str | None:
    """``dir``, ``file`` or ``other`` for an existing ``path`` (symlinks followed, as git
    follows them), None when there is nothing there; ``unsafe_cwd`` when it cannot be
    examined."""
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _undeterminable(path, type(exc).__name__) from exc
        raise _undeterminable(path, "a dangling symlink") from None
    except OSError as exc:
        raise _undeterminable(path, type(exc).__name__) from exc
    if stat.S_ISDIR(mode):
        return "dir"
    return "file" if stat.S_ISREG(mode) else "other"


def _with_common(git_dir: str) -> list[str]:
    """``git_dir`` (resolved) and the directory its ``commondir`` file names, if any."""
    resolved = os.path.realpath(git_dir)
    found = [resolved]
    common = os.path.join(resolved, "commondir")
    kind = _entry_type(common)
    if kind == "file":
        target = _read_small(common).strip()
        if not target:
            raise _undeterminable(common, "empty")
        found.append(os.path.realpath(os.path.join(resolved, target)))
    elif kind is not None:
        raise _undeterminable(common, "not a file")
    return found


def _read_small(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read(GITFILE_MAX + 1)
    except OSError as exc:
        raise _undeterminable(path, type(exc).__name__) from exc
    if len(data) > GITFILE_MAX or b"\x00" in data:
        raise _undeterminable(path, "too large or binary")
    return data.decode("utf-8", "surrogateescape")


def _undeterminable(path: str, why: str) -> LaunchValidationError:
    shown = _CONTROL.sub("?", path)
    return LaunchValidationError(
        "the cwd's git directory cannot be determined without running git",
        detail=f"{shown}: {why}",
        end_reason="unsafe_cwd",
    )


def stored_value(dirs: GitDirs | None) -> str:
    """The value stored on the session: the common directory, or ``NO_GIT_DIR``."""
    return NO_GIT_DIR if dirs is None else dirs.common_dir


def check_unchanged(stored: str | None, current: GitDirs | None) -> None:
    """At a resume or adoption: the git directory must be the one the first launch stored
    (``stored``; None: never resolved, nothing to compare). A different directory, or one
    that appeared where ``NO_GIT_DIR`` was stored (or vanished), raises
    ``LaunchValidationError`` (``git_dir_changed``)."""
    if stored is None or stored == stored_value(current):
        return
    raise LaunchValidationError(
        "the cwd's git directory changed since the session's first launch",
        end_reason="git_dir_changed",
    )


def check_git_cwd(kind: str, current: GitDirs | None) -> None:
    """A kind that needs a git cwd (``respond``) without one raises
    ``LaunchValidationError`` (``no_git_cwd``)."""
    if kind in GIT_CWD_KINDS and current is None:
        raise LaunchValidationError(
            f"the {kind} kind needs a cwd inside a git repository", end_reason="no_git_cwd"
        )


def credential_warnings(
    cwd: str,
    *,
    git: str,
    environ: Mapping[str, str],
    gw: Gateway = _SYSTEM_GATEWAY,
    git_name: str | None = None,
) -> list[str]:
    """Warnings for credentials in the repository config of ``cwd`` (local and worktree
    scopes, includes followed; read-only). A warning names the key (a subsection holding
    userinfo is masked), never the value. A git failure gives a warning that the config
    could not be checked; the launch proceeds either way."""
    argv = [
        git,
        *kinds.GIT_HARDENING,
        "-C",
        cwd,
        "config",
        "--list",
        "--includes",
        "--show-scope",
        "-z",
    ]
    try:
        result = _run(gw, argv, environ, git_name)
    except FailedError:
        return ["could not read the repository git config to check it for credentials"]
    if result.returncode != 0:
        return ["could not read the repository git config to check it for credentials"]
    return [
        f"the repository git config may hold a credential in {key}; the session can read it"
        " (use a credential helper instead)"
        for key in credential_keys(result.stdout)
    ]


def credential_keys(listing: str) -> list[str]:
    """The keys of ``git config --list --show-scope -z`` output (``scope NUL key LF value
    NUL``) in a repository scope that may hold a credential: any ``http.*.extraheader`` or
    ``http.extraHeader``; ``sendemail.smtppass``, ``sendemail.<id>.smtppass`` and
    ``imap.pass``; a ``credential.helper`` or ``credential.<url>.helper`` whose value
    contains ``password=``; a value with a URL, or a key whose subsection starts with a URL
    (``url.<base>.insteadOf``), whose userinfo holds a password (any scheme) or a user name
    (HTTP(S): a token is often given as the user name). The returned keys are masked
    (``_masked``); values are never returned."""
    fields = listing.split("\x00")
    found: list[str] = []
    for index in range(0, len(fields) - 1, 2):
        scope, entry = fields[index], fields[index + 1]
        if scope not in REPO_SCOPES:
            continue
        key, _, value = entry.partition("\n")
        lowered = key.lower()
        section, _, rest = lowered.partition(".")
        name = rest.rsplit(".", 1)[-1] if rest else ""
        subsection = key.partition(".")[2].rpartition(".")[0]
        if (
            (section == "http" and name == "extraheader")
            or (section == "sendemail" and name == "smtppass")
            or (section == "imap" and rest == "pass")
            or (section == "credential" and name == "helper" and "password=" in value)
            or _has_userinfo(value)
            or _subsection_userinfo(subsection)
        ):
            masked = _masked(key)
            if masked not in found:
                found.append(masked)
    return found


def _has_userinfo(text: str) -> bool:
    return any(_credential_userinfo(match) for match in _URL_USERINFO.finditer(text))


def _subsection_userinfo(subsection: str) -> bool:
    """Whether a key's subsection starts with a URL with credential userinfo (anchored: in
    ``url.https://tok@host/.insteadof`` the unanchored search would take ``url.https`` for
    the scheme)."""
    match = _URL_USERINFO.match(subsection)
    return match is not None and _credential_userinfo(match)


def _credential_userinfo(match: re.Match[str]) -> bool:
    scheme, userinfo = match.group(1).lower(), match.group(2)
    return ":" in userinfo or bool(userinfo and scheme in ("http", "https"))


def _masked(key: str) -> str:
    """``key`` with a subsection that holds userinfo (``url.<base>.insteadof`` names the
    credential in the key) replaced by ``<redacted>``; control characters shown as ``?``."""
    section, dot, rest = key.partition(".")
    if dot and "." in rest:
        subsection, _, name = rest.rpartition(".")
        if "@" in subsection:
            key = f"{section}.<redacted>.{name}"
    return "".join(char if char.isprintable() else "?" for char in key)


def _run(gw: Gateway, argv: list[str], environ: Mapping[str, str], git_name: str | None) -> Result:
    """Run git: ``argv[0]`` is the resolved git's realpath; with ``git_name`` that path is the
    executable and ``argv[0]`` the bare name (``pathrules.resolve_tool``)."""
    executable = None
    if git_name is not None:
        executable, argv = argv[0], [git_name, *argv[1:]]
    try:
        # git_env sets LC_ALL=C: "not a git repository" is matched in git's (untranslated)
        # stderr. The gateway gives git /dev/null as stdin and decodes its output as UTF-8.
        return gw.run(argv, timeout=GIT_TIMEOUT, env=kinds.git_env(environ), executable=executable)
    except (OSError, ValueError, GatewayTimeout) as exc:
        raise FailedError("git could not be run", detail=type(exc).__name__) from exc
