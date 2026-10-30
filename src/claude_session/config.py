"""Configuration: YAML files, ``config.d/`` drop-ins, ``CSESSION_*`` overrides and validation.

Behavior is interface.md, Config and Environment variables: ``config.yaml`` and then
``config.d/*.yaml`` in lexical order are merged over the built-in defaults (maps deep-merged,
lists replaced, ``hooks.*`` lists appended); the documented ``CSESSION_<KEY>`` variables then
override top-level scalars; flags override everything (``with_flags``). Kind settings: built-in
kind, config kind, per-repo override (``kind_settings``), flags. Any invalid value raises
``UsageError`` (exit 2, ``code: usage``) whose ``detail`` names the key and where it was set.

Never imported by the hook path (design.md, Hook-event fast path): it needs Pydantic and PyYAML.
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
)

from . import defaults, durations
from .errors import UsageError

TOOL = "claude-session"
CONFIG_FILE = "config.yaml"
DROPIN_DIR = "config.d"
DROPIN_SUFFIX = ".yaml"

# Grammars (interface.md, Identifiers). TODO(#5): use the ids grammars.
KIND_NAME = r"[a-z0-9][a-z0-9_-]{0,63}"
PROFILE_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
LABEL = r"[a-z0-9][a-z0-9_.-]{0,63}=[A-Za-z0-9._:/#@+=,-]{0,256}"
REPO_ID = r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?(/(?!\.\.?(/|$))[A-Za-z0-9._-]+)+"
VERSION = r"[0-9]+(\.[0-9]+)*"
# Names that are never config keys and may not be set in env.set (interface.md).
RESERVED_ENV = (
    "CSESSION_ID",
    "CSESSION_DIR",
    "CSESSION_KIND",
    "CSESSION_HOOK",
    "CSESSION_OBJECT_TYPE",
)


def _integer(text: str) -> int:
    if not re.fullmatch(r"-?[0-9]+", text):
        raise ValueError(f"expected an integer, got {text!r}")
    return int(text)


def _duration(text: str) -> float:
    return durations.parse(text)


# The only environment overrides: variable -> (top-level key, conversion of the text).
ENV_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "CSESSION_CLAUDE_BIN": ("claude_bin", str),
    "CSESSION_DEFAULT_KIND": ("default_kind", str),
    "CSESSION_MIN_CLAUDE_VERSION": ("min_claude_version", str),
    "CSESSION_SUMMARY_MAX_BYTES": ("summary_max_bytes", _integer),
    "CSESSION_ARTIFACT_MAX_BYTES": ("artifact_max_bytes", _integer),
    "CSESSION_INPUT_MAX_BYTES": ("input_max_bytes", _integer),
    "CSESSION_NOTES_CONTEXT_TIMEOUT": ("notes_context_timeout", _duration),
    "CSESSION_NOTES_CONTEXT_MAX_BYTES": ("notes_context_max_bytes", _integer),
    "CSESSION_RETENTION_DAYS": ("retention_days", _integer),
    "CSESSION_LOST_GRACE": ("lost_grace", _duration),
    "CSESSION_AUTO_EXIT_DELAY": ("auto_exit_delay", _duration),
    "CSESSION_KILL_GRACE": ("kill_grace", _duration),
    "CSESSION_POLL_INTERVAL": ("poll_interval", _duration),
    "CSESSION_SESSION_START_TIMEOUT": ("session_start_timeout", _duration),
}


# Field types


def _matching(pattern: str, what: str) -> AfterValidator:
    compiled = re.compile(pattern)

    def check(value: str) -> str:
        if not compiled.fullmatch(value):
            raise ValueError(f"{what} {value!r} must match ^{pattern}$")
        return value

    return AfterValidator(check)


def _duration_validator(*, positive: bool, bound: bool) -> BeforeValidator:
    """Durations in config files need a unit when given as strings (schemas/config.json);
    flags (``with_flags``) also accept bare numbers of seconds."""

    def parse(value: Any, info: ValidationInfo) -> float | None:
        bare = bool(info.context and info.context.get("bare"))
        if bound:
            return durations.parse_bound(value, positive=positive, bare=bare)
        return durations.parse(value, positive=positive, bare=bare)

    return BeforeValidator(parse)


def _unique(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError("items must be unique")
    return values


def _no_reserved(values: dict[str, str]) -> dict[str, str]:
    for name in values:
        if name in RESERVED_ENV:
            raise ValueError(f"{name} is reserved and cannot be set")
    return values


KindName = Annotated[str, _matching(KIND_NAME, "kind name")]
ProfileName = Annotated[str, _matching(PROFILE_NAME, "permission-profile name")]
RepoId = Annotated[str, _matching(REPO_ID, "repo id")]
Label = Annotated[str, _matching(LABEL, "label")]
Duration = Annotated[float, _duration_validator(positive=False, bound=False)]
PositiveDuration = Annotated[float, _duration_validator(positive=True, bound=False)]
Bound = Annotated[float | None, _duration_validator(positive=True, bound=True)]
Bytes = Annotated[int, Field(ge=1)]
Turns = Annotated[int, Field(ge=1)]
EnvSet = Annotated[dict[str, str], AfterValidator(_no_reserved)]
SettingSources = Annotated[list[Literal["user", "project", "local"]], AfterValidator(_unique)]
Sandbox = Literal["required", "preferred", "off"]
State = Literal[
    "starting",
    "working",
    "idle",
    "needs-input",
    "reported",
    "exited",
    "exited-unreported",
    "killed",
    "timeout",
    "lost",
    "not-started",
]


# Models (schemas/config.json)


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class KindEnv(_Model):
    inherit: bool
    allow: list[str]
    set: EnvSet
    path_prepend: list[str]


class Kind(_Model):
    """A kind profile; every field is present once merged over a built-in kind."""

    permission_profile: ProfileName
    sandbox: Sandbox
    reads_third_party: bool
    implicit_done: bool
    setting_sources: SettingSources
    mcp: Literal["inherit", "none"]
    cwd: Literal["session-dir", "caller"]
    env: KindEnv
    read_dirs: list[str]
    write_dirs: list[str]
    timeout: Bound
    interactive_timeout: Bound
    max_turns: Turns | None
    auto_exit: bool
    backstop: bool
    notes_context: bool
    model: str | None
    claude_args: list[str]


class EnvOverride(_Model):
    inherit: bool | None = None
    allow: list[str] | None = None
    set: EnvSet | None = None
    path_prepend: list[str] | None = None


class RepoKindOverride(_Model):
    """Per-repo override of a kind; only the fields that were given apply (``exclude_unset``)."""

    permission_profile: ProfileName | None = None
    sandbox: Sandbox | None = None
    env: EnvOverride | None = None
    read_dirs: list[str] | None = None
    write_dirs: list[str] | None = None
    timeout: Bound = None
    max_turns: Turns | None = None
    model: str | None = None


class Repo(_Model):
    kinds: dict[KindName, RepoKindOverride] = {}


class PermissionProfile(_Model):
    permissions: dict[str, Any] | None = None
    sandbox: dict[str, Any] | None = None


class PreLaunchHook(_Model):
    cmd: str
    timeout: PositiveDuration = 30.0
    kinds: list[KindName] | None = None
    exclude_kinds: list[KindName] | None = None
    labels: list[Label] | None = None


class PostStateChangeHook(PreLaunchHook):
    async_: bool = Field(default=False, alias="async")
    states: list[State] | None = None


class Hooks(_Model):
    pre_launch: list[PreLaunchHook] = Field(default=[], alias="pre-launch")
    post_state_change: list[PostStateChangeHook] = Field(default=[], alias="post-state-change")


class Me(_Model):
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)

    name: str | None = None


class Config(_Model):
    """The validated configuration. Durations are seconds (``None``: no bound)."""

    claude_bin: str
    min_claude_version: Annotated[str, _matching(VERSION, "version")]
    default_kind: str
    claude_auth_env: list[str]
    secret_env: list[str]
    notes_context_cmd: Annotated[list[str], Field(min_length=1)]
    notes_context_timeout: PositiveDuration
    notes_context_max_bytes: Annotated[int, Field(ge=0)]
    notes_context_sources: list[Literal["startup", "resume", "fork", "clear", "compact", "other"]]
    summary_max_bytes: Bytes
    artifact_max_bytes: Bytes
    input_max_bytes: Bytes
    retention_days: Annotated[int, Field(ge=0)]
    lost_grace: PositiveDuration
    auto_exit_delay: Duration
    kill_grace: Duration
    poll_interval: PositiveDuration
    session_start_timeout: PositiveDuration
    hooks: Hooks
    me: Me
    kinds: dict[KindName, Kind]
    permission_profiles: dict[ProfileName, PermissionProfile]
    repos: dict[RepoId, Repo]

    def with_flags(self, **flags: Any) -> Self:
        """This config with the given flag values (``None``: flag not given) on top."""
        return with_flags(self, **flags)


# Loading


def default_path(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/claude-session/config.yaml`` (default ``~/.config``); a relative
    ``XDG_CONFIG_HOME`` is ignored, as the XDG spec requires."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):
        home = env.get("HOME") or os.path.expanduser("~")
        base = os.path.join(home, ".config")
    return Path(base) / TOOL / CONFIG_FILE


def load(
    path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    required: bool | None = None,
) -> Config:
    """Load and validate the config at ``path`` (default: ``default_path``) and its drop-ins
    (``config.d/`` next to it). A missing file is an error only when ``required`` (default:
    when ``path`` is given, i.e. ``--config``); drop-ins are read either way."""
    env = os.environ if env is None else env
    if required is None:
        required = path is not None
    main = Path(path) if path is not None else default_path(env)
    sources: list[tuple[str, dict[str, Any]]] = []
    data = _read(main, required=required)
    if data is not None:
        sources.append((str(main), data))
    for dropin in _dropins(main.parent / DROPIN_DIR):
        sources.append((str(dropin), _read(dropin, required=True) or {}))

    raw = copy.deepcopy(defaults.BUILTIN)
    for _, layer in sources:
        merge(raw, layer)
    _complete_new_kinds(raw)
    from_env = _apply_env(raw, env)
    return _validate(Config, raw, lambda loc: _source(loc, sources, from_env))


def merge(base: dict[str, Any], over: Mapping[str, Any], path: tuple[str, ...] = ()) -> None:
    """Merge ``over`` into ``base`` in place: maps deep-merged, lists replaced, except lists
    directly under ``hooks``, which are appended."""
    for key, value in over.items():
        current = base.get(key)
        if isinstance(value, Mapping) and isinstance(current, dict):
            merge(current, value, (*path, str(key)))
        elif path == ("hooks",) and isinstance(value, list) and isinstance(current, list):
            current.extend(copy.deepcopy(value))
        else:
            base[key] = copy.deepcopy(value)


def kind_settings(cfg: Config, kind: str, repo: str | None = None) -> Kind:
    """The config kind ``kind`` with the per-repo override for ``repo`` on top. Raises
    ``KeyError`` for a kind that is neither built in nor configured.

    TODO(#9): origins, the unknown-kind fallback and previous-run flags belong to ``kinds``.
    """
    settings = cfg.kinds[kind]
    override = cfg.repos[repo].kinds.get(kind) if repo in cfg.repos else None
    if override is None:
        return settings
    data = settings.model_dump()
    merge(data, override.model_dump(exclude_unset=True))
    return _validate(Kind, data, lambda loc: f"repos.{repo}.kinds.{kind}")


def with_flags[M: BaseModel](settings: M, /, **flags: Any) -> M:
    """``settings`` with the given flag values on top (``None``: flag not given). Durations may
    be bare numbers of seconds, as on the command line."""
    data = settings.model_dump(by_alias=True)
    data.update({key: value for key, value in flags.items() if value is not None})
    return _validate(type(settings), data, lambda loc: "flags", bare=True)


def _read(path: Path, *, required: bool) -> dict[str, Any] | None:
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        if not required:
            return None
        raise UsageError("config file not found", detail=f"{path}: no such file") from None
    except UnicodeDecodeError as exc:
        raise UsageError("invalid config", detail=f"{path}: not UTF-8 ({exc.reason})") from None
    except OSError as exc:
        raise UsageError("cannot read config", detail=f"{path}: {exc.strerror}") from None
    try:
        data = yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except yaml.YAMLError as exc:
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" at line {mark.line + 1}, column {mark.column + 1}"
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise UsageError("invalid config", detail=f"{path}{where}: {problem}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        found = type(data).__name__
        detail = f"top level: expected a mapping, got {found} (in {path})"
        raise UsageError("invalid config", detail=detail)
    return data


def _dropins(directory: Path) -> list[Path]:
    """``config.d/*.yaml`` in lexical order (hidden files and non-files skipped)."""
    try:
        entries = list(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        raise UsageError("cannot read config", detail=f"{directory}: {exc.strerror}") from None
    return sorted(
        (
            entry
            for entry in entries
            if entry.name.endswith(DROPIN_SUFFIX)
            and not entry.name.startswith(".")
            and entry.is_file()
        ),
        key=lambda entry: entry.name,
    )


def _complete_new_kinds(raw: dict[str, Any]) -> None:
    """A configured kind that is not built in starts from the built-in review kind."""
    kinds = raw.get("kinds")
    if not isinstance(kinds, dict):
        return
    for name, entry in kinds.items():
        if name not in defaults.BUILTIN_KINDS and isinstance(entry, Mapping):
            kind = copy.deepcopy(defaults.BUILTIN_KINDS["review"])
            merge(kind, entry)
            kinds[name] = kind


def _apply_env(raw: dict[str, Any], env: Mapping[str, str]) -> dict[str, str]:
    """Apply the documented ``CSESSION_*`` overrides; returns key -> variable applied."""
    applied: dict[str, str] = {}
    for var, (key, convert) in ENV_OVERRIDES.items():
        if var not in env:
            continue
        try:
            raw[key] = convert(env[var])
        except ValueError as exc:
            detail = f"{key}: {exc} (in environment variable {var})"
            raise UsageError("invalid config", detail=detail) from None
        applied[key] = var
    return applied


def _source(
    loc: tuple[str | int, ...],
    sources: list[tuple[str, dict[str, Any]]],
    from_env: Mapping[str, str],
) -> str:
    """Where the value at ``loc`` was set: an environment variable, or the file that set the
    longest part of ``loc`` (the last such file; for hook entries, the file holding it)."""
    top = str(loc[0]) if loc else ""
    if top in from_env:
        return f"environment variable {from_env[top]}"
    if len(loc) >= 3 and loc[0] == "hooks" and isinstance(loc[2], int):
        index = loc[2]
        for path, data in sources:
            entries = _lookup(data, loc[:2])
            if isinstance(entries, list):
                if index < len(entries):
                    return path
                index -= len(entries)
    best, best_depth = "built-in defaults", 0
    for path, data in sources:
        depth = _depth(data, loc)
        if depth and depth >= best_depth:
            best, best_depth = path, depth
    return best


def _lookup(data: Any, loc: Iterable[str | int]) -> Any:
    for part in loc:
        if not isinstance(data, Mapping) or part not in data:
            return None
        data = data[part]
    return data


def _depth(data: Any, loc: tuple[str | int, ...]) -> int:
    depth = 0
    for part in loc:
        if isinstance(data, Mapping) and part in data:
            data = data[part]
            depth += 1
        elif isinstance(data, list) and isinstance(part, int) and part < len(data):
            data = data[part]
            depth += 1
        else:
            break
    return depth


def _validate[M: BaseModel](
    model: type[M],
    data: dict[str, Any],
    source: Callable[[tuple[str | int, ...]], str],
    *,
    bare: bool = False,
) -> M:
    try:
        return model.model_validate(data, context={"bare": bare})
    except ValidationError as exc:
        problems = [_describe(error, source) for error in exc.errors()]
        raise UsageError("invalid config", detail="; ".join(problems)) from None


def _describe(error: Any, source: Callable[[tuple[str | int, ...]], str]) -> str:
    loc = tuple(part for part in error["loc"] if part != "[key]")
    key = ".".join(str(part) for part in loc) or "top level"
    message = str(error["msg"]).removeprefix("Value error, ")
    if loc and loc[-1] == "sandbox" and isinstance(error.get("input"), bool):
        message += ' (quote "off" in YAML)'
    return f"{key}: {message} (in {source(loc)})"
