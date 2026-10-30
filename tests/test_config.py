"""Config loading (interface.md, Config and Environment variables)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from claude_session import config, defaults
from claude_session.errors import UsageError
from tests import schemas

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"
CONFIG_SCHEMA = "claude-session/config.json"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    return tmp_path / "claude-session"


def load(cfg_dir: Path, env: dict[str, str] | None = None) -> config.Config:
    return config.load(cfg_dir / "config.yaml", env=env or {}, required=False)


def invalid(cfg_dir: Path, env: dict[str, str] | None = None) -> UsageError:
    with pytest.raises(UsageError) as info:
        load(cfg_dir, env)
    return info.value


# Defaults


def test_builtin_defaults_equal_the_example_file() -> None:
    assert defaults.BUILTIN == yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_loading_nothing_gives_the_example_values(cfg_dir: Path) -> None:
    from_example = config.load(EXAMPLE, env={})
    assert load(cfg_dir) == from_example
    assert from_example == config.Config.model_validate(defaults.BUILTIN)


def test_default_values_are_parsed(cfg_dir: Path) -> None:
    cfg = load(cfg_dir)
    assert cfg.claude_bin == "claude"
    assert cfg.default_kind == "implement"
    assert cfg.lost_grace == 600.0
    assert cfg.poll_interval == 0.5
    assert cfg.kinds["implement"].timeout == 3600.0
    assert cfg.kinds["implement"].interactive_timeout is None
    assert cfg.kinds["review"].sandbox == "required"
    assert set(cfg.permission_profiles) == {"default", "readonly", "review", "respond"}
    assert cfg.me.name is None


def test_example_validates_against_schema() -> None:
    schemas.validate(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")), CONFIG_SCHEMA)


def test_default_path_respects_xdg_config_home(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path), "HOME": "/home/someone"}
    assert config.default_path(env) == tmp_path / "claude-session" / "config.yaml"
    home = {"HOME": "/home/someone"}
    assert config.default_path(home) == Path("/home/someone/.config/claude-session/config.yaml")
    relative = {"XDG_CONFIG_HOME": "rel", "HOME": "/home/someone"}
    assert config.default_path(relative) == home_path("/home/someone")


def home_path(home: str) -> Path:
    return Path(home) / ".config" / "claude-session" / "config.yaml"


def test_default_path_is_used_without_a_path(tmp_path: Path) -> None:
    write(tmp_path / "claude-session" / "config.yaml", "claude_bin: /opt/x/claude\n")
    cfg = config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path)})
    assert cfg.claude_bin == "/opt/x/claude"


def test_missing_default_file_gives_defaults_and_still_reads_dropins(cfg_dir: Path) -> None:
    write(cfg_dir / "config.d" / "a.yaml", "claude_bin: dropped-in\n")
    assert load(cfg_dir).claude_bin == "dropped-in"


def test_missing_explicit_config_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as info:
        config.load(tmp_path / "nope.yaml", env={})
    assert "nope.yaml" in str(info.value.detail)


def test_empty_file_is_an_empty_config(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "# nothing\n")
    assert load(cfg_dir) == config.Config.model_validate(defaults.BUILTIN)


# Drop-in merge


def test_maps_are_deep_merged(cfg_dir: Path) -> None:
    write(
        cfg_dir / "config.yaml",
        """
        kinds:
          implement:
            timeout: 30m
            env: {set: {A: "1"}}
        """,
    )
    write(cfg_dir / "config.d" / "10-more.yaml", "kinds: {implement: {env: {set: {B: '2'}}}}\n")
    implement = load(cfg_dir).kinds["implement"]
    assert implement.timeout == 1800.0
    assert implement.env.set == {"A": "1", "B": "2"}
    assert implement.env.inherit is True  # untouched built-in field
    assert implement.sandbox == "off"


def test_lists_are_replaced(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "secret_env: [A, B]\nkinds: {respond: {read_dirs: [/x]}}\n")
    write(
        cfg_dir / "config.d" / "10.yaml",
        "secret_env: [C]\nkinds: {respond: {read_dirs: [/y]}}\nnotes_context_sources: [resume]\n",
    )
    cfg = load(cfg_dir)
    assert cfg.secret_env == ["C"]
    assert cfg.kinds["respond"].read_dirs == ["/y"]
    assert cfg.notes_context_sources == ["resume"]


def test_hook_lists_are_appended_in_lexical_order(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "hooks: {pre-launch: [{cmd: base}]}\n")
    write(cfg_dir / "config.d" / "20-b.yaml", "hooks: {pre-launch: [{cmd: b}]}\n")
    write(
        cfg_dir / "config.d" / "10-a.yaml",
        "hooks: {pre-launch: [{cmd: a}], post-state-change: [{cmd: n, async: true}]}\n",
    )
    write(cfg_dir / "config.d" / "30-c.yml", "hooks: {pre-launch: [{cmd: ignored}]}\n")
    write(cfg_dir / "config.d" / "40-d.yaml.bak", "hooks: {pre-launch: [{cmd: ignored}]}\n")
    write(cfg_dir / "config.d" / ".50-e.yaml", "hooks: {pre-launch: [{cmd: ignored}]}\n")
    hooks = load(cfg_dir).hooks
    assert [h.cmd for h in hooks.pre_launch] == ["base", "a", "b"]
    assert [h.cmd for h in hooks.post_state_change] == ["n"]
    assert hooks.post_state_change[0].async_ is True
    assert hooks.pre_launch[0].timeout == 30.0  # default 30s


def test_later_dropin_wins_for_scalars(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "kill_grace: 1s\n")
    write(cfg_dir / "config.d" / "b.yaml", "kill_grace: 3s\n")
    write(cfg_dir / "config.d" / "a.yaml", "kill_grace: 2s\n")
    assert load(cfg_dir).kill_grace == 3.0


def test_null_overrides_a_bound(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "kinds: {review: {timeout: null}, rank: {timeout: none}}\n")
    cfg = load(cfg_dir)
    assert cfg.kinds["review"].timeout is None
    assert cfg.kinds["rank"].timeout is None


def test_new_kind_starts_from_builtin_review(cfg_dir: Path) -> None:
    write(
        cfg_dir / "config.yaml",
        """
        kinds:
          review: {timeout: 5m, notes_context: true}
          triage: {timeout: 20m}
        """,
    )
    cfg = load(cfg_dir)
    builtin_review = config.Config.model_validate(defaults.BUILTIN).kinds["review"]
    triage = cfg.kinds["triage"]
    assert triage.timeout == 1200.0
    assert triage.notes_context is False  # built-in review, not the configured override
    assert triage.model_dump(exclude={"timeout"}) == builtin_review.model_dump(exclude={"timeout"})


def test_new_permission_profile_and_repo_override(cfg_dir: Path) -> None:
    write(
        cfg_dir / "config.yaml",
        """
        permission_profiles:
          mine: {permissions: {allow: [Read]}}
        repos:
          example.com:8443/group/project:
            kinds: {respond: {read_dirs: ["~/.nvm"], timeout: 2h}}
        me: {name: someone, github: someone-else}
        """,
    )
    cfg = load(cfg_dir)
    assert cfg.permission_profiles["mine"].permissions == {"allow": ["Read"]}
    override = cfg.repos["example.com:8443/group/project"].kinds["respond"]
    assert override.read_dirs == ["~/.nvm"]
    assert override.timeout == 7200.0
    assert cfg.me.name == "someone"


# Environment overrides

ENV_CASES: list[tuple[str, str, str, Any]] = [
    ("CSESSION_CLAUDE_BIN", "/usr/local/bin/claude", "claude_bin", "/usr/local/bin/claude"),
    ("CSESSION_DEFAULT_KIND", "review", "default_kind", "review"),
    ("CSESSION_MIN_CLAUDE_VERSION", "3.0.1", "min_claude_version", "3.0.1"),
    ("CSESSION_SUMMARY_MAX_BYTES", "100", "summary_max_bytes", 100),
    ("CSESSION_ARTIFACT_MAX_BYTES", "200", "artifact_max_bytes", 200),
    ("CSESSION_INPUT_MAX_BYTES", "300", "input_max_bytes", 300),
    ("CSESSION_NOTES_CONTEXT_TIMEOUT", "7", "notes_context_timeout", 7.0),
    ("CSESSION_NOTES_CONTEXT_MAX_BYTES", "0", "notes_context_max_bytes", 0),
    ("CSESSION_RETENTION_DAYS", "7", "retention_days", 7),
    ("CSESSION_LOST_GRACE", "1h", "lost_grace", 3600.0),
    ("CSESSION_AUTO_EXIT_DELAY", "0", "auto_exit_delay", 0.0),
    ("CSESSION_KILL_GRACE", "15", "kill_grace", 15.0),
    ("CSESSION_POLL_INTERVAL", "250ms", "poll_interval", 0.25),
    ("CSESSION_SESSION_START_TIMEOUT", "0.5", "session_start_timeout", 0.5),
]


@pytest.mark.parametrize(("var", "value", "key", "expected"), ENV_CASES)
def test_env_overrides_documented_scalars(
    cfg_dir: Path, var: str, value: str, key: str, expected: Any
) -> None:
    write(cfg_dir / "config.yaml", f"{key}: {yaml.safe_dump(defaults.BUILTIN[key]).strip()}\n")
    assert getattr(load(cfg_dir, {var: value}), key) == expected


def test_env_override_list_matches_the_documented_keys() -> None:
    assert set(config.ENV_OVERRIDES) == {var for var, *_ in ENV_CASES}


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("CSESSION_KINDS", "{implement: {timeout: 1s}}"),
        ("CSESSION_KINDS__IMPLEMENT__TIMEOUT", "1s"),
        ("CSESSION_PERMISSION_PROFILES", "{}"),
        ("CSESSION_REPOS", "{}"),
        ("CSESSION_HOOKS", "{pre-launch: [{cmd: x}]}"),
        ("CSESSION_HOOKS__PRE_LAUNCH", "x"),
        ("CSESSION_SECRET_ENV", "[]"),
        ("CSESSION_CLAUDE_AUTH_ENV", "[X]"),
        ("CSESSION_CLAUDE_ARGS", "--x"),
        ("CSESSION_NOTES_CONTEXT_CMD", "evil"),
        ("CSESSION_NOTES_CONTEXT_SOURCES", "resume"),
        ("CSESSION_ME", "x"),
        ("CSESSION_ME__NAME", "x"),
        ("CSESSION_UNKNOWN", "x"),
        ("CSESSION_ID", "cs-aaaaaaaa"),
        ("CSESSION_DIR", "/tmp/x"),
        ("CSESSION_KIND", "review"),
        ("CSESSION_HOOK", "pre-launch"),
        ("CSESSION_OBJECT_TYPE", "session"),
        ("claude_bin", "x"),
        ("CLAUDE_SESSION_CLAUDE_BIN", "x"),
    ],
)
def test_other_env_vars_are_ignored(cfg_dir: Path, var: str, value: str) -> None:
    assert load(cfg_dir, {var: value}) == load(cfg_dir)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("CSESSION_RETENTION_DAYS", "seven"),
        ("CSESSION_RETENTION_DAYS", "-1"),
        ("CSESSION_SUMMARY_MAX_BYTES", "0"),
        ("CSESSION_SUMMARY_MAX_BYTES", "1.5"),
        ("CSESSION_KILL_GRACE", "-1"),
        ("CSESSION_KILL_GRACE", "10 s"),
        ("CSESSION_LOST_GRACE", "0"),
        ("CSESSION_POLL_INTERVAL", "0s"),
        ("CSESSION_SESSION_START_TIMEOUT", "none"),
        ("CSESSION_MIN_CLAUDE_VERSION", "latest"),
    ],
)
def test_invalid_env_override_names_the_variable(cfg_dir: Path, var: str, value: str) -> None:
    error = invalid(cfg_dir, {var: value})
    assert var in str(error.detail)


# Precedence


def test_scalar_precedence_flags_env_config_builtin(cfg_dir: Path) -> None:
    assert load(cfg_dir).kill_grace == 10.0  # built-in
    write(cfg_dir / "config.yaml", "kill_grace: 20s\n")
    assert load(cfg_dir).kill_grace == 20.0  # config > built-in
    write(cfg_dir / "config.d" / "x.yaml", "kill_grace: 25s\n")
    assert load(cfg_dir).kill_grace == 25.0  # drop-ins are part of the config layer
    env = {"CSESSION_KILL_GRACE": "30"}
    cfg = load(cfg_dir, env)
    assert cfg.kill_grace == 30.0  # environment > config
    assert cfg.with_flags(kill_grace="40s").kill_grace == 40.0  # flags > environment
    assert cfg.with_flags(kill_grace=None).kill_grace == 30.0  # flag not given


def test_flags_are_validated(cfg_dir: Path) -> None:
    with pytest.raises(UsageError) as info:
        load(cfg_dir).with_flags(poll_interval="0s")
    assert "poll_interval" in str(info.value.detail)


def test_kind_precedence_flags_repo_config_builtin(cfg_dir: Path) -> None:
    repo = "example.com/group/project"
    assert config.kind_settings(load(cfg_dir), "respond", repo).timeout == 3600.0  # built-in
    write(cfg_dir / "config.yaml", "kinds: {respond: {timeout: 30m, model: m1}}\n")
    cfg = load(cfg_dir)
    assert config.kind_settings(cfg, "respond", repo).timeout == 1800.0  # config > built-in
    write(
        cfg_dir / "config.d" / "r.yaml",
        f"repos: {{{repo}: {{kinds: {{respond: {{timeout: 10m, env: {{set: {{X: '1'}}}}}}}}}}}}\n",
    )
    # repo > config, also when the environment sets every overridable scalar
    env = {var: value for var, value, *_ in ENV_CASES}
    cfg = load(cfg_dir, env)
    respond = config.kind_settings(cfg, "respond", repo)
    assert respond.timeout == 600.0
    assert respond.model == "m1"  # not overridden by the repo
    assert respond.env.set == {
        "GIT_CONFIG_GLOBAL": "${CSESSION_DIR}/empty-gitconfig",
        "GIT_CONFIG_NOSYSTEM": "1",
        "X": "1",
    }
    assert config.kind_settings(cfg, "respond", "other.example/x").timeout == 1800.0
    assert config.kind_settings(cfg, "respond", None).timeout == 1800.0
    flagged = config.with_flags(respond, timeout="5m", model=None)
    assert flagged.timeout == 300.0  # flags > repo
    assert flagged.model == "m1"


def test_kind_settings_for_unknown_kind_raises(cfg_dir: Path) -> None:
    with pytest.raises(KeyError):
        config.kind_settings(load(cfg_dir), "nope")


# Validation

# (config.yaml text, key the detail must name). Each must also fail the JSON schema.
INVALID: list[tuple[str, str]] = [
    ("unknown_key: 1\n", "unknown_key"),
    ("claude_bin: 5\n", "claude_bin"),
    ("min_claude_version: latest\n", "min_claude_version"),
    ("summary_max_bytes: 0\n", "summary_max_bytes"),
    ("artifact_max_bytes: '10'\n", "artifact_max_bytes"),
    ("input_max_bytes: 1.5\n", "input_max_bytes"),
    ("retention_days: -1\n", "retention_days"),
    ("notes_context_max_bytes: -1\n", "notes_context_max_bytes"),
    ("notes_context_cmd: []\n", "notes_context_cmd"),
    ("notes_context_sources: [boot]\n", "notes_context_sources"),
    ("notes_context_timeout: 0\n", "notes_context_timeout"),
    ("lost_grace: 0s\n", "lost_grace"),
    ("poll_interval: 0\n", "poll_interval"),
    ("session_start_timeout: 0.0m\n", "session_start_timeout"),
    ("session_start_timeout: null\n", "session_start_timeout"),
    ("auto_exit_delay: -1\n", "auto_exit_delay"),
    ("kill_grace: '15'\n", "kill_grace"),
    ("kill_grace: true\n", "kill_grace"),
    ("kinds: {Bad: {}}\n", "kinds.Bad"),
    ("kinds: {implement: {timeout: 0}}\n", "kinds.implement.timeout"),
    ("kinds: {implement: {interactive_timeout: 0s}}\n", "kinds.implement.interactive_timeout"),
    ("kinds: {implement: {sandbox: off}}\n", "kinds.implement.sandbox"),
    ("kinds: {implement: {max_turns: 0}}\n", "kinds.implement.max_turns"),
    ("kinds: {implement: {mcp: some}}\n", "kinds.implement.mcp"),
    ("kinds: {implement: {cwd: elsewhere}}\n", "kinds.implement.cwd"),
    ("kinds: {implement: {setting_sources: [global]}}\n", "kinds.implement.setting_sources"),
    ("kinds: {implement: {setting_sources: [user, user]}}\n", "kinds.implement.setting_sources"),
    ("kinds: {implement: {reads_third_party: 'yes'}}\n", "kinds.implement.reads_third_party"),
    ("kinds: {implement: {colour: red}}\n", "kinds.implement.colour"),
    ("kinds: {implement: {env: {set: {CSESSION_ID: x}}}}\n", "kinds.implement.env.set"),
    ("kinds: {implement: {env: {extra: 1}}}\n", "kinds.implement.env.extra"),
    ("kinds: {implement: {permission_profile: x.json}}\n", "kinds.implement.permission_profile"),
    ("kinds: {implement: 5}\n", "kinds.implement"),
    ("permission_profiles: {x.json: {}}\n", "permission_profiles.x.json"),
    ("permission_profiles: {p: {other: {}}}\n", "permission_profiles.p.other"),
    ("hooks: {pre-launch: [{timeout: 5s}]}\n", "hooks.pre-launch.0.cmd"),
    ("hooks: {pre-launch: [{cmd: x, timeout: 0}]}\n", "hooks.pre-launch.0.timeout"),
    ("hooks: {pre-launch: [{cmd: x, async: true}]}\n", "hooks.pre-launch.0.async"),
    ("hooks: {pre-launch: [{cmd: x, kinds: [Bad]}]}\n", "hooks.pre-launch.0.kinds"),
    ("hooks: {pre-launch: [{cmd: x, labels: [nokey]}]}\n", "hooks.pre-launch.0.labels"),
    (
        "hooks: {post-state-change: [{cmd: x, states: [gone]}]}\n",
        "hooks.post-state-change.0.states",
    ),
    ("hooks: {on-exit: []}\n", "hooks.on-exit"),
    ("repos: {Example.com/x: {}}\n", "repos.Example.com/x"),
    ("repos: {example.com/../x: {}}\n", "repos.example.com/../x"),
    (
        "repos: {example.com/x: {kinds: {respond: {reads_third_party: false}}}}\n",
        "reads_third_party",
    ),
    ("repos: {example.com/x: {kinds: {Bad: {}}}}\n", "repos.example.com/x.kinds.Bad"),
    ("repos: {example.com/x: {kinds: {respond: {timeout: 0}}}}\n", "kinds.respond.timeout"),
    ("me: {name: 5}\n", "me.name"),
    ("- a list\n", "top level"),
]


@pytest.mark.parametrize(("text", "key"), INVALID)
def test_invalid_config_names_the_key(cfg_dir: Path, text: str, key: str) -> None:
    path = write(cfg_dir / "config.yaml", text)
    error = invalid(cfg_dir)
    assert error.exit_code == 2
    assert error.code == "usage"
    assert key in str(error.detail)
    assert str(path) in str(error.detail)


@pytest.mark.parametrize(("text", "key"), INVALID)
def test_schema_rejects_the_same_configs(text: str, key: str) -> None:
    with pytest.raises(jsonschema.ValidationError):
        schemas.validate(yaml.safe_load(text), CONFIG_SCHEMA)


VALID = [
    "kill_grace: 0\nauto_exit_delay: 0s\n",
    "lost_grace: 1\npoll_interval: 100ms\nnotes_context_timeout: 0.5\n",
    "kinds: {implement: {timeout: none, interactive_timeout: 8h, max_turns: 3}}\n",
    "kinds: {x_1-y: {sandbox: 'off'}}\n",
    "hooks: {post-state-change: [{cmd: x, labels: ['a.b=c=d', 'e='], states: [idle]}]}\n",
    "repos: {example.com/a/b.c: {kinds: {review: {timeout: null}}}}\n",
    "me: {name: x, other: [1]}\n",
]


@pytest.mark.parametrize("text", VALID)
def test_valid_configs_pass_loader_and_schema(cfg_dir: Path, text: str) -> None:
    write(cfg_dir / "config.yaml", text)
    load(cfg_dir)
    schemas.validate(yaml.safe_load(text), CONFIG_SCHEMA)


def test_unquoted_off_gets_a_hint(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "kinds: {implement: {sandbox: off}}\n")
    assert 'quote "off"' in str(invalid(cfg_dir).detail)


def test_detail_names_the_dropin_that_set_the_key(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "kill_grace: 1s\n")
    bad = write(cfg_dir / "config.d" / "20-bad.yaml", "lost_grace: 0\n")
    write(cfg_dir / "config.d" / "30-fine.yaml", "kill_grace: 2s\n")
    detail = str(invalid(cfg_dir).detail)
    assert "lost_grace" in detail
    assert str(bad) in detail
    assert "30-fine" not in detail


def test_yaml_syntax_error_names_the_file(cfg_dir: Path) -> None:
    path = write(cfg_dir / "config.d" / "broken.yaml", "kinds: {implement: [\n")
    assert str(path) in str(invalid(cfg_dir).detail)


def test_dropin_must_be_a_mapping(cfg_dir: Path) -> None:
    path = write(cfg_dir / "config.d" / "list.yaml", "- 1\n")
    assert str(path) in str(invalid(cfg_dir).detail)


def test_invalid_utf8_is_a_usage_error(cfg_dir: Path) -> None:
    (cfg_dir).mkdir(parents=True)
    (cfg_dir / "config.yaml").write_bytes(b"claude_bin: \xff\n")
    assert "config.yaml" in str(invalid(cfg_dir).detail)


def test_invalid_config_envelope_matches_schema(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "lost_grace: 0\n")
    envelope = invalid(cfg_dir).envelope()
    schemas.validate(envelope, "claude-session/cli/error.json")
    assert envelope["error"]["code"] == "usage"
