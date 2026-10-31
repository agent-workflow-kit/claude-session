"""Headless supervision (design.md, Supervision; interface.md, exec and Outcome mapping).

``run_headless`` spawns ``claude`` from the main thread (``die_with_parent``: Linux sets
``PR_SET_PDEATHSIG(SIGTERM)`` in the child, and the kernel sends it when the *spawning thread*
exits, so it must be a thread that lives as long as the process) in its own process group,
with ``/dev/null`` as stdin, stdout redirected straight to the private ``result.json`` and
stderr to ``stderr.log``. The spawn is the last step of the launch phase: a spawn that
fails ends the run ``not-started`` (``launch_interrupted``, ``launch.end_launch``). It then
records the child's PID and start time and the ``session`` phase, waits for the child to
exit, then, in this order: drains the run's outbox (and ``claimed/``), maps the end
(``endmap.map_headless``), accepts or rejects an implicit done (``result.md``), finalizes the
run (the compare-and-set, which also requires the report status the end was mapped from; a
report that landed meanwhile is mapped again), deletes the secret index and removes the
private tmpdir.

If supervision hits an internal error after the spawn (any exception, ``KeyboardInterrupt``
included until #17 installs the signal handlers), it ignores SIGINT while it cleans up,
records ``pending_cause`` ``internal_error``, sends SIGTERM to the exact child it spawned
(SIGKILL after ``kill_grace``, also when the wait is interrupted), finalizes the run directly
(``killed``, ``internal_error``, unless a report exists; no drain, no result parsing, cost
null), deletes the secret index and removes the tmpdir on every path, and re-raises.

TODO(#17): the signal table (forwarded_term/hup/int, pending_cause before forwarding).
TODO(#19): the run's bound (``timeout``) and the ``session_start_timeout`` warning in the poll
loop.
TODO(#20): deliver ``post-state-change`` events of the run in the poll loop and at the end.
TODO(#21): write ``--outcome-file`` right after the finalization compare-and-set.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
from dataclasses import dataclass
from typing import Any

from . import endmap, fsutil, hookevent, launch, outbox, secretindex, store, tmpdir
from .config import Config
from .endmap import EXIT_CODES, HeadlessResult, Mapped
from .errors import InternalError
from .gateway import DEVNULL, Child

IMPLICIT_ARTIFACT = "result.md"
# Final drains of one run at most (each claims and checks for up to outbox.DRAIN_BUDGET).
FINAL_DRAINS = 4
# Mappings of one end at most when a report keeps landing between mapping and finalization.
FINALIZE_ATTEMPTS = 3


@dataclass(frozen=True)
class Ended:
    """How the run ended, for the outcome object: the exit code of ``exec`` and the run's
    warnings gathered by the supervisor."""

    exit_code: int
    warnings: tuple[str, ...]


def spawn(prepared: launch.Prepared, v: launch.Validated, process: launch.Process) -> Child:
    """Start headless ``claude`` (see the module docs). Closes the output descriptors."""
    try:
        return process.gateway.spawn(
            prepared.argv,
            executable=prepared.executable,
            env=v.session_env.env,
            cwd=v.cwd,
            stdin=DEVNULL,
            stdout=prepared.stdout_fd,
            stderr=prepared.stderr_fd,
            new_process_group=True,
            die_with_parent=True,
        )
    finally:
        os.close(prepared.stdout_fd)
        os.close(prepared.stderr_fd)


def wait_for_exit(child: Child, *, poll_interval: float) -> int:
    """Wait until ``child`` exits; its return code (negative: killed by that signal)."""
    while True:
        code = child.wait(poll_interval)
        if code is not None:
            return code
        # TODO(#19): check the run's bound and the session_start_timeout warning here.
        # TODO(#20): deliver new events of the run (post-state-change) here.


def end_child(child: Child, *, kill_grace: float) -> None:
    """SIGTERM to the exact child spawned (``Child`` signals nothing once it is reaped, so a
    reused PID is never hit), SIGKILL after ``kill_grace`` (also when the wait is
    interrupted), then reap it."""
    try:
        child.send_signal(signal.SIGTERM)
        child.wait(kill_grace)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGKILL)
            child.wait()


# Implicit done


def _accept_result_md(
    text: object, *, run_fd: int, secret: str | None, artifact_max_bytes: int
) -> bytes | None:
    """The ``result.md`` bytes when the final result's text passes the ingestion checks
    (a string, at most ``artifact_max_bytes``, neither the run secret in any letter case nor
    a ``contains_secret`` value); None otherwise. A missing or unusable index rejects."""
    if not isinstance(text, str):
        return None
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(data) > artifact_max_bytes:
        return None
    if secret is not None and secret.encode("ascii").lower() in data.lower():
        return None
    try:
        index = secretindex.read_at(run_fd)
    except (OSError, secretindex.SecretIndexError):
        return None
    if index.contains(data):
        return None
    return data


def freeze_result_md(
    st: store.Store, sid: str, run_no: int, run_fd: int, data: bytes, private_run_dir: str
) -> outbox.Frozen:
    """Write ``artifact/<ingest_seq>/result.md`` in the private run directory (``ingest_seq``:
    the run's ingested requests plus this one)."""
    seq = st.ingested_count(sid, run_no) + 1
    artifact_fd = fsutil.ensure_private_dir_at(run_fd, outbox.ARTIFACT)
    try:
        seq_fd = fsutil.ensure_private_dir_at(artifact_fd, str(seq))
        try:
            fsutil.write_private_file_at(seq_fd, IMPLICIT_ARTIFACT, data)
        finally:
            os.close(seq_fd)
    finally:
        os.close(artifact_fd)
    return outbox.Frozen(
        name=IMPLICIT_ARTIFACT,
        path=os.path.join(private_run_dir, outbox.ARTIFACT, str(seq), IMPLICIT_ARTIFACT),
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        ingest_seq=seq,
    )


# The end


def finish(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    process: launch.Process,
    *,
    returncode: int | None,
    implicit_done: bool,
    secret: str | None,
) -> Ended:
    """After ``claude`` exited: final drain, end mapping, implicit done, finalization, then
    the secret index deleted and the tmpdir removed (on every path). Returns the outcome's
    exit code."""
    sid, run_no = alloc.csession_id, alloc.run
    warnings: list[str] = []
    try:
        for _ in range(FINAL_DRAINS):
            # Again while requests wait: one drain may defer some at its deadline.
            drained = hookevent.final_drain(st, sid, run_no, process.state_root)
            if not drained or not _has_pending_requests(v, run_no):
                break
        pending = _has_pending_requests(v, run_no)
        if pending:
            warnings.append(
                "report requests of this run were not ingested: the final outbox drain could"
                " not process them (see log.jsonl, hook_event_failed)"
            )
        run_fd = v.dirs.open_run_dir(run_no)
        try:
            parsed = endmap.read_result_file(run_fd)
            _finalize(st, cfg, alloc, v, run_fd, parsed, returncode, implicit_done, pending, secret)
        finally:
            _delete_index(run_fd, warnings)
            os.close(run_fd)
    finally:
        _remove_tmpdir(alloc, v, warnings)
    st.add_run_warnings(sid, run_no, warnings)
    final = st.run_record(sid, run_no)
    return Ended(exit_code=int(final["exit_code"]), warnings=tuple(warnings))


def _finalize(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    run_fd: int,
    parsed: HeadlessResult,
    returncode: int | None,
    implicit_done: bool,
    pending: bool,
    secret: str | None,
) -> None:
    """Map the end and finalize, requiring the report status it was mapped from: a report
    that landed in between is mapped again (``FINALIZE_ATTEMPTS``)."""
    sid, run_no = alloc.csession_id, alloc.run
    for _ in range(FINALIZE_ATTEMPTS):
        record = st.run_record(sid, run_no)
        if record["ended_at"] is not None:
            return  # lost: another finalizer won
        mapped = endmap.map_headless(
            pending_cause=None,
            report_status=record["report_status"],
            result=parsed,
            returncode=returncode,
            session_started=record["session_started_at"] is not None,
            implicit_done=implicit_done,
        )
        expected = record["report_status"]
        if mapped.implicit and pending:
            # A report the agent filed may wait unprocessed: never read the run as done.
            mapped = Mapped("exited-unreported", "result_rejected")
        elif mapped.implicit:
            accepted = _implicit(st, cfg, alloc, v, run_fd, parsed, secret)
            if accepted is None:
                continue  # a report was recorded meanwhile: map it
            mapped = accepted
            expected = "done" if accepted.outcome == "done" else None
        if st.finalize_run(sid, run_no, _final(mapped, parsed), report_status=expected):
            return
    raise InternalError("the run's report kept changing while its end was mapped")


def _has_pending_requests(v: launch.Validated, run_no: int) -> bool:
    """Whether report requests wait in ``outbox/`` or the run's ``claimed/`` (best effort)."""
    for path in (
        os.path.join(v.dirs.session_path, outbox.OUTBOX),
        os.path.join(v.dirs.private_path, "runs", str(run_no), outbox.CLAIMED),
    ):
        try:
            if any(outbox.is_request_name(name) for name in os.listdir(path)):
                return True
        except OSError:
            continue
    return False


def _implicit(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    run_fd: int,
    parsed: HeadlessResult,
    secret: str | None,
) -> Mapped | None:
    """The implicit done (``result.md`` frozen and stored), ``result_rejected``, or None
    when a report was recorded since the end was mapped."""
    assert parsed.final is not None
    data = _accept_result_md(
        parsed.final.get("result"),
        run_fd=run_fd,
        secret=secret,
        artifact_max_bytes=cfg.artifact_max_bytes,
    )
    if data is None:
        return Mapped("exited-unreported", "result_rejected")
    private_run_dir = os.path.join(v.dirs.private_path, "runs", str(alloc.run))
    frozen = freeze_result_md(st, alloc.csession_id, alloc.run, run_fd, data, private_run_dir)
    if not st.set_implicit_report(alloc.csession_id, alloc.run, frozen):
        return None  # a report was recorded meanwhile: never overwrite it
    return Mapped("done", "normal")


def _final(mapped: Mapped, parsed: HeadlessResult) -> store.Final:
    is_error = None
    if parsed.final is not None and isinstance(parsed.final.get("is_error"), bool):
        is_error = parsed.final["is_error"]
    return store.Final(
        phase="session",
        outcome=mapped.outcome,
        end_reason=mapped.end_reason,
        exit_code=EXIT_CODES[mapped.outcome],
        state=endmap.final_state(mapped.outcome),
        cause="exit",
        cost_usd=parsed.cost_usd,
        num_turns=parsed.num_turns,
        is_error=is_error,
    )


def _remove_tmpdir(alloc: launch.Allocated, v: launch.Validated, warnings: list[str]) -> None:
    if v.tmpdir is None:
        return
    try:
        tmpdir.remove(alloc.csession_id)
    except tmpdir.TmpdirLeftover as exc:
        warnings.append(f"{exc}; {exc.hint}")
    except OSError as exc:
        warnings.append(f"could not remove the private temporary directory: {exc.strerror}")


def _delete_index(run_fd: int, warnings: list[str]) -> None:
    """The secret index is secret-equivalent: delete it once nothing scans for this run."""
    try:
        os.unlink(secretindex.INDEX_NAME, dir_fd=run_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        warnings.append(f"could not delete the run's secret index: {exc.strerror}")


# The whole headless run


def run_headless(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    prepared: launch.Prepared,
    process: launch.Process,
    *,
    implicit_done: bool,
) -> Ended:
    """Spawn, record, supervise and finish (see the module docs). A spawn that fails ends
    the run in the launch phase (exit 12; a ``KeyboardInterrupt`` is re-raised)."""
    sid, run_no = alloc.csession_id, alloc.run
    secret = prepared.run_secret
    try:
        record = st.run_record(sid, run_no)
        child = spawn(prepared, v, process)
    except Exception as exc:
        error = launch.interrupted(exc, _not_started(exc))
        launch.end_launch(st, alloc, v, None, process, error, exc=exc)
        return Ended(exit_code=EXIT_CODES["not-started"], warnings=())
    except BaseException as exc:
        launch.end_launch(st, alloc, v, None, process, launch.interrupted(exc), exc=exc)
        raise
    try:
        st.update_run(
            sid,
            run_no,
            phase="session",
            child_pid=child.pid,
            child_start=store.start_time(child.pid, zombie=True),  # unreaped: still ours
            started_at=store.format_ts(st.clock()),
        )
        log = launch.event_log(process, sorted(prepared.secrets))
        launch.log_best_effort(
            log,
            "launch",
            csession_id=sid,
            run=run_no,
            pid=child.pid,
            decision_ids=list(_decision_ids(record)),
        )
        returncode = wait_for_exit(child, poll_interval=cfg.poll_interval)
    except BaseException:
        _internal_error(st, cfg, alloc, v, child)
        raise
    try:
        return finish(
            st,
            cfg,
            alloc,
            v,
            process,
            returncode=returncode,
            implicit_done=implicit_done,
            secret=secret,
        )
    except BaseException:
        _internal_error(st, cfg, alloc, v, None)
        raise


def _not_started(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return f"claude could not be started: {exc.strerror or type(exc).__name__}"
    return f"claude could not be started ({type(exc).__name__})"


def _decision_ids(record: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(record.get("decision_ids_json") or "[]")
    except ValueError:
        return []
    return [item for item in value if isinstance(item, str)]


def _ignore_sigint() -> Any:
    """Ignore SIGINT (a second Ctrl-C must not cut the cleanup short); returns the handler to
    restore, or None when this is not the main thread."""
    try:
        return signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        return None


def _internal_error(
    st: store.Store,
    cfg: Config,
    alloc: launch.Allocated,
    v: launch.Validated,
    child: Child | None,
) -> None:
    """``exec`` must exit after an internal error: with SIGINT ignored, record
    ``internal_error`` before any signal, end the child, and finalize directly (``killed``,
    or the report's outcome when one exists; cost null), then delete the secret index and
    remove the tmpdir on every path. Best effort: a failure here is left to the liveness
    check (#33), which finds the wrapper dead."""
    sid, run_no = alloc.csession_id, alloc.run
    previous = _ignore_sigint()
    try:
        try:
            if st.run_record(sid, run_no)["ended_at"] is None:
                st.update_run(sid, run_no, pending_cause="internal_error")
        except Exception:  # the child is ended either way
            pass
        if child is not None:
            try:
                end_child(child, kill_grace=cfg.kill_grace)
            except Exception:
                pass
        try:
            _finalize_internal_error(st, sid, run_no)
        except Exception:  # the original error is re-raised by the caller
            pass
    finally:
        warnings: list[str] = []
        try:
            run_fd = v.dirs.open_run_dir(run_no)
            try:
                _delete_index(run_fd, warnings)
            finally:
                os.close(run_fd)
        except OSError:
            pass
        _remove_tmpdir(alloc, v, warnings)
        try:
            st.add_run_warnings(sid, run_no, warnings)
        except Exception:
            pass
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _finalize_internal_error(st: store.Store, sid: str, run_no: int) -> None:
    for _ in range(FINALIZE_ATTEMPTS):
        record = st.run_record(sid, run_no)
        if record["ended_at"] is not None:
            return
        mapped = endmap.map_headless(
            pending_cause="internal_error",
            report_status=record["report_status"],
            result=HeadlessResult(),
            returncode=None,
            session_started=record["session_started_at"] is not None,
            implicit_done=False,
        )
        if st.finalize_run(
            sid, run_no, _final(mapped, HeadlessResult()), report_status=record["report_status"]
        ):
            return


# The outcome object


def outcome_object(
    st: store.Store, sid: str, run_no: int, extra: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The run's outcome object (schemas/cli/outcome.json ``$defs/outcome``)."""
    row = st.run_record(sid, run_no)
    report = None
    if row["report_status"] is not None and row["outcome"] in endmap.REPORT_OUTCOMES:
        artifact = None
        if row["artifact_name"] is not None:
            artifact = {
                "name": row["artifact_name"],
                "path": row["artifact_path"],
                "sha256": row["artifact_sha256"],
                "size": row["artifact_size"],
            }
        report = {
            "status": row["report_status"],
            "implicit": bool(row["report_implicit"]),
            "summary": row["report_summary"],
            "reason": row["report_reason"],
            "artifact": artifact,
            "reported_at": row["reported_at"],
        }
    warnings = json.loads(row["warnings_json"] or "[]")
    return {
        "csession_id": sid,
        "run": run_no,
        "forked_from": row["session_forked_from"],
        "claude_session_id": row["session_claude_session_id"],
        "phase": row["phase"],
        "outcome": row["outcome"],
        "end_reason": row["end_reason"],
        "exit_code": row["exit_code"],
        "state": row["session_state"],
        "report": report,
        "session_started_at": row["session_started_at"],
        "cost_usd": None if row["phase"] == "launch" else row["cost_usd"],
        "message": row["message"],
        "warnings": list(dict.fromkeys([*warnings, *extra])),
    }
