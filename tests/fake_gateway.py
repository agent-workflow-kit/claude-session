"""A scripted stand-in for ``claude_session.gateway.SystemGateway``.

Every later stage injects this where production code takes a ``Gateway``: register expected
commands with ``on()``, run the code under test, then inspect ``calls``. An unexpected command
fails loudly. ``spawn`` returns a scripted ``FakeChild`` unless the fake was built with
``spawn_via`` (normally a ``SystemGateway``), which really starts the program (for example the
fake ``claude`` in ``tests/fakebin``) and still records the call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from claude_session.gateway import Child, Gateway, GatewayTimeout, Result, check_argv

# Above the largest Linux pid_max (2**22), so a fake PID can never name a real process.
FAKE_PID_BASE = 2**22 + 1


@dataclass(frozen=True)
class Call:
    """One recorded gateway call."""

    kind: str  # "run" or "spawn"
    argv: tuple[str, ...]
    env: Mapping[str, str] | None = None
    extra_env: Mapping[str, str] | None = None
    cwd: str | None = None
    stdin: str | int | None = None
    timeout: float | None = None
    new_process_group: bool = False
    die_with_parent: bool = False


@dataclass
class _Rule:
    prefix: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timeout: bool
    error: OSError | None


@dataclass
class FakeChild:
    """A scripted child: ``poll``/``wait`` report ``returncode`` once ``exit()`` was called (or
    at once when built with one); signals are recorded, never sent."""

    pid: int
    returncode: int | None = None
    signals: list[int] = field(default_factory=list)

    def exit(self, returncode: int) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        del timeout
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)


class FakeGateway:
    """Records calls and answers them from rules registered with ``on()``."""

    def __init__(self, *, spawn_via: Gateway | None = None) -> None:
        self.calls: list[Call] = []
        self.children: list[FakeChild] = []
        self._rules: list[_Rule] = []
        self._spawn_via = spawn_via

    def on(
        self,
        prefix: Sequence[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timeout: bool = False,
        error: OSError | None = None,
    ) -> None:
        """Answer commands whose argv starts with ``prefix``. The most recent matching rule wins,
        so a test can override a fixture's default."""
        self._rules.append(_Rule(tuple(prefix), returncode, stdout, stderr, timeout, error))

    def _match(self, argv: tuple[str, ...]) -> _Rule:
        for rule in reversed(self._rules):
            if argv[: len(rule.prefix)] == rule.prefix:
                return rule
        raise AssertionError(f"FakeGateway: unexpected command {list(argv)}")

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        check_argv(argv, env=env, extra_env=extra_env)
        args = tuple(argv)
        self.calls.append(Call("run", args, env, extra_env, cwd, stdin, timeout))
        rule = self._match(args)
        if rule.error is not None:
            raise rule.error
        if rule.timeout:
            raise GatewayTimeout(args, timeout)
        return Result(args, rule.returncode, rule.stdout, rule.stderr)

    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        new_process_group: bool = False,
        die_with_parent: bool = False,
    ) -> Child:
        check_argv(argv, env=env, extra_env=extra_env)
        args = tuple(argv)
        self.calls.append(
            Call(
                "spawn",
                args,
                env,
                extra_env,
                cwd,
                stdin,
                None,
                new_process_group,
                die_with_parent,
            )
        )
        if self._spawn_via is not None:
            return self._spawn_via.spawn(
                argv,
                env=env,
                extra_env=extra_env,
                cwd=cwd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                new_process_group=new_process_group,
                die_with_parent=die_with_parent,
            )
        rule = self._match(args)
        if rule.error is not None:
            raise rule.error
        child = FakeChild(FAKE_PID_BASE + len(self.children))
        if not rule.timeout:
            child.exit(rule.returncode)
        self.children.append(child)
        return child
