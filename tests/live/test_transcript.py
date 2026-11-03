"""Claude Code transcript details `claude-session transcript` relies on (#37 and its review):
file name and place, the `transcript_path` of SessionStart, record and block shapes, subagent
metadata, offloaded tool results, ownership and permissions, and how many entries `subagents/`
and `tool-results/` hold."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ..fakeclaude import recorder
from .harness import (
    LiveEnv,
    content_blocks,
    events_named,
    find_transcripts,
    hook_group,
    read_log,
    transcript_records,
)

PROJECT_PROMPT = (
    "Use the Read tool on ./note.txt, then use Bash to run `echo hello`, then reply with the "
    "first word of the note."
)


def assert_user_regular_single_link(path: Path) -> None:
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(), path


def assert_user_directory(path: Path) -> None:
    info = path.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), path


@pytest.mark.live
def test_the_main_transcript_layout_records_and_blocks(live: LiveEnv) -> None:
    """#37 items 1, 2, 3 and 6; #37 review (root permissions, newline at the end)."""
    log = live.root / "start.jsonl"
    settings = live.write_json(
        "s.json", {"hooks": {"SessionStart": [hook_group(recorder(log, args=["SessionStart"]))]}}
    )
    work = live.dir("work")
    (work / "note.txt").write_text("walnut is the word\n", encoding="utf-8")
    run = live.headless(
        PROJECT_PROMPT, "--allowedTools", "Read", "Bash(echo:*)", "--settings", str(settings),
        cwd=work,
    )  # fmt: skip
    (main,) = find_transcripts(live.projects, run.session_id)
    assert main.parent.parent == live.projects, "not directly in projects/<project>/"
    assert_user_regular_single_link(main)
    assert_user_directory(live.projects)
    assert stat.S_IMODE(live.projects.stat().st_mode) & 0o002 == 0, "projects root world-writable"
    assert_user_directory(main.parent)
    assert main.read_bytes().endswith(b"\n")

    start = events_named(read_log(log), "SessionStart")[0]
    recorded = str(start["transcript_path"])
    home = str(live.home)
    assert recorded == str(main) and (
        recorded.startswith(home + "/") or recorded == os.path.realpath(main)
    )

    records = transcript_records(main)
    types = {r.get("type") for r in records}
    assert {"user", "assistant"} <= types
    uses: dict[str, dict] = {}
    results: list[dict] = []
    for record in records:
        if record.get("type") not in ("user", "assistant"):
            continue
        message = record["message"]
        assert isinstance(message["content"], str | list)
        for block in content_blocks(record):
            if block["type"] == "tool_use":
                assert {"id", "name", "input"} <= set(block) and isinstance(block["input"], dict)
                uses[block["id"]] = dict(block)
            elif block["type"] == "tool_result":
                assert isinstance(block["tool_use_id"], str)
                body = block.get("content")
                assert isinstance(body, str) or all(
                    isinstance(b, dict) and "type" in b for b in body or []
                )
                assert "is_error" not in block or isinstance(block["is_error"], bool)
                results.append(dict(block))
    assert {u["name"] for u in uses.values()} >= {"Read", "Bash"}
    assert results and all(r["tool_use_id"] in uses for r in results)
    live.observe("transcript record types", sorted(str(t) for t in types))


@pytest.mark.live
def test_a_subagents_meta_names_the_parents_tool_use_id(live: LiveEnv) -> None:
    """#37 item 4, and the `subagents/` listing size for #37's bounds."""
    work = live.dir("work")
    (work / "note.txt").write_text("walnut\n", encoding="utf-8")
    run = live.headless(
        "Use a subagent (the Task or Agent tool) to read ./note.txt with the Read tool and "
        "return its content. Then reply DONE.",
        "--allowedTools", "Read", "Task", "Agent", cwd=work,
    )  # fmt: skip
    (main,) = find_transcripts(live.projects, run.session_id)
    parent_ids = {
        b["id"]
        for r in transcript_records(main)
        if r.get("type") == "assistant"
        for b in content_blocks(r)
        if b["type"] == "tool_use" and b["name"] in ("Task", "Agent")
    }
    assert parent_ids, "the model started no subagent"
    directory = main.parent / run.session_id / "subagents"
    assert_user_directory(directory)
    metas = sorted(directory.glob("agent-*.meta.json"))
    assert metas, sorted(p.name for p in directory.iterdir())
    for meta in metas:
        assert_user_regular_single_link(meta)
    ids = {json.loads(m.read_text(encoding="utf-8"))["toolUseId"] for m in metas}
    assert ids <= parent_ids and ids
    live.observe("entries in subagents/", len(list(directory.iterdir())))


@pytest.mark.live
def test_a_large_tool_result_is_offloaded_to_a_tool_results_file(live: LiveEnv) -> None:
    """#37 item 5 and review: the `tool_result` text names `.../<claude id>/tool-results/<name>`;
    the directory is a real one owned by the user; its files are regular, one link."""
    work = live.dir("work")
    run = live.headless(
        "Run `seq 1 400000` with the Bash tool and tell me only the last number it printed.",
        "--allowedTools", "Bash(seq:*)", cwd=work,
    )  # fmt: skip
    (main,) = find_transcripts(live.projects, run.session_id)
    directory = main.parent / run.session_id / "tool-results"
    assert directory.is_dir() and not directory.is_symlink(), "no tool-results/ directory"
    assert_user_directory(directory)
    files = list(directory.iterdir())
    assert files
    for path in files:
        assert_user_regular_single_link(path)
    text = main.read_text(encoding="utf-8")
    assert f"{run.session_id}/tool-results/" in text
    live.observe("entries in tool-results/", len(files))


@pytest.mark.live
def test_a_plain_resume_appends_to_the_same_transcript_file(live: LiveEnv) -> None:
    """#37 item 7: the id and the file stay, so `--run <n>` can show ids first seen in run n.
    (The full chain is in test_headless.py; this pins the record count growth only.)"""
    first = live.headless("Reply OK.")
    (main,) = find_transcripts(live.projects, first.session_id)
    before = len(transcript_records(main))
    again = live.headless("Reply OK again.", "--resume", first.session_id)
    assert again.session_id == first.session_id
    assert find_transcripts(live.projects, first.session_id) == [main]
    assert len(transcript_records(main)) > before
