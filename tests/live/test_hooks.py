"""Hook behaviour the injected hooks rely on, probed with recorder commands in `--settings`.

Covers: payload fields and the hook environment, omitted matchers, the `timeout` unit and the
kill sequence (A10), PostToolUse payloads and `PostToolUseFailure` (#58), subagent
`PostToolUse` (A2), parallel-tool hook ordering (A7) and the `Stop` payload (#57).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from claude_session import gateway, procinfo

from ..fakeclaude import recorder
from .harness import (
    LiveEnv,
    events_named,
    hook_group,
    kill_recorded,
    read_log,
    tool_names,
    wait_for,
)


def note(live: LiveEnv) -> Path:
    work = live.dir("work")
    (work / "note.txt").write_text("walnut is the word\n", encoding="utf-8")
    return work


@pytest.mark.live
def test_hook_payload_environment_cwd_and_shell(live: LiveEnv) -> None:
    """Hooks inherit claude's environment (`CSESSION_*`), run via `/bin/sh -c` in the session
    cwd, and get the common payload fields. The Stop and SessionEnd payload shapes are recorded
    for the fake claude's assumptions (#57)."""
    log = live.root / "hooks.jsonl"
    shell = live.root / "shell-name"
    hooks = {
        event: [hook_group(recorder(log, args=[event]))]
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
    }
    hooks["SessionStart"].append(hook_group(f'printf %s "$0" > {shell}'))
    settings = live.write_json("settings.json", {"hooks": hooks})
    work = live.dir("work")
    run = live.headless(
        "Reply OK.", "--settings", str(settings), cwd=work, extra_env={"CSESSION_ID": "cs-live1"}
    )
    records = read_log(log)
    by_event = {r["args"][0]: r for r in records}
    assert set(by_event) >= {"SessionStart", "UserPromptSubmit", "Stop"}, sorted(by_event)
    for name in ("SessionStart", "UserPromptSubmit", "Stop"):
        record = by_event[name]
        payload = record["payload"]
        assert payload["hook_event_name"] == name
        assert payload["session_id"] == run.session_id
        assert str(payload["transcript_path"]).endswith(f"{run.session_id}.jsonl")
        assert os.path.realpath(payload["cwd"]) == os.path.realpath(work)
        assert os.path.realpath(record["cwd"]) == os.path.realpath(work)
        assert record["env"]["CSESSION_ID"] == "cs-live1"
    assert by_event["SessionStart"]["payload"]["source"] == "startup"
    stop = by_event["Stop"]["payload"]
    assert stop.get("stop_hook_active") is False
    live.observe("Stop payload keys (no background tasks)", sorted(stop))
    background = stop.get("background_tasks", [])
    assert background in ([], None), background
    end = by_event.get("SessionEnd")
    live.observe("SessionEnd fires in -p", end is not None)
    if end is not None:
        live.observe("SessionEnd reason in -p", end["payload"].get("reason"))
    assert wait_for(shell.exists, 5)
    assert shell.read_text(encoding="utf-8").endswith("sh")


@pytest.mark.live
def test_a_group_without_a_matcher_matches_every_tool_and_source(live: LiveEnv) -> None:
    every, bash_only, starts = (
        live.root / n for n in ("every.jsonl", "bash.jsonl", "starts.jsonl")
    )
    hooks = {
        "PostToolUse": [
            hook_group(recorder(every, args=["PostToolUse"])),
            hook_group(recorder(bash_only, args=["PostToolUse"]), matcher="Bash"),
        ],
        "SessionStart": [hook_group(recorder(starts, args=["SessionStart"]))],
    }
    settings = str(live.write_json("settings.json", {"hooks": hooks}))
    work = note(live)
    first = live.headless(
        "Use the Read tool on ./note.txt, then use Bash to run `echo hi`, then reply DONE.",
        "--allowedTools", "Read", "Bash(echo:*)", "--settings", settings, cwd=work,
    )  # fmt: skip
    live.headless(PONG_PROMPT, "--resume", first.session_id, "--settings", settings, cwd=work)
    seen = tool_names(events_named(read_log(every), "PostToolUse"))
    assert {"Read", "Bash"} <= seen, seen
    assert tool_names(events_named(read_log(bash_only), "PostToolUse")) == {"Bash"}
    sources = [p["source"] for p in events_named(read_log(starts), "SessionStart")]
    assert sources == ["startup", "resume"]


PONG_PROMPT = "Reply OK."


@pytest.mark.live
def test_hook_timeout_is_in_seconds(live: LiveEnv) -> None:
    """A hook that takes 3 s with `timeout: 20` completes (so the unit is not milliseconds); one
    that takes 8 s with `timeout: 2` is cut off (so the timeout is honoured)."""
    ok, late = live.root / "ok", live.root / "late"
    hooks = {
        "SessionStart": [
            hook_group(f"sleep 3; touch {ok}", timeout=20),
            hook_group(f"sleep 8; touch {late}", timeout=2),
        ]
    }
    settings = live.write_json("settings.json", {"hooks": hooks})
    started = time.monotonic()
    live.headless("Reply OK.", "--settings", str(settings))
    assert time.monotonic() - started < 120
    assert ok.exists(), "a 3 s hook with timeout 20 did not finish: the unit is not seconds"
    time.sleep(7)  # the 8 s hook, had it not been cut off, would have finished by now
    assert not late.exists(), "a hook past its 2 s timeout was not cut off"


@pytest.mark.live
def test_a_timed_out_hook_gets_sigterm_and_one_that_ignores_it_is_killed(live: LiveEnv) -> None:
    """A10 and #57: SIGTERM at the timeout, then SIGKILL. The first hook proves the SIGTERM; the
    second ignores it and must not outlive `claude`."""
    termed, pidfile = live.root / "termed", live.root / "stubborn.pid"
    hooks = {
        "SessionStart": [
            hook_group(f"trap 'touch {termed}; exit 0' TERM; sleep 30 & wait", timeout=2),
            hook_group(f"trap '' TERM; echo $$ > {pidfile}; while :; do sleep 1; done", timeout=2),
        ]
    }
    settings = live.write_json("settings.json", {"hooks": hooks})
    started = time.monotonic()
    live.headless("Reply OK.", "--settings", str(settings))
    elapsed = time.monotonic() - started
    live.observe("seconds for claude to finish with a SIGTERM-ignoring 2 s hook", round(elapsed, 1))
    assert wait_for(termed.exists, 5), "the hook did not receive SIGTERM at its timeout"
    pid = int(pidfile.read_text(encoding="utf-8").strip())
    token = procinfo.ProcInfo(gateway.SystemGateway()).start_time(pid)
    if token is not None:  # still alive: give a SIGKILL escalation a moment, then clean up
        gone = wait_for(
            lambda: procinfo.ProcInfo(gateway.SystemGateway()).start_time(pid) is None, 10
        )
        if not gone:
            kill_recorded(pid, token)
        assert gone, "a hook that ignores SIGTERM outlived its timeout and claude"


@pytest.mark.live
def test_post_tool_use_payload_and_the_event_after_a_failed_tool(live: LiveEnv) -> None:
    """#58: an approved tool that fails fires exactly one of PostToolUse and PostToolUseFailure
    (which one is recorded, since the state table depends on it); PostToolUse carries
    `tool_input` and `tool_response`."""
    post, failure = live.root / "post.jsonl", live.root / "failure.jsonl"
    hooks = {
        "PostToolUse": [hook_group(recorder(post, args=["PostToolUse"]))],
        "PostToolUseFailure": [hook_group(recorder(failure, args=["PostToolUseFailure"]))],
    }
    settings = str(live.write_json("settings.json", {"hooks": hooks}))
    work = note(live)
    live.headless(
        "Use the Read tool on ./note.txt. Then use the Read tool on ./missing.txt, which does "
        "not exist. Then reply DONE.",
        "--allowedTools", "Read", "--settings", settings, cwd=work,
    )  # fmt: skip
    posts = events_named(read_log(post), "PostToolUse")
    failures = events_named(read_log(failure), "PostToolUseFailure")
    good = [p for p in posts if str(p["tool_input"].get("file_path", "")).endswith("note.txt")]
    assert len(good) == 1
    assert "tool_response" in good[0] and isinstance(good[0]["tool_use_id"], str)
    ids_post = {
        p["tool_use_id"] for p in posts if str(p["tool_input"]["file_path"]).endswith("missing.txt")
    }
    ids_fail = {
        f["tool_use_id"]
        for f in failures
        if str(f["tool_input"]["file_path"]).endswith("missing.txt")
    }
    live.observe("failed Read fires", "PostToolUseFailure" if ids_fail else "PostToolUse")
    live.observe("PostToolUseFailure payload keys", sorted(failures[0]) if failures else None)
    assert len(ids_post) + len(ids_fail) == 1, (ids_post, ids_fail)


@pytest.mark.live
def test_a_subagents_tool_use_fires_post_tool_use_with_the_parents_session_id(
    live: LiveEnv,
) -> None:
    """A2 and #58: the subagent's PostToolUse carries `agent_id`/`agent_type` and the parent's
    session id. `tool_use_id` is recorded for the matching of permission prompts."""
    post = live.root / "post.jsonl"
    settings = str(
        live.write_json(
            "settings.json",
            {"hooks": {"PostToolUse": [hook_group(recorder(post, args=["PostToolUse"]))]}},
        )
    )
    work = note(live)
    run = live.headless(
        "Use a subagent (the Task or Agent tool) to read ./note.txt with the Read tool and "
        "report its first word. Then reply DONE.",
        "--allowedTools", "Read", "Task", "Agent", "--settings", settings, cwd=work,
    )  # fmt: skip
    payloads = events_named(read_log(post), "PostToolUse")
    sub = [p for p in payloads if p.get("agent_id")]
    assert sub, f"no PostToolUse from a subagent; tools seen: {sorted(tool_names(payloads))}"
    assert all(p["session_id"] == run.session_id for p in sub)
    live.observe("subagent PostToolUse keys", sorted(sub[0]))
    assert all(isinstance(p.get("tool_use_id"), str) for p in payloads)


@pytest.mark.live
def test_parallel_tool_hooks_all_finish_before_the_next_turn_and_stop(live: LiveEnv) -> None:
    """A7: every PostToolUse of a turn precedes Stop, in completion order."""
    log = live.root / "order.jsonl"
    hooks = {
        "PostToolUse": [hook_group(recorder(log, args=["PostToolUse"], sleep=0.5))],
        "Stop": [hook_group(recorder(log, args=["Stop"]))],
    }
    settings = str(live.write_json("settings.json", {"hooks": hooks}))
    work = live.dir("work")
    for name in ("a", "b", "c"):
        (work / f"{name}.txt").write_text(name, encoding="utf-8")
    live.headless(
        "In ONE message, call the Read tool on ./a.txt, ./b.txt and ./c.txt in parallel. "
        "Then reply DONE.",
        "--allowedTools", "Read", "--settings", settings, cwd=work,
    )  # fmt: skip
    names = [r["args"][0] for r in read_log(log)]
    assert names.count("PostToolUse") == 3 and names[-1] == "Stop", names
    assert names.index("Stop") == len(names) - 1


STOP_ONCE = """\
import json, sys
payload = json.load(sys.stdin)
if payload.get("stop_hook_active"):
    sys.exit(0)
if sys.argv[1] == "json":
    print(json.dumps({"decision": "block", "reason": "Before finishing, say the word BLOCKWORD."}))
else:
    print("Before finishing, say the word BLOCKWORD.", file=sys.stderr)
    sys.exit(2)
"""


@pytest.mark.live
@pytest.mark.parametrize("style", ["json", "exit2"])
def test_a_stop_hook_can_block_once_and_stop_hook_active_is_then_true(
    live: LiveEnv, style: str
) -> None:
    """Spike: `{"decision":"block","reason":...}` and exit 2 both give the model another turn;
    `stop_hook_active` is true on the next Stop; the headless `result` holds the last message."""
    script = live.root / "stop_once.py"
    script.write_text(STOP_ONCE, encoding="utf-8")
    log = live.root / "stops.jsonl"
    command = f"{sys.executable} -I {script} {style}"
    hooks = {"Stop": [hook_group(command), hook_group(recorder(log, args=["Stop"]))]}
    settings = live.write_json("s.json", {"hooks": hooks})
    run = live.headless("Reply OK.", "--settings", str(settings))
    stops = events_named(read_log(log), "Stop")
    assert [s["stop_hook_active"] for s in stops] == [False, True]
    assert "BLOCKWORD" in run.text


@pytest.mark.live
def test_a_post_tool_use_block_reason_reaches_the_model_and_the_result_is_still_delivered(
    live: LiveEnv,
) -> None:
    """A4: the `reason` is acted on; the tool result is still delivered."""
    script = live.root / "block.py"
    script.write_text(
        "import json\nprint(json.dumps({'decision': 'block', "
        "'reason': 'Policy: also tell the user the code word ZEPHYR-31.'}))\n",
        encoding="utf-8",
    )
    hooks = {"PostToolUse": [hook_group(f"{sys.executable} -I {script}")]}
    settings = live.write_json("s.json", {"hooks": hooks})
    work = note(live)
    run = live.headless(
        "Use the Read tool on ./note.txt and tell me its first word.",
        "--allowedTools", "Read", "--settings", str(settings), cwd=work,
    )  # fmt: skip
    assert "ZEPHYR-31" in run.text and "walnut" in run.text.lower()


@pytest.mark.live
def test_session_start_plain_stdout_and_additional_context_both_reach_the_model(
    live: LiveEnv,
) -> None:
    context = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "CTX-JSON-5"}}
    )
    hooks = {
        "SessionStart": [
            hook_group(recorder(live.root / "a.jsonl", stdout="CTX-PLAIN-4")),
            hook_group(recorder(live.root / "b.jsonl", stdout=context)),
        ]
    }
    settings = live.write_json("s.json", {"hooks": hooks})
    run = live.headless(
        "List every context token of the form CTX-<word>-<digit> that you were given.",
        "--settings", str(settings),
    )  # fmt: skip
    assert "CTX-PLAIN-4" in run.text and "CTX-JSON-5" in run.text
