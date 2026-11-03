"""The real subprocess gateway. Every test signals only PIDs it started itself."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

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


# --- executable: run a resolved tool from its realpath with argv[0] = its bare name ---------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_executable_runs_the_given_path_with_argv0_as_given(gw: SystemGateway) -> None:
    script = "tr '\\0' ' ' < /proc/$$/cmdline"
    result = gw.run(["shim-name", "-c", script], executable="/bin/sh")
    assert result.stdout.split()[0] == "shim-name", result.stderr


def test_executable_argv0_reaches_a_shell_script(gw: SystemGateway, tmp_path: Path) -> None:
    script = tmp_path / "real-tool"
    script.write_text('#!/bin/sh\necho "$0"\n')
    script.chmod(0o755)
    # A #! script sees its own path as $0 (the kernel passes the script path), so check with
    # /bin/sh -c instead: $0 is the argument after the command string.
    result = gw.run(["shim-name", "-c", 'echo "$0"', "zero"], executable="/bin/sh")
    assert result.stdout == "zero\n"
    child = gw.spawn(["shim-name"], executable=str(script), stdout=DEVNULL)
    assert child.wait(10) == 0


@pytest.mark.parametrize("executable", ["sh", "./bin/sh", ""])
def test_executable_must_be_absolute(gw: SystemGateway, executable: str) -> None:
    with pytest.raises(ValueError):
        gw.run(["sh"], executable=executable)
    with pytest.raises(ValueError):
        gw.spawn(["sh"], executable=executable)


@pytest.mark.parametrize("argv0", ["", "a\x00b"])
def test_executable_needs_a_usable_argv0(gw: SystemGateway, argv0: str) -> None:
    with pytest.raises(ValueError):
        gw.run([argv0], executable="/bin/sh")


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


def test_unset_path_becomes_the_fixed_default(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PATH", raising=False)
    proc = gw.run(py("import os; print(os.environ.get('PATH'))"))
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


class Interrupted(Exception):
    """Raised by a test's signal handler."""


@contextlib.contextmanager
def _raising_alarms(
    first: float, interval: float = 0.0, exc: type[BaseException] = Interrupted
) -> Iterator[None]:
    """SIGALRM after ``first`` seconds (then every ``interval``), its handler raising ``exc``.
    The timer and the previous handler are restored even if an alarm lands while restoring."""

    def on_alarm(signum: int, frame: object) -> None:
        del signum, frame
        raise exc

    previous = signal.signal(signal.SIGALRM, on_alarm)
    try:
        signal.setitimer(signal.ITIMER_REAL, first, interval)
        yield
    finally:
        for _ in range(100):
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
                break
            except exc:
                continue


@linux_only
def test_exception_during_the_kill_grace_still_sigkills_and_reaps(
    gw: SystemGateway, tmp_path: Path, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    # Timeout at 0.3 s; the alarm at 0.8 s lands in the 5 s grace of a group that ignores
    # SIGTERM. The group still gets SIGKILL and the leader is reaped before it propagates.
    pidfile, leaderfile = tmp_path / "pid", tmp_path / "leader"
    script = (
        f"echo $$ > {leaderfile}; trap '' TERM; /bin/sleep 30 & echo $! > {pidfile}; "
        "while :; do /bin/sleep 0.1; done"
    )
    start = time.monotonic()
    with pytest.raises(Interrupted), _raising_alarms(0.8, 1.0):
        gw.run(["/bin/sh", "-c", script], new_process_group=True, timeout=0.3, kill_grace=5)
    leader = _read_pid(leaderfile, grandchildren)  # recorded for cleanup should this fail
    assert time.monotonic() - start < 3
    assert _wait_dead(_read_pid(pidfile, grandchildren), 5)
    assert not _alive(leader)


@linux_only
def test_second_exception_during_the_grace_of_an_interrupted_run_still_sigkills(
    gw: SystemGateway, tmp_path: Path, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    # The first alarm interrupts the run; the second lands in the grace that follows.
    pidfile, leaderfile = tmp_path / "pid", tmp_path / "leader"
    script = (
        f"echo $$ > {leaderfile}; trap '' TERM; /bin/sleep 30 & echo $! > {pidfile}; "
        "while :; do /bin/sleep 0.1; done"
    )
    start = time.monotonic()
    with pytest.raises(Interrupted), _raising_alarms(0.5, 0.5):
        gw.run(["/bin/sh", "-c", script], new_process_group=True, timeout=30, kill_grace=5)
    leader = _read_pid(leaderfile, grandchildren)  # recorded for cleanup should this fail
    assert time.monotonic() - start < 3
    assert _wait_dead(_read_pid(pidfile, grandchildren), 5)
    assert not _alive(leader)


@linux_only
def test_exception_before_the_sigkill_is_sent_still_ends_the_group(
    gw: SystemGateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A signal handler raising inside the SIGKILL step, before killpg, must not leave the
    # cleanup waiting forever for a leader that ignores SIGTERM. (Should it hang anyway, the
    # conftest watchdog reports it; nothing unblocks it.)
    real = gateway._signal_group  # pyright: ignore[reportPrivateUsage]
    raised: list[int] = []

    def interrupted_once(popen: subprocess.Popen[bytes], sig: int) -> bool:
        if sig == signal.SIGKILL and not raised:
            raised.append(popen.pid)
            raise Interrupted
        return real(popen, sig)

    monkeypatch.setattr(gateway, "_signal_group", interrupted_once)
    script = "trap '' TERM; while :; do /bin/sleep 0.1; done"
    start = time.monotonic()
    with pytest.raises(Interrupted):
        gw.run(["/bin/sh", "-c", script], new_process_group=True, timeout=0.3, kill_grace=0.2)
    assert time.monotonic() - start < 10
    (leader,) = raised
    assert not _alive(leader)


@linux_only
def test_leftover_guard_kills_a_term_ignoring_group_by_its_recorded_leader(
    gw: SystemGateway, tmp_path: Path, no_leftover_processes: list[Any]
) -> None:
    # The conftest guard that every test relies on: what it recorded at launch, it ends.
    pidfile = tmp_path / "pid"
    script = f"trap '' TERM; /bin/sleep 30 & echo $! > {pidfile}; while :; do /bin/sleep 0.1; done"
    child = gw.spawn(["/bin/sh", "-c", script], new_process_group=True)
    member = _read_pid(pidfile, [])
    (recorded,) = no_leftover_processes
    assert recorded.popen.pid == child.pid
    recorded.kill()
    assert child.wait(timeout=10) == -signal.SIGKILL
    assert _wait_dead(member, 5)


@linux_only
def test_keyboard_interrupt_ends_the_group_of_a_leader_that_already_exited(
    gw: SystemGateway, tmp_path: Path, grandchildren: list[tuple[int, bytes | None]]
) -> None:
    # The leader exits at once; a background member keeps stdout open. On KeyboardInterrupt
    # the leader must still be unreaped, so its group can be ended safely.
    pidfile = tmp_path / "pid"
    script = f"/bin/sleep 30 & echo $! > {pidfile}; exit 0"
    with pytest.raises(KeyboardInterrupt), _raising_alarms(0.5, exc=KeyboardInterrupt):
        gw.run(["/bin/sh", "-c", script], new_process_group=True, timeout=30, kill_grace=5)
    assert _wait_dead(_read_pid(pidfile, grandchildren), 5)


@linux_only
def test_run_waits_for_the_leader_after_its_output_closes(gw: SystemGateway) -> None:
    proc = gw.run(
        ["/bin/sh", "-c", "exec >&- 2>&-; /bin/sleep 0.3; exit 5"],
        new_process_group=True,
        timeout=30,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (5, "", "")


def test_run_timeout_counts_the_wait_after_output_closes(gw: SystemGateway) -> None:
    start = time.monotonic()
    with pytest.raises(GatewayTimeout):
        gw.run(["/bin/sh", "-c", "exec >&- 2>&-; exec /bin/sleep 30"], timeout=0.5)
    assert time.monotonic() - start < 10


def test_run_translates_newlines_like_text_mode(gw: SystemGateway) -> None:
    proc = gw.run(py("import sys; sys.stdout.buffer.write(b'a\\r\\nb\\rc\\n')"))
    assert proc.stdout == "a\nb\nc\n"


def test_run_with_empty_stdin_text_gives_eof(gw: SystemGateway) -> None:
    proc = gw.run(py("import sys; print(repr(sys.stdin.read()))"), stdin="")
    assert proc.stdout == "''\n"


def test_run_survives_a_child_that_does_not_read_its_stdin(gw: SystemGateway) -> None:
    proc = gw.run(["/bin/true"], stdin="x" * (1024 * 1024), timeout=30)
    assert proc.returncode == 0


# --- group scan and grace polling --------------------------------------------------------


def _stat(state: bytes, pgrp: int, threads: int) -> bytes:
    after = [state, b"1", str(pgrp).encode(), *([b"0"] * 14), str(threads).encode(), b"0", b"9"]
    return b"42 (a) b) " + b" ".join(after) + b"\n"


@pytest.mark.parametrize(
    ("stat", "live"),
    [
        (_stat(b"S", 77, 1), True),
        (_stat(b"R", 77, 3), True),
        (_stat(b"Z", 77, 3), True),  # main thread exited, other threads still run
        (_stat(b"Z", 77, 1), False),
        (_stat(b"X", 77, 1), False),
        (_stat(b"S", 78, 1), False),  # another group
        (b"42 (a) S", False),  # truncated
    ],
)
def test_group_member_classification(stat: bytes, live: bool) -> None:
    assert gateway._live_in_group(stat, 77) is live  # pyright: ignore[reportPrivateUsage]


class _FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _wait_with_scans(
    monkeypatch: pytest.MonkeyPatch, scans: list[bool], seconds: float
) -> tuple[bool, _FakeTime, list[bool]]:
    remaining = list(scans)
    monkeypatch.setattr(gateway, "_reaped_elsewhere", lambda pid: False)
    monkeypatch.setattr(
        gateway, "_group_has_live_member", lambda pgid: remaining.pop(0) if remaining else True
    )
    clock = _FakeTime()
    empty = gateway._wait_group_empty(  # pyright: ignore[reportPrivateUsage]
        123, seconds, sleep=clock.sleep, monotonic=clock.monotonic
    )
    return empty, clock, remaining


def test_group_counts_as_empty_only_after_two_empty_scans_in_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A process forked after a scan listed /proc may be missed once, not twice.
    empty, clock, left = _wait_with_scans(monkeypatch, [False, True, False, False, True], 10)
    assert empty
    assert left == [True]  # stopped after the second empty scan in a row
    assert clock.sleeps == [0.05]


def test_grace_poll_backs_off_and_stops_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    empty, clock, _ = _wait_with_scans(monkeypatch, [], 2.0)
    assert empty
    assert clock.sleeps == pytest.approx([0.05, 0.1, 0.2, 0.4, 0.5, 0.5, 0.25])
    assert clock.now == pytest.approx(2.0)


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
def test_die_with_parent_restores_the_mask_when_blocking_raises(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch, usr1_blocked: None
) -> None:
    # A pending handler can raise from the very call that blocked every signal.
    before = _sigmask()
    real = gateway._c_pthread_sigmask()  # pyright: ignore[reportPrivateUsage]

    def raising_after_blocking_all(how: int, mask: Iterable[int]) -> set[int]:
        mask = set(mask)
        old = real(how, mask)
        if how == signal.SIG_BLOCK and signal.SIGTERM in mask:
            raise Interrupted
        return old

    monkeypatch.setattr(gateway, "_c_pthread_sigmask", lambda: raising_after_blocking_all)
    with pytest.raises(Interrupted):
        gw.spawn(["/bin/true"], die_with_parent=True)
    monkeypatch.undo()
    assert _sigmask() == before


@linux_only
def test_die_with_parent_kills_and_reaps_the_child_when_restoring_the_mask_raises(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SIGUSR1 arrives while spawn blocks every signal; its handler raises once the mask is
    # restored. The child already started must not be left behind. The signal is sent to this
    # thread: sent to the process, another thread (the cleanup fixture's watchdog) could take it
    # and the handler could then run anywhere in spawn, not only once the mask is restored.
    started: list[subprocess.Popen[Any]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        popen: subprocess.Popen[Any] = real_popen(*args, **kwargs)
        started.append(popen)
        signal.pthread_kill(threading.get_ident(), signal.SIGUSR1)  # pending until restored
        return popen

    def on_usr1(signum: int, frame: object) -> None:
        del signum, frame
        raise Interrupted

    before = _sigmask()
    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    previous = signal.signal(signal.SIGUSR1, on_usr1)
    try:
        with pytest.raises(Interrupted):
            gw.spawn(["/bin/sleep", "30"], die_with_parent=True)
    finally:
        signal.signal(signal.SIGUSR1, previous)
        monkeypatch.undo()
        leftover = [p for p in started if p.returncode is None]
        for p in leftover:  # the exact child this test started
            p.kill()
            p.wait()
    assert len(started) == 1
    assert leftover == []
    assert started[0].returncode == -signal.SIGKILL
    assert _sigmask() == before


@linux_only
def test_die_with_parent_restores_the_mask_before_a_pending_handler_can_run(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A handler for a signal another thread took runs in this thread at the next Python
    # function entry, even with every signal blocked here: Python 3.13 runs it on entry to
    # signal.pthread_sigmask's Python wrapper, before the C call. Simulate that handler raising
    # in this process; the restore must not depend on the wrapper.
    parent = os.getpid()
    real = signal.pthread_sigmask

    def handler_runs_first(how: int, mask: Iterable[int]) -> object:
        if os.getpid() == parent:
            raise Interrupted
        return real(how, mask)

    before = _sigmask()
    monkeypatch.setattr(signal, "pthread_sigmask", handler_runs_first)
    try:
        child = gw.spawn(["/bin/true"], die_with_parent=True)
    finally:
        monkeypatch.undo()
        assert _sigmask() == before
    assert child.wait(timeout=30) == 0


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
def test_run_starts_its_child_with_every_signal_blocked_and_restores_both_masks(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch, usr1_blocked: None
) -> None:
    """R1-12: ``run`` builds its Popen with signals blocked (a handler raising in exec's
    launch phase then waits until run holds the child); the caller's mask is restored, and
    the child gets it (SIGUSR1 here) without the block, no death signal."""
    masks: list[set[int]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        masks.append(_sigmask())
        return real_popen(*args, **kwargs)

    before = _sigmask()
    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    result = gw.run(["/bin/cat", "/proc/self/status"])
    monkeypatch.undo()
    assert _sigmask() == before
    assert len(masks) == 1
    assert {signal.SIGINT, signal.SIGTERM, signal.SIGHUP} <= masks[0]
    status = dict(line.split(":", 1) for line in result.stdout.splitlines() if ":" in line)
    blocked = int(status["SigBlk"].strip(), 16)
    assert blocked == 1 << (signal.SIGUSR1 - 1)


@linux_only
def test_a_handler_raising_while_run_starts_its_child_ends_that_child(
    gw: SystemGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-12: a signal arriving inside Popen (sent to this thread, so it waits for the mask):
    its handler raises only once run holds the Popen, and run ends and reaps the child."""
    started: list[subprocess.Popen[Any]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        popen: subprocess.Popen[Any] = real_popen(*args, **kwargs)
        started.append(popen)
        signal.pthread_kill(threading.get_ident(), signal.SIGUSR1)  # pending until restored
        return popen

    def on_usr1(signum: int, frame: object) -> None:
        del signum, frame
        raise Interrupted

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    previous = signal.signal(signal.SIGUSR1, on_usr1)
    try:
        with pytest.raises(Interrupted):
            gw.run(["/bin/sleep", "30"])
    finally:
        signal.signal(signal.SIGUSR1, previous)
        monkeypatch.undo()
        leftover = [p for p in started if p.returncode is None]
        for p in leftover:  # the exact child this test started
            p.kill()
            p.wait()
    assert len(started) == 1
    assert leftover == []
    assert started[0].returncode == -signal.SIGKILL


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


@linux_only
@pytest.mark.parametrize("kill_group", [False, True])
def test_kill_group_on_exit_ends_descendants_left_by_a_leader_that_exited(
    kill_group: bool,
    gw: SystemGateway,
    tmp_path: Path,
    grandchildren: list[tuple[int, bytes | None]],
) -> None:
    """S7: a member that closed its output outlives a normal exit unless
    ``kill_group_on_exit`` SIGKILLs the group (before the leader is reaped)."""
    pidfile = tmp_path / "pid"
    script = f"/bin/sleep 30 >/dev/null 2>&1 </dev/null & echo $! > {pidfile}; exit 0"
    result = gw.run(
        ["/bin/sh", "-c", script],
        new_process_group=True,
        timeout=30,
        kill_group_on_exit=kill_group,
    )
    assert result.returncode == 0
    member = _read_pid(pidfile, grandchildren)
    assert _wait_dead(member, 5 if kill_group else 0.5) is kill_group


def test_kill_group_on_exit_needs_a_process_group(gw: SystemGateway) -> None:
    with pytest.raises(ValueError, match="kill_group_on_exit needs new_process_group"):
        gw.run(["/bin/true"], kill_group_on_exit=True)
