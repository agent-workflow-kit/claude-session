"""The real subprocess gateway. Every test signals only PIDs it started itself."""

from __future__ import annotations

import dataclasses
import functools
import os
import signal
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from claude_session import gateway
from claude_session.gateway import (
    DEVNULL,
    INHERIT,
    PARENT_GONE_EXIT,
    Gateway,
    GatewayTimeout,
    SystemGateway,
    check_argv,
    sanitize_path,
)

PY = sys.executable
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


@pytest.fixture
def gw() -> SystemGateway:
    return SystemGateway()


def py(code: str) -> list[str]:
    return [PY, "-I", "-c", textwrap.dedent(code)]


# --- argv and environment contract -------------------------------------------------------


@pytest.mark.parametrize("argv", [[], ["python3"], ["./bin/claude"], [""]])
def test_argv_must_start_with_an_absolute_path(gw: SystemGateway, argv: list[str]) -> None:
    with pytest.raises(ValueError):
        gw.run(argv)
    with pytest.raises(ValueError):
        gw.spawn(argv)


@pytest.mark.parametrize("argv", ["/bin/echo", b"/bin/echo"])
def test_str_or_bytes_argv_is_refused(gw: SystemGateway, argv: object) -> None:
    with pytest.raises(TypeError):
        gw.run(argv)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        gw.spawn(argv)  # type: ignore[arg-type]


def test_refusal_names_only_the_program_never_its_arguments(gw: SystemGateway) -> None:
    with pytest.raises(ValueError) as info:
        gw.run(["curl", "--header", "Authorization: Bearer s3cr3t-token"])
    assert "curl" in str(info.value)
    assert "s3cr3t-token" not in str(info.value)


def test_env_and_extra_env_are_exclusive() -> None:
    with pytest.raises(ValueError):
        check_argv([PY], env={"A": "1"}, extra_env={"B": "2"})


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/usr/bin:/bin", "/usr/bin:/bin"),
        ("/usr/bin::/bin", "/usr/bin:/bin"),
        (":/usr/bin:", "/usr/bin"),
        ("bin:./x:/usr/bin:../y", "/usr/bin"),
        ("/a:/b:/a", "/a:/b:/a"),
    ],
)
def test_sanitize_path_drops_empty_and_relative_entries(path: str, expected: str) -> None:
    assert sanitize_path(path) == expected


@pytest.mark.parametrize("path", ["", ":", "rel:foo", "./bin::."])
def test_sanitize_path_with_no_absolute_entry_falls_back_to_a_fixed_default(path: str) -> None:
    # An empty PATH would make some programs search the current directory.
    assert sanitize_path(path) == "/usr/bin:/bin"


@pytest.mark.parametrize("path", ["rel:foo", ""])
def test_inherited_path_with_no_absolute_entry_becomes_the_fixed_default(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv("PATH", path)
    proc = gw.run(py("import os; print(os.environ['PATH'])"))
    assert proc.stdout.strip() == "/usr/bin:/bin"


def test_extra_env_path_with_no_absolute_entry_becomes_the_fixed_default(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    proc = gw.run(py("import os; print(os.environ['PATH'])"), extra_env={"PATH": "rel"})
    assert proc.stdout.strip() == "/usr/bin:/bin"


def test_default_environment_is_inherited_with_a_sanitized_path(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "relative/bin::/usr/bin:.:/bin")
    monkeypatch.setenv("CSESSION_TEST_MARK", "inherited")
    proc = gw.run(
        py("import os; print(os.environ['PATH']); print(os.environ['CSESSION_TEST_MARK'])")
    )
    assert proc.stdout.splitlines() == ["/usr/bin:/bin", "inherited"]


def test_extra_env_is_added_to_the_sanitized_default(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "x:/usr/bin")
    proc = gw.run(
        py("import os; print(os.environ['PATH']); print(os.environ['GIT_CONFIG_NOSYSTEM'])"),
        extra_env={"GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert proc.stdout.splitlines() == ["/usr/bin", "1"]


def test_extra_env_path_is_sanitized_too(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    proc = gw.run(
        py("import os; print(os.environ['PATH'])"),
        extra_env={"PATH": "rel/bin::/bin:."},
    )
    assert proc.stdout.strip() == "/bin"


def test_explicit_env_is_passed_unchanged(gw: SystemGateway) -> None:
    # A caller-built environment (session or hook env) already follows the PATH rules.
    proc = gw.run(
        py("import os; print(sorted(k for k in os.environ if k != 'LC_CTYPE'))"),
        env={"ONLY": "this", "PATH": "rel:/usr/bin"},
    )
    assert proc.stdout.strip() == "['ONLY', 'PATH']"


# --- run ---------------------------------------------------------------------------------


def test_run_captures_exit_code_stdout_and_stderr(gw: SystemGateway) -> None:
    proc = gw.run(py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"))
    assert (proc.returncode, proc.stdout, proc.stderr) == (3, "out\n", "err\n")
    assert proc.argv[0] == PY


def test_run_feeds_stdin(gw: SystemGateway) -> None:
    proc = gw.run(py("import sys; print(sys.stdin.read().upper())"), stdin="hello")
    assert proc.stdout == "HELLO\n"


@pytest.fixture
def stdin_with_data() -> Iterator[None]:
    """Our fd 0 is a pipe holding a payload, as for hook-event; restored afterwards."""
    saved = os.dup(0)
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, b"PAYLOAD")
        os.close(write_end)
        os.dup2(read_end, 0)
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_end)


def test_run_without_stdin_gives_the_child_dev_null(
    gw: SystemGateway, stdin_with_data: None
) -> None:
    # hook-event's own stdin is the hook payload; a child must never consume it.
    proc = gw.run(py("import sys; print(repr(sys.stdin.read()))"))
    assert proc.stdout == "''\n"


def test_run_uses_cwd(gw: SystemGateway, tmp_path: Path) -> None:
    proc = gw.run(py("import os; print(os.getcwd())"), cwd=str(tmp_path))
    assert proc.stdout.strip() == os.path.realpath(tmp_path)


def test_run_replaces_undecodable_output(gw: SystemGateway) -> None:
    proc = gw.run(py("import sys; sys.stdout.buffer.write(b'a\\xffb')"))
    assert proc.stdout == "a\ufffdb"


def test_run_handles_large_output_on_both_streams_with_input(gw: SystemGateway) -> None:
    size = 3 * 1024 * 1024
    proc = gw.run(
        py(
            f"""
            import sys
            data = sys.stdin.read()
            sys.stdout.write("o" * {size})
            sys.stderr.write("e" * {size})
            sys.stdout.write(str(len(data)))
            """
        ),
        stdin="i" * size,
        timeout=60,
    )
    assert proc.returncode == 0
    assert proc.stdout == "o" * size + str(size)
    assert proc.stderr == "e" * size


def test_run_timeout_raises_gateway_timeout_and_reaps_the_child(
    gw: SystemGateway, tmp_path: Path
) -> None:
    pidfile = tmp_path / "pid"
    start = time.monotonic()
    with pytest.raises(GatewayTimeout) as info:
        gw.run(
            py(
                f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(30)"
            ),
            timeout=1,
        )
    assert time.monotonic() - start < 10
    assert info.value.argv[0] == PY
    assert info.value.timeout == 1
    if pidfile.exists():  # else it never got that far, and is dead all the same
        assert not os.path.exists(f"/proc/{pidfile.read_text()}") or not sys.platform.startswith(
            "linux"
        )


def test_run_missing_executable_raises_oserror(gw: SystemGateway, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        gw.run([str(tmp_path / "no-such-program")])


@pytest.mark.parametrize("kill_grace", [-1.0])
def test_negative_kill_grace_is_refused(gw: SystemGateway, kill_grace: float) -> None:
    with pytest.raises(ValueError):
        gw.run([PY], new_process_group=True, kill_grace=kill_grace)


def test_kill_grace_needs_a_process_group(gw: SystemGateway) -> None:
    with pytest.raises(ValueError):
        gw.run([PY], kill_grace=1.0)


# --- spawn -------------------------------------------------------------------------------


def test_spawn_writes_to_the_given_descriptors_and_reports_exit(
    gw: SystemGateway, tmp_path: Path
) -> None:
    out = tmp_path / "result.json"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        child = gw.spawn(py("print('line'); raise SystemExit(4)"), stdin=DEVNULL, stdout=fd)
    finally:
        os.close(fd)
    assert child.wait(timeout=30) == 4
    assert child.poll() == 4
    assert out.read_text() == "line\n"


def test_spawn_gives_the_child_dev_null_stdin_by_default(
    gw: SystemGateway, tmp_path: Path, stdin_with_data: None
) -> None:
    out = tmp_path / "out"
    with open(out, "wb") as fh:
        child = gw.spawn(["/bin/cat"], stdout=fh.fileno())
        assert child.wait(timeout=30) == 0
    assert out.read_bytes() == b""


def test_spawn_inherit_shares_our_stdin(
    gw: SystemGateway, tmp_path: Path, stdin_with_data: None
) -> None:
    out = tmp_path / "out"
    with open(out, "wb") as fh:
        child = gw.spawn(["/bin/cat"], stdin=INHERIT, stdout=fh.fileno())
        assert child.wait(timeout=30) == 0
    assert out.read_bytes() == b"PAYLOAD"


def test_spawn_refuses_stdin_none(gw: SystemGateway) -> None:
    # None would silently inherit our stdin (the hook payload); INHERIT says so explicitly.
    with pytest.raises(TypeError):
        gw.spawn(["/bin/true"], stdin=None)  # type: ignore[arg-type]


def test_spawn_wait_times_out_with_none_and_signal_ends_the_child(gw: SystemGateway) -> None:
    child = gw.spawn(py("import time; time.sleep(60)"), stdin=DEVNULL)
    try:
        assert child.wait(timeout=0.2) is None
        assert child.poll() is None
    finally:
        child.send_signal(signal.SIGTERM)
    assert child.wait(timeout=30) == -signal.SIGTERM


def test_spawn_signal_after_exit_is_a_no_op(gw: SystemGateway) -> None:
    child = gw.spawn(py("pass"), stdin=DEVNULL)
    assert child.wait(timeout=30) == 0
    child.send_signal(signal.SIGTERM)  # reaped: must not signal a reused PID


def test_spawn_new_process_group(gw: SystemGateway, tmp_path: Path) -> None:
    out = tmp_path / "pgid"
    with open(out, "w") as fh:
        own = gw.spawn(py("import os; print(os.getpgrp())"), stdout=fh.fileno(), stdin=DEVNULL)
        assert own.wait(timeout=30) == 0
        grouped = gw.spawn(
            py("import os; print(os.getpgrp())"),
            stdout=fh.fileno(),
            stdin=DEVNULL,
            new_process_group=True,
        )
        assert grouped.wait(timeout=30) == 0
    inherited, separate = (int(x) for x in out.read_text().split())
    assert inherited == os.getpgrp()
    assert separate == grouped.pid


def test_system_gateway_satisfies_the_protocol(gw: SystemGateway) -> None:
    assert isinstance(gw, Gateway)


def test_module_has_no_module_level_call_functions() -> None:
    # Callers get a Gateway object injected; there is no global to patch.
    assert not hasattr(gateway, "run")
    assert not hasattr(gateway, "spawn")


# --- process groups on timeout -----------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rpartition(b")")[2].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state not in (b"Z", b"X")


def _start_token(pid: int) -> bytes | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().rpartition(b")")[2].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


@pytest.fixture
def grandchildren() -> Iterator[list[tuple[int, bytes | None]]]:
    """PIDs started on this test's behalf, each with its start ticks: cleanup SIGKILLs a PID
    only while it still names that same process."""
    started: list[tuple[int, bytes | None]] = []
    yield started
    for pid, token in started:
        if token is not None and _start_token(pid) == token:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _wait_dead(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _read_pid(path: Path, started: list[tuple[int, bytes | None]]) -> int:
    deadline = time.monotonic() + 10
    while not path.exists() or not path.read_text().strip():
        assert time.monotonic() < deadline, "the child never wrote its PID"
        time.sleep(0.02)
    pid = int(path.read_text())
    started.append((pid, _start_token(pid)))
    return pid


@linux_only
def test_timeout_with_a_process_group_kills_grandchildren(
    gw: SystemGateway, tmp_path: Path, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    pidfile = tmp_path / "pid"
    with pytest.raises(GatewayTimeout):
        gw.run(
            ["/bin/sh", "-c", f"/bin/sleep 30 & echo $! > {pidfile}; wait"],
            new_process_group=True,
            timeout=1,
        )
    pid = _read_pid(pidfile, grandchildren)
    assert _wait_dead(pid, 5)


@linux_only
def test_timeout_without_a_process_group_kills_only_the_direct_child(
    gw: SystemGateway, tmp_path: Path, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    # The documented guarantee: direct child only (its descendants may survive).
    pidfile = tmp_path / "pid"
    with pytest.raises(GatewayTimeout):
        gw.run(
            ["/bin/sh", "-c", f"/bin/sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}; wait"],
            timeout=1,
        )
    pid = _read_pid(pidfile, grandchildren)
    assert _alive(pid)


@linux_only
def test_timeout_kill_grace_outlasts_the_group_leader(gw: SystemGateway, tmp_path: Path) -> None:
    # The leader (sh) dies on SIGTERM at once; the worker it waits for needs ~0.5 s to clean
    # up. The grace belongs to the whole group, not to the leader.
    marker = tmp_path / "cleaned"
    worker = tmp_path / "worker.sh"
    worker.write_text(
        f"trap '/bin/sleep 0.5; echo cleaned > {marker}; exit 0' TERM\n"
        "while :; do /bin/sleep 0.1; done\n"
    )
    start = time.monotonic()
    with pytest.raises(GatewayTimeout):
        gw.run(
            ["/bin/sh", "-c", f"/bin/sh {worker}; echo after"],
            new_process_group=True,
            timeout=1,
            kill_grace=3,
        )
    elapsed = time.monotonic() - start
    assert marker.read_text() == "cleaned\n"
    assert elapsed < 1 + 3  # returned once the group was empty, before the grace ran out


@linux_only
def test_timeout_kill_grace_ends_early_once_the_group_is_empty(
    gw: SystemGateway, tmp_path: Path, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    marker = tmp_path / "term"
    pidfile = tmp_path / "pid"
    script = f"trap 'echo term > {marker}; exit 0' TERM; /bin/sleep 30 & echo $! > {pidfile}; wait"
    start = time.monotonic()
    with pytest.raises(GatewayTimeout):
        gw.run(["/bin/sh", "-c", script], new_process_group=True, timeout=1, kill_grace=20)
    assert time.monotonic() - start < 10  # did not sit out the whole grace
    assert marker.read_text() == "term\n"
    assert _wait_dead(_read_pid(pidfile, grandchildren), 5)


@linux_only
def test_timeout_sigkills_a_group_that_ignores_sigterm_after_the_grace(
    gw: SystemGateway, tmp_path: Path, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    pidfile = tmp_path / "pid"
    # The ignored SIGTERM is inherited by every sleep, so nothing in the group ends on it.
    script = f"echo $$ > {pidfile}; trap '' TERM; while :; do /bin/sleep 0.1; done"
    start = time.monotonic()
    with pytest.raises(GatewayTimeout):
        gw.run(["/bin/sh", "-c", script], new_process_group=True, timeout=0.5, kill_grace=0.5)
    elapsed = time.monotonic() - start
    assert 0.9 <= elapsed < 0.5 + 0.5 + 3
    assert _wait_dead(_read_pid(pidfile, grandchildren), 5)


@linux_only
def test_interrupted_grouped_run_gives_the_group_its_grace(
    gw: SystemGateway, tmp_path: Path
) -> None:
    # An exception escaping communicate (here a signal handler raising) ends the group the
    # same way a timeout does: SIGTERM, grace, SIGKILL. Then the exception propagates.
    class Interrupted(Exception):
        pass

    def on_alarm(signum: int, frame: object) -> None:
        del signum, frame
        raise Interrupted

    marker = tmp_path / "term"
    script = f"trap 'echo term > {marker}; exit 0' TERM; /bin/sleep 30 & wait"
    previous = signal.signal(signal.SIGALRM, on_alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.5)
        with pytest.raises(Interrupted):
            gw.run(["/bin/sh", "-c", script], new_process_group=True, timeout=30, kill_grace=5)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert marker.read_text() == "term\n"


@linux_only
def test_leader_reaped_elsewhere_stops_all_group_signals(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the leader was reaped by someone else, its PID (the group ID) may be reused: the
    # group must not be signaled any more.
    def reaped_elsewhere(*args: object) -> object:
        del args
        raise ChildProcessError

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "waitid", reaped_elsewhere)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    with pytest.raises(GatewayTimeout):
        gw.run(["/bin/sleep", "1"], new_process_group=True, timeout=0.3, kill_grace=5)
    assert sent == []


@linux_only
def test_timeout_without_kill_grace_goes_straight_to_sigkill(
    gw: SystemGateway, tmp_path: Path
) -> None:
    marker = tmp_path / "term"
    script = f"trap 'echo term > {marker}; exit 0' TERM; /bin/sleep 30 & wait"
    with pytest.raises(GatewayTimeout):
        gw.run(["/bin/sh", "-c", script], new_process_group=True, timeout=1)
    assert not marker.exists()


# --- PR_SET_PDEATHSIG --------------------------------------------------------------------

# An intermediate process spawns a long sleeper through the gateway, prints its PID and exits
# without waiting for it. The sleeper is this test's grandchild: started on its behalf, so the
# test may signal that exact PID (and only it) during cleanup.
INTERMEDIATE = """
import signal, sys
from claude_session.gateway import DEVNULL, SystemGateway
if sys.argv[2] == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = SystemGateway().spawn(
    ["/bin/sleep", "60"],
    stdout=DEVNULL,  # else it holds the captured pipe open and run() waits for it
    stderr=DEVNULL,
    die_with_parent=sys.argv[1] == "1",
)
print(child.pid, flush=True)
"""


def _start_grandchild(
    gw: SystemGateway,
    die_with_parent: bool,
    started: list[tuple[int, bytes | None]],
    *,
    ignore_term: bool = False,
) -> int:
    proc = gw.run(
        [PY, "-c", INTERMEDIATE, "1" if die_with_parent else "0", "1" if ignore_term else "0"],
        extra_env={"PYTHONPATH": str(Path(gateway.__file__).parents[1])},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    pid = int(proc.stdout)
    started.append((pid, _start_token(pid)))
    return pid


@linux_only
def test_die_with_parent_ends_the_child_when_its_parent_exits(
    gw: SystemGateway, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    pid = _start_grandchild(gw, True, grandchildren)
    assert _wait_dead(pid, 10)


@linux_only
def test_die_with_parent_works_when_the_parent_ignores_sigterm(
    gw: SystemGateway, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    # SIG_IGN survives exec; the child must reset SIGTERM or the death signal is lost.
    pid = _start_grandchild(gw, True, grandchildren, ignore_term=True)
    assert _wait_dead(pid, 10)


@linux_only
def test_without_die_with_parent_the_child_outlives_its_parent(
    gw: SystemGateway, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    pid = _start_grandchild(gw, False, grandchildren)
    assert not _wait_dead(pid, 0.5)


@linux_only
def test_child_whose_parent_already_died_exits_without_exec(
    gw: SystemGateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pretend the forking process was another one: the child sees a different parent, as it
    # would had its real parent died before PR_SET_PDEATHSIG was set.
    real: Callable[..., object] = gateway._pdeathsig_preexec  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(gateway, "_pdeathsig_preexec", functools.partial(real, parent=1))
    marker = tmp_path / "ran"
    child = gw.spawn(py(f"open({str(marker)!r}, 'w').close()"), die_with_parent=True)
    assert child.wait(timeout=30) == PARENT_GONE_EXIT
    assert not marker.exists()


def _sigmask() -> set[int]:
    return set(signal.pthread_sigmask(signal.SIG_BLOCK, []))


@pytest.fixture
def usr1_blocked() -> Iterator[None]:
    """SIGUSR1 blocked in this thread for the test; the mask is restored afterwards."""
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


@linux_only
def test_die_with_parent_restores_the_callers_signal_mask(
    gw: SystemGateway, tmp_path: Path, usr1_blocked: None
) -> None:
    before = _sigmask()
    child = gw.spawn(["/bin/true"], die_with_parent=True)
    assert _sigmask() == before
    assert child.wait(timeout=30) == 0
    with pytest.raises(FileNotFoundError):
        gw.spawn([str(tmp_path / "missing")], die_with_parent=True)
    assert _sigmask() == before


@linux_only
def test_die_with_parent_child_keeps_our_mask_except_sigterm(
    gw: SystemGateway, tmp_path: Path, usr1_blocked: None
) -> None:
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})  # restored by the fixture
    out = tmp_path / "status"
    with open(out, "wb") as fh:
        child = gw.spawn(
            ["/bin/cat", "/proc/self/status"], stdout=fh.fileno(), die_with_parent=True
        )
        assert child.wait(timeout=30) == 0
    (line,) = (x for x in out.read_text().splitlines() if x.startswith("SigBlk:"))
    blocked = int(line.split()[1], 16)
    assert blocked & (1 << (signal.SIGUSR1 - 1))
    assert not blocked & (1 << (signal.SIGTERM - 1))


@linux_only
def test_signal_between_fork_and_exec_never_runs_our_python_handler(
    gw: SystemGateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SIGHUP reaches the child while preexec_fn runs (sent by the child to itself, as the
    # first libc call there). Our Python SIGHUP handler must not run in the child: the child
    # gets the default action instead (dies of SIGHUP) and never execs.
    handled = tmp_path / "handled"
    ran = tmp_path / "ran"
    real_libc = gateway._libc  # pyright: ignore[reportPrivateUsage]

    def libc_sending_sighup() -> object:
        libc = real_libc()

        def signal_after_sighup(*args: object) -> object:
            os.kill(os.getpid(), signal.SIGHUP)  # os.getpid() is the forked child here
            return libc.signal(*args)

        return dataclasses.replace(libc, signal=signal_after_sighup)

    monkeypatch.setattr(gateway, "_libc", libc_sending_sighup)
    previous = signal.signal(signal.SIGHUP, lambda *_: handled.touch())
    try:
        child = gw.spawn(py(f"open({str(ran)!r}, 'w').close()"), die_with_parent=True)
        code = child.wait(timeout=30)
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert not handled.exists()
    assert code == -signal.SIGHUP
    assert not ran.exists()


@linux_only
def test_prctl_failure_surfaces_as_oserror(
    gw: SystemGateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_prctl(*args: object) -> int:
        del args
        return -1

    monkeypatch.setattr(gateway, "_prctl", lambda: failing_prctl)
    marker = tmp_path / "ran"
    with pytest.raises(OSError):
        gw.spawn(py(f"open({str(marker)!r}, 'w').close()"), die_with_parent=True)
    assert not marker.exists()
