"""Generated settings and ``claude`` argv for trusted kinds (interface.md, Generated settings,
Bootstrap prompt and Launch flags; design.md, Generated settings and Prompt delivery)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from claude_session import config, kinds, settings_builder
from claude_session.errors import FailedError
from claude_session.sessiondir import SessionDirs
from claude_session.settings_builder import Launch, Paths, Tools

ROOT = Path(__file__).resolve().parent.parent
DRY_RUN_SCHEMA = ROOT / "schemas" / "cli" / "exec-dry-run.json"

STATE = "/home/user/.local/state/claude-session"
SID = "cs-abcd2345"
CSDIR = f"{STATE}/sessions/{SID}"
PRIVATE = f"{STATE}/private"
CS_PATH = "/opt/claude-session/bin/claude-session"
PYTHON = "/opt/claude-session/bin/python3"
CLAUDE = "/usr/local/bin/claude"
CWD = "/work/repo"
CLAUDE_ID = "0b6f4d3e-6a8f-4f5e-9d3c-2f1e0a9b8c7d"
NEW_ID = "7c1e2d3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"

PATHS = Paths(state_root=STATE, csession_id=SID, run=1)
TOOLS = Tools(interpreter=PYTHON, claude_session=CS_PATH, claude=CLAUDE)

# The flags the fake claude (tests/fakebin, #8) accepts; claude-session must pass no others.
FAKE_VALUE_FLAGS = {
    "--settings",
    "--setting-sources",
    "--model",
    "--add-dir",
    "--plugin-dir",
    "--append-system-prompt-file",
    "--system-prompt-snapshot",
    "--resume",
    "--session-id",
    "--output-format",
    "--max-turns",
}
FAKE_REPEATABLE = {"--add-dir", "--plugin-dir"}
FAKE_BOOL_FLAGS = {"-p", "--print", "--fork-session", "--strict-mcp-config", "--verbose", "--debug"}

EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "Notification",
    "PostToolUse",
    "Stop",
    "SessionEnd",
]


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> config.Config:
    home = tmp_path_factory.mktemp("xdg")
    return config.load(None, env={"XDG_CONFIG_HOME": str(home)})


def load_cfg(tmp_path: Path, text: str) -> config.Config:
    path = tmp_path / "claude-session" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path)})


def resolve(
    cfg: config.Config, kind: str = "implement", mode: kinds.Mode = "headless", **flags: Any
) -> kinds.Resolution:
    return kinds.resolve(cfg, kind, mode=mode, flags=flags or None, origin_url=lambda _: None)


def profile(**permissions: Any) -> config.PermissionProfile:
    return config.PermissionProfile(permissions=permissions or None)


def settings(
    resolution: kinds.Resolution,
    perm: config.PermissionProfile | None = None,
    *,
    cwd: str = CWD,
    paths: Paths = PATHS,
    tools: Tools = TOOLS,
) -> dict[str, Any]:
    return settings_builder.build_settings(
        resolution, perm or config.PermissionProfile(), paths, tools, cwd=cwd
    )


def launch(**kw: Any) -> Launch:
    kw.setdefault("mode", "headless")
    kw.setdefault("cwd", CWD)
    kw.setdefault("prompt_path", f"{CSDIR}/prompt.md")
    return Launch(**kw)


def argv(resolution: kinds.Resolution, **kw: Any) -> list[str]:
    return settings_builder.build_argv(resolution, PATHS, TOOLS, launch(**kw)).argv


def parse_like_fake(args: list[str]) -> tuple[dict[str, Any], str | None]:
    """Parse argv as the fake claude does: unknown options, a missing value, a repeated
    single-use flag and a second positional are errors."""
    flags: dict[str, Any] = {}
    prompt: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in FAKE_VALUE_FLAGS:
            assert i + 1 < len(args), f"{arg} without a value"
            value = args[i + 1]
            i += 1
            if arg in FAKE_REPEATABLE:
                flags.setdefault(arg, []).append(value)
            else:
                assert arg not in flags, f"{arg} given twice"
                flags[arg] = value
        elif arg in FAKE_BOOL_FLAGS:
            flags[arg] = True
        elif arg.startswith("-") and arg != "-":
            raise AssertionError(f"unknown option {arg}")
        else:
            assert prompt is None, f"second positional {arg!r}"
            prompt = arg
        i += 1
    return flags, prompt


def check_hook_layout(data: dict[str, Any]) -> None:
    """Claude Code's hook layout, as the fake claude parses it: ``hooks.<Event>`` is a list of
    ``{matcher?, hooks: [{type: command, command, timeout}]}``."""
    hooks = data["hooks"]
    assert isinstance(hooks, dict)
    for event, groups in hooks.items():
        assert isinstance(groups, list) and groups, event
        for group in groups:
            assert set(group) <= {"matcher", "hooks"}, group
            assert isinstance(group.get("matcher", ""), str)
            assert isinstance(group["hooks"], list) and group["hooks"]
            for entry in group["hooks"]:
                assert set(entry) == {"type", "command", "timeout"}
                assert entry["type"] == "command"
                assert isinstance(entry["command"], str) and entry["command"]
                timeout = entry["timeout"]
                assert isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0


# Isolation boundary


@pytest.mark.parametrize("kind", ["implement", "draft-ticket", "audit", "improve"])
def test_builtin_trusted_kinds_are_not_isolated(cfg: config.Config, kind: str) -> None:
    assert not settings_builder.is_isolated(resolve(cfg, kind).profile)


@pytest.mark.parametrize("kind", ["review", "gate-check", "respond", "rank"])
def test_isolated_kinds_are_refused_until_isolation_lands(cfg: config.Config, kind: str) -> None:
    resolution = resolve(cfg, kind)
    assert settings_builder.is_isolated(resolution.profile)
    with pytest.raises(NotImplementedError):
        settings(resolution)


def test_reads_third_party_flag_makes_a_trusted_kind_isolated(cfg: config.Config) -> None:
    resolution = resolve(cfg, "implement", reads_third_party=True)
    with pytest.raises(NotImplementedError):
        settings(resolution)


def test_sandbox_flag_makes_a_trusted_kind_isolated(cfg: config.Config) -> None:
    with pytest.raises(NotImplementedError):
        settings(resolve(cfg, "implement", sandbox="preferred"))


# Injected hooks


def test_every_injected_event_has_a_hook(cfg: config.Config) -> None:
    hooks = settings(resolve(cfg))["hooks"]
    assert list(hooks) == EVENTS
    assert "SubagentStop" not in hooks


def test_hook_layout_matches_what_claude_code_and_the_fake_accept(cfg: config.Config) -> None:
    check_hook_layout(settings(resolve(cfg)))


@pytest.mark.parametrize("event", EVENTS)
def test_hook_command_uses_isolated_interpreter_and_absolute_state_dir(
    cfg: config.Config, event: str
) -> None:
    for group in settings(resolve(cfg))["hooks"][event]:
        (entry,) = group["hooks"]
        assert shlex.split(entry["command"]) == [
            PYTHON,
            "-I",
            "-m",
            "claude_session",
            "hook-event",
            "--state-dir",
            STATE,
            event,
        ]


@pytest.mark.parametrize(
    ("event", "timeout"),
    [
        ("SessionStart", 30),
        ("UserPromptSubmit", 30),
        ("PermissionRequest", 30),
        ("Notification", 30),
        ("PostToolUse", 60),
        ("Stop", 60),
        ("SessionEnd", 30),
    ],
)
def test_hook_timeouts(cfg: config.Config, event: str, timeout: int) -> None:
    for group in settings(resolve(cfg))["hooks"][event]:
        assert [entry["timeout"] for entry in group["hooks"]] == [timeout]


def test_notification_matchers(cfg: config.Config) -> None:
    groups = settings(resolve(cfg))["hooks"]["Notification"]
    assert [group["matcher"] for group in groups] == [
        "permission_prompt",
        "idle_prompt",
        "elicitation_dialog",
    ]


@pytest.mark.parametrize("event", [e for e in EVENTS if e != "Notification"])
def test_other_events_match_everything(cfg: config.Config, event: str) -> None:
    # PostToolUse covers every tool: an approved permission prompt for any tool must clear
    # needs-input(permission). Events without a matched field must carry no matcher.
    (group,) = settings(resolve(cfg))["hooks"][event]
    assert "matcher" not in group


def test_hook_command_quotes_paths_with_spaces_and_shell_characters(cfg: config.Config) -> None:
    state = "/home/a user/st$ate;`x`'q/claude-session"
    tools = Tools(interpreter="/opt/my python/bin/py'thon", claude_session=CS_PATH, claude=CLAUDE)
    paths = Paths(state_root=state, csession_id=SID, run=1)
    hooks = settings(resolve(cfg), paths=paths, tools=tools)["hooks"]
    command = hooks["Stop"][0]["hooks"][0]["command"]
    assert shlex.split(command) == [
        "/opt/my python/bin/py'thon",
        "-I",
        "-m",
        "claude_session",
        "hook-event",
        "--state-dir",
        state,
        "Stop",
    ]


def test_hook_command_runs_under_sh(cfg: config.Config, tmp_path: Path) -> None:
    # Run exactly as Claude Code runs a hook: /bin/sh -c <command>, payload on stdin. The
    # handler is a stub until #13, so it must exit 0 with no output.
    tools = Tools(
        interpreter=settings_builder.current_interpreter(), claude_session=CS_PATH, claude=CLAUDE
    )
    paths = Paths(state_root=str(tmp_path), csession_id=SID, run=1)
    command = settings(resolve(cfg), paths=paths, tools=tools)["hooks"]["Stop"][0]["hooks"][0]
    result = subprocess.run(
        ["/bin/sh", "-c", command["command"]],
        input=b"{}",
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_hook_command_ignores_a_planted_package_in_the_cwd(
    cfg: config.Config, tmp_path: Path
) -> None:
    # -I: the agent's cwd is not on the import path and PYTHON* variables are ignored.
    marker = tmp_path / "planted-ran"
    planted = tmp_path / "claude_session"
    planted.mkdir()
    code = f"open({str(marker)!r}, 'w').close()\n"
    (planted / "__init__.py").write_text(code, encoding="utf-8")
    (planted / "__main__.py").write_text(code, encoding="utf-8")
    tools = Tools(
        interpreter=settings_builder.current_interpreter(), claude_session=CS_PATH, claude=CLAUDE
    )
    paths = Paths(state_root=str(tmp_path / "state"), csession_id=SID, run=1)
    command = settings(resolve(cfg), paths=paths, tools=tools)["hooks"]["SessionStart"][0]
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHONSTARTUP": str(planted / "__init__.py")}
    result = subprocess.run(
        ["/bin/sh", "-c", command["hooks"][0]["command"]],
        input=b"{}",
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert not marker.exists()


def test_current_interpreter_keeps_the_venv_link(tmp_path: Path) -> None:
    real = tmp_path / "base" / "bin"
    real.mkdir(parents=True)
    (real / "python3.12").write_text("", encoding="utf-8")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").symlink_to(real / "python3.12")
    alias = tmp_path / "alias"
    alias.symlink_to(venv)
    got = settings_builder.current_interpreter(str(alias / "bin" / "python3"))
    assert got == str(venv.resolve() / "bin" / "python3")


def test_current_interpreter_defaults_to_this_python() -> None:
    got = settings_builder.current_interpreter()
    assert os.path.isabs(got)
    assert os.path.samefile(got, sys.executable)


def test_current_interpreter_refuses_an_unknown_interpreter() -> None:
    with pytest.raises(FailedError):
        settings_builder.current_interpreter("")


@pytest.mark.parametrize(
    "interpreter", ["python3", "bin/python3", "", "/opt/py\nthon", "/opt/py\x00", "/opt/../py"]
)
def test_interpreter_must_be_absolute_clean_path(cfg: config.Config, interpreter: str) -> None:
    tools = Tools(interpreter=interpreter, claude_session=CS_PATH, claude=CLAUDE)
    with pytest.raises(FailedError):
        settings(resolve(cfg), tools=tools)


# Report allow rules and file rules


def test_report_allow_rules_bare_and_absolute(cfg: config.Config) -> None:
    allow = settings(resolve(cfg))["permissions"]["allow"]
    assert "Bash(claude-session report:*)" in allow
    assert f"Bash({CS_PATH} report:*)" in allow


def test_read_allow_for_the_session_directory(cfg: config.Config) -> None:
    assert f"Read(/{CSDIR}/**)" in settings(resolve(cfg))["permissions"]["allow"]


@pytest.mark.parametrize("sub", ["artifacts", "outbox"])
@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_edit_and_write_allows_for_artifacts_and_outbox(
    cfg: config.Config, sub: str, tool: str
) -> None:
    # Live A8: a Write allow alone does not permit the Write tool; Edit does. Both are given.
    assert f"{tool}(/{CSDIR}/{sub}/**)" in settings(resolve(cfg))["permissions"]["allow"]


def test_rule_paths_use_the_double_slash_absolute_form(cfg: config.Config) -> None:
    perms = settings(resolve(cfg))["permissions"]
    for rule in perms["allow"] + perms["deny"]:
        tool, _, rest = rule.partition("(")
        if tool in ("Read", "Edit", "Write"):
            assert rest.startswith("//") and not rest.startswith("///"), rule


def test_work_dir_rules_only_when_it_is_the_cwd(cfg: config.Config) -> None:
    resolution = resolve(cfg)
    work = f"{CSDIR}/work"
    caller = settings(resolution)["permissions"]["allow"]
    assert not any("/work/" in rule for rule in caller)
    own = settings(resolution, cwd=work)["permissions"]["allow"]
    assert f"Edit(/{work}/**)" in own
    assert f"Write(/{work}/**)" in own


def test_other_sessions_work_dir_is_not_allowed(cfg: config.Config) -> None:
    other = f"{STATE}/sessions/cs-wxyz7777/work"
    allow = settings(resolve(cfg), cwd=other)["permissions"]["allow"]
    assert not any("cs-wxyz7777" in rule for rule in allow)


PROTECTED = [
    "secret",
    "prompt.md",
    "prompts/**",
    "instructions.md",
    "run.json",
    "inputs/**",
    "gh-empty/**",
    "empty-gitconfig",
]


@pytest.mark.parametrize("name", PROTECTED)
@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_protected_session_files_are_denied(cfg: config.Config, name: str, tool: str) -> None:
    assert f"{tool}(/{CSDIR}/{name})" in settings(resolve(cfg))["permissions"]["deny"]


@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_private_directory_is_denied(cfg: config.Config, tool: str) -> None:
    assert f"{tool}(/{PRIVATE}/**)" in settings(resolve(cfg))["permissions"]["deny"]


def test_no_allow_reaches_a_protected_file(cfg: config.Config) -> None:
    allow = settings(resolve(cfg), cwd=f"{CSDIR}/work")["permissions"]["allow"]
    for rule in allow:
        if rule.startswith(("Edit(", "Write(")):
            target = rule[rule.index("(") + 2 : -1]
            assert target.startswith((f"{CSDIR}/artifacts/", f"{CSDIR}/outbox/", f"{CSDIR}/work/"))


@pytest.mark.parametrize(
    "state",
    [
        "/state/with*glob",
        "/state/with?mark",
        "/state/[x]",
        "/state/{a,b}",
        "/state/(paren)",
        "/state/back\\slash",
        "/state/new\nline",
        "/state/tab\there",
        "/state/del\x7f",
        "/state/c1\x85",
        "/state/surrogate\udcff",
        "relative/state",
        "/state/../escape",
        "/state//double",
        "/state/trailing/",
    ],
)
def test_unsafe_state_root_is_refused(cfg: config.Config, state: str) -> None:
    paths = Paths(state_root=state, csession_id=SID, run=1)
    with pytest.raises(FailedError):
        settings(resolve(cfg), paths=paths)


@pytest.mark.parametrize(
    "path", ["claude-session", "/opt/*/claude-session", "/opt/cs\n", "/opt/(x)/claude-session"]
)
def test_unsafe_claude_session_path_is_refused(cfg: config.Config, path: str) -> None:
    tools = Tools(interpreter=PYTHON, claude_session=path, claude=CLAUDE)
    with pytest.raises(FailedError):
        settings(resolve(cfg), tools=tools)


def test_paths_reject_bad_session_id_and_run() -> None:
    with pytest.raises(ValueError):
        Paths(state_root=STATE, csession_id="cs-../../x", run=1)
    for run in (0, -1, True):
        with pytest.raises(ValueError):
            Paths(state_root=STATE, csession_id=SID, run=run)


def test_paths_layout() -> None:
    paths = Paths(state_root=STATE, csession_id=SID, run=3)
    assert paths.session_dir == CSDIR
    assert paths.private_root == PRIVATE
    assert paths.settings_file == f"{PRIVATE}/{SID}/runs/3/settings.json"
    assert paths.instructions_file == f"{CSDIR}/instructions.md"
    assert paths.work_dir == f"{CSDIR}/work"


# Permission profile and defaultMode


def test_default_mode_is_absent_when_user_settings_apply(cfg: config.Config) -> None:
    resolution = resolve(cfg, setting_sources="user,project")
    assert "defaultMode" not in settings(resolution)["permissions"]


@pytest.mark.parametrize("sources", ["", "project", "project,local", "local"])
def test_default_mode_is_default_without_user_settings(cfg: config.Config, sources: str) -> None:
    resolution = resolve(cfg, setting_sources=sources)
    assert settings(resolution)["permissions"]["defaultMode"] == "default"


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions", "plan", "dontAsk"])
@pytest.mark.parametrize("sources", ["", "user"])
def test_profile_mode_wins_for_trusted_runs(cfg: config.Config, mode: str, sources: str) -> None:
    resolution = resolve(cfg, setting_sources=sources)
    perms = settings(resolution, profile(defaultMode=mode))["permissions"]
    assert perms["defaultMode"] == mode


@pytest.mark.parametrize("mode", ["", 3, None, ["default"]])
def test_profile_mode_must_be_a_non_empty_string(cfg: config.Config, mode: Any) -> None:
    with pytest.raises(FailedError):
        settings(resolve(cfg), profile(defaultMode=mode))


def test_profile_rules_are_kept_and_generated_rules_added(cfg: config.Config) -> None:
    perm = profile(
        allow=["Bash(make:*)", "Bash(claude-session report:*)"],
        deny=["Bash(rm:*)"],
        ask=["Bash(git push:*)"],
        additionalDirectories=["/data"],
    )
    perms = settings(resolve(cfg), perm)["permissions"]
    assert perms["allow"][0] == "Bash(make:*)"
    assert perms["allow"].count("Bash(claude-session report:*)") == 1
    assert perms["deny"][0] == "Bash(rm:*)"
    assert perms["ask"] == ["Bash(git push:*)"]
    assert perms["additionalDirectories"] == ["/data"]


@pytest.mark.parametrize("key", ["allow", "deny", "ask"])
@pytest.mark.parametrize("value", ["Bash(x)", [1], [None], {"a": 1}])
def test_profile_rule_lists_must_be_string_lists(cfg: config.Config, key: str, value: Any) -> None:
    with pytest.raises(FailedError):
        settings(resolve(cfg), profile(**{key: value}))


def test_profile_input_is_not_mutated(cfg: config.Config) -> None:
    allow = ["Bash(make:*)"]
    perm = profile(allow=allow)
    settings(resolve(cfg), perm)
    settings(resolve(cfg), perm)
    assert allow == ["Bash(make:*)"]
    assert perm.permissions == {"allow": ["Bash(make:*)"]}


def test_profile_sandbox_fragment_is_not_emitted_for_trusted_runs(cfg: config.Config) -> None:
    perm = config.PermissionProfile(sandbox={"enabled": True})
    assert "sandbox" not in settings(resolve(cfg), perm)


def test_settings_have_only_permissions_and_hooks(cfg: config.Config) -> None:
    assert set(settings(resolve(cfg))) == {"permissions", "hooks"}


def test_settings_are_deterministic(cfg: config.Config) -> None:
    first = settings_builder.settings_json(settings(resolve(cfg)))
    second = settings_builder.settings_json(settings(resolve(cfg)))
    assert first == second
    assert json.loads(first) == settings(resolve(cfg))


def test_settings_json_keeps_non_ascii(cfg: config.Config) -> None:
    paths = Paths(state_root="/home/zoë/state", csession_id=SID, run=1)
    data = settings_builder.settings_json(settings(resolve(cfg), paths=paths))
    assert "zoë".encode() in data
    assert data.endswith(b"\n")


def test_settings_match_the_dry_run_schema(cfg: config.Config) -> None:
    schema = json.loads(DRY_RUN_SCHEMA.read_text(encoding="utf-8"))
    generated = settings_builder.build(
        resolve(cfg), config.PermissionProfile(), PATHS, TOOLS, launch()
    )
    Draft202012Validator(schema["properties"]["settings"]).validate(generated.settings)
    Draft202012Validator(schema["properties"]["claude_argv"]).validate(generated.argv)


# argv


def test_headless_argv(cfg: config.Config) -> None:
    resolution = resolve(cfg, setting_sources="user,project,local")
    args = argv(resolution, session_id=NEW_ID)
    assert args == [
        CLAUDE,
        "--settings",
        f"{PRIVATE}/{SID}/runs/1/settings.json",
        "--setting-sources",
        "user,project,local",
        "--add-dir",
        CSDIR,
        "--append-system-prompt-file",
        f"{CSDIR}/instructions.md",
        "--system-prompt-snapshot",
        "off",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--session-id",
        NEW_ID,
        f"Your claude-session directory is {CSDIR}. Read and follow {CSDIR}/prompt.md",
    ]


def test_interactive_argv_has_no_print_flags(cfg: config.Config) -> None:
    args = argv(resolve(cfg, mode="interactive"), mode="interactive")
    flags, prompt = parse_like_fake(args[1:])
    for flag in ("-p", "--output-format", "--verbose", "--max-turns"):
        assert flag not in flags
    assert prompt is not None


@pytest.mark.parametrize("mode", ["interactive", "headless"])
def test_system_prompt_snapshot_off_every_run(cfg: config.Config, mode: kinds.Mode) -> None:
    for kw in ({}, {"resume_from": CLAUDE_ID, "resume": True}):
        flags, _ = parse_like_fake(argv(resolve(cfg, mode=mode), mode=mode, **kw)[1:])
        assert flags["--system-prompt-snapshot"] == "off"


def test_empty_setting_sources_is_an_empty_value(cfg: config.Config) -> None:
    flags, _ = parse_like_fake(argv(resolve(cfg, setting_sources=""))[1:])
    assert flags["--setting-sources"] == ""


def test_model_and_strict_mcp(cfg: config.Config, tmp_path: Path) -> None:
    cfg2 = load_cfg(tmp_path, "kinds:\n  implement:\n    mcp: none\n    model: opus\n")
    flags, _ = parse_like_fake(argv(resolve(cfg2))[1:])
    assert flags["--model"] == "opus"
    assert flags["--strict-mcp-config"] is True
    plain, _ = parse_like_fake(argv(resolve(cfg))[1:])
    assert "--model" not in plain and "--strict-mcp-config" not in plain


def test_model_from_flag(cfg: config.Config) -> None:
    flags, _ = parse_like_fake(argv(resolve(cfg, model="sonnet"))[1:])
    assert flags["--model"] == "sonnet"


def test_max_turns_headless_is_n_plus_one(cfg: config.Config) -> None:
    flags, _ = parse_like_fake(argv(resolve(cfg, max_turns=5))[1:])
    assert flags["--max-turns"] == "6"


def test_max_turns_upper_bound(cfg: config.Config) -> None:
    flags, _ = parse_like_fake(argv(resolve(cfg, max_turns=9007199254740991))[1:])
    assert flags["--max-turns"] == "9007199254740992"


def test_interactive_max_turns_flag_is_ignored_with_a_warning(cfg: config.Config) -> None:
    resolution = resolve(cfg, mode="interactive", max_turns=5)
    built = settings_builder.build_argv(resolution, PATHS, TOOLS, launch(mode="interactive"))
    assert "--max-turns" not in built.argv
    assert len(built.warnings) == 1
    assert "--max-turns" in built.warnings[0]


def test_interactive_max_turns_from_the_kind_is_ignored_silently(tmp_path: Path) -> None:
    cfg = load_cfg(tmp_path, "kinds:\n  implement:\n    max_turns: 7\n")
    resolution = resolve(cfg, mode="interactive")
    built = settings_builder.build_argv(resolution, PATHS, TOOLS, launch(mode="interactive"))
    assert "--max-turns" not in built.argv
    assert built.warnings == ()


def test_headless_max_turns_from_the_kind(tmp_path: Path) -> None:
    cfg = load_cfg(tmp_path, "kinds:\n  implement:\n    max_turns: 7\n")
    flags, _ = parse_like_fake(argv(resolve(cfg))[1:])
    assert flags["--max-turns"] == "8"


def test_plugin_dirs_trusted_runs_get_no_extra_add_dir(cfg: config.Config) -> None:
    args = argv(resolve(cfg), plugin_dirs=("/plugins/a", "/plugins/b"))
    flags, _ = parse_like_fake(args[1:])
    assert flags["--plugin-dir"] == ["/plugins/a", "/plugins/b"]
    assert flags["--add-dir"] == [CSDIR]


def test_claude_args_come_before_resume_and_prompt(tmp_path: Path) -> None:
    cfg = load_cfg(tmp_path, "kinds:\n  implement:\n    claude_args: ['--debug']\n")
    args = argv(resolve(cfg), resume_from=CLAUDE_ID, resume=True, prompt_path=None)
    assert args[-4:] == [
        "--debug",
        "--resume",
        CLAUDE_ID,
        f"Your claude-session directory is {CSDIR}. Continue the task. "
        "When you are finished, file your status report.",
    ]


def test_resume_argv(cfg: config.Config) -> None:
    flags, _ = parse_like_fake(argv(resolve(cfg), resume_from=CLAUDE_ID, resume=True)[1:])
    assert flags["--resume"] == CLAUDE_ID
    assert "--session-id" not in flags and "--fork-session" not in flags


def test_fork_argv(cfg: config.Config) -> None:
    args = argv(resolve(cfg), resume_from=CLAUDE_ID, fork=True, session_id=NEW_ID)
    flags, _ = parse_like_fake(args[1:])
    assert (flags["--resume"], flags["--fork-session"], flags["--session-id"]) == (
        CLAUDE_ID,
        True,
        NEW_ID,
    )
    i = args.index("--resume")
    assert args[i : i + 5] == ["--resume", CLAUDE_ID, "--fork-session", "--session-id", NEW_ID]


@pytest.mark.parametrize(
    "kw",
    [
        {"fork": True},  # fork without a conversation
        {"fork": True, "resume_from": CLAUDE_ID},  # fork without a new id
        {"resume_from": CLAUDE_ID, "session_id": NEW_ID},  # --session-id with plain --resume
        {"resume_from": "not-a-uuid"},
        {"session_id": "not-a-uuid"},
        {"session_id": NEW_ID.upper()},
        {"resume_from": "--settings"},
        {"session_id": "{" + NEW_ID + "}"},
    ],
)
def test_invalid_conversation_flags(cfg: config.Config, kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        argv(resolve(cfg), **kw)


def test_argv_is_accepted_by_the_fake_for_every_shape(cfg: config.Config) -> None:
    shapes: list[dict[str, Any]] = [
        {},
        {"session_id": NEW_ID},
        {"resume_from": CLAUDE_ID, "resume": True},
        {"resume_from": CLAUDE_ID, "fork": True, "session_id": NEW_ID},
        {"skill": "review-pr", "prompt_path": None},
        {"plugin_dirs": ("/p",)},
    ]
    for mode in ("headless", "interactive"):
        for shape in shapes:
            args = argv(resolve(cfg, mode=mode, model="m"), mode=mode, **shape)
            assert args[0] == CLAUDE
            flags, prompt = parse_like_fake(args[1:])
            assert flags["--settings"] == PATHS.settings_file
            if prompt is not None:
                assert not prompt.startswith("-")


def test_claude_must_be_absolute(cfg: config.Config) -> None:
    tools = Tools(interpreter=PYTHON, claude_session=CS_PATH, claude="claude")
    with pytest.raises(FailedError):
        settings_builder.build_argv(resolve(cfg), PATHS, tools, launch())


def test_build_returns_settings_argv_and_warnings(cfg: config.Config) -> None:
    resolution = resolve(cfg, mode="interactive", max_turns=3)
    built = settings_builder.build(
        resolution, config.PermissionProfile(), PATHS, TOOLS, launch(mode="interactive")
    )
    assert set(built.settings) == {"permissions", "hooks"}
    assert built.argv[0] == CLAUDE
    assert len(built.warnings) == 1


# Bootstrap prompt


def boot(**kw: Any) -> str | None:
    kw.setdefault("mode", "headless")
    kw.setdefault("resume", False)
    kw.setdefault("skill", None)
    kw.setdefault("prompt_path", None)
    return settings_builder.bootstrap_prompt(CSDIR, **kw)


PROMPT = f"{CSDIR}/prompts/2.md"


@pytest.mark.parametrize("mode", ["headless", "interactive"])
@pytest.mark.parametrize("resume", [False, True])
def test_bootstrap_skill_and_prompt(mode: kinds.Mode, resume: bool) -> None:
    text = boot(skill="review-pr", prompt_path=PROMPT, mode=mode, resume=resume)
    assert text == (
        f"/review-pr Your claude-session directory is {CSDIR}. Read and follow {PROMPT}"
    )


@pytest.mark.parametrize("mode", ["headless", "interactive"])
@pytest.mark.parametrize("resume", [False, True])
def test_bootstrap_prompt_only(mode: kinds.Mode, resume: bool) -> None:
    text = boot(prompt_path=PROMPT, mode=mode, resume=resume)
    assert text == f"Your claude-session directory is {CSDIR}. Read and follow {PROMPT}"


@pytest.mark.parametrize("mode", ["headless", "interactive"])
@pytest.mark.parametrize("resume", [False, True])
def test_bootstrap_skill_only(mode: kinds.Mode, resume: bool) -> None:
    text = boot(skill="plugin:skill.v2", mode=mode, resume=resume)
    assert text == f"/plugin:skill.v2 Your claude-session directory is {CSDIR}."


def test_bootstrap_headless_resume_without_prompt() -> None:
    assert boot(resume=True) == (
        f"Your claude-session directory is {CSDIR}. Continue the task. "
        "When you are finished, file your status report."
    )


@pytest.mark.parametrize("resume", [False, True])
def test_bootstrap_interactive_without_prompt_sends_nothing(resume: bool) -> None:
    assert boot(mode="interactive", resume=resume) is None


def test_bootstrap_headless_new_session_needs_skill_or_prompt() -> None:
    with pytest.raises(ValueError):
        boot()


def test_interactive_argv_without_message_has_no_positional(cfg: config.Config) -> None:
    args = argv(resolve(cfg, mode="interactive"), mode="interactive", prompt_path=None)
    _, prompt = parse_like_fake(args[1:])
    assert prompt is None
    assert args[-2:] == ["--system-prompt-snapshot", "off"]


@pytest.mark.parametrize(
    "skill", ["", "-p", "/x", "a b", "a\nb", "ä", "x" * 129, "--settings", "a/b"]
)
def test_bootstrap_rejects_invalid_skill(skill: str) -> None:
    with pytest.raises(ValueError):
        boot(skill=skill)


@pytest.mark.parametrize(
    "path",
    [
        "prompt.md",
        f"{CSDIR}/../other/prompt.md",
        f"{CSDIR}/prompt\n.md",
        f"{STATE}/sessions/cs-wxyz7777/prompt.md",
        "/etc/passwd",
        CSDIR,
    ],
)
def test_bootstrap_prompt_path_must_be_inside_the_session_dir(path: str) -> None:
    with pytest.raises(ValueError):
        boot(prompt_path=path)


def test_bootstrap_prompt_is_the_last_argument(cfg: config.Config) -> None:
    args = argv(resolve(cfg), skill="s", prompt_path=f"{CSDIR}/prompt.md")
    assert args[-1].startswith("/s Your claude-session directory is ")


# Writing the settings file


def test_write_settings_into_the_private_run_dir(cfg: config.Config, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    paths = Paths(state_root=str(state), csession_id=SID, run=2)
    data = settings(resolve(cfg), paths=paths)
    with SessionDirs.open(str(state), SID) as dirs:
        dirs.prepare_run(2)
        written = dirs.write_settings(2, settings_builder.settings_json(data))
    assert written == paths.settings_file
    target = Path(written)
    assert json.loads(target.read_text(encoding="utf-8")) == data
    assert target.stat().st_mode & 0o777 == 0o600
    # No temporary name is left behind anywhere.
    leftovers = [p.name for p in state.rglob(".*.tmp")]
    assert leftovers == []


def test_write_settings_replaces_a_planted_symlink(cfg: config.Config, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    with SessionDirs.open(str(state), SID) as dirs:
        dirs.prepare_run(1)
        link = state / "private" / SID / "runs" / "1" / "settings.json"
        link.symlink_to(victim)
        dirs.write_settings(1, b"{}\n")
    assert victim.read_text(encoding="utf-8") == "keep"
    assert not link.is_symlink()


def test_write_settings_requires_prepare_run(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with SessionDirs.open(str(state), SID) as dirs, pytest.raises(ValueError):
        dirs.write_settings(1, b"{}\n")


def test_uuid_constants_are_canonical() -> None:
    assert str(uuid.UUID(CLAUDE_ID)) == CLAUDE_ID
    assert str(uuid.UUID(NEW_ID)) == NEW_ID
