"""``start`` and the adopting ``exec --session-id`` (interface.md, Launch order, start and
Flags at start and adoption; design.md, Launch, adoption and pending runs).

``start`` performs launch steps 2 to 5 for a new session (``launch.read_request`` through
``dryrun.request``, ``resolve_kind``, ``allocate`` with ``Pending`` and itself as the run's
wrapper, ``validate``), then freezes the run: the prompt and input copies are staged
(``SessionDirs.stage``), a file permission profile is snapshotted, and the launch spec
(``SPEC_NAME``, below) is written to the run's private directory. Only then does the run
become pending (``Store.release_pending``: no wrapper, ``expires_at`` = now +
``lost_grace``), so no adopter can take it before it is frozen. A step-5 failure ends the
run ``not-started`` (``launch.end_launch``) and ``start`` exits 12 with ``launch_failed``.
A ``start`` that dies before the run is pending leaves a launch-phase run whose wrapper is
gone; the liveness check ends it ``launch_interrupted`` and deletes ``runs/<n>/launch-spec.json``
(``discard_spec_at``).

The launch spec holds what the adopting ``exec`` must not take from anywhere else: the
fully resolved kind profile with its origins (``env.set`` values included, which
``resolved_json`` leaves out), the resolved cwd and plugin directories, the permission
profile (content, or a file profile's snapshot), the config values a session uses beyond
those the adopting ``exec`` reads from the config again (``claude_auth_env``,
``secret_env`` and the caps), the resolved ``read_dirs``, ``write_dirs`` and profile write
paths, and the per-invocation flags. It is private (never in the session directory) and
0600, and it may hold credentials (``env.set`` values): it is deleted once the adopting
``exec`` has read it, when ``start`` fails after writing it, and when the run expires
unadopted (``discard_spec``).

The adopting ``exec`` (``adopt``) runs the compare-and-set (``Store.adopt``; refusals exit 20
``already_adopted`` or ``expired``, an expired pending run is finalized ``never_adopted``
first; a forgotten session exits 10), then, owning the run, reloads the config from the
stored path (``config_invalid`` when it is invalid; a changed hash warns), reads the spec,
checks the TTY of an interactive run (``no_tty``) and reruns every step-5 check against its
own environment through ``launch.run`` with ``Adopting`` effects: the cwd and plugin
directories must resolve to the realpaths ``start`` stored (``unsafe_cwd``,
``unsafe_plugin_dir``), and so must ``read_dirs``, ``write_dirs`` and the profile's write
paths (``invalid_permission_profile``), the git directory must be the stored one
(``git_dir_changed``), and the permission profile is the frozen one. Step 7 then moves the
staged copies into place. The current config may only tighten what ``start`` froze: its
``secret_env`` names (and ``claude_auth_env`` names ``start`` did not freeze) are stripped
and indexed too, but only the frozen ``claude_auth_env`` names are passed.

``start --resume`` (a pending run n, or a fork's run 1) reuses ``start`` with the request
and allocation of ``resume``; its launch spec also freezes the conversation (``--resume``,
``--fork-session``, ``--session-id``), and its adopting ``exec`` drains the previous run's
outbox (a fork's run 1 places its inputs beside the copied ones). ``start --resume
--dry-run`` is ``resume.plan``.
TODO(#31): insert step 6 (the ``pre-launch`` hooks) of the adopting ``exec`` before its fork
taint re-check (``launch.recheck_fork_taint``).
The adopting ``exec`` installs its signal handlers before the compare-and-set and defers
them from just before it (a signal then acts once the run is its own, in the launch phase);
an interactive run is handed the terminal as plain ``exec`` hands it.
TODO(#32): ``stop`` canceling a pending run deletes its launch spec too (``discard_spec``).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import typing
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import (
    config,
    dryrun,
    gitinfo,
    ids,
    kinds,
    launch,
    outcomefile,
    pathrules,
    permission_profiles,
    sessiondir,
    store,
    taint,
    tmpdir,
    toolhooks,
)
from .config import Config
from .defaults import MAX_INTEGER
from .errors import (
    FailedError,
    InternalError,
    LaunchFailedError,
    LaunchValidationError,
    NotFoundError,
    OutcomeFileRejected,
    RefusedError,
    UsageError,
)
from .sessiondir import InputFile, SessionDirs
from .signals import ExecSignals

SPEC_NAME = "launch-spec.json"
SPEC_VERSION = 1
SPEC_MAX_BYTES = 8 << 20
# Config values frozen in the spec (the adopting exec reads the rest from the config again).
# TODO(#38): decide whether notes_context_cmd, notes_context_max_bytes and
# notes_context_sources are frozen at start (interface.md lists only the notes timeouts as
# read again at adoption).
FROZEN_CONFIG = (
    "claude_auth_env",
    "secret_env",
    "summary_max_bytes",
    "artifact_max_bytes",
    "input_max_bytes",
)
NO_TTY_HINT = "place the start argv in a terminal (stdin and stdout must be TTYs)"
SPEC_KEYS = frozenset(
    {
        "version",
        "resolution",
        "request",
        "caller_cwd",
        "cwd",
        "plugin_dirs",
        "read_dirs",
        "write_dirs",
        "profile_writes",
        "profile",
        "config",
    }
)
RESOLUTION_KEYS = frozenset(
    {
        "kind",
        "builtin",
        "fallback",
        "repo",
        "profile",
        "origins",
        "warnings",
        "env_allow",
        "env_allowlist_only",
        "permission_profile_file",
        "permission_profile_label",
    }
)
REQUEST_KEYS = frozenset(
    {
        "mode",
        "skill",
        "repo",
        "labels",
        "flags",
        "actor",
        "decision_ids",
        "hooks",
        "hook_timeouts",
        "has_prompt",
        "input_files",
        "conversation",
    }
)
CONVERSATION_KEYS = frozenset({"resume", "resume_from", "fork", "session_id"})
PROFILE_KEYS = frozenset({"name", "content", "snapshot"})
ORIGINS = frozenset(typing.get_args(kinds.Origin))
MODES = ("interactive", "headless")


# The launch spec


@dataclass(frozen=True)
class Frozen:
    """The launch spec ``start`` froze for its pending run (``SPEC_NAME``)."""

    resolution: kinds.Resolution
    request: dict[str, Any]
    caller_cwd: str | None  # None: the session's work/ (cwd: session-dir)
    cwd: str  # the resolved cwd
    plugin_dirs: tuple[str, ...]  # resolved
    dirs: launch.RunDirs  # resolved read_dirs, write_dirs and profile write paths
    profile_name: str | None  # None: a file profile
    profile_content: dict[str, Any]  # a file profile's: parsed from its snapshot
    profile_snapshot: bytes | None
    config_values: dict[str, Any]

    def profile(self) -> permission_profiles.Profile:
        return permission_profiles.Profile(
            name=self.profile_name, content=self.profile_content, snapshot=self.profile_snapshot
        )


def resolution_json(resolution: kinds.Resolution) -> dict[str, Any]:
    """Every field of ``resolution``, ``env.set`` values included (unlike ``resolved_json``,
    which is in ``state.db``)."""
    return {
        "kind": resolution.kind,
        "builtin": resolution.builtin,
        "fallback": resolution.fallback,
        "repo": resolution.repo,
        "profile": resolution.profile.model_dump(mode="json"),
        "origins": dict(resolution.origins),
        "warnings": list(resolution.warnings),
        "env_allow": list(resolution.env_allow),
        "env_allowlist_only": (
            None if resolution.env_allowlist_only is None else list(resolution.env_allowlist_only)
        ),
        "permission_profile_file": resolution.permission_profile_file,
        "permission_profile_label": resolution.permission_profile_label,
    }


def resolution_of(data: Mapping[str, Any]) -> kinds.Resolution:
    _keys(data, RESOLUTION_KEYS)
    allowlist_only = data["env_allowlist_only"]
    origins = {_text(k): _text(v) for k, v in _mapping(data["origins"]).items()}
    if not set(origins.values()) <= ORIGINS:
        raise ValueError("unknown origin")
    return kinds.Resolution(
        kind=_text(data["kind"]),
        builtin=_bool(data["builtin"]),
        fallback=_bool(data["fallback"]),
        repo=_optional_text(data["repo"]),
        profile=config.Kind.model_validate(data["profile"]),
        origins=typing.cast(dict[str, kinds.Origin], origins),
        warnings=tuple(_texts(data["warnings"])),
        env_allow=tuple(_texts(data["env_allow"])),
        env_allowlist_only=None if allowlist_only is None else tuple(_texts(allowlist_only)),
        permission_profile_file=_optional_text(data["permission_profile_file"]),
        permission_profile_label=_optional_text(data["permission_profile_label"]),
    )


def spec_json(
    cfg: Config,
    req: launch.Request,
    resolved: launch.Resolved,
    v: launch.Validated,
    dirs: launch.RunDirs,
) -> dict[str, Any]:
    profile = v.profile
    resolution = resolved.resolution
    if resolution.permission_profile_file is not None:
        # start read the file relative to its own cwd; the adopting exec runs elsewhere.
        absolute = os.path.realpath(resolution.permission_profile_file)
        resolution = dataclasses.replace(resolution, permission_profile_file=absolute)
    return {
        "version": SPEC_VERSION,
        "resolution": resolution_json(resolution),
        "request": {
            "mode": req.mode,
            "skill": req.skill,
            "repo": req.repo,
            "labels": list(req.labels),
            "flags": dict(req.flags),
            "actor": req.actor,
            "decision_ids": list(req.decision_ids),
            "hooks": list(req.hooks),
            "hook_timeouts": list(req.hook_timeouts),
            "has_prompt": req.prompt is not None,
            "input_files": [item.name for item in req.inputs],
            "conversation": dataclasses.asdict(req.conversation),
        },
        "caller_cwd": resolved.caller_cwd,
        "cwd": v.cwd,
        "plugin_dirs": list(v.plugin_dirs),
        "read_dirs": list(dirs.read_dirs),
        "write_dirs": list(dirs.write_dirs),
        "profile_writes": list(dirs.profile_writes),
        # A file profile is its snapshot only (its content is parsed from it).
        "profile": {
            "name": profile.name,
            "content": None if profile.is_file else profile.content,
            "snapshot": None if profile.snapshot is None else profile.snapshot.decode("utf-8"),
        },
        "config": {key: getattr(cfg, key) for key in FROZEN_CONFIG},
    }


def spec_bytes(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"


def parse_spec(raw: bytes, where: str) -> Frozen:
    """The spec read back, strictly: no unknown or missing key, absolute paths, known
    origins, a file profile's content parsed from its snapshot. Anything unexpected is
    ``session_dir_unsafe``: the file is in the private directory and only ``start`` writes
    it."""
    try:
        data = _mapping(json.loads(raw.decode("utf-8")))
        if data.get("version") != SPEC_VERSION:
            raise ValueError("unknown launch spec version")
        _keys(data, SPEC_KEYS)
        profile = _mapping(data["profile"])
        _keys(profile, PROFILE_KEYS)
        snapshot = _optional_text(profile["snapshot"])
        name = _optional_text(profile["name"])
        if snapshot is None:
            if name is None:
                raise ValueError("a named profile without a name")
            content = dict(_mapping(profile["content"]))
        else:
            if name is not None or profile["content"] is not None:
                raise ValueError("a file profile is its snapshot only")
            content = dict(_mapping(json.loads(snapshot)))
        caller_cwd = _optional_text(data["caller_cwd"])
        if caller_cwd is not None:
            _absolute(caller_cwd)
        dirs = launch.RunDirs(
            read_dirs=_absolutes(data["read_dirs"]),
            write_dirs=_absolutes(data["write_dirs"]),
            profile_writes=_absolutes(data["profile_writes"]),
        )
        return Frozen(
            resolution=resolution_of(_mapping(data["resolution"])),
            request=dict(_mapping(data["request"])),
            caller_cwd=caller_cwd,
            cwd=_absolute(data["cwd"]),
            plugin_dirs=_absolutes(data["plugin_dirs"]),
            dirs=dirs,
            profile_name=name,
            profile_content=content,
            profile_snapshot=None if snapshot is None else snapshot.encode("utf-8"),
            config_values=dict(_mapping(data["config"])),
        )
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError) as exc:
        # Pydantic's ValidationError is a ValueError. The message names the file only.
        raise sessiondir.SessionDirUnsafe(
            f"the launch spec {where} cannot be read ({type(exc).__name__})"
        ) from None


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return value


def _keys(data: Mapping[str, Any], expected: frozenset[str]) -> None:
    if set(data) != expected:
        raise ValueError("unexpected or missing keys")


def _text(value: Any) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise TypeError("expected a string")
    return value


def _absolute(value: Any) -> str:
    text = _text(value)
    if not os.path.isabs(text):
        raise ValueError("expected an absolute path")
    return text


def _absolutes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError("expected a list")
    return tuple(_absolute(item) for item in value)


def _optional_text(value: Any) -> str | None:
    return None if value is None else _text(value)


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise TypeError("expected a list")
    return [_text(item) for item in value]


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected a boolean")
    return value


# start


class Starting(launch.Recording):
    """``start``'s step-5 effects: a real launch's, except that the cwd is not recorded on
    the run (a pending run's cwd is null until adoption records it; ``status``)."""

    def record_cwd(self, cwd: str) -> None:
        pass


def failed(error: LaunchValidationError, alloc: launch.Allocated) -> LaunchFailedError:
    """``start``'s error envelope for a run that ended ``not-started``."""
    return LaunchFailedError(
        error.message,
        detail=error.detail,
        csession_id=alloc.csession_id,
        run=alloc.run,
        end_reason=error.end_reason,
        hint=error.hint or launch.HINTS.get(error.end_reason),
    )


class RunTaken(FailedError):
    """``start`` could not make its run pending: the run ended, or got another wrapper,
    while ``start`` froze it (exit 12, ``failed``)."""


def start(
    st: store.Store,
    args: argparse.Namespace,
    cfg: Config,
    source: config.Source,
    process: launch.Process,
) -> dict[str, Any]:
    """``start`` of a new session: launch steps 2 to 5 with ``start`` as the run's wrapper,
    then the run is frozen (staged prompt and inputs, profile snapshot, launch spec) and
    made pending. Returns the ``start --json`` object (schemas/cli/start.json). A step-5
    failure (or an unsafe session directory while freezing) ends the run ``not-started`` and
    raises ``LaunchFailedError``; a run taken over while freezing raises ``RunTaken``; any
    other error ends it ``launch_interrupted`` and is re-raised. The launch spec of a run
    that does not become pending is deleted.

    ``start --resume`` (``resume.prepare`` and ``resume.allocate``) builds the request from
    the previous run (carry-over) and allocates run n of the session (the refusal checks in
    that transaction) or, with ``--fork``, the fork's run 1; the rest is the same."""
    from . import resume

    pending = launch.Pending(
        expires_after=cfg.lost_grace,
        config_hash=config.config_hash(cfg),
        config_path=source.path,
        config_default=source.default,
    )
    forked_from = None
    if args.resume is not None:
        planned = resume.prepare(st, args, cfg, process, any_mode=True)
        req, resolved = planned.req, planned.resolved
        check_outcome_file(args, req, resolved, process)
        alloc = resume.allocate(st, planned, process, pending=pending, cfg=cfg)
        forked_from = planned.source.csession_id if planned.fork else None
    else:
        req = dryrun.request(args, cfg)  # start never checks the run's TTY (any mode)
        resolved = launch.resolve_kind(req, cfg, process)
        check_outcome_file(args, req, resolved, process)
        alloc = launch.allocate(st, req, resolved, process, pending=pending)
    resolution = resolved.resolution
    v: launch.Validated | None = None
    try:
        try:
            v = launch.validate(st, cfg, req, resolved, alloc, process, effects=Starting)
            warnings, expires_at = freeze(st, cfg, req, resolved, alloc, v, process, pending)
        except RunTaken:
            discard_spec(alloc, v)
            raise
        except LaunchValidationError as error:
            discard_spec(alloc, v)
            launch.end_launch(st, alloc, v, None, process, error)
            raise failed(error, alloc) from None
        except BaseException as exc:
            discard_spec(alloc, v)
            launch.end_launch(st, alloc, v, None, process, launch.interrupted(exc), exc=exc)
            raise
    finally:
        if v is not None:
            v.close_tmpdir_pin()
            v.dirs.close()
        # The run is pending (no wrapper) or ended: start delivers its events itself
        # (a launch-phase end included).
        toolhooks.deliver_best_effort(
            st,
            cfg,
            alloc.csession_id,
            gateway=process.gateway,
            environ=process.environ,
            state_root=process.state_root,
        )
    assert v is not None
    return {
        "csession_id": alloc.csession_id,
        "csession_dir": alloc.session_dir,
        "argv": [
            process.claude_session,
            *("exec", "--session-id", alloc.csession_id, "--run", str(alloc.run)),
        ],
        "run": alloc.run,
        "forked_from": forked_from,
        "expires_at": expires_at,
        "required_env_names": list(dryrun.required_env_names(resolution, v.session_env)),
        "env_inherit": resolution.profile.env.inherit,
        "warnings": list(dict.fromkeys([*resolution.warnings, *warnings])),
    }


def check_outcome_file(
    args: argparse.Namespace,
    req: launch.Request,
    resolved: launch.Resolved,
    process: launch.Process,
) -> None:
    """Launch step 2 of ``start``: the ``--outcome-file`` check of a launch
    (``launch.check_outcome_file``, exit 2); from here an error envelope's outcome file is
    checked as the run's is."""
    launch.check_outcome_file(req, resolved, process)
    args.outcome_file_writable = launch.step2_outcome_writable(resolved, process)


def freeze(
    st: store.Store,
    cfg: Config,
    req: launch.Request,
    resolved: launch.Resolved,
    alloc: launch.Allocated,
    v: launch.Validated,
    process: launch.Process,
    pending: launch.Pending,
) -> tuple[list[str], str]:
    """Snapshot a file profile (``validate`` staged the prompt and inputs), write the
    launch spec, record
    the profile label, remove the private tmpdir the self-check created (the adopting
    ``exec`` creates it again) and record the run's warnings; then, last, make the run
    pending (``Store.release_pending``: ``expires_at`` counts from now). Returns the run's
    warnings and ``expires_at``; raises ``RunTaken`` when the run is no longer ``start``'s."""
    run, sid = alloc.run, alloc.csession_id
    snapshot = None
    if v.profile.is_file:
        snapshot = v.dirs.write_private_run_file(
            run, launch.PROFILE_SNAPSHOT, v.profile.snapshot_bytes()
        )
    home = os.path.realpath(process.environ.get("HOME", "/"))
    dirs = launch.run_dirs(v.read_dirs, v.write_dirs, v.expanded, v.cwd, home)
    spec = spec_json(cfg, req, resolved, v, dirs)
    v.dirs.write_private_run_file(run, SPEC_NAME, spec_bytes(spec))
    st.update_run(
        sid, run, permission_profile=v.profile.label, permission_profile_snapshot=snapshot
    )
    warnings = list(v.warnings)
    if v.tmpdir is not None and v.tmpdir_pin is not None:
        try:
            tmpdir.remove(sid, pin=v.tmpdir_pin)
        except tmpdir.TmpdirLeftover as exc:
            warnings.append(f"{exc}; {exc.hint}")
        except OSError as exc:
            path = tmpdir.run_path(sid)
            warnings.append(f"could not remove the private temporary directory {path}: {exc}")
    st.add_run_warnings(sid, run, warnings)
    assert alloc.wrapper_start is not None
    expires_at = st.release_pending(
        sid,
        run,
        wrapper_pid=os.getpid(),
        wrapper_start=alloc.wrapper_start,
        pending_for=pending.expires_after,
    )
    if expires_at is None:
        raise RunTaken(
            f"run {run} of {sid} was ended or taken over while start froze it",
            detail="the run did not become pending",
        )
    return warnings, expires_at


def discard_spec(alloc: launch.Allocated, v: launch.Validated | None) -> None:
    """Delete the run's launch spec, which may hold credentials, through the pinned private
    directory (best effort; nothing to do before ``validate`` returned: it is written
    later)."""
    if v is None:
        return
    try:
        v.dirs.remove_private_run_file(alloc.run, SPEC_NAME)
    except (OSError, ValueError, LaunchValidationError):
        pass


def discard_spec_at(state_root: str, csession_id: str, run: int) -> None:
    """``discard_spec`` without open session directories (an adopting ``exec`` that does
    not own the run, or failed before it read the spec); best effort, creates nothing."""
    try:
        sessiondir.remove_private_run_file(state_root, csession_id, run, SPEC_NAME)
    except (OSError, ValueError, LaunchValidationError):
        pass


# Adoption


@dataclass
class Adoption:
    """The adopting ``exec``'s inputs, rebuilt from the pending run: the config (the stored
    path, reloaded, with the frozen values on top), the request and resolution, the spec,
    the stored git directory and the warnings so far."""

    cfg: Config
    req: launch.Request
    resolved: launch.Resolved
    frozen: Frozen
    git_dir: str | None
    profile_label: str | None  # runs.permission_profile, as start recorded it
    warnings: list[str]
    config_hash_changed: bool


class FrozenChecks:
    """The comparisons of an adoption against what ``start`` stored, shared by the adopting
    ``exec`` (``Adopting``) and its dry run (``AdoptionDryRun``)."""

    frozen: Frozen
    git_dir: str | None
    profile_label: str | None

    def compare_cwd(self, cwd: str) -> None:
        if cwd != self.frozen.cwd:
            raise LaunchValidationError(
                "the cwd resolves to another directory than at start",
                detail=f"{self.frozen.cwd} -> {cwd}",
                end_reason="unsafe_cwd",
            )

    def compare_git_dir(self, git_dirs: gitinfo.GitDirs | None) -> None:
        if self.git_dir is None:  # start always stores it: refuse rather than guess
            raise LaunchValidationError(
                "the session has no git directory recorded at start",
                end_reason="git_dir_changed",
            )
        gitinfo.check_unchanged(self.git_dir, git_dirs)

    def frozen_profile(self, resolution: kinds.Resolution) -> permission_profiles.Profile:
        """The profile ``start`` read, validated again for the run's isolation; its label
        (a file profile's: from its snapshot) must be the one ``start`` recorded on the run
        (``session_dir_unsafe`` otherwise)."""
        profile = self.frozen.profile()
        if profile.label != self.profile_label:
            raise sessiondir.SessionDirUnsafe(
                "the launch spec's permission profile is not the one start recorded",
                detail=f"{self.profile_label} -> {profile.label}",
            )
        permission_profiles.validate(
            profile.content, isolated=resolution.isolated(), source="frozen permission profile"
        )
        return profile

    def compare_dirs(self, dirs: Callable[[], launch.RunDirs]) -> None:
        current, frozen = dirs(), self.frozen.dirs
        for key, label in (
            ("read_dirs", "a read_dirs entry"),
            ("write_dirs", "a write_dirs entry"),
            ("profile_writes", "a permission profile write path"),
        ):
            if getattr(current, key) != getattr(frozen, key):
                raise LaunchValidationError(
                    f"{label} resolves to another directory than at start",
                    end_reason="invalid_permission_profile",
                )

    def compare_plugin_dirs(self, plugin_dirs: Sequence[str]) -> None:
        if tuple(plugin_dirs) != self.frozen.plugin_dirs:
            raise LaunchValidationError(
                "a plugin directory resolves to another directory than at start",
                end_reason="unsafe_plugin_dir",
            )


class Adopting(launch.Recording, FrozenChecks):
    """The adopting ``exec``'s step-5 effects: a real launch's, after comparing with what
    ``start`` stored (the git directory is checked, never recorded again)."""

    def __init__(
        self,
        st: store.Store,
        alloc: launch.Allocated,
        dirs: SessionDirs,
        adoption: Adoption,
    ) -> None:
        super().__init__(st, alloc, dirs)
        self.frozen = adoption.frozen
        self.git_dir = adoption.git_dir
        self.profile_label = adoption.profile_label

    def record_cwd(self, cwd: str) -> None:
        self.compare_cwd(cwd)
        super().record_cwd(cwd)

    def record_git_dir(self, git_dirs: gitinfo.GitDirs | None) -> None:
        self.compare_git_dir(git_dirs)

    def select_profile(
        self, cfg: Config, resolution: kinds.Resolution, writable: Callable[[str], bool]
    ) -> permission_profiles.Profile:
        return self.frozen_profile(resolution)

    def record_plugin_dirs(self, plugin_dirs: Sequence[str]) -> None:
        self.compare_plugin_dirs(plugin_dirs)

    def record_dirs(self, dirs: Callable[[], launch.RunDirs]) -> None:
        self.compare_dirs(dirs)


class AdoptionDryRun(dryrun.DryRunEffects, FrozenChecks):
    """The adoption dry run's effects: nothing recorded or created, the same comparisons."""

    def __init__(self, adoption: Adoption) -> None:
        super().__init__()
        self.frozen = adoption.frozen
        self.git_dir = adoption.git_dir
        self.profile_label = adoption.profile_label

    def record_cwd(self, cwd: str) -> None:
        self.compare_cwd(cwd)

    def git_cwd(self, cwd: str) -> str:
        return cwd  # start created work/: it exists

    def record_git_dir(self, git_dirs: gitinfo.GitDirs | None) -> None:
        super().record_git_dir(git_dirs)
        self.compare_git_dir(git_dirs)

    def select_profile(
        self, cfg: Config, resolution: kinds.Resolution, writable: Callable[[str], bool]
    ) -> permission_profiles.Profile:
        return self.frozen_profile(resolution)

    def record_plugin_dirs(self, plugin_dirs: Sequence[str]) -> None:
        self.compare_plugin_dirs(plugin_dirs)

    def record_dirs(self, dirs: Callable[[], launch.RunDirs]) -> None:
        self.compare_dirs(dirs)


def lookup(st: store.Store, given: str) -> tuple[str, dict[str, Any]]:
    """The session ``--session-id`` names and its current run (exit 10 or 21 otherwise)."""
    sid = ids.lookup_session_id(given, st.session_id_candidates)
    record = st.current_run(sid)
    if record is None or record["session_forgotten_at"] is not None:
        raise NotFoundError(f"unknown session: {given}")
    return sid, record


def check_run(sid: str, record: Mapping[str, Any], run: int | None) -> None:
    """The run ``--run`` names must be the current run (``record``); another one exits 20
    ``expired``: it ended and a later run took its place, or it never existed. Without
    ``--run`` (an argv printed before it existed) the current run is adopted."""
    if run is not None and record["run_no"] != run:
        raise RefusedError(
            f"cannot adopt run {run} of {sid}: expired (the current run is {record['run_no']})",
            reason="expired",
        )


def refusal(record: Mapping[str, Any], now_past: bool) -> str | None:
    """The refusal an adoption of ``record`` (the current run) would get, without changing
    anything: ``expired`` (final, or pending past its deadline) or ``already_adopted``."""
    if record["ended_at"] is not None:
        return "expired"
    if record["wrapper_pid"] is not None:
        return "already_adopted"
    return "expired" if now_past else None


def _isatty(stream: Any) -> bool:
    try:
        return stream is not None and bool(stream.isatty())
    except (OSError, ValueError):
        return False


def has_tty() -> bool:
    return _isatty(sys.stdin) and _isatty(sys.stdout)


def load_adoption(
    record: Mapping[str, Any], process: launch.Process, *, dry_run: bool = False
) -> Adoption:
    """The config reloaded from the stored path (an invalid one: ``config_invalid``), the
    launch spec read back (``session_dir_unsafe`` when it is unusable; the adopting
    ``exec`` deletes it once read, a dry run only reads it) and the request and resolution
    rebuilt from it. A changed config hash is a warning."""
    sid, run = record["csession_id"], record["run_no"]
    invocation = _invocation(record)
    path = record["config_path"]
    if not isinstance(path, str) or not os.path.isabs(path):
        raise LaunchValidationError(
            "the pending run has no stored config path", end_reason="config_invalid"
        )
    source = config.Source(path=path, default=invocation.get("config_default") is True)
    try:
        current = source.load(process.environ)
    except UsageError as exc:
        raise LaunchValidationError(
            exc.message, detail=exc.detail, end_reason="config_invalid"
        ) from None
    warnings: list[str] = []
    changed = config.config_hash(current) != record["config_hash"]
    if changed:
        warnings.append(
            f"the config changed since start ({path}); the run uses the launch settings start"
            " froze and only the hooks, timing values, claude_bin and min_claude_version of"
            " the current config"
        )
    if dry_run:
        # Read only, through the same descriptor walk as the adopting exec; creates nothing.
        sessiondir.check_dirs(process.state_root, sid)
        raw = sessiondir.read_private_run_file(
            process.state_root, sid, run, SPEC_NAME, SPEC_MAX_BYTES
        )
    else:
        with SessionDirs.open(process.state_root, sid) as dirs:
            try:
                raw = dirs.read_private_run_file(run, SPEC_NAME, SPEC_MAX_BYTES)
            finally:
                # It may hold credentials; the run is this process's now and is never
                # adopted again, whatever happens next.
                dirs.remove_private_run_file(run, SPEC_NAME)
    where = os.path.join(sessiondir.private_dir(process.state_root, sid), "runs", str(run))
    frozen = parse_spec(raw, os.path.join(where, SPEC_NAME))
    cfg = current.model_copy(update=_adoption_config(frozen, current))
    req = _request(frozen, record)
    resolved = launch.Resolved(resolution=frozen.resolution, caller_cwd=frozen.caller_cwd)
    return Adoption(
        cfg=cfg,
        req=req,
        resolved=resolved,
        frozen=frozen,
        git_dir=record["session_git_dir"],
        profile_label=record["permission_profile"],
        warnings=warnings,
        config_hash_changed=changed,
    )


def _invocation(record: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(record["invocation_json"] or "{}")
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise sessiondir.SessionDirUnsafe("the pending run's invocation record is unreadable")
    return value


def _frozen_config(frozen: Frozen) -> dict[str, Any]:
    """The config values ``start`` froze, checked as the config checks them."""
    values = frozen.config_values
    names_ok = all(
        isinstance(values.get(key), list)
        and all(isinstance(n, str) and ids.is_env_name(n) for n in values[key])
        for key in ("claude_auth_env", "secret_env")
    )
    caps_ok = all(
        type(values.get(key)) is int and 1 <= values[key] <= MAX_INTEGER
        for key in ("summary_max_bytes", "artifact_max_bytes", "input_max_bytes")
    )
    if set(values) != set(FROZEN_CONFIG) or not names_ok or not caps_ok:
        raise sessiondir.SessionDirUnsafe("the launch spec's config values are unusable")
    return {key: values[key] for key in FROZEN_CONFIG}


def _adoption_config(frozen: Frozen, current: Config) -> dict[str, Any]:
    """The frozen config values, where the current config may only tighten: the
    ``secret_env`` names are those of both, plus the current ``claude_auth_env`` names
    ``start`` did not freeze (stripped and indexed, never passed); only the frozen
    ``claude_auth_env`` names are passed."""
    values = _frozen_config(frozen)
    auth = values["claude_auth_env"]
    values["secret_env"] = list(
        dict.fromkeys(
            [
                *values["secret_env"],
                *current.secret_env,
                *(name for name in current.claude_auth_env if name not in auth),
            ]
        )
    )
    return values


def _request(frozen: Frozen, record: Mapping[str, Any]) -> launch.Request:
    """The request ``start`` read, rebuilt and checked again: no unknown key, the flag rules
    and identifier grammars of ``start``'s argv (``cli.check_flag_rules``), and the mode, kind
    and prompt presence the run row records. The prompt and inputs are staged (moved into
    place by step 7), so only their presence and names are known here."""
    data = frozen.request
    try:
        _keys(data, REQUEST_KEYS)
        _check_request(frozen, data, record)
        flags = dict(_mapping(data["flags"]))
        inputs = tuple(InputFile(name=name, data=b"") for name in _texts(data["input_files"]))
        return launch.Request(
            kind=frozen.resolution.kind,
            mode=launch.parse_mode(_text(data["mode"])),
            skill=_optional_text(data["skill"]),
            prompt=b"" if _bool(data["has_prompt"]) else None,
            inputs=inputs,
            plugin_dirs=frozen.plugin_dirs,
            repo=_optional_text(data["repo"]),
            cwd=frozen.caller_cwd,
            labels=tuple(_texts(data["labels"])),
            flags=flags,
            actor=_text(data["actor"]),
            decision_ids=tuple(_texts(data["decision_ids"])),
            hooks=tuple(_texts(data["hooks"])),
            hook_timeouts=tuple(_texts(data["hook_timeouts"])),
            outcome_file=record["outcome_file"],
            conversation=_conversation(data["conversation"], record),
            # From the run row, never the spec file: the store decides by the row too.
            allow_tainted=taint.AllowTainted.from_invocation(_invocation(record)),
        )
    except (KeyError, TypeError, ValueError, AttributeError, UsageError) as exc:
        raise sessiondir.SessionDirUnsafe(
            f"the launch spec's request cannot be read ({type(exc).__name__})"
        ) from None


def _conversation(value: Any, record: Mapping[str, Any]) -> launch.Conversation:
    """The conversation ``start`` froze: a new session's (run 1, not forked: none), a
    resume's (run n > 1: ``--resume`` or a fresh ``--session-id``) or a fork's (its run 1,
    or a later run of a fork that never captured a Claude id, which repeats the fork); the
    ids must be canonical UUIDs (``settings_builder`` checks the combination)."""
    data = _mapping(value)
    _keys(data, CONVERSATION_KEYS)
    talk = launch.Conversation(
        resume=_bool(data["resume"]),
        resume_from=_optional_text(data["resume_from"]),
        fork=_bool(data["fork"]),
        session_id=_optional_text(data["session_id"]),
    )
    for claude_id in (talk.resume_from, talk.session_id):
        if claude_id is not None and str(uuid.UUID(claude_id)) != claude_id:
            raise ValueError("not a canonical UUID")
    forked = record["session_forked_from"] is not None
    if talk.resume != (record["run_no"] > 1 or forked) or talk.fork and not forked:
        raise ValueError("the launch spec's conversation is not the run's")
    return talk


def _check_request(frozen: Frozen, data: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    """``start``'s argv rebuilt from the spec's request through the flag rules ``start`` ran
    (``UsageError``), with the values ``argparse`` guarantees (strings and lists of
    strings) checked first; the mode, kind and prompt presence must be the run row's."""
    from . import cli  # the flag rules; not at import time (cli imports this module late)

    mode = _text(data["mode"])
    if mode not in MODES or mode != record["mode"]:
        raise ValueError("the launch spec's mode is not the run's")
    if frozen.resolution.kind != record["run_kind"]:
        raise ValueError("the launch spec's kind is not the run's")
    # The taint checks decide by the row's reads_third_party: a spec that launches the run
    # with another profile would run a trusted profile where the store saw an isolated one.
    if frozen.resolution.profile.reads_third_party != bool(record["reads_third_party"]):
        raise ValueError("the launch spec's reads_third_party is not the run's")
    # The audit records of the run (resume_tainted, sandbox_lowered) name the request's actor
    # and decision ids: they must be those start recorded on the run row.
    invocation = _invocation(record)
    if _text(data["actor"]) != invocation.get("actor"):
        raise ValueError("the launch spec's actor is not the run's")
    if _texts(data["decision_ids"]) != invocation.get("decision_ids"):
        raise ValueError("the launch spec's decision ids are not the run's")
    if _bool(data["has_prompt"]) != bool(record["has_prompt"]):
        raise ValueError("the launch spec's prompt presence is not the run's")
    flags = dict(_mapping(data["flags"]))
    if set(flags) != set(kinds.FLAG_KEYS):
        raise ValueError("unexpected or missing flags")
    args = cli.build_parser().parse_args(["start", "--json"])
    args.kind = frozen.resolution.kind
    args.mode = mode
    args.skill = _optional_text(data["skill"])
    args.repo = _optional_text(data["repo"])
    args.label = _texts(data["labels"])
    args.actor = _text(data["actor"])
    args.decision_id = _texts(data["decision_ids"])
    args.hook = _texts(data["hooks"])
    args.hook_timeout = _texts(data["hook_timeouts"])
    args.input_file = _texts(data["input_files"])
    args.prompt_file = SPEC_NAME if data["has_prompt"] else None  # any non-stdin name
    if _mapping(data["conversation"]).get("resume") is True:
        args.resume = record["csession_id"]  # a resumed or forked run: no new-session rules
    for key, value in flags.items():
        setattr(args, key, value)
    cli.check_flag_rules(args)


def log_config_warnings(
    process: launch.Process, alloc: launch.Allocated, warnings: Sequence[str]
) -> None:
    log = launch.event_log(process)
    for warning in warnings:
        launch.log_best_effort(
            log,
            "config_warning",
            level="warn",
            csession_id=alloc.csession_id,
            run=alloc.run,
            message=warning,
        )


@dataclass
class Adopted:
    """What ``adopt`` leaves: the adopted run (``alloc``) and, when it may launch, its
    ``Adoption``; None when the run already ended ``not-started`` (its outcome is in the
    store). ``cfg``: the config its ``post-state-change`` events are delivered with (None:
    the stored config could not be loaded; ``reconcile`` delivers them later)."""

    alloc: launch.Allocated
    adoption: Adoption | None
    cfg: Config | None


def check_adoption_outcome_file(path: str, writable: Callable[[str], bool] | None) -> None:
    """An ``--outcome-file`` given at adoption overrides the stored one and is checked again
    before the compare-and-set (interface.md, Launch order), against the run's stored
    session-writable paths (``outcomefile.run_writable``): a rejected path exits 2
    (``OutcomeFileRejected``) and the run stays pending. When those paths cannot be
    determined (an isolated run without a stored set) it is rejected too (fail closed)."""
    if writable is None:
        raise OutcomeFileRejected(
            "--outcome-file cannot be checked: the run's session-writable paths are not known"
        )
    pathrules.check_outcome_file(path, writable)


def stored_config(record: Mapping[str, Any], process: launch.Process) -> Config | None:
    """The config at the path ``start`` stored, for delivering the events of a run that ends
    without being adopted; None when it cannot be loaded (best effort)."""
    path = record["config_path"]
    if not isinstance(path, str) or not os.path.isabs(path):
        return None
    try:
        default = _invocation(record).get("config_default") is True
        return config.Source(path=path, default=default).load(process.environ)
    except Exception:
        return None


def ended_unadopted(
    st: store.Store, process: launch.Process, record: Mapping[str, Any], run: int
) -> None:
    """After a compare-and-set that ended the pending run ``never_adopted`` (expired, or its
    fork source tainted): its launch spec deleted, its outcome file written and its events
    delivered (best effort)."""
    sid = record["csession_id"]
    discard_spec_at(process.state_root, sid, run)
    outcomefile.write_run(st, sid, run)
    cfg = stored_config(record, process)
    if cfg is not None:
        toolhooks.deliver_best_effort(
            st,
            cfg,
            sid,
            gateway=process.gateway,
            environ=process.environ,
            state_root=process.state_root,
        )


def adopt(
    st: store.Store,
    args: argparse.Namespace,
    process: launch.Process,
    signals: ExecSignals,
) -> Adopted:
    """``exec --session-id <id>`` up to the adopted run's launch (see the module docs): the
    ``--outcome-file`` re-check, the compare-and-set (``signals`` deferred from just before
    it: a signal received from then on acts once ``launch_adopted`` enters the launch phase,
    when the run is this process's), then the config, launch spec and TTY. Refusals raise
    ``RefusedError`` (exit 20) or ``NotFoundError`` (exit 10) and change nothing (an expired
    pending run is finalized ``never_adopted`` first, its launch spec deleted, its outcome
    file written and its events delivered). A failure after the compare-and-set ends the run
    ``not-started`` here (``Adopted.adoption`` None)."""
    sid, record = lookup(st, args.session_id)
    check_run(sid, record, args.run)
    writable = outcomefile.run_writable(record, process.state_root)
    # From here an error envelope's outcome file is checked as the run's is.
    args.outcome_file_writable = writable
    outcome_file = None
    if args.outcome_file is not None:
        outcome_file = outcomefile.absolute(args.outcome_file)
        check_adoption_outcome_file(outcome_file, writable)
    signals.defer()  # the compare-and-set and what follows: a signal acts once the run is ours
    # An unreadable start token is stored as "": the liveness check then decides by the PID
    # alone and keeps the run live while that PID exists (it never finalizes a live run).
    result = st.adopt(
        sid,
        wrapper_pid=os.getpid(),
        wrapper_start=store.start_time(os.getpid()),
        outcome_file=outcome_file,
        hook_interpreter=process.interpreter,
        run_no=args.run,
    )
    if result.outcome == "not_found":
        raise NotFoundError(f"unknown session: {args.session_id}")
    if result.outcome != "adopted":
        if result.expired_now and result.run_no is not None:
            ended_unadopted(st, process, record, result.run_no)
        if result.outcome == "tainted":
            # A fork's source became tainted after start allocated the fork (interface.md,
            # Taint): the run was finalized never_adopted.
            assert result.run_no is not None
            source = record["session_forked_from"]
            raise RefusedError(
                f"cannot adopt {sid}: its fork source {source} is tainted and run"
                f" {result.run_no} is not third-party-reading; the run ended not-started",
                reason="tainted",
            )
        reason = "already_adopted" if result.outcome == "already_adopted" else "expired"
        raise RefusedError(f"cannot adopt {sid}: {reason.replace('_', ' ')}", reason=reason)
    assert result.run_no is not None
    run = result.run_no
    # A wrapper that dies from here until the spawn leaves an adopted run in the launch phase
    # (no child recorded): the liveness check ends it launch_interrupted and deletes the
    # launch spec if it is still there.
    launch.log_best_effort(
        launch.event_log(process), "adopt", level="info", csession_id=sid, run=run
    )
    # Any run of a fork may merge into an inputs/<run>/ the fork copy made.
    forked_from = record["session_forked_from"]
    forked = forked_from is not None
    # A run that forks the source's conversation (a fork's run 1, or a later run of a fork
    # that never captured a Claude id: it repeats the fork) re-checks the source's taint
    # right before step 7, as a plain resume --fork does.
    forks_conversation = forked and record["session_claude_session_id"] is None
    alloc = launch.Allocated(
        csession_id=sid,
        run=run,
        session_dir=sessiondir.session_dir(process.state_root, sid),
        private_dir=sessiondir.private_dir(process.state_root, sid),
        warnings=(),
        cause="adopt",
        # A resumed run drains its previous run's outbox at step 7; a fork's run 1 places
        # its inputs beside the ones start copied from the source.
        previous_run=run - 1 if run > 1 else None,
        forked=forked,
        taint_source=forked_from if forks_conversation else None,
    )
    adoption: Adoption | None = None
    try:
        adopted = st.current_run(sid)
        if adopted is None or adopted["run_no"] != run:
            raise InternalError(f"run {run} of {sid} is no longer the current run")
        adoption = load_adoption(adopted, process)
        log_config_warnings(process, alloc, adoption.warnings)
        st.add_run_warnings(sid, run, adoption.warnings)
        if adoption.req.mode == "interactive" and not has_tty():
            raise LaunchValidationError(
                "an interactive run needs a terminal on stdin and stdout",
                end_reason="no_tty",
                hint=NO_TTY_HINT,
            )
    except LaunchValidationError as error:
        discard_spec_at(process.state_root, sid, run)
        launch.end_launch(st, alloc, None, None, process, error)
        return Adopted(alloc, None, _delivery_config(adoption, record, process))
    except Exception as exc:
        discard_spec_at(process.state_root, sid, run)
        launch.end_launch(st, alloc, None, None, process, launch.interrupted(exc), exc=exc)
        return Adopted(alloc, None, _delivery_config(adoption, record, process))
    except BaseException as exc:
        discard_spec_at(process.state_root, sid, run)
        launch.end_launch(st, alloc, None, None, process, launch.interrupted(exc), exc=exc)
        raise
    return Adopted(alloc, adoption, adoption.cfg)


def _delivery_config(
    adoption: Adoption | None, record: Mapping[str, Any], process: launch.Process
) -> Config | None:
    return adoption.cfg if adoption is not None else stored_config(record, process)


def launch_adopted(
    st: store.Store,
    adopted: Adopted,
    process: launch.Process,
    *,
    supervise: Callable[..., Any],
    signals: ExecSignals,
) -> Any:
    """Steps 5 to 7 of the adopted run and its supervision (``launch.run`` with ``Adopting``
    effects; the bound is the frozen resolution's ``timeout`` for the run's mode). None when
    the run already ended (``Adopted.adoption`` None) or ended in the launch phase."""
    adoption = adopted.adoption
    if adoption is None:
        return None

    def effects(s: store.Store, a: launch.Allocated, dirs: SessionDirs) -> launch.Effects:
        assert adoption is not None
        return Adopting(s, a, dirs, adoption)

    return launch.run(
        st,
        adoption.cfg,
        adoption.req,
        adoption.resolved,
        adopted.alloc,
        process,
        supervise=supervise,
        effects=effects,
        staged=True,
        signals=signals,
    )


# The adoption dry run


def adoption_plan(
    st: store.Store,
    args: argparse.Namespace,
    process: launch.Process,
    *,
    prerequisites: dryrun.Prerequisites | None = None,
) -> tuple[dict[str, Any], str | None]:
    """``exec --session-id <id> --dry-run``: the plan of adopting the pending run now,
    without adopting it or finalizing anything (interface.md, Dry run): ``would_refuse``
    (``already_adopted``, ``expired``), ``config_hash_changed``, and the step-5 checks with
    the adoption's comparisons (``would_fail``). An invalid stored config exits 2 as an
    invalid config, as a dry run of a new session does. When the stored run cannot be read
    back (no usable launch spec: a failed ``start``, an adopted or expired run), there is
    no plan to print (interface.md, Dry run): a refused adoption exits 20 with the refusal
    envelope and any other one 12 (``failed``)."""
    sid, record = lookup(st, args.session_id)
    check_run(sid, record, args.run)
    would_refuse = refusal(record, st.past_deadline(record["expires_at"]))
    if would_refuse is None and st.fork_source_taints(sid):
        would_refuse = "tainted"
    try:
        adoption = load_adoption(record, process, dry_run=True)
    except LaunchValidationError as error:
        if would_refuse is not None:
            # A run that never got a launch spec (its start failed) or lost it: no plan can
            # be built, and adopting it would be refused first.
            raise RefusedError(
                f"cannot adopt {sid}: {would_refuse.replace('_', ' ')}", reason=would_refuse
            ) from None
        if error.end_reason == "config_invalid":
            raise UsageError("invalid config", detail=error.detail or error.message) from None
        raise
    cfg, req, resolved, resolution = (
        adoption.cfg,
        adoption.req,
        adoption.resolved,
        adoption.resolved.resolution,
    )
    alloc = launch.Allocated(
        csession_id=sid,
        run=record["run_no"],
        session_dir=sessiondir.session_dir(process.state_root, sid),
        private_dir=sessiondir.private_dir(process.state_root, sid),
        warnings=(),
    )
    effects = AdoptionDryRun(adoption)
    found = dryrun.run_checks(cfg, req, resolved, alloc, process, effects=effects)
    if req.mode == "interactive" and found.would_fail is None and not has_tty():
        found.would_fail = "no_tty"
        found.message = f"an interactive run needs a terminal on stdin and stdout; {NO_TTY_HINT}"
        found.warnings.append(f"a launch would fail (no_tty): {found.message}")
        found.generated = None
    stored_git = adoption.git_dir
    git = found.git
    if git is None and stored_git is not None:
        git = dryrun.GitDir(
            None if stored_git == gitinfo.NO_GIT_DIR else gitinfo.GitDirs(common_dir=stored_git)
        )
    writable = dryrun.unchecked_writable(
        cfg,
        process,
        resolution,
        probe_cwd=resolved.caller_cwd,
        caller_cwd=resolved.caller_cwd,
        recorded=found.profile or adoption.frozen.profile(),
        git=git,
        alloc=alloc,
    )
    sandbox = dryrun.plan_sandbox(process, resolution, found, writable, prerequisites)
    plan = dryrun.plan_json(
        cfg,
        req,
        resolution,
        found,
        sandbox=sandbox,
        preview=dryrun.plan_preview(cfg, resolution, process, found, writable),
        cwd=found.checked.cwd if found.checked is not None else adoption.frozen.cwd,
        allow_tainted=req.allow_tainted is not None,
        csession_id=sid,
        run=record["run_no"],
    )
    plan["would_refuse"] = would_refuse
    plan["config_hash_changed"] = adoption.config_hash_changed
    plan["warnings"] = list(dict.fromkeys([*adoption.warnings, *plan["warnings"]]))
    return plan, found.message
