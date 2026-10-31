"""``stop`` (interface.md, stop and Signals; design.md, Supervision: Stop).

Wrappers and children are real processes this test started (``test_liveness.Procs``: a live
one is a ``sleep`` with its PID and procinfo start token, a dead one exited after its token was
read); processes that must ignore SIGTERM signal readiness through a marker file before
``stop`` runs. End-to-end runs start a real ``claude-session exec`` with the fake ``claude``
hanging on its ``hang`` step (``wait_for`` the step record before ``stop``). Only exact PIDs
are signaled, by ``stop`` and by the tests' cleanup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from claude_session import cli, lifecycle, liveness, stopper, store
from claude_session.store import Final, Store

from .fakeclaude import read_jsonl, wait_for
from .schemas import validate
from .test_exec_headless import START, gone, run_row_of, start_hanging
from .test_liveness import Procs, final_events, run_row, session, write_private

STOP_SCHEMA = "claude-session/cli/stop.json"
FUTURE = timedelta(hours=1)
# Ignores SIGTERM, then touches its marker (argv[1]) and sleeps.
STUBBORN = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').close()\n"
    "time.sleep(120)\n"
)


@pytest.fixture
def procs() -> Iterator[Procs]:
    p = Procs()
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def st() -> Iterator[Store]:
    with store.open_state() as s:
        yield s


@pytest.fixture
def root(st: Store) -> str:
    return os.path.dirname(st.path)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("kill_grace: 0.5s\npoll_interval: 0.05s\n", encoding="utf-8")
    return path


def stubborn(procs: Procs, tmp_path: Path, name: str) -> tuple[int, str]:
    """A process that ignores SIGTERM (ready once its marker exists): PID and token."""
    from .test_liveness import wait_token

    marker = tmp_path / f"{name}.ready"
    proc = subprocess.Popen([sys.executable, "-c", STUBBORN, str(marker)])
    procs.started.append(proc)
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert proc.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    return proc.pid, wait_token(proc.pid)


def pending(st: Store, root: str) -> str:
    expires = store.format_ts(store.utc_now() + FUTURE)
    return session(st, root, wrapper=None, expires_at=expires)


def run_stop(
    capsys: pytest.CaptureFixture[str], config: Path, sid: str, *extra: str
) -> tuple[int, dict[str, Any]]:
    capsys.readouterr()
    code = cli.main(["stop", sid, "--json", "--config", str(config), *extra])
    out = capsys.readouterr().out
    data = json.loads(out)
    if "error" in data:
        validate(data, "claude-session/cli/error.json")
    else:
        validate(data, STOP_SCHEMA)
    return code, data


def audit_records(root: str) -> list[dict[str, Any]]:
    return read_jsonl(Path(root) / "audit.jsonl")


def sent(out: dict[str, Any]) -> list[tuple[int, str, str]]:
    return [(s["pid"], s["role"], s["signal"]) for s in out["signals"]]


# Lookup, already final, dry run


def test_an_unknown_session_exits_10(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store
) -> None:
    code, out = run_stop(capsys, config, "cs-aaaaaaaa")
    assert (code, out["error"]["code"]) == (10, "not_found")


def test_an_invalid_grace_exits_2(capsys: pytest.CaptureFixture[str], config: Path) -> None:
    code, out = run_stop(capsys, config, "cs-aaaaaaaa", "--grace", "-1")
    assert (code, out["error"]["code"]) == (2, "usage")


def test_a_final_run_is_left_alone(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    sid = session(st, root, wrapper=procs.live())
    st.finalize_run(sid, 1, Final("session", "lost", "lost", 17, "lost", "exit"))
    code, out = run_stop(capsys, config, sid)
    assert code == 0
    assert (out["already_final"], out["ended"], out["signals"]) == (True, True, [])
    assert (out["outcome"], out["end_reason"], out["run_exit_code"], out["state"]) == (
        "lost",
        "lost",
        17,
        "lost",
    )
    assert run_row(st, sid)["stop_requested_at"] is None
    assert audit_records(root) == []  # nothing was done


def test_a_run_the_liveness_check_ends_first_is_already_final(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    """The inline liveness check runs first: a dead wrapper with no child ends the run
    launch_interrupted (cause liveness) before stop would request anything."""
    sid = session(st, root, wrapper=procs.dead())
    code, out = run_stop(capsys, config, sid)
    assert (code, out["already_final"], out["end_reason"]) == (0, True, "launch_interrupted")
    assert final_events(st, sid) == [(1, "liveness", "not-started")]
    assert run_row(st, sid)["stop_requested_at"] is None


def test_a_run_that_ends_before_the_request_is_already_final(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """exec finishing between the liveness check and the request: nothing is signaled."""
    pid, token = procs.live()
    sid = session(st, root, wrapper=(pid, token))
    real = Store.request_stop

    def request_stop(self: Store, csession_id: str, run_no: int) -> bool:
        self.finalize_run(csession_id, run_no, Final("session", "lost", "lost", 17, "lost", "exit"))
        return real(self, csession_id, run_no)

    monkeypatch.setattr(Store, "request_stop", request_stop)
    code, out = run_stop(capsys, config, sid)
    assert (code, out["already_final"], out["signals"]) == (0, True, [])
    assert procs.running(pid)
    assert run_row(st, sid)["stop_requested_at"] is None


def test_a_dry_run_changes_nothing_and_names_the_first_signal(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    wrapper = procs.live()
    sid = session(st, root, wrapper=wrapper, child=procs.live())
    code, out = run_stop(capsys, config, sid, "--dry-run")
    assert code == 0
    assert (out["dry_run"], out["ended"], out["state"]) == (True, False, "starting")
    assert sent(out) == [(wrapper[0], "wrapper", "SIGTERM")]
    assert procs.running(wrapper[0])
    assert run_row(st, sid)["stop_requested_at"] is None
    assert audit_records(root) == []


def test_a_dry_run_on_a_pending_run_predicts_the_cancel(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str
) -> None:
    sid = pending(st, root)
    code, out = run_stop(capsys, config, sid, "--dry-run")
    assert (code, out["canceled"], out["end_reason"], out["signals"]) == (0, True, "canceled", [])
    assert run_row(st, sid)["ended_at"] is None


# Pending runs


def test_a_pending_run_is_canceled_without_signals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], st: Store, root: str
) -> None:
    hooked = tmp_path / "hooked.jsonl"
    outcome_file = tmp_path / "outcome.json"
    config = tmp_path / "config.yaml"
    config.write_text(
        f"hooks:\n  post-state-change:\n    - cmd: cat >> {hooked}; echo >> {hooked}\n",
        encoding="utf-8",
    )
    expires = store.format_ts(store.utc_now() + FUTURE)
    sid = session(st, root, wrapper=None, expires_at=expires, outcome_file=str(outcome_file))
    spec = write_private(root, sid, 1, lifecycle.SPEC_NAME, b'{"env": "credential"}')
    code, out = run_stop(capsys, config, sid, "--actor", "human:tester", "--decision-id", "d-1")
    assert code == 0
    assert (out["canceled"], out["ended"], out["signals"], out["recorded_by_stop"]) == (
        True,
        True,
        [],
        False,
    )
    assert (out["outcome"], out["end_reason"], out["state"], out["run_exit_code"]) == (
        "not-started",
        "canceled",
        "not-started",
        12,
    )
    row = run_row(st, sid)
    assert row["stop_requested_at"] is not None and row["phase"] == "launch"
    assert final_events(st, sid) == [(1, "stop", "not-started")]
    assert not spec.exists()  # it may hold credentials
    assert json.loads(outcome_file.read_text())["end_reason"] == "canceled"
    payloads = [json.loads(line) for line in hooked.read_text().splitlines() if line]
    assert payloads[-1]["final"] is True and payloads[-1]["end_reason"] == "canceled"
    [record] = audit_records(root)
    assert (record["action"], record["target"], record["result"], record["actor"]) == (
        "stop",
        sid,
        "ok",
        "human:tester",
    )
    assert record["decision_id"] == ["d-1"]
    assert record["inputs"] == {"run": 1, "grace": 10.0}  # the built-in kill_grace


def test_a_canceled_run_is_never_adopted(st: Store, root: str) -> None:
    sid = pending(st, root)
    assert st.cancel_pending(sid, 1, store.CANCELED)
    adopted = st.adopt(sid, wrapper_pid=os.getpid(), wrapper_start="x")
    assert adopted.outcome == "expired"
    assert not st.cancel_pending(sid, 1, store.CANCELED)  # one final event only
    assert final_events(st, sid) == [(1, "stop", "not-started")]


def test_an_adopted_run_is_never_canceled(st: Store, root: str) -> None:
    sid = pending(st, root)
    assert st.adopt(sid, wrapper_pid=os.getpid(), wrapper_start="x").outcome == "adopted"
    assert not st.cancel_pending(sid, 1, store.CANCELED)
    assert run_row(st, sid)["ended_at"] is None


def test_a_run_start_still_holds_is_not_pending(st: Store, root: str, procs: Procs) -> None:
    sid = session(st, root, wrapper=procs.live())
    assert not st.cancel_pending(sid, 1, store.CANCELED)


def race(sid: str) -> dict[str, Any]:
    """``adopt`` and ``cancel_pending`` of ``sid`` from two connections at once."""
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def adopt() -> None:
        with store.open_state() as s:
            barrier.wait()
            results["adopt"] = s.adopt(sid, wrapper_pid=os.getpid(), wrapper_start="x")

    def cancel() -> None:
        with store.open_state() as s:
            barrier.wait()
            results["cancel"] = s.cancel_pending(sid, 1, store.CANCELED)

    threads = [threading.Thread(target=adopt), threading.Thread(target=cancel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_cancel_and_adoption_racing_have_a_single_winner(st: Store, root: str) -> None:
    """Two connections (as two processes would have) race each pending run: exactly one of
    the compare-and-sets wins, every time."""
    for _ in range(25):
        sid = pending(st, root)
        results = race(sid)
        adopted = results["adopt"].outcome == "adopted"
        assert adopted != results["cancel"]
        row = run_row(st, sid)
        if adopted:
            assert row["ended_at"] is None and row["wrapper_pid"] == os.getpid()
        else:
            assert row["end_reason"] == "canceled" and row["wrapper_pid"] is None


# Signals: synthetic wrappers and children (real processes)


def test_a_wrapper_that_dies_on_sigterm_leaves_the_end_to_stop(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    """The SIGTERM goes to the wrapper only; a wrapper that dies without finalizing leaves
    an orphaned child, which gets the SIGTERM next (pending_cause stopped first); stop then
    records the end itself."""
    wrapper, child = procs.live(), procs.live()
    sid = session(st, root, wrapper=wrapper, child=child)
    code, out = run_stop(capsys, config, sid, "--grace", "5s")
    assert code == 0
    assert sent(out) == [(wrapper[0], "wrapper", "SIGTERM"), (child[0], "child", "SIGTERM")]
    assert (out["outcome"], out["end_reason"], out["state"], out["run_exit_code"]) == (
        "killed",
        "stopped",
        "killed",
        15,
    )
    assert out["recorded_by_stop"] is True
    assert run_row(st, sid)["pending_cause"] == "stopped"
    assert final_events(st, sid) == [(1, "stop", "killed")]
    assert not procs.running(wrapper[0]) and not procs.running(child[0])


def test_after_grace_the_child_is_killed_first_then_the_wrapper(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    wrapper = stubborn(procs, tmp_path, "wrapper")
    child = stubborn(procs, tmp_path, "child")
    sid = session(st, root, wrapper=wrapper, child=child)
    began = time.monotonic()
    code, out = run_stop(capsys, config, sid, "--grace", "0.3s")
    took = time.monotonic() - began
    assert code == 0
    assert sent(out) == [
        (wrapper[0], "wrapper", "SIGTERM"),
        (child[0], "child", "SIGKILL"),
        (wrapper[0], "wrapper", "SIGKILL"),
    ]
    assert 0.3 + 0.5 <= took < 10, took  # grace, then kill_grace for the wrapper to finalize
    assert (out["outcome"], out["end_reason"], out["recorded_by_stop"]) == (
        "killed",
        "stopped",
        True,
    )
    assert not procs.running(wrapper[0]) and not procs.running(child[0])
    log = [r for r in read_jsonl(Path(root) / "log.jsonl") if r["event"] == "signal_sent"]
    assert [(r["pid"], r["signal"]) for r in log] == [
        (wrapper[0], "SIGTERM"),
        (child[0], "SIGKILL"),
        (wrapper[0], "SIGKILL"),
    ]


def test_a_report_keeps_its_outcome_when_stop_ends_the_run(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    child = procs.live()
    sid = session(st, root, wrapper=procs.dead(), child=child, report_status="blocked")
    code, out = run_stop(capsys, config, sid)
    assert code == 0
    assert sent(out) == [(child[0], "child", "SIGTERM")]
    assert (out["outcome"], out["end_reason"], out["state"], out["run_exit_code"]) == (
        "blocked",
        "stopped",
        "exited",
        13,
    )


def test_an_orphaned_child_is_ended_and_recorded_by_stop(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    child = procs.live()
    sid = session(st, root, wrapper=procs.dead(), child=child)
    code, out = run_stop(capsys, config, sid)
    assert code == 0
    assert sent(out) == [(child[0], "child", "SIGTERM")]
    assert (out["outcome"], out["end_reason"], out["recorded_by_stop"]) == (
        "killed",
        "stopped",
        True,
    )


def test_a_launch_phase_wrapper_killed_by_stop_ends_canceled(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """No child yet (pre-launch hooks run): after grace the wrapper itself is SIGKILLed and
    stop ends the run canceled through the liveness path."""
    wrapper = stubborn(procs, tmp_path, "wrapper")
    sid = session(st, root, wrapper=wrapper)
    spec = write_private(root, sid, 1, lifecycle.SPEC_NAME)
    code, out = run_stop(capsys, config, sid, "--grace", "0.2s")
    assert code == 0
    assert sent(out) == [(wrapper[0], "wrapper", "SIGTERM"), (wrapper[0], "wrapper", "SIGKILL")]
    assert (out["outcome"], out["end_reason"], out["state"], out["recorded_by_stop"]) == (
        "not-started",
        "canceled",
        "not-started",
        True,
    )
    assert not spec.exists()


def test_an_unverifiable_wrapper_is_never_signaled_and_stays_live(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, st: Store, root: str, procs: Procs
) -> None:
    """An empty start token keeps the PID live (liveness) but never verifies it: nothing is
    signaled, and stop reports the run still live (exit 12) after its wait."""
    config = tmp_path / "fast.yaml"
    config.write_text("kill_grace: 0.1s\npoll_interval: 0.05s\n", encoding="utf-8")
    pid, _token = procs.live()
    sid = session(st, root, wrapper=(pid, ""))
    code, out = run_stop(capsys, config, sid, "--grace", "0.1s")
    assert code == 12
    assert (out["ended"], out["signals"], out["state"], out["outcome"]) == (
        False,
        [],
        "starting",
        None,
    )
    assert procs.running(pid)
    assert run_row(st, sid)["stop_requested_at"] is not None
    not_sent = [r for r in read_jsonl(Path(root) / "log.jsonl") if r["event"] == "signal_not_sent"]
    assert not_sent and all("pid" not in r or r["pid"] is None for r in not_sent)


def test_a_reused_pid_is_never_signaled(procs: Procs) -> None:
    pid, token = procs.live()
    assert not stopper.send_verified(pid, "0:1", signal.SIGTERM)
    assert not stopper.send_verified(pid, "", signal.SIGTERM)
    for bad in (0, -1, 2**31, True, "1", None):
        assert not stopper.send_verified(bad, token, signal.SIGTERM)
    assert procs.running(pid)
    assert stopper.send_verified(pid, token, signal.SIGTERM)


def test_an_audit_record_that_cannot_be_written_blocks_stop(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    """Fail closed (interface.md, Audit and log): exit 1, nothing requested or signaled.
    TODO(#49): the maintainer's decision on exempting stop may change this."""
    wrapper = procs.live()
    sid = session(st, root, wrapper=wrapper)
    (Path(root) / "audit.jsonl").mkdir()
    code, out = run_stop(capsys, config, sid)
    assert (code, out["error"]["code"]) == (1, "internal")
    assert procs.running(wrapper[0])
    assert run_row(st, sid)["stop_requested_at"] is None


def test_the_liveness_launch_end_of_a_stop_request_is_canceled(
    st: Store, root: str, procs: Procs
) -> None:
    sid = session(st, root, wrapper=procs.dead())
    st.request_stop(sid, 1)
    predicted = liveness.check_session(st, None, sid, dry_run=True).changes
    assert [c.end_reason for c in predicted] == ["canceled"]
    liveness.check_session(st, None, sid)
    assert run_row(st, sid)["end_reason"] == "canceled"


def log_records(root: str, event: str) -> list[dict[str, Any]]:
    return [r for r in read_jsonl(Path(root) / "log.jsonl") if r["event"] == event]


def set_child(st: Store, sid: str, child: tuple[int, str]) -> None:
    st.conn.execute(
        "UPDATE runs SET child_pid = ?, child_start = ?, phase = 'session' "
        "WHERE csession_id = ? AND run_no = 1",
        (child[0], child[1], sid),
    )


def set_wrapper(st: Store, sid: str, wrapper: tuple[int, str]) -> None:
    st.conn.execute(
        "UPDATE runs SET wrapper_pid = ?, wrapper_start = ? WHERE csession_id = ? AND run_no = 1",
        (wrapper[0], wrapper[1], sid),
    )


def on_first_kill_phase(monkeypatch: pytest.MonkeyPatch, action: Any) -> None:
    """Run ``action`` once, right after stop's grace-end SIGKILL step for a run that has no
    child recorded (the kill phase has started)."""
    real = stopper.Stop._signal_child  # pyright: ignore[reportPrivateUsage]
    done: list[bool] = []

    def signal_child(self: stopper.Stop, result: Any, record: dict[str, Any], sig: int) -> None:
        real(self, result, record, sig)
        if sig == signal.SIGKILL and record["child_pid"] is None and not done:
            done.append(True)
            action()

    monkeypatch.setattr(stopper.Stop, "_signal_child", signal_child)


def after_send(monkeypatch: pytest.MonkeyPatch, role: str, sig: int, action: Any) -> None:
    """Run ``action`` once, right after stop sent ``sig`` to the ``role``."""
    real = stopper.Stop._send  # pyright: ignore[reportPrivateUsage]
    done: list[bool] = []

    def send(self: stopper.Stop, result: Any, record: dict[str, Any], role_: str, sig_: int) -> str:
        sent_now = real(self, result, record, role_, sig_)
        if sent_now == stopper.SENT and (role_, sig_) == (role, sig) and not done:
            done.append(True)
            action()
        return sent_now

    monkeypatch.setattr(stopper.Stop, "_send", send)


# The kill phase (review round 1)


def test_a_wrapper_whose_child_is_gone_still_gets_kill_grace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """No child SIGKILL was needed (the child had exited), yet the wrapper gets kill_grace to
    finish its closing pass before its SIGKILL; pending_cause stopped is recorded first."""
    wrapper = stubborn(procs, tmp_path, "wrapper")
    sid = session(st, root, wrapper=wrapper, child=procs.dead())
    began = time.monotonic()
    code, out = run_stop(capsys, config, sid, "--grace", "0.3s")
    took = time.monotonic() - began
    assert code == 0
    assert sent(out) == [(wrapper[0], "wrapper", "SIGTERM"), (wrapper[0], "wrapper", "SIGKILL")]
    assert 0.3 + 0.5 <= took < 10, took
    assert run_row(st, sid)["pending_cause"] == "stopped"
    assert (out["end_reason"], out["recorded_by_stop"]) == ("stopped", True)


def test_a_child_recorded_during_kill_grace_is_killed_before_the_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    wrapper = stubborn(procs, tmp_path, "wrapper")
    child = stubborn(procs, tmp_path, "child")
    sid = session(st, root, wrapper=wrapper)
    on_first_kill_phase(monkeypatch, lambda: set_child(st, sid, child))
    code, out = run_stop(capsys, config, sid, "--grace", "0.2s")
    assert code == 0
    assert sent(out) == [
        (wrapper[0], "wrapper", "SIGTERM"),
        (child[0], "child", "SIGKILL"),
        (wrapper[0], "wrapper", "SIGKILL"),
    ]
    assert run_row(st, sid)["pending_cause"] == "stopped"
    assert out["ended"] is True
    assert not procs.running(wrapper[0]) and not procs.running(child[0])


def test_a_live_child_that_cannot_be_verified_keeps_its_wrapper(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """The child's PID exists but its token is empty: SIGKILLing the wrapper would leave it
    unsupervised, so the wrapper is not SIGKILLed and the run stays live (exit 12)."""
    wrapper = stubborn(procs, tmp_path, "wrapper")
    child_pid, _ = procs.live()
    sid = session(st, root, wrapper=wrapper, child=(child_pid, ""))
    code, out = run_stop(capsys, config, sid, "--grace", "0.1s")
    assert (code, out["ended"]) == (12, False)
    assert sent(out) == [(wrapper[0], "wrapper", "SIGTERM")]
    assert procs.running(wrapper[0]) and procs.running(child_pid)
    assert any(
        r["signal"] == "SIGKILL" and "wrapper" in r["message"]
        for r in log_records(root, "signal_not_sent")
    )


def test_with_an_unverifiable_wrapper_the_child_gets_sigterm_before_sigkill(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    wrapper_pid, _ = procs.live()
    child = stubborn(procs, tmp_path, "child")
    sid = session(st, root, wrapper=(wrapper_pid, ""), child=child)
    code, out = run_stop(capsys, config, sid, "--grace", "0.3s")
    assert code == 12  # the unverifiable wrapper stays live
    assert sent(out) == [(child[0], "child", "SIGTERM"), (child[0], "child", "SIGKILL")]
    assert run_row(st, sid)["pending_cause"] == "stopped"
    log = log_records(root, "signal_sent")
    term, kill = (r for r in log if r["pid"] == child[0])
    assert term["signal"] == "SIGTERM" and kill["signal"] == "SIGKILL"
    assert procs.running(wrapper_pid)


def test_a_wrapper_that_took_over_after_the_sigterm_gets_one_too(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """The run changes hands (another wrapper recorded) after stop's SIGTERM to the first one:
    the new wrapper gets its own SIGTERM, not just the grace-end SIGKILL."""
    first, second = procs.live(), procs.live()
    sid = session(st, root, wrapper=first)
    after_send(monkeypatch, "wrapper", signal.SIGTERM, lambda: set_wrapper(st, sid, second))
    code, out = run_stop(capsys, config, sid, "--grace", "5s")
    assert code == 0
    assert sent(out) == [(first[0], "wrapper", "SIGTERM"), (second[0], "wrapper", "SIGTERM")]
    assert not procs.running(second[0])


def test_an_orphan_that_appears_in_the_kill_phase_gets_sigkill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    wrapper = stubborn(procs, tmp_path, "wrapper")
    child = stubborn(procs, tmp_path, "child")
    dead = procs.dead()
    sid = session(st, root, wrapper=wrapper)

    def orphan() -> None:
        set_child(st, sid, child)
        set_wrapper(st, sid, dead)

    on_first_kill_phase(monkeypatch, orphan)
    code, out = run_stop(capsys, config, sid, "--grace", "0.2s")
    assert code == 0
    assert sent(out) == [(wrapper[0], "wrapper", "SIGTERM"), (child[0], "child", "SIGKILL")]
    assert (out["end_reason"], out["recorded_by_stop"]) == ("stopped", True)
    assert not procs.running(child[0])


def test_an_orphaned_child_ignoring_sigterm_is_killed_after_grace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    child = stubborn(procs, tmp_path, "child")
    sid = session(st, root, wrapper=procs.dead(), child=child)
    began = time.monotonic()
    code, out = run_stop(capsys, config, sid, "--grace", "0.3s")
    took = time.monotonic() - began
    assert code == 0
    assert sent(out) == [(child[0], "child", "SIGTERM"), (child[0], "child", "SIGKILL")]
    assert 0.3 <= took < 10, took
    assert (out["outcome"], out["end_reason"], out["recorded_by_stop"]) == (
        "killed",
        "stopped",
        True,
    )


# Races and failures (review round 1)


def test_a_run_ended_while_stop_cancels_it_is_already_final(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
) -> None:
    """Another process ends the pending run between stop's request and its cancel: stop lost
    the race, sent nothing and reports already_final."""
    sid = pending(st, root)
    real = Store.cancel_pending

    def cancel_pending(self: Store, csession_id: str, run_no: int, final: Final) -> bool:
        self.finalize_run(csession_id, run_no, Final("session", "lost", "lost", 17, "lost", "exit"))
        return real(self, csession_id, run_no, final)

    monkeypatch.setattr(Store, "cancel_pending", cancel_pending)
    code, out = run_stop(capsys, config, sid)
    assert (code, out["already_final"], out["canceled"], out["signals"]) == (0, True, False, [])
    assert out["end_reason"] == "lost"


def test_a_signal_not_sent_clears_the_cause_stop_recorded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """Not sent for a reason other than the target being gone (EPERM, a pidfd error): the
    cause this stop recorded is cleared again."""
    config = tmp_path / "fast.yaml"
    config.write_text("kill_grace: 0.1s\npoll_interval: 0.05s\n", encoding="utf-8")
    child = procs.live()
    sid = session(st, root, wrapper=procs.dead(), child=child)
    monkeypatch.setattr(stopper, "deliver", lambda pid, token, sig: stopper.NOT_SENT)
    code, out = run_stop(capsys, config, sid, "--grace", "0.1s")
    assert (code, out["signals"]) == (12, [])
    assert run_row(st, sid)["pending_cause"] is None
    assert procs.running(child[0])


def test_a_signal_to_a_gone_target_keeps_the_cause_stop_recorded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """The target is gone (ESRCH, or its token no longer matches): stop was requested, so the
    cause it recorded stays."""
    config = tmp_path / "fast.yaml"
    config.write_text("kill_grace: 0.1s\npoll_interval: 0.05s\n", encoding="utf-8")
    child = procs.live()
    sid = session(st, root, wrapper=procs.dead(), child=child)
    monkeypatch.setattr(stopper, "deliver", lambda pid, token, sig: stopper.GONE)
    cleared: list[str] = []
    real_clear = Store.clear_pending_cause

    def clear(self: Store, csession_id: str, run_no: int, cause: str) -> bool:
        cleared.append(cause)
        return real_clear(self, csession_id, run_no, cause)

    monkeypatch.setattr(Store, "clear_pending_cause", clear)
    code, out = run_stop(capsys, config, sid, "--grace", "0.1s")
    assert (code, out["signals"]) == (12, [])
    assert cleared == []
    assert run_row(st, sid)["pending_cause"] == "stopped"


def test_a_cause_a_second_stop_relied_on_is_kept(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """Two stops: this one records ``stopped``, then a second stop SIGKILLs the child (its own
    set found the cause already there) before this one's send, which finds the child gone.
    The cause stays, so the run's end reason is ``stopped``, never an unexplained signal."""
    child = procs.live()
    sid = session(st, root, wrapper=procs.dead(), child=child)
    real = stopper.deliver
    statuses: list[str] = []

    def deliver(pid: Any, token: Any, sig: int) -> str:
        if pid == child[0] and not statuses:
            assert not st.set_pending_cause(sid, 1, "stopped")  # the second stop's set
            proc = next(p for p in procs.started if p.pid == child[0])
            proc.kill()  # the second stop's SIGKILL, to the exact process this test started
            proc.wait(timeout=30)
        status = real(pid, token, sig)
        statuses.append(status)
        return status

    monkeypatch.setattr(stopper, "deliver", deliver)
    code, out = run_stop(capsys, config, sid)
    assert statuses[0] == stopper.GONE
    assert (code, out["ended"], out["recorded_by_stop"], out["signals"]) == (0, True, True, [])
    assert run_row(st, sid)["pending_cause"] == "stopped"
    assert out["end_reason"] == "stopped"


def test_an_unverifiable_wrapper_never_sees_the_cause(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """A live wrapper that cannot be verified, with a dead recorded child: stop neither
    records ``pending_cause`` nor sends anything, on any poll (a live exec finalizing its
    dead child must not read a transient ``stopped``)."""
    config = tmp_path / "fast.yaml"
    config.write_text("kill_grace: 0.1s\npoll_interval: 0.05s\n", encoding="utf-8")
    wrapper_pid, _ = procs.live()
    sid = session(st, root, wrapper=(wrapper_pid, ""), child=procs.dead())
    calls: list[str] = []
    real_set = Store.set_pending_cause

    def spy(self: Store, csession_id: str, run_no: int, cause: str) -> bool:
        calls.append(cause)
        return real_set(self, csession_id, run_no, cause)

    monkeypatch.setattr(Store, "set_pending_cause", spy)
    code, out = run_stop(capsys, config, sid, "--grace", "0.1s")
    assert (code, out["signals"]) == (12, [])
    assert calls == []
    assert run_row(st, sid)["pending_cause"] is None
    assert procs.running(wrapper_pid)
    kills = [r for r in log_records(root, "signal_not_sent") if r["signal"] == "SIGKILL"]
    assert len([r for r in kills if "wrapper" in r["message"]]) == 1


def test_clearing_the_cause_leaves_another_cause_alone(st: Store, root: str, procs: Procs) -> None:
    sid = session(st, root, wrapper=procs.live(), pending_cause="timeout")
    assert not st.set_pending_cause(sid, 1, "stopped")
    assert not st.clear_pending_cause(sid, 1, "stopped")
    assert run_row(st, sid)["pending_cause"] == "timeout"


def test_a_start_release_before_the_first_signal_is_a_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """start releases its run (pending again) after stop saw it held: stop cancels it with no
    signal (canceled true)."""
    outcome_file = tmp_path / "outcome.json"
    wrapper = procs.live()
    sid = session(st, root, wrapper=wrapper, outcome_file=str(outcome_file))
    spec = write_private(root, sid, 1, lifecycle.SPEC_NAME, b'{"env": "credential"}')
    real = stopper.Stop._signal_path  # pyright: ignore[reportPrivateUsage]

    def signal_path(self: stopper.Stop, result: stopper.Stopped) -> None:
        assert st.release_pending(
            sid, 1, wrapper_pid=wrapper[0], wrapper_start=wrapper[1], pending_for=3600
        )
        real(self, result)

    monkeypatch.setattr(stopper.Stop, "_signal_path", signal_path)
    code, out = run_stop(capsys, config, sid)
    assert code == 0
    assert (out["canceled"], out["recorded_by_stop"], out["signals"]) == (True, False, [])
    assert out["end_reason"] == "canceled"
    assert not spec.exists()
    assert json.loads(outcome_file.read_text())["end_reason"] == "canceled"
    assert final_events(st, sid) == [(1, "stop", "not-started")]
    assert procs.running(wrapper[0])


def test_a_start_release_after_the_sigterm_is_ended_by_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    """start releases the run after stop's SIGTERM reached it: stop cancels the pending run,
    but it was signaled, so canceled is false and recorded_by_stop true."""
    outcome_file = tmp_path / "outcome.json"
    wrapper = stubborn(procs, tmp_path, "start")
    sid = session(st, root, wrapper=wrapper, outcome_file=str(outcome_file))
    spec = write_private(root, sid, 1, lifecycle.SPEC_NAME, b'{"env": "credential"}')

    def release() -> None:
        assert st.release_pending(
            sid, 1, wrapper_pid=wrapper[0], wrapper_start=wrapper[1], pending_for=3600
        )

    after_send(monkeypatch, "wrapper", signal.SIGTERM, release)
    code, out = run_stop(capsys, config, sid, "--grace", "5s")
    assert code == 0
    assert (out["canceled"], out["recorded_by_stop"]) == (False, True)
    assert sent(out) == [(wrapper[0], "wrapper", "SIGTERM")]
    assert (out["outcome"], out["end_reason"]) == ("not-started", "canceled")
    assert not spec.exists()
    assert json.loads(outcome_file.read_text())["end_reason"] == "canceled"
    assert final_events(st, sid) == [(1, "stop", "not-started")]


def test_a_failed_stop_adds_a_best_effort_error_record(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: Path,
    st: Store,
    root: str,
    procs: Procs,
) -> None:
    sid = session(st, root, wrapper=procs.live())

    def fail(self: stopper.Stop, sid_: str, run: int) -> stopper.Stopped:
        raise RuntimeError("boom")

    monkeypatch.setattr(stopper.Stop, "_stop", fail)
    code, out = run_stop(capsys, config, sid)
    assert (code, out["error"]["code"]) == (1, "internal")
    assert [(r["action"], r["result"]) for r in audit_records(root)] == [
        ("stop", "ok"),
        ("stop", "error"),
    ]


# Dry runs (review round 1)


def test_a_dry_run_on_a_final_run_is_already_final(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    sid = session(st, root, wrapper=procs.live())
    st.finalize_run(sid, 1, Final("session", "lost", "lost", 17, "lost", "exit"))
    code, out = run_stop(capsys, config, sid, "--dry-run")
    assert (code, out["already_final"], out["ended"], out["end_reason"]) == (0, True, True, "lost")


def test_a_dry_run_predicts_the_liveness_end_and_changes_nothing(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    sid = session(st, root, wrapper=procs.dead())
    code, out = run_stop(capsys, config, sid, "--dry-run")
    assert (code, out["already_final"], out["ended"]) == (0, True, True)
    assert (out["outcome"], out["end_reason"], out["signals"]) == (
        "not-started",
        "launch_interrupted",
        [],
    )
    assert run_row(st, sid)["ended_at"] is None
    assert final_events(st, sid) == []


def test_a_dry_run_on_an_orphan_names_the_child_sigterm(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str, procs: Procs
) -> None:
    child = procs.live()
    sid = session(st, root, wrapper=procs.dead(), child=child)
    code, out = run_stop(capsys, config, sid, "--dry-run")
    assert (code, out["ended"], sent(out)) == (0, False, [(child[0], "child", "SIGTERM")])
    assert procs.running(child[0])
    assert run_row(st, sid)["pending_cause"] is None


# Exact signals through pidfds (review round 1)


PIDFD_MACHINE = sys.platform.startswith("linux") and os.uname().machine in ("x86_64", "aarch64")


@pytest.mark.skipif(not PIDFD_MACHINE, reason="pidfds: x86_64 and aarch64 Linux only")
def test_a_signal_goes_through_a_pidfd_on_linux(
    monkeypatch: pytest.MonkeyPatch, procs: Procs
) -> None:
    from claude_session import gateway

    calls: list[int] = []
    real = gateway.pidfd_send_signal

    def spy(fd: int, sig: int) -> None:
        calls.append(sig)
        real(fd, sig)

    monkeypatch.setattr(gateway, "pidfd_send_signal", spy)
    pid, token = procs.live()
    assert stopper.send_verified(pid, token, signal.SIGTERM)
    assert calls == [signal.SIGTERM]
    proc = next(p for p in procs.started if p.pid == pid)
    assert proc.wait(timeout=30) == -signal.SIGTERM


def test_without_a_pidfd_the_token_is_checked_before_kill(
    monkeypatch: pytest.MonkeyPatch, procs: Procs
) -> None:
    import errno

    from claude_session import gateway

    def no_pidfd(pid: int) -> int:
        raise OSError(errno.ENOSYS, "no pidfd")

    monkeypatch.setattr(gateway, "pidfd_open", no_pidfd)
    pid, token = procs.live()
    assert not stopper.send_verified(pid, "0:1", signal.SIGTERM)
    assert procs.running(pid)
    assert stopper.send_verified(pid, token, signal.SIGTERM)
    proc = next(p for p in procs.started if p.pid == pid)
    assert proc.wait(timeout=30) == -signal.SIGTERM


def test_a_pidfd_error_other_than_esrch_falls_back_to_the_token_check(
    monkeypatch: pytest.MonkeyPatch, procs: Procs
) -> None:
    """A seccomp profile refusing pidfd_open (EPERM) is no pidfd, not a gone process: the
    token check then kill fallback signals the verified process."""
    import errno

    from claude_session import gateway

    def refused(pid: int) -> int:
        raise OSError(errno.EPERM, "refused")

    monkeypatch.setattr(gateway, "pidfd_open", refused)
    pid, token = procs.live()
    assert stopper.deliver(pid, token, signal.SIGTERM) == stopper.SENT
    proc = next(p for p in procs.started if p.pid == pid)
    assert proc.wait(timeout=30) == -signal.SIGTERM


def test_esrch_from_pidfd_open_is_a_gone_process(
    monkeypatch: pytest.MonkeyPatch, procs: Procs
) -> None:
    import errno

    from claude_session import gateway

    def gone_(pid: int) -> int:
        raise OSError(errno.ESRCH, "gone")

    monkeypatch.setattr(gateway, "pidfd_open", gone_)
    pid, token = procs.live()
    assert stopper.deliver(pid, token, signal.SIGTERM) == stopper.GONE
    assert procs.running(pid)


def test_a_dead_pid_is_never_signaled(procs: Procs) -> None:
    pid, token = procs.dead()
    assert not stopper.send_verified(pid, token, signal.SIGTERM)


# End to end: a real exec and the fake claude


def test_stop_ends_a_real_headless_run_through_its_wrapper(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, state: Path, work: Path
) -> None:
    """The wrapper forwards stop's SIGTERM as pending_cause stopped and finalizes the run
    itself: exec exits 15, and stop reports what exec recorded (recorded_by_stop false)."""
    marker = tmp_path / "hanging"
    steps = [START, {"do": "touch", "path": str(marker)}, {"do": "hang"}]
    proc = start_hanging(tmp_path, work, steps, marker, config="kill_grace: 2s\n")
    try:
        wait_for(
            tmp_path / "fake.jsonl",
            lambda r: r["event"] == "step" and r["step"] == {"do": "hang"},
        )
        row = run_row_of(state)
        code, out = run_stop(capsys, tmp_path / "config.yaml", row["csession_id"])
        exec_out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()  # the exact process started here
            proc.wait()
    assert code == 0
    assert proc.returncode == 15, err
    assert sent(out) == [(proc.pid, "wrapper", "SIGTERM")]
    assert (out["outcome"], out["end_reason"], out["recorded_by_stop"]) == (
        "killed",
        "stopped",
        False,
    )
    assert json.loads(exec_out)["end_reason"] == "stopped"
    row = run_row_of(state)
    assert row["pending_cause"] == "stopped" and row["stop_requested_at"] is not None
    assert gone(row["child_pid"])


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


# End to end: start, adoption and stop


def stop_cli(env: Any, sid: str, *extra: str) -> tuple[int, dict[str, Any]]:
    proc = env.cs(["stop", sid, "--json", "--config", str(env.config), *extra])
    assert proc.stdout, proc.stderr
    out = json.loads(proc.stdout)
    validate(out, STOP_SCHEMA)
    return proc.returncode, out


def test_a_started_run_canceled_by_stop_refuses_adoption(
    tmp_path: Path, state: Path, work: Path
) -> None:
    from .test_start_adopt import Env

    env = Env(tmp_path, state, work, tmp_path / "config.yaml", tmp_path / "fake.jsonl")
    env.scenario([START, {"do": "hang"}])
    env.write_config()
    started = env.started()
    sid = started["csession_id"]
    spec = env.private_run(sid) / lifecycle.SPEC_NAME
    assert spec.exists()
    code, out = stop_cli(env, sid)
    assert (code, out["canceled"], out["end_reason"]) == (0, True, "canceled")
    assert not spec.exists()
    proc, refused = env.adopt(sid, "--run", "1")
    assert (proc.returncode, refused["error"]["reason"]) == (20, "expired")
    assert read_jsonl(env.log) == []  # claude never started


def test_stop_racing_adoption_has_a_single_winner(tmp_path: Path, state: Path, work: Path) -> None:
    """A real stop and a real adopting exec race one pending run: either stop cancels it (no
    signal; the exec is refused) or the exec adopts it and stop signals that exact wrapper,
    which ends the run canceled (launch window) or stopped (session phase)."""
    from .test_start_adopt import Env

    env = Env(tmp_path, state, work, tmp_path / "config.yaml", tmp_path / "fake.jsonl")
    env.scenario([START, {"do": "hang"}])
    env.write_config("kill_grace: 2s\n")
    sid = env.started()["csession_id"]
    adopter = subprocess.Popen(
        [*_cmd(), "exec", "--session-id", sid, "--run", "1", "--json"],
        cwd=work,
        env=env.environ(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        code, out = stop_cli(env, sid, "--grace", "5s")
        exec_out, err = adopter.communicate(timeout=60)
    finally:
        if adopter.poll() is None:
            adopter.kill()  # the exact process started here
            adopter.wait()
    assert code == 0 and out["ended"] is True, out
    if out["canceled"]:
        assert out["signals"] == []
        assert adopter.returncode == 20, exec_out + err
        assert json.loads(exec_out)["error"]["reason"] == "expired"
    else:
        assert sent(out)[0] == (adopter.pid, "wrapper", "SIGTERM")
        assert out["end_reason"] in ("canceled", "stopped"), out
        assert adopter.returncode == out["run_exit_code"], exec_out + err
    assert len(final_events_of(state, sid)) == 1


def _cmd() -> list[str]:
    from .test_exec_headless import claude_session_cmd

    return claude_session_cmd()


def final_events_of(state: Path, sid: str) -> list[tuple[Any, ...]]:
    import sqlite3

    db = sqlite3.connect(f"file:{state / 'state.db'}?mode=ro", uri=True)
    try:
        return db.execute(
            "SELECT run_no, cause FROM events WHERE csession_id = ? AND final = 1", (sid,)
        ).fetchall()
    finally:
        db.close()


def test_when_adoption_won_stop_signals_the_adopting_exec(
    tmp_path: Path, state: Path, work: Path
) -> None:
    from .test_start_adopt import Env

    env = Env(tmp_path, state, work, tmp_path / "config.yaml", tmp_path / "fake.jsonl")
    env.scenario([START, {"do": "hang"}])
    env.write_config("kill_grace: 2s\n")
    sid = env.started()["csession_id"]
    adopter = subprocess.Popen(
        [*_cmd(), "exec", "--session-id", sid, "--run", "1", "--json"],
        cwd=work,
        env=env.environ(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for(env.log, lambda r: r["event"] == "step" and r["step"] == {"do": "hang"})
        code, out = stop_cli(env, sid)
        exec_out, err = adopter.communicate(timeout=60)
    finally:
        if adopter.poll() is None:
            adopter.kill()  # the exact process started here
            adopter.wait()
    assert code == 0
    assert (out["canceled"], sent(out)) == (False, [(adopter.pid, "wrapper", "SIGTERM")])
    assert (out["outcome"], out["end_reason"], out["recorded_by_stop"]) == (
        "killed",
        "stopped",
        False,
    )
    assert adopter.returncode == 15, exec_out + err
    assert len(final_events_of(state, sid)) == 1


def test_the_human_output_is_one_sanitized_line(
    capsys: pytest.CaptureFixture[str], config: Path, st: Store, root: str
) -> None:
    sid = pending(st, root)
    capsys.readouterr()
    assert cli.main(["stop", sid, "--config", str(config)]) == 0
    out = capsys.readouterr().out
    assert out == f"{sid} run 1: canceled, not-started (canceled), exit 12\n"
