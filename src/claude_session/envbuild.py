"""The session environment of a run's ``claude`` process (interface.md, Environment variables;
design.md, Environment).

``build`` is a pure function of the resolved kind, the config and an environment mapping (the
caller's for ``exec``, the adopting process's for ``exec --session-id``); it never reads
``os.environ`` and never mutates its inputs. Steps, in the order of design.md, Environment:

1. Base: the whole environment (``env.inherit``) or only the built-in allowlist plus the
   effective allowlist (``Resolution.env_allowlist()``: the kind's ``env.allow`` or
   ``--env-allowlist-only``, then ``--env-allow``). Entries no process environment can hold
   (an empty name, a name with ``=`` or NUL, a value with NUL) are dropped first.
2. ``claude_auth_env`` variables present in the environment (``AWS_*`` only when
   ``CLAUDE_CODE_USE_BEDROCK`` is true, the Google/Vertex names only when
   ``CLAUDE_CODE_USE_VERTEX`` is true; a flag is true when its value, trimmed and lowercased, is
   ``1``, ``true``, ``yes`` or ``on``, and it is read as it will be passed: the expanded
   ``env.set`` value when ``env.set`` sets it, else the environment's value unless step 3
   strips it, in which case it selects nothing).
3. ``secret_env`` variables removed unless the kind's ``env.allow`` (built-in, config and
   per-repo layers: ``profile.env.allow``) names them exactly. Names from ``--env-allow`` or
   ``--env-allowlist-only`` never unlock one. In allowlist-only mode the kind's ``env.allow`` is
   replaced, so it unlocks nothing either (interface.md, ``--env-allowlist-only``).
4. Third-party-reading runs: token, tmux and ssh-agent variables removed; ``GH_CONFIG_DIR`` and
   ``GLAB_CONFIG_DIR`` point at ``$CSESSION_DIR/gh-empty``. Re-applied after step 5, so these
   win over ``env.set`` (``env.set`` may still set a ``secret_env`` name).
5. ``env.set`` (``${CSESSION_DIR}``, ``${CWD}``, ``${HOME}``, ``${GIT_DIR}`` expanded once,
   before step 2 reads the provider flags; any other ``${`` is refused), then ``PATH`` = the
   executable directory, ``env.path_prepend``, the kind's ``PATH``. Config validation
   already refuses invalid ``env.set``, ``env.allow`` and ``path_prepend`` entries; the checks
   here are a backstop.
6. (#24) PATH sanitizing for isolated runs. Isolated runs also lose the dynamic-loader
   variables (``LD_PRELOAD``, ``LD_LIBRARY_PATH``, ``LD_AUDIT``, every ``DYLD_*``), also ones
   ``env.set`` sets: they would reach the unsandboxed hook interpreter.
7. Third-party-reading runs: the git variables that override config or redirect git
   (``GIT_CONFIG_PARAMETERS``, ``GIT_SSH``, ``GIT_DIR``, ...; ``GIT_ENV_DROP``) removed, then
   the ``GIT_CONFIG_COUNT``/``KEY``/``VALUE`` hardening entries appended after the existing
   ones (renumbered; a malformed existing set is replaced) and ``GIT_OPTIONAL_LOCKS=0``.
   After step 5, so they win over ``env.set``.
8. Every ``CSESSION_*`` removed, then ``CSESSION_ID``, ``CSESSION_DIR``, ``CSESSION_KIND`` and,
   when a private temporary directory is given, ``TMPDIR`` and ``CLAUDE_CODE_TMPDIR`` set.

Glob entries (``PREFIX*``, e.g. ``LC_*``) match a name starting with the prefix; the prefix must
itself be a valid variable-name start. Any other entry matches only the identical name.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

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
# Claude Code's true values for the provider flags (compared trimmed and lowercased).
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
# Step 4: removed for third-party-reading runs.
THIRD_PARTY_DROP = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITLAB_TOKEN",
    "GL_TOKEN",
    "TMUX",
    "TMUX_PANE",
    "SSH_AUTH_SOCK",
)
GH_EMPTY = "gh-empty"
GH_CONFIG_ENV = ("GH_CONFIG_DIR", "GLAB_CONFIG_DIR")
# Step 7: git config entries for the agent's own git commands (design.md, Environment), in
# this order, after any existing entries (git takes the last value of a key).
GIT_HARDENING = (
    ("core.fsmonitor", ""),
    ("core.hooksPath", "/dev/null"),
    ("core.sshCommand", ""),
    ("credential.helper", ""),
    ("diff.external", ""),
    ("core.pager", "cat"),
)
# Step 7: removed first; each beats the hardening entries (GIT_CONFIG_PARAMETERS is applied
# after the GIT_CONFIG_COUNT entries) or points git at other commands or another repository.
# Not kinds._GIT_ENV_DROP: that one also drops the GIT_CONFIG_COUNT set kept here.
GIT_ENV_DROP = (
    "GIT_CONFIG_PARAMETERS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_EXTERNAL_DIFF",
    "GIT_PAGER",
    "GIT_ASKPASS",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)
GIT_CONFIG_COUNT = "GIT_CONFIG_COUNT"
GIT_CONFIG_KEY = "GIT_CONFIG_KEY_"
GIT_CONFIG_VALUE = "GIT_CONFIG_VALUE_"
# An existing GIT_CONFIG_COUNT above this (or not plain ASCII digits) is treated as malformed.
MAX_GIT_CONFIG_COUNT = 1000
# Step 6: dynamic-loader variables removed for isolated runs.
LOADER_DROP = ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT")
LOADER_DROP_PREFIX = "DYLD_"
# Step 8.
CSESSION_PREFIX = "CSESSION_"
TMPDIR_ENV = ("TMPDIR", "CLAUDE_CODE_TMPDIR")
# Step 5: the variables env.set may use (interface.md, Permission profiles).
EXPAND_VARS = config.EXPAND_VARS


@dataclass(frozen=True)
class SessionEnv:
    """The built environment and the names ``contains_secret`` needs.

    - ``env``: the values; left out of ``repr`` (it holds passed secrets).
    - ``auth_names``: the ``claude_auth_env`` names in the final environment, in its order:
      the exact names (built-in and config) and, for a selected provider, its globbed names,
      wherever the value came from (the caller or ``env.set``). Consumers (#15, #26) take the
      values from ``env``.
    - ``secret_names``: the ``secret_env`` names present in the input environment, whether or
      not they were passed (interface.md, report: "a ``secret_env`` variable present in
      ``exec``'s environment").
    """

    env: dict[str, str] = field(repr=False)
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
    # and the contains_secret index (#26); the auth values come from SessionEnv.env (they may
    # come from env.set), the secret_env values from the input environment.
    _check_path_entry(exe_dir, ValueError, "the claude-session executable directory")
    profile = resolution.profile

    # 1. Base (entries execve cannot pass are dropped).
    environ = {name: value for name, value in environ.items() if _passable(name, value)}
    if profile.env.inherit:
        env = dict(environ)
    else:
        allowed = [*BASE_ALLOWLIST, *resolution.env_allowlist()]
        env = {name: value for name, value in environ.items() if _matches(name, allowed)}

    # 3 and 5 (computed first: step 2 reads the provider flags as they will be passed).
    unlocking = set(profile.env.allow) if resolution.env_allowlist_only is None else set()
    stripped = [name for name in cfg.secret_env if name not in unlocking]
    variables = {
        "CSESSION_DIR": csession_dir,
        "CWD": cwd,
        "HOME": environ.get("HOME", ""),
        "GIT_DIR": git_dir or "",
    }
    set_values: dict[str, str] = {}
    for key, value in profile.env.set.items():
        name = _check_set_name(key)  # before the value: an invalid name is never echoed
        set_values[name] = _expand(name, value, variables)

    # 2. claude_auth_env.
    auth = [*AUTH_ENV, *cfg.claude_auth_env]
    auth_globs: list[str] = []
    if _selected(BEDROCK_FLAG, environ, stripped, set_values):
        auth_globs.extend(BEDROCK_ENV)
    if _selected(VERTEX_FLAG, environ, stripped, set_values):
        auth_globs.extend(VERTEX_ENV)
    for name, value in environ.items():
        # Config extras are names only: compared exactly, never as globs.
        if name in auth or _matches(name, auth_globs):
            env[name] = value

    # 3. secret_env.
    for name in stripped:
        env.pop(name, None)
    secret_names = tuple(name for name in cfg.secret_env if name in environ)

    # 4. Third-party-reading runs (again after step 5).
    if profile.reads_third_party:
        _third_party(env, csession_dir)

    # 5. env.set (expanded above), path_prepend, the executable directory.
    env.update(set_values)
    for entry in profile.env.path_prepend:
        _check_path_entry(entry, UsageError, "env.path_prepend")
    kind_path = env.get("PATH", "")
    env["PATH"] = ":".join(
        [exe_dir, *profile.env.path_prepend, *([kind_path] if kind_path else [])]
    )
    # Step 4 wins over env.set: a run can be third-party-reading by flag, so a token literal
    # in the kind's env.set must not reach it.
    if profile.reads_third_party:
        _third_party(env, csession_dir)

    # 6. TODO(#24): sanitize PATH for isolated runs (drop empty and relative entries, refuse
    #    entries inside session-writable paths with unsafe_path).
    if resolution.isolated():
        for name in [n for n in env if n in LOADER_DROP or n.startswith(LOADER_DROP_PREFIX)]:
            del env[name]
    # 7. Git hardening (after step 5: it wins over env.set).
    if profile.reads_third_party:
        for name in GIT_ENV_DROP:
            env.pop(name, None)
        _git_hardening(env)

    # 8. CSESSION_* and the private temporary directory.
    for name in [name for name in env if name.startswith(CSESSION_PREFIX)]:
        del env[name]
    env["CSESSION_ID"] = csession_id
    env["CSESSION_DIR"] = csession_dir
    env["CSESSION_KIND"] = resolution.kind
    if tmpdir is not None:
        for name in TMPDIR_ENV:
            env[name] = tmpdir

    auth_names = tuple(name for name in env if name in auth or _matches(name, auth_globs))
    return SessionEnv(env=env, auth_names=auth_names, secret_names=secret_names)


def _passable(name: str, value: str) -> bool:
    """Whether ``execve`` can pass the entry: a non-empty name without ``=`` or NUL, and a
    value without NUL."""
    return bool(name) and "=" not in name and "\x00" not in name and "\x00" not in value


def _selected(
    flag: str,
    environ: Mapping[str, str],
    stripped: Iterable[str],
    set_values: Mapping[str, str],
) -> bool:
    """Whether a provider flag selects its provider, read as it will be passed: the expanded
    ``env.set`` value when ``env.set`` sets it, else the environment's value unless
    ``secret_env`` strips it; true when, trimmed and lowercased, it is one of ``TRUE_VALUES``."""
    if flag in set_values:
        value = set_values[flag]
    elif flag in stripped:
        return False
    else:
        value = environ.get(flag, "")
    return value.strip().lower() in TRUE_VALUES


def _third_party(env: dict[str, str], csession_dir: str) -> None:
    for name in THIRD_PARTY_DROP:
        env.pop(name, None)
    for name in GH_CONFIG_ENV:
        env[name] = os.path.join(csession_dir, GH_EMPTY)


def _git_hardening(env: dict[str, str]) -> None:
    """Append ``GIT_HARDENING`` after the existing ``GIT_CONFIG_*`` entries and set
    ``GIT_OPTIONAL_LOCKS=0``. Entries git would ignore (numbered at or above the count) are
    dropped. A malformed existing set (a count that is not 0-``MAX_GIT_CONFIG_COUNT`` in ASCII
    digits, or a missing key or value below it) makes git refuse every command, so it is
    dropped whole and only the hardening is passed."""
    count = _git_config_count(env)
    keep = count or 0
    for name in list(env):
        index = _git_config_index(name)
        if name == GIT_CONFIG_COUNT or (index is not None and not 0 <= index < keep):
            del env[name]
    for offset, (key, value) in enumerate(GIT_HARDENING):
        env[f"{GIT_CONFIG_KEY}{keep + offset}"] = key
        env[f"{GIT_CONFIG_VALUE}{keep + offset}"] = value
    env[GIT_CONFIG_COUNT] = str(keep + len(GIT_HARDENING))
    env["GIT_OPTIONAL_LOCKS"] = "0"


def _git_config_count(env: Mapping[str, str]) -> int | None:
    """The existing entry count (0 when unset), or None when the set is malformed."""
    raw = env.get(GIT_CONFIG_COUNT)
    if raw is None:
        return 0
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 4 or int(raw) > MAX_GIT_CONFIG_COUNT:
        return None
    count = int(raw)
    for index in range(count):
        if f"{GIT_CONFIG_KEY}{index}" not in env or f"{GIT_CONFIG_VALUE}{index}" not in env:
            return None
    return count


def _git_config_index(name: str) -> int | None:
    """The index of a ``GIT_CONFIG_KEY_<n>``/``VALUE_<n>`` name; -1 for one git never reads
    (a suffix that is not a canonical decimal); None for any other name."""
    for prefix in (GIT_CONFIG_KEY, GIT_CONFIG_VALUE):
        if name.startswith(prefix):
            suffix = name.removeprefix(prefix)
            canonical = suffix.isascii() and suffix.isdigit() and str(int(suffix)) == suffix
            return int(suffix) if canonical else -1
    return None


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
    """Expand the allowed ``${NAME}`` references once (results are not expanded again).
    ``key`` is a valid name (``_check_set_name``); the value is never echoed."""
    problem = config.env_set_value_problem(value)
    if problem:
        raise UsageError(f"env.set.{key}: {problem}")
    return config.ENV_REFERENCE.sub(lambda match: variables[match.group(1)], value)


def _check_path_entry(entry: str, error: type[Exception], where: str) -> None:
    """A ``PATH`` entry must be one absolute path: no ``:``, no NUL, never empty."""
    if not config.is_path_entry(entry):
        raise error(f"{where}: each entry must be an absolute path without ':'")
