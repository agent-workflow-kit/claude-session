"""``--dry-run`` of the launch commands and the ``kinds`` command (interface.md, Dry run and
kinds; schemas/cli/exec-dry-run.json and kinds.json).

A dry run reuses the launch pipeline (``launch``) without its side effects: ``read_request``
and ``resolve_kind`` (step 2), then the step-5 checks in their documented order
(``settings_builder.check_rule_paths``, ``sessiondir.check_dirs`` in place of
``launch.open_dirs``, and ``launch.checks`` with ``launch.Effects``, which records and creates
nothing: ``tmpdir.check`` and ``self_check(create=False)``), then ``launch.generate`` (the
settings and argv step 7 would write). It never allocates, opens no session directory, runs
no hook and writes no audit or log record. The processes it starts are the read-only probes
of a launch: ``git`` (repo id and git directory) and the sandbox prerequisite probes.

A new session has no id yet: its paths (session directory, private tmpdir, settings file)
are shown with the placeholder id ``PLACEHOLDER_ID``, and its own ``work/``, ``outbox/`` and
``artifacts/`` count as the real directories the launch would create
(``pathrules.PathRules(planned=True)``).

``kinds`` resolves every kind (or one) without a session and without the session-writable
checks of a launch (it has no session); it probes ``claude --version`` and the sandbox
prerequisites.

TODO(#28): ``start --resume --dry-run`` and ``exec --session-id <id> --dry-run`` (inspect
the pending run without adopting it: ``would_refuse`` ``already_adopted``/``expired``,
``config_hash_changed``).
TODO(#29): ``resume --dry-run`` (carry-over from the previous run, the liveness check that
only reports, ``would_refuse`` ``live``/``tainted``, the run number, ``fork``).
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from . import (
    claude_version,
    durations,
    envbuild,
    kinds,
    launch,
    pathrules,
    selfcheck,
    sessiondir,
    settings_builder,
    store,
    tmpdir,
)
from .config import Config
from .errors import LaunchValidationError, UsageError
from .gateway import Gateway

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


# Shared pieces


@dataclass(frozen=True)
class Sandbox:
    """The sandbox prerequisites as a run of a kind would find them."""

    available: bool
    problems: tuple[str, ...] = ()


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
    ``interactive_timeout``)."""
    origins: dict[str, str] = {}
    for key, origin in resolution.origins.items():
        origins["env.set_names" if key == "env.set" else key] = origin
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


def _tool(name: str, entries: Sequence[str]) -> pathrules.ResolvedTool | None:
    """A tool on the sanitized ``PATH`` (no session: no session-writable check); None when
    it is not found or its name is unusable."""
    try:
        return pathrules.resolve_tool(name, entries, launch.NO_SANDBOX)
    except LaunchValidationError:
        return None


class Prerequisites:
    """The sandbox prerequisite probes, run once per ``bwrap``/``socat`` pair."""

    def __init__(self, gw: Gateway, platform: str | None = None) -> None:
        self.gw = gw
        self.platform = selfcheck.detect_platform() if platform is None else platform
        self._seen: dict[tuple[str | None, str | None], Sandbox] = {}

    def check(self, process: launch.Process, resolution: kinds.Resolution) -> Sandbox | None:
        """None for sandbox ``off``; else whether the prerequisites are present here."""
        if resolution.profile.sandbox == "off":
            return None
        entries = _entries(process, resolution)
        bwrap, socat = _tool("bwrap", entries), _tool("socat", entries)
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
    cfg: Config, resolution: kinds.Resolution, process: launch.Process, claude: str | None
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
        tools = [_tool(name, entries) for name in ("bwrap", "socat", "git")]
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
    claude = _tool(cfg.claude_bin, _entries(process, default))
    version = (
        None if claude is None else claude_version.probe(claude, process.environ, process.gateway)
    )
    warnings: list[str] = []
    if claude is None:
        warnings.append(f"claude not found: {cfg.claude_bin!r}")
    elif version is None:
        warnings.append(f"could not run {claude.path} --version")
    names = [kind] if kind is not None else list(cfg.kinds)
    profiles = []
    for name in names:
        resolution = kinds.resolve(cfg, name, mode="headless", repo=repo, cwd=None)
        warnings += resolution.warnings
        sandbox = probes.check(process, resolution)
        profiles.append(
            profile_json(
                resolution,
                permission_profile=resolution.profile.permission_profile,
                sandbox=sandbox,
                preview=preview(cfg, resolution, process, None if claude is None else claude.path),
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


def _sandbox_warnings(resolution: kinds.Resolution, sandbox: Sandbox | None) -> list[str]:
    warnings = list(settings_builder.isolation_warnings(resolution))
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


def request(args: argparse.Namespace, cfg: Config) -> launch.Request:
    """``launch.read_request`` for any mode.

    TODO(#17): call ``read_request`` as it is once it accepts interactive mode (and keep
    its interactive TTY checks for ``exec``, never for ``start``)."""
    mode = args.mode or "interactive"
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
    try:
        settings_builder.check_rule_paths(process.state_root, process.claude_session)
        sessiondir.check_dirs(process.state_root, alloc.csession_id)
        found.checked = launch.checks(cfg, req, resolved, alloc, process, launch.Effects())
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
        found.warnings.append(f"a launch would fail ({error.end_reason}): {found.message}")
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
    alloc = launch.Allocated(
        csession_id=PLACEHOLDER_ID,
        run=1,
        session_dir=sessiondir.session_dir(process.state_root, PLACEHOLDER_ID),
        private_dir=sessiondir.private_dir(process.state_root, PLACEHOLDER_ID),
        warnings=resolution.warnings,
    )
    found = run_checks(cfg, req, resolved, alloc, process)
    probes = prerequisites or Prerequisites(process.gateway)
    sandbox = probes.check(process, resolution)
    if found.checked is not None and sandbox is not None and found.checked.sandbox_active:
        sandbox = Sandbox(available=True)
    plan = plan_json(
        cfg,
        req,
        resolution,
        found,
        sandbox=sandbox,
        preview=_plan_preview(cfg, resolution, process, found),
        cwd=found.checked.cwd if found.checked is not None else _cwd(resolved, alloc),
        allow_tainted=bool(getattr(args, "allow_tainted", False)),
        override_escalation=bool(getattr(args, "override_escalation", False)),
    )
    return plan, found.message


def _cwd(resolved: launch.Resolved, alloc: launch.Allocated) -> str:
    return os.path.realpath(resolved.caller_cwd or os.path.join(alloc.session_dir, sessiondir.WORK))


def _plan_preview(
    cfg: Config, resolution: kinds.Resolution, process: launch.Process, found: Outcome
) -> Preview:
    """The profile's ``PATH``, directories and required names: from the launch checks when
    they passed, else as ``kinds`` computes them."""
    checked = found.checked
    if checked is None:
        return preview(cfg, resolution, process, None)
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
    label = (
        found.checked.profile.label
        if found.checked is not None
        else None
        if resolution.permission_profile_file is not None
        else profile.permission_profile
    )
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
            permission_profile=label or profile.permission_profile,
            sandbox=sandbox,
            preview=preview,
            warnings=[*resolution.warnings, *_sandbox_warnings(resolution, sandbox)],
        ),
        "origins": origins_json(resolution, mode),
        "settings": settings,
        "claude_argv": [] if generated is None else list(generated.argv),
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


def exit_code(plan: Mapping[str, Any]) -> int:
    """20 with ``would_refuse``, else 12 with ``would_fail``, else 0."""
    if plan["would_refuse"] is not None:
        return 20
    return 12 if plan["would_fail"] is not None else 0
