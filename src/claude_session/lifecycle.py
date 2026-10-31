"""``start`` and the adopting ``exec --session-id`` (interface.md, Launch order, start and
Flags at start and adoption; design.md, Launch, adoption and pending runs).

``start`` performs launch steps 2 to 5 for a new session (``launch.read_request`` through
``dryrun.request``, ``resolve_kind``, ``allocate`` with a ``Pending`` deadline,
``validate``), then freezes the run: the prompt and input copies are staged
(``SessionDirs.stage``), a file permission profile is snapshotted, and the launch spec
(``SPEC_NAME``, below) is written to the run's private directory. The run is pending: no
wrapper, ``expires_at`` = now + ``lost_grace``. A step-5 failure ends the run
``not-started`` (``launch.end_launch``) and ``start`` exits 12 with ``launch_failed``.

The launch spec holds what the adopting ``exec`` must not take from anywhere else: the
fully resolved kind profile with its origins (``env.set`` values included, which
``resolved_json`` leaves out), the resolved cwd and plugin directories, the permission
profile (content, or a file profile's snapshot), the config values a session uses beyond
those the adopting ``exec`` reads from the config again (``claude_auth_env``,
``secret_env`` and the caps), and the per-invocation flags. It is private (never in the
session directory) and 0600.

The adopting ``exec`` (``adopt``) runs the compare-and-set (``Store.adopt``; refusals exit 20
``already_adopted`` or ``expired``, an expired pending run is finalized ``never_adopted``
first; a forgotten session exits 10), then, owning the run, reloads the config from the
stored path (``config_invalid`` when it is invalid; a changed hash warns), reads the spec,
checks the TTY of an interactive run (``no_tty``) and reruns every step-5 check against its
own environment through ``launch.run`` with ``Adopting`` effects: the cwd and plugin
directories must resolve to the realpaths ``start`` stored (``unsafe_cwd``,
``unsafe_plugin_dir``), the git directory must be the stored one (``git_dir_changed``), and
the permission profile is the frozen one. Step 7 then moves the staged copies into place.

TODO(#29): ``start --resume`` (a pending run n, a fork) and its dry run.
TODO(#31): the ``pre-launch`` hooks of the adopting ``exec`` (launch step 6).
TODO(#17): interactive adoption (signal handlers before the compare-and-set, the TTY handed
to ``claude``); until then an interactive pending run with a TTY is refused before the
compare-and-set (it stays pending).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
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
    permission_profiles,
    sessiondir,
    store,
    tmpdir,
)
from .config import Config
from .defaults import MAX_INTEGER
from .errors import (
    InternalError,
    LaunchFailedError,
    LaunchValidationError,
    NotFoundError,
    RefusedError,
    UsageError,
)
from .sessiondir import InputFile, SessionDirs

SPEC_NAME = "launch-spec.json"
SPEC_VERSION = 1
SPEC_MAX_BYTES = 8 << 20
# Config values frozen in the spec (the adopting exec reads the rest from the config again).
FROZEN_CONFIG = (
    "claude_auth_env",
    "secret_env",
    "summary_max_bytes",
    "artifact_max_bytes",
    "input_max_bytes",
)
NO_TTY_HINT = "place the start argv in a terminal (stdin and stdout must be TTYs)"


# The launch spec


@dataclass(frozen=True)
class Frozen:
    """The launch spec ``start`` froze for its pending run (``SPEC_NAME``)."""

    resolution: kinds.Resolution
    request: dict[str, Any]
    caller_cwd: str | None  # None: the session's work/ (cwd: session-dir)
    cwd: str  # the resolved cwd
    plugin_dirs: tuple[str, ...]  # resolved
    profile_name: str | None  # None: a file profile
    profile_content: dict[str, Any]
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
    }


def resolution_of(data: Mapping[str, Any]) -> kinds.Resolution:
    allowlist_only = data["env_allowlist_only"]
    return kinds.Resolution(
        kind=_text(data["kind"]),
        builtin=_bool(data["builtin"]),
        fallback=_bool(data["fallback"]),
        repo=_optional_text(data["repo"]),
        profile=config.Kind.model_validate(data["profile"]),
        origins={_text(k): v for k, v in _mapping(data["origins"]).items()},
        warnings=tuple(_texts(data["warnings"])),
        env_allow=tuple(_texts(data["env_allow"])),
        env_allowlist_only=None if allowlist_only is None else tuple(_texts(allowlist_only)),
        permission_profile_file=_optional_text(data["permission_profile_file"]),
    )


def spec_json(
    cfg: Config, req: launch.Request, resolved: launch.Resolved, v: launch.Validated
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
        },
        "caller_cwd": resolved.caller_cwd,
        "cwd": v.cwd,
        "plugin_dirs": list(v.plugin_dirs),
        "profile": {
            "name": profile.name,
            "content": profile.content,
            "snapshot": None if profile.snapshot is None else profile.snapshot.decode("utf-8"),
        },
        "config": {key: getattr(cfg, key) for key in FROZEN_CONFIG},
    }


def spec_bytes(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"


def parse_spec(raw: bytes, where: str) -> Frozen:
    """The spec read back. Anything unexpected is ``session_dir_unsafe``: the file is in the
    private directory and only ``start`` writes it."""
    try:
        data = json.loads(raw.decode("utf-8"))
        if data.get("version") != SPEC_VERSION:
            raise ValueError("unknown launch spec version")
        profile = _mapping(data["profile"])
        snapshot = _optional_text(profile["snapshot"])
        caller_cwd = _optional_text(data["caller_cwd"])
        return Frozen(
            resolution=resolution_of(_mapping(data["resolution"])),
            request=dict(_mapping(data["request"])),
            caller_cwd=caller_cwd,
            cwd=_text(data["cwd"]),
            plugin_dirs=tuple(_texts(data["plugin_dirs"])),
            profile_name=_optional_text(profile["name"]),
            profile_content=dict(_mapping(profile["content"])),
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


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a string")
    return value


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


def start(
    st: store.Store,
    args: argparse.Namespace,
    cfg: Config,
    source: config.Source,
    process: launch.Process,
) -> dict[str, Any]:
    """``start`` of a new session: launch steps 2 to 5, then the run is frozen (staged
    prompt and inputs, profile snapshot, launch spec). Returns the ``start --json`` object
    (schemas/cli/start.json). A step-5 failure (or an unsafe session directory while
    freezing) ends the run ``not-started`` and raises ``LaunchFailedError``; any other error
    ends it ``launch_interrupted`` and is re-raised."""
    req = dryrun.request(args, cfg)  # start never checks the run's TTY (any mode)
    resolved = launch.resolve_kind(req, cfg, process)
    resolution = resolved.resolution
    pending = launch.Pending(
        expires_after=cfg.lost_grace,
        config_hash=config.config_hash(cfg),
        config_path=source.path,
        config_default=source.default,
    )
    alloc = launch.allocate(st, req, resolved, process, pending=pending)
    v: launch.Validated | None = None
    try:
        try:
            v = launch.validate(st, cfg, req, resolved, alloc, process, effects=Starting)
            warnings = freeze(st, cfg, req, resolved, alloc, v)
        except LaunchValidationError as error:
            launch.end_launch(st, alloc, v, None, process, error)
            raise failed(error, alloc) from None
        except BaseException as exc:
            launch.end_launch(st, alloc, v, None, process, launch.interrupted(exc), exc=exc)
            raise
    finally:
        if v is not None:
            v.dirs.close()
    assert v is not None
    record = st.run_record(alloc.csession_id, alloc.run)
    return {
        "csession_id": alloc.csession_id,
        "csession_dir": alloc.session_dir,
        "argv": [process.claude_session, "exec", "--session-id", alloc.csession_id],
        "run": alloc.run,
        "forked_from": None,
        "expires_at": record["expires_at"],
        "required_env_names": list(dryrun.required_env_names(resolution, v.session_env)),
        "env_inherit": resolution.profile.env.inherit,
        "warnings": list(dict.fromkeys([*resolution.warnings, *warnings])),
    }


def freeze(
    st: store.Store,
    cfg: Config,
    req: launch.Request,
    resolved: launch.Resolved,
    alloc: launch.Allocated,
    v: launch.Validated,
) -> list[str]:
    """Stage the prompt and inputs, snapshot a file profile, write the launch spec, record
    the profile label and the run's warnings, and remove the private tmpdir the self-check
    created (the adopting ``exec`` creates it again). Returns the run's warnings."""
    run, sid = alloc.run, alloc.csession_id
    v.dirs.stage(run, req.prompt, req.inputs)
    snapshot = None
    if v.profile.is_file:
        snapshot = v.dirs.write_private_run_file(
            run, launch.PROFILE_SNAPSHOT, v.profile.snapshot_bytes()
        )
    v.dirs.write_private_run_file(run, SPEC_NAME, spec_bytes(spec_json(cfg, req, resolved, v)))
    st.update_run(
        sid, run, permission_profile=v.profile.label, permission_profile_snapshot=snapshot
    )
    warnings = list(v.warnings)
    if v.tmpdir is not None:
        try:
            tmpdir.remove(sid)
        except tmpdir.TmpdirLeftover as exc:
            warnings.append(f"{exc}; {exc.hint}")
        except OSError:
            pass  # an empty directory of ours; the adopting exec recreates it either way
    st.add_run_warnings(sid, run, warnings)
    return warnings


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
    warnings: list[str]
    config_hash_changed: bool


class FrozenChecks:
    """The comparisons of an adoption against what ``start`` stored, shared by the adopting
    ``exec`` (``Adopting``) and its dry run (``AdoptionDryRun``)."""

    frozen: Frozen
    git_dir: str | None

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
        """The profile ``start`` read, validated again for the run's isolation; a file
        profile's snapshot must still have its label."""
        profile = self.frozen.profile()
        permission_profiles.validate(
            profile.content, isolated=resolution.isolated(), source="frozen permission profile"
        )
        return profile

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
        frozen: Frozen,
        git_dir: str | None,
    ) -> None:
        super().__init__(st, alloc, dirs)
        self.frozen = frozen
        self.git_dir = git_dir

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


class AdoptionDryRun(dryrun.DryRunEffects, FrozenChecks):
    """The adoption dry run's effects: nothing recorded or created, the same comparisons."""

    def __init__(self, frozen: Frozen, git_dir: str | None) -> None:
        super().__init__()
        self.frozen = frozen
        self.git_dir = git_dir

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


def lookup(st: store.Store, given: str) -> tuple[str, dict[str, Any]]:
    """The session ``--session-id`` names and its current run (exit 10 or 21 otherwise)."""
    sid = ids.lookup_session_id(given, st.session_id_candidates)
    record = st.current_run(sid)
    if record is None or record["session_forgotten_at"] is not None:
        raise NotFoundError(f"unknown session: {given}")
    return sid, record


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
    launch spec read back (``session_dir_unsafe`` when it is unusable) and the request and
    resolution rebuilt from it. A changed config hash is a warning."""
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
        sessiondir.check_dirs(process.state_root, sid)
        raw = _read_spec_readonly(process.state_root, sid, run)
    else:
        with SessionDirs.open(process.state_root, sid) as dirs:
            raw = dirs.read_private_run_file(run, SPEC_NAME, SPEC_MAX_BYTES)
    where = os.path.join(sessiondir.private_dir(process.state_root, sid), "runs", str(run))
    frozen = parse_spec(raw, os.path.join(where, SPEC_NAME))
    cfg = current.model_copy(update=_frozen_config(frozen))
    req = _request(frozen, record)
    resolved = launch.Resolved(resolution=frozen.resolution, caller_cwd=frozen.caller_cwd)
    return Adoption(
        cfg=cfg,
        req=req,
        resolved=resolved,
        frozen=frozen,
        git_dir=record["session_git_dir"],
        warnings=warnings,
        config_hash_changed=changed,
    )


def _read_spec_readonly(state_root: str, sid: str, run: int) -> bytes:
    """The launch spec for a dry run, which opens no ``SessionDirs`` (it would create
    directories): the same checks on a no-follow descriptor."""
    path = os.path.join(sessiondir.private_dir(state_root, sid), "runs", str(run), SPEC_NAME)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise sessiondir.SessionDirUnsafe(f"cannot read the launch spec {path}: {exc}") from None
    try:
        info = os.fstat(fd)
        if not (
            os.path.realpath(path) == path
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and (info.st_mode & 0o170000) == 0o100000
            and info.st_size <= SPEC_MAX_BYTES
        ):
            raise sessiondir.SessionDirUnsafe(f"unsafe launch spec {path}")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
            if sum(map(len, chunks)) > SPEC_MAX_BYTES:
                raise sessiondir.SessionDirUnsafe(f"unsafe launch spec {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


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


def _request(frozen: Frozen, record: Mapping[str, Any]) -> launch.Request:
    """The request ``start`` read, rebuilt. The prompt and inputs are staged (moved into
    place by step 7), so only their presence and names are known here."""
    data = frozen.request
    try:
        flags = dict(_mapping(data["flags"]))
        inputs = tuple(InputFile(name=name, data=b"") for name in _texts(data["input_files"]))
        return launch.Request(
            kind=frozen.resolution.kind,
            mode=_text(data["mode"]),
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
        )
    except (KeyError, TypeError) as exc:
        raise sessiondir.SessionDirUnsafe(
            f"the launch spec's request cannot be read ({type(exc).__name__})"
        ) from None


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


def adopt(
    st: store.Store,
    args: argparse.Namespace,
    process: launch.Process,
    *,
    supervise: Callable[..., Any],
) -> tuple[dict[str, Any], int]:
    """``exec --session-id <id>``: the compare-and-set, then the adopted run's launch (see
    the module docs). Returns the outcome object and the exit code. Refusals raise
    ``RefusedError`` (exit 20) or ``NotFoundError`` (exit 10) and change nothing (an expired
    pending run is finalized ``never_adopted`` first)."""
    sid, record = lookup(st, args.session_id)
    outcome_file = None if args.outcome_file is None else os.path.abspath(args.outcome_file)
    # TODO(#21): an isolated run's --outcome-file under a session-writable path exits 2 here,
    # before the compare-and-set, and the run stays pending.
    if record["mode"] == "interactive" and record["wrapper_pid"] is None and has_tty():
        # TODO(#17): interactive supervision; the run stays pending until it exists.
        raise InternalError(
            "adopting an interactive run is not implemented yet", detail="not_implemented"
        )
    # TODO(#17): install the signal handlers (interface.md, Signals) here, before the CAS.
    result = st.adopt(
        sid,
        wrapper_pid=os.getpid(),
        wrapper_start=store.start_time(os.getpid()),
        outcome_file=outcome_file,
        hook_interpreter=process.interpreter,
    )
    if result.outcome == "not_found":
        raise NotFoundError(f"unknown session: {args.session_id}")
    if result.outcome != "adopted":
        reason = "already_adopted" if result.outcome == "already_adopted" else "expired"
        raise RefusedError(f"cannot adopt {sid}: {reason.replace('_', ' ')}", reason=reason)
    assert result.run_no is not None
    run = result.run_no
    alloc = launch.Allocated(
        csession_id=sid,
        run=run,
        session_dir=sessiondir.session_dir(process.state_root, sid),
        private_dir=sessiondir.private_dir(process.state_root, sid),
        warnings=(),
        cause="adopt",
    )
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
        launch.end_launch(st, alloc, None, None, process, error)
        return _outcome(st, sid, run)
    except Exception as exc:
        launch.end_launch(st, alloc, None, None, process, launch.interrupted(exc), exc=exc)
        return _outcome(st, sid, run)
    except BaseException as exc:
        launch.end_launch(st, alloc, None, None, process, launch.interrupted(exc), exc=exc)
        raise

    def effects(s: store.Store, a: launch.Allocated, dirs: SessionDirs) -> launch.Effects:
        return Adopting(s, a, dirs, adoption.frozen, adoption.git_dir)

    ended = launch.run(
        st,
        adoption.cfg,
        adoption.req,
        adoption.resolved,
        alloc,
        process,
        supervise=supervise,
        effects=effects,
        staged=True,
    )
    outcome, code = _outcome(st, sid, run)
    return outcome, code if ended is None else ended.exit_code


def _outcome(st: store.Store, sid: str, run: int) -> tuple[dict[str, Any], int]:
    from . import supervisor

    outcome = supervisor.outcome_object(st, sid, run)
    return outcome, int(outcome["exit_code"])


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
    back (no launch spec: a failed ``start``), a refused adoption exits 20 with the refusal
    envelope and any other one 12 (``failed``): there is no plan to print."""
    sid, record = lookup(st, args.session_id)
    would_refuse = refusal(record, st.past_deadline(record["expires_at"]))
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
    effects = AdoptionDryRun(adoption.frozen, adoption.git_dir)
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
        csession_id=sid,
        run=record["run_no"],
    )
    plan["would_refuse"] = would_refuse
    plan["config_hash_changed"] = adoption.config_hash_changed
    plan["warnings"] = list(dict.fromkeys([*adoption.warnings, *plan["warnings"]]))
    return plan, found.message
