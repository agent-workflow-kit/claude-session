"""The README's examples: every ``claude-session`` command in a ``sh`` block parses with the
real argument parser, relative links and anchors resolve, and the quick start (headless
``exec``, then ``list``, ``status`` and ``wait``) runs end to end against the fake ``claude``."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from claude_session.cli import build_parser, main

from .test_exec_headless import START, STOP, result_step, run_exec, state, work  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
FENCE = re.compile(r"^```sh\n(.*?)^```", re.S | re.M)


def readme_commands() -> list[list[str]]:
    """The argv (after ``claude-session``) of every command in the README's ``sh`` blocks.
    A trailing ``\\`` joins lines, a heredoc body and anything after ``#`` is dropped."""
    commands: list[list[str]] = []
    for block in FENCE.findall(README):
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


@pytest.mark.parametrize("argv", readme_commands(), ids=lambda argv: " ".join(argv))
def test_every_readme_command_parses(argv: list[str]) -> None:
    if argv[0].startswith("-"):
        pytest.skip("a global flag (--version) exits at once")
    # The README names real sessions by example id; the parser only checks the shape.
    build_parser().parse_args(argv)


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


def test_the_quick_start_runs_against_the_fake_claude(
    tmp_path: Path,
    state: Path,  # noqa: F811
    work: Path,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    steps = [
        START,
        {"do": "report", "args": ["done", "--summary", "All done."]},
        STOP,
        result_step(total_cost_usd=0.01, num_turns=1),
    ]
    run = run_exec(tmp_path, state, work, steps, args=["--label", "demo=quickstart"])
    assert run.code == 0, run.proc.stderr
    sid = run.sid
    capsys.readouterr()

    assert main(["list", "--label", "demo=quickstart"]) == 0
    listing = capsys.readouterr().out.splitlines()
    assert listing[0].split() == ["ID", "STATE", "KIND", "RUN", "MODE", "UPDATED", "LABELS"]
    assert listing[1].split()[:5] == [sid, "exited", "implement", "1", "headless"]
    assert listing[1].split()[-1] == "demo=quickstart,kind=implement"

    prefix = sid.removeprefix("cs-")[:8]  # the README's "cs- prefix is optional"
    assert main(["status", prefix]) == 0
    status = capsys.readouterr().out
    assert f"session:        {sid}" in status
    assert "outcome:      done (normal), exit 0" in status
    assert "summary:    All done." in status

    assert main(["wait", prefix, "--timeout", "5s", "--json"]) == 0
    waited = json.loads(capsys.readouterr().out)
    assert (waited["outcome"], waited["exit_code"], waited["timed_out"]) == ("done", 0, False)
    assert waited["pending"] is False and waited["run_exit_code"] == 0
