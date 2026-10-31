"""Generated Claude Code settings and ``claude`` argv (interface.md, Generated settings,
Bootstrap prompt and Launch flags; design.md, Generated settings and Prompt delivery).

Pure functions of the effective kind profile, the permission profile and the session paths:
nothing here reads the file system, the environment or the clock. ``exec`` resolves the
inputs (absolute, symlink-resolved paths) and writes the result with
``SessionDirs.write_settings``.

This module covers trusted runs. Isolated runs (third-party-reading, or ``sandbox`` not
``off``) are refused with ``NotImplementedError`` until the isolation rules land.

TODO(#15): exec calls build() in launch step 7 and writes the settings with
SessionDirs.write_settings before spawning claude. Before allocation, the step-5 launch
validations call check_rule_paths() (state root and claude-session path); a
LaunchValidationError there, or from build(), ends the run not-started with its end_reason
(unsafe_path, invalid_permission_profile).
TODO(#27): the dry-run serializer reports build()'s settings and argv.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import config, ids, sessiondir
from .errors import FailedError
from .kinds import Mode, Resolution

# Injected hooks: (event, matchers, timeout in seconds). ``None``: no matcher (everything).
# SubagentStop is not injected (live B3). PostToolUse and Stop drain the outbox: 60 s.
HOOKS: tuple[tuple[str, tuple[str | None, ...], int], ...] = (
    ("SessionStart", (None,), 30),
    ("UserPromptSubmit", (None,), 30),
    ("PermissionRequest", (None,), 30),
    ("Notification", ("permission_prompt", "idle_prompt", "elicitation_dialog"), 30),
    ("PostToolUse", (None,), 60),
    ("Stop", (None,), 60),
    ("SessionEnd", (None,), 30),
)
# Agent-visible files the agent must not change (design.md, Session directories); a
# trailing "/" marks a directory, denied with "/**".
PROTECTED = (
    sessiondir.SECRET,
    sessiondir.PROMPT,
    sessiondir.PROMPTS + "/",
    sessiondir.INSTRUCTIONS,
    sessiondir.RUN_JSON,
    sessiondir.INPUTS + "/",
    "gh-empty/",
    "empty-gitconfig",
)
_RULE_LISTS = ("allow", "deny", "ask")
# C0 and C1 controls and DEL.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Characters that would change the meaning of a path inside a permission rule (glob syntax,
# rule parentheses, escapes). A path containing one is refused rather than escaped.
_RULE_UNSAFE = re.compile(r"[*?\[\]{}()\\]")
# Characters ``shlex.quote`` leaves unquoted. A claude-session path with any other character
# (whitespace, shell metacharacters, non-ASCII) is invoked quoted, which the absolute
# ``Bash(<path> report:*)`` rule never matches; such a path is refused.
_SHELL_SAFE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")
# The permission modes Claude Code accepts; any other value makes it discard the whole
# settings file, injected hooks included.
DEFAULT_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
)
_HEADLESS_RESUME = "Continue the task. When you are finished, file your status report."


class LaunchValidationError(FailedError):
    """A launch validation failure: the run ends ``not-started`` with ``end_reason`` (a
    launch-phase end reason of interface.md, Outcome mapping)."""

    def __init__(self, message: str, *, end_reason: str) -> None:
        super().__init__(message)
        self.end_reason = end_reason


@dataclass(frozen=True)
class Paths:
    """The session paths of one run. ``state_root`` is absolute and symlink-resolved."""

    state_root: str
    csession_id: str
    run: int

    def __post_init__(self) -> None:
        if not ids.is_session_id(self.csession_id):
            raise ValueError(f"not a session id: {self.csession_id!r}")
        if isinstance(self.run, bool) or not isinstance(self.run, int) or self.run < 1:
            raise ValueError(f"run must be a positive integer, got {self.run!r}")

    @property
    def session_dir(self) -> str:
        return sessiondir.session_dir(self.state_root, self.csession_id)

    @property
    def private_root(self) -> str:
        return os.path.join(self.state_root, sessiondir.PRIVATE)

    @property
    def settings_file(self) -> str:
        private = sessiondir.private_dir(self.state_root, self.csession_id)
        return os.path.join(private, sessiondir.RUNS, str(self.run), sessiondir.SETTINGS)

    @property
    def instructions_file(self) -> str:
        return os.path.join(self.session_dir, sessiondir.INSTRUCTIONS)

    @property
    def work_dir(self) -> str:
        return os.path.join(self.session_dir, sessiondir.WORK)


@dataclass(frozen=True)
class Tools:
    """Absolute executables: the Python ``exec`` runs under (``current_interpreter()``), the
    ``claude-session`` executable and the resolved ``claude``."""

    interpreter: str
    claude_session: str
    claude: str


@dataclass(frozen=True)
class Launch:
    """The per-run launch facts the argv needs.

    - ``resume``: the run continues an existing session (``resume``, ``start --resume``); it
      selects the headless resume text of the bootstrap prompt.
    - ``resume_from``: the Claude session id passed to ``--resume``.
    - ``fork``: ``--fork-session``; needs ``resume_from`` and ``session_id``.
    - ``session_id``: a new Claude session id (``--session-id``): a fork's id, or a fresh
      conversation's.
    - ``prompt_path``: the run's prompt inside the session directory, if any.
    """

    mode: Mode
    cwd: str
    skill: str | None = None
    prompt_path: str | None = None
    resume: bool = False
    resume_from: str | None = None
    fork: bool = False
    session_id: str | None = None
    plugin_dirs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Argv:
    argv: list[str]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Generated:
    settings: dict[str, Any]
    argv: list[str]
    warnings: tuple[str, ...] = field(default=())


def current_interpreter(executable: str | None = None) -> str:
    """The interpreter the injected hooks run: ``sys.executable`` (or ``executable``) with
    its directory symlink-resolved but not its last component. A venv's ``bin/python`` is a
    symlink to the base interpreter; resolving it would lose the venv (``pyvenv.cfg`` is found
    next to the link), and the hook could no longer import ``claude_session``.

    TODO(#23): for isolated runs, refuse the launch (and adoption) when this path, its
    realpath, ``sys.prefix``/``sys.exec_prefix`` or the ``claude_session`` package directory
    is inside a session-writable path (design.md, Security notes)."""
    exe = sys.executable if executable is None else executable
    if not exe:
        raise FailedError("cannot determine the Python interpreter (sys.executable is empty)")
    exe = os.path.abspath(exe)
    return os.path.join(os.path.realpath(os.path.dirname(exe)), os.path.basename(exe))


def check_rule_paths(state_root: str, claude_session: str) -> None:
    """The paths the generated permission rules name must be usable there: absolute,
    normalized, valid UTF-8, free of control characters and of characters with a meaning in
    rules; the claude-session path also needs no shell quoting. Raises
    ``LaunchValidationError`` with end reason ``unsafe_path``."""
    try:
        _check_abs("state root", state_root, rule=True)
        _check_abs("claude-session path", claude_session, rule=True)
    except LaunchValidationError:
        raise
    except FailedError as exc:
        raise LaunchValidationError(exc.message, end_reason="unsafe_path") from exc
    if not _SHELL_SAFE.fullmatch(claude_session):
        shown = claude_session.encode("utf-8", "backslashreplace").decode("utf-8")
        raise LaunchValidationError(
            "claude-session path contains whitespace, a shell metacharacter or another "
            f"character that needs shell quoting: {shown!r}",
            end_reason="unsafe_path",
        )


def is_isolated(profile: config.Kind) -> bool:
    """A run is isolated when it is third-party-reading or its sandbox is not ``off``."""
    return profile.reads_third_party or profile.sandbox != "off"


def build(
    resolution: Resolution,
    permission_profile: config.PermissionProfile,
    paths: Paths,
    tools: Tools,
    launch: Launch,
) -> Generated:
    """The settings and argv of one run."""
    settings = build_settings(resolution, permission_profile, paths, tools, cwd=launch.cwd)
    built = build_argv(resolution, paths, tools, launch)
    return Generated(settings=settings, argv=built.argv, warnings=built.warnings)


# Settings


def build_settings(
    resolution: Resolution,
    permission_profile: config.PermissionProfile,
    paths: Paths,
    tools: Tools,
    *,
    cwd: str,
) -> dict[str, Any]:
    """The generated settings JSON of a trusted run: the permission profile's
    ``permissions`` plus the generated rules, an explicit ``defaultMode`` where the
    interface asks for one, and the injected hooks.

    ``permission_profile`` is the expanded profile (``${...}`` variables already
    substituted). Raises ``LaunchValidationError`` for a state root or claude-session path
    that cannot be put into a rule (``unsafe_path``) or a malformed profile
    (``invalid_permission_profile``), and ``FailedError`` for an unusable interpreter."""
    profile = resolution.profile
    if is_isolated(profile):
        # TODO(#23): sandbox settings and the isolation rules of interface.md, Generated
        # settings (isolated runs and third-party-reading runs); #24 adds the path rules.
        raise NotImplementedError("generated settings for isolated runs are not implemented")
    check_rule_paths(paths.state_root, tools.claude_session)
    _check_abs("interpreter", tools.interpreter)

    permissions = _profile_permissions(permission_profile)
    allow, deny = _generated_rules(paths, tools, cwd)
    permissions["allow"] = _merge(permissions.get("allow", []), allow)
    permissions["deny"] = _merge(permissions.get("deny", []), deny)
    mode = _default_mode(permission_profile, profile.setting_sources)
    if mode is not None:
        permissions["defaultMode"] = mode
    # A trusted run's sandbox is off: a profile's sandbox fragment is not emitted.
    return {"permissions": permissions, "hooks": _hooks(paths, tools)}


def settings_json(settings: Mapping[str, Any]) -> bytes:
    """The settings file's bytes (UTF-8, deterministic)."""
    return json.dumps(settings, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"


def _rule(path: str) -> str:
    """A Claude Code rule path: absolute paths are written ``//abs/path``."""
    return "/" + path


def _generated_rules(paths: Paths, tools: Tools, cwd: str) -> tuple[list[str], list[str]]:
    csdir = paths.session_dir
    allow = [
        "Bash(claude-session report:*)",
        f"Bash({tools.claude_session} report:*)",
        f"Read({_rule(csdir)}/**)",
    ]
    writable = [os.path.join(csdir, sessiondir.ARTIFACTS), os.path.join(csdir, sessiondir.OUTBOX)]
    if cwd == paths.work_dir:
        writable.append(paths.work_dir)
    for target in writable:
        # Live A8: a Write allow rule alone does not permit the Write tool; both are given.
        allow += [f"Edit({_rule(target)}/**)", f"Write({_rule(target)}/**)"]
    targets = [
        _rule(os.path.join(csdir, name.rstrip("/"))) + ("/**" if name.endswith("/") else "")
        for name in PROTECTED
    ]
    targets.append(f"{_rule(paths.private_root)}/**")
    deny = [f"{tool}({target})" for target in targets for tool in ("Edit", "Write")]
    return allow, deny


def _profile_permissions(profile: config.PermissionProfile) -> dict[str, Any]:
    # TODO(#22): the permission profile is resolved (name or file snapshot), validated and
    # its ${...} variables expanded before it gets here; validation at profile load must
    # apply the same value checks (defaultMode, string lists) as below.
    permissions = copy.deepcopy(dict(profile.permissions or {}))
    for key in (*_RULE_LISTS, "additionalDirectories"):
        if key not in permissions:
            continue
        value = permissions[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise LaunchValidationError(
                f"permission profile: permissions.{key} must be a list of strings",
                end_reason="invalid_permission_profile",
            )
    mode = permissions.get("defaultMode", "default")
    if not isinstance(mode, str) or mode not in DEFAULT_MODES:
        raise LaunchValidationError(
            "permission profile: permissions.defaultMode must be one of "
            + ", ".join(sorted(DEFAULT_MODES)),
            end_reason="invalid_permission_profile",
        )
    return permissions


def _default_mode(profile: config.PermissionProfile, setting_sources: Sequence[str]) -> str | None:
    """Trusted runs: the profile's mode; without one, ``default`` when ``setting_sources``
    excludes ``user`` (otherwise the user's settings decide)."""
    permissions = profile.permissions or {}
    if "defaultMode" in permissions:
        return str(permissions["defaultMode"])
    return None if "user" in setting_sources else "default"


def _merge(base: list[str], extra: list[str]) -> list[str]:
    merged = list(base)
    merged += [rule for rule in extra if rule not in merged]
    return merged


def _hooks(paths: Paths, tools: Tools) -> dict[str, list[dict[str, Any]]]:
    hooks: dict[str, list[dict[str, Any]]] = {}
    for event, matchers, timeout in HOOKS:
        command = shlex.join(
            [
                tools.interpreter,
                "-I",
                "-m",
                "claude_session",
                "hook-event",
                "--state-dir",
                paths.state_root,
                event,
            ]
        )
        groups = []
        for matcher in matchers:
            group: dict[str, Any] = {} if matcher is None else {"matcher": matcher}
            group["hooks"] = [{"type": "command", "command": command, "timeout": timeout}]
            groups.append(group)
        hooks[event] = groups
    return hooks


def _check_abs(label: str, path: str, *, rule: bool = False) -> None:
    """``path`` must be absolute and normalized, valid UTF-8 and free of control
    characters; with ``rule``, also free of characters with a meaning in permission rules."""
    if not isinstance(path, str) or not path:
        raise FailedError(f"{label} is empty")
    shown = path.encode("utf-8", "backslashreplace").decode("utf-8")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FailedError(f"{label} is not valid UTF-8: {shown!r}") from exc
    if _CONTROL.search(path):
        raise FailedError(f"{label} contains a control character: {shown!r}")
    if not os.path.isabs(path) or os.path.normpath(path) != path or path.startswith("//"):
        raise FailedError(f"{label} must be an absolute, normalized path: {shown!r}")
    if rule and _RULE_UNSAFE.search(path):
        raise FailedError(
            f"{label} contains a character that cannot appear in a permission rule "
            f"(one of * ? [ ] {{ }} ( ) \\): {shown!r}"
        )


# argv


def build_argv(resolution: Resolution, paths: Paths, tools: Tools, launch: Launch) -> Argv:
    """The ``claude`` argv (design.md, Generated settings: argv); the bootstrap prompt, if
    any, is the last argument, after ``--``. An interactive run ignores
    an explicit ``--max-turns`` with a warning."""
    profile = resolution.profile
    _check_abs("claude", tools.claude)
    _check_conversation(launch)
    warnings: list[str] = []
    argv = [
        tools.claude,
        "--settings",
        paths.settings_file,
        "--setting-sources",
        ",".join(profile.setting_sources),
    ]
    if profile.mcp == "none":
        argv.append("--strict-mcp-config")
    if profile.model is not None:
        if profile.model.startswith("-"):
            raise ValueError(f"model must not start with '-': {profile.model!r}")
        argv += ["--model", profile.model]
    argv += ["--add-dir", paths.session_dir]
    for plugin_dir in launch.plugin_dirs:
        argv += ["--plugin-dir", plugin_dir]
        if is_isolated(profile):
            # Live C6: the Read tool reaches plugin files only through --add-dir.
            argv += ["--add-dir", plugin_dir]
    argv += [
        "--append-system-prompt-file",
        paths.instructions_file,
        "--system-prompt-snapshot",
        "off",
    ]
    if launch.mode == "headless":
        argv += ["-p", "--output-format", "stream-json", "--verbose"]
        if profile.max_turns is not None:
            argv += ["--max-turns", str(profile.max_turns + 1)]
    elif profile.max_turns is not None and resolution.origins.get("max_turns") in (
        "flag",
        "previous_run",
    ):
        warnings.append("--max-turns applies to headless runs only; ignored in interactive mode")
    argv += profile.claude_args
    if launch.resume_from is not None:
        argv += ["--resume", launch.resume_from]
        if launch.fork:
            argv.append("--fork-session")
    if launch.session_id is not None:
        argv += ["--session-id", launch.session_id]
    message = bootstrap_prompt(
        paths.session_dir,
        mode=launch.mode,
        resume=launch.resume,
        skill=launch.skill,
        prompt_path=launch.prompt_path,
    )
    if message is not None:
        # "--" ends the options: a preceding variadic or optional-value option (from
        # claude_args, e.g. --allowedTools or --debug) would otherwise take the prompt.
        argv += ["--", message]
    return Argv(argv=argv, warnings=tuple(warnings))


def _check_conversation(launch: Launch) -> None:
    for name in ("resume_from", "session_id"):
        value = getattr(launch, name)
        if value is not None and not _is_uuid(value):
            raise ValueError(f"{name} must be a canonical UUID, got {value!r}")
    if launch.fork and (launch.resume_from is None or launch.session_id is None):
        raise ValueError("a fork needs resume_from and session_id")
    if launch.resume_from is not None and launch.session_id is not None and not launch.fork:
        raise ValueError("session_id with resume_from needs fork")


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError):
        return False


# Bootstrap prompt


def bootstrap_prompt(
    session_dir: str,
    *,
    mode: Mode,
    resume: bool,
    skill: str | None,
    prompt_path: str | None,
) -> str | None:
    """The first message of a run (interface.md, Bootstrap prompt), skill first so the text
    after the skill name is the skill's argument text. None: no initial message (an
    interactive run with neither skill nor prompt). A headless new session with neither is a
    usage error the caller rejects first (``ValueError`` here)."""
    if _CONTROL.search(session_dir) or not os.path.isabs(session_dir):
        raise ValueError(f"invalid session directory: {session_dir!r}")
    if skill is not None and not ids.is_skill_name(skill):
        raise ValueError(f"invalid skill name: {skill!r}")
    if prompt_path is not None:
        _check_prompt_path(session_dir, prompt_path)
    where = f"Your claude-session directory is {session_dir}."
    prefix = f"/{skill} " if skill is not None else ""
    if prompt_path is not None:
        return f"{prefix}{where} Read and follow {prompt_path}"
    if skill is not None:
        return f"{prefix}{where}"
    if mode == "interactive":
        return None
    if resume:
        return f"{where} {_HEADLESS_RESUME}"
    raise ValueError("a headless new session needs a skill or a prompt")


def _check_prompt_path(session_dir: str, path: str) -> None:
    if (
        _CONTROL.search(path)
        or not os.path.isabs(path)
        or os.path.normpath(path) != path
        or not path.startswith(session_dir.rstrip("/") + "/")
    ):
        raise ValueError(f"prompt path must be inside the session directory: {path!r}")
