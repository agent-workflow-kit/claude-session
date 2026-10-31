"""A scripted stand-in for ``claude_session.gateway.SystemGateway``.

Every later stage injects this where production code takes a ``Gateway``: register expected
commands with ``on()``, run the code under test, then inspect ``calls``. An unexpected command
fails loudly. ``run`` is always answered from ``on()`` rules (it never starts a program).
``spawn`` returns a scripted ``FakeChild`` unless the fake was built with ``spawn_via``
(normally a ``SystemGateway``), which really starts the program (for example the fake
``claude`` in ``tests/fakebin``) and still records the call.

A rule holds one response, or a list consumed one per matching call (the last one repeats), so
a polling test can script, say, "alive, alive, gone". With ``clock``, a scripted child's
``wait(timeout)`` on a still-running child advances that clock by ``timeout``, and a scripted
``run`` timeout advances it by ``timeout`` plus ``kill_grace`` (the longest a real one takes).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from claude_session.gateway import (
    DEVNULL,
    Child,
    Gateway,
    GatewayTimeout,
    Inherit,
    Result,
    check_argv,
    check_kill_grace,
    check_output_limit,
    check_spawn_stdin,
)

from .fake_clock import FakeClock

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
    stdin: str | int | Inherit | None = None  # run: the text; spawn: the stream
    timeout: float | None = None
    new_process_group: bool = False
    die_with_parent: bool = False
    stdout: int | Inherit | None = None  # spawn only
    stderr: int | Inherit | None = None  # spawn only
    kill_grace: float | None = None  # run only
    kill_group_on_exit: bool = False  # run only
    executable: str | None = None  # the program run when argv[0] is only its name
    output_limit: int | None = None  # run only
    discard_output: bool = False  # run only


@dataclass(frozen=True)
class Response:
    """One scripted answer. ``timeout``: ``run`` raises ``GatewayTimeout`` (only for a call
    given a ``timeout``: otherwise the fake fails, as the real call could not time out), a
    spawned child keeps running. ``error``: the call raises it."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timeout: bool = False
    error: OSError | None = None


@dataclass
class _Rule:
    prefix: tuple[str, ...]
    responses: list[Response]
    used: int = 0

    def next(self) -> Response:
        response = self.responses[min(self.used, len(self.responses) - 1)]
        self.used += 1
        return response


@dataclass
class FakeChild:
    """A scripted child: ``poll``/``wait`` report ``returncode`` once ``exit()`` was called (or
    at once when built with one); signals to a running child are recorded, never sent, and
    signals after it exited are dropped (the real child ignores them once reaped). With a
    ``clock``, waiting on a running child advances it by the timeout, as a real wait would take
    that long; ``wait()`` with no timeout on a running child fails (it would never return)."""

    pid: int
    returncode: int | None = None
    signals: list[int] = field(default_factory=list)
    clock: FakeClock | None = None

    def exit(self, returncode: int) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        if self.returncode is None:
            if timeout is None:
                raise AssertionError(
                    f"FakeChild {self.pid}: wait() with no timeout on a running child would "
                    "block forever; call exit() first"
                )
            if self.clock is not None:
                self.clock.advance(timeout)
        return self.returncode

    def send_signal(self, sig: int) -> None:
        if self.returncode is None:
            self.signals.append(sig)


class FakeGateway:
    """Records calls and answers them from rules registered with ``on()``."""

    def __init__(self, *, spawn_via: Gateway | None = None, clock: FakeClock | None = None) -> None:
        self.calls: list[Call] = []
        self.children: list[FakeChild] = []
        self._rules: list[_Rule] = []
        self._spawn_via = spawn_via
        self._clock = clock

    def on(
        self,
        prefix: Sequence[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timeout: bool = False,
        error: OSError | None = None,
        responses: Sequence[Response] | None = None,
    ) -> None:
        """Answer commands whose argv starts with ``prefix``, with one response (the keyword
        arguments) or ``responses`` in order (the last repeats). The most recent matching rule
        wins, so a test can override a fixture's default."""
        single = Response(returncode, stdout, stderr, timeout, error)
        if responses is None:
            scripted = [single]
        elif single != Response():
            raise ValueError("pass either responses or a single response, not both")
        elif not responses:
            raise ValueError("responses must not be empty")
        else:
            scripted = list(responses)
        self._rules.append(_Rule(tuple(prefix), scripted))

    def _answer(self, argv: tuple[str, ...]) -> Response:
        for rule in reversed(self._rules):
            if argv[: len(rule.prefix)] == rule.prefix:
                return rule.next()
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
        new_process_group: bool = False,
        kill_grace: float | None = None,
        executable: str | None = None,
        kill_group_on_exit: bool = False,
        output_limit: int | None = None,
        discard_output: bool = False,
        on_started: Callable[[], None] | None = None,
        canceled: Callable[[], bool] | None = None,
    ) -> Result:
        check_argv(argv, env=env, extra_env=extra_env, executable=executable)
        check_kill_grace(
            kill_grace, new_process_group=new_process_group, kill_group_on_exit=kill_group_on_exit
        )
        check_output_limit(output_limit)
        args = tuple(argv)
        self.calls.append(
            Call(
                "run",
                args,
                env=env,
                extra_env=extra_env,
                cwd=cwd,
                stdin=stdin,
                timeout=timeout,
                new_process_group=new_process_group,
                kill_grace=kill_grace,
                kill_group_on_exit=kill_group_on_exit,
                executable=executable,
                output_limit=output_limit,
                discard_output=discard_output,
            )
        )
        response = self._answer(args)
        if response.error is not None:
            raise response.error
        if on_started is not None:  # as the real gateway: the child got its stdin
            on_started()
        if response.timeout:
            if timeout is None:
                raise AssertionError(
                    f"FakeGateway: scripted timeout for {args[0]} run without a timeout (the real "
                    "gateway cannot time out then)"
                )
            if self._clock is not None:
                self._clock.advance(timeout + (kill_grace or 0))
            raise GatewayTimeout(args, timeout)
        out, err = response.stdout, response.stderr
        if discard_output:
            out, err = "", ""
        if output_limit is not None:  # as the real gateway: a byte prefix of each stream
            out = out.encode()[:output_limit].decode("utf-8", "replace")
            err = err.encode()[:output_limit].decode("utf-8", "replace")
        return Result(args, response.returncode, out, err)

    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        stdin: int | Inherit = DEVNULL,
        stdout: int | Inherit | None = None,
        stderr: int | Inherit | None = None,
        new_process_group: bool = False,
        die_with_parent: bool = False,
        executable: str | None = None,
    ) -> Child:
        check_argv(argv, env=env, extra_env=extra_env, executable=executable)
        check_spawn_stdin(stdin)
        args = tuple(argv)
        self.calls.append(
            Call(
                "spawn",
                args,
                env=env,
                extra_env=extra_env,
                cwd=cwd,
                stdin=stdin,
                new_process_group=new_process_group,
                die_with_parent=die_with_parent,
                stdout=stdout,
                stderr=stderr,
                executable=executable,
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
                executable=executable,
            )
        response = self._answer(args)
        if response.error is not None:
            raise response.error
        child = FakeChild(FAKE_PID_BASE + len(self.children), clock=self._clock)
        if not response.timeout:
            child.exit(response.returncode)
        self.children.append(child)
        return child
