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
import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from claude_session import cli, endmap, errors

from .golden import GOLDEN_ROOT, golden_name, schema_id_for

ROOT = Path(__file__).resolve().parent.parent
INTERFACE = (ROOT / "docs" / "interface.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
SCHEMAS = ROOT / "schemas"
SRC = ROOT / "src" / "claude_session"

# Commands whose handler is not implemented on this line yet: command -> the issue that owns it.
AWAITING: dict[str, int] = {}

# End reasons in the schema that no code produces yet: reason -> the issue that owns it. An
# entry fails the test the moment the code produces it (stale).
AWAITING_END_REASONS: dict[str, int] = {}

# Flags every command takes (Global flags), and argparse's own help flags.
GLOBAL_FLAGS = {"--json", "--config", "--verbose", "--quiet", "--yes", "--version"}
HELP_FLAGS = {"-h", "--help"}

USAGE_START = re.compile(
    r"claude-session ((?:[a-z][a-z-]*)(?: (?:install|uninstall|add|remove))?)(?![a-z-])"
)
FLAG = re.compile(r"--[a-z][a-z-]*")


def section(heading: str, level: int = 2, text: str = INTERFACE) -> str:
    """The body of the section whose heading line is ``heading`` (up to the next heading of the
    same or a higher level)."""
    marks = "#" * level
    match = re.search(
        rf"^{marks} {re.escape(heading)}\n(.*?)(?=^#{{1,{level}}} |\Z)",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match, f"no section {heading!r} in interface.md"
    return match.group(1)


def schema(name: str) -> Any:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def options(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}  # noqa: SLF001


def usage_lines(text: str = INTERFACE) -> list[tuple[str, str]]:
    """``(command, usage)`` of every ``claude-session ...`` usage in the code blocks of the
    Commands section; a usage includes its indented continuation lines."""
    found: list[tuple[str, str]] = []
    in_block = False
    for line in section("Commands", text=text).splitlines():
        if line.startswith("```"):
            in_block = not in_block
            continue
        if not in_block:
            continue
        match = USAGE_START.match(line)
        if match:
            found.append((match.group(1), line))
        elif line[:1].isspace() and found:
            command, usage = found[-1]
            found[-1] = (command, f"{usage}\n{line}")
    return found


def launch_flag_rows(text: str = INTERFACE) -> list[tuple[set[str], str]]:
    """``(flags, meaning)`` of each row of the Launch flags table."""
    rows = re.findall(
        r"^\| (`--.*?`) \| .*? \| (.*) \|$", section("Launch flags", text=text), flags=re.M
    )
    return [(set(FLAG.findall(cell)), meaning) for cell, meaning in rows]


def launch_flags(text: str = INTERFACE) -> set[str]:
    """The flags of the Launch flags table."""
    return {flag for flags, _ in launch_flag_rows(text) for flag in flags}


def resume_only_launch_flags(text: str = INTERFACE) -> set[str]:
    """Launch flags whose row restricts them to resuming (``Resume ... only``): the usage lines
    of `start` and `resume` name them; `exec` does not take them."""
    return {
        flag
        for flags, meaning in launch_flag_rows(text)
        if re.match(r"Resume\b[^.]*\bonly\b", meaning)
        for flag in flags
    }


def documented_flags(text: str = INTERFACE) -> dict[str, set[str]]:
    """Per command, the flags interface.md documents for it: its usage lines (with their
    continuation lines), plus, where a usage takes ``[launch flags]``, the Launch flags table
    (without the resume-only rows). `verify-secret` has no usage block: its Internal commands
    entry documents it."""
    documented: dict[str, set[str]] = {}
    table = launch_flags(text) - resume_only_launch_flags(text)
    for command, usage in usage_lines(text):
        flags = documented.setdefault(command, set())
        flags.update(FLAG.findall(usage))
        if "[launch flags]" in usage:
            flags.update(table)
    internal = re.search(
        r"^- `verify-secret [^`]*`.*$", section("Internal commands", 3, text), flags=re.M
    )
    assert internal, "no verify-secret entry under Internal commands"
    documented["verify-secret"] = set(FLAG.findall(internal.group(0)))
    return documented


def flag_mismatches(text: str = INTERFACE) -> list[tuple[str, str, str]]:
    """``(command, flag, problem)`` for every flag that a command's own parser and the
    interface (``text``) disagree on, ignoring the global and help flags."""
    leaves = cli.build_parser().leaves
    documented = documented_flags(text)
    mismatches = []
    for command, parser in sorted(leaves.items()):
        accepted = options(parser) - GLOBAL_FLAGS - HELP_FLAGS
        named = documented.get(command, set()) - GLOBAL_FLAGS
        mismatches += [(command, flag, "undocumented") for flag in sorted(accepted - named)]
        mismatches += [(command, flag, "not accepted") for flag in sorted(named - accepted)]
    return mismatches


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


def test_each_command_documents_exactly_the_flags_its_parser_accepts() -> None:
    assert flag_mismatches() == []


@pytest.mark.parametrize(
    ("command", "removed", "flag"),
    [
        ("stop", " [--grace <duration>]", "--grace"),
        ("cron install", " [--interval <minutes>]", "--interval"),
        ("resume", " [--fork]", "--fork"),
        ("list", " [--live]", "--live"),  # on a continuation line
    ],
)
def test_the_flag_audit_detects_a_flag_removed_from_interface_md(
    command: str, removed: str, flag: str
) -> None:
    """The audit runs over a copy of interface.md whose usage of ``command`` lost ``flag``."""
    usage = next(u for c, u in usage_lines() if c == command)
    assert removed in usage
    mutated = INTERFACE.replace(usage, usage.replace(removed, "", 1), 1)
    assert mutated != INTERFACE
    assert (command, flag, "undocumented") in flag_mismatches(mutated)


def test_the_flag_audit_detects_a_documented_flag_the_parser_lacks() -> None:
    usage = next(u for c, u in usage_lines() if c == "reconcile")
    mutated = INTERFACE.replace(usage, usage.replace("[--dry-run]", "[--dry-run] [--bogus]"), 1)
    assert ("reconcile", "--bogus", "not accepted") in flag_mismatches(mutated)


def test_awaiting_commands_are_exactly_the_unimplemented_ones() -> None:
    unimplemented = {command for command in cli.HANDLERS if not handler_is_real(command)}
    assert unimplemented == set(AWAITING), (
        "a command left AWAITING (or joined it): update AWAITING, the README table and "
        "add the golden files the schema test below asks for"
    )


def readme_commands() -> list[tuple[set[str], str]]:
    """``(commands, availability)`` of each row of the README's Commands table. Every code span
    of the first cell names commands: ``label add|remove <id>`` names `label add` and
    `label remove`, ``report done|blocked`` names `report`."""
    leaves = set(cli.build_parser().leaves)
    rows = []
    for line in section_of_readme("Commands").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
        commands: set[str] = set()
        for span in re.findall(r"`([^`]+)`", cells[0]):
            words = span.replace("\\|", "|").split()
            base = words[0]
            alternatives = words[1].split("|") if len(words) > 1 else []
            named = {f"{base} {alt}" for alt in alternatives} & leaves
            commands |= named or {base}
        rows.append((commands, cells[-1]))
    return rows


def test_the_readme_table_names_every_command() -> None:
    named = {command for commands, _ in readme_commands() for command in commands}
    assert set(cli.build_parser().leaves) <= named, set(cli.build_parser().leaves) - named


def test_the_readme_marks_exactly_the_awaiting_commands_as_coming() -> None:
    coming = {
        command
        for commands, availability in readme_commands()
        if "coming" in availability
        for command in commands
    }
    assert coming == set(AWAITING)


def section_of_readme(heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", README, flags=re.DOTALL | re.MULTILINE
    )
    assert match, heading
    return match.group(1)


def test_every_schema_of_an_implemented_command_has_a_golden_file() -> None:
    awaiting = {command.replace(" ", "-") for command in AWAITING}
    hit = {schema_id_for(golden_name(p)) for p in GOLDEN_ROOT.rglob("*.json")}
    expected = {
        f"claude-session/cli/{path.stem}.json"
        for path in (SCHEMAS / "cli").glob("*.json")
        # outcome.json holds the shared $defs
        if path.stem not in awaiting and path.stem != "outcome"
    } | {f"claude-session/hooks/{path.name}" for path in (SCHEMAS / "hooks").glob("*.json")}
    missing = sorted(expected - hit)
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


# Module-level names whose value only declares a vocabulary (it mirrors a schema enum) and so
# produces nothing: their strings do not count as produced end reasons.
VOCABULARY_DECLARATIONS = {"logs.py": {"REASONS"}}


def code_string_constants(source: str, skip: set[str] | None = None) -> set[str]:
    """The string constants of the Python ``source``, docstrings excluded (comments are not
    part of the tree), and those inside the assignments to the names in ``skip``."""
    tree = ast.parse(source)
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in (skip or set()) for target in node.targets
        ):
            skipped |= {id(sub) for sub in ast.walk(node.value)}
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                skipped.add(id(first.value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skipped
    }


def test_code_string_constants_ignore_docstrings_and_comments() -> None:
    source = (
        '"""user_exit"""\n# "claude_too_old"\nKNOWN = {"normal"}\n'
        'def f():\n    """x"""\n    return "done"\n'
    )
    assert code_string_constants(source, {"KNOWN"}) == {"done"}


def test_every_end_reason_in_the_schema_is_produced_by_the_code() -> None:
    defs = schema("cli/outcome.json")["$defs"]
    reasons = end_reasons(defs)
    constants: set[str] = set()
    for path in sorted(SRC.glob("*.py")):
        constants |= code_string_constants(
            path.read_text(encoding="utf-8"), VOCABULARY_DECLARATIONS.get(path.name)
        )
    dynamic = {"signal_"}  # `signal_<name>`: built from the signal's name
    assert "signal_" in constants
    missing = {
        reason
        for reason in reasons
        if reason not in constants and not any(reason.startswith(p) for p in dynamic)
    }
    # An awaiting reason the code now produces is stale; any other missing one is a gap.
    assert missing == set(AWAITING_END_REASONS), (
        f"not produced: {sorted(missing - set(AWAITING_END_REASONS))}; "
        f"stale AWAITING_END_REASONS: {sorted(set(AWAITING_END_REASONS) - missing)}"
    )
    assert set(AWAITING_END_REASONS) <= reasons


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
    assert documented == schema_codes == classes


@pytest.mark.parametrize("name", sorted(p.name for p in (SCHEMAS / "cli").glob("*.json")))
def test_every_cli_schema_is_named_in_interface_md(name: str) -> None:
    assert f"schemas/cli/{name}" in INTERFACE


# Per-outcome goldens (issue #127): every documented run state, needs-input reason, outcome,
# end reason and report status appears in at least one schema-validated golden of the command
# that can show it. Coverage is read from the golden files' contents, not their names.

OUTCOME_COMMANDS = {"exec", "resume", "status", "wait"}  # outcome and end reason (status: .run)


def documented_outcomes(text: str = INTERFACE) -> set[str]:
    """The outcomes of the ``Outcome -> exit code`` table."""
    match = re.search(r"^\*\*Outcome → exit code\*\*.*?(?=^## )", text, re.DOTALL | re.MULTILINE)
    assert match, "no Outcome -> exit code table in interface.md"
    return set(re.findall(r"^\| `([a-z-]+)`", match.group(0), flags=re.MULTILINE))


def documented_end_reasons(text: str = INTERFACE) -> set[str]:
    """The end reasons the End reasons table and the launch validation row of the Launch
    phase table name; ``signal_<name>`` and ``exit_<code>`` are families."""
    table = re.search(r"^\*\*End reasons\*\*.*?(?=^Per outcome)", text, re.DOTALL | re.MULTILINE)
    row = re.search(r"^\| launch validation failed \|.*$", text, re.MULTILINE)
    assert table and row, "no End reasons table or launch validation row in interface.md"
    return set(re.findall(r"`([a-z_<>]+)`", table.group(0) + row.group(0)))


def documented_states(text: str = INTERFACE) -> set[str]:
    body = section("States", text=text).split(NEEDS_INPUT_MARKER)[0]
    return set(re.findall(r"`([a-z-]+)`", body)) - {"state"}


NEEDS_INPUT_MARKER = "`needs-input` carries"


def documented_needs_input_reasons(text: str = INTERFACE) -> set[str]:
    states = section("States", text=text)
    assert NEEDS_INPUT_MARKER in states, (
        f"interface.md, States: no paragraph starting {NEEDS_INPUT_MARKER!r}; "
        "tests/test_interface_audit.py reads the needs-input reasons from it"
    )
    body = states.split(NEEDS_INPUT_MARKER)[1].split("Pressing Esc")[0]
    return set(re.findall(r"`([a-z]+)` \(", body))


def golden_documents(root: Path = GOLDEN_ROOT) -> list[tuple[str, Any]]:
    """``(command, output)`` of every command golden (the hook payloads are not command
    outputs)."""
    return [
        (schema_id_for(golden_name(path, root)).rsplit("/", 1)[1].removesuffix(".json"), output)
        for path in sorted(root.glob("*.json"))
        for output in [json.loads(path.read_text(encoding="utf-8"))]
    ]


def run_of(command: str, output: Any) -> dict[str, Any] | None:
    """The object that carries the run's outcome and end reason: the output itself for the
    outcome object (``exec``, ``resume``, ``wait``), ``run`` for ``status``."""
    if not isinstance(output, dict):
        return None
    run = output.get("run") if command == "status" else output
    return run if command in OUTCOME_COMMANDS and isinstance(run, dict) else None


def covered_by_goldens(documents: list[tuple[str, Any]]) -> dict[str, set[Any]]:
    seen: dict[str, set[Any]] = {
        key: set() for key in ("state", "reason", "outcome", "end_reason", "report")
    }
    for command, output in documents:
        run = run_of(command, output)
        if command == "status":
            seen["state"] |= {output["state"], *([run["state"]] if run else [])}
            seen["reason"] |= {output["needs_input_reason"]}
        if run is not None:
            seen["outcome"] |= {run["outcome"]}
            seen["end_reason"] |= {run["end_reason"]}
        if command == "report":
            seen["report"] |= {output["status"]}
    return seen


def goldens_missing(
    text: str = INTERFACE, documents: list[tuple[str, Any]] | None = None
) -> list[str]:
    """What interface.md (``text``) documents that no golden shows, as ``kind value``."""
    seen = covered_by_goldens(golden_documents() if documents is None else documents)
    missing = [f"state {s}" for s in sorted(documented_states(text) - seen["state"])]
    missing += [
        f"needs_input_reason {r}"
        for r in sorted(documented_needs_input_reasons(text) - seen["reason"])
    ]
    missing += [f"outcome {o}" for o in sorted(documented_outcomes(text) - seen["outcome"])]
    for reason in sorted(documented_end_reasons(text)):
        # A family such as `exit_<code>` is covered by any value of it.
        family = re.sub(r"<[a-z]+>", ".+", reason)
        if not any(re.fullmatch(family, s) for s in seen["end_reason"] if s is not None):
            missing.append(f"end_reason {reason}")
    statuses = set(schema("cli/report.json")["properties"]["status"]["enum"])
    missing += [f"report status {s}" for s in sorted(statuses - seen["report"])]
    return missing


def test_every_documented_state_outcome_and_end_reason_has_a_golden() -> None:
    assert goldens_missing() == []


def test_the_documented_vocabularies_are_the_schema_ones() -> None:
    defs = schema("cli/outcome.json")["$defs"]
    assert documented_needs_input_reasons() == set(defs["needs_input_reason"]["enum"]) - {None}
    assert documented_outcomes() == set(defs["outcome_value"]["enum"]) - {None}
    assert documented_end_reasons() - {"signal_<name>", "exit_<code>"} == end_reasons(defs)
    assert {"signal_<name>", "exit_<code>"} <= documented_end_reasons()  # the two families


def without(
    keep: Any,
) -> list[tuple[str, Any]]:
    """The command goldens for which ``keep(command, output)`` holds."""
    assert goldens_missing() == []  # a mutation is only a proof from a complete start
    return [(c, o) for c, o in golden_documents() if keep(c, o)]


def test_the_golden_audit_detects_a_state_with_no_golden() -> None:
    documents = without(lambda c, o: not (c == "status" and o["state"] == "idle"))
    assert "state idle" in goldens_missing(documents=documents)


def test_the_golden_audit_detects_an_end_reason_with_no_golden() -> None:
    def keep(command: str, output: Any) -> bool:
        run = run_of(command, output)
        return run is None or run["end_reason"] != "api_error"

    assert "end_reason api_error" in goldens_missing(documents=without(keep))


def test_the_golden_audit_detects_a_family_with_no_golden() -> None:
    def keep(command: str, output: Any) -> bool:
        run = run_of(command, output)
        return run is None or not str(run["end_reason"]).startswith("signal_")

    assert "end_reason signal_<name>" in goldens_missing(documents=without(keep))


def test_the_golden_audit_detects_a_report_status_with_no_golden() -> None:
    documents = without(lambda c, o: not (c == "report" and o["status"] == "failed"))
    assert "report status failed" in goldens_missing(documents=documents)


def test_the_golden_audit_detects_a_documented_state_that_is_new() -> None:
    text = INTERFACE.replace("`lost`, `not-started`.", "`lost`, `not-started`, `paused`.", 1)
    assert text != INTERFACE
    assert goldens_missing() == []
    assert "state paused" in goldens_missing(text)


def test_the_golden_audit_detects_a_documented_end_reason_that_is_new() -> None:
    text = INTERFACE.replace("| session | `normal`,", "| session | `normal`, `disk_full`,", 1)
    assert text != INTERFACE
    assert goldens_missing() == []
    assert "end_reason disk_full" in goldens_missing(text)


def test_the_golden_audit_detects_a_needs_input_reason_with_no_golden() -> None:
    documents = without(
        lambda c, o: not (c == "status" and o["needs_input_reason"] == "elicitation")
    )
    assert "needs_input_reason elicitation" in goldens_missing(documents=documents)


def test_the_golden_audit_detects_an_outcome_with_no_golden() -> None:
    def keep(command: str, output: Any) -> bool:
        run = run_of(command, output)
        return run is None or run["outcome"] != "vetoed"

    assert "outcome vetoed" in goldens_missing(documents=without(keep))


def test_the_golden_audit_detects_a_documented_needs_input_reason_that_is_new() -> None:
    text = INTERFACE.replace(
        "or `escalation` (the run", "`paused` (a pause) or `escalation` (the run", 1
    )
    assert text != INTERFACE
    assert goldens_missing() == []
    assert "needs_input_reason paused" in goldens_missing(text)


def test_the_golden_audit_detects_a_documented_outcome_that_is_new() -> None:
    row = "| `needs-input` | 13 | `exited` |"
    text = INTERFACE.replace(row, f"{row}\n| `paused` | 19 | `exited` |", 1)
    assert "paused" in documented_outcomes(text)
    assert goldens_missing() == []
    assert "outcome paused" in goldens_missing(text)


def test_the_audit_names_a_missing_needs_input_paragraph() -> None:
    text = INTERFACE.replace(NEEDS_INPUT_MARKER, "`needs-input` has", 1)
    with pytest.raises(AssertionError, match="no paragraph starting"):
        documented_needs_input_reasons(text)
