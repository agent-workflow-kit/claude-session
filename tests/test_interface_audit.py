"""Audit of docs/interface.md against the implementation and the tests (issue #45, 1.0
readiness): every command, flag, state, outcome, end reason, exit code, error code and schema
the interface names exists, and every JSON schema of an implemented command has a golden file.

The documents are parsed, not copied: a command or flag added to interface.md without code, or
code that interface.md does not name, fails here. ``AWAITING`` lists the commands whose
implementation is still on an open pull request; a command leaves it the moment its handler is
real (the test fails on a stale entry), and from then on its schema needs a golden file and
the README must stop marking it as coming.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pytest

from claude_session import cli, endmap, errors

from .golden import GOLDEN_ROOT

ROOT = Path(__file__).resolve().parent.parent
INTERFACE = (ROOT / "docs" / "interface.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
SCHEMAS = ROOT / "schemas"
SRC = ROOT / "src" / "claude_session"

# Commands whose handler is not implemented on this line yet: command -> the issue that owns it.
AWAITING = {
    "label add": 36,
    "label remove": 36,
    "verify-secret": 36,
    "transcript": 37,
    "export": 39,
}

# Documented flags that are not options of the command's own parser: every command takes the
# global flags, and the usage line of `resume` shows the launch flags it takes collectively.
GLOBAL_FLAGS = {"--json", "--config", "--verbose", "--quiet", "--yes", "--version"}


def section(heading: str, level: int = 2) -> str:
    """The body of the section whose heading line is ``heading`` (up to the next heading of the
    same or a higher level)."""
    marks = "#" * level
    match = re.search(
        rf"^{marks} {re.escape(heading)}\n(.*?)(?=^#{{1,{level}}} |\Z)",
        INTERFACE,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match, f"no section {heading!r} in interface.md"
    return match.group(1)


def schema(name: str) -> Any:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def options(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}  # noqa: SLF001


def usage_lines() -> list[tuple[str, str]]:
    """``(command, usage line)`` of every ``claude-session ...`` line in the usage blocks of
    the Commands section."""
    found = []
    for line in section("Commands").splitlines():
        match = re.match(
            r"claude-session ((?:[a-z][a-z-]*)(?: (?:install|uninstall|add|remove))?)", line
        )
        if match:
            found.append((match.group(1), line))
    return found


def launch_flags() -> set[str]:
    """The flags of the Launch flags table."""
    first_cells = re.findall(r"^\| (`--.*?`) \| ", section("Launch flags"), flags=re.MULTILINE)
    return {flag for cell in first_cells for flag in re.findall(r"--[a-z][a-z-]*", cell)}


def handler_is_real(command: str) -> bool:
    return cli.HANDLERS[command] is not cli._not_implemented  # noqa: SLF001


def test_every_documented_command_has_a_parser_leaf_and_a_handler() -> None:
    documented = {command for command, _ in usage_lines()}
    leaves = set(cli.build_parser().leaves)
    assert documented <= leaves, documented - leaves
    assert set(cli.HANDLERS) <= leaves
    # `hook-event` is internal and runs before argument parsing: documented under Internal
    # commands, not by a usage block.
    assert "`hook-event " in section("Internal commands", level=3)


def test_every_command_the_parser_has_is_documented() -> None:
    documented = {command for command, _ in usage_lines()} | {"verify-secret"}
    assert set(cli.build_parser().leaves) <= documented


def test_every_documented_flag_is_accepted_by_its_command() -> None:
    leaves = cli.build_parser().leaves
    missing = []
    for command, line in usage_lines():
        if command not in leaves:
            continue
        accepted = options(leaves[command]) | GLOBAL_FLAGS
        for flag in re.findall(r"--[a-z][a-z-]*", line):
            if flag not in accepted:
                missing.append((command, flag))
    assert not missing, missing


def test_every_launch_flag_is_accepted_by_a_launching_command() -> None:
    leaves = cli.build_parser().leaves
    accepted = options(leaves["exec"]) | options(leaves["start"]) | options(leaves["resume"])
    assert launch_flags() - accepted == set()
    # ... and the table names every launch flag the parser has.
    undocumented = {
        flag
        for flag in options(leaves["exec"])
        if flag.startswith("--")
        and flag not in launch_flags()
        and flag not in GLOBAL_FLAGS
        and flag not in {"--session-id", "--run", "--help"}
    }
    assert undocumented == set()


def test_awaiting_commands_are_exactly_the_unimplemented_ones() -> None:
    unimplemented = {command for command in cli.HANDLERS if not handler_is_real(command)}
    assert unimplemented == set(AWAITING), (
        "a command left AWAITING (or joined it): update AWAITING, the README table and "
        "add the golden files the schema test below asks for"
    )


def test_the_readme_marks_exactly_the_awaiting_commands_as_coming() -> None:
    table = section_of_readme("Commands")
    rows = re.findall(r"^\| `([a-z -]+?)(?: [^`]*)?` \|.*\| ([^|]+) \|$", table, flags=re.MULTILINE)
    coming = {command.strip() for command, availability in rows if "coming" in availability}
    expected = {name.split()[0] for name in AWAITING}
    assert {c.split()[0] for c in coming} == expected


def section_of_readme(heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", README, flags=re.DOTALL | re.MULTILINE
    )
    assert match, heading
    return match.group(1)


def test_every_schema_of_an_implemented_command_has_a_golden_file() -> None:
    awaiting = {command.replace(" ", "-") for command in AWAITING}
    golden = {p.relative_to(GOLDEN_ROOT).as_posix() for p in GOLDEN_ROOT.rglob("*.json")}
    missing = []
    for path in sorted((SCHEMAS / "cli").glob("*.json")):
        name = path.stem
        if name in awaiting or name == "outcome":  # outcome.json holds the shared $defs
            continue
        if not any(g == f"{name}.json" or g.startswith(f"{name}-") for g in golden):
            missing.append(f"cli/{name}")
    for path in sorted((SCHEMAS / "hooks").glob("*.json")):
        if not any(g.startswith(f"hooks/{path.stem}-") for g in golden):
            missing.append(f"hooks/{path.stem}")
    assert not missing, f"no golden file in tests/golden for: {missing}"


def test_documented_states_are_the_schema_states_and_the_cli_states() -> None:
    body = section("States")
    documented = set(re.findall(r"`([a-z-]+)`", body.split("`needs-input` carries")[0]))
    documented -= {"state"}
    defs = schema("cli/outcome.json")["$defs"]
    assert documented == set(defs["state"]["enum"]) == set(cli.STATES)


def test_outcomes_are_the_endmap_outcomes() -> None:
    defs = schema("cli/outcome.json")["$defs"]
    assert set(defs["outcome_value"]["enum"]) - {None} == set(endmap.EXIT_CODES)


def end_reasons(defs: Any) -> set[str]:
    """The strings of the ``end_reason`` definition (a nullable string enum)."""
    (enum,) = [b["enum"] for b in defs["end_reason"]["anyOf"] if "enum" in b]
    return set(enum)


def test_every_end_reason_in_the_schema_is_produced_by_the_code() -> None:
    defs = schema("cli/outcome.json")["$defs"]
    reasons = end_reasons(defs)
    source = "\n".join(p.read_text(encoding="utf-8") for p in sorted(SRC.glob("*.py")))
    dynamic = {"signal_"}  # `signal_<name>`: built from the signal's name
    missing = {
        reason
        for reason in reasons
        if not re.search(rf"[\"'`]{re.escape(reason)}[\"'`]", source)
        and not any(reason.startswith(prefix) for prefix in dynamic)
    }
    assert not missing, missing


def test_every_end_reason_in_the_schema_is_named_in_interface_md() -> None:
    defs = schema("cli/outcome.json")["$defs"]
    undocumented = {r for r in end_reasons(defs) if f"`{r}`" not in INTERFACE}
    assert not undocumented, undocumented


def test_documented_exit_codes_are_the_implemented_ones() -> None:
    documented = {int(code) for code in re.findall(r"^\| (\d+) \|", section("Exit codes"), re.M)}
    implemented = (
        set(endmap.EXIT_CODES.values())
        | {
            cls.exit_code
            for cls in vars(errors).values()
            if isinstance(cls, type) and issubclass(cls, errors.CliError)
        }
        | {22}  # `wait --timeout` expired: the timed-out outcome object, not an error
    )
    assert documented == implemented


def test_documented_error_codes_are_the_schema_error_codes() -> None:
    documented = set(re.findall(r"`([a-z_]+)` \(\d+", section("Error codes", level=3)))
    schema_codes = set(
        schema("cli/error.json")["properties"]["error"]["properties"]["code"]["enum"]
    )
    classes = {
        cls.code
        for cls in vars(errors).values()
        if isinstance(cls, type) and issubclass(cls, errors.CliError) and hasattr(cls, "code")
    }
    assert documented <= schema_codes
    assert classes <= schema_codes


@pytest.mark.parametrize("name", sorted(p.name for p in (SCHEMAS / "cli").glob("*.json")))
def test_every_cli_schema_is_named_in_interface_md(name: str) -> None:
    assert f"schemas/cli/{name}" in INTERFACE
