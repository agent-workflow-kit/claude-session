"""Interactive sessions in a private tmux (B rows of design.md): the trust dialog, permission
prompts and notifications, AskUserQuestion, `/clear` and `/compact`, and the exit codes of
signals and Ctrl-C (#41 comments from #56, #57, #58, #67).

Each test starts `claude` directly as the pane's process (no shell), so the pane PID is the exact
PID of `claude` and signals go to it alone. The tmux server uses `-f /dev/null`, a private socket
and the private environment; the user's real tmux is never touched. Directories are trusted
through the private `~/.claude.json` unless a test is about the dialog itself."""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ..fakeclaude import recorder
from .harness import LiveEnv, events_named, hook_group, read_log, wait_for
from .tmux_driver import Tmux

ALL_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)


class Session:
    def __init__(self, live: LiveEnv, tmux: Tmux) -> None:
        self.live = live
        self.tmux = tmux
        self.work = live.dir("work")
        self.log = live.root / "hooks.jsonl"
        self.notify = {
            kind: live.root / f"notify-{kind}.jsonl"
            for kind in ("permission_prompt", "idle_prompt", "elicitation_dialog")
        }

    def settings(self) -> Path:
        hooks: dict[str, Any] = {
            event: [hook_group(recorder(self.log, args=[event]))] for event in ALL_EVENTS
        }
        hooks["Notification"] = [
            hook_group(recorder(path, args=["Notification"]), matcher=kind)
            for kind, path in self.notify.items()
        ]
        return self.live.write_json("settings.json", {"hooks": hooks})

    def start(self, *extra: str, trusted: bool = True) -> None:
        self.live.trust(*([self.work] if trusted else []))
        argv = [self.live.claude, "--model", self.live.model, "--setting-sources", "",
                "--settings", str(self.settings()), *extra]  # fmt: skip
        self.tmux.start(argv, cwd=self.work)

    def events(self, name: str) -> list[dict[str, Any]]:
        return [dict(p) for p in events_named(read_log(self.log), name)]

    def wait_event(self, name: str, timeout: float, count: int = 1) -> bool:
        return wait_for(lambda: len(self.events(name)) >= count, timeout)

    def ready(self) -> None:
        assert self.wait_event("SessionStart", 90), self.tmux.capture()
        time.sleep(3)  # the input box is up shortly after SessionStart

    def submit(self, text: str) -> None:
        self.tmux.send_text(text)
        time.sleep(0.5)
        self.tmux.send_keys("Enter")


@pytest.fixture
def session(live: LiveEnv) -> Iterator[Session]:
    import shutil

    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    tmux = Tmux(live.env(), live.root / "t.sock")
    try:
        yield Session(live, tmux)
    finally:
        tmux.kill()


@pytest.mark.live
def test_the_trust_dialog_fires_no_hooks_ignores_ctrl_c_and_escape_exits_0(
    session: Session,
) -> None:
    """B2: the dialog appears even with `--setting-sources ""`, fires no hooks, Ctrl-C is
    ignored and Esc exits 0."""
    session.start(trusted=False)
    assert session.tmux.wait_for("trust", 60), session.tmux.capture()
    session.tmux.send_keys("C-c")
    time.sleep(2)
    assert not session.tmux.state().dead, "Ctrl-C closed the trust dialog"
    session.tmux.send_keys("Escape")
    state = session.tmux.wait_dead(30)
    assert state is not None and state.exit_status == 0
    assert session.events("SessionStart") == []


@pytest.mark.live
def test_permission_prompt_notifications_matchers_and_denial(session: Session) -> None:
    """B and #56 item 2, #57, #58 item 1: `PermissionRequest` when the prompt appears; the
    `permission_prompt` notification follows; `idle_prompt` about 60 s after Stop; approval
    fires `PostToolUse`, a denial fires nothing; each matcher sees only its own type."""
    session.start()
    session.ready()
    session.submit("Run `echo first` with the Bash tool.")
    assert session.wait_event("PermissionRequest", 90), session.tmux.capture()
    asked = session.events("PermissionRequest")[0]
    session.live.observe("PermissionRequest keys", sorted(asked))
    assert asked["tool_name"] == "Bash"
    assert wait_for(lambda: bool(read_log(session.notify["permission_prompt"])), 30)
    session.tmux.send_keys("Enter")  # the first option: yes
    assert session.wait_event("PostToolUse", 60), session.tmux.capture()
    done = session.events("PostToolUse")[0]
    assert isinstance(done.get("tool_use_id"), str)
    session.live.observe("PostToolUse matches PermissionRequest tool_use_id",
                         done.get("tool_use_id") == asked.get("tool_use_id"))  # fmt: skip
    assert session.wait_event("Stop", 90)
    stopped = time.monotonic()

    session.submit("Run `echo second` with the Bash tool.")
    assert session.wait_event("PermissionRequest", 90, count=2), session.tmux.capture()
    session.tmux.send_keys("Escape")  # deny
    time.sleep(5)
    assert len(session.events("PostToolUse")) == 1, "a denial fired PostToolUse"

    assert wait_for(lambda: bool(read_log(session.notify["idle_prompt"])), 150)
    live_gap = time.monotonic() - stopped
    session.live.observe(
        "seconds from first Stop to idle_prompt (includes a second turn)", round(live_gap)
    )
    for kind, path in session.notify.items():
        types = {p["notification_type"] for p in events_named(read_log(path), "Notification")}
        assert types <= {kind}, (kind, types)
        for payload in events_named(read_log(path), "Notification"):
            session.live.observe(f"Notification message ({kind})", payload.get("message"))
    assert not read_log(session.notify["elicitation_dialog"])

    session.submit("/exit")
    state = session.tmux.wait_dead(30)
    assert state is not None and state.exit_status == 0
    ends = session.events("SessionEnd")
    assert ends and ends[0]["reason"] == "prompt_input_exit", ends


@pytest.mark.live
def test_ask_user_question_is_a_permission_request_for_that_tool(session: Session) -> None:
    """B4 and #58 item 2: `PermissionRequest` with `tool_name: AskUserQuestion`; whether a
    `permission_prompt` notification follows is recorded (the table assumes it does)."""
    session.start()
    session.ready()
    session.submit("Use the AskUserQuestion tool to ask which fruit I prefer: apple or pear.")
    assert session.wait_event("PermissionRequest", 90), session.tmux.capture()
    asked = session.events("PermissionRequest")[0]
    assert asked["tool_name"] == "AskUserQuestion"
    assert "questions" in asked["tool_input"]
    followed = wait_for(lambda: bool(read_log(session.notify["permission_prompt"])), 30)
    session.live.observe("permission_prompt notification follows AskUserQuestion", followed)
    session.tmux.send_keys("Escape")
    time.sleep(3)
    assert session.events("PostToolUse") == [], "Esc on a question fired PostToolUse"


@pytest.mark.live
def test_clear_changes_the_id_and_compact_keeps_it(session: Session) -> None:
    """B3 and #76: the SessionStart `source` values `clear` and `compact` (and that no other
    value than the known five appears)."""
    session.start()
    session.ready()
    session.submit("Reply OK.")
    assert session.wait_event("Stop", 90)
    first = session.events("SessionStart")[0]["session_id"]
    session.submit("/compact")
    assert session.wait_event("SessionStart", 120, count=2), session.tmux.capture()
    compacted = session.events("SessionStart")[1]
    assert compacted["source"] == "compact" and compacted["session_id"] == first
    time.sleep(3)
    session.submit("/clear")
    assert session.wait_event("SessionStart", 60, count=3), session.tmux.capture()
    cleared = session.events("SessionStart")[2]
    assert cleared["source"] == "clear" and cleared["session_id"] != first
    sources = {p["source"] for p in session.events("SessionStart")}
    assert sources <= {"startup", "resume", "clear", "compact", "fork"}


@pytest.mark.live
def test_double_ctrl_c_exit_code(session: Session) -> None:
    """#67 item 1: the end mapping treats 130 as `killed`; what claude really exits with after
    two Ctrl-C is recorded and must be 0 or 130."""
    session.start()
    session.ready()
    session.tmux.send_keys("C-c")
    time.sleep(0.5)
    assert not session.tmux.state().dead, "one Ctrl-C at an idle prompt must not exit"
    session.tmux.send_keys("C-c")
    state = session.tmux.wait_dead(30)
    assert state is not None, session.tmux.capture()
    session.live.observe("exit after double Ctrl-C", (state.exit_status, state.exit_signal))
    assert state.exit_status in (0, 130) or state.exit_signal == signal.SIGINT


@pytest.mark.live
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
@pytest.mark.parametrize(
    ("sig", "count", "accepted"),
    [
        (signal.SIGTERM, 1, {143}),
        (signal.SIGHUP, 1, {129, 0}),
        (signal.SIGHUP, 2, {129, 0}),
    ],
    ids=["sigterm", "sighup", "two-sighups"],
)
def test_signals_end_interactive_claude_cleanly(
    session: Session, sig: signal.Signals, count: int, accepted: set[int]
) -> None:
    """B (SIGTERM exits 143, SessionEnd reason `other`, SIGHUP 129), #57 (does SIGHUP fire
    SessionEnd; exit code or death by signal) and #67 item 2 (two SIGHUPs end it cleanly). The
    signal goes to the exact pane PID, which is `claude` itself."""
    session.start()
    session.ready()
    pid = session.tmux.state().pid
    argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode()
    assert argv0 == session.live.claude, "the pane process is not claude itself"
    for _ in range(count):
        os.kill(pid, sig)
        time.sleep(0.2)
    state = session.tmux.wait_dead(30)
    assert state is not None, session.tmux.capture()
    session.live.observe(
        f"{sig.name} x{count}: (exit, signal, SessionEnd)",
        (
            state.exit_status,
            state.exit_signal,
            [e.get("reason") for e in session.events("SessionEnd")],
        ),
    )
    assert state.exit_status in accepted or state.exit_signal == sig


@pytest.mark.live
def test_an_mcp_elicitation_fires_the_elicitation_dialog_notification(session: Session) -> None:
    """MCP elicitation (design.md, still to verify): a test MCP server (`mcp_elicit_server.py`)
    asks the user a question while its tool runs. The `Notification` hook with the
    `elicitation_dialog` matcher must fire, which `needs-input(elicitation)` relies on. The
    payload, the tool permission events and what Esc does to the pending request are recorded.
    The test fails before the notification only if the server never asked (the model did not
    call the tool), so a silent notification is told apart from a tool that never ran."""
    server_log = session.live.root / "elicit-server.log"
    config = session.live.write_json(
        "mcp.json",
        {
            "mcpServers": {
                "elicit": {
                    "command": sys.executable,
                    "args": [str(Path(__file__).with_name("mcp_elicit_server.py"))],
                    "env": {"ELICIT_SERVER_LOG": str(server_log)},
                }
            }
        },
    )
    session.start(
        f"--mcp-config={config}", "--strict-mcp-config", "--allowedTools=mcp__elicit__ask"
    )
    session.ready()
    session.submit(
        "Call the ask tool of the elicit MCP server once, with no arguments. Then tell me which "
        "action the user took."
    )

    def asked() -> bool:
        return server_log.exists() and "elicitation sent" in server_log.read_text(encoding="utf-8")

    assert wait_for(asked, 120), (
        "control failed: the MCP server never sent an elicitation",
        session.tmux.capture(),
    )
    session.live.observe(
        "MCP server log before the answer", server_log.read_text(encoding="utf-8").splitlines()
    )
    fired = wait_for(lambda: bool(read_log(session.notify["elicitation_dialog"])), 30)
    session.live.observe("elicitation screen", session.tmux.capture()[-600:])
    session.live.observe(
        "events around the elicitation",
        {
            "PermissionRequest": [e.get("tool_name") for e in session.events("PermissionRequest")],
            "permission_prompt": len(read_log(session.notify["permission_prompt"])),
            "elicitation_dialog": [dict(p) for p in read_log(session.notify["elicitation_dialog"])],
        },
    )
    assert fired, "the elicitation was pending but no elicitation_dialog notification fired"
    session.tmux.send_keys("Escape")
    wait_for(lambda: "answered" in server_log.read_text(encoding="utf-8"), 30)
    session.live.observe(
        "MCP server log after Esc", server_log.read_text(encoding="utf-8").splitlines()
    )
