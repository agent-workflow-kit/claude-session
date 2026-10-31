"""``transcript`` (interface.md, transcript; design.md, Transcripts): discovery across
``/clear``, resume and fork, subagents and offloaded tool results, ``--raw``, ``--paths``,
``--json``, ``--subagents`` and ``--run``, missing files, and the handling of hostile
transcript files, paths and content."""

from __future__ import annotations

import errno
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from claude_session import store, transcripts
from claude_session.cli import main
from claude_session.errors import FailedError
from claude_session.store import Store

from .golden import assert_golden
from .schemas import validate
from .test_status_list import HOSTILE, Clock, allocate, sql

SCHEMA = "claude-session/cli/transcript.json"
ERROR_SCHEMA = "claude-session/cli/error.json"
SID = "cs-aaaaaaaa"
CID1 = "11111111-1111-4111-8111-111111111111"
CID2 = "22222222-2222-4222-8222-222222222222"
CID3 = "33333333-3333-4333-8333-333333333333"
PROJECT = "-work-project"
OTHER_PROJECT = "-work-fork"
# Characters that must never reach a terminal from rendered text.
UNSAFE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\u200b\ufeff]"
)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def st(clock: Clock) -> Iterator[Store]:
    with store.open_state(clock=clock) as s:
        yield s


@pytest.fixture
def projects(monkeypatch: pytest.MonkeyPatch) -> Path:
    """``$HOME/.claude/projects`` (HOME is private to each test)."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    root = Path(os.environ["HOME"]) / ".claude" / "projects"
    root.mkdir(parents=True)
    return root


def session(st: Store, clock: Clock, *, runs: int = 1) -> str:
    sid = allocate(st, SID, clock)
    for run_no in range(2, runs + 1):
        sql(
            st,
            "INSERT INTO runs (csession_id, run_no, created_at, run_kind) VALUES (?, ?, ?, ?)",
            sid,
            run_no,
            "2026-10-30T12:00:00.000Z",
            "implement",
        )
    sql(st, "UPDATE sessions SET current_run = ? WHERE csession_id = ?", runs, sid)
    return sid


def claude_id(
    st: Store, cid: str, run: int, source: str, path: object, seen: str = "12:00:01"
) -> None:
    sql(
        st,
        "INSERT INTO claude_ids (csession_id, claude_session_id, run_no, source, "
        "transcript_path, seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        SID,
        cid,
        run,
        source,
        path,
        f"2026-10-30T{seen}.000Z",
    )


def jsonl(*records: object) -> bytes:
    return b"".join(
        (r if isinstance(r, bytes) else json.dumps(r).encode()) + b"\n" for r in records
    )


def user(text: object) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(tool_id: str, name: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": data}


def tool_result(tool_id: str, content: object, *, error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if error:
        block["is_error"] = True
    return user([block])


def write_main(projects: Path, cid: str, data: bytes, project: str = PROJECT) -> Path:
    path = projects / project / f"{cid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_subagent(
    projects: Path,
    cid: str,
    agent_id: str,
    data: bytes | None,
    meta: object = None,
    project: str = PROJECT,
) -> Path:
    directory = projects / project / cid / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"agent-{agent_id}.jsonl"
    if data is not None:
        path.write_bytes(data)
    if meta is not None:
        raw = meta if isinstance(meta, bytes) else json.dumps(meta).encode()
        (directory / f"agent-{agent_id}.meta.json").write_bytes(raw)
    return path


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def run_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any, str]:
    code, out, err = run([*argv, "--json"], capsys)
    return code, json.loads(out), err


def run_raw(argv: list[str], capfdbinary: pytest.CaptureFixture[bytes]) -> tuple[int, bytes]:
    code = main(argv)
    out, _err = capfdbinary.readouterr()
    return code, out


# A session that went through /clear, a resume into a new Claude id and a fork into another
# project directory, with subagents (one without meta, one whose file is gone) and a missing
# main file.


@pytest.fixture
def full(st: Store, clock: Clock, projects: Path) -> Path:
    session(st, clock, runs=2)
    main1 = jsonl(
        user("first prompt"),
        assistant(text("I'll delegate."), tool_use("toolu_1", "Task", {"description": "dig"})),
        tool_result("toolu_1", "done digging"),
    )
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, main1)), "12:00:01")
    write_subagent(
        projects,
        CID1,
        "a1",
        jsonl(user("sub prompt"), assistant(text("sub answer"))),
        {"agentType": "Explore", "description": "dig around", "toolUseId": "toolu_1"},
    )
    write_subagent(projects, CID1, "b2", jsonl(assistant(text("orphan"))))
    write_subagent(
        projects, CID1, "c3", None, {"agentType": "general", "description": None, "toolUseId": 5}
    )
    # /clear: a new Claude id in the same run; its file was deleted since.
    missing = projects / PROJECT / f"{CID2}.jsonl"
    claude_id(st, CID2, 1, "clear", str(missing), "12:00:02")
    # Run 2 forks into another project directory.
    main3 = jsonl(user("after fork"), assistant(text("forked")))
    claude_id(
        st, CID3, 2, "fork", str(write_main(projects, CID3, main3, OTHER_PROJECT)), "12:00:03"
    )
    return projects


def normalize(obj: dict[str, Any]) -> dict[str, Any]:
    home = os.environ["HOME"]
    text_out = json.dumps(obj).replace(home, "$HOME")
    result: dict[str, Any] = json.loads(text_out)
    return result


def test_json_lists_every_file_with_subagents(
    full: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, obj, err = run_json(["transcript", SID], capsys)
    assert code == 0
    assert_golden(obj, "transcript-full", normalize=normalize)
    assert "transcript file missing" not in err  # --json lists, it does not warn about files


def test_paths_json_is_the_same_object(full: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, plain, _ = run_json(["transcript", SID], capsys)
    code, paths, _ = run_json(["transcript", SID, "--paths"], capsys)
    assert code == 0
    assert paths == plain


def test_json_order_and_sources(full: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, obj, _ = run_json(["transcript", SID], capsys)
    assert [(f["claude_session_id"], f["run"], f["source"], f["exists"]) for f in obj["files"]] == [
        (CID1, 1, "startup", True),
        (CID2, 1, "clear", False),
        (CID3, 2, "fork", True),
    ]
    subs = obj["files"][0]["subagents"]
    assert [(s["agent_id"], s["exists"]) for s in subs] == [
        ("a1", True),
        ("b2", True),
        ("c3", False),
    ]
    assert subs[2]["tool_use_id"] is None  # not a string in the meta file


def test_run_filter(full: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, obj, _ = run_json(["transcript", SID, "--run", "2"], capsys)
    assert code == 0
    assert [f["claude_session_id"] for f in obj["files"]] == [CID3]
    assert_golden(obj, "transcript-run", normalize=normalize)


@pytest.mark.parametrize("run_no", ["0", "3", "-1"])
def test_run_out_of_range_is_not_found(
    full: Path, capsys: pytest.CaptureFixture[str], run_no: str
) -> None:
    code, obj, _ = run_json(["transcript", SID, "--run", run_no], capsys)
    assert code == 10
    validate(obj, ERROR_SCHEMA)
    assert obj["error"]["code"] == "not_found"


def test_unknown_and_forgotten_sessions_are_not_found(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_json(["transcript", "cs-bbbbbbbb"], capsys)[0] == 10
    session(st, clock)
    sql(st, "UPDATE sessions SET forgotten_at = '2026-10-30T13:00:00.000Z'")
    assert run_json(["transcript", SID], capsys)[0] == 10


def test_no_claude_ids(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    code, obj, _ = run_json(["transcript", SID], capsys)
    assert code == 0
    assert_golden(obj, "transcript-empty")
    assert run(["transcript", SID], capsys)[1] == "No transcripts.\n"
    assert run(["transcript", SID, "--paths"], capsys)[1] == ""


def test_a_missing_projects_directory_is_reported_missing(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    projects.rmdir()
    claude_id(st, CID1, 1, "startup", str(projects / PROJECT / f"{CID1}.jsonl"))
    code, obj, _ = run_json(["transcript", SID], capsys)
    assert code == 0
    assert obj["files"][0]["exists"] is False
    assert obj["files"][0]["subagents"] == []


# --paths


def test_paths_prints_existing_files_and_warns_about_missing_ones(
    full: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(["transcript", SID, "--paths"], capsys)
    assert code == 0
    sub = full / PROJECT / CID1 / "subagents"
    assert out.splitlines() == [
        str(full / PROJECT / f"{CID1}.jsonl"),
        str(sub / "agent-a1.jsonl"),
        str(sub / "agent-b2.jsonl"),
        str(full / OTHER_PROJECT / f"{CID3}.jsonl"),
    ]
    assert f"transcript file missing: {full / PROJECT / CID2}.jsonl" in err
    assert f"subagent transcript file missing: {sub / 'agent-c3.jsonl'}" in err


# --raw


def test_raw_is_the_bytes_unchanged(
    st: Store, clock: Clock, projects: Path, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    session(st, clock)
    data = jsonl(user(HOSTILE)) + b"\xff\xfe not utf-8 \x1b[2J\r\n" + b"no trailing newline"
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    write_subagent(projects, CID1, "a1", b"SUB\n")
    code, out = run_raw(["transcript", SID, "--raw"], capfdbinary)
    assert code == 0
    assert out == data


def test_raw_with_subagents_appends_them_after_their_parent(
    full: Path, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    code, out = run_raw(["transcript", SID, "--raw", "--subagents"], capfdbinary)
    assert code == 0
    sub = full / PROJECT / CID1 / "subagents"
    assert out == b"".join(
        p.read_bytes()
        for p in (
            full / PROJECT / f"{CID1}.jsonl",
            sub / "agent-a1.jsonl",
            sub / "agent-b2.jsonl",
            full / OTHER_PROJECT / f"{CID3}.jsonl",
        )
    )


def test_raw_run_filter(full: Path, capfdbinary: pytest.CaptureFixture[bytes]) -> None:
    code, out = run_raw(["transcript", SID, "--raw", "--run", "2"], capfdbinary)
    assert code == 0
    assert out == (full / OTHER_PROJECT / f"{CID3}.jsonl").read_bytes()


def test_raw_reads_only_up_to_the_size_at_open(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"a" * (transcripts.CHUNK_BYTES + 10))
    fd = os.open(path, os.O_RDONLY)
    try:
        reader = transcripts.Reader()
        chunks = reader.chunks(fd)
        first = next(chunks)
        with path.open("ab") as fh:
            fh.write(b"appended later")
        rest = b"".join(chunks)
    finally:
        os.close(fd)
    assert first + rest == b"a" * (transcripts.CHUNK_BYTES + 10)


# Text rendering


def render(argv: list[str], capsys: pytest.CaptureFixture[str]) -> list[str]:
    code, out, _ = run(argv, capsys)
    assert code == 0
    return out.splitlines()


def test_text_sections_are_headed_by_id_run_and_source(
    full: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = render(["transcript", SID], capsys)
    headers = [line for line in lines if line.startswith("===")]
    assert headers == [
        f"=== Claude session {CID1} (run 1, startup) ===",
        f"=== Claude session {CID2} (run 1, clear) ===",
        f"=== Claude session {CID3} (run 2, fork) ===",
    ]
    assert "(missing: Claude Code deleted it or never wrote it)" in lines
    assert "[user] first prompt" in lines
    assert "[assistant] I'll delegate." in lines
    assert "[tool Task] dig" in lines
    assert "[result] done digging" in lines
    assert not any("sub answer" in line for line in lines)  # no --subagents


def test_subagents_are_rendered_inline_after_their_tool_call(
    full: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = render(["transcript", SID, "--subagents", "--run", "1"], capsys)
    call = lines.index("[tool Task] dig")
    assert lines[call + 1 : call + 4] == [
        "  --- subagent a1 (Explore): dig around",
        "    | [user] sub prompt",
        "    | [assistant] sub answer",
    ]
    assert lines[call + 4] == "[result] done digging"
    # Subagents without a matching tool call follow the section.
    tail = lines[lines.index("[result] done digging") + 1 :]
    assert tail[:3] == [
        "--- subagent b2 (subagent)",
        "  | [assistant] orphan",
        "--- subagent c3 (general)",
    ]
    assert tail[3].startswith("  | (missing: ")


def test_tool_results_are_truncated_to_20_lines(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    body = "\n".join(f"line {n}" for n in range(1, 31))
    data = jsonl(tool_result("t", [{"type": "text", "text": body}], error=True))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    lines = render(["transcript", SID], capsys)
    start = lines.index("[error] line 1")
    assert lines[start + 19] == "        line 20"
    assert lines[start + 20] == "        ... (10 more lines)"
    assert not any("line 21" in line for line in lines)


def test_offloaded_tool_results_are_referenced_by_path(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    results = projects / PROJECT / CID1 / "tool-results"
    results.mkdir(parents=True)
    (results / "abc123.txt").write_text("big output")
    message = f"Output too large. Full output saved to: {results}/abc123.txt"
    data = jsonl(tool_result("t", message), tool_result("u", "see tool-results/absent.txt"))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    lines = render(["transcript", SID], capsys)
    assert f"[offloaded] {results}/abc123.txt" in lines
    assert not any("absent.txt" in line and line.startswith("[offloaded]") for line in lines)


def test_tool_call_summaries_are_one_line(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    data = jsonl(
        assistant(
            tool_use("a", "Bash", {"command": "ls -la\nrm -rf x", "description": "list"}),
            tool_use("b", "Read", {"file_path": "/x/y"}),
            tool_use("c", "Custom", {"z": 1, "a": [2]}),
            tool_use("d", "Long", {"command": "x" * 500}),
            {"type": "tool_use", "id": "e", "name": 7, "input": "not a dict"},
        )
    )
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    lines = render(["transcript", SID], capsys)
    assert "[tool Bash] ls -la ..." in lines
    assert "[tool Read] /x/y" in lines
    assert '[tool Custom] {"a": [2], "z": 1}' in lines
    assert f"[tool Long] {'x' * transcripts.SUMMARY_MAX_CHARS} ..." in lines
    assert "[tool ?]" in lines


def test_hostile_content_is_escaped(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    data = jsonl(
        user(HOSTILE),
        assistant(text(HOSTILE), tool_use("t", HOSTILE, {"command": HOSTILE})),
        tool_result("t", HOSTILE),
        b'{"type": "user", "message": {"content": "lone \\ud800 surrogate"}}',
        b'{"type": "user", "message": {"content": "bad \xff\xfe utf-8"}}',
    )
    write_subagent(
        projects, CID1, "a1", jsonl(user(HOSTILE)), {"agentType": HOSTILE, "description": HOSTILE}
    )
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    code, out, err = run(["transcript", SID, "--subagents"], capsys)
    assert code == 0
    for line in (out + err).splitlines():
        assert UNSAFE.search(line) is None, line
    assert "\\x1b]0;pwned" in out
    assert "lone \\ud800 surrogate" in out
    assert "bad \ufffd\ufffd utf-8" in out


def test_hostile_meta_is_carried_escaped_in_json(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, b"")))
    write_subagent(projects, CID1, "a1", b"", b'{"description": "x \\ud800 \\u001b[2J"}')
    code, out, _ = run(["transcript", SID, "--json"], capsys)
    assert code == 0
    assert "\x1b" not in out
    obj = json.loads(out)
    validate(obj, SCHEMA)
    assert obj["files"][0]["subagents"][0]["description"] == "x \ufffd \x1b[2J"


@pytest.mark.parametrize(
    "line",
    [
        b"not json",
        b"[1, 2]",
        b'"a string"',
        b"{",
        b"[" * 100_000 + b"]" * 100_000,
        b'{"type": "user", "message": {"content": ' + b"[" * 100_000 + b"]" * 100_000 + b"}}",
        b'{"type": "user", "message": {"content": 1' + b"0" * 10_000 + b"}}",
        b"\x00\x00\x00",
    ],
)
def test_unparseable_records_are_skipped_with_a_marker(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str], line: bytes
) -> None:
    session(st, clock)
    data = jsonl(user("before"), line, user("after"))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    lines = render(["transcript", SID], capsys)
    assert lines[2:] == ["[user] before", "[unparseable record skipped]", "[user] after"]


@pytest.mark.parametrize(
    "line",
    [
        b'{"type": "user", "message": "not an object"}',
        b'{"type": "user", "message": {"content": [1, "x", null, {"type": "text"}]}}',
        b'{"type": "summary", "summary": "skipped"}',
        b'{"type": "unknown-future-type", "message": {"content": "hidden"}}',
        b"   ",
    ],
)
def test_unknown_records_and_blank_lines_are_skipped_silently(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str], line: bytes
) -> None:
    session(st, clock)
    data = jsonl(user("before"), line, user("after"))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    lines = render(["transcript", SID], capsys)
    assert lines[2:] == ["[user] before", "[user] after"]


def test_an_oversized_record_is_skipped_without_holding_it(
    st: Store,
    clock: Clock,
    projects: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcripts, "RECORD_MAX_BYTES", 1000)
    monkeypatch.setattr(transcripts, "CHUNK_BYTES", 64)
    session(st, clock)
    huge = json.dumps(user("y" * 5000)).encode()
    data = jsonl(user("before"), huge, user("after")) + huge  # also as the unterminated last line
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    lines = render(["transcript", SID], capsys)
    skipped = "[record over 1000 bytes skipped]"
    assert lines[2:] == ["[user] before", skipped, "[user] after", skipped]


def test_long_lines_are_clipped(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    data = jsonl(user("z" * (transcripts.LINE_MAX_CHARS + 50)))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    lines = render(["transcript", SID], capsys)
    assert lines[2] == f"[user] {'z' * transcripts.LINE_MAX_CHARS} ... (50 more characters)"


def test_multiline_text_is_indented(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    data = jsonl(assistant(text("one\ntwo\r\nthree\n")), user(""))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    lines = render(["transcript", SID], capsys)
    assert lines[2:] == ["[assistant] one", "            two\\x0d", "            three", "[user]"]


# Recorded paths outside the allowed location are never read.


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "relative/x.jsonl",
        "{projects}/../{cid}.jsonl",
        "{projects}/{project}/../{project}/{cid}.jsonl",
        "{projects}/{project}/./{cid}.jsonl",
        "{projects}//{project}/{cid}.jsonl",
        "{projects}/{project}/{cid}.jsonl/",
        "{projects}/{project}/sub/{cid}.jsonl",
        "{projects}/{cid}.jsonl",
        "{projects}/{project}/other.jsonl",
        "{projects}/{project}/{cid}.json",
        "{projects}/{project}\x1b[2J/{cid}.jsonl",
        "{projects}/{project}\u202e/{cid}.jsonl",
        "{home}/.claude/{project}/{cid}.jsonl",
        "{home}/.claude/projects-x/{project}/{cid}.jsonl",
        "/{projects}/{project}/{cid}.jsonl",
    ],
)
def test_a_path_outside_the_projects_directory_is_not_read(
    st: Store,
    clock: Clock,
    projects: Path,
    capsys: pytest.CaptureFixture[str],
    path: str,
) -> None:
    session(st, clock)
    write_main(projects, CID1, jsonl(user("secret")))
    stored = path.format(projects=projects, project=PROJECT, cid=CID1, home=os.environ["HOME"])
    claude_id(st, CID1, 1, "startup", stored)
    code, obj, err = run_json(["transcript", SID], capsys)
    assert code == 0
    validate(obj, SCHEMA)
    assert obj["files"][0]["path"] is None
    assert obj["files"][0]["exists"] is False
    assert "not under a Claude Code projects directory" in err
    code, out, err = run(["transcript", SID], capsys)
    assert code == 0 and "secret" not in out
    assert UNSAFE.search(err) is None


def test_a_null_path_is_reported(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    claude_id(st, CID1, 1, "startup", None)
    code, obj, err = run_json(["transcript", SID], capsys)
    assert code == 0
    validate(obj, SCHEMA)
    assert obj["files"][0]["path"] is None
    assert "no transcript path was recorded" in err


@pytest.mark.parametrize("cid", ["ABCDEF00-1111-4111-8111-111111111111", "../x", "", "a/b"])
def test_a_malformed_claude_id_is_never_used_in_a_path(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str], cid: str
) -> None:
    session(st, clock)
    claude_id(st, cid, 1, "startup", f"{projects}/{PROJECT}/{cid}.jsonl")
    code, obj, _ = run_json(["transcript", SID], capsys)
    assert code == 0
    assert obj["files"][0]["path"] is None


def test_an_unknown_source_is_other(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    claude_id(st, CID1, 1, "\x1bweird", None)
    _, obj, _ = run_json(["transcript", SID], capsys)
    validate(obj, SCHEMA)
    assert obj["files"][0]["source"] == "other"


def test_claude_config_dir_is_an_allowed_root(
    st: Store,
    clock: Clock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    session(st, clock)
    path = write_main(config / "projects", CID1, jsonl(user("relocated")))
    claude_id(st, CID1, 1, "startup", str(path))
    lines = render(["transcript", SID], capsys)
    assert "[user] relocated" in lines


def test_a_symlinked_projects_root_is_matched_by_its_realpath(
    st: Store,
    clock: Clock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A user's own ``~/.claude`` may be a symlink (dotfiles); Claude Code may record either
    form."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    real = tmp_path / "dotfiles-claude"
    (real / "projects").mkdir(parents=True)
    (Path(os.environ["HOME"]) / ".claude").symlink_to(real)
    session(st, clock)
    path = write_main(real / "projects", CID1, jsonl(user("via realpath")))
    claude_id(st, CID1, 1, "startup", str(path))
    lines = render(["transcript", SID], capsys)
    assert "[user] via realpath" in lines


def test_projects_roots() -> None:
    assert transcripts.projects_roots({"HOME": "/h/", "CLAUDE_CONFIG_DIR": "/c"}) == [
        "/h/.claude/projects",
        "/c/projects",
    ]
    assert transcripts.projects_roots({"HOME": "rel", "CLAUDE_CONFIG_DIR": ""}) == []
    assert transcripts.projects_roots({}) == []


# Unsafe files fail the command (exit 12) and are never read.


def _symlink_main(projects: Path) -> None:
    target = projects.parent / "secret.txt"
    target.write_text("secret")
    (projects / PROJECT).mkdir()
    (projects / PROJECT / f"{CID1}.jsonl").symlink_to(target)


def _hardlink_main(projects: Path) -> None:
    target = projects.parent / "secret.txt"
    target.write_text("secret")
    (projects / PROJECT).mkdir()
    os.link(target, projects / PROJECT / f"{CID1}.jsonl")


def _fifo_main(projects: Path) -> None:
    (projects / PROJECT).mkdir()
    os.mkfifo(projects / PROJECT / f"{CID1}.jsonl")


def _dir_main(projects: Path) -> None:
    (projects / PROJECT / f"{CID1}.jsonl").mkdir(parents=True)


def _symlink_project(projects: Path) -> None:
    real = projects.parent / "elsewhere"
    real.mkdir()
    (real / f"{CID1}.jsonl").write_text("secret")
    (projects / PROJECT).symlink_to(real)


def _file_as_project(projects: Path) -> None:
    (projects / PROJECT).write_text("secret")


def _symlink_subagents(projects: Path) -> None:
    write_main(projects, CID1, b"")
    real = projects.parent / "subs"
    real.mkdir()
    (real / "agent-x.jsonl").write_text("secret")
    (projects / PROJECT / CID1).mkdir()
    (projects / PROJECT / CID1 / "subagents").symlink_to(real)


def _symlink_subagent_file(projects: Path) -> None:
    write_main(projects, CID1, b"")
    target = projects.parent / "secret.txt"
    target.write_text("secret")
    directory = projects / PROJECT / CID1 / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-x.jsonl").symlink_to(target)


def _symlink_meta(projects: Path) -> None:
    write_subagent(projects, CID1, "x", b"")
    write_main(projects, CID1, b"")
    target = projects.parent / "secret.json"
    target.write_text('{"description": "secret"}')
    (projects / PROJECT / CID1 / "subagents" / "agent-x.meta.json").symlink_to(target)


@pytest.mark.parametrize(
    "plant",
    [
        _symlink_main,
        _hardlink_main,
        _fifo_main,
        _dir_main,
        _symlink_project,
        _file_as_project,
        _symlink_subagents,
        _symlink_subagent_file,
        _symlink_meta,
    ],
)
@pytest.mark.parametrize("form", [[], ["--json"], ["--paths"], ["--raw", "--subagents"]])
def test_unsafe_files_fail_and_are_never_read(
    st: Store,
    clock: Clock,
    projects: Path,
    capfd: pytest.CaptureFixture[str],
    plant: Any,
    form: list[str],
) -> None:
    session(st, clock)
    plant(projects)
    claude_id(st, CID1, 1, "startup", str(projects / PROJECT / f"{CID1}.jsonl"))
    code = main(["transcript", SID, *form])
    out, err = capfd.readouterr()
    assert code == 12
    assert "secret" not in out
    assert "unsafe transcript file" in err


def test_unsafe_error_json(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    _symlink_main(projects)
    claude_id(st, CID1, 1, "startup", str(projects / PROJECT / f"{CID1}.jsonl"))
    code, obj, _ = run_json(["transcript", SID], capsys)
    assert code == 12
    validate(obj, ERROR_SCHEMA)
    assert obj["error"]["code"] == "failed"


def test_unrelated_names_in_the_subagents_directory_are_ignored(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, b"")))
    directory = projects / PROJECT / CID1 / "subagents"
    directory.mkdir(parents=True)
    (directory / "notes.txt").symlink_to("/etc/passwd")
    (directory / "agent-../x.jsonl".replace("/", "_")).write_text("")  # agent-.._x: bad id
    (directory / "agent-\x1b.jsonl").write_text("")
    (directory / "agent-ok.jsonl").write_text("")
    _, obj, _ = run_json(["transcript", SID], capsys)
    assert [s["agent_id"] for s in obj["files"][0]["subagents"]] == ["ok"]


def test_oversized_or_invalid_meta_gives_null_fields(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, b"")))
    big = json.dumps({"description": "x" * transcripts.META_MAX_BYTES}).encode()
    write_subagent(projects, CID1, "big", b"", big)
    write_subagent(projects, CID1, "bad", b"", b"[not json")
    write_subagent(projects, CID1, "arr", b"", b"[1]")
    _, obj, _ = run_json(["transcript", SID], capsys)
    assert len(obj["files"][0]["subagents"]) == 3
    for sub in obj["files"][0]["subagents"]:
        assert (sub["agent_type"], sub["description"], sub["tool_use_id"]) == (None, None, None)


def test_reading_stops_at_the_deadline(
    st: Store, clock: Clock, projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session(st, clock)
    path = write_main(projects, CID1, jsonl(user("x")) * 10)
    ticks = iter(range(1000))
    reader = transcripts.Reader(deadline=3, clock=lambda: float(next(ticks)))
    loc = transcripts.locate(str(path), CID1, transcripts.projects_roots(os.environ))
    assert loc is not None
    with pytest.raises(FailedError, match="longer than 3 seconds"):
        listing = transcripts.discover(
            SID,
            [
                {
                    "claude_session_id": CID1,
                    "run_no": 1,
                    "source": "startup",
                    "transcript_path": str(path),
                }
            ],
            transcripts.projects_roots(os.environ),
            reader,
        )
        list(transcripts.Renderer(reader).lines(listing))


def test_the_store_is_closed_before_files_are_read(
    st: Store,
    clock: Clock,
    projects: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A slow file system must not hold state.db open (and its readers' snapshot)."""
    session(st, clock)
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, b"")))
    real_discover = transcripts.discover
    closed: list[bool] = []
    real_close = Store.close

    def on_close(self: Store) -> None:
        closed.append(True)
        real_close(self)

    def checking_discover(*args: Any, **kwargs: Any) -> transcripts.Listing:
        assert closed, "state.db is still open"
        return real_discover(*args, **kwargs)

    monkeypatch.setattr(Store, "close", on_close)
    monkeypatch.setattr(transcripts, "discover", checking_discover)
    assert main(["transcript", SID]) == 0
    capsys.readouterr()


@pytest.mark.parametrize("form", [[], ["--raw"]])
def test_a_read_error_fails_with_the_path(
    st: Store,
    clock: Clock,
    projects: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    form: list[str],
) -> None:
    session(st, clock)
    path = write_main(projects, CID1, jsonl(user("x")))
    claude_id(st, CID1, 1, "startup", str(path))

    def broken(fd: int, size: int) -> bytes:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(transcripts.os, "read", broken)
    assert main(["transcript", SID, *form]) == 12
    err = capsys.readouterr().err
    assert f"cannot read transcript file ({path}: Input/output error)" in err


# Review round 1


class FakeTime:
    """A monotonic clock that moves only when a test says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def rows(*cids: str, projects: Path) -> list[dict[str, Any]]:
    return [
        {
            "claude_session_id": cid,
            "run_no": 1,
            "source": "startup",
            "transcript_path": str(projects / PROJECT / f"{cid}.jsonl"),
        }
        for cid in cids
    ]


def test_time_waiting_for_the_text_consumer_does_not_count(projects: Path) -> None:
    write_main(projects, CID1, jsonl(*(user(f"line {n}") for n in range(5))))
    write_subagent(projects, CID1, "a1", jsonl(user("sub")), {"toolUseId": "t"})
    now = FakeTime()
    reader = transcripts.Reader(deadline=3, clock=now)
    listing = transcripts.discover(
        SID, rows(CID1, projects=projects), transcripts.projects_roots(os.environ), reader
    )
    lines = []
    for line in transcripts.Renderer(reader, subagents=True).lines(listing):
        lines.append(line)
        now.now += 100  # a slow pager
    assert "[user] line 4" in lines and "  | [user] sub" in lines


def test_time_waiting_for_the_raw_consumer_does_not_count(
    projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcripts, "CHUNK_BYTES", 8)
    data = jsonl(*(user(f"line {n}") for n in range(5)))
    write_main(projects, CID1, data)
    now = FakeTime()
    reader = transcripts.Reader(deadline=3, clock=now)
    listing = transcripts.discover(
        SID, rows(CID1, projects=projects), transcripts.projects_roots(os.environ), reader
    )

    class SlowSink:
        def __init__(self) -> None:
            self.data = b""

        def write(self, chunk: bytes) -> int:
            now.now += 100
            self.data += chunk
            return len(chunk)

    sink = SlowSink()
    transcripts.write_raw(listing, reader, sink, subagents=True)  # type: ignore[arg-type]
    assert sink.data == data


def test_slow_reads_still_fail_after_the_deadline(
    st: Store,
    clock: Clock,
    projects: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(transcripts, "CHUNK_BYTES", 8)
    session(st, clock)
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, jsonl(user("x" * 200)))))
    now = FakeTime()
    real_reader, real_read = transcripts.Reader, os.read

    def slow_read(fd: int, size: int) -> bytes:
        now.now += 2
        return real_read(fd, size)

    monkeypatch.setattr(transcripts, "Reader", lambda: real_reader(deadline=3, clock=now))
    monkeypatch.setattr(transcripts.os, "read", slow_read)
    assert main(["transcript", SID]) == 12
    assert "longer than 3 seconds" in capsys.readouterr().err


def _plant_tool_results_symlink(projects: Path) -> None:
    real = projects.parent / "elsewhere"
    real.mkdir()
    (real / "abc.txt").write_text("secret")
    (projects / PROJECT / CID1).mkdir(parents=True)
    (projects / PROJECT / CID1 / "tool-results").symlink_to(real)


@pytest.mark.parametrize("form", [[], ["--json"], ["--paths"], ["--raw"]])
def test_an_unsafe_tool_results_directory_fails_before_any_output(
    st: Store,
    clock: Clock,
    projects: Path,
    capfd: pytest.CaptureFixture[str],
    form: list[str],
) -> None:
    session(st, clock)
    data = jsonl(user("hello"), tool_result("t", "saved to tool-results/abc.txt"))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    _plant_tool_results_symlink(projects)
    code = main(["transcript", SID, *form])
    out, err = capfd.readouterr()
    assert code == 12
    if form == ["--json"]:
        assert json.loads(out)["error"]["message"] == "unsafe transcript file"
    else:
        assert out == ""
        assert "unsafe transcript file" in err


def _symlink_entry(path: Path) -> None:
    target = path.parent.parent / "secret.txt"
    target.write_text("secret")
    path.symlink_to(target)


def _fifo_entry(path: Path) -> None:
    os.mkfifo(path)


def _hardlink_entry(path: Path) -> None:
    target = path.parent.parent / "secret.txt"
    target.write_text("secret")
    os.link(target, path)


@pytest.mark.parametrize("plant", [_symlink_entry, _fifo_entry, _hardlink_entry])
def test_an_unsafe_offloaded_entry_is_not_referenced(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str], plant: Any
) -> None:
    session(st, clock)
    results = projects / PROJECT / CID1 / "tool-results"
    results.mkdir(parents=True)
    plant(results / "abc.txt")
    (results / "ok.txt").write_text("fine")
    data = jsonl(tool_result("t", "saved to tool-results/abc.txt and tool-results/ok.txt"))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    code, out, err = run(["transcript", SID], capsys)
    assert code == 0
    lines = out.splitlines()
    assert "[offloaded result not shown: unsafe file]" in lines
    assert f"[offloaded] {results}/ok.txt" in lines
    assert not any("abc.txt" in line and line.startswith("[offloaded]") for line in lines)
    assert f"offloaded tool result not shown: {results}/abc.txt" in err


def test_raw_separates_files_without_a_final_newline(
    st: Store, clock: Clock, projects: Path, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    session(st, clock)
    main_data = jsonl(user("a")) + b'{"type": "user"'  # caught mid-record
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, main_data)))
    write_subagent(projects, CID1, "a1", b"", None)  # empty: adds nothing
    write_subagent(projects, CID1, "b2", b'{"sub": 1}')
    write_subagent(projects, CID1, "c3", b'{"sub": 2}\n')
    code, out = run_raw(["transcript", SID, "--raw", "--subagents"], capfdbinary)
    assert code == 0
    assert out == main_data + b'\n{"sub": 1}\n{"sub": 2}\n'
    code, out = run_raw(["transcript", SID, "--raw"], capfdbinary)
    assert out == main_data  # nothing follows: unchanged


def test_raw_warns_about_missing_subagents_only_with_subagents(
    full: Path, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert main(["transcript", SID, "--raw"]) == 0
    err = capfdbinary.readouterr().err.decode()
    assert "subagent transcript file missing" not in err
    assert "transcript file missing" in err
    assert main(["transcript", SID, "--raw", "--subagents"]) == 0
    err = capfdbinary.readouterr().err.decode()
    assert "subagent transcript file missing" in err


def test_unrelated_names_do_not_use_up_the_entry_cap(
    st: Store,
    clock: Clock,
    projects: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(transcripts, "MAX_DIR_ENTRIES", 5)
    session(st, clock)
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, b"")))
    directory = projects / PROJECT / CID1 / "subagents"
    directory.mkdir(parents=True)
    for n in range(6):
        (directory / f"agent-{n}.txt").write_text("")  # sorts before agent-real.jsonl
    write_subagent(projects, CID1, "real", b"")
    code, obj, err = run_json(["transcript", SID], capsys)
    assert code == 0
    assert [s["agent_id"] for s in obj["files"][0]["subagents"]] == ["real"]
    assert "entries" not in err


def test_more_matching_entries_than_the_cap_warn(
    st: Store,
    clock: Clock,
    projects: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(transcripts, "MAX_DIR_ENTRIES", 2)
    session(st, clock)
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, b"")))
    for name in ("a", "b", "c"):
        write_subagent(projects, CID1, name, b"")
    code, obj, err = run_json(["transcript", SID], capsys)
    assert code == 0
    assert len(obj["files"][0]["subagents"]) == 2
    assert "more than 2 entries" in err


def test_a_huge_directory_is_scanned_only_so_far_and_warns(
    st: Store,
    clock: Clock,
    projects: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(transcripts, "MAX_DIR_ENTRIES", 2)
    session(st, clock)
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, b"")))
    directory = projects / PROJECT / CID1 / "subagents"
    directory.mkdir(parents=True)
    for n in range(10 * 2 + 5):
        (directory / f"junk-{n}.txt").write_text("")
    code, _obj, err = run_json(["transcript", SID], capsys)
    assert code == 0
    assert "more than 20 entries" in err


def test_tool_results_are_listed_once_per_claude_id(
    full: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listed: list[str] = []
    real_list_dir = transcripts.Reader.list_dir

    def counting(self: transcripts.Reader, loc: Any, segments: Any, *args: Any, **kw: Any) -> Any:
        if segments[-1:] == ("tool-results",):
            listed.append(segments[0])
        return real_list_dir(self, loc, segments, *args, **kw)

    monkeypatch.setattr(transcripts.Reader, "list_dir", counting)
    render(["transcript", SID, "--subagents"], capsys)
    assert sorted(listed) == sorted({CID1, CID2, CID3})


@pytest.mark.parametrize(("records", "code"), [(2000, 1), (1, 0)])
def test_a_closed_stdout_ends_the_command_without_noise(
    st: Store, clock: Clock, projects: Path, records: int, code: int
) -> None:
    """interface.md, Exit codes: a write that fails while the command runs exits 1; output
    that fits in the stream buffer fails only at the final flush and keeps the exit code."""
    from .test_main import entry_code, run_python

    session(st, clock)
    data = jsonl(*(user(f"line {n} " + "x" * 100) for n in range(records)))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    proc = run_python(entry_code(["transcript", SID]), broken=("stdout",))
    assert (proc.returncode, proc.stderr) == (code, "")


def _home_root(home: Path) -> None:
    (home / ".claude").mkdir()
    (home / ".claude" / "projects").symlink_to(home)


def _ancestor_root(home: Path) -> None:
    (home / ".claude").mkdir()
    (home / ".claude" / "projects").symlink_to(home.parent)


def _credential_root(home: Path) -> None:
    (home / ".local" / "state").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude" / "projects").symlink_to(home / ".local")


@pytest.mark.parametrize("plant", [_home_root, _ancestor_root, _credential_root])
def test_a_projects_root_at_or_above_home_is_not_used(
    st: Store,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    capfdbinary: pytest.CaptureFixture[bytes],
    plant: Any,
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home = Path(os.environ["HOME"])
    plant(home)
    session(st, clock)
    (home / ".ssh").mkdir()
    (home / ".ssh" / f"{CID1}.jsonl").write_text("secret key")
    recorded = home / ".claude" / "projects" / ".ssh" / f"{CID1}.jsonl"
    claude_id(st, CID1, 1, "startup", str(recorded))
    assert main(["transcript", SID, "--json"]) == 0
    out, err = capfdbinary.readouterr()
    assert json.loads(out)["files"][0]["path"] is None
    assert b"projects directory" in err and b"is not used" in err
    code, out = run_raw(["transcript", SID, "--raw"], capfdbinary)
    assert code == 0 and b"secret" not in out


def test_projects_roots_drops_home_and_its_ancestors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "projects").symlink_to(home)
    warnings: list[str] = []
    assert transcripts.projects_roots({"HOME": str(home)}, warnings) == []
    assert len(warnings) == 1
    config = tmp_path / "config"
    config.mkdir()
    assert transcripts.projects_roots(
        {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(config)}, []
    ) == [str(config / "projects")]
    slash = tmp_path / "slash"
    (slash / "projects").parent.mkdir()
    (slash / "projects").symlink_to("/")
    assert (
        transcripts.projects_roots({"HOME": str(home), "CLAUDE_CONFIG_DIR": str(slash)}, []) == []
    )


@pytest.mark.parametrize(("mode", "code"), [(0o777, 12), (0o757, 12), (0o775, 0), (0o700, 0)])
def test_the_projects_root_must_not_be_writable_by_others(
    st: Store,
    clock: Clock,
    projects: Path,
    capsys: pytest.CaptureFixture[str],
    mode: int,
    code: int,
) -> None:
    session(st, clock)
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, jsonl(user("hi")))))
    projects.chmod(mode)
    got, out, err = run(["transcript", SID], capsys)
    assert got == code
    if code:
        assert out == "" and "unsafe transcript file" in err and "writable by others" in err
    else:
        assert "[user] hi" in out.splitlines()


def test_the_clip_never_cuts_an_escape(
    st: Store, clock: Clock, projects: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session(st, clock)
    limit = transcripts.LINE_MAX_CHARS
    data = jsonl(user("a" * (limit - 2) + "\x1b[31m"), user("b" * (limit - 1) + "é̂z"))
    claude_id(st, CID1, 1, "startup", str(write_main(projects, CID1, data)))
    lines = render(["transcript", SID], capsys)
    assert lines[2] == f"[user] {'a' * (limit - 2)}\\x1b[ ... (3 more characters)"
    assert lines[3] == f"[user] {'b' * (limit - 1)} ... (4 more characters)"
