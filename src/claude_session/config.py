"""Configuration: YAML files, ``config.d/`` drop-ins, ``CSESSION_*`` overrides and validation.

Behavior is interface.md, Config and Environment variables: the default ``config.yaml`` and then
``config.d/*.yaml`` next to it in byte order of file name (or only the ``--config`` file) are
merged over the built-in defaults (maps deep-merged, lists replaced, ``hooks.*`` lists appended);
the documented ``CSESSION_<KEY>`` variables then override top-level scalars; flags override
everything (``with_flags``). Kind settings: built-in kind, config kind, per-repo override
(``kind_settings``), flags. Any invalid value raises
``UsageError`` (exit 2, ``code: usage``) whose ``detail`` names the key and where it was set,
never the offending value.

Never imported by the hook path (design.md, Hook-event fast path): it needs Pydantic and PyYAML.
"""

from __future__ import annotations

import copy
import math
import os
import re
import stat
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
from yaml.composer import ComposerError
from yaml.parser import ParserError
from yaml.reader import ReaderError
from yaml.scanner import ScannerError

from . import defaults, durations, ids
from .errors import UsageError

TOOL = "claude-session"
CONFIG_FILE = "config.yaml"
DROPIN_DIR = "config.d"
DROPIN_SUFFIX = ".yaml"
# Limits for each config file (interface.md, Config).
MAX_FILE_BYTES = 1 << 20
MAX_NODES = 100_000  # with aliases expanded
MAX_DEPTH = 64
MAX_INTEGER = defaults.MAX_INTEGER  # every integer key (JSON's exact-integer range)
MAX_RETENTION_DAYS = 36500

VERSION = r"[0-9]+(\.[0-9]+)*"
INVALID_REPO = "<invalid repo id>"
# No env.set name may start with it (interface.md, Environment variables).
CSESSION_PREFIX = "CSESSION_"
# env.allow entries: a name, or a PREFIX* glob whose prefix is a valid name start.
ENV_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*\*?"
# The only references env.set values may contain (interface.md, Permission profiles).
EXPAND_VARS = ("CSESSION_DIR", "CWD", "HOME", "GIT_DIR")
ENV_REFERENCE = re.compile(r"\$\{(" + "|".join(EXPAND_VARS) + r")\}")
REFERENCE_PROBLEM = (
    "only ${CSESSION_DIR}, ${CWD}, ${HOME} and ${GIT_DIR} may be referenced"
    " (any other '${' is refused)"
)
INVALID_ENV_NAME = "<invalid variable name>"


def _integer(text: str) -> int:
    if not re.fullmatch(r"-?[0-9]+", text):
        raise ValueError("expected an integer (ASCII digits)")
    try:
        return int(text)
    except ValueError:  # more digits than int() converts
        raise ValueError("expected an integer (ASCII digits)") from None


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
            raise ValueError(f"{what} must match ^{pattern}$")
        return value

    return AfterValidator(check)


def _not_null(value: Any) -> Any:
    """Refuses an explicit ``null`` for a field whose ``None`` default only means "not given"."""
    if value is None:
        raise ValueError("must not be null")
    return value


def _integral(value: Any) -> Any:
    """An integral float (``3.0``) is an integer, as in JSON Schema; anything else is unchanged."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _grammar(check: Callable[[str], bool], what: str, pattern: str) -> AfterValidator:
    """A name grammar of interface.md, Identifiers (the ``ids`` checks)."""

    def validate(value: str) -> str:
        if not check(value):
            raise ValueError(f"{what} must match ^{pattern}$")
        return value

    return AfterValidator(validate)


def _label(value: str) -> str:
    ids.parse_label(value)  # its ValueError names the broken part, never the value
    return value


def _no_leading_dash(value: str) -> str:
    """``model`` is passed as ``claude --model <value>``: a leading ``-`` reads as an option."""
    if value.startswith("-"):
        raise ValueError("must not start with '-'")
    return value


def _no_controls(value: str) -> str:
    """Non-empty, no C0, DEL or C1 control characters (``claude_bin``, ``model``)."""
    if not value or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        raise ValueError("must be non-empty and contain no control characters")
    return value


def _no_nul(value: str) -> str:
    """No NUL character (a hook ``cmd``: it becomes an ``sh -c`` argument)."""
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


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


def _settable(name: str) -> str:
    """``env.set`` names: every ``CSESSION_*`` name is claude-session's own (step 8 of
    design.md, Environment clears and sets them), so none may be set."""
    if name.startswith(CSESSION_PREFIX):
        raise ValueError(f"{CSESSION_PREFIX}* names cannot be set")
    return name


def env_set_value_problem(value: str) -> str | None:
    """Why ``value`` is not a valid ``env.set`` value (None: valid). A value has no NUL, and
    every ``${`` in it starts one of the exact references ``${CSESSION_DIR}``, ``${CWD}``,
    ``${HOME}``, ``${GIT_DIR}``. A ``$``, ``{`` or ``}`` on its own is literal (``$${HOME}{``
    is valid: expansion is single-pass, so no new reference forms). The message never contains
    the value."""
    if "\x00" in value:
        return "must not contain a NUL character"
    start = value.find("${")
    while start != -1:
        if not ENV_REFERENCE.match(value, start):
            return REFERENCE_PROBLEM
        start = value.find("${", start + 1)
    return None


def _set_value(value: str) -> str:
    problem = env_set_value_problem(value)
    if problem:
        raise ValueError(problem)
    return value


def is_path_entry(entry: str) -> bool:
    """A ``PATH`` entry is one absolute path: non-empty, starts with ``/``, no ``:`` or NUL."""
    return entry.startswith("/") and ":" not in entry and "\x00" not in entry


def _path_entry(entry: str) -> str:
    if not is_path_entry(entry):
        raise ValueError("must be an absolute path without ':' or NUL (not expanded)")
    return entry


KindName = Annotated[str, _grammar(ids.is_kind_name, "kind name", ids.KIND_NAME.pattern)]
ProfileName = Annotated[
    str,
    _grammar(
        ids.is_permission_profile_name,
        "permission-profile name",
        ids.PERMISSION_PROFILE_NAME.pattern,
    ),
]
RepoId = Annotated[str, _grammar(ids.is_repo_id, "repo id", ids.REPO_ID.pattern.strip("^$"))]
Label = Annotated[str, AfterValidator(_label)]
Duration = Annotated[float, _duration_validator(positive=False, bound=False)]
PositiveDuration = Annotated[float, _duration_validator(positive=True, bound=False)]
Bound = Annotated[float | None, _duration_validator(positive=True, bound=True)]
Integral = BeforeValidator(_integral)
NotNull = BeforeValidator(_not_null)
Bytes = Annotated[int, Integral, Field(ge=1, le=MAX_INTEGER)]
Count = Annotated[int, Integral, Field(ge=0, le=MAX_INTEGER)]
Days = Annotated[int, Integral, Field(ge=0, le=MAX_RETENTION_DAYS)]
Turns = Annotated[int, Integral, Field(ge=1, le=MAX_INTEGER)]
Model = Annotated[str, AfterValidator(_no_controls), AfterValidator(_no_leading_dash)]
EnvName = Annotated[str, _grammar(ids.is_env_name, "variable name", ids.ENV_NAME.pattern)]
EnvSetName = Annotated[EnvName, AfterValidator(_settable)]
EnvSetValue = Annotated[str, AfterValidator(_set_value)]
EnvSet = dict[EnvSetName, EnvSetValue]
EnvPattern = Annotated[str, _matching(ENV_PATTERN, "entry")]
PathEntry = Annotated[str, AfterValidator(_path_entry)]
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
    allow: list[EnvPattern]
    set: EnvSet
    path_prepend: list[PathEntry]


class Kind(_Model):
    """A kind profile; every field is present once merged over a built-in kind."""

    permission_profile: ProfileName  # names a built-in or configured profile (``load``)
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
    model: Model | None
    claude_args: list[str]


# In the override and selector models below, ``None`` defaults mean "not given"; an explicit
# ``null`` is refused (``NotNull``) wherever schemas/config.json refuses it.


class EnvOverride(_Model):
    inherit: Annotated[bool | None, NotNull] = None
    allow: Annotated[list[EnvPattern] | None, NotNull] = None
    set: Annotated[EnvSet | None, NotNull] = None
    path_prepend: Annotated[list[PathEntry] | None, NotNull] = None


class RepoKindOverride(_Model):
    """Per-repo override of a kind; only the fields that were given apply (``exclude_unset``)."""

    # Names a built-in or configured profile (``load``).
    permission_profile: Annotated[ProfileName | None, NotNull] = None
    sandbox: Annotated[Sandbox | None, NotNull] = None
    env: Annotated[EnvOverride | None, NotNull] = None
    read_dirs: Annotated[list[str] | None, NotNull] = None
    write_dirs: Annotated[list[str] | None, NotNull] = None
    timeout: Bound = None
    max_turns: Turns | None = None
    model: Model | None = None


class Repo(_Model):
    kinds: dict[KindName, RepoKindOverride] = {}


class PermissionProfile(_Model):
    permissions: Annotated[dict[str, Any] | None, NotNull] = None
    sandbox: Annotated[dict[str, Any] | None, NotNull] = None


class PreLaunchHook(_Model):
    cmd: Annotated[str, AfterValidator(_no_nul)]
    timeout: PositiveDuration = 30.0
    kinds: Annotated[list[KindName] | None, NotNull] = None
    exclude_kinds: Annotated[list[KindName] | None, NotNull] = None
    labels: Annotated[list[Label] | None, NotNull] = None


class PostStateChangeHook(PreLaunchHook):
    async_: bool = Field(default=False, alias="async")
    states: Annotated[list[State] | None, NotNull] = None


class Hooks(_Model):
    pre_launch: list[PreLaunchHook] = Field(default=[], alias="pre-launch")
    post_state_change: list[PostStateChangeHook] = Field(default=[], alias="post-state-change")


class Me(_Model):
    model_config = ConfigDict(strict=True, extra="allow", frozen=True)

    name: Annotated[str | None, NotNull] = None


class Config(_Model):
    """The validated configuration. Durations are seconds (``None``: no bound)."""

    claude_bin: Annotated[str, AfterValidator(_no_controls)]
    min_claude_version: Annotated[str, _matching(VERSION, "version")]
    default_kind: KindName  # also names a built-in or configured kind (checked by ``load``)
    claude_auth_env: list[EnvName]
    secret_env: list[EnvName]
    notes_context_cmd: Annotated[list[str], Field(min_length=1)]
    notes_context_timeout: PositiveDuration
    notes_context_max_bytes: Count
    notes_context_sources: list[Literal["startup", "resume", "fork", "clear", "compact", "other"]]
    summary_max_bytes: Bytes
    artifact_max_bytes: Bytes
    input_max_bytes: Bytes
    retention_days: Days
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


# Keys a flag may set (interface.md, Launch flags). No flag sets a top-level config key
# (``stop --grace`` is its own value; ``kill_grace`` is only its default). Anything else passed to
# ``with_flags`` is a programming error.
CONFIG_FLAG_KEYS: frozenset[str] = frozenset()
KIND_FLAG_KEYS = frozenset(
    {
        "timeout",  # --timeout (headless)
        "interactive_timeout",  # --timeout (interactive)
        "max_turns",
        "model",
        "permission_profile",
        "sandbox",
        "setting_sources",
        "reads_third_party",
        "mcp",  # implied by --reads-third-party
        "env",  # --env-allow, --env-allowlist-only; inherit implied by --reads-third-party
        "auto_exit",
    }
)
FLAG_KEYS: dict[type[BaseModel], frozenset[str]] = {
    Config: CONFIG_FLAG_KEYS,
    Kind: KIND_FLAG_KEYS,
}


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
    """Load and validate the config. Without ``path``: ``default_path`` (missing is fine) and
    its ``config.d/`` drop-ins. With ``path`` (``--config``): only that file, which must exist
    unless ``required`` is false.

    TODO(#28): ``start`` also needs the absolute config path used and a ``config_hash``.
    """
    env = os.environ if env is None else env
    if required is None:
        required = path is not None
    main = Path(path) if path is not None else default_path(env)
    sources: list[tuple[str, dict[str, Any]]] = []
    data = _read(main, required=required)
    if data is not None:
        sources.append((str(main), data))
    if path is None:
        for dropin in _dropins(main.parent / DROPIN_DIR):
            sources.append((str(dropin), _read(dropin, required=True) or {}))

    raw = copy.deepcopy(defaults.BUILTIN)
    for _, layer in sources:
        merge(raw, layer)
    _complete_new_kinds(raw)
    from_env = _apply_env(raw, env)
    cfg = _validate(Config, raw, lambda loc: _source(loc, sources, from_env))
    if cfg.default_kind not in cfg.kinds:
        where = _source(("default_kind",), sources, from_env)
        detail = f"default_kind: names no built-in or configured kind (in {where})"
        raise UsageError("invalid config", detail=detail)
    for repo_id, repo in cfg.repos.items():
        for kind in repo.kinds:
            if kind not in cfg.kinds:
                loc = ("repos", repo_id, "kinds", kind)
                where = _source(loc, sources, from_env)
                detail = (
                    f"repos.{repo_id}.kinds.{kind}: names no built-in or configured kind"
                    f" (in {where})"
                )
                raise UsageError("invalid config", detail=detail)
    _check_profile_references(cfg, lambda loc: _source(loc, sources, from_env))
    return cfg


def _check_profile_references(cfg: Config, source: Callable[[tuple[str | int, ...]], str]) -> None:
    """Every kind's and per-repo override's ``permission_profile`` names a built-in or
    configured profile (config kinds hold names only; files are a flag form)."""
    refs: list[tuple[tuple[str, ...], str | None]] = [
        (("kinds", name, "permission_profile"), kind.permission_profile)
        for name, kind in cfg.kinds.items()
    ]
    for repo_id, repo in cfg.repos.items():
        for name, override in repo.kinds.items():
            loc = ("repos", repo_id, "kinds", name, "permission_profile")
            refs.append((loc, override.permission_profile))
    for loc, profile in refs:
        if profile is not None and profile not in cfg.permission_profiles:
            detail = (
                f"{'.'.join(loc)}: names no built-in or configured permission profile"
                f" (in {source(loc)})"
            )
            raise UsageError("invalid config", detail=detail)


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
    ``KeyError`` for a kind that is neither built in nor configured (``kinds.resolve`` handles
    the unknown-kind fallback, origins and flags)."""
    settings = cfg.kinds[kind]
    override = cfg.repos[repo].kinds.get(kind) if repo in cfg.repos else None
    if override is None:
        return settings
    data = settings.model_dump()
    merge(data, override.model_dump(exclude_unset=True))
    return _validate(Kind, data, lambda loc: f"repos.{repo}.kinds.{kind}")


def validate_kind(data: dict[str, Any], source: Callable[[tuple[str | int, ...]], str]) -> Kind:
    """A ``Kind`` from ``data`` with flag values applied (durations may be bare numbers of
    seconds). Raises ``UsageError("invalid flag value")`` whose ``detail`` names the key and
    ``source(loc)``, never the value."""
    return _validate(Kind, data, source, bare=True, message="invalid flag value")


def with_flags[M: BaseModel](settings: M, /, **flags: Any) -> M:
    """``settings`` (a ``Config`` or ``Kind``) with the given flag values on top (``None``: flag
    not given). Durations may be bare numbers of seconds, as on the command line. A key that no
    flag sets raises ``TypeError``."""
    allowed = FLAG_KEYS.get(type(settings), frozenset())
    unknown = sorted(set(flags) - allowed)
    if unknown:
        raise TypeError(f"not settable by flags on {type(settings).__name__}: {unknown}")
    data = settings.model_dump(by_alias=True, exclude_unset=True)
    data.update({key: value for key, value in flags.items() if value is not None})
    return _validate(type(settings), data, lambda loc: "flags", bare=True)


try:
    from yaml import CSafeLoader as _BaseLoader
except ImportError:  # PyYAML built without libyaml
    from yaml import SafeLoader as _BaseLoader


class _Refused(yaml.constructor.ConstructorError):
    """A constructor error whose ``problem`` is one of our fixed phrases (no file content)."""

    def __init__(self, problem: str, mark: Any) -> None:
        super().__init__(None, None, problem, mark)


_INT, _FLOAT = "tag:yaml.org,2002:int", "tag:yaml.org,2002:float"
_MERGE = "tag:yaml.org,2002:merge"
# YAML types with no JSON counterpart: loaded as ``_Unsupported`` (nothing built), refused by
# ``check_tree`` with the key path.
_UNSUPPORTED = ("timestamp", "binary", "set", "omap", "pairs")


class _Unsupported:
    """Stands for a value of a non-JSON YAML type; holds the type name and position only."""

    __slots__ = ("kind", "mark")

    def __init__(self, kind: str, mark: Any) -> None:
        self.kind = kind
        self.mark = mark


class _Loader(_BaseLoader):  # pyright: ignore[reportGeneralTypeIssues]
    """Safe YAML loading that refuses duplicate mapping keys and merge keys (``<<``, before
    anything is merged), loads non-JSON types as ``_Unsupported``, reads YAML 1.1 base-60
    numbers (``1:30``) as strings and refuses them when tagged ``!!int``/``!!float``. Any other
    constructor exception becomes ``_Refused("invalid value")`` at the node's position."""

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep=deep)
        except yaml.YAMLError:
            raise
        except Exception:  # e.g. !!bool x (KeyError), !!int '' (IndexError), 5000-digit ints
            pass
        # Raised outside the handler: the original exception (it may quote the value) must not
        # become its ``__context__``.
        raise _Refused("invalid value", node.start_mark)

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        for key_node, _ in node.value:
            if key_node.tag == _MERGE:
                raise _Refused("merge keys are not supported", key_node.start_mark)
        super().flatten_mapping(node)

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[Any, Any]:
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)  # refuses merge keys before any key is constructed
            seen: set[tuple[type, Any]] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    marker = (type(key), key)  # True and 1 are different keys here
                    duplicate = marker in seen
                except TypeError:  # unhashable key: the base constructor reports it
                    continue
                if duplicate:
                    raise _Refused("duplicate key", key_node.start_mark)
                seen.add(marker)
        return super().construct_mapping(node, deep=deep)

    def construct_number(self, node: yaml.Node) -> Any:
        if ":" in self.construct_scalar(node):  # pyright: ignore[reportArgumentType]
            raise _Refused("base-60 numbers are not supported", node.start_mark)
        if node.tag == _INT:
            return self.construct_yaml_int(node)
        return self.construct_yaml_float(node)


_Loader.add_constructor(_INT, _Loader.construct_number)
_Loader.add_constructor(_FLOAT, _Loader.construct_number)
for _kind in _UNSUPPORTED:
    _Loader.add_constructor(
        f"tag:yaml.org,2002:{_kind}",
        lambda _loader, node, kind=_kind: _Unsupported(kind, node.start_mark),
    )
_Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in (_INT, _FLOAT)]
    for first, resolvers in _Loader.yaml_implicit_resolvers.items()
}
# PyYAML's int and float resolvers without their base-60 alternatives.
_Loader.add_implicit_resolver(
    _FLOAT,
    re.compile(
        r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
        |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN))$""",
        re.X,
    ),
    list("-+0123456789."),
)
_Loader.add_implicit_resolver(
    _INT,
    re.compile(
        r"""^(?:[-+]?0b[0-1_]+
        |[-+]?0[0-7_]+
        |[-+]?(?:0|[1-9][0-9_]*)
        |[-+]?0x[0-9a-fA-F_]+)$""",
        re.X,
    ),
    list("-+0123456789"),
)


def _read(path: Path, *, required: bool) -> dict[str, Any] | None:
    raw = _read_bytes(path, required=required)
    if raw is None:
        return None
    return parse_yaml(raw, path)


def parse_yaml(raw: bytes, path: Path, *, empty: bool = True) -> dict[str, Any]:
    """A YAML document (``raw``, read from ``path``) with every config-file rule: UTF-8, the
    safe loader (no duplicate or merge keys, JSON types only), the node and depth limits and a
    mapping at the top level (an empty document or ``null`` is ``{}`` when ``empty``, else
    refused). Raises ``UsageError("invalid config")``
    whose ``detail`` names ``path`` and the problem, never the content. Also used for
    permission-profile files."""
    # Errors are raised after the handlers, so the original exception (which may hold or quote
    # the file's content) is never their ``__context__``.
    problem: str | None = None
    text = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        problem = f"{path}: not UTF-8 ({exc.reason})"
    if problem is not None:
        raise UsageError("invalid config", detail=problem)
    data: Any = None
    try:
        _check_nesting(text, path)
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 (a safe loader)
    except yaml.YAMLError as exc:
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" at line {mark.line + 1}, column {mark.column + 1}"
        # Parser and constructor messages may quote the file: report a fixed phrase only.
        problem = f"{path}{where}: {_problem(exc)}"
    except UsageError:
        raise
    except Exception:  # never an internal error, never the original (possibly quoting) text
        problem = f"{path}: invalid YAML"
    if problem is not None:
        raise UsageError("invalid config", detail=problem)
    if data is None and empty:
        return {}
    if isinstance(data, _Unsupported):
        raise _unsupported(data, "top level", path)
    if not isinstance(data, dict):
        found = type(data).__name__
        detail = f"top level: expected a mapping, got {found} (in {path})"
        raise UsageError("invalid config", detail=detail)
    check_tree(data, path)
    return data


def _problem(exc: yaml.YAMLError) -> str:
    if isinstance(exc, _Refused):
        return str(exc.problem)
    if isinstance(exc, ReaderError):
        return "invalid character"
    if isinstance(exc, (ScannerError, ParserError)):
        return "invalid YAML syntax"
    if isinstance(exc, ComposerError):
        return "invalid alias or document structure"
    if isinstance(exc, yaml.constructor.ConstructorError):
        return "unsupported or invalid tagged value"
    return "invalid YAML"


def _check_nesting(text: str, path: Path) -> None:
    """Refuse nesting deeper than ``check_tree`` allows from the event stream, before composing:
    libyaml's composer recurses once per level without limit. The parser is not recursive, and
    scanning stops at the first event that is too deep."""
    depth = 0
    for event in yaml.parse(text, Loader=_Loader):
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            # A collection at tree depth d is the (d + 1)-th open one (top level: depth 0).
            if depth > MAX_DEPTH + 1:
                mark = event.start_mark
                where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
                detail = f"{path}{where}: nested more than {MAX_DEPTH} levels deep"
                raise UsageError("invalid config", detail=detail)
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1


def _read_bytes(path: Path, *, required: bool) -> bytes | None:
    """The file's bytes: a regular file (symlinks followed) of at most ``MAX_FILE_BYTES``.
    Opened non-blocking, so a FIFO is refused instead of waited on."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if os.path.lexists(path):
            detail = f"{path}: symlink does not resolve"
            raise UsageError("cannot read config", detail=detail) from None
        if not required:
            return None
        raise UsageError("config file not found", detail=f"{path}: no such file") from None
    except OSError as exc:
        raise UsageError("cannot read config", detail=f"{path}: {exc.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UsageError("invalid config", detail=f"{path}: not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        raise UsageError("cannot read config", detail=f"{path}: {exc.strerror}") from None
    finally:
        os.close(fd)
    if size > MAX_FILE_BYTES:
        raise UsageError("invalid config", detail=f"{path}: larger than 1 MiB")
    return b"".join(chunks)


def check_tree(data: dict[str, Any], path: Path) -> None:
    """Refuse (``UsageError``) trees over ``MAX_NODES`` nodes (aliases expanded) or
    ``MAX_DEPTH`` levels, non-string mapping keys and non-finite numbers (``.inf``, ``.nan``,
    ``1e400``: not JSON). Shared (aliased) nodes are walked once (memoized by identity). Also
    applied to JSON permission-profile files."""
    seen: dict[int, tuple[int, int]] = {}  # id -> (nodes, height)

    def too_deep() -> UsageError:
        detail = f"{path}: nested more than {MAX_DEPTH} levels deep"
        return UsageError("invalid config", detail=detail)

    def walk(node: Any, depth: int, where: str) -> tuple[int, int]:
        if depth > MAX_DEPTH:
            raise too_deep()
        if not isinstance(node, (dict, list)):
            if isinstance(node, float) and not math.isfinite(node):
                detail = f"{where or 'top level'}: not a finite number (in {path})"
                raise UsageError("invalid config", detail=detail)
            if node is None or isinstance(node, (str, int, float)):  # bool is an int
                return 1, 0
            if isinstance(node, _Unsupported):
                raise _unsupported(node, where, path)
            # Not produced by _Loader; the guard that only JSON types get through.
            detail = f"{where or 'top level'}: unsupported value type (in {path})"
            raise UsageError("invalid config", detail=detail)
        if id(node) in seen:
            nodes, height = seen[id(node)]
            if depth + height > MAX_DEPTH:
                raise too_deep()
            return nodes, height
        if isinstance(node, dict):
            children = []
            for key, value in node.items():
                if not isinstance(key, str):
                    detail = (
                        f"{where or 'top level'}: mapping keys must be strings; quote the key "
                        f"(in {path})"
                    )
                    raise UsageError("invalid config", detail=detail)
                children.append((value, _key_path(where, key)))
        else:
            children = [(value, f"{where}.{index}") for index, value in enumerate(node)]
        nodes, height = 1, 0
        for child, child_where in children:
            child_nodes, child_height = walk(child, depth + 1, child_where)
            nodes += child_nodes
            height = max(height, child_height + 1)
            if nodes > MAX_NODES:
                detail = f"{path}: more than {MAX_NODES} nodes (with aliases expanded)"
                raise UsageError("invalid config", detail=detail)
        seen[id(node)] = (nodes, height)
        return nodes, height

    walk(data, 0, "")


def _unsupported(value: _Unsupported, where: str, path: Path) -> UsageError:
    hint = "; quote it to use it as a string" if value.kind == "timestamp" else ""
    mark = value.mark
    at = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    detail = f"{where or 'top level'}: unsupported value type ({value.kind}){hint} (in {path}{at})"
    return UsageError("invalid config", detail=detail)


def _key_path(where: str, key: str) -> str:
    """``where.key``; a repo key that is not a valid repo id and an ``env.set`` key that is not
    a valid variable name are not echoed (they may hold credentials or control characters)."""
    if where == "repos" and not ids.is_repo_id(key):
        key = INVALID_REPO
    elif (where == "env.set" or where.endswith(".env.set")) and not ids.is_env_name(key):
        key = INVALID_ENV_NAME
    return f"{where}.{key}" if where else key


def _dropins(directory: Path) -> list[Path]:
    """``config.d/*.yaml`` (hidden names skipped) in byte order of file name. Entries that are
    not regular files are returned too, so reading them fails. A missing ``config.d`` has no
    drop-ins; one that exists but is not a directory (after symlinks) is an error."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        if os.path.lexists(directory):
            detail = f"{directory}: symlink does not resolve"
            raise UsageError("cannot read config", detail=detail) from None
        return []
    except NotADirectoryError:
        raise UsageError("invalid config", detail=f"{directory}: not a directory") from None
    except OSError as exc:
        raise UsageError("cannot read config", detail=f"{directory}: {exc.strerror}") from None
    names = [n for n in names if n.endswith(DROPIN_SUFFIX) and not n.startswith(".")]
    return [directory / name for name in sorted(names, key=os.fsencode)]


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
        if env[var] == "":
            detail = f"{key}: must not be empty (in environment variable {var})"
            raise UsageError("invalid config", detail=detail)
        problem: str | None = None
        try:
            raw[key] = convert(env[var])
        except ValueError as exc:
            problem = f"{key}: {exc} (in environment variable {var})"
        if problem is not None:  # raised outside the handler: no ``__context__``
            raise UsageError("invalid config", detail=problem)
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
    message: str = "invalid config",
) -> M:
    try:
        return model.model_validate(data, context={"bare": bare})
    except ValidationError as exc:
        problems = [_describe(error, source) for error in exc.errors()]
    # Raised outside the handler: the ValidationError (it holds the input values) must not become
    # its ``__context__``.
    raise UsageError(message, detail="; ".join(problems))


def _describe(error: Any, source: Callable[[tuple[str | int, ...]], str]) -> str:
    loc = tuple(part for part in error["loc"] if part != "[key]")
    key = ""
    for part in loc:
        key = _key_path(key, str(part))
    key = key or "top level"
    message = str(error["msg"]).removeprefix("Value error, ")
    if loc and loc[-1] == "sandbox" and isinstance(error.get("input"), bool):
        message += ' (quote "off" in YAML)'
    return f"{key}: {message} (in {source(loc)})"
