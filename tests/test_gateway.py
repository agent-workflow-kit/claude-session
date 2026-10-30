"""The real subprocess gateway. Every test signals only PIDs it started itself."""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_session import gateway
from claude_session.gateway import (
    DEVNULL,
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
        ("", ""),
        ("/a:/b:/a", "/a:/b:/a"),
    ],
)
def test_sanitize_path_drops_empty_and_relative_entries(path: str, expected: str) -> None:
    assert sanitize_path(path) == expected


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


def test_run_without_stdin_gives_the_child_dev_null(gw: SystemGateway) -> None:
    # hook-event's own stdin is the hook payload; a child must never consume it.
    proc = gw.run(py("import sys; print(repr(sys.stdin.read()))"))
    assert proc.stdout == "''\n"


def test_run_uses_cwd(gw: SystemGateway, tmp_path: Path) -> None:
    proc = gw.run(py("import os; print(os.getcwd())"), cwd=str(tmp_path))
    assert proc.stdout.strip() == os.path.realpath(tmp_path)


def test_run_replaces_undecodable_output(gw: SystemGateway) -> None:
    proc = gw.run(py("import sys; sys.stdout.buffer.write(b'a\\xffb')"))
    assert proc.stdout == "a\ufffdb"


def test_run_timeout_raises_gateway_timeout(gw: SystemGateway) -> None:
    start = time.monotonic()
    with pytest.raises(GatewayTimeout) as info:
        gw.run(py("import time; time.sleep(30)"), timeout=0.5)
    assert time.monotonic() - start < 10
    assert info.value.argv[0] == PY
    assert info.value.timeout == 0.5


def test_run_missing_executable_raises_oserror(gw: SystemGateway, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        gw.run([str(tmp_path / "no-such-program")])


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


# --- PR_SET_PDEATHSIG --------------------------------------------------------------------

# An intermediate process spawns a long sleeper through the gateway, prints its PID and exits
# without waiting for it. The sleeper is this test's grandchild: started on its behalf, so the
# test may signal that exact PID (and only it) during cleanup.
INTERMEDIATE = """
import sys
from claude_session.gateway import DEVNULL, SystemGateway
child = SystemGateway().spawn(
    [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
    stdin=DEVNULL,
    stdout=DEVNULL,  # else it holds the captured pipe open and run() waits for it
    stderr=DEVNULL,
    die_with_parent=sys.argv[1] == "1",
)
print(child.pid, flush=True)
"""


@pytest.fixture
def grandchildren() -> Iterator[list[int]]:
    started: list[int] = []
    yield started
    for pid in started:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rpartition(")")[2].split()[0]
    except FileNotFoundError:
        return False
    return state not in ("Z", "X")


def _start_grandchild(gw: SystemGateway, die_with_parent: bool, started: list[int]) -> int:
    proc = gw.run(
        [PY, "-c", INTERMEDIATE, "1" if die_with_parent else "0"],
        extra_env={"PYTHONPATH": str(Path(gateway.__file__).parents[1])},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    pid = int(proc.stdout)
    started.append(pid)
    return pid


def _wait_dead(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@linux_only
def test_die_with_parent_ends_the_child_when_its_parent_exits(
    gw: SystemGateway, grandchildren: list[int]
) -> None:
    pid = _start_grandchild(gw, True, grandchildren)
    assert _wait_dead(pid, 10)


@linux_only
def test_without_die_with_parent_the_child_outlives_its_parent(
    gw: SystemGateway, grandchildren: list[int]
) -> None:
    pid = _start_grandchild(gw, False, grandchildren)
    assert not _wait_dead(pid, 0.5)
