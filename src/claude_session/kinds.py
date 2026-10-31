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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Literal

from . import config, defaults, gateway, ids
from .errors import UsageError

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
_RANK: dict[str, int] = {"previous_run": 0, "flag": 1}

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
    """The effective kind profile of a run and where each value came from."""

    kind: str  # the kind as given (also when it fell back to the review profile)
    builtin: bool
    fallback: bool  # unknown kind: the review profile is used
    repo: str | None
    profile: config.Kind
    origins: dict[str, Origin]  # ORIGIN_FIELDS, plus "repo" when there is one
    warnings: tuple[str, ...] = ()

    def timeout(self, mode: Mode) -> float | None:
        """The run's wall-clock bound in ``mode`` (seconds; None: no bound)."""
        return self.profile.timeout if mode == "headless" else self.profile.interactive_timeout


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
    base_name = FALLBACK_KIND if fallback else name
    if fallback:
        warnings.append(f"unknown kind {name!r}: using the {FALLBACK_KIND} profile")
    base = cfg.kinds[base_name]
    origins = _config_origins(base_name, base)

    repo_id, repo_origin = _resolve_repo(base, repo, cwd, previous, origin_url)
    # The fallback is the whole review profile: per-repo overrides of neither name apply.
    profile = base if fallback else config.kind_settings(cfg, name, repo_id)
    if not fallback and repo_id is not None and repo_id in cfg.repos:
        override = cfg.repos[repo_id].kinds.get(name)
        if override is not None:
            for key in _override_fields(override):
                origins[key] = "repo"
    if repo_id is not None and repo_origin is not None:
        origins["repo"] = repo_origin

    explicit = _explicit(previous.flags if previous else {}, flags or {})
    profile = _apply_flags(profile, explicit, origins, mode)
    return Resolution(
        kind=name,
        builtin=name in BUILTIN_KINDS,
        fallback=fallback,
        repo=repo_id,
        profile=profile,
        origins=origins,
        warnings=tuple(warnings),
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
    lookup = git_origin_url if origin_url is None else origin_url
    url = lookup(cwd)
    derived = ids.repo_id_from_origin(url) if url is not None else None
    return derived, "derived" if derived is not None else None


def _check_repo(value: str, where: str) -> None:
    if not ids.is_repo_id(value):
        raise UsageError(f"{where}: invalid repo id (expected host[:port]/path)")


def git_origin_url(cwd: str) -> str | None:
    """The URL of the ``origin`` remote of the repository at ``cwd`` (hardened, read-only git),
    or None when there is none or git fails. Only git's trailing newline is removed."""
    # TODO(#15): run git through the #51 gateway: the absolute git resolved at launch, with
    # GIT_CONFIG_NOSYSTEM=1 in its environment and the cwd set by the gateway.
    argv = ["git", *GIT_HARDENING, "-C", os.fspath(cwd), "remote", "get-url", "origin"]
    try:
        result = gateway.run(argv, timeout=GIT_TIMEOUT)
    except (OSError, ValueError, gateway.subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.removesuffix("\n")


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
            if value is not None:
                explicit[key] = (value, origin)
    return explicit


def _apply_flags(
    profile: config.Kind,
    explicit: Mapping[str, tuple[Any, Origin]],
    origins: dict[str, Origin],
    mode: Mode,
) -> config.Kind:
    data = profile.model_dump()
    env = data["env"]
    sources: dict[str, Origin] = {}  # field -> origin of the flag that set it (for errors)

    def put(key: str, value: Any, origin: Origin) -> None:
        if key.startswith("env."):
            env[key.removeprefix("env.")] = value
        else:
            data[key] = value
        origins[key] = origin
        sources.setdefault(key.partition(".")[0], origin)

    for key in FLAG_KEYS:
        if key not in explicit:
            continue
        value, origin = explicit[key]
        if key == "timeout":
            put("timeout" if mode == "headless" else "interactive_timeout", value, origin)
        elif key == "setting_sources":
            put(key, _setting_sources(value), origin)
        elif key == "env_allowlist_only":
            put("env.allow", _env_names(value, key, origin, allow_empty=True), origin)
            put("env.inherit", False, origin)
        elif key == "env_allow":
            names = _env_names(value, key, origin, allow_empty=False)
            current = list(env["allow"])
            allowlist = explicit.get("env_allowlist_only")
            if allowlist is not None and _RANK[allowlist[1]] > _RANK[origin]:
                origin = allowlist[1]
            put("env.allow", current + [n for n in names if n not in current], origin)
        else:
            put(key, value, origin)

    third_party = explicit.get("reads_third_party")
    if third_party is not None and third_party[0] is True:
        rank = _RANK[third_party[1]]
        for key, value, flag in _IMPLIED:
            given = explicit.get(flag) if flag is not None else None
            if given is None or _RANK[given[1]] < rank:
                put(key, value, "flag-implied")

    def where(loc: tuple[str | int, ...]) -> str:
        top = str(loc[0]) if loc else ""
        origin = sources.get(top)
        if origin is None:
            return "flags"
        flag = _flag_for(top)
        return f"{flag}, {_LAYER_NAMES[origin]}" if flag else _LAYER_NAMES[origin]

    return config.validate_kind(data, where)


def _flag_for(field_name: str) -> str | None:
    if field_name in ("timeout", "interactive_timeout"):
        return "--timeout"
    if field_name == "env":
        return "--env-allow/--env-allowlist-only"
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
        if name == "" or "=" in name or "\0" in name:
            raise UsageError(
                f"{flag}: a variable name must be non-empty and contain no '=' or NUL"
                f" ({_LAYER_NAMES[origin]})"
            )
        if name not in names:
            names.append(name)
    return names
