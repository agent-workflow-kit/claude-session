"""The gateway fake keeps the real gateway's contract, so later stages can rely on it."""

from __future__ import annotations

import signal
import sys

import pytest

from claude_session.gateway import DEVNULL, INHERIT, Gateway, GatewayTimeout, SystemGateway

from .fake_clock import FakeClock
from .fake_gateway import FAKE_PID_BASE, FakeChild, FakeGateway, Response


def test_satisfies_the_gateway_protocol() -> None:
    assert isinstance(FakeGateway(), Gateway)


def test_enforces_the_argv_contract() -> None:
    fake = FakeGateway()
    fake.on(["git"])
    with pytest.raises(ValueError):
        fake.run(["git", "status"])
    with pytest.raises(ValueError):
        fake.run(["/usr/bin/git"], env={}, extra_env={})


def test_unexpected_command_fails_loudly() -> None:
    with pytest.raises(AssertionError, match="unexpected command"):
        FakeGateway().run(["/usr/bin/git", "status"])


def test_records_calls_and_answers_by_prefix_latest_rule_first() -> None:
    fake = FakeGateway()
    fake.on(["/usr/bin/git"], returncode=1, stderr="default")
    fake.on(["/usr/bin/git", "rev-parse"], stdout="/repo/.git\n")
    hit = fake.run(["/usr/bin/git", "rev-parse", "--git-common-dir"], cwd="/repo", timeout=5)
    miss = fake.run(["/usr/bin/git", "status"], extra_env={"GIT_CONFIG_NOSYSTEM": "1"})
    assert (hit.returncode, hit.stdout) == (0, "/repo/.git\n")
    assert (miss.returncode, miss.stderr) == (1, "default")
    assert [c.argv[1] for c in fake.calls] == ["rev-parse", "status"]
    assert fake.calls[0].cwd == "/repo" and fake.calls[0].timeout == 5
    assert fake.calls[1].extra_env == {"GIT_CONFIG_NOSYSTEM": "1"}


def test_scripted_timeout_and_error() -> None:
    fake = FakeGateway()
    fake.on(["/usr/bin/memoctl"], timeout=True)
    fake.on(["/usr/bin/crontab"], error=FileNotFoundError("crontab"))
    with pytest.raises(GatewayTimeout):
        fake.run(["/usr/bin/memoctl", "context"], timeout=3)
    with pytest.raises(FileNotFoundError):
        fake.run(["/usr/bin/crontab", "-l"])


def test_scripted_spawn_returns_fake_children_with_impossible_pids() -> None:
    fake = FakeGateway()
    fake.on(["/opt/claude"], returncode=0)
    fake.on(["/opt/claude", "--hang"], timeout=True)
    done = fake.spawn(["/opt/claude", "-p"], new_process_group=True, die_with_parent=True)
    hung = fake.spawn(["/opt/claude", "--hang"])
    assert done.wait() == 0
    assert hung.poll() is None and hung.pid >= FAKE_PID_BASE and hung.pid != done.pid
    hung.send_signal(signal.SIGTERM)
    assert isinstance(hung, FakeChild) and hung.signals == [signal.SIGTERM]
    hung.exit(-signal.SIGTERM)
    assert hung.wait(1) == -signal.SIGTERM
    assert fake.calls[0].new_process_group and fake.calls[0].die_with_parent


def test_spawn_via_starts_the_real_program_and_records_it() -> None:
    fake = FakeGateway(spawn_via=SystemGateway())
    child = fake.spawn([sys.executable, "-I", "-c", "raise SystemExit(7)"], stdin=DEVNULL)
    assert child.wait(timeout=30) == 7
    assert fake.calls[0].kind == "spawn"


@pytest.mark.parametrize("argv", ["/usr/bin/git", b"/usr/bin/git"])
def test_refuses_str_or_bytes_argv(argv: object) -> None:
    fake = FakeGateway()
    fake.on(["/"])
    with pytest.raises(TypeError):
        fake.run(argv)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        fake.spawn(argv)  # type: ignore[arg-type]


def test_records_spawn_streams_separately() -> None:
    fake = FakeGateway()
    fake.on(["/opt/claude"])
    fake.spawn(["/opt/claude"], stdout=7, stderr=8)
    fake.spawn(["/opt/claude"], stdin=INHERIT)
    first, second = fake.calls
    assert (first.stdin, first.stdout, first.stderr) == (DEVNULL, 7, 8)
    assert (second.stdin, second.stdout, second.stderr) == (INHERIT, None, None)


def test_records_run_process_group_and_kill_grace() -> None:
    fake = FakeGateway()
    fake.on(["/bin/sh"])
    fake.run(["/bin/sh", "-c", "true"], new_process_group=True, kill_grace=10, timeout=30)
    (call,) = fake.calls
    assert (call.new_process_group, call.kill_grace, call.timeout) == (True, 10, 30)


def test_kill_grace_contract_matches_the_real_gateway() -> None:
    fake = FakeGateway()
    fake.on(["/bin/sh"])
    with pytest.raises(ValueError):
        fake.run(["/bin/sh"], kill_grace=1)
    with pytest.raises(ValueError):
        fake.run(["/bin/sh"], new_process_group=True, kill_grace=-1)


def test_a_response_list_is_consumed_in_order_and_the_last_repeats() -> None:
    fake = FakeGateway()
    ps = ["/bin/ps", "-p", "42"]
    fake.on(
        ps,
        responses=[
            Response(stdout="S Thu Oct 30 17:08:12 2026\n"),
            Response(returncode=1),
        ],
    )
    codes = [fake.run(ps).returncode for _ in range(4)]
    assert codes == [0, 1, 1, 1]


def test_a_response_list_scripts_spawned_children_too() -> None:
    fake = FakeGateway()
    fake.on(["/opt/claude"], responses=[Response(timeout=True), Response(returncode=3)])
    assert fake.spawn(["/opt/claude"]).poll() is None
    assert fake.spawn(["/opt/claude"]).poll() == 3


def test_responses_exclude_single_response_arguments() -> None:
    with pytest.raises(ValueError):
        FakeGateway().on(["/bin/ps"], returncode=1, responses=[Response()])
    with pytest.raises(ValueError):
        FakeGateway().on(["/bin/ps"], responses=[])


def test_waiting_on_a_running_child_advances_the_fake_clock() -> None:
    clock = FakeClock()
    fake = FakeGateway(clock=clock)
    fake.on(["/opt/claude"], timeout=True)
    child = fake.spawn(["/opt/claude"])
    start = clock.monotonic()
    assert child.wait(timeout=5) is None
    assert clock.monotonic() - start == 5
    assert isinstance(child, FakeChild)
    child.exit(0)
    assert child.wait(timeout=5) == 0
    assert clock.monotonic() - start == 5  # an exited child returns at once


def test_waiting_forever_on_a_running_child_fails_loudly() -> None:
    # The real wait(None) on a child that never exits would block forever.
    fake = FakeGateway()
    fake.on(["/opt/claude"], timeout=True)
    child = fake.spawn(["/opt/claude"])
    with pytest.raises(AssertionError):
        child.wait()
    assert isinstance(child, FakeChild)
    child.exit(0)
    assert child.wait() == 0


def test_a_scripted_run_timeout_advances_the_fake_clock() -> None:
    clock = FakeClock()
    fake = FakeGateway(clock=clock)
    fake.on(["/bin/sh"], timeout=True)
    start = clock.monotonic()
    with pytest.raises(GatewayTimeout):
        fake.run(["/bin/sh", "-c", "hook"], timeout=30, new_process_group=True, kill_grace=10)
    assert clock.monotonic() - start == 40
    with pytest.raises(GatewayTimeout):
        fake.run(["/bin/sh", "-c", "notes"], timeout=5, new_process_group=True)
    assert clock.monotonic() - start == 45


def test_signals_to_an_exited_child_are_not_recorded() -> None:
    fake = FakeGateway()
    fake.on(["/opt/claude"])
    child = fake.spawn(["/opt/claude"])
    child.send_signal(signal.SIGTERM)
    assert isinstance(child, FakeChild) and child.signals == []


def test_spawn_refuses_stdin_none_like_the_real_gateway() -> None:
    fake = FakeGateway()
    fake.on(["/opt/claude"])
    with pytest.raises(TypeError):
        fake.spawn(["/opt/claude"], stdin=None)  # type: ignore[arg-type]


def test_a_scripted_timeout_without_a_timeout_fails() -> None:
    # The real gateway cannot time out a run that has no timeout.
    fake = FakeGateway()
    fake.on(["/bin/sh"], timeout=True)
    with pytest.raises(AssertionError):
        fake.run(["/bin/sh", "-c", "hook"], new_process_group=True)


def test_max_output_matches_the_real_gateway() -> None:
    from claude_session.gateway import GatewayOutputLimit

    fake = FakeGateway()
    fake.on(["/bin/sh"], stdout="x" * 5, stderr="e" * 9)
    assert fake.run(["/bin/sh"], max_output=5).stderr == "e" * 5
    assert fake.calls[0].max_output == 5
    with pytest.raises(GatewayOutputLimit):
        fake.run(["/bin/sh"], max_output=4)
    with pytest.raises(ValueError):
        fake.run(["/bin/sh"], max_output=-1)
