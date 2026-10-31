"""Hardened, read-only git calls of a launch: the cwd's git directory and the repository-config
credential warning (interface.md, Path rules and Generated settings; design.md, Git calls).

Every git command runs through ``gateway`` with the absolute ``git`` resolved at launch, the
hardening options of ``kinds.GIT_HARDENING`` and ``kinds.git_env`` (no inherited variable that
selects another repository or extra config; ``GIT_CONFIG_NOSYSTEM=1``).

TODO(#15): exec resolves the git directory at the session's first launch (``resolve``), stores
``stored_value`` on the session, and at every resume and adoption calls ``check_unchanged``;
``check_git_cwd`` for the run kind; ``credential_warnings`` on third-party-reading runs with a
git directory (each warning also logged as ``config_warning``). kinds.git_origin_url moves here
with the #51 gateway wiring.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from subprocess import CompletedProcess

from . import gateway, kinds
from .errors import FailedError, LaunchValidationError

# Stored on the session when the first launch found no git directory (a resolved value).
NO_GIT_DIR = "none"
# Kinds that need a git cwd (interface.md, Config: built-in kinds).
GIT_CWD_KINDS = frozenset({"respond"})
GIT_TIMEOUT = 10.0
# Config scopes that belong to the repository (local and worktree config, with includes).
REPO_SCOPES = frozenset({"local", "worktree"})
# A URL with userinfo: ``scheme://userinfo@``; the userinfo is a credential when it has a
# password (``user:secret``) or the scheme is HTTP(S) (a token given as the user name).
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/@\s]*)@")

Runner = Callable[..., CompletedProcess[str]]


@dataclass(frozen=True)
class GitDirs:
    """The cwd's git common directory and, for a linked worktree, its own gitdir (resolved)."""

    common_dir: str
    worktree_git_dir: str | None = None


def resolve(
    cwd: str, *, git: str, environ: Mapping[str, str], run: Runner = gateway.run
) -> GitDirs | None:
    """The git directories of ``cwd`` (``git rev-parse --git-common-dir --git-dir``, made
    absolute against ``cwd`` and resolved), or None when ``cwd`` is not in a repository.
    Raises ``FailedError`` when git fails for any other reason (for example a repository
    git refuses as of dubious ownership): "no git directory" is a stored value and must
    never be recorded by mistake."""
    argv = [git, *kinds.GIT_HARDENING, "-C", cwd, "rev-parse", "--git-common-dir", "--git-dir"]
    result = _run(run, argv, environ)
    if result.returncode != 0:
        if "not a git repository" in result.stderr:
            return None
        raise FailedError("git rev-parse failed", detail=f"{cwd}: exit {result.returncode}")
    lines = result.stdout.split("\n")
    if len(lines) < 2 or not lines[0] or not lines[1]:
        raise FailedError("git rev-parse printed no git directory", detail=cwd)
    common = os.path.realpath(os.path.join(cwd, lines[0]))
    own = os.path.realpath(os.path.join(cwd, lines[1]))
    return GitDirs(common_dir=common, worktree_git_dir=None if own == common else own)


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
    cwd: str, *, git: str, environ: Mapping[str, str], run: Runner = gateway.run
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
        result = _run(run, argv, environ)
    except FailedError:
        return ["could not read the repository git config to check it for credentials"]
    if result.returncode != 0:
        return ["could not read the repository git config to check it for credentials"]
    return [
        f"the repository git config holds a credential in {key}; the session can read it"
        " (use a credential helper instead)"
        for key in credential_keys(result.stdout)
    ]


def credential_keys(listing: str) -> list[str]:
    """The keys of ``git config --list --show-scope -z`` output (``scope NUL key LF value
    NUL``) in a repository scope that hold a credential: any ``http.*.extraheader`` or
    ``http.extraHeader``, and any key or value with a URL whose userinfo holds a password or
    is given to an HTTP(S) URL. The returned keys are masked (``_masked``)."""
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
        if (section == "http" and name == "extraheader") or _has_userinfo(key + " " + value):
            masked = _masked(key)
            if masked not in found:
                found.append(masked)
    return found


def _has_userinfo(text: str) -> bool:
    for match in _URL_USERINFO.finditer(text):
        scheme, userinfo = match.group(1).lower(), match.group(2)
        if ":" in userinfo or (userinfo and scheme in ("http", "https")):
            return True
    return False


def _masked(key: str) -> str:
    """``key`` with a subsection that holds userinfo (``url.<base>.insteadof`` names the
    credential in the key) replaced by ``<redacted>``; control characters shown as ``?``."""
    section, dot, rest = key.partition(".")
    if dot and "." in rest:
        subsection, _, name = rest.rpartition(".")
        if "@" in subsection:
            key = f"{section}.<redacted>.{name}"
    return "".join(char if char.isprintable() else "?" for char in key)


def _run(run: Runner, argv: list[str], environ: Mapping[str, str]) -> CompletedProcess[str]:
    try:
        # LC_ALL=C: "not a git repository" is matched in git's (untranslated) stderr.
        env = {**kinds.git_env(environ), "LC_ALL": "C"}
        return run(argv, timeout=GIT_TIMEOUT, env=env)
    except (OSError, ValueError, gateway.subprocess.SubprocessError) as exc:
        raise FailedError("git could not be run", detail=type(exc).__name__) from exc
