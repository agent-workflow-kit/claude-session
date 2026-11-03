"""``post-state-change`` hooks: selectors, execution, payload and delivery (interface.md, Hooks;
design.md, Lifecycle hooks and Data model).

Hooks are real ``sh -c`` processes (``tests/fakebin/post_hook.py``) run through the real
gateway. Each records its PID and start ticks in ``$FAKE_CLAUDE_STARTED_DIR`` (conftest), so
one that outlives its test is ended and fails it; the tests that leave a hook running on
purpose (a killed deliverer) end it themselves through the exact PID it recorded.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from claude_session import config, ids, logs, store, toolhooks
from claude_session.gateway import Result, SystemGateway
from claude_session.store import Final, NewRun, Store
from claude_session.toolhooks import Entry

from .fake_gateway import FakeGateway
from .schemas import validate

HOOK = Path(__file__).parent / "fakebin" / "post_hook.py"
PAYLOAD_SCHEMA = "claude-session/hooks/post-state-change.json"
LINUX = sys.platform.startswith("linux")


# Helpers


def hook_cmd(log: Path, *args: str) -> str:
    return shlex.join([sys.executable, "-I", str(HOOK), "--log", str(log), *args])


def load_config(tmp_path: Path, text: str = "") -> config.Config:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return config.load(str(path))


def hooks_config(*entries: dict[str, Any], extra: str = "") -> str:
    return (
        extra
        + "hooks:\n  post-state-change:\n"
        + "".join("    - " + json.dumps(entry) + "\n" for entry in entries)
    )


def new_run(state_root: str, **fields: Any) -> NewRun:
    base: dict[str, Any] = {
        "state_root": state_root,
        "kind": "implement",
        "reads_third_party": False,
        "mode": "headless",
        "repo": "example.com/group/project",
        "model": None,
        "skill": None,
        "has_prompt": True,
        "spec": {},
        "resolved": {},
        "config_hash": None,
        "invocation": {"actor": "auto:unknown"},
        "hook_interpreter": sys.executable,
        "config_path": None,
        "wrapper_pid": os.getpid(),
        "wrapper_start": store.start_time(os.getpid()),
        "labels": ("team=x",),
    }
    return NewRun(**{**base, **fields})


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def st(root: Path) -> Iterator[Store]:
    with Store.open(str(root / "state.db")) as s:
        yield s


def allocate(st: Store, root: Path, **fields: Any) -> str:
    return st.allocate(new_run(str(root), **fields), new_id=ids.new_session_id)


def change(st: Store, sid: str, state: str, activity: str | None, cause: str, **kw: Any) -> None:
    with st.transaction():
        run = st.hook_run(sid)
        assert run is not None
        st.apply_state(
            run,
            state=state,
            activity=activity,
            needs_input_reason=kw.get("reason"),
            cause=cause,
        )


def finalize(st: Store, sid: str, **fields: Any) -> None:
    final = {
        "phase": "session",
        "outcome": "exited-unreported",
        "end_reason": "no_report",
        "exit_code": 14,
        "state": "exited-unreported",
        "cause": "exit",
        "cost_usd": 0.25,
        **fields,
    }
    assert st.finalize_run(sid, 1, Final(**final))


def deliverer(st: Store, cfg: config.Config, root: Path, **kw: Any) -> toolhooks.Deliverer:
    environ = kw.pop("environ", dict(os.environ))
    return toolhooks.Deliverer(
        st,
        cfg,
        gateway=kw.pop("gateway", SystemGateway()),
        environ=environ,
        state_root=str(root),
        **kw,
    )


def records(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def event_ids(st: Store, sid: str) -> list[int]:
    return [
        row[0]
        for row in st.conn.execute(
            "SELECT id FROM events WHERE csession_id = ? ORDER BY id", (sid,)
        )
    ]


def undelivered(st: Store, sid: str) -> list[int]:
    return [int(e["id"]) for e in st.undelivered_events(sid, 1000)]


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


def wait_until(check: Any, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.02)


# Selectors


def entry(**kw: Any) -> Entry:
    return Entry(cmd="true", timeout=1.0, origin="config", index=1, **kw)


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({}, True),
        ({"kinds": ("implement", "review")}, True),
        ({"kinds": ("review",)}, False),
        ({"kinds": ()}, False),  # an empty list selects nothing
        ({"exclude_kinds": ("implement",)}, False),
        ({"exclude_kinds": ("gate-check",)}, True),
        ({"exclude_kinds": ()}, True),
        ({"labels": ("team=x",)}, True),
        ({"labels": ("team=x", "pr=github.com/o/r#1")}, True),
        ({"labels": ("team=y",)}, False),
        ({"labels": ("team=x", "team=y")}, False),
        ({"labels": ("team",)}, False),  # does not parse: never matches
        ({"labels": ("team=x=1",)}, False),  # split at the first '=': value x=1
        ({"labels": ()}, True),  # all of no labels
        ({"states": ("needs-input", "exited")}, True),
        ({"states": ("idle",)}, False),
        ({"kinds": ("implement",), "labels": ("team=x",), "states": ("exited",)}, True),
        ({"kinds": ("implement",), "labels": ("team=x",), "states": ("idle",)}, False),
    ],
)
def test_selectors_are_combined_with_and(kw: dict[str, Any], expected: bool) -> None:
    labels = ["kind=implement", "pr=github.com/o/r#1", "team=x", "team=z"]
    assert toolhooks.selected(entry(**kw), run_kind="implement", labels=labels, state="exited") is (
        expected
    )


def test_a_label_value_holding_an_equals_sign_matches_verbatim() -> None:
    e = entry(labels=("ref=a=b",))
    assert toolhooks.selected(e, run_kind="k", labels=["ref=a=b"])
    assert not toolhooks.selected(e, run_kind="k", labels=["ref=a"])


def test_without_a_state_the_states_selector_is_not_checked() -> None:
    # The pre-launch and dry-run use: no state change is known.
    assert toolhooks.selected(entry(states=("idle",)), run_kind="k", labels=[])


def test_unusable_stored_labels_match_no_label_selector() -> None:
    assert toolhooks.stored_labels("not json") == []
    assert toolhooks.stored_labels('{"a": 1}') == []
    assert toolhooks.stored_labels('["a=b", 3]') == ["a=b"]
    malformed = ["Bad Key=x", "team=x"]
    assert toolhooks.selected(entry(labels=("team=x",)), run_kind="k", labels=malformed)


def test_config_entries_keep_order_and_fields(tmp_path: Path) -> None:
    cfg = load_config(
        tmp_path,
        hooks_config(
            {"cmd": "a"},
            {
                "cmd": "b",
                "timeout": "2s",
                "async": True,
                "kinds": ["review"],
                "exclude_kinds": ["gate-check"],
                "labels": ["team=x"],
                "states": ["idle"],
            },
        ),
    )
    first, second = toolhooks.config_entries(cfg)
    assert (first.cmd, first.timeout, first.async_, first.index, first.kinds) == (
        "a",
        30.0,
        False,
        1,
        None,
    )
    assert second == Entry(
        cmd="b",
        timeout=2.0,
        origin="config",
        index=2,
        async_=True,
        kinds=("review",),
        exclude_kinds=("gate-check",),
        labels=("team=x",),
        states=("idle",),
    )


def test_invocation_entries_take_their_hooks_and_timeout() -> None:
    stored = json.dumps(
        {
            "hooks": ["pre-launch=x", "post-state-change=echo a=b", "post-state-change=two"],
            "hook_timeouts": ["post-state-change=5s", "pre-launch=1s", "post-state-change=7s"],
        }
    )
    got = toolhooks.invocation_entries(stored)
    assert [(e.cmd, e.timeout, e.origin, e.index) for e in got] == [
        ("echo a=b", 7.0, "invocation", 1),
        ("two", 7.0, "invocation", 2),
    ]
    assert all(e.kinds is None and e.states is None and not e.async_ for e in got)


@pytest.mark.parametrize(
    "stored",
    [None, "", "not json", "[]", '{"hooks": "post-state-change=x"}', '{"hooks": [1, "nope"]}'],
)
def test_unusable_invocation_data_gives_no_entries(stored: str | None) -> None:
    assert toolhooks.invocation_entries(stored) == []


def test_an_unparsable_stored_hook_timeout_falls_back_to_the_default() -> None:
    stored = json.dumps(
        {"hooks": ["post-state-change=x"], "hook_timeouts": ["post-state-change=0"]}
    )
    assert toolhooks.invocation_entries(stored)[0].timeout == toolhooks.DEFAULT_TIMEOUT


# Environment


def test_hook_env_clears_csession_and_secret_variables_and_sanitizes_path() -> None:
    environ = {
        "PATH": "relative:/usr/bin::/bin",
        "CSESSION_ID": "cs-zzzzzzzz",
        "CSESSION_DIR": "/elsewhere",
        "CSESSION_POLL_INTERVAL": "1",
        "VAULT_TOKEN": "secret",
        "KEEP": "me",
        "": "empty name",
        "BAD": "nul\x00value",
    }
    env = toolhooks.hook_env(
        environ, secret_env=["VAULT_TOKEN"], hook="post-state-change", csession_id="cs-abcd2345"
    )
    assert env == {
        "PATH": "/usr/bin:/bin",
        "KEEP": "me",
        "CSESSION_HOOK": "post-state-change",
        "CSESSION_ID": "cs-abcd2345",
        "CSESSION_OBJECT_TYPE": "session",
    }


def test_hook_env_without_an_absolute_path_entry_uses_the_default() -> None:
    env = toolhooks.hook_env({"PATH": "bin:."}, secret_env=[], hook="h", csession_id="cs-abcd2345")
    assert env["PATH"] == "/usr/bin:/bin"


def test_sh_is_resolved_to_an_absolute_path() -> None:
    sh = toolhooks.resolve_sh({"PATH": "/usr/bin:/bin"})
    assert sh is not None and os.path.isabs(sh)
    assert toolhooks.resolve_sh({"PATH": "/nonexistent"}) is None


def test_run_entry_uses_sh_c_its_own_group_the_timeout_and_no_output_pipes(tmp_path: Path) -> None:
    gw = FakeGateway()
    gw.on(["sh", "-c", "notify"], stdout="x" * 100)
    e = Entry(cmd="notify", timeout=3.0, origin="config", index=1)
    ran = toolhooks.run_entry(
        gw, e, sh="/bin/sh", env={"PATH": "/bin"}, cwd=str(tmp_path), payload="{}", kill_grace=2.0
    )
    assert ran == toolhooks.Ran("ok", "exit 0")
    (call,) = gw.calls
    assert call.argv == ("sh", "-c", "notify") and call.executable == "/bin/sh"
    assert call.new_process_group and call.kill_grace == 2.0 and call.timeout == 3.0
    assert call.cwd == str(tmp_path) and call.stdin == "{}" and call.discard_output


def test_run_entry_without_sh_fails_without_running_anything() -> None:
    gw = FakeGateway()
    ran = toolhooks.run_entry(gw, entry(), sh=None, env={}, cwd="/", payload="{}", kill_grace=1.0)
    assert ran.result == "failed" and gw.calls == []


# Delivery: order, once, payload


def test_each_event_is_delivered_once_in_order_with_a_valid_payload(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    change(st, sid, "working", "working", "SessionStart")
    change(st, sid, "needs-input", "working", "PermissionRequest", reason="permission")
    change(st, sid, "working", "working", "PostToolUse")
    change(st, sid, "idle", "idle", "Stop")
    finalize(st, sid)
    ids_ = event_ids(st, sid)
    assert len(ids_) == 6

    done = deliverer(st, cfg, root).deliver(sid)
    assert done.delivered == ids_ and not (done.lease_busy or done.lease_lost or done.blocked)
    got = records(log)
    assert [r["payload"]["event_id"] for r in got] == ids_
    for record in got:
        validate(record["payload"], PAYLOAD_SCHEMA)
    payloads = [r["payload"] for r in got]
    assert [p["cause"] for p in payloads] == [
        "launch",
        "SessionStart",
        "PermissionRequest",
        "PostToolUse",
        "Stop",
        "exit",
    ]
    assert [p["final"] for p in payloads] == [False] * 5 + [True]
    assert payloads[2]["needs_input_reason"] == "permission"
    assert (payloads[2]["previous_state"], payloads[2]["previous_activity"]) == (
        "working",
        "working",
    )
    last = payloads[-1]
    assert (last["outcome"], last["end_reason"], last["exit_code"], last["state"]) == (
        "exited-unreported",
        "no_report",
        14,
        "exited-unreported",
    )
    assert last["live"] is False and last["cost_usd"] == 0.25
    assert all(p["outcome"] is None and p["cost_usd"] is None for p in payloads[:-1])
    assert all(p["object_id"] == sid and p["run"] == 1 for p in payloads)
    assert payloads[0]["labels"] == ["kind=implement", "team=x"]
    event_ts = [r[0] for r in st.conn.execute("SELECT event_ts FROM events ORDER BY id")]
    assert [p["event_ts"] for p in payloads] == event_ts
    assert all(p["ts"] >= p["event_ts"] for p in payloads)
    assert undelivered(st, sid) == []
    # A second delivery has nothing to do: each real change fires once.
    assert deliverer(st, cfg, root).deliver(sid).delivered == []
    assert len(records(log)) == 6
    assert st.lease_holder(toolhooks.lease_name(sid)) is None  # released


def test_events_no_entry_selects_are_marked_delivered_without_running_anything(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log), "states": ["needs-input"]}))
    sid = allocate(st, root)
    change(st, sid, "working", "working", "SessionStart")
    change(st, sid, "needs-input", "working", "PermissionRequest", reason="permission")
    change(st, sid, "working", "working", "PostToolUse")
    done = deliverer(st, cfg, root).deliver(sid)
    assert len(done.delivered) == 4 and undelivered(st, sid) == []
    assert [r["payload"]["cause"] for r in records(log)] == ["PermissionRequest"]


def test_without_any_hook_events_are_still_marked_delivered(
    tmp_path: Path, root: Path, st: Store
) -> None:
    cfg = load_config(tmp_path)
    gw = FakeGateway()  # any run would fail loudly
    sid = allocate(st, root)
    finalize(st, sid)
    assert len(deliverer(st, cfg, root, gateway=gw).deliver(sid).delivered) == 2
    assert gw.calls == []


def test_hooks_run_in_the_state_root_with_the_documented_environment(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(
        tmp_path, hooks_config({"cmd": hook_cmd(log)}, extra="secret_env: [VAULT_TOKEN]\n")
    )
    sid = allocate(st, root)
    environ = {
        **os.environ,
        "VAULT_TOKEN": "vault-token-value-0123456789",
        "CSESSION_DIR": "/somewhere",
        "CSESSION_KIND": "review",
        "PATH": f"relative-dir:{os.environ['PATH']}",
    }
    deliverer(st, cfg, root, environ=environ).deliver(sid)
    (record,) = records(log)
    assert record["cwd"] == str(root)
    env = record["env"]
    assert "VAULT_TOKEN" not in env and "CSESSION_DIR" not in env and "CSESSION_KIND" not in env
    assert (env["CSESSION_HOOK"], env["CSESSION_ID"], env["CSESSION_OBJECT_TYPE"]) == (
        "post-state-change",
        sid,
        "session",
    )
    assert all(os.path.isabs(p) for p in env["PATH"].split(":"))
    assert record["pgid"] != os.getpgid(0)  # its own process group
    delivered = [r for r in log_records(root) if r["event"] == "hook_delivered"]
    assert len(delivered) == 1 and delivered[0]["csession_id"] == sid
    assert "post_hook" not in delivered[0]["message"]  # entries are named, never their command


def test_entries_run_in_config_order_then_invocation_entries(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(
        tmp_path,
        hooks_config(
            {"cmd": hook_cmd(log, "--name", "c1")}, {"cmd": hook_cmd(log, "--name", "c2")}
        ),
    )
    invocation = {
        "hooks": [
            "post-state-change=" + hook_cmd(log, "--name", "i1"),
            "pre-launch=" + hook_cmd(log, "--name", "never"),
        ],
        "hook_timeouts": [],
    }
    sid = allocate(st, root, invocation=invocation)
    deliverer(st, cfg, root).deliver(sid)
    assert [r["name"] for r in records(log)] == ["c1", "c2", "i1"]


def test_late_delivery_uses_the_labels_stored_with_the_event(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log), "labels": ["team=x"]}))
    sid = allocate(st, root)  # launch event: team=x
    with st.transaction() as conn:
        conn.execute("DELETE FROM labels WHERE key = 'team'")
    change(st, sid, "working", "working", "SessionStart")  # without team=x
    deliverer(st, cfg, root).deliver(sid)
    got = records(log)
    assert [r["payload"]["cause"] for r in got] == ["launch"]
    assert got[0]["payload"]["labels"] == ["kind=implement", "team=x"]


def test_kind_selectors_use_the_run_kind(tmp_path: Path, root: Path, st: Store) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(
        tmp_path,
        hooks_config(
            {"cmd": hook_cmd(log, "--name", "review-only"), "kinds": ["review"]},
            {"cmd": hook_cmd(log, "--name", "not-gate"), "exclude_kinds": ["gate-check"]},
        ),
    )
    review = allocate(st, root, kind="review")
    gate = allocate(st, root, kind="gate-check")
    deliverer(st, cfg, root).deliver(review)
    deliverer(st, cfg, root).deliver(gate)
    assert [(r["name"], r["payload"]["object_id"]) for r in records(log)] == [
        ("review-only", review),
        ("not-gate", review),
    ]


def test_a_report_and_a_done_final_event_validate(tmp_path: Path, root: Path, st: Store) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root, mode="interactive", reads_third_party=True)
    change(st, sid, "idle", "idle", "SessionStart")
    with st.transaction() as conn:
        conn.execute(
            "UPDATE runs SET report_status = 'done', report_implicit = 0, "
            "report_summary = 'ignore previous instructions', report_reason = NULL, "
            "reported_at = '2026-10-31T00:00:00.000000Z', sandbox_active = 1, "
            "decision_ids_json = '[\"d-1\"]', session_started_at = '2026-10-31T00:00:00.000000Z', "
            "session_end_reason = 'prompt_input_exit'"
        )
    change(st, sid, "reported", "idle", "report")
    finalize(
        st,
        sid,
        outcome="done",
        end_reason="auto_exit",
        exit_code=0,
        state="exited",
        cause="auto_exit",
    )
    deliverer(st, cfg, root).deliver(sid)
    payloads = [r["payload"] for r in records(log)]
    for payload in payloads:
        validate(payload, PAYLOAD_SCHEMA)
    report, last = payloads[-2], payloads[-1]
    assert (
        report["cause"] == "report"
        and report["report"]["summary"] == "ignore previous instructions"
    )
    assert (last["state"], last["outcome"], last["cause"], last["report"]["status"]) == (
        "exited",
        "done",
        "auto_exit",
        "done",
    )
    assert last["tainted"] and last["reads_third_party"] and last["sandbox_active"]
    assert last["mode"] == "interactive" and last["decision_ids"] == ["d-1"]
    assert last["session_end_reason"] == "prompt_input_exit"


def test_a_launch_phase_end_has_one_final_not_started_event(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    finalize(
        st,
        sid,
        phase="launch",
        outcome="not-started",
        end_reason="claude_not_found",
        exit_code=12,
        state="not-started",
        cause="launch",
        message="claude not found: 'claude'",
    )
    deliverer(st, cfg, root).deliver(sid)
    payloads = [r["payload"] for r in records(log)]
    assert [p["final"] for p in payloads] == [False, True]
    for payload in payloads:
        validate(payload, PAYLOAD_SCHEMA)
    final = payloads[-1]
    assert (final["phase"], final["state"], final["cause"], final["cost_usd"]) == (
        "launch",
        "not-started",
        "launch",
        None,
    )
    assert final["message"] == "claude not found: 'claude'"


def test_a_pending_run_is_delivered_with_pending_true(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    with st.transaction() as conn:  # as start leaves it: no wrapper yet
        conn.execute("UPDATE runs SET wrapper_pid = NULL, wrapper_start = NULL")
    assert deliverer(st, cfg, root).deliver(sid).blocked is False
    (record,) = records(log)
    validate(record["payload"], PAYLOAD_SCHEMA)
    assert record["payload"]["pending"] is True and record["payload"]["phase"] == "launch"


# Failures, timeouts and the process group


def test_a_failing_hook_is_logged_and_never_fatal(tmp_path: Path, root: Path, st: Store) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(
        tmp_path,
        hooks_config(
            {"cmd": hook_cmd(log, "--name", "bad", "--exit", "3", "--stderr-bytes", "200000")},
            {"cmd": hook_cmd(log, "--name", "good")},
            {"cmd": "exit 4"},
        ),
    )
    sid = allocate(st, root)
    done = deliverer(st, cfg, root).deliver(sid)
    assert len(done.delivered) == 1
    assert [r["name"] for r in records(log)] == ["bad", "good"]
    failed = [r for r in log_records(root) if r["event"] == "hook_failed"]
    assert [r["message"] for r in failed] == [
        f"post-state-change config entry 1, event {done.delivered[0]}: exit 3",
        f"post-state-change config entry 3, event {done.delivered[0]}: exit 4",
    ]
    assert all(r["level"] == "warn" for r in failed)
    assert all("e" * 50 not in json.dumps(r) for r in log_records(root))  # output never logged


def test_a_missing_shell_fails_the_entry_and_still_delivers(
    tmp_path: Path, root: Path, st: Store
) -> None:
    cfg = load_config(tmp_path, hooks_config({"cmd": "true"}))
    sid = allocate(st, root)
    done = deliverer(st, cfg, root, environ={"PATH": str(tmp_path / "empty")}).deliver(sid)
    assert len(done.delivered) == 1
    (failed,) = [r for r in log_records(root) if r["event"] == "hook_failed"]
    assert "sh not found" in failed["message"]


@pytest.mark.skipif(not LINUX, reason="process groups are checked through /proc")
def test_a_timed_out_hook_group_gets_sigterm_then_sigkill(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cmd = hook_cmd(log, "--ignore-term", "--child", "--sleep", "30")
    cfg = load_config(
        tmp_path, hooks_config({"cmd": cmd, "timeout": "1s"}, extra="kill_grace: 0.5s\n")
    )
    sid = allocate(st, root)
    start = time.monotonic()
    done = deliverer(st, cfg, root).deliver(sid)
    elapsed = time.monotonic() - start
    assert 1.0 <= elapsed < 10
    assert len(done.delivered) == 1  # delivered: the hook got its payload
    (record,) = records(log)
    # Both the hook (ignoring SIGTERM) and its child in the group are gone.
    wait_until(lambda: not running(record["pid"]) and not running(record["child"]), 5)
    (timed_out,) = [r for r in log_records(root) if r["event"] == "hook_timeout"]
    assert timed_out["level"] == "warn" and "timed out after 1 s" in timed_out["message"]


# The lease


def test_another_live_holder_of_the_lease_means_nothing_is_delivered(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    with Store.open(st.path) as other:
        assert other.take_lease(toolhooks.lease_name(sid))
        done = deliverer(st, cfg, root).deliver(sid)
        assert done.lease_busy and done.delivered == [] and records(log) == []
        assert other.lease_holder(toolhooks.lease_name(sid)) is not None  # untouched
    assert undelivered(st, sid) != []


def test_a_dead_holders_lease_is_taken_over_at_once(tmp_path: Path, root: Path, st: Store) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead = store.Holder(pid=proc.pid, start=store.start_time(proc.pid, zombie=True))
    proc.wait()
    with Store.open(st.path, holder=dead) as other:
        assert other.take_lease(toolhooks.lease_name(sid))
    done = deliverer(st, cfg, root).deliver(sid)
    assert not done.lease_busy and len(done.delivered) == 1


def test_a_lease_lost_mid_pass_leaves_the_rest_undelivered(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    marker = tmp_path / "go"
    cfg = load_config(
        tmp_path,
        hooks_config({"cmd": hook_cmd(log, "--wait-for", str(marker))}),
    )
    sid = allocate(st, root)
    change(st, sid, "working", "working", "SessionStart")
    first = event_ids(st, sid)[0]
    result: list[toolhooks.Delivery] = []

    def deliver_in_thread() -> None:  # a store connection belongs to the thread that opened it
        with Store.open(st.path) as own:
            result.append(deliverer(own, cfg, root).deliver(sid))

    thread = threading.Thread(target=deliver_in_thread)
    thread.start()
    try:
        wait_until(lambda: len(records(log)) == 1)
        with st.transaction() as conn:  # taken over (e.g. after an expiry)
            conn.execute("UPDATE leases SET holder_token = 'someone-else'")
    finally:
        marker.touch()
        thread.join(20)
    (done,) = result
    assert done.lease_lost and done.delivered == []
    assert undelivered(st, sid)[0] == first  # stays undelivered: at least once


def test_the_lease_is_renewed_while_a_long_hook_runs(tmp_path: Path, root: Path, st: Store) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log, "--sleep", "1")}))
    sid = allocate(st, root)
    now = [store.utc_now()]
    d = deliverer(st, cfg, root, renew_interval=0.1)
    renewals: list[bool] = []
    real_renew = st.renew_lease

    def renew(name: str) -> bool:
        now[0] += timedelta(seconds=30)  # each renewal happens "30 s later"
        renewals.append(real_renew(name))
        return renewals[-1]

    st.clock = lambda: now[0]
    st.renew_lease = renew  # type: ignore[method-assign]
    done = d.deliver(sid)
    # Without renewals during the 1 s hook the lease would have expired (2 x 30 s > 60 s).
    assert len(renewals) >= 3 and all(renewals)
    assert len(done.delivered) == 1


def test_a_renewal_store_error_mid_hook_keeps_waiting_and_order(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log, "--sleep", "0.5")}))
    sid = allocate(st, root)
    change(st, sid, "working", "working", "SessionStart")
    real_renew = st.renew_lease
    calls: list[int] = []

    def renew(name: str) -> bool:
        calls.append(1)
        if len(calls) == 2:  # the first renewal while the first hook runs
            raise sqlite3.OperationalError("database is locked")
        return real_renew(name)

    st.renew_lease = renew  # type: ignore[method-assign]
    done = deliverer(st, cfg, root, renew_interval=0.1).deliver(sid)
    assert done.delivered == event_ids(st, sid) and not done.lease_lost
    first, second = records(log)
    assert [first["payload"]["event_id"], second["payload"]["event_id"]] == done.delivered
    assert second["time"] >= first["time"] + 0.5  # started after the first one ended
    warned = [r for r in log_records(root) if r["event"] == "hook_failed"]
    assert len(warned) == 1 and "renewal failed (OperationalError)" in warned[0]["message"]


def hold_lease_for(st: Store, sid: str, seconds: float) -> threading.Thread:
    """Another store takes the session's delivery lease, then releases it after ``seconds``."""
    taken = threading.Event()

    def hold() -> None:
        with Store.open(st.path) as other:
            assert other.take_lease(toolhooks.lease_name(sid))
            taken.set()
            time.sleep(seconds)
            other.release_lease(toolhooks.lease_name(sid))

    thread = threading.Thread(target=hold)
    thread.start()
    assert taken.wait(10)
    return thread


def test_a_patient_delivery_waits_out_a_busy_lease(tmp_path: Path, root: Path, st: Store) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    finalize(st, sid)
    holder = hold_lease_for(st, sid, 1.0)
    try:
        done = toolhooks.deliver(
            st,
            cfg,
            sid,
            gateway=SystemGateway(),
            environ=dict(os.environ),
            state_root=str(root),
            patience=toolhooks.LEASE_TTL,
        )
    finally:
        holder.join(20)
    assert len(done.delivered) == 2 and undelivered(st, sid) == []


def test_a_patient_delivery_gives_up_after_its_patience(
    tmp_path: Path, root: Path, st: Store
) -> None:
    cfg = load_config(tmp_path)
    sid = allocate(st, root)
    holder = hold_lease_for(st, sid, 1.5)
    try:
        done = deliverer(st, cfg, root).deliver_all(sid, patience=0.5)
    finally:
        holder.join(20)
    assert done.lease_busy and undelivered(st, sid) != []
    (gave_up,) = [r for r in log_records(root) if r["event"] == "hook_failed"]
    assert "gave up after 0.5 s (the delivery lease is busy)" in gave_up["message"]


def test_a_store_error_and_a_failed_release_are_retried_and_the_lease_taken_again(
    tmp_path: Path, root: Path, st: Store
) -> None:
    cfg = load_config(tmp_path)
    sid = allocate(st, root)
    finalize(st, sid)
    real_mark, real_release = st.mark_delivered, st.release_lease
    failures = {"mark": 1, "release": 1}

    def mark(event_id: int, lease: str) -> bool:
        if failures["mark"]:
            failures["mark"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_mark(event_id, lease)

    def release(name: str) -> None:
        if failures["release"]:
            failures["release"] -= 1
            raise sqlite3.OperationalError("database is locked")
        real_release(name)

    st.mark_delivered = mark  # type: ignore[method-assign]
    st.release_lease = release  # type: ignore[method-assign]
    done = deliverer(st, cfg, root).deliver_all(sid, patience=toolhooks.LEASE_TTL)
    # The lease this store could not release is still its own: the retry takes it again.
    assert len(done.delivered) == 2 and undelivered(st, sid) == []
    assert failures == {"mark": 0, "release": 0}
    assert st.lease_holder(toolhooks.lease_name(sid)) is None


# Live wrappers


def test_an_event_of_another_live_wrapper_stops_late_delivery(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        sid = allocate(st, root, wrapper_pid=proc.pid, wrapper_start=store.start_time(proc.pid))
        done = deliverer(st, cfg, root).deliver(sid)
        assert done.blocked and done.delivered == [] and records(log) == []
    finally:
        proc.kill()
        proc.wait()
    # Its wrapper is dead now: the next delivering command delivers.
    assert len(deliverer(st, cfg, root).deliver(sid).delivered) == 1


# Async entries


def test_an_async_entry_does_not_hold_up_the_next_one(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    marker = tmp_path / "go"
    cfg = load_config(
        tmp_path,
        hooks_config(
            {"cmd": hook_cmd(log, "--name", "slow", "--wait-for", str(marker)), "async": True},
            {"cmd": hook_cmd(log, "--name", "fast")},
        ),
    )
    sid = allocate(st, root)
    d = deliverer(st, cfg, root)
    try:
        start = time.monotonic()
        done = d.deliver(sid, wait_async=False)
        # Well under the slow hook's 20 s wait: it held up neither the next entry nor deliver.
        assert time.monotonic() - start < 10
        assert len(done.delivered) == 1
        assert "fast" in {r["name"] for r in records(log)}  # before the slow one may end
        wait_until(lambda: {r["name"] for r in records(log)} == {"slow", "fast"})
    finally:
        marker.touch()
        d.wait_async()


class _BlockingGateway:
    """A gateway whose ``run`` blocks before the hook gets its payload, until released."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, argv: Any, **kw: Any) -> Result:
        self.entered.set()
        assert self.release.wait(20)
        kw["on_started"]()
        return Result(tuple(argv), 0, "", "")

    def spawn(self, argv: Any, **kw: Any) -> Any:
        raise AssertionError("hooks are run, never spawned")


def test_an_async_event_is_marked_only_once_its_hook_got_the_payload(
    tmp_path: Path, root: Path, st: Store
) -> None:
    cfg = load_config(tmp_path, hooks_config({"cmd": "true", "async": True}))
    sid = allocate(st, root)
    (launch,) = event_ids(st, sid)
    gw = _BlockingGateway()
    result: list[toolhooks.Delivery] = []

    def deliver_in_thread() -> None:
        with Store.open(st.path) as own:
            result.append(deliverer(own, cfg, root, gateway=gw).deliver(sid))

    thread = threading.Thread(target=deliver_in_thread)
    thread.start()
    try:
        assert gw.entered.wait(10)
        time.sleep(0.3)
        # A deliverer killed now must leave the event for the next one: not yet marked.
        assert undelivered(st, sid) == [launch]
    finally:
        gw.release.set()
        thread.join(20)
    assert result[0].delivered == [launch] and undelivered(st, sid) == []


def test_an_async_hook_that_cannot_start_is_logged_and_the_event_delivered(
    tmp_path: Path, root: Path, st: Store
) -> None:
    cfg = load_config(tmp_path, hooks_config({"cmd": "true", "async": True}))
    gw = FakeGateway()
    gw.on(["sh"], error=OSError("no such file"))
    sid = allocate(st, root)
    done = deliverer(st, cfg, root, gateway=gw).deliver(sid)
    assert len(done.delivered) == 1
    (failed,) = [r for r in log_records(root) if r["event"] == "hook_failed"]
    assert "could not start sh (OSError)" in failed["message"]


@pytest.mark.skipif(not LINUX, reason="process groups are checked through /proc")
def test_an_async_hook_timeout_ends_its_process_group(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cmd = hook_cmd(log, "--ignore-term", "--child", "--sleep", "30")
    cfg = load_config(
        tmp_path,
        hooks_config({"cmd": cmd, "timeout": "1s", "async": True}, extra="kill_grace: 0.5s\n"),
    )
    sid = allocate(st, root)
    start = time.monotonic()
    done = deliverer(st, cfg, root).deliver(sid)  # waits for the async hook
    assert 1.0 <= time.monotonic() - start < 10
    assert len(done.delivered) == 1
    (record,) = records(log)
    wait_until(lambda: not running(record["pid"]) and not running(record["child"]), 5)
    assert [r["event"] for r in log_records(root) if r["level"] == "warn"] == ["hook_timeout"]


def test_finished_async_threads_are_dropped_as_new_ones_start(
    tmp_path: Path, root: Path, st: Store
) -> None:
    cfg = load_config(tmp_path, hooks_config({"cmd": "true", "async": True}))
    gw = FakeGateway()
    gw.on(["sh"])
    sid = allocate(st, root)
    for n in range(5):
        state = "working" if n % 2 else "idle"
        change(st, sid, state, state, "Stop")
    d = deliverer(st, cfg, root, gateway=gw)
    assert len(d.deliver(sid, wait_async=False).delivered) == 6
    assert len(d._async) <= 2  # pyright: ignore[reportPrivateUsage]
    d.wait_async()


# Payload time, redelivery, background output, long timeouts and runner errors


def test_each_entry_gets_its_own_delivery_time(tmp_path: Path, root: Path, st: Store) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(
        tmp_path,
        hooks_config(
            {"cmd": hook_cmd(log, "--name", "first", "--sleep", "0.3")},
            {"cmd": hook_cmd(log, "--name", "second")},
        ),
    )
    sid = allocate(st, root)
    deliverer(st, cfg, root).deliver(sid)
    first, second = (r["payload"] for r in records(log))
    assert second["ts"] > first["ts"]
    assert first["event_ts"] == second["event_ts"]


def test_a_redelivered_event_keeps_its_event_fields(tmp_path: Path, root: Path, st: Store) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    change(st, sid, "idle", "idle", "Stop")
    deliverer(st, cfg, root).deliver(sid)
    with st.transaction() as conn:  # a crash before the mark, and the run moved on meanwhile
        conn.execute("UPDATE events SET delivered_at = NULL")
        conn.execute(
            "UPDATE runs SET report_status = 'done', report_implicit = 0, "
            "report_summary = 'later', reported_at = '2026-10-31T00:00:00.000000Z'"
        )
    deliverer(st, cfg, root).deliver(sid)
    got = [r["payload"] for r in records(log)]
    assert len(got) == 4
    event_fields = [
        "event_id",
        "event_ts",
        "state",
        "activity",
        "needs_input_reason",
        "previous_state",
        "previous_activity",
        "cause",
        "final",
        "live",
        "labels",
    ]
    for before, again in ((got[0], got[2]), (got[1], got[3])):
        assert {k: before[k] for k in event_fields} == {k: again[k] for k in event_fields}
        assert before["report"] is None and again["report"]["summary"] == "later"


@pytest.mark.skipif(not LINUX, reason="the background process is checked through /proc")
def test_a_hook_leaving_a_process_in_the_background_finishes_when_sh_exits(
    tmp_path: Path, root: Path, st: Store
) -> None:
    pidfile = tmp_path / "pid"
    cmd = f"/bin/sleep 30 & echo $! > {shlex.quote(str(pidfile))}; exit 0"
    cfg = load_config(tmp_path, hooks_config({"cmd": cmd, "timeout": "2s"}))
    sid = allocate(st, root)
    start = time.monotonic()
    try:
        done = deliverer(st, cfg, root).deliver(sid)
        assert time.monotonic() - start < 2
        assert len(done.delivered) == 1
        hooks = [r for r in log_records(root) if r["event"].startswith("hook_")]
        assert [r["event"] for r in hooks] == ["hook_delivered"]
        assert hooks[0]["message"].endswith(": exit 0")
    finally:
        wait_until(lambda: pidfile.exists() and pidfile.read_text().strip() != "", 5)
        pid = int(pidfile.read_text())

        def argv0() -> bytes:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    return fh.read().split(b"\0")[0]
            except OSError:
                return b""

        # The backgrounded subshell may not have exec'd sleep yet: wait (bounded) until it
        # has, and signal only the exact sleep this test started.
        wait_until(lambda: argv0().endswith(b"sleep"), 5)
        os.kill(pid, signal.SIGKILL)


def test_a_hook_timeout_longer_than_one_wait_delivers_normally(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log), "timeout": "30d"}))
    sid = allocate(st, root)
    assert len(deliverer(st, cfg, root).deliver(sid).delivered) == 1
    assert len(records(log)) == 1
    assert [r["event"] for r in log_records(root) if r["event"].startswith("hook_")] == [
        "hook_delivered"
    ]


class _BrokenGateway:
    def run(self, argv: Any, **kw: Any) -> Result:
        raise RuntimeError("a runner bug")

    def spawn(self, argv: Any, **kw: Any) -> Any:
        raise AssertionError("hooks are run, never spawned")


@pytest.mark.parametrize("async_", [False, True])
def test_a_runner_exception_is_logged_hook_failed_and_never_escapes(
    tmp_path: Path, root: Path, st: Store, monkeypatch: pytest.MonkeyPatch, async_: bool
) -> None:
    escaped: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", escaped.append)
    cfg = load_config(tmp_path, hooks_config({"cmd": "true", "async": async_}))
    sid = allocate(st, root)
    done = deliverer(st, cfg, root, gateway=_BrokenGateway()).deliver(sid)
    assert len(done.delivered) == 1 and escaped == []
    (failed,) = [r for r in log_records(root) if r["event"] == "hook_failed"]
    assert failed["message"].endswith("could not run the hook (RuntimeError)")
    assert "a runner bug" not in failed["message"]


def test_a_cmd_with_a_nul_byte_is_logged_hook_failed(
    tmp_path: Path, root: Path, st: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    escaped: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", escaped.append)
    sid = allocate(st, root)
    d = deliverer(st, load_config(tmp_path), root)
    d.entries = [Entry(cmd="true\x00false", timeout=5.0, origin="config", index=1)]
    assert len(d.deliver(sid).delivered) == 1 and escaped == []
    (failed,) = [r for r in log_records(root) if r["event"] == "hook_failed"]
    assert failed["message"].endswith("could not run the hook (ValueError)")


# Cancel


@pytest.mark.skipif(not LINUX, reason="process groups are checked through /proc")
def test_cancel_ends_the_running_hook_and_leaves_the_event_undelivered(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(
        tmp_path,
        hooks_config({"cmd": hook_cmd(log, "--child", "--sleep", "30")}, extra="kill_grace: 1s\n"),
    )
    sid = allocate(st, root)
    change(st, sid, "idle", "idle", "Stop")
    cancel = threading.Event()
    result: list[toolhooks.Delivery] = []

    def deliver_in_thread() -> None:
        with Store.open(st.path) as own:
            result.append(deliverer(own, cfg, root, cancel=cancel).deliver(sid))

    thread = threading.Thread(target=deliver_in_thread)
    thread.start()
    wait_until(lambda: len(records(log)) == 1)
    start = time.monotonic()
    cancel.set()
    thread.join(20)
    assert time.monotonic() - start < 1 + 3
    (done,) = result
    assert done.canceled and done.delivered == []
    (record,) = records(log)
    wait_until(lambda: not running(record["pid"]) and not running(record["child"]), 5)
    assert len(undelivered(st, sid)) == 2  # the interrupted event and the next one
    assert st.lease_holder(toolhooks.lease_name(sid)) is None
    events = [r["event"] for r in log_records(root) if r["event"].startswith("hook_")]
    assert events == ["hook_failed"]  # never hook_delivered


# Real concurrent processes and crashes


DELIVER = """\
import os, sys
from claude_session import config, store, toolhooks
from claude_session.gateway import Result, SystemGateway
db, cfg_path, sid = sys.argv[1:4]
cfg = config.load(cfg_path)
with store.Store.open(db) as st:
    done = toolhooks.deliver(
        st, cfg, sid, gateway=SystemGateway(), environ=dict(os.environ),
        state_root=os.path.dirname(db),
    )
print(len(done.delivered), done.lease_busy, flush=True)
"""


def deliver_process(st: Store, cfg_path: Path, sid: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", DELIVER, st.path, str(cfg_path), sid],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_concurrent_deliverers_never_deliver_an_event_twice(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    load_config(tmp_path, hooks_config({"cmd": hook_cmd(log, "--sleep", "0.05")}))
    sid = allocate(st, root)
    for n in range(10):
        change(st, sid, "working" if n % 2 else "idle", "working" if n % 2 else "idle", "Stop")
    finalize(st, sid)  # this test process is the wrapper: a live one would block delivery
    expected = event_ids(st, sid)
    procs = [deliver_process(st, tmp_path / "config.yaml", sid) for _ in range(4)]
    for proc in procs:
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
    got = [r["payload"]["event_id"] for r in records(log)]
    # Without a crash every event runs exactly once, in order, whoever delivered it.
    assert got == expected
    assert undelivered(st, sid) == []


@pytest.mark.skipif(not LINUX, reason="start ticks come from /proc")
def test_a_deliverer_killed_mid_hook_is_redelivered_from_that_event(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    release = tmp_path / "release"
    blocking = tmp_path / "block.jsonl"
    # The second event's hook blocks until released; every hook logs its payload first.
    block = hook_cmd(blocking, "--wait-for", str(release))
    script = textwrap.dedent(
        f"""\
        payload=$(cat)
        printf '%s\\n' "$payload" | {hook_cmd(log)}
        case "$payload" in
          *'"cause": "SessionStart"'*) printf '%s\\n' "$payload" | {block} ;;
        esac
        """
    )
    load_config(tmp_path, hooks_config({"cmd": script}))
    sid = allocate(st, root)
    change(st, sid, "working", "working", "SessionStart")
    change(st, sid, "idle", "idle", "Stop")
    finalize(st, sid)  # this test process is the wrapper: a live one would block delivery
    launch_id, start_id, stop_id, exit_id = event_ids(st, sid)
    victim = deliver_process(st, tmp_path / "config.yaml", sid)
    hook_pid: int | None = None
    try:
        wait_until(lambda: len(records(blocking)) == 1)
        hook_pid = records(blocking)[0]["pid"]
        victim.kill()  # the exact deliverer this test started, mid-hook
        victim.wait(10)
        # The dead deliverer's lease is taken over at once; delivery resumes at the event it
        # had not marked: that event runs again (at least once), then the rest, in order.
        done = toolhooks.deliver(
            st,
            load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)})),
            sid,
            gateway=SystemGateway(),
            environ=dict(os.environ),
            state_root=str(root),
        )
        assert not done.lease_busy
        assert done.delivered == [start_id, stop_id, exit_id]
        got = [r["payload"]["event_id"] for r in records(log)]
        assert got == [launch_id, start_id, start_id, stop_id, exit_id]
        assert undelivered(st, sid) == []
    finally:
        release.touch()  # the orphaned hook from the killed deliverer ends on its own
        if hook_pid is not None:
            wait_until(lambda: not running(hook_pid), 25)


# The worker


def test_the_worker_delivers_on_wake_and_everything_on_close(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    worker = toolhooks.start_worker(
        st, cfg, sid, gateway=SystemGateway(), environ=dict(os.environ), state_root=str(root)
    )
    try:
        worker.wake()
        wait_until(lambda: len(records(log)) == 1)
        change(st, sid, "working", "working", "SessionStart")
        finalize(st, sid)
    finally:
        worker.close()
    assert [r["payload"]["cause"] for r in records(log)] == ["launch", "SessionStart", "exit"]
    assert records(log)[-1]["payload"]["final"] is True
    assert undelivered(st, sid) == []
    worker.close()  # idempotent


def test_the_worker_close_waits_out_a_busy_lease_for_the_final_event(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    holder = hold_lease_for(st, sid, 1.0)  # e.g. a reconcile delivering at the same time
    worker = toolhooks.start_worker(
        st, cfg, sid, gateway=SystemGateway(), environ=dict(os.environ), state_root=str(root)
    )
    try:
        finalize(st, sid)
        start = time.monotonic()
        worker.close()
        assert time.monotonic() - start < 10
    finally:
        holder.join(20)
    assert undelivered(st, sid) == []
    assert records(log)[-1]["payload"]["final"] is True


def test_a_canceled_worker_stops_without_delivering_the_rest(
    tmp_path: Path, root: Path, st: Store
) -> None:
    log = tmp_path / "hook.jsonl"
    cfg = load_config(tmp_path, hooks_config({"cmd": hook_cmd(log)}))
    sid = allocate(st, root)
    cancel = threading.Event()
    cancel.set()
    worker = toolhooks.start_worker(
        st,
        cfg,
        sid,
        gateway=SystemGateway(),
        environ=dict(os.environ),
        state_root=str(root),
        cancel=cancel,
    )
    finalize(st, sid)
    worker.close()
    assert records(log) == [] and len(undelivered(st, sid)) == 2


def test_a_worker_that_cannot_open_the_store_logs_and_never_raises(
    root: Path, tmp_path: Path
) -> None:
    cfg = load_config(tmp_path)

    def broken() -> Store:
        raise OSError("no database")

    worker = toolhooks.Worker(
        broken,
        cfg,
        "cs-abcd2345",
        gateway=FakeGateway(),
        environ={},
        state_root=str(root),
    ).start()
    worker.close()
    (failed,) = [r for r in log_records(root) if r["event"] == "hook_failed"]
    assert "delivery failed (OSError)" in failed["message"]


def test_a_worker_never_started_closes_quietly(root: Path, tmp_path: Path) -> None:
    worker = toolhooks.Worker(
        lambda: Store.open(str(root / "state.db")),
        load_config(tmp_path),
        "cs-abcd2345",
        gateway=FakeGateway(),
        environ={},
        state_root=str(root),
    )
    worker.close()


def test_log_records_validate() -> None:
    # hook_delivered, hook_failed and hook_timeout are log.json events.
    for event in ("hook_delivered", "hook_failed", "hook_timeout"):
        assert event in logs.EVENTS
