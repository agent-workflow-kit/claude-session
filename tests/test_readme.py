"""The README's examples: every ``claude-session`` command in a shell block passes the real
argument parser and flag rules (as ``main`` applies them), relative links and anchors resolve,
and the quick start (its own ``exec``, ``list`` and ``status`` commands, then ``wait``) runs
end to end against the fake ``claude`` and prints what the README shows."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from claude_session import cli
from claude_session.cli import main
from claude_session.errors import UsageError

from .fakeclaude import claude_bin, write_json
from .test_exec_headless import (  # noqa: F401
    START,
    STOP,
    claude_session_cmd,
    private_home,
    state,
    work,
)

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
# Every fenced block: its info string and body.
FENCE = re.compile(r"^```([^\n`]*)\n(.*?)^```", re.S | re.M)
SHELL_FENCES = {"sh", "bash", "shell", "zsh", "console"}
# The session id the README's examples use.
EXAMPLE_ID = "o72rzt5u"


def shell_blocks(text: str = README) -> list[str]:
    """The bodies of the shell blocks (``console`` prompts stripped)."""
    blocks = []
    for info, body in FENCE.findall(text):
        language = info.strip().split(" ")[0]
        if language not in SHELL_FENCES:
            continue
        if language == "console":
            body = "".join(
                line[2:] if line.startswith("$ ") else "" for line in body.splitlines(True)
            )
        blocks.append(body)
    return blocks


def commands_in(block: str) -> list[list[str]]:
    """The argv (after ``claude-session``) of every command in one shell block. A trailing
    ``\\`` joins lines, a heredoc body and anything after ``#`` is dropped."""
    commands: list[list[str]] = []
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line.startswith("claude-session "):
            continue
        while line.endswith("\\"):
            line = line[:-1] + " " + lines[i].strip()
            i += 1
        heredoc = re.search(r"<<'(\w+)'", line)
        if heredoc:
            while lines[i].strip() != heredoc.group(1):
                i += 1
            i += 1
            line = line[: heredoc.start()]
        words = shlex.split(line, comments=True)
        commands.append(words[1:])
    return commands


def readme_commands() -> list[list[str]]:
    """The argv of every ``claude-session`` command in the README's shell blocks."""
    return [argv for block in shell_blocks() for argv in commands_in(block)]


def check_command(argv: list[str]) -> None:
    """What ``main`` checks before running a command: the parser, then the flag rules
    (``UsageError``, exit 2, for either)."""
    cli.check_flag_rules(cli.parse(cli.build_parser(), argv))


def test_the_readme_has_commands_to_check() -> None:
    commands = readme_commands()
    assert len(commands) >= 20
    assert {c[0] for c in commands} >= {
        "exec",
        "start",
        "resume",
        "status",
        "list",
        "wait",
        "report",
        "kinds",
    }


def test_every_shell_fence_language_is_scanned() -> None:
    blocks = shell_blocks(
        "```bash\nclaude-session list\n```\n```console\n$ claude-session kinds\n```\n"
    )
    assert [commands_in(b) for b in blocks] == [[["list"]], [["kinds"]]]


@pytest.mark.parametrize("argv", readme_commands(), ids=lambda argv: " ".join(argv))
def test_every_readme_command_passes_the_parser_and_flag_rules(argv: list[str]) -> None:
    if argv[0].startswith("-"):
        pytest.skip("a global flag (--version) exits at once")
    # The README names real sessions by example id; only the id's shape is checked.
    check_command(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "--no-such-flag"],
        ["start", "--mode", "headless", "--prompt-file", "prompt.md"],  # start needs --json
        ["status", "abc"],  # an id prefix needs 4 characters
        ["resume", EXAMPLE_ID, "--fork", "--override-escalation"],
    ],
    ids=["unknown-flag", "start-without-json", "short-id", "fork-override-escalation"],
)
def test_the_checker_rejects_a_bad_command(argv: list[str]) -> None:
    with pytest.raises(UsageError):
        check_command(argv)


def slug(heading: str) -> str:
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower().replace("`", ""))
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = re.sub(r"^```.*?^```", "", path.read_text(encoding="utf-8"), flags=re.S | re.M)
    return {slug(m.group(1)) for m in re.finditer(r"^#{1,6} (.+)$", text, re.M)}


def test_relative_links_and_anchors_resolve() -> None:
    checked = 0
    for target in re.findall(r"\]\(([^)\s]+)\)", README):
        if re.match(r"[a-z]+://", target) or target.startswith("../"):
            continue  # external, or the sibling awkit-docs checkout (not in this repo)
        name, _, fragment = target.partition("#")
        path = ROOT / name if name else ROOT / "README.md"
        assert path.exists(), target
        if fragment and path.suffix == ".md":
            assert fragment in anchors(path), target
        checked += 1
    assert checked >= 10


def section(heading: str) -> str:
    """The README text from ``heading`` to the next heading of the same or a higher level."""
    level = heading.split(" ")[0]
    start = README.index(f"\n{heading}\n")
    rest = README[start + len(heading) + 2 :]
    stop = re.search(rf"^#{{1,{len(level)}}} ", rest, re.M)
    return rest if stop is None else rest[: stop.start()]


def text_blocks(text: str) -> list[str]:
    return [body for info, body in FENCE.findall(text) if info.strip() == "text"]


def test_the_quick_start_runs_against_the_fake_claude(
    tmp_path: Path,
    state: Path,  # noqa: F811
    work: Path,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    quick = section("## Quick start")
    (setup, inspect) = shell_blocks(quick)
    (exec_line, list_sample) = text_blocks(quick)
    # The prompt file, as the README writes it.
    echo = re.fullmatch(r'echo "(.*)" > (\S+)', setup.splitlines()[0])
    assert echo is not None
    (work / echo.group(2)).write_text(echo.group(1) + "\n", encoding="utf-8")
    [exec_argv] = commands_in(setup)
    assert exec_argv[0] == "exec" and "--json" not in exec_argv

    # The README's exec, plus only test isolation (the fake claude, a private home).
    # The fake agent files a done report, as the README's sample line ("done (normal)") shows.
    steps = [
        START,
        {"do": "report", "args": ["done", "--summary", "All done."]},
        STOP,
        {"do": "result", "result": "the result text", "total_cost_usd": 0.01, "num_turns": 1},
    ]
    scenario = write_json(tmp_path / "scenario.json", {"steps": steps})
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"claude_bin: {claude_bin(tmp_path / 'fakebin', scenario)}\n", encoding="utf-8")
    proc = subprocess.run(
        [*claude_session_cmd(), *exec_argv, "--config", str(cfg)],
        cwd=work,
        env={**os.environ, **private_home(tmp_path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    sample = exec_line.strip().replace(f"cs-{EXAMPLE_ID}", "@ID@")
    pattern = re.escape(sample).replace("@ID@", r"(cs-[a-z2-7]{8})")
    match = re.fullmatch(pattern, proc.stderr.strip())
    assert match is not None, proc.stderr
    sid = match.group(1)

    # The README's list and status, with its example id replaced by a 4-character prefix
    # (the README: "a unique prefix of at least 4 characters is accepted").
    prefix = sid.removeprefix("cs-")[:4]
    list_argv, status_argv = commands_in(inspect)
    assert list_argv[0] == "list" and status_argv == ["status", EXAMPLE_ID]
    capsys.readouterr()
    assert main(list_argv) == 0
    listing = capsys.readouterr().out.splitlines()
    expected = list_sample.splitlines()
    assert listing[0] == expected[0]  # the header, column widths included
    updated = listing[1].split()[5]
    assert listing[1] == expected[1].replace(f"cs-{EXAMPLE_ID}", sid).replace(
        expected[1].split()[5], updated
    )

    assert main([status_argv[0], prefix]) == 0
    status = capsys.readouterr().out
    assert f"session:        {sid}" in status
    assert "outcome:      done (normal), exit 0" in status
    assert "summary:    All done." in status

    [wait_argv] = [argv for argv in readme_commands() if argv[0] == "wait"]
    wait_argv = [prefix if word == EXAMPLE_ID else word for word in wait_argv]
    assert main(wait_argv) == 0
    waited = json.loads(capsys.readouterr().out)
    assert (waited["outcome"], waited["exit_code"], waited["timed_out"]) == ("done", 0, False)
    assert waited["pending"] is False and waited["run_exit_code"] == 0


def test_the_readme_start_sample_has_the_schema_keys() -> None:
    [sample] = [
        body
        for info, body in FENCE.findall(
            section("### `start`: allocate a session for another process to place")
        )
        if info.strip() == "json"
    ]
    required = json.loads((ROOT / "schemas" / "cli" / "start.json").read_text())["required"]
    assert list(json.loads(sample)) == required
