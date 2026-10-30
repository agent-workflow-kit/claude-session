"""Process start time and liveness. Every test signals only PIDs it started itself."""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_session.gateway import DEVNULL, SystemGateway
from claude_session.procinfo import ProcInfo, ProcInfoError, parse_proc_stat

from .fake_gateway import FakeGateway

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
PS = ("/bin/ps", "-o", "stat=", "-o", "lstart=", "-p")
BOOT_ID = "0b6c4a3e-5f2d-4c1b-9a8e-7d6f5e4c3b2a"


def stat_line(pid: int, comm: str, state: str = "S", starttime: int = 123456) -> str:
    # Fields 4..52 after comm and state; field 22 (index 19 after the comm) is starttime.
    rest = [str(i) for i in range(4, 53)]
    rest[22 - 4] = str(starttime)
    return f"{pid} ({comm}) {state} " + " ".join(rest) + "\n"


# --- /proc/<pid>/stat parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    "comm",
    ["sleep", "with space", "a) b (c", "))", "(", "x) S 1 2 3 (", "", "tab\there"],
)
def test_parse_proc_stat_handles_any_comm(comm: str) -> None:
    assert parse_proc_stat(stat_line(42, comm, "R", 987654)) == ("R", 987654)


def test_parse_proc_stat_reads_the_state() -> None:
    assert parse_proc_stat(stat_line(42, "z", "Z")) == ("Z", 123456)


@pytest.mark.parametrize(
    "text",
    ["", "42 (no close paren S 1 2", "42 (short) S 1 2 3", "42 (bad) S " + "x " * 60],
)
def test_parse_proc_stat_rejects_malformed_input(text: str) -> None:
    with pytest.raises(ProcInfoError):
        parse_proc_stat(text)


# --- Linux, against a fake /proc ---------------------------------------------------------


@pytest.fixture
def fake_proc(tmp_path: Path) -> Callable[..., ProcInfo]:
    (tmp_path / "sys/kernel/random").mkdir(parents=True)
    (tmp_path / "sys/kernel/random/boot_id").write_text(BOOT_ID + "\n")

    def make(**pids: str) -> ProcInfo:
        for pid, text in pids.items():
            (tmp_path / pid.lstrip("p")).mkdir(exist_ok=True)
            (tmp_path / pid.lstrip("p") / "stat").write_text(text)
        return ProcInfo(FakeGateway(), platform="linux", proc_root=str(tmp_path))

    return make


def test_linux_start_time_token_combines_boot_id_and_ticks(
    fake_proc: Callable[..., ProcInfo],
) -> None:
    info = fake_proc(p42=stat_line(42, "a) (b", starttime=555))
    assert info.start_time(42) == f"{BOOT_ID}:555"


def test_linux_missing_process_has_no_start_time(fake_proc: Callable[..., ProcInfo]) -> None:
    info = fake_proc()
    assert info.start_time(42) is None
    assert info.is_alive(42, f"{BOOT_ID}:555") is False


@pytest.mark.parametrize("state", ["Z", "X"])
def test_linux_zombie_or_dead_counts_as_gone(
    fake_proc: Callable[..., ProcInfo], state: str
) -> None:
    info = fake_proc(p42=stat_line(42, "claude", state, 555))
    assert info.start_time(42) is None
    assert info.is_alive(42, f"{BOOT_ID}:555") is False


def test_pid_reuse_same_pid_different_start_time_is_not_alive(
    fake_proc: Callable[..., ProcInfo],
) -> None:
    info = fake_proc(p42=stat_line(42, "claude", starttime=999))
    assert info.is_alive(42, f"{BOOT_ID}:999") is True
    assert info.is_alive(42, f"{BOOT_ID}:555") is False


def test_pid_reuse_across_a_reboot_is_not_alive(fake_proc: Callable[..., ProcInfo]) -> None:
    info = fake_proc(p42=stat_line(42, "claude", starttime=555))
    assert info.is_alive(42, "11111111-2222-3333-4444-555555555555:555") is False


def test_linux_without_boot_id_still_uses_ticks(tmp_path: Path) -> None:
    (tmp_path / "42").mkdir()
    (tmp_path / "42/stat").write_text(stat_line(42, "x", starttime=7))
    info = ProcInfo(FakeGateway(), platform="linux", proc_root=str(tmp_path))
    assert info.start_time(42) == ":7"


def test_linux_malformed_stat_is_an_error(fake_proc: Callable[..., ProcInfo]) -> None:
    info = fake_proc(p42="garbage")
    with pytest.raises(ProcInfoError):
        info.start_time(42)


@pytest.mark.parametrize("pid", [0, -1, -42])
def test_non_positive_pids_are_refused(fake_proc: Callable[..., ProcInfo], pid: int) -> None:
    # 0 and negative PIDs name process groups for kill(2); never treat them as one process.
    with pytest.raises(ValueError):
        fake_proc().start_time(pid)
    with pytest.raises(ValueError):
        fake_proc().is_alive(pid, "x")


def test_linux_never_calls_the_gateway(fake_proc: Callable[..., ProcInfo]) -> None:
    fake = FakeGateway()
    info = fake_proc(p42=stat_line(42, "x"))
    info._gateway = fake  # pyright: ignore[reportPrivateUsage]
    info.start_time(42)
    assert fake.calls == []


# --- Linux, real processes ---------------------------------------------------------------


@linux_only
def test_real_start_time_matches_when_the_process_started() -> None:
    gw = SystemGateway()
    before = time.time()
    child = gw.spawn([sys.executable, "-I", "-c", "import time; time.sleep(60)"], stdin=DEVNULL)
    after = time.time()
    try:
        token = ProcInfo(gw).start_time(child.pid)
        assert token is not None
        boot_id, ticks = token.rsplit(":", 1)
        assert boot_id == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        with open("/proc/stat") as fh:
            btime = next(int(line.split()[1]) for line in fh if line.startswith("btime "))
        started = btime + int(ticks) / os.sysconf("SC_CLK_TCK")
        assert before - 2 <= started <= after + 2
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)


@linux_only
def test_real_process_is_alive_and_stable() -> None:
    info = ProcInfo(SystemGateway())
    token = info.start_time(os.getpid())
    assert token is not None
    assert info.start_time(os.getpid()) == token
    assert info.is_alive(os.getpid(), token)
    assert not info.is_alive(os.getpid(), token + "0")


@linux_only
def test_real_zombie_then_reaped_child_is_not_alive() -> None:
    gw = SystemGateway()
    info = ProcInfo(gw)
    child = gw.spawn([sys.executable, "-I", "-c", "import time; time.sleep(60)"], stdin=DEVNULL)
    token = info.start_time(child.pid)
    assert token is not None and info.is_alive(child.pid, token)
    child.send_signal(signal.SIGKILL)
    # Not reaped yet: a zombie, which liveness treats as dead.
    deadline = time.monotonic() + 10
    while info.start_time(child.pid) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not info.is_alive(child.pid, token)
    child.wait(timeout=30)
    assert info.start_time(child.pid) is None


# --- macOS path through the gateway fake -------------------------------------------------


def mac(fake: FakeGateway) -> ProcInfo:
    return ProcInfo(fake, platform="darwin")


def test_macos_reads_lstart_through_ps_with_a_fixed_locale_and_zone() -> None:
    fake = FakeGateway()
    fake.on([*PS, "42"], stdout="Ss   Thu Oct 30 17:08:12 2026\n")
    assert mac(fake).start_time(42) == "Thu Oct 30 17:08:12 2026"
    (call,) = fake.calls
    assert call.argv == (*PS, "42")
    assert call.extra_env == {"LC_ALL": "C", "TZ": "UTC"}
    assert call.timeout is not None


def test_macos_normalizes_lstart_padding() -> None:
    fake = FakeGateway()
    fake.on([*PS, "42"], stdout="  S  Mon Nov  3 09:01:02 2026  \n")
    assert mac(fake).start_time(42) == "Mon Nov 3 09:01:02 2026"


def test_macos_missing_process() -> None:
    fake = FakeGateway()
    fake.on([*PS, "42"], returncode=1, stdout="")
    info = mac(fake)
    assert info.start_time(42) is None
    assert info.is_alive(42, "Thu Oct 30 17:08:12 2026") is False


def test_macos_zombie_counts_as_gone() -> None:
    fake = FakeGateway()
    fake.on([*PS, "42"], stdout="Z    Thu Oct 30 17:08:12 2026\n")
    assert mac(fake).is_alive(42, "Thu Oct 30 17:08:12 2026") is False


def test_macos_pid_reuse_is_not_alive() -> None:
    fake = FakeGateway()
    fake.on([*PS, "42"], stdout="S    Thu Oct 30 17:08:12 2026\n")
    info = mac(fake)
    assert info.is_alive(42, "Thu Oct 30 17:08:12 2026") is True
    assert info.is_alive(42, "Thu Oct 30 16:00:00 2026") is False


@pytest.mark.parametrize("stdout", ["S\n", "S Thu\n", "\n"])
def test_macos_unparsable_ps_output_is_an_error(stdout: str) -> None:
    fake = FakeGateway()
    fake.on([*PS, "42"], stdout=stdout)
    with pytest.raises(ProcInfoError):
        mac(fake).start_time(42)


def test_macos_ps_failure_with_output_is_an_error() -> None:
    fake = FakeGateway()
    fake.on([*PS, "42"], returncode=2, stdout="", stderr="ps: illegal option\n")
    with pytest.raises(ProcInfoError):
        mac(fake).start_time(42)


def test_is_alive_has_the_store_checker_shape() -> None:
    # store takes an injectable liveness checker `(pid, start_time) -> bool`.
    checker: Callable[[int, str], bool] = ProcInfo(FakeGateway(), platform="linux").is_alive
    assert callable(checker)
