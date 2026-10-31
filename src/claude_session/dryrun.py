"""``--dry-run`` of the launch commands and the ``kinds`` command (interface.md, Dry run and
kinds; schemas/cli/exec-dry-run.json and kinds.json).

A dry run reuses the launch pipeline (``launch``) without its side effects: ``read_request``
and ``resolve_kind`` (step 2), then the step-5 checks in their documented order
(``settings_builder.check_rule_paths``, ``sessiondir.check_dirs`` in place of
``launch.open_dirs``, and ``launch.checks`` with ``launch.Effects``, which records and creates
nothing: ``tmpdir.check`` and ``self_check(create=False)``), then ``launch.generate`` (the
settings and argv step 7 would write). It never allocates, opens no session directory, runs
no hook and writes no audit or log record. The processes it starts are the read-only probes
of a launch: ``git`` (repo id and git directory) and the sandbox prerequisite probes. When
the checks pass, the sandbox state comes from them (no second probe); when one fails, the
prerequisites are probed with ``bwrap`` and ``socat`` resolved against the session-writable
paths known without a session (``unchecked_writable``: with the permission profile's write
paths, and their ``${GIT_DIR}`` entries once the checks found the git directory), and a
binary a launch would refuse is reported, never run; with a profile file that was not read,
or a ``${GIT_DIR}`` write entry before the git directory is known, nothing is probed.

A new session has no id yet: its paths (session directory, private tmpdir, settings file)
are shown with the placeholder id ``PLACEHOLDER_ID``, and its own ``work/``, ``outbox/`` and
``artifacts/`` count as the real directories the launch would create
(``pathrules.PathRules(planned=True)``).

``kinds`` resolves every kind (or one) without a session and without the session-writable
checks of a launch (it has no session); it probes ``claude --version`` (minimal environment,
``claude_version.probe``) and the sandbox prerequisites, never running a binary under a path
a session could write without a session context (``launch.step2_writable``) or under the
kind's ``write_dirs`` and permission profile write paths (``unchecked_writable``); it never
knows the git directory, so an isolated kind with a ``${GIT_DIR}`` write entry probes
nothing.

TODO(#28): ``start --resume --dry-run`` and ``exec --session-id <id> --dry-run`` (inspect
the pending run without adopting it: ``would_refuse`` ``already_adopted``/``expired``,
``config_hash_changed``).
TODO(#29): ``resume --dry-run`` (carry-over from the previous run, the liveness check that
only reports, ``would_refuse`` ``live``/``tainted``, the run number, ``fork``).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from . import (
    claude_version,
    durations,
    envbuild,
    gitinfo,
    kinds,
    launch,
    pathrules,
    permission_profiles,
    selfcheck,
    sessiondir,
    settings_builder,
    store,
    tmpdir,
)
from .config import Config
from .errors import LaunchValidationError, UsageError
from .gateway import Gateway
from .permission_profiles import Profile

# A new session's paths are shown with this id (valid grammar; ids are random, so a real
# session with it is as unlikely as any other collision).
PLACEHOLDER_ID = "cs-aaaaaaaa"
DEFAULT_HOOK_TIMEOUT = 30.0
# A run secret: 32 random bytes as hex (interface.md, Identifiers).
RUN_SECRET_CHARS = 2 * sessiondir.SECRET_BYTES
SANDBOX_LISTS = {
    "allow_read": "allowRead",
    "allow_write": "allowWrite",
    "deny_read": "denyRead",
    "deny_write": "denyWrite",
}
# Profile variables that need a session; read_dirs/write_dirs entries using them are shown
# as written by ``kinds``.
_SESSION_VARIABLES = ("${CSESSION_DIR}", "${CWD}", "${GIT_DIR}")
# The tool name of a permission rule (``Tool`` or ``Tool(content)``).
_RULE_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


# Shared pieces


@dataclass(frozen=True)
class Sandbox:
    """The sandbox prerequisites as a run of a kind would find them. ``probed`` False: the
    dry run could not tell which tools a launch would accept and probed nothing
    (``available`` False, the reason in ``problems``)."""

    available: bool
    problems: tuple[str, ...] = ()
    probed: bool = True


@dataclass(frozen=True)
class Preview:
    """What a kind's session would get, computed without a session (``kinds``, and a dry run
    that fails before the launch checks compute it)."""

    path: str
    read_dirs: tuple[str, ...]
    write_dirs: tuple[str, ...]
    required_env_names: tuple[str, ...]


def required_env_names(
    resolution: kinds.Resolution, session_env: envbuild.SessionEnv
) -> tuple[str, ...]:
    """The names an adopting process should have (interface.md, Launch order: Environment
    at adoption): the ``claude_auth_env`` names in use (in the built environment), the
    effective ``env.allow`` names (patterns as written) and the ``env.set`` names."""
    names = [
        *session_env.auth_names,
        *resolution.env_allowlist(),
        *resolution.profile.env.set,
    ]
    return tuple(dict.fromkeys(names))


def origins_json(resolution: kinds.Resolution, mode: kinds.Mode) -> dict[str, str]:
    """``origins``: ``env.set`` is reported as ``env.set_names`` (names only), and the
    top-level ``timeout`` has the origin of the mode's key (``timeout`` or
    ``interactive_timeout``). ``env.allow`` has the origin of the last layer that changed the
    effective list (``--env-allow`` after ``--env-allowlist-only``), and
    ``permission_profile`` that of a ``--permission-profile`` file, which replaces it."""
    origins: dict[str, str] = {}
    for key, origin in resolution.origins.items():
        origins["env.set_names" if key == "env.set" else key] = origin
    for flag in ("env_allowlist_only", "env_allow"):
        if flag in resolution.origins:
            origins["env.allow"] = resolution.origins[flag]
    if "permission_profile_file" in resolution.origins:
        origins["permission_profile"] = resolution.origins["permission_profile_file"]
    origins["timeout"] = resolution.origins[
        "timeout" if mode == "headless" else "interactive_timeout"
    ]
    return origins


def profile_json(
    resolution: kinds.Resolution,
    *,
    permission_profile: str,
    sandbox: Sandbox | None,
    preview: Preview,
    warnings: Sequence[str],
) -> dict[str, Any]:
    """One effective kind profile (schemas/cli/kinds.json ``$defs/profile``). ``sandbox``
    None: the mode is ``off`` (``sandbox_available`` null)."""
    profile = resolution.profile
    return {
        "name": resolution.kind,
        "builtin": resolution.builtin,
        "fallback": resolution.fallback,
        "permission_profile": permission_profile,
        "sandbox": profile.sandbox,
        "sandbox_available": None if sandbox is None else sandbox.available,
        "sandbox_problems": [] if sandbox is None else list(sandbox.problems),
        "reads_third_party": profile.reads_third_party,
        "implicit_done": profile.implicit_done,
        "setting_sources": list(profile.setting_sources),
        "mcp": profile.mcp,
        "cwd": profile.cwd,
        "env": {
            "inherit": profile.env.inherit,
            "allow": resolution.env_allowlist(),
            "set_names": sorted(profile.env.set),
            "path_prepend": list(profile.env.path_prepend),
        },
        "required_env_names": list(preview.required_env_names),
        "path": preview.path,
        "read_dirs": list(preview.read_dirs),
        "write_dirs": list(preview.write_dirs),
        "timeout": profile.timeout,
        "interactive_timeout": profile.interactive_timeout,
        "max_turns": profile.max_turns,
        "auto_exit": profile.auto_exit,
        "backstop": profile.backstop,
        "notes_context": profile.notes_context,
        "model": profile.model,
        "claude_args": list(profile.claude_args),
        "warnings": list(dict.fromkeys(warnings)),
    }


def _entries(process: launch.Process, resolution: kinds.Resolution) -> list[str]:
    return pathrules.sanitize_path(
        ":".join([*resolution.profile.env.path_prepend, process.environ.get("PATH", "")])
    )


@dataclass(frozen=True)
class Lookup:
    """A tool looked up on the sanitized ``PATH``: ``tool`` None when it is not found or
    refused; ``refused`` names why it is not run (it is never run)."""

    tool: pathrules.ResolvedTool | None
    refused: str | None = None


REFUSED = "not run (a launch refuses it: unsafe_path)"
# A trusted kind's launch checks no session-writable path: it would use the tool, which a
# dry run still never runs.
NOT_PROBED = "not run (not probed by a dry run: under a session-writable path)"
# The session-writable paths are not known (a profile file that was not read, a profile or
# write_dirs entry that does not expand): nothing is run.
UNKNOWN = "not run (not probed by a dry run: the permission profile or write_dirs are not known)"


def _tool(
    name: str,
    entries: Sequence[str],
    writable: Callable[[str], bool],
    *,
    launch_refuses: bool = True,
) -> Lookup:
    """``name`` resolved as a launch resolves it, against ``writable``; ``launch_refuses``
    words a refusal as the launch's own (isolated kinds) or as a dry run's (trusted)."""
    try:
        return Lookup(pathrules.resolve_tool(name, entries, writable))
    except LaunchValidationError as error:
        return Lookup(None, f"{error.message}: {REFUSED if launch_refuses else NOT_PROBED}")


def _kind_profile(
    cfg: Config, resolution: kinds.Resolution, recorded: Profile | None
) -> Profile | None:
    """The permission profile a launch would use: the one its checks read (``recorded``),
    else the kind's built-in or configured profile; None for a profile file that was not
    read (a dry run never reads it outside the launch checks)."""
    if recorded is not None:
        return recorded
    if resolution.permission_profile_file is not None:
        return None
    try:
        return permission_profiles.select(cfg, resolution, session_writable=launch.NO_SANDBOX)
    except LaunchValidationError:
        return None


def unchecked_writable(
    cfg: Config,
    process: launch.Process,
    resolution: kinds.Resolution,
    *,
    probe_cwd: str | None,
    caller_cwd: str | None,
    recorded: Profile | None = None,
    git: GitDir | None = None,
) -> pathrules.SessionWritable | None:
    """The session-writable paths known without a session (a dry run whose checks failed,
    and ``kinds``): the step-2 set (``probe_cwd``, ``<state>/sessions``, the tmp root;
    ``launch.step2_writable``) plus, for an isolated kind, the placeholder session's paths,
    ``write_dirs`` and the permission profile's write paths: once the dry run's checks found
    the git directory (``git``), the full set the launch checks its tools against
    (``launch.full_writable``, ``${GIT_DIR}`` expanded); before that, the set the launch
    resolves git against (``launch.git_writable``), which is the whole set only when no
    ``write_dirs`` entry or profile write entry uses ``${GIT_DIR}``. For a trusted kind (a
    launch checks none): its ``write_dirs`` that resolve without a session. None when the
    profile or the ``write_dirs`` cannot be known (a profile file that was not read, an entry
    that does not expand, a ``${GIT_DIR}`` write entry while the git directory is not known):
    nothing may be probed. ``caller_cwd`` is the run's (None: ``work/``)."""
    step2 = launch.step2_writable(process, probe_cwd).paths
    home = os.path.realpath(process.environ.get("HOME", "/"))
    if not resolution.isolated():
        write = [
            entry
            for entry in _expanded_dirs(resolution.profile.write_dirs, home, "write_dirs")
            if os.path.isabs(entry) and not any(v in entry for v in _SESSION_VARIABLES)
        ]
        return pathrules.SessionWritable([*step2, *write])
    profile = _kind_profile(cfg, resolution, recorded)
    if profile is None:
        return None
    alloc = placeholder_alloc(process, resolution)
    tmp = None if resolution.profile.sandbox == "off" else tmpdir.run_path(PLACEHOLDER_ID)
    cwd = os.path.realpath(caller_cwd or os.path.join(alloc.session_dir, sessiondir.WORK))
    try:
        if git is not None:
            variables = launch.profile_variables(alloc, cwd, home, git.dirs)
            expanded = permission_profiles.expand(profile.content, variables)
            _, write = launch.resolve_run_dirs(resolution, variables, home, alloc)
            full = launch.full_writable(resolution, alloc, cwd, home, tmp, write, expanded)
            assert full is not None
            return pathrules.SessionWritable([*step2, *full.paths])
        if _uses_git_dir(resolution, profile):
            return None
        before_git = launch.git_writable(resolution, alloc, cwd, home, tmp, profile)
    except LaunchValidationError:
        return None
    assert isinstance(before_git, pathrules.SessionWritable)
    return pathrules.SessionWritable([*step2, *before_git.paths])


@dataclass(frozen=True)
class GitDir:
    """The git directory the launch checks found (``dirs`` None: the cwd has none)."""

    dirs: gitinfo.GitDirs | None


def _uses_git_dir(resolution: kinds.Resolution, profile: Profile) -> bool:
    """Whether a ``write_dirs`` entry or a profile write entry (sandbox ``allowWrite``, an
    ``Edit``/``Write`` allow rule) uses ``${GIT_DIR}``: the session-writable paths are then
    not known until git found the git directory."""
    variable = launch.GIT_DIR_VARIABLE
    if any(variable in entry for entry in resolution.profile.write_dirs):
        return True
    content = profile.content
    sandbox = content.get("sandbox")
    filesystem = sandbox.get("filesystem") if isinstance(sandbox, dict) else None
    allow_write = filesystem.get("allowWrite") if isinstance(filesystem, dict) else None
    if isinstance(allow_write, list) and any(
        isinstance(entry, str) and variable in entry for entry in allow_write
    ):
        return True
    permissions = content.get("permissions")
    rules = permissions.get("allow") if isinstance(permissions, dict) else None
    if not isinstance(rules, list):
        return False
    for rule in rules:
        if not isinstance(rule, str) or variable not in rule:
            continue
        tool = _RULE_TOOL.match(rule)
        if tool is None or tool.group(0) in pathrules.WRITE_TOOLS:
            return True
    return False


def placeholder_alloc(process: launch.Process, resolution: kinds.Resolution) -> launch.Allocated:
    """A new session as the dry run shows it (``PLACEHOLDER_ID``)."""
    return launch.Allocated(
        csession_id=PLACEHOLDER_ID,
        run=1,
        session_dir=sessiondir.session_dir(process.state_root, PLACEHOLDER_ID),
        private_dir=sessiondir.private_dir(process.state_root, PLACEHOLDER_ID),
        warnings=resolution.warnings,
    )


def not_probed(resolution: kinds.Resolution) -> Sandbox | None:
    """The sandbox of a kind whose session-writable paths are not known: nothing probed."""
    if resolution.profile.sandbox == "off":
        return None
    return Sandbox(available=False, problems=(f"sandbox prerequisites {UNKNOWN}",), probed=False)


class Prerequisites:
    """The sandbox prerequisite probes, run once per ``bwrap``/``socat`` pair."""

    def __init__(self, gw: Gateway, platform: str | None = None) -> None:
        self.gw = gw
        self.platform = selfcheck.detect_platform() if platform is None else platform
        self._seen: dict[tuple[str | None, str | None], Sandbox] = {}

    def check(
        self,
        process: launch.Process,
        resolution: kinds.Resolution,
        writable: pathrules.SessionWritable | None,
    ) -> Sandbox | None:
        """None for sandbox ``off``; else whether the prerequisites are present here, with
        ``bwrap`` and ``socat`` resolved against ``writable``: a refused one is reported as a
        problem and nothing is probed (nor when ``writable`` is None: not known)."""
        if resolution.profile.sandbox == "off":
            return None
        if writable is None:
            return not_probed(resolution)
        entries = _entries(process, resolution)
        refuses = resolution.isolated()
        found = [
            _tool(name, entries, writable, launch_refuses=refuses) for name in ("bwrap", "socat")
        ]
        refused = tuple(item.refused for item in found if item.refused is not None)
        if refused:
            return Sandbox(available=False, problems=refused)
        bwrap, socat = (item.tool for item in found)
        key = (None if bwrap is None else bwrap.path, None if socat is None else socat.path)
        if key not in self._seen:
            found = selfcheck.check_prerequisites(
                self.platform, bwrap=key[0], socat=key[1], gw=self.gw
            )
            problems = () if found.available else (f"{found.problem}; {found.hint}",)
            self._seen[key] = Sandbox(available=found.available, problems=problems)
        return self._seen[key]


def _expanded_dirs(entries: Sequence[str], home: str, key: str) -> list[str]:
    """``read_dirs``/``write_dirs`` without a session: an entry using a session variable
    as written, any other expanded (``${HOME}``, ``~``) and resolved."""
    shown: list[str] = []
    for entry in entries:
        if any(variable in entry for variable in _SESSION_VARIABLES):
            shown.append(entry)
            continue
        variables: dict[str, str | None] = {
            "HOME": home,
            "CSESSION_DIR": None,
            "CWD": None,
            "GIT_DIR": None,
        }
        try:
            shown += pathrules.resolve_dirs([entry], variables, home=home, key=key)
        except LaunchValidationError:
            shown.append(entry)
    return list(dict.fromkeys(shown))


def preview(
    cfg: Config,
    resolution: kinds.Resolution,
    process: launch.Process,
    claude: str | None,
    writable: pathrules.SessionWritable | None,
) -> Preview:
    """A kind's ``PATH``, directories and required names without a session: the
    environment is built with the placeholder session and no session-writable check; the
    read directories of an isolated kind include the generated entries (system, tool,
    Python and ``env.path_prepend`` directories)."""
    profile = resolution.profile
    session_dir = sessiondir.session_dir(process.state_root, PLACEHOLDER_ID)
    cwd = (
        os.path.join(session_dir, sessiondir.WORK)
        if profile.cwd == "session-dir"
        else process.caller_cwd
    )
    tmp = None if profile.sandbox == "off" else tmpdir.run_path(PLACEHOLDER_ID)
    session_env = envbuild.build(
        resolution,
        cfg,
        process.environ,
        csession_id=PLACEHOLDER_ID,
        csession_dir=session_dir,
        cwd=cwd,
        exe_dir=process.exe_dir,
        tmpdir=tmp,
        session_writable=launch.NO_SANDBOX if resolution.isolated() else None,
    )
    home = os.path.realpath(process.environ.get("HOME", "/"))
    read = _expanded_dirs(profile.read_dirs, home, "read_dirs")
    if resolution.isolated():
        entries = _entries(process, resolution)
        tools: list[pathrules.ResolvedTool | None] = []
        if writable is not None:
            tools = [_tool(name, entries, writable).tool for name in ("bwrap", "socat", "git")]
        paths = [t.path for t in tools if t is not None] + ([claude] if claude else [])
        read = [
            *settings_builder.system_read_dirs(),
            *settings_builder.tool_dirs(paths),
            *settings_builder.python_read_dirs(executable_dir=process.exe_dir),
            *(os.path.realpath(p) for p in profile.env.path_prepend),
            *read,
        ]
    return Preview(
        path=session_env.env["PATH"],
        read_dirs=tuple(dict.fromkeys(read)),
        write_dirs=tuple(_expanded_dirs(profile.write_dirs, home, "write_dirs")),
        required_env_names=required_env_names(resolution, session_env),
    )


# kinds


def kinds_report(
    cfg: Config,
    process: launch.Process,
    *,
    kind: str | None,
    repo: str | None,
    prerequisites: Prerequisites | None = None,
) -> dict[str, Any]:
    """``kinds --json`` (schemas/cli/kinds.json). Per-repo overrides apply when ``repo``
    is given (never derived: ``cwd=None``)."""
    probes = prerequisites or Prerequisites(process.gateway)
    default = kinds.resolve(cfg, None, mode="headless", repo=repo, cwd=None)
    names = [kind] if kind is not None else list(cfg.kinds)
    resolutions = [kinds.resolve(cfg, name, mode="headless", repo=repo, cwd=None) for name in names]
    sets = [
        unchecked_writable(
            cfg,
            process,
            resolution,
            probe_cwd=process.caller_cwd,
            caller_cwd=_kinds_cwd(process, resolution),
        )
        for resolution in resolutions
    ]
    lookup = _claude_lookup(cfg, process, default, resolutions, sets)
    claude = lookup.tool
    version = (
        None if claude is None else claude_version.probe(claude, process.environ, process.gateway)
    )
    warnings: list[str] = []
    if lookup.refused is not None:
        warnings.append(f"claude: {lookup.refused}")
    elif claude is None:
        warnings.append(f"claude not found: {cfg.claude_bin!r}")
    elif version is None:
        warnings.append(f"could not run {claude.path} --version")
    profiles = []
    for resolution, kind_writable in zip(resolutions, sets, strict=True):
        warnings += resolution.warnings
        sandbox = probes.check(process, resolution, kind_writable)
        claude_path = None if claude is None else claude.path
        profiles.append(
            profile_json(
                resolution,
                permission_profile=resolution.profile.permission_profile,
                sandbox=sandbox,
                preview=preview(cfg, resolution, process, claude_path, kind_writable),
                warnings=[*resolution.warnings, *_sandbox_warnings(resolution, sandbox)],
            )
        )
    return {
        "default_kind": cfg.default_kind,
        "repo": repo,
        "claude_bin_resolved": None if claude is None else claude.path,
        "claude_version": version,
        "claude_version_ok": claude_version.at_least(version, cfg.min_claude_version),
        "state_dir_warnings": state_dir_warnings(process.state_root, probes.platform),
        "min_claude_version": cfg.min_claude_version,
        "kinds": profiles,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _kinds_cwd(process: launch.Process, resolution: kinds.Resolution) -> str | None:
    """The cwd a ``kinds`` row assumes: the caller's, unless the kind runs in ``work/``."""
    return None if resolution.profile.cwd == "session-dir" else process.caller_cwd


def _claude_lookup(
    cfg: Config,
    process: launch.Process,
    default: kinds.Resolution,
    resolutions: Sequence[kinds.Resolution],
    sets: Sequence[pathrules.SessionWritable | None],
) -> Lookup:
    """``claude_bin`` on the default kind's ``PATH``, checked against the step-2 set and the
    union of the reported kinds' sets; never run when a set is not known. A refusal is the
    launch's own when an isolated kind's set holds it."""
    known = [writable for writable in sets if writable is not None]
    if len(known) != len(sets):
        return Lookup(None, f"{cfg.claude_bin!r}: {UNKNOWN}")
    entries = _entries(process, default)
    step2 = launch.step2_writable(process, process.caller_cwd).paths
    union = pathrules.SessionWritable([*step2, *(p for writable in known for p in writable.paths)])
    found = _tool(cfg.claude_bin, entries, union, launch_refuses=False)
    if found.refused is None:
        return found
    isolated = pathrules.SessionWritable(
        [p for r, w in zip(resolutions, known, strict=True) if r.isolated() for p in w.paths]
    )
    by_launch = _tool(cfg.claude_bin, entries, isolated)
    return by_launch if by_launch.refused is not None else found


def _sandbox_warnings(resolution: kinds.Resolution, sandbox: Sandbox | None) -> list[str]:
    warnings = list(settings_builder.isolation_warnings(resolution))
    if sandbox is not None and not sandbox.probed:
        return warnings
    if sandbox is not None and not sandbox.available:
        if resolution.profile.sandbox == "required":
            warnings.append("sandbox 'required' but unavailable here: a launch fails")
        elif resolution.profile.reads_third_party:
            warnings.append(
                "third-party-reading kind would run unsandboxed here (sandbox 'preferred' but"
                " unavailable): only the evadable permission rules isolate it"
            )
        else:
            warnings.append("sandbox 'preferred' but unavailable here: runs are not sandboxed")
    return warnings


def state_dir_warnings(state_root: str, platform: str) -> list[str]:
    """The state directory under ``/mnt/`` on WSL (a Windows drive: slow, and without the
    permissions the state needs)."""
    if platform in ("wsl1", "wsl2") and (state_root + "/").startswith("/mnt/"):
        return [
            f"state directory {state_root} is on a Windows drive (/mnt/); keep it on the Linux"
            " file system (set XDG_STATE_HOME)"
        ]
    return []


# Dry run of a new session


def _isatty(stream: Any) -> bool:
    try:
        return stream is not None and bool(stream.isatty())
    except (OSError, ValueError):
        return False


def check_interactive_tty(args: argparse.Namespace) -> None:
    """The step-2 TTY checks of interactive mode (interface.md, Launch order): plain
    ``exec`` needs a TTY on stdin and stdout, and ``--prompt-file -`` refuses a TTY stdin
    (``exec`` and ``start``; ``start`` never checks the TTY for the run). Exit 2."""
    if args.prompt_file == "-" and _isatty(sys.stdin):
        raise UsageError("--prompt-file - with a terminal on stdin in interactive mode")
    if getattr(args, "handler", None) == "exec" and not (
        _isatty(sys.stdin) and _isatty(sys.stdout)
    ):
        raise UsageError(
            "interactive exec needs a terminal on stdin and stdout",
            detail="pass --mode headless, or use start for a program caller",
        )


def request(args: argparse.Namespace, cfg: Config) -> launch.Request:
    """``launch.read_request`` for any mode: interactive mode gets its TTY checks here, then
    is read as headless (the rest of step 2 is the same).

    TODO(#17): once ``read_request`` accepts interactive mode (with these TTY checks),
    call it as it is and drop ``check_interactive_tty`` here."""
    mode = args.mode or "interactive"
    if mode == "interactive":
        check_interactive_tty(args)
    headless = argparse.Namespace(**{**vars(args), "mode": "headless"})
    return replace(launch.read_request(headless, cfg), mode=mode)


def hooks_json(
    cfg: Config, req: launch.Request, run_kind: str, labels: Sequence[str]
) -> list[dict[str, Any]]:
    """The configured and per-invocation hook entries. ``selected``: the entry's ``kinds``,
    ``exclude_kinds`` and ``labels`` selectors match this run (its run kind and the
    session's labels after the launch); ``states`` depends on the state change and is not
    evaluated. Per-invocation entries have no selectors.

    TODO(#31, #20): share the selector match with the hook runners."""
    have = set(labels)
    entries: list[dict[str, Any]] = []
    configured = (
        ("pre-launch", cfg.hooks.pre_launch),
        ("post-state-change", cfg.hooks.post_state_change),
    )
    for hook, items in configured:
        for item in items:
            selected = (
                (item.kinds is None or run_kind in item.kinds)
                and (item.exclude_kinds is None or run_kind not in item.exclude_kinds)
                and (item.labels is None or set(item.labels) <= have)
            )
            entries.append(
                {
                    "hook": hook,
                    "cmd": item.cmd,
                    "timeout": item.timeout,
                    "source": "config",
                    "selected": selected,
                }
            )
    timeouts: dict[str, float] = {}
    for text in req.hook_timeouts:
        name, _, value = text.partition("=")
        timeouts[name] = durations.parse(value, positive=True)
    for text in req.hooks:
        name, _, cmd = text.partition("=")
        entries.append(
            {
                "hook": name,
                "cmd": cmd,
                "timeout": timeouts.get(name, DEFAULT_HOOK_TIMEOUT),
                "source": "flag",
                "selected": True,
            }
        )
    return entries


@dataclass
class Outcome:
    """What the dry run found: the step-5 results (None when it failed before them), the
    generated settings and argv (None when it failed), the end reason and message of the
    first failure."""

    checked: launch.Checked | None = None
    generated: settings_builder.Generated | None = None
    would_fail: str | None = None
    message: str | None = None
    warnings: list[str] = field(default_factory=list)
    profile: Profile | None = None  # the permission profile, once read
    git: GitDir | None = None  # the git directory, once the checks found it


class DryRunEffects(launch.Effects):
    """The effects of a dry run: nothing is recorded or created; the permission profile,
    the git directory and the warnings are kept for a plan whose checks fail later."""

    def __init__(self) -> None:
        self.profile: Profile | None = None
        self.git: GitDir | None = None
        self.warnings: list[str] = []

    def record_profile(self, profile: Profile) -> None:
        self.profile = profile

    def record_git_dir(self, git_dirs: gitinfo.GitDirs | None) -> None:
        self.git = GitDir(git_dirs)

    def warn(self, warnings: Sequence[str]) -> None:
        self.warnings += warnings


def run_checks(
    cfg: Config,
    req: launch.Request,
    resolved: launch.Resolved,
    alloc: launch.Allocated,
    process: launch.Process,
) -> Outcome:
    """Launch step 5 and the step-7 generation, without side effects; the first
    ``LaunchValidationError`` becomes ``would_fail``."""
    found = Outcome()
    effects = DryRunEffects()
    try:
        settings_builder.check_rule_paths(process.state_root, process.claude_session)
        sessiondir.check_dirs(process.state_root, alloc.csession_id)
        found.checked = launch.checks(cfg, req, resolved, alloc, process, effects)
        found.warnings += found.checked.warnings
        # The size check of step 7, with a run secret of the real length (never printed).
        values = envbuild.secret_values(
            found.checked.session_env, process.environ, run_secret="0" * RUN_SECRET_CHARS
        )
        launch.secret_index(values)
        prompt_path = (
            None if req.prompt is None else os.path.join(alloc.session_dir, sessiondir.PROMPT)
        )
        found.generated = launch.generate(
            req, resolved.resolution, alloc, found.checked, process, prompt_path
        )
        found.warnings += found.generated.warnings
    except LaunchValidationError as error:
        if error.end_reason == "config_invalid":
            # A launch ends config_invalid only for config that step 2 validates first; a
            # dry run reports it as the invalid config it is (exit 2).
            raise UsageError("invalid config", detail=error.message) from error
        found.generated = None
        found.would_fail = error.end_reason
        found.message = launch.failure_message(error)
        # The warnings found before the failure, then the failure.
        found.warnings = [
            *effects.warnings,
            f"a launch would fail ({error.end_reason}): {found.message}",
        ]
    found.profile = effects.profile
    found.git = effects.git
    return found


def new_session_plan(
    args: argparse.Namespace,
    cfg: Config,
    process: launch.Process,
    *,
    prerequisites: Prerequisites | None = None,
) -> tuple[dict[str, Any], str | None]:
    """The dry-run plan of a new session (``exec`` and ``start`` without ``--resume``) and
    the failure message (None when a launch would pass the validations). Usage errors of
    step 2 raise ``UsageError`` as in a launch."""
    req = request(args, cfg)
    resolved = launch.resolve_kind(req, cfg, process)
    resolution = resolved.resolution
    alloc = placeholder_alloc(process, resolution)
    found = run_checks(cfg, req, resolved, alloc, process)
    writable = unchecked_writable(
        cfg,
        process,
        resolution,
        probe_cwd=resolved.caller_cwd,
        caller_cwd=resolved.caller_cwd,
        recorded=found.profile,
        git=found.git,
    )
    sandbox: Sandbox | None
    if found.checked is not None:
        # The checks passed: their (verified) tools and self-check, no second probe.
        sandbox = (
            None
            if resolution.profile.sandbox == "off"
            else Sandbox(
                available=found.checked.sandbox_active, problems=found.checked.sandbox_problems
            )
        )
    else:
        probes = prerequisites or Prerequisites(process.gateway)
        sandbox = probes.check(process, resolution, writable)
    plan = plan_json(
        cfg,
        req,
        resolution,
        found,
        sandbox=sandbox,
        preview=_plan_preview(cfg, resolution, process, found, writable),
        cwd=found.checked.cwd if found.checked is not None else _cwd(resolved, alloc),
        allow_tainted=bool(getattr(args, "allow_tainted", False)),
        override_escalation=bool(getattr(args, "override_escalation", False)),
    )
    return plan, found.message


def _cwd(resolved: launch.Resolved, alloc: launch.Allocated) -> str:
    return os.path.realpath(resolved.caller_cwd or os.path.join(alloc.session_dir, sessiondir.WORK))


def _plan_preview(
    cfg: Config,
    resolution: kinds.Resolution,
    process: launch.Process,
    found: Outcome,
    writable: pathrules.SessionWritable | None,
) -> Preview:
    """The profile's ``PATH``, directories and required names: from the launch checks when
    they passed, else as ``kinds`` computes them."""
    checked = found.checked
    if checked is None:
        return preview(cfg, resolution, process, None, writable)
    read: list[str] = list(checked.read_dirs)
    if checked.isolation is not None:
        iso = checked.isolation
        read = [*iso.system_dirs, *iso.tool_dirs, *iso.python_dirs, *iso.path_prepend, *read]
    return Preview(
        path=checked.session_env.env["PATH"],
        read_dirs=tuple(dict.fromkeys(read)),
        write_dirs=checked.write_dirs,
        required_env_names=required_env_names(resolution, checked.session_env),
    )


def plan_json(
    cfg: Config,
    req: launch.Request,
    resolution: kinds.Resolution,
    found: Outcome,
    *,
    sandbox: Sandbox | None,
    preview: Preview,
    cwd: str,
    allow_tainted: bool = False,
    override_escalation: bool = False,
    csession_id: str | None = None,
    run: int = 1,
) -> dict[str, Any]:
    """The dry-run plan (schemas/cli/exec-dry-run.json). With ``would_fail`` set,
    ``settings`` is empty and ``claude_argv``, ``env_names`` and the sandbox lists are
    empty: a launch never gets that far."""
    profile = resolution.profile
    mode: kinds.Mode = "headless" if req.mode == "headless" else "interactive"
    labels = sorted({f"kind={resolution.kind}", *req.labels})
    generated = found.generated if found.would_fail is None else None
    checked = found.checked if found.would_fail is None else None
    settings: dict[str, Any] = {} if generated is None else generated.settings
    filesystem: Mapping[str, Any] = settings.get("sandbox", {}).get("filesystem", {})
    # The profile the launch checks read (also when a later check failed); a profile file
    # that could not be read has no label, and the profile shows the flag's value.
    recorded = found.checked.profile if found.checked is not None else found.profile
    label: str | None
    if recorded is not None:
        label = recorded.label
    elif resolution.permission_profile_file is not None:
        label = None
    else:
        label = profile.permission_profile
    shown_profile = label or resolution.permission_profile_file or profile.permission_profile
    # The launch checks give the isolation and sandbox warnings when they ran.
    extra = _sandbox_warnings(resolution, sandbox) if found.checked is None else []
    warnings = [*resolution.warnings, *found.warnings, *extra]
    return {
        "dry_run": True,
        "csession_id": csession_id,
        "run": run,
        "fork": False,
        "forked_from": None,
        "launch_kind": resolution.kind,
        "run_kind": resolution.kind,
        "labels": labels,
        "taints": profile.reads_third_party,
        "mode": mode,
        "cwd": cwd,
        "repo": resolution.repo,
        "skill": req.skill,
        "has_prompt": req.prompt is not None,
        "input_files": [item.name for item in req.inputs],
        "implicit_done": profile.implicit_done,
        "permission_profile": label,
        "timeout": resolution.timeout(mode),
        "max_turns": profile.max_turns,
        "actor": req.actor,
        "override_escalation": override_escalation,
        "allow_tainted": allow_tainted,
        "sandbox_lowered": resolution.sandbox_lowered(),
        "config_hash_changed": None,
        "profile": profile_json(
            resolution,
            permission_profile=shown_profile,
            sandbox=sandbox,
            preview=preview,
            warnings=[*resolution.warnings, *_sandbox_warnings(resolution, sandbox)],
        ),
        "origins": origins_json(resolution, mode),
        "settings": settings,
        # argv[0] is the bare name a launch execs (launch.Prepared.argv).
        "claude_argv": (
            []
            if generated is None or found.checked is None
            else [found.checked.claude.name, *generated.argv[1:]]
        ),
        "env_names": [] if checked is None else sorted(checked.session_env.env),
        "sandbox": {
            "mode": profile.sandbox,
            "available": False if sandbox is None else sandbox.available,
            "problems": [] if sandbox is None else list(sandbox.problems),
            **{key: list(filesystem.get(name, [])) for key, name in SANDBOX_LISTS.items()},
        },
        "hooks": hooks_json(cfg, req, resolution.kind, labels),
        "would_refuse": None,
        "would_fail": found.would_fail,
        "warnings": list(dict.fromkeys(warnings)),
    }


def current_process(gw: Gateway) -> launch.Process:
    """The running process, with the state root the schema check created."""
    return launch.Process.current(gw, store.state_dir())


def printable(value: Any) -> Any:
    """``value`` with every string that is not valid UTF-8 (a non-UTF-8 path decoded with
    surrogate escapes) shown with backslash escapes, so the JSON output is valid UTF-8."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "backslashreplace").decode("utf-8")
        return value
    if isinstance(value, dict):
        return {printable(key): printable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [printable(item) for item in value]
    return value


def exit_code(plan: Mapping[str, Any]) -> int:
    """20 with ``would_refuse``, else 12 with ``would_fail``, else 0."""
    if plan["would_refuse"] is not None:
        return 20
    return 12 if plan["would_fail"] is not None else 0
