"""Headless (`-p`) behaviour: output format, argv forms, limits, resume, fork, plugins, MCP.

Design.md rows covered: `-p` JSON/stream-json (A1), prompt delivery, `--append-system-prompt-file`,
`--max-turns`, `--session-id`, resume and fork ids (A6, B1), `--plugin-dir`, `--strict-mcp-config`
(D4). Issue #41 comments: `--` before the prompt (#56), the stream-json trailing newline (#64).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

import pytest

from ..fakeclaude import recorder
from .harness import (
    Headless,
    LiveEnv,
    events_named,
    find_transcripts,
    hook_group,
    init_event,
    read_log,
    wait_for,
)

PONG = "Reply with exactly the single word PONG and nothing else."


def session_start_settings(live: LiveEnv, log: Path) -> Path:
    command = recorder(log, args=["SessionStart"])
    return live.write_json(
        "settings-start.json", {"hooks": {"SessionStart": [hook_group(command)]}}
    )


@pytest.mark.live
def test_headless_stream_json_ends_its_last_line_with_a_newline(live: LiveEnv) -> None:
    """The headless parser reads a tail without its newline as truncated (interface.md,
    Headless output), so Claude Code must end its last ``stream-json`` line with one."""
    run = live.headless("Reply with OK.")
    assert run.proc.returncode == 0, run.proc.stderr
    assert run.proc.stdout.endswith("\n")
    assert json.loads(run.proc.stdout.splitlines()[-1])["type"] == "result"


@pytest.mark.live
def test_result_object_and_init_event_carry_the_documented_fields(live: LiveEnv) -> None:
    run = live.headless(PONG, "--setting-sources", "")
    final = run.final
    for key in (
        "session_id",
        "result",
        "total_cost_usd",
        "num_turns",
        "subtype",
        "is_error",
        "terminal_reason",
        "permission_denials",
    ):
        assert key in final, key
    assert final["subtype"] == "success" and final["is_error"] is False
    assert "PONG" in run.text
    init = init_event(run.events)
    assert init["session_id"] == run.session_id
    assert "permissionMode" in init


@pytest.mark.live
@pytest.mark.parametrize(
    "flags",
    [
        ["--debug"],
        ["--allowedTools", "Read", "Bash(echo:*)"],
        ["--add-dir", "{d1}", "{d2}"],
    ],
    ids=["optional-value", "variadic-allowedTools", "variadic-add-dir"],
)
def test_double_dash_keeps_the_prompt_out_of_variadic_options(
    live: LiveEnv, flags: list[str]
) -> None:
    """#56: `--debug` (optional value) and `--allowedTools`/`--add-dir` (several values) swallow
    a positional prompt; `-- "<prompt>"` is what the launch argv relies on."""
    d1, d2 = live.dir("d1"), live.dir("d2")
    flags = [f.format(d1=d1, d2=d2) for f in flags]
    run = live.headless(PONG, *flags)
    assert run.proc.returncode == 0, run.proc.stderr[:500]
    assert "PONG" in run.text


@pytest.mark.live
def test_without_double_dash_a_variadic_option_swallows_the_prompt(live: LiveEnv) -> None:
    """The control for the test above: why the `--` exists. The prompt becomes a second
    `--allowedTools` value, so `-p` has no prompt and must fail for that reason (no input),
    not for any other."""
    proc = live.claude_run(
        ["-p", "--model", live.model, *live.budget(), "--allowedTools", "Read", PONG],
        stdin="",
        timeout=120,
    )
    live.observe(
        "-p with a swallowed prompt: exit and stderr", (proc.returncode, proc.stderr[:300])
    )
    assert proc.returncode != 0
    assert "PONG" not in proc.stdout
    assert re.search(r"\binput\b|\bprompt\b", proc.stderr, re.IGNORECASE), proc.stderr[:500]


@pytest.mark.live
def test_argv_prompt_and_stdin_are_concatenated(live: LiveEnv) -> None:
    run = live.headless(
        "What is the code word given above? Reply with the code word only.",
        stdin="The code word is TANGERINE.\n",
    )
    assert "TANGERINE" in run.text


@pytest.mark.live
def test_append_system_prompt_file_max_turns_and_snapshot_flags_are_accepted(
    live: LiveEnv,
) -> None:
    """`--append-system-prompt-file` and `--max-turns` are not in `--help`; the snapshot flag is."""
    instructions = live.dir("work") / "extra.md"
    instructions.write_text("End every reply with the token ZQX9.\n", encoding="utf-8")
    run = live.headless(
        "Say hello.",
        "--append-system-prompt-file",
        str(instructions),
        "--max-turns",
        "3",
        "--system-prompt-snapshot",
        "off",
    )
    assert run.proc.returncode == 0, run.proc.stderr[:500]
    assert "ZQX9" in run.text


@pytest.mark.live
def test_the_turn_limit_ends_with_error_max_turns_and_exit_1(live: LiveEnv) -> None:
    work = live.dir("work")
    (work / "a.txt").write_text("alpha\n", encoding="utf-8")
    (work / "b.txt").write_text("beta\n", encoding="utf-8")
    run = live.headless(
        "Use the Read tool on ./a.txt, then use the Read tool on ./b.txt, then reply DONE.",
        "--allowedTools",
        "Read",
        "--max-turns",
        "1",
        cwd=work,
    )
    assert run.final["subtype"] == "error_max_turns"
    assert run.proc.returncode == 1


@pytest.mark.live
def test_a_preassigned_session_id_is_used_and_names_the_transcript(live: LiveEnv) -> None:
    sid = str(uuid.uuid4())
    run = live.headless(PONG, "--session-id", sid)
    assert run.session_id == sid and init_event(run.events)["session_id"] == sid
    assert len(find_transcripts(live.projects, sid)) == 1


@pytest.mark.live
def test_resume_keeps_the_id_and_the_transcript_and_fork_makes_a_new_one(live: LiveEnv) -> None:
    """Design: resume appends to the same transcript and keeps the id; `--session-id` is rejected
    next to a plain `--resume`; a fork has a new id and transcript and leaves the original
    byte-identical; a resume from another cwd works (A6); a fork into another cwd reports its
    transcript's real path in its SessionStart payload (B1; where that file lies is observed).
    SessionStart sources: startup, resume, fork (#76)."""
    log = live.root / "starts.jsonl"
    settings = str(session_start_settings(live, log))
    first = str(uuid.uuid4())
    fork = str(uuid.uuid4())
    base = live.dir("work")
    elsewhere = live.dir("elsewhere")

    live.headless(
        "Remember the word PERSIMMON. Reply OK.",
        "--session-id",
        first,
        "--settings",
        settings,
        cwd=base,
    )
    (original,) = find_transcripts(live.projects, first)
    size_before = original.stat().st_size

    resumed = live.headless(
        "What word did I ask you to remember? One word.",
        "--resume",
        first,
        "--settings",
        settings,
        cwd=base,
    )
    assert resumed.session_id == first and "PERSIMMON" in resumed.text.upper()
    assert find_transcripts(live.projects, first) == [original]
    assert original.stat().st_size > size_before

    bad = live.claude_run(
        [
            "-p",
            "--model",
            live.model,
            *live.budget(),
            "--resume",
            first,
            "--session-id",
            first,
            "--",
            "hi",
        ],
        cwd=base,
        timeout=120,
    )
    assert bad.returncode != 0

    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    forked = live.headless(
        "What word did I ask you to remember? One word.",
        "--resume", first, "--fork-session", "--session-id", fork, "--settings", settings,
        cwd=elsewhere,
    )  # fmt: skip
    assert forked.session_id == fork and "PERSIMMON" in forked.text.upper()
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    (fork_file,) = find_transcripts(live.projects, fork)
    assert fork_file != original
    # Unverified: whether the fork's transcript lands in the new cwd's project directory. The
    # design does not depend on it (the path is taken from SessionStart, asserted below).
    live.observe("fork transcript in another project dir than the original",
                 fork_file.parent != original.parent)  # fmt: skip

    again = live.headless(PONG, "--resume", fork, "--settings", settings, cwd=elsewhere)
    assert again.session_id == fork

    from_elsewhere = live.headless(PONG, "--resume", first, cwd=elsewhere)
    assert from_elsewhere.session_id == first
    assert find_transcripts(live.projects, first) == [original]  # A6: stays in its project

    starts = events_named(read_log(log), "SessionStart")
    assert [(s["source"], s["session_id"]) for s in starts] == [
        ("startup", first),
        ("resume", first),
        ("fork", fork),
        ("resume", fork),
    ]
    fork_start = starts[2]
    assert fork_start["transcript_path"] == str(fork_file)


def write_plugin(root: Path, name: str, skill: str, reply: str) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "description": "live test plugin", "version": "1.0.0"}),
        encoding="utf-8",
    )
    skill_dir = root / "skills" / skill
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: Replies with a fixed token.\n---\n\n"
        f"Reply with exactly the token {reply} followed by the arguments you were given.\n",
        encoding="utf-8",
    )
    return root


@pytest.mark.live
def test_a_plugin_skill_runs_with_its_arguments_and_the_manifest_names_the_plugin(
    live: LiveEnv,
) -> None:
    plugin = write_plugin(live.dir("plug-a"), "plug", "hello", "ONE-OK")
    run = live.headless("/plug:hello apples", "--setting-sources", "", "--plugin-dir", str(plugin))
    assert "ONE-OK" in run.text and "apples" in run.text


@pytest.mark.live
def test_with_two_plugins_of_one_name_the_first_plugin_dir_wins(live: LiveEnv) -> None:
    first = write_plugin(live.dir("plug-first"), "plug", "hello", "FIRST-OK")
    second = write_plugin(live.dir("plug-second"), "plug", "hello", "SECOND-OK")
    run = live.headless(
        "/plug:hello x",
        *("--setting-sources", ""),
        *("--plugin-dir", str(first)),
        *("--plugin-dir", str(second)),
    )
    assert "FIRST-OK" in run.text and "SECOND-OK" not in run.text


@pytest.mark.live
def test_strict_mcp_config_drops_the_mcp_servers_a_plugin_declares(live: LiveEnv) -> None:
    """D4: the server process is never spawned (and its skills still load). The same plugin
    without the flag is the control: its server must start, or the test proves nothing."""
    plugin = write_plugin(live.dir("plug-mcp"), "mcpplug", "hello", "MCP-OK")
    marker = live.root / "mcp-started"
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"probe": {"command": "sh", "args": ["-c", f"touch {marker}; sleep 5"]}}}
        ),
        encoding="utf-8",
    )
    common = ["--setting-sources", "", "--plugin-dir", str(plugin)]
    live.headless("/mcpplug:hello", *common, "--strict-mcp-config")
    assert not wait_for(marker.exists, 3), "strict mode still spawned the plugin's MCP server"
    live.headless("/mcpplug:hello", *common)
    assert wait_for(marker.exists, 10), "control failed: the plugin's server was not started"


def plugin_loaded(run: Headless, name: str) -> bool:
    """Whether the run's init event lists the plugin ``name`` or one of its slash commands."""
    init = init_event(run.events)
    plugins = init.get("plugins") or []
    names = {str(p.get("name")) for p in plugins if isinstance(p, dict)}
    commands = [str(c) for c in init.get("slash_commands") or []]
    return name in names or any(c.lstrip("/").startswith(f"{name}:") for c in commands)


@pytest.mark.live
def test_resume_does_not_remember_plugin_dirs(live: LiveEnv) -> None:
    """Judged from the init event (the plugin and its commands), not from the model's text."""
    plugin = write_plugin(live.dir("plug-r"), "plug", "hello", "REMEMBER-OK")
    first = live.headless("/plug:hello a", "--setting-sources", "", "--plugin-dir", str(plugin))
    live.observe("init plugins with --plugin-dir", init_event(first.events).get("plugins"))
    assert plugin_loaded(first, "plug"), "control: the plugin is not in the init event"
    again = live.headless("Reply OK.", "--setting-sources", "", "--resume", first.session_id)
    assert not plugin_loaded(again, "plug")


@pytest.mark.live
def test_system_prompt_snapshot_off_applies_to_one_run_and_keeps_the_stored_snapshot(
    live: LiveEnv,
) -> None:
    """A3: the first run's appended text is snapshotted; `--system-prompt-snapshot off` makes the
    new text win for that run only; a later plain resume still sees the stored snapshot."""
    first = live.headless("Say hello.", "--append-system-prompt", "End every reply with SNAPONE.")
    sid = first.session_id
    assert "SNAPONE" in first.text
    new = live.headless(
        "Say hello again.", "--resume", sid, "--system-prompt-snapshot", "off",
        "--append-system-prompt", "End every reply with SNAPTWO.",
    )  # fmt: skip
    assert "SNAPTWO" in new.text
    later = live.headless("Say hello a third time.", "--resume", sid)
    assert "SNAPONE" in later.text


@pytest.mark.live
def test_in_default_mode_headless_denies_a_tool_no_rule_allows(live: LiveEnv) -> None:
    """A9: in `default` mode, with no rule, everything that needs permission is denied in `-p`.
    The mode is set explicitly: `--setting-sources ""` alone starts in `auto` (design.md,
    Permission mode)."""
    settings = live.write_json("s.json", {"permissions": {"defaultMode": "default"}})
    run = live.headless(
        "Run `echo hi > denied.txt` with the Bash tool. Do not retry. Then reply DONE.",
        "--setting-sources", "", "--settings", str(settings),
    )  # fmt: skip
    assert init_event(run.events)["permissionMode"] == "default"
    assert not (live.dir("work") / "denied.txt").exists()
    assert run.final["permission_denials"]
