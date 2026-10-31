"""The launch pipeline of a new session's run (interface.md, Launch order; design.md, Launch,
adoption and pending runs, and Supervision).

One function per launch step, called in order by ``exec`` (``cli``):

- step 2, before anything is allocated: ``read_request`` (flags, prompt and input files,
  exit 2 on a usage error) and ``resolve_kind`` (the effective profile, the cwd, the repo);
- step 4: ``allocate`` (session id, session and run rows, labels, the ``launch`` event);
- step 5: ``validate`` (the launch validations, each an ordered helper below);
- step 7: ``prepare`` (session-directory files, secret index, hook configuration, settings).
  ``supervisor`` then spawns and supervises ``claude``; ``run`` strings steps 5 and 7 and the
  hand-over together.

Everything up to and including a successful spawn of ``claude`` is the launch phase. A
``LaunchValidationError`` in step 5 or 7 (an unsafe session directory included,
``session_dir_unsafe``; a ``UsageError`` from the environment builder, which config
validation normally catches first, becomes ``config_invalid``) ends the run ``not-started``
with its end reason (``end_launch``); any other error before the spawn (the audit record
cannot be written, ``claude`` cannot be started) ends it ``launch_interrupted``. Either way
the command exits 12 with the outcome object; only a ``KeyboardInterrupt`` (SIGINT) is
re-raised once the run is final.

The step-5 helpers take every input explicitly and return values; the side effects of step 5
are confined to ``open_dirs`` (the session's directories) and the ``Effects`` that
``validate`` passes to ``checks`` (the run and session rows, ``create_write_dirs`` and the
private tmpdir, created last by ``check_sandbox``). ``--dry-run`` (``dryrun``) runs
``read_request``, ``resolve_kind`` and ``checks`` with effects that record and create
nothing (``self_check(create=False)``), then ``generate`` for the settings and argv; never
``allocate``, ``open_dirs`` or ``prepare``.
``start`` and the adopting ``exec`` (``lifecycle``) reuse the same steps: ``start`` allocates
the run with itself as its wrapper (``allocate(..., pending=...)``), runs ``validate`` and
makes the run pending once it is frozen (``Store.release_pending``); the adopting ``exec``
runs ``run`` with its own effects (``Recording`` subclass comparing what ``start`` stored)
and ``staged=True`` (step 7 moves ``start``'s staged prompt and inputs into place).
TODO(#29): resume and fork build their request from the previous run (carry-over).
TODO(#31): launch step 6, the ``pre-launch`` hooks, between ``validate`` and ``prepare``.
TODO(#40): ``claude --version`` against ``min_claude_version`` (``claude_too_old``) in
``validate``, after ``claude_bin`` is resolved.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import (
    audit,
    envbuild,
    gitinfo,
    hookevent,
    ids,
    kinds,
    logs,
    pathrules,
    permission_profiles,
    secretindex,
    selfcheck,
    sessiondir,
    settings_builder,
    store,
    tmpdir,
)
from .config import Config
from .endmap import EXIT_CODES
from .errors import (
    FailedError,
    InternalError,
    InvalidPermissionProfile,
    LaunchValidationError,
    UsageError,
)
from .gateway import Gateway
from .sessiondir import InputFile, SessionDirs

LOG_NAME = "log.jsonl"
AUDIT_NAME = "audit.jsonl"
RUN = 1  # a new session's run (resume: TODO(#29))
RESULT_FILE = "result.json"
STDERR_FILE = "stderr.log"
PROFILE_SNAPSHOT = "permission-profile.json"
MESSAGE_MAX = 2048  # the outcome message, in characters
GIT_DIR_VARIABLE = "${GIT_DIR}"
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# A session not allocated yet (step 2, ``kinds``, a dry run) is shown and checked with this
# id (valid grammar; ids are random, so a real session with it is as unlikely as any other
# collision).
PLACEHOLDER_ID = "cs-aaaaaaaa"
# The tool name of a permission rule (``Tool`` or ``Tool(content)``).
_RULE_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def NO_SANDBOX(_path: str) -> bool:  # noqa: N802 (a constant predicate)
    """The session-writable check of a trusted run: nothing is session-writable."""
    return False


# Hints for launch failures that have a fixed remedy (interface.md, Error handling).
HINTS = {
    "claude_not_found": "install Claude Code, or set claude_bin in the config to its path",
    "session_dir_unsafe": "remove or repair the named entry of the claude-session state "
    "directory (it must be a real directory or file owned by you)",
}


# Inputs


@dataclass(frozen=True)
class Process:
    """The facts of the running ``exec``: its environment, cwd, executable and interpreter,
    the state root and the gateway. Everything the pipeline reads from the process."""

    environ: Mapping[str, str] = field(repr=False)
    caller_cwd: str
    claude_session: str  # the absolute claude-session executable (argv[0], not resolved)
    interpreter: str  # settings_builder.current_interpreter()
    state_root: str  # realpath of the state directory
    gateway: Gateway

    @classmethod
    def current(cls, gw: Gateway, state_root: str) -> Process:
        return cls(
            environ=dict(os.environ),
            caller_cwd=os.path.realpath(os.getcwd()),
            claude_session=os.path.abspath(sys.argv[0]),
            interpreter=settings_builder.current_interpreter(),
            state_root=os.path.realpath(state_root),
            gateway=gw,
        )

    @property
    def exe_dir(self) -> str:
        return os.path.dirname(self.claude_session)


@dataclass(frozen=True)
class Request:
    """What ``exec`` was asked to launch, read and usage-checked (launch step 2)."""

    kind: str | None
    mode: str
    skill: str | None
    prompt: bytes | None = field(repr=False)
    inputs: tuple[InputFile, ...]
    plugin_dirs: tuple[str, ...]
    repo: str | None
    cwd: str | None  # --cwd as given (resolved in resolve_kind)
    labels: tuple[str, ...]
    flags: dict[str, Any]  # kinds.FLAG_KEYS given now (None: not given)
    actor: str
    decision_ids: tuple[str, ...]
    hooks: tuple[str, ...]
    hook_timeouts: tuple[str, ...]
    outcome_file: str | None


def _actor(given: str | None, cfg: Config) -> str:
    """``--actor``, else ``human:<me.name>`` with a TTY stdin, else ``auto:unknown``."""
    if given is not None:
        return given
    try:
        tty = sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError):
        tty = False
    if tty:
        name = cfg.me.name or os.environ.get("USER") or "unknown"
        return f"human:{name}"
    return "auto:unknown"


def read_request(args: argparse.Namespace, cfg: Config) -> Request:
    """Launch step 2 for ``exec``: the flags (already checked by ``cli``'s flag rules), the
    prompt and the input files read into memory (each at most ``input_max_bytes``; a bad file
    is a ``UsageError``, exit 2). Only headless runs are implemented."""
    mode = args.mode or "interactive"
    if mode != "headless":
        # TODO(#17): interactive supervision (exec's default mode).
        raise InternalError(
            "interactive exec is not implemented yet; pass --mode headless",
            detail="not_implemented",
        )
    cap = cfg.input_max_bytes
    prompt = None
    if args.prompt_file == "-":
        prompt = sessiondir.read_stream_capped(sys.stdin.buffer, cap, flag="--prompt-file -")
    elif args.prompt_file is not None:
        prompt = sessiondir.read_source_file(args.prompt_file, cap, flag="--prompt-file")
    inputs = sessiondir.load_inputs(args.input_file or [], cap)
    flags = {key: getattr(args, key, None) for key in kinds.FLAG_KEYS}
    return Request(
        kind=args.kind,
        mode=mode,
        skill=args.skill,
        prompt=prompt,
        inputs=tuple(inputs),
        plugin_dirs=tuple(args.plugin_dir or ()),
        repo=args.repo,
        cwd=args.cwd,
        labels=tuple(args.label or ()),
        flags=flags,
        actor=_actor(args.actor, cfg),
        decision_ids=tuple(args.decision_id or ()),
        hooks=tuple(args.hook or ()),
        hook_timeouts=tuple(args.hook_timeout or ()),
        outcome_file=None if args.outcome_file is None else os.path.abspath(args.outcome_file),
    )


@dataclass(frozen=True)
class Resolved:
    """The effective profile of the run (launch step 2) and the caller-side cwd it resolved
    (None for ``cwd: session-dir``, which is ``$CSESSION_DIR/work/`` once allocated)."""

    resolution: kinds.Resolution
    caller_cwd: str | None


def _path_entries(environ: Mapping[str, str], prepend: Sequence[str]) -> list[str]:
    """Where the tools are looked up: ``env.path_prepend``, then the caller's ``PATH``,
    sanitized (empty and relative entries dropped)."""
    return pathrules.sanitize_path(":".join([*prepend, environ.get("PATH", "")]))


def _caller_cwd(given: str | None, process: Process) -> str:
    """``--cwd`` (made absolute against the caller's cwd) or the caller's cwd, resolved; it
    must be an existing directory (exit 2 otherwise)."""
    path = os.path.realpath(os.path.join(process.caller_cwd, given or "."))
    if not os.path.isdir(path):
        raise UsageError(f"--cwd is not an existing directory: {given!r}")
    return path


def step2_writable(process: Process, probe_cwd: str | None) -> pathrules.SessionWritable:
    """What git must not be under at step 2, before the run's session-writable paths are
    known (conservative): the probe cwd, every session's directory (``<state>/sessions``)
    and the tmp root of the private temporary directories (``/tmp/csession-<uid>``)."""
    paths = [os.path.join(process.state_root, sessiondir.SESSIONS), pathrules.tmp_parent()]
    return pathrules.SessionWritable([*([probe_cwd] if probe_cwd else []), *paths])


def _first_executable(name: str, entries: Sequence[str]) -> str:
    for entry in entries:
        candidate = os.path.join(entry, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return name


def step2_git_writable(
    cfg: Config, req: Request, process: Process, probe_cwd: str
) -> pathrules.SessionWritable | None:
    """What git must not be under when it derives the repo at step 2: the step-2 set
    (``step2_writable``) plus the session-writable paths of the kind known without a
    session (``known_write_paths``: its ``write_dirs`` and permission profile write paths,
    with the placeholder session), for the kind as configured and as every per-repo
    override of it makes it (the repo is not known yet), for trusted and isolated kinds
    alike. None when any of them is not known (a ``${GIT_DIR}`` write entry, a profile
    file that cannot be read, an entry that does not expand): no git may run."""
    paths = list(step2_writable(process, probe_cwd).paths)
    mode: kinds.Mode = "headless" if req.mode == "headless" else "interactive"
    for repo in (None, *cfg.repos):
        resolution = kinds.resolve(cfg, req.kind, mode=mode, repo=repo, cwd=None, flags=req.flags)
        try:
            profile = permission_profiles.select(
                cfg,
                resolution,
                session_writable=pathrules.SessionWritable(paths),
            )
        except LaunchValidationError:
            return None
        caller = probe_cwd if resolution.profile.cwd == "caller" or req.cwd is not None else None
        known = known_write_paths(resolution, profile, process, caller_cwd=caller)
        if known is None:
            return None
        paths += known.paths
    return pathrules.SessionWritable(paths)


def resolve_kind(req: Request, cfg: Config, process: Process) -> Resolved:
    """Launch step 2: the effective profile (``kinds.resolve``), its cwd and the repo derived
    from the ``origin`` remote, looked up with git resolved on the sanitized PATH and run by
    name from its realpath (looked up with the kind's configured ``env.path_prepend``: the
    repo, and so a per-repo override, is not known yet; step 5 resolves git again) and with
    the minimal git environment (``kinds.git_env``). A git under the probe cwd,
    ``<state>/sessions``, the tmp root or the kind's write paths known without a session
    (``step2_git_writable``, checked hop by hop) is never run, nor is any git when those
    paths are not known: no repo is derived and the run gets a warning. A usage error
    exits 2 before allocation."""
    session_dir_cwd = req.cwd == "session-dir"
    configured = cfg.kinds.get(req.kind or cfg.default_kind)
    prepend = configured.env.path_prepend if configured is not None else []
    probe_cwd = None if session_dir_cwd else _caller_cwd(req.cwd, process)
    entries = _path_entries(process.environ, prepend)
    warnings: list[str] = []

    def origin(cwd: str) -> str | None:
        assert probe_cwd is not None  # the repo is never derived for a session-dir cwd
        writable = step2_git_writable(cfg, req, process, probe_cwd)
        if writable is None:
            warnings.append(
                "the kind's session-writable paths are not known before the launch (a"
                " ${GIT_DIR} write entry, a permission profile file that cannot be read or an"
                " entry that does not expand); repo not derived from origin"
            )
            return None
        try:
            git = pathrules.resolve_tool("git", entries, writable)
        except LaunchValidationError:
            warnings.append(
                f"git at {_first_executable('git', entries)} is under a session-writable path;"
                " repo not derived from origin"
            )
            return None
        if git is None:
            return None
        return kinds.git_origin_url(
            cwd, git=git.path, git_name=git.name, gw=process.gateway, environ=process.environ
        )

    resolution = kinds.resolve(
        cfg,
        req.kind,
        mode="headless" if req.mode == "headless" else "interactive",
        repo=req.repo,
        cwd=probe_cwd,
        flags=req.flags,
        origin_url=origin,
    )
    if warnings:
        resolution = dataclasses.replace(resolution, warnings=(*resolution.warnings, *warnings))
    caller_cwd = None
    if not session_dir_cwd and (resolution.profile.cwd == "caller" or req.cwd is not None):
        caller_cwd = probe_cwd  # --cwd given for a session-dir kind: the caller's choice wins
    return Resolved(resolution=resolution, caller_cwd=caller_cwd)


# Step 4: allocation


@dataclass(frozen=True)
class Allocated:
    csession_id: str
    run: int
    session_dir: str
    private_dir: str
    warnings: tuple[str, ...]
    # The ``cause`` of a launch-phase end of this run: ``adopt`` in the adopting exec.
    cause: str = "launch"
    # The procinfo start-time token recorded as the run's wrapper_start (allocate).
    wrapper_start: str | None = None


def _resolved_record(resolution: kinds.Resolution) -> dict[str, Any]:
    """``resolved_json``: every resolved setting with its origin. ``env.set`` is stored as
    its names only (its values are literal configuration but may be credentials)."""
    profile = resolution.profile.model_dump()
    env = profile.pop("env")
    profile["env"] = {**env, "set": sorted(env["set"])}
    return {
        "kind": resolution.kind,
        "fallback": resolution.fallback,
        "repo": resolution.repo,
        "profile": profile,
        "origins": dict(resolution.origins),
        "env_allow": list(resolution.env_allow),
        "env_allowlist_only": (
            None if resolution.env_allowlist_only is None else list(resolution.env_allowlist_only)
        ),
        "permission_profile_file": resolution.permission_profile_file,
    }


@dataclass(frozen=True)
class Pending:
    """What ``start`` records on its run besides the request: the config it froze
    (``config.Source`` and ``config.config_hash``), and the adoption deadline set when the
    frozen run becomes pending (``lost_grace`` seconds from then)."""

    expires_after: float
    config_hash: str
    config_path: str
    config_default: bool


def allocate(
    st: store.Store,
    req: Request,
    resolved: Resolved,
    process: Process,
    *,
    pending: Pending | None = None,
) -> Allocated:
    """Launch step 4: the session id (checked against every live, forgotten and deleted id in
    the inserting transaction) and the session and run rows, with this process as the run's
    wrapper (plain ``exec`` performs every step; ``start`` until the run is frozen), its
    labels and the ``launch`` event. With ``pending`` (``start``) the config hash and path
    are frozen with it."""
    resolution = resolved.resolution
    # TODO(#33): start_time() gives "" when the token cannot be read (plain exec and start
    # alike); the liveness check must never take an empty token as matching a live process.
    wrapper_start = store.start_time(os.getpid())
    spec = {key: value for key, value in req.flags.items() if value is not None}
    if req.plugin_dirs:
        spec["plugin_dir"] = list(req.plugin_dirs)
    invocation = {
        "hooks": list(req.hooks),
        "hook_timeouts": list(req.hook_timeouts),
        "actor": req.actor,
        "decision_ids": list(req.decision_ids),
        "outcome_file": req.outcome_file,
        "input_files": [item.name for item in req.inputs],
    }
    if pending is not None:
        invocation["config_default"] = pending.config_default
    warnings = tuple(resolution.warnings)
    new = store.NewRun(
        state_root=process.state_root,
        kind=resolution.kind,
        reads_third_party=resolution.profile.reads_third_party,
        mode=req.mode,
        repo=resolution.repo,
        model=resolution.profile.model,
        skill=req.skill,
        has_prompt=req.prompt is not None,
        spec=spec,
        resolved=_resolved_record(resolution),
        # exec runs have no frozen config (it is loaded once, by this process).
        config_hash=None if pending is None else pending.config_hash,
        invocation=invocation,
        hook_interpreter=process.interpreter,
        config_path=None if pending is None else pending.config_path,
        wrapper_pid=os.getpid(),
        wrapper_start=wrapper_start,
        labels=req.labels,
        decision_ids=req.decision_ids,
        warnings=warnings,
        outcome_file=req.outcome_file,
    )
    csession_id = st.allocate(new, new_id=ids.new_session_id)
    return Allocated(
        csession_id=csession_id,
        run=RUN,
        session_dir=sessiondir.session_dir(process.state_root, csession_id),
        private_dir=sessiondir.private_dir(process.state_root, csession_id),
        warnings=warnings,
        wrapper_start=wrapper_start,
    )


# Step 5: launch validations


@dataclass
class Checked:
    """What ``checks`` resolved: everything ``Validated`` holds but the session
    directories."""

    cwd: str
    git_dirs: gitinfo.GitDirs | None
    profile: permission_profiles.Profile
    expanded: dict[str, Any]
    read_dirs: tuple[str, ...]
    write_dirs: tuple[str, ...]
    plugin_dirs: tuple[str, ...]
    claude: pathrules.ResolvedTool
    git: pathrules.ResolvedTool | None
    bwrap: pathrules.ResolvedTool | None
    socat: pathrules.ResolvedTool | None
    session_env: envbuild.SessionEnv
    isolation: settings_builder.Isolation | None
    sandbox_active: bool
    tmpdir: str | None
    warnings: list[str] = field(default_factory=list)
    credential_warnings: tuple[str, ...] = ()
    sandbox_problems: tuple[str, ...] = ()  # why a preferred sandbox is unavailable


@dataclass
class Validated(Checked):
    """Everything step 5 resolved and checked, for step 7 and the supervisor: ``Checked``
    plus the session directories ``open_dirs`` opened."""

    dirs: SessionDirs = field(kw_only=True)
    layout: sessiondir.RunLayout = field(kw_only=True)


def open_dirs(
    alloc: Allocated, process: Process, *, isolated: bool
) -> tuple[SessionDirs, sessiondir.RunLayout]:
    """The session and private directories, and the run's agent-writable directories
    (``prepare_run``), created now: a ``cwd: session-dir`` run's cwd and the ``write_dirs``
    inside ``$CSESSION_DIR`` must exist for the path rules. An unsafe entry raises
    ``SessionDirUnsafe`` (``session_dir_unsafe``)."""
    dirs = SessionDirs.open(process.state_root, alloc.csession_id)
    try:
        layout = dirs.prepare_run(alloc.run, isolated=isolated)
    except BaseException:
        dirs.close()
        raise
    return dirs, layout


def host_rules(
    process: Process,
    resolution: kinds.Resolution,
    alloc: Allocated,
    cwd: str,
    tmp: str | None,
    *,
    planned: bool = False,
) -> pathrules.PathRules:
    """The ``Host`` facts and the run's ``PathRules`` (after ``open_dirs``: the exceptions for
    the run's own directories apply only to real directories that exist; a dry run, which
    creates none, passes ``planned``); for isolated runs ``check_host`` first: the write
    checks assume a valid ``$HOME``. The private tmpdir does not exist yet (the self-check
    creates it at the end of step 5), so an entry inside it is refused here (conservative:
    nothing a profile needs lives there)."""
    host = pathrules.Host.from_environ(
        process.environ,
        state_root=process.state_root,
        path_prepend=resolution.profile.env.path_prepend,
    )
    rules = pathrules.PathRules(
        host,
        session_dir=alloc.session_dir,
        cwd=cwd,
        caller_cwd=process.caller_cwd,
        tmpdir=tmp,
        planned=planned,
    )
    if resolution.isolated():
        rules.check_host()
    return rules


def check_cwd(rules: pathrules.PathRules, resolution: kinds.Resolution) -> None:
    """The cwd must be a directory and valid UTF-8 (every run: it is recorded and printed);
    isolated runs also write it into rules and may write in it, so it passes the cwd rules
    (``unsafe_cwd``), checked before the profile is expanded so a bad cwd fails
    ``unsafe_cwd`` whatever the profile. A dry run's ``work/`` that a launch
    would create (``rules.planned``) counts as a directory."""
    if not os.path.isdir(rules.cwd) and not rules.planned(rules.cwd):
        raise LaunchValidationError(
            f"cwd is not a directory: {rules.cwd!r}", end_reason="unsafe_cwd"
        )
    if resolution.isolated():
        settings_builder.check_cwd(rules.cwd)
        rules.check_cwd()
    else:
        settings_builder.check_cwd_utf8(rules.cwd)


def base_writable(
    resolution: kinds.Resolution, alloc: Allocated, cwd: str, home: str, tmp: str | None
) -> Callable[[str], bool]:
    """The session-writable paths known before the profile and ``write_dirs`` are (the cwd,
    ``$CSESSION_DIR`` and its writable directories, the tmpdir): used to read the profile
    file, which is checked again against the full set (``check_profile_file``)."""
    if not resolution.isolated():
        return NO_SANDBOX
    return pathrules.session_writable(session_dir=alloc.session_dir, cwd=cwd, home=home, tmpdir=tmp)


def _without_git_dir(node: Any) -> Any:
    """``node`` without the strings that use ``${GIT_DIR}`` (list items and mapping values)."""

    def uses(value: Any) -> bool:
        return isinstance(value, str) and GIT_DIR_VARIABLE in value

    if isinstance(node, dict):
        return {key: _without_git_dir(value) for key, value in node.items() if not uses(value)}
    if isinstance(node, list):
        return [_without_git_dir(value) for value in node if not uses(value)]
    return node


def git_writable(
    resolution: kinds.Resolution,
    alloc: Allocated,
    cwd: str,
    home: str,
    tmp: str | None,
    profile: permission_profiles.Profile,
    git_cwd: str,
) -> Callable[[str], bool]:
    """The session-writable paths git is resolved against before it runs (isolated runs):
    the ``base_writable`` set plus every ``write_dirs`` entry and every profile write
    addition that does not use ``${GIT_DIR}``; when one does, every directory git could
    take for the git directory of ``git_cwd`` (``gitinfo.candidate_dirs``, found without
    running git), so a git planted where a ``${GIT_DIR}`` entry makes writable is refused
    before it runs (``unsafe_path``). Raises ``LaunchValidationError`` (``unsafe_cwd``)
    when those candidates cannot be found."""
    if not resolution.isolated():
        return NO_SANDBOX
    variables = profile_variables(alloc, cwd, home, None)
    entries = [e for e in resolution.profile.write_dirs if GIT_DIR_VARIABLE not in e]
    write = pathrules.resolve_dirs(entries, variables, home=home, key="write_dirs")
    if uses_git_dir(resolution, profile):
        write = [*write, *gitinfo.candidate_dirs(git_cwd)]
    content = permission_profiles.expand(_without_git_dir(profile.content), variables)
    return pathrules.session_writable(
        session_dir=alloc.session_dir,
        cwd=cwd,
        home=home,
        tmpdir=tmp,
        write_dirs=write,
        profile=content,
    )


def uses_git_dir(resolution: kinds.Resolution, profile: permission_profiles.Profile) -> bool:
    """Whether a ``write_dirs`` entry or a profile write entry (sandbox ``allowWrite``, an
    ``Edit``/``Write`` allow rule) uses ``${GIT_DIR}``: the session-writable paths are then
    not known until the git directory is."""
    if any(GIT_DIR_VARIABLE in entry for entry in resolution.profile.write_dirs):
        return True
    content = profile.content
    sandbox = content.get("sandbox")
    filesystem = sandbox.get("filesystem") if isinstance(sandbox, dict) else None
    allow_write = filesystem.get("allowWrite") if isinstance(filesystem, dict) else None
    if isinstance(allow_write, list) and any(
        isinstance(entry, str) and GIT_DIR_VARIABLE in entry for entry in allow_write
    ):
        return True
    permissions = content.get("permissions")
    rules = permissions.get("allow") if isinstance(permissions, dict) else None
    if not isinstance(rules, list):
        return False
    for rule in rules:
        if not isinstance(rule, str) or GIT_DIR_VARIABLE not in rule:
            continue
        tool = _RULE_TOOL.match(rule)
        if tool is None or tool.group(0) in pathrules.WRITE_TOOLS:
            return True
    return False


def placeholder_alloc(process: Process, resolution: kinds.Resolution) -> Allocated:
    """A session not allocated yet, with ``PLACEHOLDER_ID``."""
    return Allocated(
        csession_id=PLACEHOLDER_ID,
        run=1,
        session_dir=sessiondir.session_dir(process.state_root, PLACEHOLDER_ID),
        private_dir=sessiondir.private_dir(process.state_root, PLACEHOLDER_ID),
        warnings=resolution.warnings,
    )


def known_write_paths(
    resolution: kinds.Resolution,
    profile: permission_profiles.Profile | None,
    process: Process,
    *,
    caller_cwd: str | None,
    git_dirs: gitinfo.GitDirs | None = None,
    git_known: bool = False,
    alloc: Allocated | None = None,
) -> pathrules.SessionWritable | None:
    """The session-writable paths of a run of ``resolution`` known without a session
    (step 2, ``kinds``, a dry run), computed alike for trusted and isolated kinds: those of
    the placeholder session (its directory and writable directories, its private tmpdir),
    the cwd (``caller_cwd``; None: ``work/``), every ``write_dirs`` entry and the profile's
    write paths (sandbox ``allowWrite``, ``Edit``/``Write`` allow targets), as
    ``git_writable`` computes them; ``${GIT_DIR}`` is expanded with ``git_dirs`` when the
    git directory is known (``git_known``). None when they are not known: no ``profile``
    (a profile file that was not read), a ``${GIT_DIR}`` write entry while the git
    directory is not known, or an entry that does not expand. ``alloc`` is the session's
    (default: the placeholder session; the adoption dry run passes the pending run's)."""
    if profile is None or (not git_known and uses_git_dir(resolution, profile)):
        return None
    home = os.path.realpath(process.environ.get("HOME", "/"))
    alloc = alloc or placeholder_alloc(process, resolution)
    tmp = None if resolution.profile.sandbox == "off" else tmpdir.run_path(alloc.csession_id)
    cwd = os.path.realpath(caller_cwd or os.path.join(alloc.session_dir, sessiondir.WORK))
    variables = profile_variables(alloc, cwd, home, git_dirs)
    content = profile.content if git_known else _without_git_dir(profile.content)
    try:
        write = pathrules.resolve_dirs(
            resolution.profile.write_dirs, variables, home=home, key="write_dirs"
        )
        return pathrules.session_writable(
            session_dir=alloc.session_dir,
            cwd=cwd,
            home=home,
            tmpdir=tmp,
            write_dirs=write,
            profile=permission_profiles.expand(content, variables),
        )
    except LaunchValidationError:
        return None


def check_git(
    resolution: kinds.Resolution,
    cwd: str,
    git: pathrules.ResolvedTool | None,
    process: Process,
) -> tuple[gitinfo.GitDirs | None, list[str]]:
    """The cwd's git directories (``unsafe_cwd`` when git cannot determine them; no git found
    counts as none only when no ``.git`` entry is above the cwd), ``no_git_cwd`` for a kind
    that needs one, and the repository-config credential warnings of third-party-reading
    runs."""
    if git is None:
        if gitinfo.git_entry_above(cwd):
            raise LaunchValidationError(
                "git is needed to find the cwd's git directory but was not found",
                end_reason="unsafe_cwd",
            )
        dirs = None
    else:
        try:
            dirs = gitinfo.resolve(
                cwd, git=git.path, git_name=git.name, environ=process.environ, gw=process.gateway
            )
        except LaunchValidationError:
            raise
        except FailedError as exc:
            raise LaunchValidationError(
                exc.message, detail=exc.detail, end_reason="unsafe_cwd"
            ) from exc
    gitinfo.check_git_cwd(resolution.kind, dirs)
    warnings: list[str] = []
    if resolution.profile.reads_third_party and dirs is not None and git is not None:
        warnings = gitinfo.credential_warnings(
            cwd, git=git.path, git_name=git.name, environ=process.environ, gw=process.gateway
        )
    return dirs, warnings


def profile_variables(
    alloc: Allocated, cwd: str, home: str, git_dirs: gitinfo.GitDirs | None
) -> dict[str, str | None]:
    return {
        "CSESSION_DIR": alloc.session_dir,
        "CWD": cwd,
        "HOME": home,
        "GIT_DIR": None if git_dirs is None else git_dirs.common_dir,
    }


def select_profile(
    cfg: Config, resolution: kinds.Resolution, writable: Callable[[str], bool]
) -> permission_profiles.Profile:
    """The run's permission profile (a file is read once, here; expanded once the git
    directory is known)."""
    return permission_profiles.select(
        cfg, resolution, session_writable=writable if resolution.isolated() else None
    )


def check_profile_file(
    resolution: kinds.Resolution, writable: pathrules.SessionWritable | None
) -> None:
    """Isolated runs: the profile file, read against the partial set, must not be reached
    through any session-writable path of the full set (``write_dirs``, the profile's own
    write additions), walked hop by hop (``invalid_permission_profile``)."""
    if writable is None or resolution.permission_profile_file is None:
        return
    hop = pathrules.writable_hop(os.path.abspath(resolution.permission_profile_file), writable)
    if hop is not None:
        raise InvalidPermissionProfile(
            "permission profile file is under a session-writable path", detail=hop
        )


def resolve_run_dirs(
    resolution: kinds.Resolution, variables: Mapping[str, str | None], home: str, alloc: Allocated
) -> tuple[list[str], pathrules.WriteDirs]:
    profile = resolution.profile
    read = pathrules.resolve_dirs(profile.read_dirs, variables, home=home, key="read_dirs")
    resolved = pathrules.resolve_dirs(profile.write_dirs, variables, home=home, key="write_dirs")
    write = pathrules.resolve_write_dirs(resolved, session_dir=alloc.session_dir)
    return read, write


@dataclass(frozen=True)
class RunDirs:
    """A run's resolved directories as ``start`` freezes them: ``read_dirs`` and
    ``write_dirs`` (realpaths) and the realpaths of the expanded profile's write paths."""

    read_dirs: tuple[str, ...]
    write_dirs: tuple[str, ...]
    profile_writes: tuple[str, ...]


def run_dirs(
    read: Sequence[str],
    write_dirs: Sequence[str],
    expanded: Mapping[str, Any],
    cwd: str,
    home: str,
) -> RunDirs:
    writes = pathrules.profile_write_paths(expanded, cwd=cwd, home=home)
    return RunDirs(
        read_dirs=tuple(read),
        write_dirs=tuple(write_dirs),
        profile_writes=tuple(dict.fromkeys(os.path.realpath(path) for path in writes)),
    )


def create_write_dirs(dirs: SessionDirs, write: pathrules.WriteDirs) -> None:
    """Create the ``write_dirs`` inside ``$CSESSION_DIR`` before launch (no-follow, 0700)."""
    for path in write.create:
        dirs.ensure_session_subdir(path)


def full_writable(
    resolution: kinds.Resolution,
    alloc: Allocated,
    cwd: str,
    home: str,
    tmp: str | None,
    write: pathrules.WriteDirs,
    expanded: Mapping[str, Any],
) -> pathrules.SessionWritable | None:
    """The run's session-writable paths (isolated runs; None for trusted ones)."""
    if not resolution.isolated():
        return None
    return pathrules.session_writable(
        session_dir=alloc.session_dir,
        cwd=cwd,
        home=home,
        tmpdir=tmp,
        write_dirs=write.dirs,
        profile=expanded,
    )


def check_paths(
    rules: pathrules.PathRules,
    writable: pathrules.SessionWritable | None,
    read: Sequence[str],
    write: pathrules.WriteDirs,
    expanded: Mapping[str, Any],
) -> None:
    """``read_dirs``, ``write_dirs`` and the expanded profile's paths."""
    if writable is None:
        rules.check_profile(expanded, isolated=False)
        return
    rules.check_read_dirs(read, writable)
    rules.check_write_dirs(write.dirs, writable)
    rules.check_profile(expanded, isolated=True, writable=writable)


def check_plugins(
    rules: pathrules.PathRules,
    plugin_dirs: Sequence[str],
    writable: pathrules.SessionWritable | None,
    caller_cwd: str,
) -> tuple[str, ...]:
    """The ``--plugin-dir`` entries, resolved; each must exist (``plugin_dir_missing``) and,
    for isolated runs, pass the plugin-directory rules (``unsafe_plugin_dir``)."""
    resolved = tuple(
        dict.fromkeys(os.path.realpath(os.path.join(caller_cwd, d)) for d in plugin_dirs)
    )
    if writable is not None:
        rules.check_plugin_dirs(resolved, writable)
        return resolved
    for entry in resolved:
        if not os.path.isdir(entry):
            raise LaunchValidationError(
                f"plugin directory does not exist: {entry!r}", end_reason="plugin_dir_missing"
            )
    return resolved


@dataclass(frozen=True)
class Tools:
    claude: pathrules.ResolvedTool
    git: pathrules.ResolvedTool | None
    bwrap: pathrules.ResolvedTool | None
    socat: pathrules.ResolvedTool | None


def check_tools(
    cfg: Config,
    resolution: kinds.Resolution,
    process: Process,
    writable: Callable[[str], bool],
    git_before: pathrules.ResolvedTool | None,
) -> Tools:
    """``claude_bin``, ``git``, ``bwrap`` and ``socat`` resolved on the sanitized PATH, outside
    the session-writable paths (``unsafe_path``); each runs from its realpath with ``argv[0]``
    its name. ``claude_not_found`` when there is no ``claude_bin``. git must be the one the
    git directory was resolved with (it was looked up before the full set was known)."""
    entries = _path_entries(process.environ, resolution.profile.env.path_prepend)
    claude = pathrules.resolve_tool(cfg.claude_bin, entries, writable)
    if claude is None:
        raise LaunchValidationError(
            f"claude not found: {cfg.claude_bin!r}",
            end_reason="claude_not_found",
            hint=HINTS["claude_not_found"],
        )
    git = pathrules.resolve_tool("git", entries, writable)
    if git != git_before:
        raise LaunchValidationError(
            "git resolved differently once every session-writable path was known",
            end_reason="unsafe_path",
        )
    bwrap = socat = None
    if resolution.profile.sandbox != "off":
        bwrap = pathrules.resolve_tool("bwrap", entries, writable)
        socat = pathrules.resolve_tool("socat", entries, writable)
    return Tools(claude=claude, git=git, bwrap=bwrap, socat=socat)


def build_env(
    resolution: kinds.Resolution,
    cfg: Config,
    process: Process,
    alloc: Allocated,
    cwd: str,
    git_dirs: gitinfo.GitDirs | None,
    tmp: str | None,
    writable: pathrules.SessionWritable | None,
) -> envbuild.SessionEnv:
    """The session environment (its ``PATH`` checked for isolated runs: ``unsafe_path``). An
    invalid ``env.set`` or ``path_prepend`` entry that reached here (config validation refuses
    them) ends the run ``config_invalid``."""
    try:
        return envbuild.build(
            resolution,
            cfg,
            process.environ,
            csession_id=alloc.csession_id,
            csession_dir=alloc.session_dir,
            cwd=cwd,
            exe_dir=process.exe_dir,
            git_dir=None if git_dirs is None else git_dirs.common_dir,
            tmpdir=tmp,
            session_writable=writable,
        )
    except UsageError as exc:
        raise LaunchValidationError(
            exc.message, detail=exc.detail, end_reason="config_invalid"
        ) from exc


def generated_entries(
    resolution: kinds.Resolution,
    tools: Tools,
    git_dirs: gitinfo.GitDirs | None,
    process: Process,
) -> dict[str, tuple[str, ...]]:
    """The read entries ``exec`` adds to an isolated run's ``allowRead`` on its own."""
    git: tuple[str, ...] = () if git_dirs is None else (git_dirs.common_dir,)
    if git_dirs is not None and git_dirs.worktree_git_dir is not None:
        git = (*git, git_dirs.worktree_git_dir)
    paths = [t.path for t in (tools.bwrap, tools.socat, tools.git, tools.claude) if t is not None]
    return {
        "system": settings_builder.system_read_dirs(),
        "tools": settings_builder.tool_dirs(paths),
        "python": settings_builder.python_read_dirs(executable_dir=process.exe_dir),
        "path_prepend": tuple(
            dict.fromkeys(os.path.realpath(p) for p in resolution.profile.env.path_prepend)
        ),
        "git": git,
    }


def check_tmpdir(resolution: kinds.Resolution, alloc: Allocated) -> None:
    """The temporary-directory check of a sandboxed run (``tmpdir_unsafe``); nothing is
    created."""
    if resolution.profile.sandbox != "off":
        tmpdir.check(alloc.csession_id)


def check_sandbox(
    resolution: kinds.Resolution,
    alloc: Allocated,
    tools: Tools,
    process: Process,
    *,
    create: bool = True,
) -> selfcheck.SelfCheck:
    """``claude_args`` and setting sources, then the sandbox self-check, which creates the
    private tmpdir: the last action of step 5 (``unsafe_claude_args``,
    ``unsafe_setting_sources``, ``sandbox_unavailable``, ``socket_path_too_long``). A dry run
    passes ``create=False``: the same checks, nothing created."""
    profile = resolution.profile
    pathrules.check_claude_args(profile.claude_args, isolated=resolution.isolated())
    settings_builder.check_setting_sources(resolution)
    return selfcheck.self_check(
        profile.sandbox,
        alloc.csession_id,
        bwrap=None if tools.bwrap is None else tools.bwrap.path,
        socat=None if tools.socat is None else tools.socat.path,
        gw=process.gateway,
        create=create,
    )


def check_hook_interpreter(process: Process, writable: pathrules.SessionWritable | None) -> None:
    """Isolated runs: nothing the unsandboxed hook interpreter loads code from may be
    session-writable, and the install has no hard-linked files (``unsafe_path``)."""
    if writable is None:
        return
    settings_builder.check_interpreter(
        process.interpreter, session_writable=writable, writable_paths=writable.paths
    )


class Effects:
    """The side effects inside the launch validations (``checks``): what a launch records
    and creates between the checks. ``validate`` passes ``Recording``; a dry run passes this
    base class, which records and creates nothing.

    - ``planned``: whether the run's own directories may not exist yet (dry run;
      ``pathrules.PathRules``).
    - ``git_cwd``: where git looks for the cwd's git directory (a dry run's ``work/`` that
      does not exist yet: its nearest existing parent, which a new empty directory inherits
      the git directory from).
    - ``record_profile``: the permission profile, once read (a dry run reports its label
      also when a later check fails).
    - ``warn``: each warning as ``checks`` finds it, in order (a dry run keeps them when a
      later check fails); ``Checked.warnings`` holds the same list either way.
    """

    planned = True

    def record_cwd(self, cwd: str) -> None:
        pass

    def git_cwd(self, cwd: str) -> str:
        current = cwd
        while not os.path.isdir(current) and os.path.dirname(current) != current:
            current = os.path.dirname(current)
        return current

    def record_git_dir(self, git_dirs: gitinfo.GitDirs | None) -> None:
        pass

    def record_profile(self, profile: permission_profiles.Profile) -> None:
        pass

    def select_profile(
        self, cfg: Config, resolution: kinds.Resolution, writable: Callable[[str], bool]
    ) -> permission_profiles.Profile:
        """The run's permission profile (the adopting ``exec``: the one ``start`` froze)."""
        return select_profile(cfg, resolution, writable)

    def record_plugin_dirs(self, plugin_dirs: Sequence[str]) -> None:
        pass

    def record_dirs(self, dirs: Callable[[], RunDirs]) -> None:
        """The resolved ``read_dirs``, ``write_dirs`` and profile write paths (computed by
        calling ``dirs``), once checked: the adopting ``exec`` compares them with what
        ``start`` froze."""

    def warn(self, warnings: Sequence[str]) -> None:
        pass

    def create_write_dirs(self, write: pathrules.WriteDirs) -> None:
        pass

    def sandbox(
        self, resolution: kinds.Resolution, alloc: Allocated, tools: Tools, process: Process
    ) -> selfcheck.SelfCheck:
        return check_sandbox(resolution, alloc, tools, process, create=False)


class Recording(Effects):
    """The effects of a real launch: the cwd and git directory recorded on the run and
    session, the ``write_dirs`` inside ``$CSESSION_DIR`` created and the private tmpdir
    created (and recorded) by the self-check."""

    planned = False

    def __init__(self, st: store.Store, alloc: Allocated, dirs: SessionDirs) -> None:
        self.st = st
        self.alloc = alloc
        self.dirs = dirs

    def record_cwd(self, cwd: str) -> None:
        self.st.update_run(self.alloc.csession_id, self.alloc.run, cwd=cwd)

    def git_cwd(self, cwd: str) -> str:
        return cwd

    def record_git_dir(self, git_dirs: gitinfo.GitDirs | None) -> None:
        self.st.update_session(self.alloc.csession_id, git_dir=gitinfo.stored_value(git_dirs))

    def create_write_dirs(self, write: pathrules.WriteDirs) -> None:
        create_write_dirs(self.dirs, write)

    def sandbox(
        self, resolution: kinds.Resolution, alloc: Allocated, tools: Tools, process: Process
    ) -> selfcheck.SelfCheck:
        check = check_sandbox(resolution, alloc, tools, process)
        if check.tmpdir is not None:
            self.st.update_session(alloc.csession_id, tmp_dir=check.tmpdir)
        return check


def checks(
    cfg: Config,
    req: Request,
    resolved: Resolved,
    alloc: Allocated,
    process: Process,
    effects: Effects,
) -> Checked:
    """Launch step 5 after the rule paths and the session directories, in the order
    interface.md (Launch order) lists: ``$HOME``, the cwd, the permission profile read (its
    write entries and ``write_dirs`` without ``${GIT_DIR}`` expanded), git and the git
    directory, the profile expanded, ``read_dirs``/``write_dirs``, the profile file against
    the full set, the profile's paths, plugin directories, the hook interpreter, the tmpdir,
    the tools (``claude_bin``, ``git``, ``bwrap``, ``socat``), the session environment
    (``PATH``), the generated read entries, ``claude_args``, setting sources and the sandbox
    self-check, which creates the private tmpdir last. What a launch records and creates in
    between goes through ``effects``. Raises ``LaunchValidationError``; the first failure
    wins."""
    resolution = resolved.resolution
    isolated = resolution.isolated()
    home = os.path.realpath(process.environ.get("HOME", "/"))
    tmp = None if resolution.profile.sandbox == "off" else tmpdir.run_path(alloc.csession_id)
    cwd = os.path.realpath(resolved.caller_cwd or os.path.join(alloc.session_dir, sessiondir.WORK))
    rules = host_rules(process, resolution, alloc, cwd, tmp, planned=effects.planned)
    check_cwd(rules, resolution)
    effects.record_cwd(cwd)
    profile = effects.select_profile(
        cfg, resolution, base_writable(resolution, alloc, cwd, home, tmp)
    )
    effects.record_profile(profile)
    git_cwd = effects.git_cwd(cwd)
    before_git = git_writable(resolution, alloc, cwd, home, tmp, profile, git_cwd)
    entries = _path_entries(process.environ, resolution.profile.env.path_prepend)
    git_tool = pathrules.resolve_tool("git", entries, before_git)
    git_dirs, credential = check_git(resolution, git_cwd, git_tool, process)
    warnings = list(credential)
    effects.warn(credential)
    effects.record_git_dir(git_dirs)
    variables = profile_variables(alloc, cwd, home, git_dirs)
    expanded = permission_profiles.expand(profile.content, variables)
    read, write = resolve_run_dirs(resolution, variables, home, alloc)
    warnings += write.warnings
    effects.warn(write.warnings)
    effects.create_write_dirs(write)
    writable = full_writable(resolution, alloc, cwd, home, tmp, write, expanded)
    check_profile_file(resolution, writable)
    check_paths(rules, writable, read, write, expanded)
    effects.record_dirs(lambda: run_dirs(read, write.dirs, expanded, cwd, home))
    plugins = check_plugins(rules, req.plugin_dirs, writable, process.caller_cwd)
    effects.record_plugin_dirs(plugins)
    check_hook_interpreter(process, writable)
    check_tmpdir(resolution, alloc)
    tools = check_tools(cfg, resolution, process, writable or NO_SANDBOX, git_tool)
    session_env = build_env(resolution, cfg, process, alloc, cwd, git_dirs, tmp, writable)
    generated = generated_entries(resolution, tools, git_dirs, process)
    if isolated:
        rules.check_generated(generated)
    check = effects.sandbox(resolution, alloc, tools, process)
    warnings += check.warnings
    effects.warn(check.warnings)
    isolation = None
    if isolated:
        isolation = settings_builder.Isolation(
            home=home,
            tmpdir=check.tmpdir,
            git_dir=None if git_dirs is None else git_dirs.common_dir,
            worktree_git_dir=None if git_dirs is None else git_dirs.worktree_git_dir,
            read_dirs=tuple(read),
            write_dirs=write.dirs,
            system_dirs=generated["system"],
            tool_dirs=generated["tools"],
            python_dirs=generated["python"],
            path_prepend=generated["path_prepend"],
        )
    isolation_warnings = settings_builder.isolation_warnings(resolution)
    warnings += isolation_warnings
    effects.warn(isolation_warnings)
    return Checked(
        cwd=cwd,
        git_dirs=git_dirs,
        profile=profile,
        expanded=expanded,
        read_dirs=tuple(read),
        write_dirs=write.dirs,
        plugin_dirs=plugins,
        claude=tools.claude,
        git=tools.git,
        bwrap=tools.bwrap,
        socat=tools.socat,
        session_env=session_env,
        isolation=isolation,
        sandbox_active=check.sandbox_active,
        tmpdir=check.tmpdir,
        warnings=warnings,
        credential_warnings=tuple(credential),
        sandbox_problems=check.problems,
    )


EffectsFactory = Callable[[store.Store, Allocated, SessionDirs], Effects]


def validate(
    st: store.Store,
    cfg: Config,
    req: Request,
    resolved: Resolved,
    alloc: Allocated,
    process: Process,
    *,
    effects: EffectsFactory | None = None,
) -> Validated:
    """Launch step 5, in the order interface.md (Launch order) lists: rule paths, the
    session directories (``open_dirs``), then ``checks`` with the effects of a real launch
    (``Recording``, or ``effects``: ``start`` and the adopting ``exec``). Raises
    ``LaunchValidationError``; the first failure wins."""
    settings_builder.check_rule_paths(process.state_root, process.claude_session)
    dirs, layout = open_dirs(alloc, process, isolated=resolved.resolution.isolated())
    try:
        made = (effects or Recording)(st, alloc, dirs)
        checked = checks(cfg, req, resolved, alloc, process, made)
    except BaseException:
        dirs.close()
        raise
    values = {f.name: getattr(checked, f.name) for f in dataclasses.fields(checked)}
    values["warnings"] = list(dict.fromkeys([*checked.warnings, *layout.warnings]))
    return Validated(dirs=dirs, layout=layout, **values)


# Audit and log


def event_log(process: Process, secrets: Sequence[str] = ()) -> logs.EventLog:
    return logs.EventLog(os.path.join(process.state_root, LOG_NAME), secrets=secrets)


def log_best_effort(log: logs.EventLog, event: str, **fields: Any) -> str | None:
    """Write a ``log.jsonl`` record; a failure is returned as a warning, never raised."""
    try:
        log.write(event, **fields)
    except (OSError, ValueError) as exc:
        return f"could not write log.jsonl ({type(exc).__name__})"
    return None


def audit_sandbox_lowered(
    process: Process,
    req: Request,
    resolution: kinds.Resolution,
    alloc: Allocated,
    secrets: Sequence[str],
) -> None:
    """A third-party-reading run below the ``required`` sandbox baseline: the
    ``sandbox_lowered`` audit record (raises when it cannot be written: the run must not
    launch without it) and the matching log event."""
    if not resolution.sandbox_lowered():
        return
    inputs = settings_builder.sandbox_lowered_inputs(resolution, run=alloc.run)
    audit.append(
        os.path.join(process.state_root, AUDIT_NAME),
        actor=req.actor,
        target=alloc.csession_id,
        action="sandbox_lowered",
        result="ok",
        decision_ids=req.decision_ids,
        inputs=inputs,
        secrets=secrets,
    )
    log_best_effort(
        event_log(process, secrets),
        "sandbox_lowered",
        level="warn",
        csession_id=alloc.csession_id,
        run=alloc.run,
        message=f"sandbox {resolution.profile.sandbox} (baseline required)",
    )


# Step 7: prepare


@dataclass
class Prepared:
    """What step 7 wrote and the supervisor needs: the ``claude`` argv (``argv[0]`` the
    tool's name, run from ``executable``), the run's secret values (for log redaction) and
    the run secret (refused in an implicit ``result.md``), and the descriptors for its stdout
    and stderr (the supervisor closes them)."""

    argv: list[str]
    executable: str
    secrets: frozenset[str] = field(repr=False)
    run_secret: str = field(repr=False)
    stdout_fd: int
    stderr_fd: int
    warnings: list[str]


def secret_index(values: frozenset[str]) -> bytes:
    """The run's ``contains_secret`` index. Values over ``secretindex.VALUES_MAX_BYTES`` in
    total fail the launch: no end reason names this, and the values come from the
    environment variables the configured ``claude_auth_env`` and ``secret_env`` names select,
    so it is ``config_invalid``."""
    try:
        return secretindex.build(values)
    except ValueError as exc:
        raise LaunchValidationError(
            f"secret values exceed {secretindex.VALUES_MAX_BYTES // 1024} KiB: the values of"
            " the claude_auth_env and secret_env variables and the run secret are too large"
            " to index for contains_secret",
            end_reason="config_invalid",
        ) from exc


def generate(
    req: Request,
    resolution: kinds.Resolution,
    alloc: Allocated,
    v: Checked,
    process: Process,
    prompt_path: str | None,
) -> settings_builder.Generated:
    """The run's generated settings and ``claude`` argv (``settings_builder.build``), from
    what step 5 resolved; ``prompt_path`` is the prompt's path in ``$CSESSION_DIR`` (None:
    no prompt). Pure: ``prepare`` writes the result, a dry run prints it."""
    paths = settings_builder.Paths(
        state_root=process.state_root, csession_id=alloc.csession_id, run=alloc.run
    )
    tools = settings_builder.Tools(
        interpreter=process.interpreter,
        claude_session=process.claude_session,
        claude=v.claude.path,
    )
    launch = settings_builder.Launch(
        mode="headless" if req.mode == "headless" else "interactive",
        cwd=v.cwd,
        skill=req.skill,
        prompt_path=prompt_path,
        plugin_dirs=v.plugin_dirs,
    )
    return settings_builder.build(
        resolution, v.expanded, paths, tools, launch, isolation=v.isolation
    )


def prepare(
    st: store.Store,
    cfg: Config,
    req: Request,
    resolution: kinds.Resolution,
    alloc: Allocated,
    v: Validated,
    process: Process,
    *,
    staged: bool = False,
) -> Prepared:
    """Launch step 7: the prompt and inputs (``staged``: the adopting ``exec`` moves the
    copies ``start`` staged into place, ``place_staged``; they must be the prompt and input
    names the run recorded), ``instructions.md``, ``run.json`` and a new secret (its digest
    and the directory identities recorded), the secret index (before the agent can file a
    report), the hook configuration, the profile snapshot, the generated settings and the
    output files."""
    dirs, run, sid = v.dirs, alloc.run, alloc.csession_id
    # TODO(#29): drain (ingest or discard) the previous run's outbox before the secret rotates.
    if staged:
        placed = dirs.place_staged(run)
        expected = sorted(item.name for item in req.inputs)
        if (placed.prompt_path is not None) != (req.prompt is not None) or sorted(
            placed.input_names
        ) != expected:
            raise sessiondir.SessionDirUnsafe(
                "the staged prompt and inputs differ from the ones start recorded"
            )
        prompt_path, names = placed.prompt_path, list(placed.input_names)
    else:
        prompt_path = None if req.prompt is None else dirs.write_prompt(run, req.prompt)
        names = dirs.write_inputs(run, req.inputs)
    secret = dirs.write_run_files(
        run,
        claude_session_path=process.claude_session,
        summary_max_bytes=cfg.summary_max_bytes,
        artifact_max_bytes=cfg.artifact_max_bytes,
        input_files=names,
        respond=resolution.kind == "respond" and not resolution.fallback,
    )
    st.record_run_files(
        sid,
        run,
        secret_sha256=secret.sha256,
        outbox_dev_ino=v.layout.outbox.text,
        artifacts_dev_ino=v.layout.artifacts.text,
    )
    values = envbuild.secret_values(v.session_env, process.environ, run_secret=secret.value)
    dirs.write_private_run_file(run, secretindex.INDEX_NAME, secret_index(values))
    dirs.write_private_run_file(
        run,
        hookevent.HOOKCONF_NAME,
        hookevent.hookconf_bytes(
            names,
            summary_max_bytes=cfg.summary_max_bytes,
            artifact_max_bytes=cfg.artifact_max_bytes,
        ),
    )
    snapshot = None
    if v.profile.is_file:
        # Adoption rewrites start's snapshot with the same bytes (read back with its label).
        snapshot = dirs.write_private_run_file(run, PROFILE_SNAPSHOT, v.profile.snapshot_bytes())
    generated = generate(req, resolution, alloc, v, process, prompt_path)
    dirs.write_settings(run, settings_builder.settings_json(generated.settings))
    fields: dict[str, Any] = {
        "permission_profile": v.profile.label,
        "permission_profile_snapshot": snapshot,
        "sandbox_active": int(v.sandbox_active),
    }
    if not resolution.profile.backstop:
        fields["backstop_used"] = 1  # the kind turns the Stop backstop off: never taken
    st.update_run(sid, run, **fields)
    warnings = [*v.warnings, *generated.warnings]
    stdout_fd = dirs.create_run_output(run, RESULT_FILE)
    try:
        stderr_fd = dirs.create_run_output(run, STDERR_FILE)
    except BaseException:
        os.close(stdout_fd)
        raise
    return Prepared(
        argv=[v.claude.name, *generated.argv[1:]],
        executable=v.claude.path,
        secrets=values,
        run_secret=secret.value,
        stdout_fd=stdout_fd,
        stderr_fd=stderr_fd,
        warnings=warnings,
    )


# Launch-phase end


def failure_message(error: LaunchValidationError) -> str:
    """The outcome ``message`` of a launch failure: the error's message and detail, then its
    hint (or the fixed hint of its end reason), control characters stripped, at most
    ``MESSAGE_MAX`` characters."""
    text = error.message
    if error.detail:
        text += f": {error.detail}"
    hint = error.hint or HINTS.get(error.end_reason)
    if hint:
        text += f"; {hint}"
    return _CONTROL.sub("", text)[:MESSAGE_MAX]


def fail_launch(st: store.Store, alloc: Allocated, error: LaunchValidationError) -> bool:
    """End the run ``not-started`` with the error's end reason (exit 12) and its
    ``failure_message``. Returns whether this call finalized the run.

    TODO(#20): the one final ``post-state-change`` of a launch-phase end.
    TODO(#21): the ``--outcome-file`` of a launch-phase end, after the compare-and-set."""
    return st.finalize_run(
        alloc.csession_id,
        alloc.run,
        store.Final(
            phase="launch",
            outcome="not-started",
            end_reason=error.end_reason,
            exit_code=EXIT_CODES["not-started"],
            state="not-started",
            cause=alloc.cause,
            message=failure_message(error),
        ),
    )


def interrupted(exc: BaseException, message: str | None = None) -> LaunchValidationError:
    """The ``launch_interrupted`` error for ``exc``, an exception before the spawn."""
    if message is None:
        if isinstance(exc, KeyboardInterrupt):
            message = "launch interrupted by SIGINT"
        else:
            message = f"launch interrupted by an internal error ({type(exc).__name__})"
    return LaunchValidationError(message, end_reason="launch_interrupted")


def end_launch(
    st: store.Store,
    alloc: Allocated,
    v: Validated | None,
    prepared: Prepared | None,
    process: Process,
    error: LaunchValidationError,
    *,
    exc: BaseException | None = None,
) -> None:
    """A launch-phase end: finalize the run (``fail_launch``), then, whatever happens, close
    the output descriptors, delete the secret index, remove the private tmpdir and log the
    failure (``launch`` with the end reason; ``launch_interrupted`` with the exception type
    for an ``exc``)."""
    try:
        fail_launch(st, alloc, error)
    finally:
        if prepared is not None:
            for fd in (prepared.stdout_fd, prepared.stderr_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
        warnings = _cleanup(alloc, v)
        if warnings:
            try:
                st.add_run_warnings(alloc.csession_id, alloc.run, warnings)
            except Exception:  # best effort: the run is final either way
                pass
        log = event_log(process, () if prepared is None else sorted(prepared.secrets))
        fields: dict[str, Any] = {"csession_id": alloc.csession_id, "run": alloc.run}
        if exc is None:
            log_best_effort(log, "launch", level="error", reason=error.end_reason, **fields)
        else:
            log_best_effort(
                log,
                "launch_interrupted",
                level="error",
                reason="launch_interrupted",
                message=type(exc).__name__,
                **fields,
            )


def log_credential_warnings(
    process: Process, alloc: Allocated, warnings: Sequence[str], secrets: Sequence[str]
) -> None:
    """One ``config_warning`` log record per repository-config credential warning (best
    effort)."""
    log = event_log(process, secrets)
    for warning in warnings:
        log_best_effort(
            log,
            "config_warning",
            level="warn",
            csession_id=alloc.csession_id,
            run=alloc.run,
            message=warning,
        )


# The launch as a whole (steps 5 to 7 and the hand-over to supervision)


def _cleanup(alloc: Allocated, v: Validated | None) -> list[str]:
    """After a launch-phase end: the secret index and the private tmpdir are removed (best
    effort; a leftover tmpdir is reported as a run warning and is reconcile's, #33). The
    tmpdir is removed whenever it exists, also when the failure came before ``validate``
    returned."""
    warnings: list[str] = []
    if v is not None:
        try:
            run_fd = v.dirs.open_run_dir(alloc.run)
            try:
                os.unlink(secretindex.INDEX_NAME, dir_fd=run_fd)
            finally:
                os.close(run_fd)
        except OSError:
            pass
    if os.path.lexists(tmpdir.run_path(alloc.csession_id)):
        try:
            tmpdir.remove(alloc.csession_id)
        except tmpdir.TmpdirLeftover as exc:
            warnings.append(f"{exc}; {exc.hint}")
        except OSError:
            pass
    return warnings


def run(
    st: store.Store,
    cfg: Config,
    req: Request,
    resolved: Resolved,
    alloc: Allocated,
    process: Process,
    *,
    supervise: Callable[..., Any],
    effects: EffectsFactory | None = None,
    staged: bool = False,
) -> Any:
    """Steps 5 and 7 of an allocated run, then ``supervise`` (``supervisor.run_headless``,
    which spawns ``claude``). A launch validation failure (or an unsafe session directory)
    ends the run ``not-started`` and returns None (the outcome is in the store); so does any
    other error before ``claude`` is spawned (``launch_interrupted``), except a
    ``KeyboardInterrupt`` (or another ``BaseException``), which is re-raised once the run is
    final. The adopting ``exec`` passes its ``effects`` (``validate``) and ``staged``
    (``prepare``)."""
    resolution = resolved.resolution
    v: Validated | None = None
    prepared: Prepared | None = None
    try:
        try:
            v = validate(st, cfg, req, resolved, alloc, process, effects=effects)
            secrets = sorted(envbuild.secret_values(v.session_env, process.environ))
            log_credential_warnings(process, alloc, v.credential_warnings, secrets)
            # The audit record is written before the run secret exists (prepare rotates it),
            # so it is redacted against the session's secret values only; prepared.secrets,
            # used for the log from here on, is that set plus the run secret.
            audit_sandbox_lowered(process, req, resolution, alloc, secrets)
            st.add_run_warnings(alloc.csession_id, alloc.run, v.warnings)
            prepared = prepare(st, cfg, req, resolution, alloc, v, process, staged=staged)
            st.add_run_warnings(alloc.csession_id, alloc.run, prepared.warnings)
        except LaunchValidationError as error:
            end_launch(st, alloc, v, prepared, process, error)
            return None
        except Exception as exc:
            end_launch(st, alloc, v, prepared, process, interrupted(exc), exc=exc)
            return None
        except BaseException as exc:
            end_launch(st, alloc, v, prepared, process, interrupted(exc), exc=exc)
            raise
        return supervise(
            st, cfg, alloc, v, prepared, process, implicit_done=resolution.profile.implicit_done
        )
    finally:
        if v is not None:
            v.dirs.close()
