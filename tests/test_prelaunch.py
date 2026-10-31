"""``pre-launch`` hooks, the runner side (interface.md, Hooks: ``pre-launch`` outcome rules;
design.md, Lifecycle hooks): strict stdout parsing, entry hashes, veto/escalate/timeout
semantics, overrides, decision-id caps and logging.

Hooks are real ``sh -c`` processes (``tests/fakebin/pre_hook.py``) run through the real
gateway. Each records its PID and start ticks in ``$FAKE_CLAUDE_STARTED_DIR`` (conftest), so
one that outlives its test is ended and fails it.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from claude_session import config, dryrun, launch, logs, toolhooks
from claude_session.gateway import SystemGateway
from claude_session.store import Store
from claude_session.toolhooks import Entry

from .fake_gateway import FakeGateway

HOOK = Path(__file__).parent / "fakebin" / "pre_hook.py"
LINUX = sys.platform.startswith("linux")
SID = "cs-abcdefgh"


def hook_cmd(log: Path, *args: str) -> str:
    return shlex.join([sys.executable, "-I", str(HOOK), "--log", str(log), *args])


def entry(cmd: str, index: int = 1, *, timeout: float = 20.0, **kw: Any) -> Entry:
    return Entry(cmd=cmd, timeout=timeout, origin="config", index=index, **kw)


def records(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def log_records(root: Path) -> list[dict[str, Any]]:
    path = root / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rpartition(b")")[2].split()[0]
    except OSError:
        return False
    return state not in (b"Z", b"X")


def wait_until(check: Any, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.02)


def run(
    root: Path,
    entries: list[Entry],
    *,
    gateway: Any = None,
    environ: dict[str, str] | None = None,
    overridable: frozenset[str] = frozenset(),
    payload: str = '{"hook": "pre-launch"}',
    kill_grace: float = 0.5,
    st: Store | None = None,
    secrets: list[str] | None = None,
    collected: toolhooks.Collected | None = None,
) -> toolhooks.Verdict:
    env = toolhooks.hook_env(
        environ if environ is not None else dict(os.environ),
        secret_env=(),
        hook=toolhooks.PRE_LAUNCH,
        csession_id=SID,
    )
    return toolhooks.run_pre_launch(
        gateway or SystemGateway(),
        entries,
        payload=lambda: payload,
        sh=toolhooks.resolve_sh(env),
        env=env,
        cwd=str(root),
        kill_grace=kill_grace,
        overridable=overridable,
        log=logs.EventLog(str(root / "log.jsonl")),
        csession_id=SID,
        run=1,
        st=st,
        secrets=secrets or (),
        collected=collected,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    return path


# Strict stdout parsing


@pytest.mark.parametrize(
    ("stdout", "ids", "problem"),
    [
        ("", [], None),
        (" \n\t\r", [], None),  # whitespace only: no decision, not an error
        ('{"decision_id": "d-1"}', ["d-1"], None),
        ('{"decision_id": ["a", "b", "a"]}', ["a", "b", "a"], None),
        ('{"decision_id": []}', [], None),
        ('{"other": 1}', [], None),  # other keys ignored
        ('{"decision_id": "x", "other": [1, {"y": null}]}', ["x"], None),
        (
            '{"decision_id": "line\\nbreak \\"quoted\\" $(rm -rf /)"}',
            ['line\nbreak "quoted" $(rm -rf /)'],
            None,
        ),
        ('{"decision_id": "\\ud83d\\ude00"}', ["\U0001f600"], None),  # an escaped pair joins
        ('{"decision_id": "é"}', ["é"], None),
        ("not json", [], "not JSON"),
        ('{"decision_id": "x"} trailing', [], "not JSON"),
        ('{"decision_id": "x"', [], "not JSON"),
        ("[1, 2]", [], "not a JSON object"),
        ('"just a string"', [], "not a JSON object"),
        ("null", [], "not a JSON object"),
        ('{"decision_id": 1}', [], "decision_id is not a string or a list of strings"),
        ('{"decision_id": null}', [], "decision_id is not a string or a list of strings"),
        ('{"decision_id": ["a", 1]}', [], "decision_id is not a string or a list of strings"),
        ('{"decision_id": {"a": "b"}}', [], "decision_id is not a string or a list of strings"),
        ('{"decision_id": "a", "decision_id": "b"}', [], "not JSON"),  # duplicate key
        ('{"decision_id": "a", "x": NaN}', [], "not JSON"),
        ('{"decision_id": "a", "x": Infinity}', [], "not JSON"),
        ('{"decision_id": "\\ud800"}', [], "decision_id holds an unpaired surrogate"),
        ('{"decision_id": "\\udc00\\ud800"}', [], "decision_id holds an unpaired surrogate"),
        ('{"decision_id": ["ok", "\\udfff"]}', [], "decision_id holds an unpaired surrogate"),
        ('{"decision_id": "bad �"}', ["bad �"], None),  # a literal U+FFFD is valid UTF-8
        ("[" * 2000, [], "not JSON"),  # nesting deep enough to overflow the decoder
        (b'{"decision_id": "a\xffb"}', [], "not valid UTF-8"),
        (b'{"decision_id": "\xed\xa0\x80"}', [], "not valid UTF-8"),  # an encoded surrogate
        (b'{"decision_id":\r\n"crlf"}\r\n', ["crlf"], None),  # CR is JSON whitespace, kept
        ('{"decision_id":"d-1"}' + "\r\n" * 1500 + "garbage", [], "longer than 2048 bytes"),
        (" " * 3000 + "x", [], "longer than 2048 bytes"),  # whitespace-led, still too long
        (" " * 3000, [], "longer than 2048 bytes"),
    ],
)
def test_parse_decision(stdout: str | bytes, ids: list[str], problem: str | None) -> None:
    raw = stdout.encode() if isinstance(stdout, str) else stdout
    assert toolhooks.parse_decision(raw) == (ids, problem)


def test_parse_decision_takes_exactly_the_cap_and_ignores_more() -> None:
    head = '{"decision_id": "'
    fill = toolhooks.PRE_LAUNCH_OUTPUT_MAX - len(head) - 2
    exact = head + "x" * fill + '"}'
    assert len(exact.encode()) == toolhooks.PRE_LAUNCH_OUTPUT_MAX
    assert toolhooks.parse_decision(exact.encode()) == (["x" * fill], None)
    # One byte more (a valid object padded with a space): ignored, as a cut output would be.
    assert toolhooks.parse_decision(exact.encode() + b" ") == ([], "longer than 2048 bytes")


def test_outcome_message_strips_control_characters() -> None:
    raw = "a\x1b[31mb\x00c\r\nd\x7fe\x85f".encode()
    assert toolhooks.outcome_message(raw) == "a[31mbcdef"


def test_outcome_message_caps_at_2048_utf8_bytes_on_a_character_boundary() -> None:
    assert toolhooks.outcome_message(("é" * 5000).encode()) == "é" * 1024
    message = toolhooks.outcome_message(("x" + "é" * 5000).encode())
    assert message == "x" + "é" * 1023 and len(message.encode()) == 2047


def test_outcome_message_strips_control_characters_before_the_cap() -> None:
    assert toolhooks.outcome_message(b"\x01" * 3000 + b"visible") == "visible"


def test_outcome_message_is_redacted_against_the_secret_values() -> None:
    secret = "s3cret-value-123"
    message = toolhooks.outcome_message(f"denied: {secret}\n".encode(), [secret])
    assert message == "denied: [redacted]"
    # A secret split by a control character is still redacted once they are stripped.
    split = f"key={secret[:5]}\x07{secret[5:]}".encode()
    assert toolhooks.outcome_message(split, [secret]) == "key=[redacted]"


# Entry hashes


def test_entry_hash_is_stable_and_names_name_command_and_selectors() -> None:
    base = entry("check", kinds=("implement",), labels=("team=x",))
    digest = toolhooks.entry_hash(base)
    assert len(digest) == 64 and int(digest, 16) >= 0
    assert toolhooks.entry_hash(entry("check", kinds=("implement",), labels=("team=x",))) == digest
    # The timeout is not part of it.
    assert (
        toolhooks.entry_hash(entry("check", timeout=99, kinds=("implement",), labels=("team=x",)))
        == digest
    )
    others = [
        entry("check", 2, kinds=("implement",), labels=("team=x",)),  # moved
        Entry(
            cmd="check",
            timeout=20,
            origin="invocation",
            index=1,
            kinds=("implement",),
            labels=("team=x",),
        ),
        entry("check2", kinds=("implement",), labels=("team=x",)),  # edited
        entry("check", kinds=("review",), labels=("team=x",)),
        entry("check", kinds=("implement",)),
        entry("check", kinds=("implement",), labels=("team=x",), exclude_kinds=()),
        entry("check", kinds=(), labels=("team=x",)),  # [] is not "no selector"
    ]
    assert len({toolhooks.entry_hash(e) for e in others} | {digest}) == len(others) + 1


def test_entry_hash_handles_commands_that_are_not_valid_utf8() -> None:
    odd = entry("printf '\udcff'")  # an argv command decoded with surrogate escapes
    assert len(toolhooks.entry_hash(odd)) == 64


# Entries and selectors


def test_pre_launch_entries_come_from_config_then_flags(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "hooks:\n  pre-launch:\n"
        '    - {cmd: "a", timeout: 5s, kinds: [implement], labels: ["t=1"]}\n'
        '    - {cmd: "b", exclude_kinds: [gate-check]}\n'
        "  post-state-change:\n    - {cmd: post}\n",
        encoding="utf-8",
    )
    cfg = config.load(str(path))
    got = toolhooks.pre_launch_config_entries(cfg)
    assert [(e.cmd, e.timeout, e.name) for e in got] == [
        ("a", 5.0, "config entry 1"),
        ("b", 30.0, "config entry 2"),
    ]
    assert got[0].kinds == ("implement",) and got[0].labels == ("t=1",)
    assert got[1].exclude_kinds == ("gate-check",) and got[1].states is None
    flags = toolhooks.flag_entries(
        ["pre-launch=x", "post-state-change=y", "pre-launch=z=1"],
        ["post-state-change=1s", "pre-launch=7s"],
        toolhooks.PRE_LAUNCH,
    )
    assert [(e.cmd, e.timeout, e.name) for e in flags] == [
        ("x", 7.0, "invocation entry 1"),
        ("z=1", 7.0, "invocation entry 2"),
    ]


@pytest.mark.parametrize(
    ("hook", "labels", "run_kind"),
    [
        ({"kinds": ["implement"]}, ["kind=implement"], "implement"),
        ({"kinds": ["review"]}, ["kind=implement"], "implement"),
        ({"exclude_kinds": ["implement"]}, [], "implement"),
        ({"labels": ["team=x"]}, ["team=x", "kind=implement"], "implement"),
        ({"labels": ["team=x", "pr=a/b#1"]}, ["team=x"], "implement"),
        ({"kinds": []}, [], "implement"),
        ({}, [], "gate-check"),
    ],
)
def test_the_dry_run_plan_selects_exactly_as_the_runner(
    tmp_path: Path, hook: dict[str, Any], labels: list[str], run_kind: str
) -> None:
    path = tmp_path / "config.yaml"
    item = json.dumps({"cmd": "true", **hook})
    path.write_text(
        f"hooks:\n  pre-launch:\n    - {item}\n  post-state-change:\n    - {item}\n",
        encoding="utf-8",
    )
    cfg = config.load(str(path))
    req = launch.Request(
        kind=None, mode="headless", skill=None, prompt=None, inputs=(), plugin_dirs=(),
        repo=None, cwd=None, labels=(), flags={}, actor="auto:x", decision_ids=(), hooks=(),
        hook_timeouts=(), outcome_file=None,
    )  # fmt: skip
    plan = dryrun.hooks_json(cfg, req, run_kind, labels)
    runner = [
        toolhooks.selected(e, run_kind=run_kind, labels=labels)
        for e in [*toolhooks.pre_launch_config_entries(cfg), *toolhooks.config_entries(cfg)]
    ]
    assert [item["selected"] for item in plan] == runner


# Outcome rules (real hooks)


def test_all_exit_0_continues_runs_every_entry_in_order_and_collects_decision_ids(
    tmp_path: Path, root: Path
) -> None:
    log = tmp_path / "hook.jsonl"
    entries = [
        entry(hook_cmd(log, "--name", "a", "--stdout", '{"decision_id": "d-a"}'), 1),
        entry(hook_cmd(log, "--name", "b", "--stdout", "plain text"), 2),
        entry(hook_cmd(log, "--name", "c", "--stdout", '{"decision_id": ["d-c1", "d-c2"]}'), 3),
    ]
    verdict = run(root, entries, payload='{"marker": 1}')
    assert verdict == toolhooks.Verdict(decision_ids=("d-a", "d-c1", "d-c2"))
    got = records(log)
    assert [r["name"] for r in got] == ["a", "b", "c"]
    assert all(r["payload"] == {"marker": 1} for r in got)
    assert {r["cwd"] for r in got} == {str(root)}
    assert {r["env"]["CSESSION_HOOK"] for r in got} == {"pre-launch"}
    # Each in a process group of its own (led by its sh), not this process's.
    groups = [r["pgid"] for r in got]
    assert len(set(groups)) == 3 and os.getpgid(0) not in groups
    (ignored,) = [r for r in log_records(root) if r["event"] == "hook_output_ignored"]
    assert ignored["message"] == "pre-launch config entry 2: stdout ignored (not JSON)"
    assert "plain text" not in (root / "log.jsonl").read_text()


def test_exit_13_escalates_and_the_remaining_entries_still_run(tmp_path: Path, root: Path) -> None:
    log = tmp_path / "hook.jsonl"
    first = entry(hook_cmd(log, "--name", "a", "--exit", "13", "--stderr", "needs a human\n"), 1)
    second = entry(hook_cmd(log, "--name", "b", "--exit", "13", "--stderr", "me too"), 2)
    third = entry(hook_cmd(log, "--name", "c", "--stdout", '{"decision_id": "late"}'), 3)
    verdict = run(root, [first, second, third])
    assert [r["name"] for r in records(log)] == ["a", "b", "c"]
    assert verdict.outcome == "escalated" and verdict.end_reason == "hook_escalate"
    assert verdict.message == "needs a human"  # the first escalation's stderr
    assert verdict.escalated == (toolhooks.entry_hash(first), toolhooks.entry_hash(second))
    assert verdict.overridden == () and verdict.decision_ids == ("late",)
    escalations = [r for r in log_records(root) if r["event"] == "hook_escalate"]
    assert [r["level"] for r in escalations] == ["warn", "warn"]


@pytest.mark.parametrize("code", [1, 2, 11, 12, 14, 127, 255])
def test_any_other_exit_vetoes_at_once(tmp_path: Path, root: Path, code: int) -> None:
    log = tmp_path / "hook.jsonl"
    escalating = entry(hook_cmd(log, "--name", "a", "--exit", "13"), 1)
    vetoing = entry(
        hook_cmd(log, "--name", "b", "--exit", str(code), "--stderr", "no\x1b[0m way\n",
                 "--stdout", '{"decision_id": "why"}'),
        2,
    )  # fmt: skip
    never = entry(hook_cmd(log, "--name", "c"), 3)
    verdict = run(root, [escalating, vetoing, never])
    assert [r["name"] for r in records(log)] == ["a", "b"]  # stopped immediately
    assert (verdict.outcome, verdict.end_reason) == ("vetoed", "hook_veto")
    assert verdict.message == "no[0m way"
    # The escalation before the veto is still recorded; the vetoing hook's id too.
    assert verdict.escalated == (toolhooks.entry_hash(escalating),)
    assert verdict.decision_ids == ("why",)
    (veto,) = [r for r in log_records(root) if r["event"] == "hook_veto"]
    assert veto["message"] == f"pre-launch config entry 2: exit {code}"


def test_a_veto_or_escalation_without_stderr_gets_a_fixed_message(root: Path) -> None:
    assert run(root, [entry("exit 4")]).message == (
        "pre-launch config entry 1 vetoed the launch (exit 4)"
    )
    assert run(root, [entry("exit 13")]).message == (
        "pre-launch config entry 1 escalated the launch (exit 13)"
    )
    # Only control characters: nothing left, so the fixed text.
    assert run(root, [entry("printf '\\033\\n' >&2; exit 4")]).message == (
        "pre-launch config entry 1 vetoed the launch (exit 4)"
    )


def test_a_hook_killed_by_a_signal_vetoes(root: Path) -> None:
    verdict = run(root, [entry("kill -9 $$")])
    assert (verdict.outcome, verdict.end_reason) == ("vetoed", "hook_veto")


def test_long_stderr_is_capped_and_long_stdout_ignored(root: Path) -> None:
    # 100 kB on each stream: more than the pipe holds, so the rest must be read and dropped.
    big = "head -c 100000 /dev/zero | tr '\\0' e"
    cmd = f"{big} >&2; printf '{{\"decision_id\": \"'; {big}; printf '\"}}'; exit 4"
    verdict = run(root, [entry(cmd)])
    assert verdict.message == "e" * 2048
    assert verdict.decision_ids == ()
    (ignored,) = [r for r in log_records(root) if r["event"] == "hook_output_ignored"]
    assert ignored["message"].endswith("stdout ignored (longer than 2048 bytes)")


def test_invalid_utf8_on_stdout_is_ignored(tmp_path: Path, root: Path) -> None:
    raw = tmp_path / "raw"
    raw.write_bytes(b'{"decision_id": "a\xffb"}')
    log = tmp_path / "hook.jsonl"
    verdict = run(root, [entry(hook_cmd(log, "--stdout-file", str(raw)))])
    assert verdict == toolhooks.Verdict()
    (ignored,) = [r for r in log_records(root) if r["event"] == "hook_output_ignored"]
    assert ignored["message"].endswith("(not valid UTF-8)")


def test_crlf_padding_past_2_kib_is_ignored_and_logged(root: Path) -> None:
    cmd = (
        "printf '{\"decision_id\":\"d-1\"}'; i=0; while [ $i -lt 1500 ]; do printf '\\r\\n';"
        " i=$((i+1)); done; printf garbage"
    )
    verdict = run(root, [entry(cmd)])
    assert verdict == toolhooks.Verdict()
    (ignored,) = [r for r in log_records(root) if r["event"] == "hook_output_ignored"]
    assert ignored["message"].endswith("(longer than 2048 bytes)")


def test_whitespace_led_output_past_2_kib_is_ignored_and_logged(root: Path) -> None:
    cmd = "head -c 50000 /dev/zero | tr '\\0' ' '; printf x"
    verdict = run(root, [entry(cmd)])
    assert verdict == toolhooks.Verdict()
    (ignored,) = [r for r in log_records(root) if r["event"] == "hook_output_ignored"]
    assert ignored["message"].endswith("(longer than 2048 bytes)")


def test_a_literal_replacement_character_on_stdout_is_accepted(tmp_path: Path, root: Path) -> None:
    raw = tmp_path / "raw"
    raw.write_bytes('{"decision_id": "a\ufffdb"}'.encode())
    log = tmp_path / "hook.jsonl"
    verdict = run(root, [entry(hook_cmd(log, "--stdout-file", str(raw)))])
    assert verdict.decision_ids == ("a\ufffdb",)
    assert [r for r in log_records(root) if r["event"] == "hook_output_ignored"] == []


def test_the_message_is_the_hooks_stderr_redacted(root: Path) -> None:
    secret = "s3cret-value-123"
    verdict = run(root, [entry(f"echo 'token {secret}' >&2; exit 4")], secrets=[secret])
    assert verdict.message == "token [redacted]"


@pytest.mark.skipif(not LINUX, reason="process groups are checked through /proc")
def test_a_timeout_carries_the_hooks_stderr_as_the_message(root: Path) -> None:
    verdict = run(root, [entry("echo 'gate busy' >&2; sleep 30", timeout=1.0)])
    assert (verdict.outcome, verdict.end_reason) == ("vetoed", "hook_timeout")
    assert verdict.message == "gate busy"


def sleep_pid(pidfile: Path) -> int:
    """The PID a hook wrote to ``pidfile``."""
    wait_until(lambda: pidfile.exists() and pidfile.read_text().strip() != "", 5)
    return int(pidfile.read_text())


def argv0(pid: int) -> bytes:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().split(b"\0")[0]
    except OSError:
        return b""


@pytest.mark.skipif(not LINUX, reason="process groups are checked through /proc")
def test_a_background_process_left_in_the_hooks_group_is_ended_when_sh_exits(
    tmp_path: Path, root: Path
) -> None:
    pidfile = tmp_path / "pid"
    cmd = f"sleep 30 >/dev/null 2>&1 </dev/null & echo $! > {shlex.quote(str(pidfile))}; exit 0"
    start = time.monotonic()
    verdict = run(root, [entry(cmd)])
    assert verdict == toolhooks.Verdict() and time.monotonic() - start < 10
    pid = sleep_pid(pidfile)
    try:
        wait_until(lambda: not running(pid), 5)
    finally:
        if running(pid) and argv0(pid).endswith(b"sleep"):  # only the exact sleep it started
            os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(not LINUX, reason="process groups are checked through /proc")
def test_each_hooks_group_is_recorded_while_it_runs_and_removed_after(
    tmp_path: Path, root: Path
) -> None:
    log = tmp_path / "hook.jsonl"
    sql = "CREATE TABLE seen AS SELECT * FROM hook_groups"
    with Store.open(str(root / "state.db")) as st:
        verdict = run(root, [entry(hook_cmd(log, "--sql", sql))], st=st)
        assert verdict == toolhooks.Verdict()
        (record,) = records(log)
        seen = st.conn.execute("SELECT event_id, pgid, holder_pid FROM seen").fetchall()
        assert seen == [(toolhooks.PRE_LAUNCH_EVENT, record["pgid"], os.getpid())]
        assert st.hook_groups() == []


@pytest.mark.skipif(not LINUX, reason="process groups are checked through /proc")
def test_a_timeout_vetoes_and_ends_the_hooks_process_group(tmp_path: Path, root: Path) -> None:
    log = tmp_path / "hook.jsonl"
    slow = entry(hook_cmd(log, "--ignore-term", "--child", "--sleep", "30"), 1, timeout=1.0)
    never = entry(hook_cmd(log, "--name", "never"), 2)
    start = time.monotonic()
    verdict = run(root, [slow, never], kill_grace=0.5)
    assert 1.0 <= time.monotonic() - start < 10
    assert (verdict.outcome, verdict.end_reason) == ("vetoed", "hook_timeout")
    assert verdict.message == "pre-launch config entry 1 timed out after 1 s; process group ended"
    (record,) = records(log)  # the second entry never ran
    wait_until(lambda: not running(record["pid"]) and not running(record["child"]))
    (timed_out,) = [r for r in log_records(root) if r["event"] == "hook_timeout"]
    assert timed_out["level"] == "warn"


def test_a_missing_shell_vetoes_without_running_anything(tmp_path: Path, root: Path) -> None:
    verdict = run(root, [entry("true")], environ={"PATH": str(tmp_path / "empty")})
    assert (verdict.outcome, verdict.end_reason) == ("vetoed", "hook_veto")
    assert "sh not found" in (verdict.message or "")
    events = [r["event"] for r in log_records(root)]
    assert events == ["hook_failed", "hook_veto"]


class _BrokenGateway(FakeGateway):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self.exc = exc

    def run(self, argv: Any, **kw: Any) -> Any:
        raise self.exc


def test_a_runner_exception_is_a_defined_veto_never_a_traceback(root: Path) -> None:
    verdict = run(root, [entry("true")], gateway=_BrokenGateway(RuntimeError("secret detail")))
    assert (verdict.outcome, verdict.end_reason) == ("vetoed", "hook_veto")
    assert verdict.message == "pre-launch config entry 1 could not run the hook (RuntimeError)"
    assert "secret detail" not in (root / "log.jsonl").read_text()


def test_a_nul_in_a_flag_command_vetoes(root: Path) -> None:
    verdict = run(root, [entry("true\x00false")])
    assert verdict.outcome == "vetoed"
    assert verdict.message == "pre-launch config entry 1 could not run the hook (ValueError)"


def test_a_signal_while_a_hook_runs_propagates(root: Path) -> None:
    with pytest.raises(KeyboardInterrupt):
        run(root, [entry("true")], gateway=_BrokenGateway(KeyboardInterrupt()))


class _InterruptedSecond(FakeGateway):
    """Answers the first hook with a decision id and exit 13, then a signal ends the run."""

    def run(self, argv: Any, **kw: Any) -> Any:
        if self.calls:
            raise KeyboardInterrupt
        self.on(["sh"], returncode=13, stdout='{"decision_id": "d1"}')
        return super().run(argv, **kw)


def test_what_finished_hooks_returned_is_collected_before_a_signal(root: Path) -> None:
    collected = toolhooks.Collected()
    first = entry("first", 1)
    with pytest.raises(KeyboardInterrupt):
        run(root, [first, entry("second", 2)], gateway=_InterruptedSecond(), collected=collected)
    assert collected.decision_ids == ["d1"]
    assert collected.escalated == [toolhooks.entry_hash(first)]


def test_the_log_never_names_a_hook_by_its_command_or_output(tmp_path: Path, root: Path) -> None:
    log = tmp_path / "hook.jsonl"
    marker = "UNIQUE-MARKER-TEXT"
    entries = [
        entry(hook_cmd(log, "--name", marker, "--stdout", f"not json {marker}"), 1),
        entry(hook_cmd(log, "--exit", "13", "--stderr", marker), 2),
        entry(hook_cmd(log, "--exit", "3", "--stderr", marker), 3),
    ]
    run(root, entries)
    assert marker not in (root / "log.jsonl").read_text()


# Overrides


def test_an_overridden_escalation_continues_and_is_still_recorded(
    tmp_path: Path, root: Path
) -> None:
    log = tmp_path / "hook.jsonl"
    covered = entry(hook_cmd(log, "--exit", "13"), 1)
    verdict = run(root, [covered], overridable=frozenset({toolhooks.entry_hash(covered)}))
    digest = toolhooks.entry_hash(covered)
    assert verdict == toolhooks.Verdict(escalated=(digest,), overridden=(digest,))
    (escalate,) = [r for r in log_records(root) if r["event"] == "hook_escalate"]
    assert escalate["level"] == "info" and "overridden" in escalate["message"]


def test_an_override_covers_only_the_recorded_entries(tmp_path: Path, root: Path) -> None:
    log = tmp_path / "hook.jsonl"
    covered = entry(hook_cmd(log, "--exit", "13"), 1)
    other = entry(hook_cmd(log, "--exit", "13", "--stderr", "other"), 2)
    vetoing = entry("exit 2", 3)
    overridable = frozenset({toolhooks.entry_hash(covered)})
    verdict = run(root, [covered, other], overridable=overridable)
    assert (verdict.outcome, verdict.message) == ("escalated", "other")
    assert verdict.escalated == (toolhooks.entry_hash(covered), toolhooks.entry_hash(other))
    assert verdict.overridden == (toolhooks.entry_hash(covered),)
    # A veto is never overridden.
    verdict = run(root, [covered, vetoing], overridable=overridable)
    assert verdict.outcome == "vetoed"
    # The same command at another position is another entry.
    moved = entry(covered.cmd, 2)
    assert run(root, [entry("true", 1), moved], overridable=overridable).outcome == "escalated"


# The decision-id budget


def test_decision_ids_beyond_the_budget_are_dropped_and_logged(root: Path) -> None:
    gateway = FakeGateway()
    # Characters that escape to 12 bytes each (a surrogate pair): the worst case.
    item = "\U0001f600" * 150
    body = json.dumps({"decision_id": [item]}, ensure_ascii=False)
    assert len(body.encode()) <= toolhooks.PRE_LAUNCH_OUTPUT_MAX
    gateway.on(["sh"], stdout=body)
    size = toolhooks.escaped_size(item) + 1
    fits = toolhooks.DECISION_IDS_MAX_BYTES // size
    entries = [entry(f"hook {n}", n) for n in range(1, fits + 3)]
    verdict = run(root, entries, gateway=gateway)
    assert verdict.outcome == "continue"
    assert len(verdict.decision_ids) == fits
    total = sum(toolhooks.escaped_size(i) + 1 for i in verdict.decision_ids)
    assert total <= toolhooks.DECISION_IDS_MAX_BYTES
    dropped = [r for r in log_records(root) if r["event"] == "hook_output_ignored"]
    assert dropped and all("were dropped" in r["message"] for r in dropped)
    # Every entry still ran.
    assert len(gateway.calls) == len(entries)


def test_each_entry_gets_a_fresh_payload(root: Path) -> None:
    gateway = FakeGateway()
    gateway.on(["sh"])
    count = iter(range(100))
    env = toolhooks.hook_env(
        dict(os.environ), secret_env=(), hook=toolhooks.PRE_LAUNCH, csession_id=SID
    )
    toolhooks.run_pre_launch(
        gateway,
        [entry("a", 1), entry("b", 2)],
        payload=lambda: json.dumps({"n": next(count)}),
        sh="/bin/sh",
        env=env,
        cwd=str(root),
        kill_grace=0.5,
        log=logs.EventLog(str(root / "log.jsonl")),
        csession_id=SID,
        run=1,
    )
    assert [call.stdin for call in gateway.calls] == ['{"n": 0}', '{"n": 1}']
    assert all(call.new_process_group and call.kill_grace == 0.5 for call in gateway.calls)
    assert all(call.output_limit == toolhooks.PRE_LAUNCH_KEEP for call in gateway.calls)
    assert all(call.kill_group_on_exit for call in gateway.calls)
    assert not any(call.discard_output for call in gateway.calls)
    assert [call.argv for call in gateway.calls] == [("sh", "-c", "a"), ("sh", "-c", "b")]
