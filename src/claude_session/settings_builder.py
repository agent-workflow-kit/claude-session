"""Generated Claude Code settings and ``claude`` argv (interface.md, Generated settings,
Bootstrap prompt and Launch flags; design.md, Generated settings and Prompt delivery).

``build``, ``build_settings`` and ``build_argv`` are pure functions of the effective kind
profile, the expanded permission profile (``permission_profiles.expand``), the session paths
and, for isolated runs, the ``Isolation`` inputs: they never read the file system, the
environment or the clock. ``exec`` resolves the inputs (absolute, symlink-resolved paths; the
helpers ``system_read_dirs``, ``tool_dirs`` and ``python_read_dirs`` at the end of this module
compute the generated read entries) and writes the result with ``SessionDirs.write_settings``.

Isolated runs (third-party-reading, or ``sandbox`` not ``off``) get the isolation rules of
interface.md, Generated settings, which always win over profile content. The sandbox is the
boundary; the permission rules are defense in depth. Every path the rules or the sandbox lists
name is checked to be absolute, normalized and free of glob and control characters here; the
path rules that compare paths with each other (unsafe write targets, credential paths, the
session-writable paths) are #24's.

TODO(#24): before build(), the launch validations refuse unsafe cwd, read_dirs, write_dirs,
plugin directories and generated read entries (Isolation fields), and pass the
session_writable check and the session-writable paths to check_interpreter().
TODO(#15): exec runs selfcheck.self_check() in launch step 5 (and at adoption): it creates
the private tmpdir (tmpdir.create) that becomes Isolation.tmpdir, gives sandbox_active, and
returns the warning of a `preferred` run without a working sandbox.

TODO(#15): exec calls build() in launch step 7 and writes the settings with
SessionDirs.write_settings before spawning claude. Before allocation, the step-5 launch
validations call check_rule_paths() (state root and claude-session path) and, for isolated
runs, check_cwd() before permission_profiles.expand(), so a bad cwd fails unsafe_cwd whether
or not the profile uses ${CWD}; a LaunchValidationError there, or from build(), ends the run
not-started with its end_reason (unsafe_path, unsafe_cwd, unsafe_setting_sources,
invalid_permission_profile, ...).
TODO(#27): the dry-run serializer reports build()'s settings and argv.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import stat
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import config, ids, permission_profiles, sessiondir
from .errors import FailedError, LaunchValidationError
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
    sessiondir.GH_EMPTY + "/",
    sessiondir.EMPTY_GITCONFIG,
)
# Isolated runs: the fixed system directories of the sandbox allowRead list (missing ones are
# skipped by system_read_dirs, which adds their realpaths too). /opt is deliberately not here
# (design.md, Generated settings).
SYSTEM_READ_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc")
# Home-directory credential locations denied to the Read tool (conservative until live F1 is
# verified); a trailing "/" marks a directory, denied with "/**".
HOME_READ_DENIES = (
    ".ssh/",
    ".aws/",
    ".gnupg/",
    ".kube/",
    ".docker/",
    ".config/",
    ".claude/",
    ".netrc",
    ".git-credentials",
    ".local/state/",
)
# Claude Code project configuration, write-denied under $CSESSION_DIR, every --add-dir and
# (third-party-reading runs) the cwd; a trailing "/" marks a directory.
CLAUDE_CONFIG = (".claude/", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md")
FILE_TOOLS = ("Edit", "Write")
# Characters a path in a sandbox filesystem list must not contain: glob syntax (single-* globs
# in denyWrite are silently ignored, live C2) and escapes.
_SANDBOX_UNSAFE = re.compile(r"[*?\[\]{}\\]")
_SANDBOX_LEVELS = {"off": 0, "preferred": 1, "required": 2}
# Sandbox escape keys the generated sandbox block sets to false explicitly: --settings outranks
# user settings, so a user setting cannot turn them on (a profile may not set them at all).
SANDBOX_FALSE_KEYS = (
    "allowNestedUserNamespaces",
    "allowAppleEvents",
    "enableWeakerNestedSandbox",
    "enableWeakerNetworkIsolation",
)
# Isolated runs: permission modes that would let an unattended run prompt (network or tool
# prompts under a lower mode) or skip the rules; disabled explicitly (verification: #41).
DISABLED_MODES = ("disableAutoMode", "disableBypassPermissionsMode")
# Largest .pth file check_interpreter reads (site.py reads any size; a larger one is refused).
_MAX_PTH_BYTES = 1 << 20
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
class Isolation:
    """The inputs only isolated runs need. Every path is absolute, normalized and
    symlink-resolved by the caller, and has passed #24's path rules.

    - ``home``: ``$HOME`` (the Read denies of credential locations).
    - ``tmpdir``: the private temporary directory (required when the sandbox is not ``off``);
      it goes to ``allowWrite`` only, never ``allowRead`` (live E1).
    - ``git_dir``: the cwd's git common directory; None when the cwd has none.
      ``worktree_git_dir``: a linked worktree's own gitdir (needs ``git_dir``).
    - ``read_dirs``, ``write_dirs``: the kind's (expanded, resolved; missing write_dirs
      already skipped).
    - ``system_dirs`` (``system_read_dirs()``), ``tool_dirs`` (``tool_dirs()`` of the resolved
      bwrap, socat, git and claude), ``python_dirs`` (``python_read_dirs()``) and
      ``path_prepend`` (the kind's ``env.path_prepend``, resolved): the generated read
      entries.
    """

    home: str
    tmpdir: str | None = None
    git_dir: str | None = None
    worktree_git_dir: str | None = None
    read_dirs: tuple[str, ...] = ()
    write_dirs: tuple[str, ...] = ()
    system_dirs: tuple[str, ...] = ()
    tool_dirs: tuple[str, ...] = ()
    python_dirs: tuple[str, ...] = ()
    path_prepend: tuple[str, ...] = ()


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
    next to the link), and the hook could no longer import ``claude_session``. Isolated runs
    also pass it to ``check_interpreter``."""
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


def check_cwd(cwd: str) -> None:
    """Isolated runs: the cwd appears in permission rules and sandbox lists, so it must be
    absolute, normalized, valid UTF-8 and free of control and rule characters. Raises
    ``LaunchValidationError`` (``unsafe_cwd``). The launch path calls it before
    ``permission_profiles.expand`` (so a bad cwd fails ``unsafe_cwd`` whether or not the
    profile uses ``${CWD}``); ``build_settings`` repeats it first for isolated runs."""
    _check_input("cwd", cwd, "unsafe_cwd", rule=True)


def check_interpreter(
    interpreter: str,
    *,
    session_writable: Callable[[str], bool],
    writable_paths: Sequence[str],
    prefixes: Sequence[str] | None = None,
    stdlib_dirs: Sequence[str] | None = None,
    package_dir: str | None = None,
    sys_path: Sequence[str] | None = None,
    site_dirs: Sequence[str] | None = None,
    link_dirs: Sequence[str] | None = None,
) -> None:
    """Isolated runs (launch and adoption): nothing the unsandboxed hook interpreter loads code
    from may be changeable by the sandboxed agent, or it could replace the code the next hook
    runs outside the sandbox (design.md, Security notes). Raises ``LaunchValidationError``
    (``unsafe_path``).

    The candidates (stricter than what ``python -I`` actually loads; nothing is run): the hook
    interpreter path and its realpath; the ``prefixes`` (default ``sys.prefix``,
    ``sys.exec_prefix``, ``sys.base_prefix``, ``sys.base_exec_prefix``); the ``stdlib_dirs``
    (default ``sysconfig``'s ``stdlib`` and ``platstdlib``); the ``claude_session`` package
    directory; every absolute ``sys_path`` entry (default this process's ``sys.path``); the
    ``site_dirs`` (default ``site.getsitepackages()`` plus the user site when enabled), every
    path line of their ``*.pth`` files (``site`` adds those to ``sys.path``; ``import`` lines
    are code inside the checked file) and the realpath of each symlinked ``.pth`` file. A
    ``.pth`` file that cannot be opened is skipped, as ``site`` skips it. A candidate is
    refused when ``session_writable`` (#24's check, comparing realpaths) accepts it. A
    code-bearing directory (stdlib, package, ``sys_path`` entry, site dir, ``.pth`` path entry)
    is also refused when one of the ``writable_paths`` (the session-writable paths, realpaths;
    required, so the check is never skipped by omission) is the directory or inside it; the
    interpreter, the prefixes and the ``.pth`` files are checked one way only, so a writable
    ``/usr/src/app`` under the ``/usr`` prefix passes.

    Then every regular file under ``link_dirs`` must have a single link (``st_nlink`` 1): a
    hard link elsewhere (``uv`` links venv files to its cache by default) may be writable by
    the agent. Default: the package directory and, in a virtual environment
    (``sys.prefix != sys.base_prefix``), its own site-packages.

    TODO(#15): exec calls this for isolated runs at launch, and #28 at adoption, with
    current_interpreter(), #24's session_writable check and the session-writable paths."""
    import site

    if prefixes is None:
        prefixes = (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)
    if stdlib_dirs is None:
        stdlib_dirs = _stdlib_dirs()
    if package_dir is None:
        package_dir = os.path.dirname(os.path.abspath(__file__))
    if sys_path is None:
        sys_path = tuple(sys.path)
    if site_dirs is None:
        site_dirs = _site_dirs()
    if link_dirs is None:
        link_dirs = [package_dir]
        if sys.prefix != sys.base_prefix:
            venv = os.path.realpath(sys.prefix)
            link_dirs += [d for d in site.getsitepackages() if _inside(os.path.realpath(d), venv)]
    pth_entries, pth_files = _pth_entries(site_dirs)
    # (label, path, code-bearing directory: also checked for a writable path inside it)
    candidates = [
        ("hook interpreter", interpreter, False),
        ("hook interpreter (resolved)", os.path.realpath(interpreter), False),
        *(("Python prefix", prefix, False) for prefix in prefixes),
        *(("standard library directory", entry, True) for entry in stdlib_dirs),
        ("claude_session package directory", package_dir, True),
        *(("sys.path entry", entry, True) for entry in sys_path if entry and os.path.isabs(entry)),
        *(("site-packages directory", entry, True) for entry in site_dirs),
        *((".pth path entry", entry, True) for entry in pth_entries),
        *((".pth file (resolved)", entry, False) for entry in pth_files),
    ]
    for label, path, code_dir in candidates:
        if session_writable(path):
            raise LaunchValidationError(
                f"{label} is inside a session-writable path: {_shown(path)!r}",
                end_reason="unsafe_path",
            )
        if code_dir and _overlaps(path, writable_paths):
            raise LaunchValidationError(
                f"{label} contains a session-writable path: {_shown(path)!r}",
                end_reason="unsafe_path",
            )
    check_hard_links(link_dirs)


def check_hard_links(dirs: Sequence[str]) -> None:
    """Every regular file below ``dirs`` (symlinks not followed) must have exactly one link.
    Raises ``LaunchValidationError`` (``unsafe_path``) naming the file and the fix."""
    for top in dirs:
        for root, _dirs, files in os.walk(top):
            for name in files:
                path = os.path.join(root, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    raise LaunchValidationError(
                        f"{_shown(path)!r} has {info.st_nlink} hard links: the code of the "
                        "unsandboxed hook could be changed through another link; reinstall "
                        "claude-session with `uv tool install --link-mode=copy` (or set "
                        "UV_LINK_MODE=copy)",
                        end_reason="unsafe_path",
                    )


def _overlaps(candidate: str, writable_paths: Sequence[str]) -> bool:
    """Whether a session-writable path is ``candidate`` (or its realpath) or inside it: the
    other direction of the session-writable check.

    TODO(#24): fold into #24's session-writable predicate."""
    forms = {os.path.normpath(candidate), os.path.realpath(candidate)}
    return any(_inside(os.path.realpath(path), form) for path in writable_paths for form in forms)


def _stdlib_dirs() -> list[str]:
    import sysconfig

    paths = sysconfig.get_paths()
    return list(dict.fromkeys(paths[key] for key in ("stdlib", "platstdlib") if key in paths))


def _site_dirs() -> list[str]:
    import site

    dirs = list(site.getsitepackages())
    if site.ENABLE_USER_SITE:
        dirs.append(site.getusersitepackages())
    return dirs


def _pth_entries(site_dirs: Sequence[str]) -> tuple[list[str], list[str]]:
    """The path lines of the ``*.pth`` files in ``site_dirs`` (relative ones joined to their
    directory), as ``site`` would add them; comment, blank and ``import`` lines skipped. Also
    the realpaths of the symlinked ``.pth`` files read (the agent could rewrite one whose
    target it can write). A ``.pth`` file that cannot be opened (symlinks followed) is skipped,
    as ``site.addpackage`` skips it; an oversized one is refused (``unsafe_path``): what it
    adds is unknown."""
    entries: list[str] = []
    linked: list[str] = []
    for directory in site_dirs:
        try:
            names = sorted(n for n in os.listdir(directory) if n.endswith(".pth"))
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                with open(path, "rb") as handle:
                    raw = handle.read(_MAX_PTH_BYTES + 1)
            except OSError:
                continue  # As site.addpackage: a .pth file it cannot open adds nothing.
            if os.path.islink(path):
                linked.append(os.path.realpath(path))
            if len(raw) > _MAX_PTH_BYTES:
                raise LaunchValidationError(
                    f".pth file {_shown(path)!r} is larger than 1 MiB", end_reason="unsafe_path"
                )
            for line in raw.decode("utf-8", "replace").splitlines():
                # As site.addpackage reads them.
                if line.startswith("#") or not line.strip():
                    continue
                if line.startswith(("import ", "import\t")):
                    continue
                line = line.rstrip()
                entries.append(os.path.normpath(os.path.join(directory, line)))
    return entries, linked


def is_isolated(profile: config.Kind) -> bool:
    """A run is isolated when it is third-party-reading or its sandbox is not ``off``."""
    return profile.reads_third_party or profile.sandbox != "off"


ProfileContent = Mapping[str, Any] | config.PermissionProfile


def build(
    resolution: Resolution,
    permission_profile: ProfileContent,
    paths: Paths,
    tools: Tools,
    launch: Launch,
    *,
    isolation: Isolation | None = None,
) -> Generated:
    """The settings and argv of one run (``isolation``: required for isolated runs)."""
    settings = build_settings(
        resolution,
        permission_profile,
        paths,
        tools,
        cwd=launch.cwd,
        plugin_dirs=launch.plugin_dirs,
        isolation=isolation,
    )
    built = build_argv(resolution, paths, tools, launch)
    return Generated(settings=settings, argv=built.argv, warnings=built.warnings)


# Settings


def build_settings(
    resolution: Resolution,
    permission_profile: ProfileContent,
    paths: Paths,
    tools: Tools,
    *,
    cwd: str,
    plugin_dirs: Sequence[str] = (),
    isolation: Isolation | None = None,
) -> dict[str, Any]:
    """The generated settings JSON of a run: the permission profile's ``permissions`` plus
    the generated rules, ``defaultMode`` as the interface asks, and the injected hooks; for
    isolated runs also the isolation rules and the ``sandbox`` block (``_isolated``).

    ``permission_profile`` is the expanded profile content (``permission_profiles.expand``;
    a ``config.PermissionProfile`` is accepted too). Raises ``LaunchValidationError`` for a
    path that cannot be put into a rule (``unsafe_path`` and the per-input reasons of
    ``_isolated``), a malformed profile (``invalid_permission_profile``, also as its subclass
    ``InvalidPermissionProfile`` from the isolated-run key backstop) and, for
    third-party-reading runs, setting sources (``unsafe_setting_sources``); ``FailedError``
    for an unusable interpreter and ``ValueError`` for an isolated run without
    ``isolation``."""
    profile = resolution.profile
    content = _content(permission_profile)
    check_rule_paths(paths.state_root, tools.claude_session)
    _check_abs("interpreter", tools.interpreter)
    if is_isolated(profile):
        if isolation is None:
            raise ValueError("isolated runs need the isolation inputs")
        return _isolated(resolution, content, paths, tools, cwd, tuple(plugin_dirs), isolation)

    permissions = _profile_permissions(content)
    allow, deny = _generated_rules(paths, tools, cwd)
    permissions["allow"] = _merge(permissions.get("allow", []), allow)
    permissions["deny"] = _merge(permissions.get("deny", []), deny)
    mode = _default_mode(permissions, profile.setting_sources)
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


def _content(profile: ProfileContent) -> dict[str, Any]:
    """The profile as a plain mapping of its set sections (a deep copy)."""
    if isinstance(profile, config.PermissionProfile):
        return {
            key: copy.deepcopy(value)
            for key in permission_profiles.SECTIONS
            if (value := getattr(profile, key)) is not None
        }
    return copy.deepcopy(dict(profile))


def _profile_permissions(content: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the profile's ``permissions`` after the value checks Claude Code needs
    (``permission_profiles.validate`` applies them at profile load; this is the backstop)."""
    section = content.get("permissions") or {}
    if not isinstance(section, Mapping):
        raise LaunchValidationError(
            "permission profile: permissions must be a mapping",
            end_reason="invalid_permission_profile",
        )
    permissions = copy.deepcopy(dict(section))
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


def _default_mode(permissions: Mapping[str, Any], setting_sources: Sequence[str]) -> str | None:
    """Trusted runs: the profile's mode; without one, ``default`` when ``setting_sources``
    excludes ``user`` (otherwise the user's settings decide)."""
    if "defaultMode" in permissions:
        return str(permissions["defaultMode"])
    return None if "user" in setting_sources else "default"


def _merge(base: Sequence[str], extra: Sequence[str]) -> list[str]:
    """``base`` then the entries of ``extra`` not seen yet (duplicates in ``extra`` too)."""
    merged = list(base)
    for item in extra:
        if item not in merged:
            merged.append(item)
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


# Isolated runs (interface.md, Generated settings: isolated and third-party-reading runs)


def _isolated(
    resolution: Resolution,
    content: dict[str, Any],
    paths: Paths,
    tools: Tools,
    cwd: str,
    plugin_dirs: tuple[str, ...],
    iso: Isolation,
) -> dict[str, Any]:
    """The settings of an isolated run: the expanded profile, then the generated isolation
    keys, which always win over profile content (a deny beats any allow; the generated scalar
    keys replace the profile's)."""
    profile = resolution.profile
    third_party = profile.reads_third_party
    check_cwd(cwd)  # first: a bad cwd is unsafe_cwd whatever else is wrong
    check_setting_sources(resolution)
    # Backstop: select() refused these already; a profile must never undo the isolation keys.
    permission_profiles.validate(content, isolated=True)
    _check_isolation_inputs(content, paths, cwd, plugin_dirs, iso)
    csdir = paths.session_dir
    git_dirs = [d for d in (iso.git_dir, iso.worktree_git_dir) if d is not None]
    # The git directory is read-only on third-party-reading runs only (design.md, Git
    # directory); ${CWD}/.git is denied there always, and on other isolated runs only when
    # the cwd has no git directory (no planted repository).
    denied_git_dirs = git_dirs if third_party else []
    dot_git = os.path.join(cwd, ".git")
    deny_dot_git = third_party or iso.git_dir is None
    # Claude config is denied under $CSESSION_DIR and every --add-dir; third-party: the cwd.
    config_roots = [csdir, *plugin_dirs, *([cwd] if third_party else [])]

    permissions = _profile_permissions(content)
    allow, deny = _generated_rules(paths, tools, cwd)
    for plugin in plugin_dirs:
        # Live C6: the Read tool reaches plugin files only through --add-dir, which makes
        # the directory writable: Edit/Write denies (and sandbox denyWrite) undo that.
        allow.append(f"Read({_rule(plugin)}/**)")
        deny += _file_denies(f"{_rule(plugin)}/**")
    for git_dir in denied_git_dirs:
        deny += _file_denies(f"{_rule(git_dir)}/**")
    if deny_dot_git:
        deny += _file_denies(_rule(dot_git)) + _file_denies(f"{_rule(dot_git)}/**")
    for root in config_roots:
        for target in _claude_config_targets(root):
            deny += _file_denies(target)
    deny += [f"Read({target})" for target in _read_deny_targets(iso.home, paths, keep=(cwd, csdir))]
    permissions["allow"] = _merge(permissions.get("allow", []), allow)
    permissions["deny"] = _merge(permissions.get("deny", []), deny)
    permissions["defaultMode"] = "dontAsk"  # a profile's mode is ignored
    permissions["blockReadsOutsideWorkingDirectories"] = True
    for key in DISABLED_MODES:
        permissions[key] = "disable"

    allow_read = [
        cwd,
        csdir,
        *iso.system_dirs,
        *iso.tool_dirs,
        *iso.python_dirs,
        *iso.read_dirs,
        *plugin_dirs,
        *iso.path_prepend,
        *git_dirs,
    ]
    # work/ only when it is the cwd, like its Edit/Write allows (narrower than the
    # session-writable paths, which always include it).
    allow_write = [
        cwd,
        os.path.join(csdir, sessiondir.OUTBOX),
        os.path.join(csdir, sessiondir.ARTIFACTS),
        *iso.write_dirs,
        *([iso.tmpdir] if iso.tmpdir is not None else []),
    ]
    deny_write = [
        *(os.path.join(csdir, name.rstrip("/")) for name in PROTECTED),
        paths.private_root,
        *denied_git_dirs,
        *([dot_git] if deny_dot_git else []),
        *plugin_dirs,
        *(os.path.join(csdir, name.rstrip("/")) for name in CLAUDE_CONFIG),
        *(os.path.join(cwd, name.rstrip("/")) for name in CLAUDE_CONFIG if third_party),
    ]
    sandbox = _sandbox(
        content.get("sandbox") or {},
        profile.sandbox,
        tmpdir=iso.tmpdir,
        allow_read=allow_read,
        allow_write=allow_write,
        deny_write=deny_write,
    )
    return {"permissions": permissions, "sandbox": sandbox, "hooks": _hooks(paths, tools)}


def _sandbox(
    section: Mapping[str, Any],
    mode: str,
    *,
    tmpdir: str | None,
    allow_read: list[str],
    allow_write: list[str],
    deny_write: list[str],
) -> dict[str, Any]:
    """The ``sandbox`` block: the profile's section (validated), then the generated keys.
    ``enabled`` is ``true`` unless the mode is ``off`` (then ``false``, so user or managed
    settings cannot enable a sandbox without the private tmpdir); ``failIfUnavailable`` only
    when the mode is not ``off``; the other keys always (inert without a sandbox)."""
    if mode != "off" and tmpdir is None:
        raise ValueError("a sandboxed run needs its private tmpdir")
    sandbox = copy.deepcopy(dict(section))
    sandbox["enabled"] = mode != "off"
    if mode != "off":
        sandbox["failIfUnavailable"] = mode == "required"
    sandbox["allowUnsandboxedCommands"] = False
    # Live D3: true (Claude Code's default) runs sandboxed Bash without the allow rules.
    sandbox["autoAllowBashIfSandboxed"] = False
    for key in SANDBOX_FALSE_KEYS:
        sandbox[key] = False
    network = dict(sandbox.get("network") or {})
    network.setdefault("allowedDomains", [])
    # No prompt for a domain outside the allowlist (an interactive run could approve one).
    network["strictAllowlist"] = True
    sandbox["network"] = network
    filesystem = dict(sandbox.get("filesystem") or {})
    filesystem["disabled"] = False
    # Live E1: deny-root reads plus the allowRead list; the tmpdir in allowWrite only.
    filesystem["denyRead"] = _merge(filesystem.get("denyRead", []), ["/"])
    filesystem["allowRead"] = _merge(filesystem.get("allowRead", []), allow_read)
    filesystem["allowWrite"] = _merge(filesystem.get("allowWrite", []), allow_write)
    filesystem["denyWrite"] = _merge(filesystem.get("denyWrite", []), deny_write)
    sandbox["filesystem"] = filesystem
    return sandbox


def _file_denies(target: str) -> list[str]:
    return [f"{tool}({target})" for tool in FILE_TOOLS]


def _claude_config_targets(root: str) -> list[str]:
    """Rule targets of the Claude config under ``root``: top-level and at any depth."""
    targets = []
    for name in CLAUDE_CONFIG:
        pattern = name.rstrip("/") + ("/**" if name.endswith("/") else "")
        targets += [f"{_rule(root)}/{pattern}", f"{_rule(root)}/**/{pattern}"]
    return targets


def _read_deny_targets(home: str, paths: Paths, *, keep: Sequence[str]) -> list[str]:
    """The Read-deny targets: home credential locations, the state root and the private
    directory, except a prefix that is or contains a ``keep`` path (the cwd and
    ``$CSESSION_DIR`` stay readable; the default state root under ``~/.local/state`` therefore
    never gets a deny, and the private directory deny covers the rest of the state root that
    matters)."""
    prefixes = [
        (os.path.join(home, name.rstrip("/")), name.endswith("/")) for name in HOME_READ_DENIES
    ]
    prefixes += [(paths.state_root, True), (paths.private_root, True)]
    targets = []
    for path, is_dir in prefixes:
        if any(_inside(kept, path) for kept in keep):
            continue
        targets.append(_rule(path) + ("/**" if is_dir else ""))
    return targets


def _inside(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or below it (both normalized absolute paths)."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _check_isolation_inputs(
    content: Mapping[str, Any],
    paths: Paths,
    cwd: str,
    plugin_dirs: Sequence[str],
    iso: Isolation,
) -> None:
    """Every path the isolation rules and sandbox lists name: absolute, normalized, valid
    UTF-8, no control characters; no rule characters for paths in permission rules, no glob
    characters for sandbox-only paths; nothing re-opened for reading inside the private
    tmpdir (live E1: it would be mounted read-only and break every Bash call); and no
    allowRead or allowWrite entry that is, or contains, the state root, the private root or
    the per-uid tmp parent (``dirname(tmpdir)``, other sessions' proxy sockets)."""
    rule_paths: list[tuple[str, str, Iterable[str]]] = [
        ("cwd", "unsafe_cwd", [cwd]),
        ("plugin directory", "unsafe_plugin_dir", plugin_dirs),
        ("HOME", "unsafe_path", [iso.home]),
        ("git directory", "unsafe_read_dir", [p for p in (iso.git_dir, iso.worktree_git_dir) if p]),
    ]
    for label, reason, values in rule_paths:
        for value in values:
            _check_input(label, value, reason, rule=True)
    if iso.git_dir is not None and iso.git_dir == "":
        _check_input("git directory", "", "unsafe_read_dir", rule=True)
    if iso.worktree_git_dir is not None and not iso.git_dir:
        raise ValueError("a worktree gitdir needs the git directory")
    if iso.tmpdir is not None:
        _check_input("private tmpdir", iso.tmpdir, "tmpdir_unsafe", rule=False)
    sandbox_paths: list[tuple[str, str, Sequence[str]]] = [
        ("read_dirs entry", "invalid_permission_profile", iso.read_dirs),
        ("write_dirs entry", "invalid_permission_profile", iso.write_dirs),
        ("system read directory", "unsafe_read_dir", iso.system_dirs),
        ("tool directory", "unsafe_read_dir", iso.tool_dirs),
        ("Python read directory", "unsafe_read_dir", iso.python_dirs),
        ("env.path_prepend directory", "unsafe_read_dir", iso.path_prepend),
    ]
    for label, reason, values in sandbox_paths:
        for value in values:
            _check_input(label, value, reason, rule=False)
    filesystem = (content.get("sandbox") or {}).get("filesystem") or {}
    profile_reads = [v for v in filesystem.get("allowRead") or [] if isinstance(v, str)]
    profile_writes = [v for v in filesystem.get("allowWrite") or [] if isinstance(v, str)]
    # TODO(#24): the general path predicate (unsafe write targets, credential paths, the
    # caller's cwd) covers these entries too; this checks only the state-root containment.
    roots = [paths.state_root, paths.private_root]
    if iso.tmpdir is not None:
        roots.append(os.path.dirname(iso.tmpdir))
    allowed: list[tuple[str, str, Sequence[str]]] = [
        ("cwd", "unsafe_cwd", [cwd]),
        ("plugin directory", "unsafe_plugin_dir", plugin_dirs),
        ("read_dirs entry", "invalid_permission_profile", iso.read_dirs),
        ("write_dirs entry", "invalid_permission_profile", iso.write_dirs),
        ("profile allowRead entry", "invalid_permission_profile", profile_reads),
        ("profile allowWrite entry", "invalid_permission_profile", profile_writes),
        ("system read directory", "unsafe_read_dir", iso.system_dirs),
        ("tool directory", "unsafe_read_dir", iso.tool_dirs),
        ("Python read directory", "unsafe_read_dir", iso.python_dirs),
        ("env.path_prepend directory", "unsafe_read_dir", iso.path_prepend),
        (
            "git directory",
            "unsafe_read_dir",
            [p for p in (iso.git_dir, iso.worktree_git_dir) if p],
        ),
    ]
    for label, reason, values in allowed:
        for value in values:
            entry = os.path.normpath(value)
            if any(_inside(root, entry) for root in roots):
                raise LaunchValidationError(
                    f"{label} is or contains the state root, the private directory or the"
                    f" private temporary directories' parent: {_shown(value)!r}",
                    end_reason=reason,
                )
    if iso.tmpdir is None:
        return
    readable: list[tuple[str, str, Sequence[str]]] = [
        ("read_dirs entry", "invalid_permission_profile", iso.read_dirs),
        ("profile allowRead entry", "invalid_permission_profile", profile_reads),
        ("system read directory", "unsafe_read_dir", iso.system_dirs),
        ("tool directory", "unsafe_read_dir", iso.tool_dirs),
        ("Python read directory", "unsafe_read_dir", iso.python_dirs),
        ("env.path_prepend directory", "unsafe_read_dir", iso.path_prepend),
    ]
    for label, reason, values in readable:
        for value in values:
            if _inside(os.path.normpath(value), iso.tmpdir):
                raise LaunchValidationError(
                    f"{label} is inside the private tmpdir, which must stay out of allowRead:"
                    f" {_shown(value)!r}",
                    end_reason=reason,
                )


def _check_input(label: str, value: str, reason: str, *, rule: bool) -> None:
    try:
        _check_abs(label, value, rule=rule)
        if not rule and _SANDBOX_UNSAFE.search(value):
            raise FailedError(
                f"{label} contains a glob or escape character (one of * ? [ ] {{ }} \\):"
                f" {_shown(value)!r}"
            )
    except FailedError as exc:
        raise LaunchValidationError(exc.message, end_reason=reason) from exc


def _shown(path: str) -> str:
    return path.encode("utf-8", "backslashreplace").decode("utf-8")


# Sandbox baseline (interface.md, Generated settings: Sandbox baseline)


def check_setting_sources(resolution: Resolution) -> None:
    """Third-party-reading runs load no Claude Code settings source: project and local
    settings come from the (third-party) checkout, whose hooks run unsandboxed, and the list
    keys of any source (``excludedCommands``, ``allowRead``, ``additionalDirectories``) merge
    with the generated ones. Raises ``LaunchValidationError`` (``unsafe_setting_sources``)."""
    profile = resolution.profile
    if profile.reads_third_party and profile.setting_sources:
        raise LaunchValidationError(
            "third-party-reading runs load no setting sources (setting_sources must be empty),"
            f" got: {', '.join(profile.setting_sources)}",
            end_reason="unsafe_setting_sources",
        )


def isolation_warnings(resolution: Resolution) -> tuple[str, ...]:
    """The isolation warnings of a run: for a third-party-reading run below the ``required``
    sandbox baseline, ``sandbox_lowered`` and, for ``off``, that it runs without a sandbox;
    for another isolated run with setting sources, that their list keys merge with the
    generated ones. (A ``preferred`` run whose sandbox turns out unavailable gets its warning
    from ``selfcheck.self_check``.)"""
    profile = resolution.profile
    warnings: list[str] = []
    if is_isolated(profile) and not profile.reads_third_party and profile.setting_sources:
        warnings.append(
            f"isolated run with setting sources {', '.join(profile.setting_sources)}: their"
            " sandbox and permission list keys (excludedCommands, allowRead, allowWrite,"
            " additionalDirectories) merge with the generated isolation settings, and their"
            " hooks run outside the sandbox"
        )
    if not resolution.sandbox_lowered():
        return tuple(warnings)
    mode = profile.sandbox
    origin = resolution.origins.get("sandbox", "builtin")
    warnings.append(
        f"sandbox_lowered: third-party-reading run with sandbox {mode!r} (from {origin});"
        " the baseline is 'required'"
    )
    if mode == "off":
        warnings.append(
            "third-party-reading run without a sandbox: only the evadable permission rules"
            " isolate it"
        )
    return tuple(warnings)


def sandbox_lowered_inputs(resolution: Resolution, *, run: int) -> dict[str, Any]:
    """The ``inputs`` of the ``sandbox_lowered`` audit record (schemas/audit.json).

    TODO(#15): exec writes the audit record (and the sandbox_lowered log event) at adoption
    with the #7 writers, and adds isolation_warnings() to the run's warnings; #28 does the
    same when start's pending run is adopted."""
    if not resolution.sandbox_lowered():
        raise ValueError("the run's sandbox is not lowered")
    return {
        "run_kind": resolution.kind,
        "run": run,
        "sandbox_from": "required",
        "sandbox_to": resolution.profile.sandbox,
    }


# Generated read entries: these helpers read the file system (exec calls them).


def system_read_dirs(candidates: Sequence[str] = SYSTEM_READ_DIRS) -> tuple[str, ...]:
    """The fixed system directories that exist, each as given and then symlink-resolved
    (deduplicated, in that order): on a usr-merged system ``/bin`` is a symlink to
    ``/usr/bin``, and the loader and ``/bin/sh`` are reached through the literal path."""
    return _existing_unique(form for path in candidates for form in (path, os.path.realpath(path)))


def tool_dirs(tools: Iterable[str | None]) -> tuple[str, ...]:
    """The directories of the resolved tools (``bwrap``, ``socat``, ``git``, ``claude``;
    None: not resolved), each the directory of the tool's realpath."""
    return _existing_unique(
        os.path.dirname(os.path.realpath(tool)) for tool in tools if tool is not None
    )


def python_read_dirs(
    *,
    executable_dir: str | None = None,
    package_dir: str | None = None,
    prefixes: Sequence[str] | None = None,
    sys_path: Sequence[str] | None = None,
    user_site: str | None | bool = True,
) -> tuple[str, ...]:
    """What sandboxed ``claude-session report`` needs to read (design.md, Python allowRead):
    the directory of the ``claude-session`` executable, the package directory, ``sys.prefix``,
    ``sys.exec_prefix``, ``sys.base_prefix`` and ``sys.base_exec_prefix`` (``prefixes``, in
    that order; a missing base prefix falls back to
    ``dirname(dirname(realpath(<prefix>/bin/python)))``), the ``sys.path`` entries outside
    those, and the user site directory when it is in use. Every entry is realpath-resolved
    and missing ones are skipped. Empty and relative ``sys.path`` entries are dropped first;
    absolute ones are kept even inside the caller's cwd (``check_interpreter`` refuses one
    inside a session-writable path, and #24's generated-entry checks refuse the caller's
    cwd). ``user_site=True``: this interpreter's user site when enabled and on ``sys.path``."""
    import site

    if executable_dir is None:
        executable_dir = os.path.dirname(os.path.abspath(sys.argv[0] or sys.executable))
    if package_dir is None:
        package_dir = os.path.dirname(os.path.abspath(__file__))
    if prefixes is None:
        prefixes = (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)
    if sys_path is None:
        sys_path = tuple(sys.path)
    if user_site is True:
        used = site.ENABLE_USER_SITE and site.USER_SITE in sys_path
        user_site = site.USER_SITE if used else None
    resolved_prefixes = []
    for prefix in prefixes:
        if os.path.isdir(prefix):
            resolved_prefixes.append(os.path.realpath(prefix))
            continue
        python = os.path.join(prefixes[0], "bin", "python")
        if os.path.exists(python):
            resolved_prefixes.append(os.path.dirname(os.path.dirname(os.path.realpath(python))))
    extra = []
    for entry in sys_path:
        if not entry or not os.path.isabs(entry):
            continue
        real = os.path.realpath(entry)
        if any(_inside(real, p) for p in resolved_prefixes):
            continue
        extra.append(real)
    if user_site:
        extra.append(os.path.realpath(user_site))
    candidates = [
        os.path.realpath(executable_dir),
        os.path.realpath(package_dir),
        *resolved_prefixes,
        *extra,
    ]
    return _existing_unique(candidates)


def _existing_unique(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(path for path in paths if os.path.exists(path)))


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
