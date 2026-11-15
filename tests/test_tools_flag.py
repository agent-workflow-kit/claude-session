"""``--tools <name>...`` (interface.md, Launch flags; issue #108): a per-call allowlist of Claude
Code built-in tools for isolated kinds, passed as ``claude --tools``. It only narrows the
tools Claude Code offers; permissions (deny first) still apply on top of it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_session import cli, config, ids, kinds, resume as resume_mod, settings_builder
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
    spec,
    starts,
)

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


def argv_of(resolution: kinds.Resolution, perm: dict[str, Any] | None = None) -> Any:
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


def test_known_names_pass_the_flag_rules() -> None:
    args = parse("start", "--json", "--kind", "review", "--tools", "Read", "Grep", "Glob")
    cli.check_flag_rules(args)


def test_the_name_list_is_ascii_identifiers_only() -> None:
    assert all(ids.is_builtin_tool(n) for n in ("Read", "Grep", "Bash", "WebFetch"))
    assert not ids.is_builtin_tool("Read ")
    assert not ids.is_builtin_tool("ＲＥＡＤ")


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


def _isolation() -> settings_builder.Isolation:
    return settings_builder.Isolation(
        home="/home/u", tmpdir="/tmp/x", git_dir=None, worktree_git_dir=None
    )


# End to end: dry run


def test_a_dry_run_shows_the_effective_tools_and_argv(
    tmp_path: Path, tools: Path, work: Path  # noqa: F811
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
    tmp_path: Path, tools: Path, work: Path  # noqa: F811
) -> None:
    _, plan = dry_run("review", cwd=work, tools=tools, tmp_path=tmp_path)
    assert plan["tools"] is None
    assert "tools" not in plan["origins"]
    assert "--tools" not in plan["claude_argv"]


def test_a_dry_run_drops_a_denied_tool_from_the_plan(
    tmp_path: Path, tools: Path, work: Path  # noqa: F811
) -> None:
    _, plan = dry_run(
        "review", "--tools", "Read", "WebFetch", cwd=work, tools=tools, tmp_path=tmp_path
    )
    assert plan["tools"] == ["Read"]
    assert any("WebFetch" in w for w in plan["warnings"])


def test_a_dry_run_refuses_an_unknown_tool_with_exit_2(
    tmp_path: Path, tools: Path, work: Path  # noqa: F811
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
    tmp_path: Path, tools: Path, work: Path  # noqa: F811
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
