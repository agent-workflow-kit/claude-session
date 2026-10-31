"""Kind profile resolution with origins (interface.md, Launch flags, Config and Resume and fork
carry-over; design.md, Resume and fork).

The effective profile of a run is built in this order: built-in kind -> config kind -> per-repo
override -> previous run's stored explicit flags -> flags given now. Every resolved field gets
an origin (``builtin``, ``config``, ``repo``, ``previous_run``, ``flag``, ``flag-implied``;
``derived`` for a repo id taken from the ``origin`` remote).

Origins of the first two layers are decided by value: a field whose value equals the value the
kind starts from (its built-in kind, or the built-in ``review`` kind for a kind that is only
configured) has origin ``builtin``, anything else ``config``. A config file that repeats a
built-in value therefore still shows ``builtin``.

Never imported by the hook path: it needs ``config`` (Pydantic and PyYAML).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Literal

from . import config, defaults, ids
from .errors import UsageError
from .gateway import Gateway, GatewayTimeout, SystemGateway, sanitize_path

Origin = Literal["builtin", "config", "repo", "previous_run", "flag", "flag-implied", "derived"]
Mode = Literal["interactive", "headless"]

# Built-in kinds: the table of interface.md, Config (one copy, shared with the config defaults).
BUILTIN_KINDS = defaults.BUILTIN_KINDS
FALLBACK_KIND = "review"
# Fields that get an origin: every kind field, with `env` split into its parts.
ENV_FIELDS = ("inherit", "allow", "set", "path_prepend")
ORIGIN_FIELDS: tuple[str, ...] = tuple(
    name
    for key in config.Kind.model_fields
    for name in ([f"env.{part}" for part in ENV_FIELDS] if key == "env" else [key])
)
# Explicit launch-setting flags a run stores and a later run may carry (``spec_json``), as
# keyword names. ``timeout`` sets the kind's `timeout` (headless) or `interactive_timeout`
# (interactive). ``--plugin-dir`` is stored too but is not part of the kind profile.
FLAG_KEYS = (
    "timeout",
    "max_turns",
    "model",
    "permission_profile",
    "sandbox",
    "setting_sources",
    "reads_third_party",
    "auto_exit",
    "env_allowlist_only",
    "env_allow",
)
_FLAG_NAMES = {key: "--" + key.replace("_", "-") for key in FLAG_KEYS}
_FLAG_NAMES["auto_exit"] = "--[no-]auto-exit"
_LAYER_NAMES: dict[str, str] = {"flag": "flags", "previous_run": "the previous run's flags"}
# `--reads-third-party` implies these, each unless given explicitly (field, value, flag key).
_IMPLIED: tuple[tuple[str, Any, str | None], ...] = (
    ("sandbox", "required", "sandbox"),
    ("setting_sources", [], "setting_sources"),
    ("env.inherit", False, "env_allowlist_only"),
    ("mcp", "none", None),
)
# Variables that would point git at another repository or config than the one at ``cwd``.
_GIT_ENV_DROP = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    }
)
_GIT_ENV_DROP_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

# Hardening for every git call (design.md, Git calls).
GIT_HARDENING = (
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.sshCommand=",
    "-c",
    "credential.helper=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=cat",
)
GIT_TIMEOUT = 10.0


@dataclass(frozen=True)
class Previous:
    """What a new run carries from the previous run: its resolved repo and its stored explicit
    flags (``FLAG_KEYS``), already filtered by the carry-over rules (kind change, mode change,
    ``--reset``)."""

    repo: str | None
    flags: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Resolution:
    """The effective kind profile of a run and where each value came from.

    ``profile`` holds the kind's values (built-in, config, per-repo override) with the explicit
    flags applied, except for the flag values kept apart below so their provenance survives:

    - ``env_allow``: names from ``--env-allow`` (given now or carried); () when not given.
    - ``env_allowlist_only``: names from ``--env-allowlist-only``; None when not given, ()
      for ``--env-allowlist-only ''``. When set it replaces ``profile.env.allow`` in the
      effective allowlist (``env_allowlist()``); ``profile.env.inherit`` is then false.
    - ``permission_profile_file``: the raw ``--permission-profile`` value when it names a file
      (contains ``/`` or ends in ``.json``, ``.yaml`` or ``.yml``); None otherwise. The file
      replaces the named profile entirely (``permission_profiles.select``);
      ``profile.permission_profile`` keeps the kind's profile name. With origin
      ``previous_run`` it is the previous run's snapshot (the stored flag value is the snapshot
      path, never the original file).

    So ``profile.env.allow`` only ever holds names from the built-in, config and per-repo
    layers. ``origins`` has an entry ``env_allow``, ``env_allowlist_only`` or
    ``permission_profile_file`` (``flag`` or ``previous_run``) exactly when that field is set.
    """

    kind: str  # the kind as given (also when it fell back to the review profile)
    builtin: bool
    fallback: bool  # unknown kind: the review profile is used
    repo: str | None
    profile: config.Kind
    origins: dict[str, Origin]  # ORIGIN_FIELDS, "repo" and the flag-only fields when set
    warnings: tuple[str, ...] = ()
    # These flag-supplied names never unlock a secret_env variable; only profile.env.allow
    # (the kind's config env.allow) can (envbuild.build, step 3).
    env_allow: tuple[str, ...] = ()
    env_allowlist_only: tuple[str, ...] | None = None
    # TODO(#29): resume stores the snapshot path as the --permission-profile flag value (the
    # docstring above relies on it) and passes the stored permission_profile label as
    # expected_label when re-reading a carried snapshot (permission_profiles.select).
    permission_profile_file: str | None = None

    def isolated(self) -> bool:
        """Whether the run is isolated: third-party-reading or its sandbox is not ``off``
        (design.md, Generated settings), whether or not the sandbox actually starts."""
        return self.profile.reads_third_party or self.profile.sandbox != "off"

    def sandbox_lowered(self) -> bool:
        """Whether a third-party-reading run's sandbox mode is below the ``required`` baseline,
        whatever set it (built-in, config, repo override, flag or carry-over); audited
        ``sandbox_lowered`` at adoption and warned about (interface.md, Sandbox baseline)."""
        return self.profile.reads_third_party and self.profile.sandbox != "required"

    def timeout(self, mode: Mode) -> float | None:
        """The run's wall-clock bound in ``mode`` (seconds; None: no bound)."""
        return self.profile.timeout if mode == "headless" else self.profile.interactive_timeout

    def env_allowlist(self) -> list[str]:
        """The effective allowlist: ``env_allowlist_only`` (when set) or the kind's
        ``env.allow``, then the ``env_allow`` names not already in it."""
        base = (
            self.profile.env.allow if self.env_allowlist_only is None else self.env_allowlist_only
        )
        names = list(base)
        return names + [name for name in self.env_allow if name not in names]


def resolve(
    cfg: config.Config,
    kind: str | None = None,
    *,
    mode: Mode,
    repo: str | None = None,
    cwd: str | None = None,
    previous: Previous | None = None,
    flags: Mapping[str, Any] | None = None,
    origin_url: Callable[[str], str | None] | None = None,
) -> Resolution:
    """Resolve the effective profile of a run.

    ``kind`` is the kind of the new run (None: ``default_kind``; a resume passes the previous
    run's kind). ``repo`` is ``--repo`` given now. Without it the repo is the previous run's
    (``previous``), else derived from the ``origin`` remote of ``cwd`` (the caller's cwd or
    ``--cwd``) for kinds whose ``cwd`` is not ``session-dir``; ``cwd=None`` derives nothing.
    ``flags`` are the explicit flags given now (``FLAG_KEYS``; None values: not given).
    Raises ``UsageError`` (exit 2) for an invalid kind name, repo id or flag value.
    """
    name = cfg.default_kind if kind is None else kind
    if not ids.is_kind_name(name):
        raise UsageError(f"invalid kind name {name!r} (^{ids.KIND_NAME.pattern}$)")
    warnings: list[str] = []
    fallback = name not in cfg.kinds
    # An unknown kind resolves exactly as `--kind review` would (including the repo's
    # per-repo `review` override); it keeps its own name.
    base_name = FALLBACK_KIND if fallback else name
    if fallback:
        warnings.append(f"unknown kind {name!r}: using the {FALLBACK_KIND} profile")
    base = cfg.kinds[base_name]
    # TODO(#27): the dry-run serializer must rename env.set -> env.set_names (names only),
    # add the profile's name, and take the top-level `timeout` origin from the mode's key
    # (`timeout` or `interactive_timeout`).
    origins = _config_origins(base_name, base)

    repo_id, repo_origin = _resolve_repo(base, repo, cwd, previous, origin_url)
    profile = config.kind_settings(cfg, base_name, repo_id)
    if repo_id is not None and repo_id in cfg.repos:
        override = cfg.repos[repo_id].kinds.get(base_name)
        if override is not None:
            for key in _override_fields(override):
                origins[key] = "repo"
    if repo_id is not None and repo_origin is not None:
        origins["repo"] = repo_origin

    explicit = _explicit(previous.flags if previous else {}, flags or {})
    extra: dict[str, Any] = {}
    profile = _apply_flags(profile, explicit, origins, mode, extra, warnings)
    _check_profile_name(cfg, profile, origins)
    return Resolution(
        kind=name,
        builtin=name in BUILTIN_KINDS,
        fallback=fallback,
        repo=repo_id,
        profile=profile,
        origins=origins,
        warnings=tuple(warnings),
        **extra,
    )


# Repo


def _resolve_repo(
    base: config.Kind,
    given: str | None,
    cwd: str | None,
    previous: Previous | None,
    origin_url: Callable[[str], str | None] | None,
) -> tuple[str | None, Origin | None]:
    if given is not None:
        _check_repo(given, "--repo")
        return given, "flag"
    if previous is not None:
        if previous.repo is not None:
            _check_repo(previous.repo, "the previous run's repo")
        return previous.repo, "previous_run" if previous.repo is not None else None
    if base.cwd == "session-dir" or cwd is None:
        return None, None
    lookup = _default_origin_url if origin_url is None else origin_url
    url = lookup(cwd)
    derived = ids.repo_id_from_origin(url) if url is not None else None
    return derived, "derived" if derived is not None else None


def _check_repo(value: str, where: str) -> None:
    if not ids.is_repo_id(value):
        raise UsageError(f"{where}: invalid repo id (expected host[:port]/path)")


def git_origin_url(cwd: str, *, git: str, gw: Gateway, git_name: str | None = None) -> str | None:
    """The URL of the ``origin`` remote of the repository at ``cwd`` (hardened, read-only git),
    or None when there is none or git fails. ``git`` is the absolute git resolved at launch
    (``pathrules.resolve_tool``: its realpath, run with ``argv[0]`` = ``git_name`` when
    given). The output is decoded by the gateway (UTF-8, invalid bytes replaced); only git's
    trailing newline is removed."""
    argv = [git_name or git, *GIT_HARDENING, "-C", os.fspath(cwd), "remote", "get-url", "origin"]
    executable = git if git_name else None
    try:
        result = gw.run(argv, timeout=GIT_TIMEOUT, env=git_env(os.environ), executable=executable)
    except (OSError, ValueError, GatewayTimeout):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.removesuffix("\n")


def _default_origin_url(cwd: str) -> str | None:
    """``git_origin_url`` with the ``git`` found on the sanitized ``PATH`` and the real gateway
    (callers outside the launch path, which pass the git resolved under the path rules)."""
    git = shutil.which("git", path=sanitize_path(os.environ.get("PATH", "")))
    if git is None:
        return None
    return git_origin_url(cwd, git=os.path.abspath(git), gw=SystemGateway())


def git_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without the variables that select another repository or extra config
    (``GIT_DIR``, ``GIT_CONFIG_*`` and the like), with ``GIT_CONFIG_NOSYSTEM=1`` and
    ``LC_ALL=C``."""
    clean = {
        key: value
        for key, value in env.items()
        if key not in _GIT_ENV_DROP and not key.startswith(_GIT_ENV_DROP_PREFIXES)
    }
    clean["GIT_CONFIG_NOSYSTEM"] = "1"
    clean["LC_ALL"] = "C"  # git's output never depends on the caller's locale
    return clean


# Origins of the built-in and config layers


@cache
def _builtin_start(name: str) -> config.Kind:
    """The built-in fields a kind starts from (its built-in kind, else the built-in review)."""
    raw = BUILTIN_KINDS.get(name, BUILTIN_KINDS[FALLBACK_KIND])
    return config.Kind.model_validate(raw, context={"bare": False})


def _flatten(kind: config.Kind) -> dict[str, Any]:
    data = kind.model_dump()
    env = data.pop("env")
    data.update({f"env.{part}": env[part] for part in ENV_FIELDS})
    return data


def _config_origins(name: str, kind: config.Kind) -> dict[str, Origin]:
    start = _flatten(_builtin_start(name))
    current = _flatten(kind)
    return {key: "builtin" if current[key] == start[key] else "config" for key in ORIGIN_FIELDS}


def _override_fields(override: config.RepoKindOverride) -> list[str]:
    given = override.model_dump(exclude_unset=True)
    env = given.pop("env", None) or {}
    return [*given, *(f"env.{part}" for part in env)]


# Explicit flags


def _explicit(previous: Mapping[str, Any], now: Mapping[str, Any]) -> dict[str, tuple[Any, Origin]]:
    """Flag key -> (value, origin): a flag given now replaces the stored value."""
    explicit: dict[str, tuple[Any, Origin]] = {}
    layers: tuple[tuple[Mapping[str, Any], Origin], ...] = (
        (previous, "previous_run"),
        (now, "flag"),
    )
    for layer, origin in layers:
        unknown = sorted(set(layer) - set(FLAG_KEYS))
        if unknown:
            raise TypeError(f"not a launch-setting flag: {unknown}")
        for key, value in layer.items():
            # `--reads-third-party` can only force true: any other stored value is not given.
            if value is None or (key == "reads_third_party" and value is not True):
                continue
            explicit[key] = (value, origin)
    return explicit


def _apply_flags(
    profile: config.Kind,
    explicit: Mapping[str, tuple[Any, Origin]],
    origins: dict[str, Origin],
    mode: Mode,
    extra: dict[str, Any],
    warnings: list[str],
) -> config.Kind:
    """Apply the explicit flags to ``profile``. Flag values kept apart from the profile go to
    ``extra`` (``Resolution`` field -> value) with their origin in ``origins``."""
    data = profile.model_dump()
    env = data["env"]
    sources: dict[str, Origin] = {}  # field -> origin of the flag that set it (for errors)

    def get(key: str) -> Any:
        return env[key.removeprefix("env.")] if key.startswith("env.") else data[key]

    def put(key: str, value: Any, origin: Origin) -> None:
        if key.startswith("env."):
            env[key.removeprefix("env.")] = value
        else:
            data[key] = value
        origins[key] = origin
        sources.setdefault(key.partition(".")[0], origin)

    def keep(key: str, value: Any, origin: Origin) -> None:
        extra[key] = value
        origins[key] = origin

    for key in FLAG_KEYS:
        if key not in explicit:
            continue
        value, origin = explicit[key]
        if key == "timeout":
            put("timeout" if mode == "headless" else "interactive_timeout", value, origin)
        elif key == "setting_sources":
            put(key, _setting_sources(value), origin)
        elif key in ("env_allowlist_only", "env_allow"):
            only = key == "env_allowlist_only"
            keep(key, tuple(_env_names(value, key, origin, allow_empty=only)), origin)
            if only:
                put("env.inherit", False, origin)
        elif key == "permission_profile" and ids.is_permission_profile_file(value):
            keep("permission_profile_file", value, origin)
        else:
            put(key, value, origin)

    # `--reads-third-party` implications (interface.md, Launch flags): an explicit flag beats
    # one only when it and `--reads-third-party` are both given in the current call; a
    # carried flag never does.
    third_party = explicit.get("reads_third_party")
    if third_party is not None:
        for key, value, flag in _IMPLIED:
            given = explicit.get(flag) if flag is not None else None
            if given is not None and given[1] == "flag" and third_party[1] == "flag":
                continue
            if flag is not None and given is not None and get(key) != value:
                warnings.append(
                    f"{_FLAG_NAMES[flag]} (from {_LAYER_NAMES[given[1]]}) is overridden by "
                    "--reads-third-party; give both in the same call to keep it"
                )
            put(key, value, "flag-implied")

    def where(loc: tuple[str | int, ...]) -> str:
        top = str(loc[0]) if loc else ""
        origin = sources.get(top)
        if origin is None:
            return "flags"
        flag = _flag_for(top)
        return f"{flag}, {_LAYER_NAMES[origin]}" if flag else _LAYER_NAMES[origin]

    return config.validate_kind(data, where)


def _check_profile_name(
    cfg: config.Config, profile: config.Kind, origins: Mapping[str, Origin]
) -> None:
    """A ``--permission-profile`` name (given now or carried) must name a built-in or configured
    profile; the grammar is checked with the profile (``config.ProfileName``). Names from the
    kind layers were checked by ``config.load``."""
    origin = origins["permission_profile"]
    if origin in _LAYER_NAMES and profile.permission_profile not in cfg.permission_profiles:
        detail = (
            "permission_profile: names no built-in or configured permission profile"
            f" (in --permission-profile, {_LAYER_NAMES[origin]})"
        )
        if origin == "previous_run":  # removed from config since: say how to recover
            detail += "; use --reset permission-profile or give --permission-profile"
        raise UsageError("invalid flag value", detail=detail)


def _flag_for(field_name: str) -> str | None:
    if field_name in ("timeout", "interactive_timeout"):
        return "--timeout"
    if field_name == "env":
        return "--env-allowlist-only"
    return _FLAG_NAMES.get(field_name)


def _setting_sources(value: Any) -> Any:
    """``--setting-sources`` as given on the command line (a comma list, ``""`` for none) or as
    a list; validated with the profile."""
    if isinstance(value, str):
        return [] if value == "" else value.split(",")
    return value


def _env_names(value: Any, key: str, origin: Origin, *, allow_empty: bool) -> list[str]:
    """Variable names of ``--env-allow``/``--env-allowlist-only``. ``''`` stands for "no names"
    in ``--env-allowlist-only`` and is ignored when names are also given."""
    flag = _FLAG_NAMES[key]
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise UsageError(f"{flag}: expected a list of variable names ({_LAYER_NAMES[origin]})")
    names: list[str] = []
    for name in value:
        if not isinstance(name, str):
            raise UsageError(f"{flag}: expected variable names ({_LAYER_NAMES[origin]})")
        if name == "" and allow_empty:
            continue
        if not ids.is_env_name(name):
            raise UsageError(
                f"{flag}: invalid variable name (^{ids.ENV_NAME.pattern}$) ({_LAYER_NAMES[origin]})"
            )
        if name not in names:
            names.append(name)
    return names
