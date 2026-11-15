# ruff: noqa: F811
"""``--tools <name>...`` (interface.md, Launch flags; issue #108): a per-call allowlist of Claude
Code built-in tools for isolated kinds, passed as ``claude --tools``. It only narrows the
tools Claude Code offers; permissions (deny first) still apply on top of it."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from claude_session import cli, config, ids, kinds, settings_builder
from claude_session import resume as resume_mod
from claude_session.cli import build_parser
from claude_session.errors import UsageError
from claude_session.settings_builder import Launch, Paths, Tools

from .test_dryrun_cli import cli as run_cli
from .test_dryrun_cli import dry_run, tools, work  # noqa: F401  (fixtures)
from .test_resume import (  # noqa: F401  (fixtures)
    env,
    exec_session,
    flag_value,
    resume,
    run_cs,
    spec,
    starts,
)
from .test_start_adopt import env as start_env  # noqa: F401  (fixture)
from .test_start_adopt import state  # noqa: F401  (fixture)

SID = "cs-abcd2345"
PATHS = Paths(state_root="/state", csession_id=SID, run=1)
TOOLS = Tools(
    interpreter="/opt/py/bin/python3", claude_session="/opt/cs/bin/claude-session", claude="/c"
)


def parse(*argv: str) -> Any:
    return build_parser().parse_args(list(argv))


def resolve(kind: str = "review", **flags: Any) -> kinds.Resolution:
    cfg = config.load(None, env={"XDG_CONFIG_HOME": "/nonexistent-xdg"})
    return kinds.resolve(cfg, kind, mode="headless", flags=flags or None, origin_url=lambda _: None)


def argv_of(resolution: kinds.Resolution) -> Any:
    return settings_builder.build_argv(
        resolution,
        PATHS,
        TOOLS,
        Launch(mode="headless", cwd="/w", prompt_path="/state/sessions/cs-abcd2345/prompt.md"),
    )


# Parsing and names


def test_tools_takes_several_names_and_may_repeat() -> None:
    assert parse("start", "--json", "--tools", "Read", "Grep").tools == ["Read", "Grep"]
    args = parse("start", "--json", "--tools", "Read", "--tools", "Grep", "--kind", "review")
    assert args.tools == ["Read", "Grep"]
    assert parse("start", "--json").tools is None
    assert parse("resume", SID, "--tools", "Read").tools == ["Read"]


@pytest.mark.parametrize(
    "name",
    [
        "Frob",
        "read",  # names are case-sensitive
        "mcp__server__tool",  # MCP tools are governed by --strict-mcp-config, not --tools
        "Bash(git status:*)",  # rule syntax is for permissions, not --tools
        "Skill(foo)",
        "default",  # would widen to every tool
        "",  # would disable every tool
        "Read,Grep",
        "-x",
        "Read\nGrep",
    ],
)
def test_an_unknown_tool_name_is_refused(name: str) -> None:
    args = parse("start", "--json", "--kind", "review", f"--tools={name}")
    with pytest.raises(UsageError) as info:
        cli.check_flag_rules(args)
    assert "--tools" in info.value.message


def test_a_session_id_given_as_a_tool_name_gets_the_order_hint() -> None:
    args = parse("start", "--json", "--kind", "review", "--tools", "Read", "cs-abcd2345")
    with pytest.raises(UsageError) as info:
        cli.check_flag_rules(args)
    assert "resume <id> --tools" in (info.value.detail or "")


def test_resume_without_an_id_but_with_tools_gets_the_order_hint() -> None:
    with pytest.raises(UsageError) as info:
        parse("resume", "--tools", "Read")
    assert "resume <id> --tools" in (info.value.detail or "")
    with pytest.raises(UsageError) as plain:
        parse("resume", "--json")
    assert plain.value.detail is None


def test_known_names_pass_the_flag_rules() -> None:
    args = parse("start", "--json", "--kind", "review", "--tools", "Read", "Grep", "Glob")
    cli.check_flag_rules(args)


def test_the_name_list_is_ascii_identifiers_only() -> None:
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", n) for n in ids.BUILTIN_TOOLS)
    assert all(ids.is_builtin_tool(n) for n in ("Read", "Grep", "Bash", "WebFetch"))
    assert not ids.is_builtin_tool("Read ")
    assert not ids.is_builtin_tool("ＲＥＡＤ")


def test_the_name_list_matches_the_installed_tool_set_and_the_interface_doc() -> None:
    # R2: names found in Claude Code 2.1.325; SlashCommand is not one of them.
    for name in ("TaskOutput", "TaskStop", "EnterPlanMode", "ToolSearch", "LSP", "Monitor"):
        assert name in ids.BUILTIN_TOOLS
    assert "SlashCommand" not in ids.BUILTIN_TOOLS
    doc = (Path(__file__).parent.parent / "docs" / "interface.md").read_text(encoding="utf-8")
    row = next(line for line in doc.splitlines() if line.startswith("| `--tools <name>...`"))
    listed = re.search(r"list kept in the source \(([^)]*)\)", row)
    assert listed is not None
    assert set(re.findall(r"`(\w+)`", listed.group(1))) == set(ids.BUILTIN_TOOLS)


def test_reset_tools_cannot_be_combined_with_tools() -> None:
    args = parse("resume", SID, "--reset", "tools", "--tools", "Read")
    with pytest.raises(UsageError) as info:
        cli.check_flag_rules(args)
    assert "--reset tools cannot be combined with --tools" in info.value.message


# Resolution


def test_an_isolated_kind_keeps_the_names_in_order_without_duplicates() -> None:
    r = resolve("review", tools=["Read", "Grep", "Read"])
    assert r.tools == ("Read", "Grep")
    assert r.origins["tools"] == "flag"
    assert resolve("review").tools is None
    assert "tools" not in resolve("review").origins


def test_a_sandboxed_trusted_kind_is_isolated_too() -> None:
    r = resolve("implement", tools=["Read"], sandbox="required")
    assert r.tools == ("Read",)


def test_a_trusted_kind_refuses_tools() -> None:
    with pytest.raises(UsageError) as info:
        resolve("implement", tools=["Read"])
    assert "--tools" in info.value.message and "isolated" in info.value.message


def test_a_carried_value_is_marked_previous_run_and_refused_when_no_longer_isolated() -> None:
    prev = kinds.Previous(repo=None, flags={"tools": ["Read"]})
    cfg = config.load(None, env={"XDG_CONFIG_HOME": "/nonexistent-xdg"})
    r = kinds.resolve(cfg, "review", mode="headless", previous=prev, origin_url=lambda _: None)
    assert (r.tools, r.origins["tools"]) == (("Read",), "previous_run")
    with pytest.raises(UsageError) as info:
        kinds.resolve(
            cfg,
            "rank",
            mode="headless",
            previous=prev,
            flags={"sandbox": "off"},
            origin_url=lambda _: None,
        )
    assert "--reset tools" in (info.value.detail or "")


@pytest.mark.parametrize("bad", ["Read", [], [1], ["Frob"]])
def test_a_malformed_stored_value_is_refused(bad: Any) -> None:
    with pytest.raises(UsageError):
        resolve("review", tools=bad)


# argv


def test_argv_passes_one_comma_joined_value_before_the_options_that_follow() -> None:
    built = argv_of(resolve("review", tools=["Read", "Grep"]))
    argv = built.argv
    at = argv.index("--tools")
    assert argv[at + 1] == "Read,Grep"
    assert argv[at + 2].startswith("--")  # variadic: always followed by an option
    assert argv.index("--") > at
    assert argv.count("--tools") == 1


def test_no_tools_flag_means_no_argv_entry() -> None:
    assert "--tools" not in argv_of(resolve("review")).argv


def test_a_run_without_bash_is_warned_that_it_cannot_report() -> None:
    assert any("report" in w for w in argv_of(resolve("review", tools=["Read"])).warnings)
    assert not any("report" in w for w in argv_of(resolve("review", tools=["Bash"])).warnings)


def test_the_no_bash_warning_follows_implicit_done() -> None:
    # review: a clean exit counts as done; gate-check: it does not.
    implicit = " ".join(argv_of(resolve("review", tools=["Read"])).warnings)
    assert "implicit" in implicit and "without a report" not in implicit
    explicit = " ".join(argv_of(resolve("gate-check", tools=["Read"])).warnings)
    assert "ends without a report" in explicit and "implicit" not in explicit


def test_a_tool_the_profile_denies_is_dropped_with_a_warning() -> None:
    # review denies WebFetch/WebSearch: deny wins, and --tools never lists a denied tool.
    resolution = resolve("review", tools=["Read", "WebFetch"])
    perm = config.PermissionProfile(permissions={"deny": ["WebFetch", "mcp__*"], "allow": []})
    built = settings_builder.build(
        resolution,
        perm,
        PATHS,
        TOOLS,
        Launch(mode="headless", cwd="/w", prompt_path="/state/sessions/cs-abcd2345/prompt.md"),
        isolation=_isolation(),
    )
    argv = built.argv
    assert argv[argv.index("--tools") + 1] == "Read"
    assert built.tools == ("Read",)
    assert any("WebFetch" in w and "denied" in w for w in built.warnings)


def _build(resolution: kinds.Resolution, deny: list[str]) -> settings_builder.Generated:
    perm = config.PermissionProfile(permissions={"deny": deny, "allow": []})
    return settings_builder.build(
        resolution,
        perm,
        PATHS,
        TOOLS,
        Launch(mode="headless", cwd="/w", prompt_path="/state/sessions/cs-abcd2345/prompt.md"),
        isolation=_isolation(),
    )


def test_a_deny_of_edit_covers_every_file_edit_tool() -> None:
    names = ["Read", "Edit", "MultiEdit", "Write", "NotebookEdit"]
    built = _build(resolve("review", tools=names), ["Edit"])
    assert built.tools == ("Read",)
    for name in names[2:]:
        assert any(name in w and "denied" in w for w in built.warnings)


@pytest.mark.parametrize(("denied", "dropped"), [("Task", "Agent"), ("Agent", "Task")])
def test_task_and_agent_are_aliases_for_a_deny(denied: str, dropped: str) -> None:
    built = _build(resolve("review", tools=["Read", dropped]), [denied])
    assert built.tools == ("Read",)


def test_a_deny_with_a_specifier_does_not_drop_a_tool() -> None:
    built = _build(resolve("review", tools=["Read", "Bash"]), ["Bash(rm:*)"])
    assert built.tools == ("Read", "Bash")


def test_every_tool_denied_is_a_usage_error_naming_them() -> None:
    with pytest.raises(UsageError) as info:
        _build(resolve("review", tools=["WebFetch", "WebSearch"]), ["WebFetch", "WebSearch"])
    assert "WebFetch" in info.value.message and "WebSearch" in info.value.message


def _isolation() -> settings_builder.Isolation:
    return settings_builder.Isolation(
        home="/home/u", tmpdir="/tmp/x", git_dir=None, worktree_git_dir=None
    )


# End to end: dry run


def test_a_dry_run_shows_the_effective_tools_and_argv(
    tmp_path: Path,
    tools: Path,
    work: Path,  # noqa: F811
) -> None:
    proc, plan = dry_run(
        "review", "--tools", "Read", "Grep", cwd=work, tools=tools, tmp_path=tmp_path
    )
    assert proc.returncode == 0, plan["warnings"]
    assert plan["tools"] == ["Read", "Grep"]
    argv = plan["claude_argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep"
    assert plan["origins"]["tools"] == "flag"


def test_a_dry_run_without_the_flag_has_null_tools(
    tmp_path: Path,
    tools: Path,
    work: Path,  # noqa: F811
) -> None:
    _, plan = dry_run("review", cwd=work, tools=tools, tmp_path=tmp_path)
    assert plan["tools"] is None
    assert "tools" not in plan["origins"]
    assert "--tools" not in plan["claude_argv"]


def test_a_dry_run_drops_a_denied_tool_from_the_plan(
    tmp_path: Path,
    tools: Path,
    work: Path,  # noqa: F811
) -> None:
    _, plan = dry_run(
        "review", "--tools", "Read", "WebFetch", cwd=work, tools=tools, tmp_path=tmp_path
    )
    assert plan["tools"] == ["Read"]
    assert any("WebFetch" in w for w in plan["warnings"])


def test_a_dry_run_refuses_an_unknown_tool_with_exit_2(
    tmp_path: Path,
    tools: Path,
    work: Path,  # noqa: F811
) -> None:
    proc = run_cli(
        [
            "exec", "--dry-run", "--json", "--kind", "review", "--mode", "headless",
            "--prompt-file", "prompt.md", "--tools", "Frob",
        ],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert "Frob" in proc.stdout + proc.stderr


def test_a_dry_run_refuses_tools_on_a_trusted_kind_with_exit_2(
    tmp_path: Path,
    tools: Path,
    work: Path,  # noqa: F811
) -> None:
    proc = run_cli(
        [
            "exec", "--dry-run", "--json", "--kind", "implement", "--mode", "headless",
            "--prompt-file", "prompt.md", "--tools", "Read",
        ],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr


# End to end: stored, carried, reset


def test_tools_reach_claude_are_carried_and_reset(env: Any) -> None:  # noqa: F811
    sid = exec_session(env, "--kind", "rank", "--tools", "Read", "Grep")["csession_id"]
    assert flag_value(starts(env)[-1], "--tools") == ["Read,Grep"]
    assert spec(env, sid, 1) == {"tools": ["Read", "Grep"]}
    proc, _ = resume(env, sid)
    assert proc.returncode == 0, proc.stderr
    assert flag_value(starts(env)[-1], "--tools") == ["Read,Grep"]
    assert spec(env, sid, 2) == {"tools": ["Read", "Grep"]}
    proc, _ = resume(env, sid, "--tools", "Read")
    assert flag_value(starts(env)[-1], "--tools") == ["Read"]
    assert spec(env, sid, 3) == {"tools": ["Read"]}
    proc, _ = resume(env, sid, "--reset", "tools")
    assert proc.returncode == 0, proc.stderr
    assert flag_value(starts(env)[-1], "--tools") is None
    assert spec(env, sid, 4) == {}


def test_a_kind_change_drops_the_carried_tools_and_reset_drops_them_too() -> None:
    from .test_carryover import source

    src = source(spec={"tools": ["Read"], "model": "m"})
    assert resume_mod.carry(src, kind="implement", mode="headless", resets=()).flags == {
        "model": "m",
        "tools": ["Read"],
    }
    assert resume_mod.carry(src, kind="review", mode="headless", resets=()).flags == {}
    other = resume_mod.carry(src, kind="implement", mode="headless", resets=("tools",))
    assert other.flags == {"model": "m"}
    given = resume_mod.carry(src, kind="implement", mode="headless", resets=(), given={"tools"})
    assert "tools" not in given.flags


def test_a_dry_run_whose_tools_are_all_denied_exits_2(
    tmp_path: Path,
    tools: Path,
    work: Path,  # noqa: F811
) -> None:
    proc = run_cli(
        [
            "exec", "--dry-run", "--json", "--kind", "review", "--mode", "headless",
            "--prompt-file", "prompt.md", "--tools", "WebFetch", "WebSearch",
        ],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert "WebFetch" in proc.stdout + proc.stderr


def test_an_exec_whose_tools_are_all_denied_does_not_start_claude(env: Any) -> None:  # noqa: F811
    prompt = env.tmp_path / "prompt.md"
    prompt.write_text("go\n", encoding="utf-8")
    proc, out = run_cs(
        env,
        ["exec", "--json", "--mode", "headless", "--kind", "rank", "--prompt-file",
         str(prompt), "--tools", "WebFetch", "WebSearch"],
    )  # fmt: skip
    assert proc.returncode == 12, proc.stdout + proc.stderr
    assert out["end_reason"] == "config_invalid"
    assert starts(env) == []


def test_start_accepts_all_denied_tools_and_the_adoption_ends_config_invalid(
    start_env: Any,  # noqa: F811
) -> None:
    # start does not read the permission profile: it freezes the spec; the adoption refuses.
    out = start_env.started(
        "--mode", "headless", "--kind", "rank", "--tools", "WebFetch", "WebSearch"
    )  # fmt: skip
    proc, ended = start_env.adopt(out["csession_id"])
    assert proc.returncode == 12, proc.stdout + proc.stderr
    assert ended["end_reason"] == "config_invalid"


def test_status_with_tools_does_not_get_the_resume_order_hint() -> None:
    with pytest.raises(UsageError) as info:
        parse("status", "--tools")
    assert info.value.detail is None


def test_a_dry_run_plan_filters_tools_even_when_the_launch_would_fail(
    tmp_path: Path,
    tools: Path,
    work: Path,  # noqa: F811
) -> None:
    # respond outside git would fail (no_git_cwd): no generated argv, yet the filtered list.
    proc, plan = dry_run(
        "respond", "--tools", "Read", "WebFetch", cwd=work, tools=tools, tmp_path=tmp_path
    )
    assert plan["would_fail"] == "no_git_cwd", proc.stdout
    assert plan["tools"] == ["Read"]
