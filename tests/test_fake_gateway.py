"""The gateway fake keeps the real gateway's contract, so later stages can rely on it."""

from __future__ import annotations

import signal
import sys

import pytest

from claude_session.gateway import DEVNULL, Gateway, GatewayTimeout, SystemGateway

from .fake_gateway import FAKE_PID_BASE, FakeChild, FakeGateway


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
