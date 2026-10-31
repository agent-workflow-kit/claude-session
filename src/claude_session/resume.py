"""Resume and fork: the previous run, the carry-over table, the conversation, the refusals and
the allocation (interface.md, Resume and fork carry-over, Launch order and resume; design.md,
Resume and fork).

``resume <id>``, ``start --resume <id>`` and their ``--fork`` variants share launch steps 5 to
7 with a new session (``launch``); this module builds what differs:

- the **source** (``load_source``): the session's current run, read in one transaction after
  the inline liveness check has expired a pending run past its deadline (``liveness``);
- the **request** (``prepare``, launch step 2): the kind and mode default to the previous
  run's; the explicit flags the previous run stored (``spec_json``) are carried by the
  carry-over table (``carry``: nothing but repo, cwd and mode when the kind changes; no
  ``--timeout`` or ``--max-turns`` when the mode changes; ``--reset`` drops a setting; a
  profile file is carried as its snapshot with its recorded label) and resolved by ``kinds``
  with origin ``previous_run``, so a carried flag never beats a ``--reads-third-party``
  implication; the prompt and inputs of a previous run that ended ``not-started`` are carried
  unless given now (``sessiondir.read_undelivered``);
- the **cwd**: a resume always runs in the previous run's resolved cwd (a different
  ``--cwd`` exits 2); a fork in ``--cwd`` when given, else in the source's cwd, except a
  session ``work/`` directory, which fails the launch ``fork_cwd_unsupported``;
- the **conversation**: ``--resume <latest Claude id>``, or for a fork ``--resume <id>
  --fork-session --session-id <new>``; with no captured Claude id, a fresh conversation
  (``--session-id <new>``) and a warning;
- the **allocation** (launch step 4): a resume is run n + 1 with the refusal checks in the
  same transaction (``Store.allocate_resume``: ``live``, ``tainted``); a fork is a new
  session (``forked_from``, the source's taint), whose step 5 copies the source's
  ``inputs/`` (``SessionDirs.copy_inputs``).

TODO(#33): the inline liveness check proper (wrapper-lost runs finalized and salvaged before
the refusal checks). Until it exists, a resume of a run that is not final is refused ``live``
(also when its wrapper is gone); only a pending run past its deadline is finalized here.
TODO(#17): interactive resume (a TTY on stdin and stdout; ``claude`` on the terminal).
TODO(#30): ``--allow-tainted`` (human-only, audited ``resume_tainted``); until then a tainted
session is refused for a run that is not third-party-reading whatever the flags.
TODO(#31): ``--override-escalation`` (human-only; only after a run that ended ``escalated``;
it overrides that run's escalating ``pre-launch`` hooks).
TODO(#20): deliver the ``post-state-change`` events of a run this module finalizes
(``toolhooks.deliver``).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import dryrun, gitinfo, ids, kinds, launch, sessiondir, store
from .config import Config
from .errors import InternalError, LaunchValidationError, NotFoundError, UsageError

# `--reset <setting>` -> the stored key it drops (cli.RESETTABLE has the same names).
RESETS = {
    "model": "model",
    "plugin-dir": "plugin_dir",
    "env-allow": "env_allow",
    "env-allowlist-only": "env_allowlist_only",
    "setting-sources": "setting_sources",
    "sandbox": "sandbox",
    "timeout": "timeout",
    "max-turns": "max_turns",
    "permission-profile": "permission_profile",
    "auto-exit": "auto_exit",
}
# Dropped when the mode changes (interface.md, Resume and fork carry-over).
MODE_BOUND = ("timeout", "max_turns")
FRESH_WARNING = (
    "the session has no captured Claude session id (it never reached SessionStart);"
    " the run starts a fresh conversation"
)
FRESH_FORK_WARNING = (
    "the source session has no captured Claude session id (it never reached SessionStart);"
    " the fork starts a fresh conversation"
)


@dataclass(frozen=True)
class Source:
    """The session a resume continues, or a fork's source, and its current (last) run."""

    csession_id: str
    run: int
    launch_kind: str
    run_kind: str
    mode: str
    cwd: str | None  # the latest resolved cwd of any run (None: never resolved)
    repo: str | None
    spec: Mapping[str, Any]
    permission_profile: str | None  # the run's label (file:<prefix> for a file profile)
    permission_profile_snapshot: str | None
    final: bool  # ended, or a pending run past its deadline (the liveness check ends it)
    not_started: bool  # ended (or would end) in the launch phase: undelivered prompt, inputs
    has_prompt: bool
    input_files: tuple[str, ...]
    claude_session_id: str | None
    tainted: bool
    git_dir: str | None
    labels: tuple[str, ...]
    session_dir: str
    outcome: str | None

    @property
    def work_dir(self) -> str:
        return os.path.realpath(os.path.join(self.session_dir, sessiondir.WORK))


def lookup(st: store.Store, given: str) -> str:
    """The session id ``given`` names (exit 10 unknown or forgotten, 21 ambiguous)."""
    return ids.lookup_session_id(given, st.session_id_candidates)


def liveness(st: store.Store, process: launch.Process, csession_id: str) -> None:
    """The part of the inline liveness check this branch has: a current run still pending at
    or past its ``expires_at`` is finalized ``not-started`` (``never_adopted``, cause
    ``liveness``) and its launch spec deleted. TODO(#33): the wrapper-lost rules.
    TODO(#21): the outcome file of the run finalized here. TODO(#20): its delivery."""
    from . import lifecycle

    record = st.current_run(csession_id)
    if record is None or record["ended_at"] is not None or record["wrapper_pid"] is not None:
        return
    if st.expire_pending(csession_id, record["run_no"], cause="liveness"):
        lifecycle.discard_spec_at(process.state_root, csession_id, record["run_no"])


def load_source(st: store.Store, csession_id: str, state_root: str) -> Source:
    """The session and its current run (exit 10 when unknown or forgotten). Stored values the
    carry-over relies on are checked here; an unusable one is ``InternalError``."""
    record = st.resume_source(csession_id)
    if record is None or record["session_forgotten_at"] is not None:
        raise NotFoundError(f"unknown session: {csession_id}")
    pending_expired = (
        record["ended_at"] is None
        and record["wrapper_pid"] is None
        and st.past_deadline(record["expires_at"])
    )
    final = record["ended_at"] is not None or pending_expired
    not_started = (record["ended_at"] is not None and record["phase"] == "launch") or (
        pending_expired
    )
    try:
        spec = json.loads(record["spec_json"] or "{}")
        invocation = json.loads(record["invocation_json"] or "{}")
        if not isinstance(spec, dict) or not isinstance(invocation, dict):
            raise ValueError("not an object")
        input_files = invocation.get("input_files", [])
        if not isinstance(input_files, list) or not all(isinstance(n, str) for n in input_files):
            raise ValueError("input_files")
    except ValueError as exc:
        raise InternalError(
            f"run {record['run_no']} of {csession_id} has an unreadable stored record"
        ) from exc
    mode = record["mode"] if record["mode"] in ("interactive", "headless") else "interactive"
    return Source(
        csession_id=csession_id,
        run=record["run_no"],
        launch_kind=record["session_launch_kind"],
        run_kind=record["run_kind"],
        mode=mode,
        cwd=record["last_cwd"],
        repo=record["repo"],
        spec=spec,
        permission_profile=record["permission_profile"],
        permission_profile_snapshot=record["permission_profile_snapshot"],
        final=final,
        not_started=not_started,
        has_prompt=bool(record["has_prompt"]),
        input_files=tuple(input_files),
        claude_session_id=record["session_claude_session_id"],
        tainted=bool(record["session_tainted"]),
        git_dir=record["session_git_dir"],
        labels=tuple(record["labels"]),
        session_dir=sessiondir.session_dir(state_root, csession_id),
        outcome=record["outcome"],
    )


# The carry-over table


@dataclass(frozen=True)
class Carried:
    """What a run carries from the previous run's explicit flags: ``flags`` (``FLAG_KEYS``,
    for ``kinds.Previous``), the plugin directories (absolute) and a carried snapshot's
    recorded label."""

    flags: dict[str, Any]
    plugin_dirs: tuple[str, ...]
    permission_profile_label: str | None


def carry(source: Source, *, kind: str, mode: str, resets: Sequence[str]) -> Carried:
    """The carry-over table (interface.md, Resume and fork carry-over) applied to the
    previous run's stored explicit flags: a kind change carries none of them (repo, cwd and
    mode are carried elsewhere); a mode change drops ``--timeout`` and ``--max-turns``;
    ``--reset`` drops the setting it names. A profile file is carried as the previous run's
    snapshot with the label that run recorded (never the original file); without a snapshot
    it cannot be carried (``kinds.resolve`` then exits 2). A stored value of the wrong type
    exits 2 (``kinds`` checks the flag values themselves)."""
    if kind != source.run_kind:
        return Carried({}, (), None)
    spec = source.spec
    flags = {key: spec[key] for key in kinds.FLAG_KEYS if spec.get(key) is not None}
    plugins = spec.get("plugin_dir") or []
    if not isinstance(plugins, list) or not all(
        isinstance(entry, str) and os.path.isabs(entry) and "\x00" not in entry for entry in plugins
    ):
        raise UsageError(
            "the previous run's stored --plugin-dir list is unusable",
            detail="use --reset plugin-dir or give --plugin-dir",
        )
    plugin_dirs = tuple(plugins)
    if mode != source.mode:
        for key in MODE_BOUND:
            flags.pop(key, None)
    for name in resets:
        key = RESETS[name]
        if key == "plugin_dir":
            plugin_dirs = ()
        else:
            flags.pop(key, None)
    _check_stored(source, flags, plugin_dirs)
    label = None
    profile = flags.get("permission_profile")
    if isinstance(profile, str) and ids.is_permission_profile_file(profile):
        if source.permission_profile_snapshot is not None:
            flags["permission_profile"] = source.permission_profile_snapshot
            label = source.permission_profile
        else:
            # The previous run carried a snapshot it never re-snapshotted (it ended before
            # step 7): its spec holds that snapshot path and label. Otherwise None, and
            # kinds.resolve refuses a file it would have to re-read.
            stored = spec.get(launch.CARRIED_LABEL)
            label = stored if isinstance(stored, str) else None
    return Carried(flags, plugin_dirs, label)


def _check_stored(source: Source, flags: Mapping[str, Any], plugin_dirs: Sequence[str]) -> None:
    """The carried flags pass the flag rules they passed when given (``cli``: durations,
    ``--max-turns`` range, ``--model`` not option-like, variable names, setting sources),
    so a stored value is never trusted more than a typed one. Exit 2 otherwise."""
    from . import cli  # the flag rules; cli imports this module late

    args = cli.build_parser().parse_args(["resume", source.csession_id])
    for key, value in flags.items():
        setattr(args, key, value)
    args.plugin_dir = list(plugin_dirs) or None
    try:
        cli.check_flag_rules(args)
    except (UsageError, TypeError, AttributeError) as exc:
        detail = exc.message if isinstance(exc, UsageError) else type(exc).__name__
        raise UsageError(
            "the previous run's stored flags are invalid",
            detail=f"{detail}; use --reset or give the flag again",
        ) from None


# Step 2


@dataclass(frozen=True)
class Planned:
    """A resume's or fork's step 2: the source, the request and resolution, and whether it
    forks (and from which not-started run it carries the inputs instead of copying them)."""

    source: Source
    req: launch.Request
    resolved: launch.Resolved
    fork: bool
    warnings: tuple[str, ...]

    def fork_of(self) -> launch.Fork | None:
        if not self.fork:
            return None
        exclude = self.source.run if self.source.not_started else None
        return launch.Fork(source=self.source.csession_id, exclude_run=exclude)


def conversation(source: Source, *, fork: bool) -> tuple[launch.Conversation, list[str]]:
    """The Claude Code conversation the run continues: the session's latest Claude id
    (``--resume``; a fork adds ``--fork-session`` and a new ``--session-id``), or with none
    captured a fresh conversation (a new ``--session-id``) and a warning."""
    claude_id = source.claude_session_id
    if claude_id is None:
        fresh = launch.Conversation(resume=True, session_id=str(uuid.uuid4()))
        return fresh, [FRESH_FORK_WARNING if fork else FRESH_WARNING]
    if fork:
        return (
            launch.Conversation(
                resume=True, resume_from=claude_id, fork=True, session_id=str(uuid.uuid4())
            ),
            [],
        )
    return launch.Conversation(resume=True, resume_from=claude_id), []


def _read_request(
    args: argparse.Namespace, cfg: Config, mode: str, *, any_mode: bool
) -> launch.Request:
    """``launch.read_request`` in ``mode``: ``start --resume`` and dry runs read any mode
    (``dryrun.request``, with the interactive TTY checks of a launch); a plain interactive
    ``resume`` needs a TTY on stdin and stdout (exit 2), then is not implemented yet."""
    with_mode = argparse.Namespace(**{**vars(args), "mode": mode})
    if any_mode:
        return dryrun.request(with_mode, cfg)
    if mode == "interactive":
        dryrun.check_interactive_tty(with_mode)
        # TODO(#17): interactive resume.
        raise InternalError(
            "interactive resume is not implemented yet; pass --mode headless",
            detail="not_implemented",
        )
    return launch.read_request(with_mode, cfg)


def _undelivered(
    source: Source, args: argparse.Namespace, process: launch.Process, cap: int
) -> tuple[bytes | None, list[sessiondir.InputFile]] | None:
    """The prompt and inputs a previous run that ended ``not-started`` never delivered,
    for the parts not given now (None: nothing to carry). Unreadable ones exit 2: the
    caller can replace them."""
    want_prompt = source.has_prompt and args.prompt_file is None
    want_inputs = list(source.input_files) if args.input_file is None else []
    if not source.not_started or not (want_prompt or want_inputs):
        return None
    try:
        return sessiondir.read_undelivered(
            process.state_root,
            source.csession_id,
            source.run,
            prompt=want_prompt,
            input_names=want_inputs,
            cap=cap,
        )
    except LaunchValidationError as exc:
        raise UsageError(
            f"cannot carry the undelivered prompt and inputs of run {source.run}",
            detail=f"{exc.message}; give --prompt-file and --input-file to replace them",
        ) from None


def _cwd(
    args: argparse.Namespace,
    source: Source,
    resolution: kinds.Resolution,
    process: launch.Process,
    *,
    fork: bool,
) -> tuple[str | None, bool]:
    """The run's caller-side cwd (None: the session's own ``work/``) and whether a fork must
    fail ``fork_cwd_unsupported``. A resume runs in the previous run's resolved cwd; a
    ``--cwd`` that resolves elsewhere exits 2. A fork runs in ``--cwd`` when given, else in
    the source's cwd. A session that never resolved a cwd (every run ended before step 5
    recorded one) resolves it as a new session would."""
    given = args.cwd
    if source.cwd is None or (fork and given is not None):
        if given == "session-dir":
            return None, False
        if given is not None or resolution.profile.cwd == "caller":
            return launch.resolve_caller_cwd(given, process), False
        return None, False
    previous = os.path.realpath(source.cwd)
    own_work = previous == source.work_dir
    if fork:
        return (None, True) if own_work else (previous, False)
    if given is not None:
        if given == "session-dir":
            wanted = source.work_dir
        else:
            wanted = os.path.realpath(os.path.join(process.caller_cwd, given))
        if wanted != previous:
            raise UsageError(
                "--cwd on a resume must be the previous run's cwd",
                detail=f"{previous} (use --fork to continue elsewhere)",
            )
    return (None, False) if own_work else (previous, False)


def _check_fork_inputs(
    source: Source, req: launch.Request, process: launch.Process, *, fork: bool
) -> None:
    """A fork's run-1 inputs go into ``inputs/1/``, which the copy of the source's
    ``inputs/`` may already hold: a name already there exits 2 (never replaced)."""
    if not fork or not req.inputs or (source.not_started and source.run == 1):
        return
    try:
        existing = set(sessiondir.input_names(process.state_root, source.csession_id, 1))
    except LaunchValidationError:
        return  # the copy refuses the source's entry (session_dir_unsafe) at step 5
    clash = sorted(existing & {item.name for item in req.inputs})
    if clash:
        raise UsageError(
            f"--input-file name {clash[0]!r} is already in the source's inputs/1/, which the"
            " fork copies",
            detail="rename the input",
        )


def prepare(
    st: store.Store,
    args: argparse.Namespace,
    cfg: Config,
    process: launch.Process,
    *,
    any_mode: bool = False,
    dry_run: bool = False,
) -> Planned:
    """Launch step 2 of ``resume`` and ``start --resume`` (``args.id`` or ``args.resume``):
    the session looked up (exit 10, 21), the liveness check (not in a dry run, which never
    finalizes), the source read, then the request with the carry-over and the resolution.
    Usage errors exit 2 before anything is allocated."""
    given = args.id if getattr(args, "id", None) is not None else args.resume
    fork = bool(getattr(args, "fork", False))
    sid = lookup(st, given)
    if not dry_run:
        liveness(st, process, sid)
    source = load_source(st, sid, process.state_root)
    kind = args.kind or source.run_kind
    mode = args.mode or source.mode
    carried = carry(source, kind=kind, mode=mode, resets=args.reset or ())
    req = _read_request(args, cfg, mode, any_mode=any_mode or dry_run)
    undelivered = _undelivered(source, args, process, cfg.input_max_bytes)
    if undelivered is not None:
        prompt, inputs = undelivered
        if prompt is not None:
            req = dataclasses.replace(req, prompt=prompt)
        if args.input_file is None:
            req = dataclasses.replace(req, inputs=tuple(inputs))
    talk, warnings = conversation(source, fork=fork)
    req = dataclasses.replace(
        req,
        kind=kind,
        plugin_dirs=req.plugin_dirs if args.plugin_dir is not None else carried.plugin_dirs,
        carried=carried.flags,
        conversation=talk,
    )
    _check_fork_inputs(source, req, process, fork=fork)
    previous = kinds.Previous(
        repo=source.repo,
        flags=carried.flags,
        permission_profile_label=carried.permission_profile_label,
    )
    resolution = kinds.resolve(
        cfg,
        kind,
        mode="headless" if mode == "headless" else "interactive",
        repo=req.repo,
        cwd=None,
        previous=previous,
        flags=req.flags,
    )
    if warnings:
        resolution = dataclasses.replace(resolution, warnings=(*resolution.warnings, *warnings))
    caller_cwd, unsupported = _cwd(args, source, resolution, process, fork=fork)
    resolved = launch.Resolved(
        resolution=resolution, caller_cwd=caller_cwd, fork_cwd_unsupported=unsupported
    )
    return Planned(source=source, req=req, resolved=resolved, fork=fork, warnings=tuple(warnings))


# Step 4


def allocate(
    st: store.Store,
    planned: Planned,
    process: launch.Process,
    *,
    pending: launch.Pending | None = None,
) -> launch.Allocated:
    """Launch step 4: a fork is a new session (``launch.allocate``; its git directory is
    checked against the source's when it runs in the source's cwd); a resume is run n + 1
    (``launch.allocate_resume``, refusals in the same transaction)."""
    source = planned.source
    if planned.fork:
        alloc = launch.allocate(
            st, planned.req, planned.resolved, process, pending=pending, fork=planned.fork_of()
        )
        if planned.resolved.caller_cwd is not None and planned.req.cwd is None:
            # The fork runs where the source did: a git directory that appeared or changed
            # there since the source's first launch is refused as on a resume.
            alloc = dataclasses.replace(
                alloc, check_git_dir=source.git_dir is not None, stored_git_dir=source.git_dir
            )
        return alloc
    return launch.allocate_resume(
        st,
        planned.req,
        planned.resolved,
        process,
        csession_id=source.csession_id,
        previous_run=source.run,
        git_dir=source.git_dir,
        pending=pending,
    )


# The dry run


def would_refuse(planned: Planned) -> str | None:
    """The refusal a launch would get, without changing anything (interface.md, resume):
    ``live`` (not with ``--fork``; a pending run past its deadline is not live: the
    liveness check would end it), then ``tainted``."""
    source = planned.source
    if not planned.fork and not source.final:
        return "live"
    if source.tainted and not planned.resolved.resolution.profile.reads_third_party:
        return "tainted"
    return None


class ResumeDryRun(dryrun.DryRunEffects):
    """A resume's dry-run effects: nothing recorded, the git directory compared with the
    stored one as a launch compares it."""

    def __init__(self, alloc: launch.Allocated) -> None:
        super().__init__()
        self.alloc = alloc

    def record_git_dir(self, git_dirs: gitinfo.GitDirs | None) -> None:
        super().record_git_dir(git_dirs)
        if self.alloc.check_git_dir:
            gitinfo.check_unchanged(self.alloc.stored_git_dir, git_dirs)


def plan(
    st: store.Store,
    args: argparse.Namespace,
    cfg: Config,
    process: launch.Process,
    *,
    prerequisites: dryrun.Prerequisites | None = None,
) -> tuple[dict[str, Any], str | None]:
    """``resume --dry-run`` and ``start --resume --dry-run``: the plan of the run a launch
    would allocate (interface.md, Dry run), built from the previous run with the carry-over;
    ``would_refuse`` (``live``, ``tainted``) as the refusal checks would decide now; nothing
    allocated or finalized. A resume's paths are the session's, a fork's the placeholder
    session's."""
    planned = prepare(st, args, cfg, process, dry_run=True)
    source, req, resolved = planned.source, planned.req, planned.resolved
    resolution = resolved.resolution
    if planned.fork:
        alloc = launch.placeholder_alloc(process, resolution)
        if resolved.caller_cwd is not None and req.cwd is None:
            alloc = dataclasses.replace(
                alloc, check_git_dir=source.git_dir is not None, stored_git_dir=source.git_dir
            )
    else:
        alloc = launch.Allocated(
            csession_id=source.csession_id,
            run=source.run + 1,
            session_dir=sessiondir.session_dir(process.state_root, source.csession_id),
            private_dir=sessiondir.private_dir(process.state_root, source.csession_id),
            warnings=resolution.warnings,
            check_git_dir=True,
            stored_git_dir=source.git_dir,
        )
    found = dryrun.run_checks(cfg, req, resolved, alloc, process, effects=ResumeDryRun(alloc))
    writable = dryrun.unchecked_writable(
        cfg,
        process,
        resolution,
        probe_cwd=resolved.caller_cwd,
        caller_cwd=resolved.caller_cwd,
        recorded=found.profile,
        git=found.git,
        alloc=None if planned.fork else alloc,
    )
    sandbox = dryrun.plan_sandbox(process, resolution, found, writable, prerequisites)
    cwd = (
        found.checked.cwd
        if found.checked is not None
        else os.path.realpath(resolved.caller_cwd or os.path.join(alloc.session_dir, "work"))
    )
    labels = sorted(
        {f"kind={resolution.kind}", *req.labels} if planned.fork else {*source.labels, *req.labels}
    )
    result = dryrun.plan_json(
        cfg,
        req,
        resolution,
        found,
        sandbox=sandbox,
        preview=dryrun.plan_preview(cfg, resolution, process, found, writable),
        cwd=cwd,
        allow_tainted=bool(getattr(args, "allow_tainted", False)),
        override_escalation=bool(getattr(args, "override_escalation", False)),
        csession_id=None if planned.fork else source.csession_id,
        run=alloc.run,
        fork=planned.fork,
        forked_from=source.csession_id if planned.fork else None,
        launch_kind=resolution.kind if planned.fork else source.launch_kind,
        labels=labels,
    )
    result["would_refuse"] = would_refuse(planned)
    return result, found.message
