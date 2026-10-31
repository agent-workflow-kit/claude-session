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
6. (#24) PATH sanitizing for isolated runs.
7. (#23) git hardening for third-party-reading runs.
8. Every ``CSESSION_*`` removed, then ``CSESSION_ID``, ``CSESSION_DIR``, ``CSESSION_KIND`` and,
   when a private temporary directory is given, ``TMPDIR`` and ``CLAUDE_CODE_TMPDIR`` set.

Glob entries (``PREFIX*``, e.g. ``LC_*``) match a name starting with the prefix; the prefix must
itself be a valid variable-name start. Any other entry matches only the identical name.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from . import config, ids, kinds, logs
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
    # the private tmpdir (#25), and builds the redaction set and the contains_secret index
    # (secretindex.write_at) from secret_values(session_env, environ, run_secret).
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

    auth_names = tuple(name for name in env if name in auth or _matches(name, auth_globs))
    return SessionEnv(env=env, auth_names=auth_names, secret_names=secret_names)


def secret_values(
    session_env: SessionEnv, environ: Mapping[str, str], *, run_secret: str | None = None
) -> frozenset[str]:
    """The run's secret values (interface.md, report: ``contains_secret``), which the log and
    audit writers also redact: the ``claude_auth_env`` values as passed (from
    ``session_env.env``, so a value ``env.set`` supplies or overrides is the one indexed),
    the ``secret_env`` values of ``environ`` (passed or stripped) and the run secret; only
    values of ``logs.MIN_SECRET_CHARS`` or more characters. Other ``env.set`` values are
    literal configuration, not secrets."""
    passed = logs.secret_values(
        session_env.env, claude_auth_env=session_env.auth_names, run_secret=run_secret
    )
    return passed | logs.secret_values(environ, secret_env=session_env.secret_names)


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
