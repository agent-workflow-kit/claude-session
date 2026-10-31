"""The session environment of a run's ``claude`` process (interface.md, Environment variables;
design.md, Environment).

``build`` is a pure function of the resolved kind, the config and an environment mapping (the
caller's for ``exec``, the adopting process's for ``exec --session-id``); it never reads
``os.environ`` and never mutates its inputs. Steps, in the order of design.md, Environment:

1. Base: the whole environment (``env.inherit``) or only the built-in allowlist plus the
   effective allowlist (``Resolution.env_allowlist()``: the kind's ``env.allow`` or
   ``--env-allowlist-only``, then ``--env-allow``).
2. ``claude_auth_env`` variables present in the environment (``AWS_*`` only with
   ``CLAUDE_CODE_USE_BEDROCK`` set, the Google/Vertex names only with ``CLAUDE_CODE_USE_VERTEX``
   set; "set" means present and non-empty).
3. ``secret_env`` variables removed unless the kind's ``env.allow`` (built-in, config and
   per-repo layers: ``profile.env.allow``) names them exactly. Names from ``--env-allow`` or
   ``--env-allowlist-only`` never unlock one. In allowlist-only mode the kind's ``env.allow`` is
   replaced, so it unlocks nothing either (the conservative reading).
4. Third-party-reading runs: token, tmux and ssh-agent variables removed; ``GH_CONFIG_DIR`` and
   ``GLAB_CONFIG_DIR`` point at ``$CSESSION_DIR/gh-empty``.
5. ``env.set`` (``${CSESSION_DIR}``, ``${CWD}``, ``${HOME}``, ``${GIT_DIR}`` expanded once; any
   other ``${NAME}`` is refused), then ``PATH`` = the executable directory, ``env.path_prepend``,
   the kind's ``PATH``.
6. (#24) PATH sanitizing for isolated runs.
7. (#23) git hardening for third-party-reading runs.
8. Every ``CSESSION_*`` removed, then ``CSESSION_ID``, ``CSESSION_DIR``, ``CSESSION_KIND`` and,
   when a private temporary directory is given, ``TMPDIR`` and ``CLAUDE_CODE_TMPDIR`` set.

Glob entries (``PREFIX*``, e.g. ``LC_*``) match a name starting with the prefix; the prefix must
itself be a valid variable-name start. Any other entry matches only the identical name.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from . import config, ids, kinds
from .errors import UsageError

# Step 1: passed in allowlist mode (design.md, Environment).
BASE_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_*",
    "TERM",
    "COLORTERM",
    "TZ",
)
# Step 2: built-in claude_auth_env names, and the provider-conditional ones.
AUTH_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)
BEDROCK_FLAG = "CLAUDE_CODE_USE_BEDROCK"
BEDROCK_ENV = ("AWS_*",)
VERTEX_FLAG = "CLAUDE_CODE_USE_VERTEX"
VERTEX_ENV = ("GOOGLE_*", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID")
# Step 4: removed for third-party-reading runs.
THIRD_PARTY_DROP = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "GL_TOKEN",
    "TMUX",
    "TMUX_PANE",
    "SSH_AUTH_SOCK",
)
GH_EMPTY = "gh-empty"
GH_CONFIG_ENV = ("GH_CONFIG_DIR", "GLAB_CONFIG_DIR")
# Step 8.
CSESSION_PREFIX = "CSESSION_"
TMPDIR_ENV = ("TMPDIR", "CLAUDE_CODE_TMPDIR")
# Step 5: the variables env.set may use (interface.md, Path rules).
EXPAND_VARS = ("CSESSION_DIR", "CWD", "HOME", "GIT_DIR")
_REFERENCE = re.compile(r"\$\{([^{}]*)\}")


@dataclass(frozen=True)
class SessionEnv:
    """The built environment and the names ``contains_secret`` needs.

    - ``auth_names``: the ``claude_auth_env`` names passed to the session (step 2, minus any
      removed later), in the order they were added.
    - ``secret_names``: the ``secret_env`` names present in the input environment, whether or
      not they were passed (interface.md, report: "a ``secret_env`` variable present in
      ``exec``'s environment").
    """

    env: dict[str, str]
    auth_names: tuple[str, ...]
    secret_names: tuple[str, ...]


def build(
    resolution: kinds.Resolution,
    cfg: config.Config,
    environ: Mapping[str, str],
    *,
    csession_id: str,
    csession_dir: str,
    cwd: str,
    exe_dir: str,
    git_dir: str | None = None,
    tmpdir: str | None = None,
) -> SessionEnv:
    """Build the session environment from ``environ`` (never modified).

    ``exe_dir`` is the directory of the running ``claude-session`` executable, ``git_dir`` the
    cwd's git common directory (None: none), ``tmpdir`` the private temporary directory of a
    sandboxed run (None: not sandboxed). Raises ``UsageError`` for an invalid ``env.set`` or
    ``env.path_prepend`` entry (the message names the key, never a value) and ``ValueError``
    for an ``exe_dir`` that is not one absolute path.
    """
    # TODO(#15): exec passes exe_dir (from the absolute argv[0]), git_dir (from gitinfo) and
    # the private tmpdir (#25), and feeds auth_names and secret_names to logs.secret_values
    # and the contains_secret index (#26).
    _check_path_entry(exe_dir, ValueError, "the claude-session executable directory")
    profile = resolution.profile

    # 1. Base.
    if profile.env.inherit:
        env = dict(environ)
    else:
        allowed = [*BASE_ALLOWLIST, *resolution.env_allowlist()]
        env = {name: value for name, value in environ.items() if _matches(name, allowed)}

    # 2. claude_auth_env.
    auth = [*AUTH_ENV, *cfg.claude_auth_env]
    auth_globs: list[str] = []
    if environ.get(BEDROCK_FLAG):
        auth_globs.extend(BEDROCK_ENV)
    if environ.get(VERTEX_FLAG):
        auth_globs.extend(VERTEX_ENV)
    auth_added: list[str] = []
    for name, value in environ.items():
        # Config extras are names only: compared exactly, never as globs.
        if name in auth or _matches(name, auth_globs):
            env[name] = value
            auth_added.append(name)

    # 3. secret_env.
    unlocking = set(profile.env.allow) if resolution.env_allowlist_only is None else set()
    for name in cfg.secret_env:
        if name not in unlocking:
            env.pop(name, None)
    secret_names = tuple(name for name in cfg.secret_env if name in environ)

    # 4. Third-party-reading runs.
    if profile.reads_third_party:
        for name in THIRD_PARTY_DROP:
            env.pop(name, None)
        for name in GH_CONFIG_ENV:
            env[name] = os.path.join(csession_dir, GH_EMPTY)

    # 5. env.set, path_prepend, the executable directory.
    variables = {
        "CSESSION_DIR": csession_dir,
        "CWD": cwd,
        "HOME": environ.get("HOME", ""),
        "GIT_DIR": git_dir or "",
    }
    for key, value in profile.env.set.items():
        env[_check_set_name(key)] = _expand(key, value, variables)
    for entry in profile.env.path_prepend:
        _check_path_entry(entry, UsageError, "env.path_prepend")
    kind_path = env.get("PATH", "")
    env["PATH"] = ":".join(
        [exe_dir, *profile.env.path_prepend, *([kind_path] if kind_path else [])]
    )

    # 6. TODO(#24): sanitize PATH for isolated runs (drop empty and relative entries, refuse
    #    entries inside session-writable paths with unsafe_path).
    # 7. TODO(#23): third-party-reading runs: append the GIT_CONFIG_COUNT/KEY/VALUE hardening
    #    entries (renumbered after existing ones) and set GIT_OPTIONAL_LOCKS=0.

    # 8. CSESSION_* and the private temporary directory.
    for name in [name for name in env if name.startswith(CSESSION_PREFIX)]:
        del env[name]
    env["CSESSION_ID"] = csession_id
    env["CSESSION_DIR"] = csession_dir
    env["CSESSION_KIND"] = resolution.kind
    if tmpdir is not None:
        for name in TMPDIR_ENV:
            env[name] = tmpdir

    auth_names = tuple(name for name in auth_added if name in env)
    return SessionEnv(env=env, auth_names=auth_names, secret_names=secret_names)


def _matches(name: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if name == pattern:
            return True
        prefix = pattern.removesuffix("*")
        if (
            prefix != pattern
            and "*" not in prefix
            and ids.is_env_name(prefix)
            and name.startswith(prefix)
        ):
            return True
    return False


def _check_set_name(key: str) -> str:
    if not ids.is_env_name(key):
        raise UsageError(f"env.set: invalid variable name (^{ids.ENV_NAME.pattern}$)")
    return key


def _expand(key: str, value: str, variables: Mapping[str, str]) -> str:
    """Expand ``${NAME}`` references once (results are not expanded again)."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            allowed = ", ".join(f"${{{var}}}" for var in EXPAND_VARS)
            # Only a valid variable name is echoed: it is a reference, not part of a value.
            shown = f"${{{name}}}" if ids.is_env_name(name) else "a reference"
            raise UsageError(f"env.set.{key}: unknown variable {shown} (allowed: {allowed})")
        return variables[name]

    if "\x00" in value:
        raise UsageError(f"env.set.{key}: the value contains a NUL character")
    return _REFERENCE.sub(replace, value)


def _check_path_entry(entry: str, error: type[Exception], where: str) -> None:
    """A ``PATH`` entry must be one absolute path: no ``:``, no NUL, never empty."""
    if not entry.startswith("/") or ":" in entry or "\x00" in entry:
        raise error(f"{where}: each entry must be an absolute path without ':'")
