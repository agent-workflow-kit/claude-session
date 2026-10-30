"""Config loading (interface.md, Config and Environment variables)."""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
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
    """The default config (``cfg_dir/config.yaml`` and its drop-ins) via ``XDG_CONFIG_HOME``."""
    return config.load(None, env={"XDG_CONFIG_HOME": str(cfg_dir.parent), **(env or {})})


def invalid(cfg_dir: Path, env: dict[str, str] | None = None) -> UsageError:
    with pytest.raises(UsageError) as info:
        load(cfg_dir, env)
    return info.value


# Defaults


def test_builtin_defaults_equal_the_example_file() -> None:
    assert defaults.BUILTIN == yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_loading_nothing_gives_the_example_values(cfg_dir: Path, tmp_path: Path) -> None:
    example = tmp_path / "example" / "config.yaml"
    example.parent.mkdir()
    shutil.copyfile(EXAMPLE, example)
    from_example = config.load(example, env={})
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
    # No flag sets a top-level key (stop --grace is separate from kill_grace).
    assert cfg.with_flags() == cfg


def test_flags_are_validated(cfg_dir: Path) -> None:
    with pytest.raises(UsageError) as info:
        config.with_flags(load(cfg_dir).kinds["implement"], timeout="soon")
    assert "timeout" in str(info.value.detail)


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
    ("repos: {Example.com/x: {}}\n", "repos.<invalid repo id>"),
    ("repos: {example.com/../x: {}}\n", "repos.<invalid repo id>"),
    (
        "repos: {example.com/x: {kinds: {respond: {reads_third_party: false}}}}\n",
        "reads_third_party",
    ),
    ("repos: {example.com/x: {kinds: {Bad: {}}}}\n", "repos.example.com/x.kinds.Bad"),
    ("repos: {example.com/x: {kinds: {respond: {timeout: 0}}}}\n", "kinds.respond.timeout"),
    ("me: {name: 5}\n", "me.name"),
    ("default_kind: Bad\n", "default_kind"),
    ("default_kind: ''\n", "default_kind"),
    ("claude_bin: ''\n", "claude_bin"),
    ('claude_bin: "a\\x01b"\n', "claude_bin"),
    ('claude_bin: "a\\x85b"\n', "claude_bin"),
    ("retention_days: 3.5\n", "retention_days"),
    ("kinds: {implement: {max_turns: 1.5}}\n", "kinds.implement.max_turns"),
    ("summary_max_bytes: true\n", "summary_max_bytes"),
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
    # integral floats are integers (JSON Schema "integer")
    "summary_max_bytes: 3.0\nretention_days: 0.0\nkinds: {implement: {max_turns: 2.0}}\n",
    # the only nulls the schema allows
    "kinds: {implement: {model: null, max_turns: null, interactive_timeout: null}}\n",
    "repos: {example.com/x: {kinds: {respond: {timeout: null, max_turns: null, model: null}}}}\n",
    "default_kind: x_1-y\nkill_grace: 36500d\n",
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


# Nulls (R16): the loader and the schema agree on every field that refuses null.

_OVERRIDE = "repos: {example.com/x: {kinds: {respond: %s}}}\n"
NULLS = [
    *[
        _OVERRIDE % f"{{{field}: null}}"
        for field in ["permission_profile", "sandbox", "env", "read_dirs", "write_dirs"]
    ],
    *[
        _OVERRIDE % f"{{env: {{{field}: null}}}}"
        for field in ["inherit", "allow", "set", "path_prepend"]
    ],
    *[
        f"hooks: {{pre-launch: [{{cmd: x, {field}: null}}]}}\n"
        for field in ["kinds", "exclude_kinds", "labels", "timeout"]
    ],
    "hooks: {post-state-change: [{cmd: x, states: null}]}\n",
    "hooks: {post-state-change: [{cmd: x, async: null}]}\n",
    "permission_profiles: {p: {permissions: null}}\n",
    "permission_profiles: {p: {sandbox: null}}\n",
    "me: {name: null}\n",
    "kinds: {implement: {env: {set: null}}}\n",
    "kinds: {implement: {permission_profile: null}}\n",
    "default_kind: null\n",
]


@pytest.mark.parametrize("text", NULLS)
def test_null_is_refused_where_the_schema_refuses_it(cfg_dir: Path, text: str) -> None:
    write(cfg_dir / "config.yaml", text)
    assert "config.yaml" in str(invalid(cfg_dir).detail)
    with pytest.raises(jsonschema.ValidationError):
        schemas.validate(yaml.safe_load(text), CONFIG_SCHEMA)


@pytest.mark.parametrize("value", ["claude\\n", "\\tclaude", "clau\\x7fde", "a\\x85b", "a\\x9fb"])
def test_claude_bin_refuses_control_characters(cfg_dir: Path, value: str) -> None:
    # (Python's re lets the schema's "$" match before a trailing newline; ECMA-262 does not.)
    write(cfg_dir / "config.yaml", f'claude_bin: "{value}"\n')
    assert "claude_bin" in str(invalid(cfg_dir).detail)


def test_integral_floats_become_ints(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "summary_max_bytes: 3.0\nkinds: {implement: {max_turns: 2.0}}\n")
    cfg = load(cfg_dir)
    assert cfg.summary_max_bytes == 3
    assert type(cfg.summary_max_bytes) is int
    assert cfg.kinds["implement"].max_turns == 2


# Files read (R1, R6, R9, R23)


def test_explicit_config_reads_only_that_file(tmp_path: Path) -> None:
    path = write(tmp_path / "shared" / "config.yaml", "kill_grace: 1s\n")
    write(
        tmp_path / "shared" / "config.d" / "x.yaml",
        "kill_grace: 2s\nhooks: {pre-launch: [{cmd: x}]}\n",
    )
    default_dir = tmp_path / "xdg" / "claude-session"
    write(default_dir / "config.d" / "y.yaml", "kill_grace: 3s\n")
    cfg = config.load(path, env={"XDG_CONFIG_HOME": str(default_dir.parent)})
    assert cfg.kill_grace == 1.0
    assert cfg.hooks.pre_launch == []


def test_dropins_apply_in_byte_order_of_file_name(cfg_dir: Path) -> None:
    write(cfg_dir / "config.d" / "9-a.yaml", "kill_grace: 9s\n")
    write(cfg_dir / "config.d" / "10-b.yaml", "kill_grace: 10s\n")
    write(cfg_dir / "config.d" / "B.yaml", "lost_grace: 2s\n")
    write(cfg_dir / "config.d" / "a.yaml", "lost_grace: 1s\n")
    cfg = load(cfg_dir)
    assert cfg.kill_grace == 9.0  # "10-b" < "9-a"
    assert cfg.lost_grace == 1.0  # "B" < "a"


def test_fifo_config_is_refused_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    os.mkfifo(path)
    started = time.monotonic()
    with pytest.raises(UsageError) as info:
        config.load(path, env={})
    assert time.monotonic() - started < 5
    assert "not a regular file" in str(info.value.detail)


def test_character_device_config_is_refused() -> None:
    with pytest.raises(UsageError) as info:
        config.load("/dev/null", env={})
    assert "not a regular file" in str(info.value.detail)


def test_config_over_1_mib_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(b"#" * config.MAX_FILE_BYTES + b"\n")
    with pytest.raises(UsageError) as info:
        config.load(path, env={})
    assert "1 MiB" in str(info.value.detail)


def test_config_of_exactly_1_mib_is_read(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes(b"#" * (config.MAX_FILE_BYTES - 1) + b"\n")
    assert config.load(path, env={}) == config.Config.model_validate(defaults.BUILTIN)


def test_symlinked_config_is_followed(tmp_path: Path) -> None:
    target = write(tmp_path / "real.yaml", "kill_grace: 4s\n")
    link = tmp_path / "config.yaml"
    link.symlink_to(target)
    assert config.load(link, env={}).kill_grace == 4.0


def test_dangling_default_config_symlink_is_an_error(cfg_dir: Path) -> None:
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").symlink_to(cfg_dir / "missing.yaml")
    assert "config.yaml" in str(invalid(cfg_dir).detail)


def _dropin_directory(path: Path) -> None:
    path.mkdir()


def _dropin_dangling(path: Path) -> None:
    path.symlink_to(path.parent / "missing")


def _dropin_loop(path: Path) -> None:
    path.symlink_to(path)


def _dropin_fifo(path: Path) -> None:
    os.mkfifo(path)


@pytest.mark.parametrize("make", [_dropin_directory, _dropin_dangling, _dropin_loop, _dropin_fifo])
def test_unreadable_dropin_is_an_error(cfg_dir: Path, make: Any) -> None:
    (cfg_dir / "config.d").mkdir(parents=True)
    make(cfg_dir / "config.d" / "x.yaml")
    assert "x.yaml" in str(invalid(cfg_dir).detail)


# YAML hardening (R5, R7, R10, R11, R12)


def test_alias_bomb_is_refused_quickly(cfg_dir: Path) -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for level in range(1, 6):
        refs = ", ".join([f"*a{level - 1}"] * 10)
        lines.append(f"a{level}: &a{level} [{refs}]")
    # me allows extra keys, so only the size limit can refuse this (10^6 nodes expanded)
    text = "me:\n" + "".join(f"  {line}\n" for line in lines)
    write(cfg_dir / "config.yaml", text)
    started = time.monotonic()
    detail = str(invalid(cfg_dir).detail)
    assert time.monotonic() - started < 5
    assert "nodes" in detail
    assert "config.yaml" in detail


def test_deep_nesting_is_refused(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "me: {x: " + "[" * 100 + "]" * 100 + "}\n")
    detail = str(invalid(cfg_dir).detail)
    assert "nested" in detail
    assert "config.yaml" in detail


def test_nesting_at_the_limit_is_accepted(cfg_dir: Path) -> None:
    depth = config.MAX_DEPTH - 2  # top-level mapping, me, x, then the lists
    write(cfg_dir / "config.yaml", "me: {x: " + "[" * depth + "]" * depth + "}\n")
    load(cfg_dir)


def test_very_deep_nesting_is_a_usage_error(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "me: {x: " + "[" * 900 + "]" * 900 + "}\n")
    assert "config.yaml" in str(invalid(cfg_dir).detail)


def test_extreme_nesting_is_refused_before_composing(tmp_path: Path) -> None:
    # libyaml's composer recurses without limit: run in a child so a regression crashes only it.
    levels = 50_000
    path = tmp_path / "deep.yaml"
    path.write_text("me: {x: " + "[" * levels + "]" * levels + "}\n", encoding="utf-8")
    code = textwrap.dedent(
        """
        import sys
        from claude_session import config
        from claude_session.errors import UsageError
        try:
            config.load(sys.argv[1])
        except UsageError as exc:
            print(exc.detail)
            sys.exit(0)
        sys.exit(3)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "nested more than" in proc.stdout
    assert str(path) in proc.stdout


def test_recursive_alias_is_refused(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "me: {x: &a [*a]}\n")
    assert "config.yaml" in str(invalid(cfg_dir).detail)


def test_overlong_integer_is_a_usage_error(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "retention_days: " + "1" * 5000 + "\n")
    assert "config.yaml" in str(invalid(cfg_dir).detail)


def test_huge_integer_duration_is_a_usage_error(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "kill_grace: 1" + "0" * 400 + "\n")
    assert "kill_grace" in str(invalid(cfg_dir).detail)


def test_duplicate_keys_are_refused(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "kill_grace: 1s\nlost_grace: 1m\nkill_grace: 2s\n")
    detail = str(invalid(cfg_dir).detail)
    assert "duplicate" in detail
    assert "line 3" in detail


def test_nested_duplicate_keys_are_refused(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "kinds:\n  implement: {timeout: 1m, timeout: 2m}\n")
    assert "duplicate" in str(invalid(cfg_dir).detail)


@pytest.mark.parametrize("value", ["1:30", "1:30:00", "1:30.5"])
def test_sexagesimal_numbers_stay_strings(cfg_dir: Path, value: str) -> None:
    write(cfg_dir / "config.yaml", f"kill_grace: {value}\n")
    assert "kill_grace" in str(invalid(cfg_dir).detail)


@pytest.mark.parametrize("value", ["!!int 1:30", "!!float 1:30.5", "!!int 1:30:00"])
def test_tagged_base60_numbers_are_invalid(cfg_dir: Path, value: str) -> None:
    write(cfg_dir / "config.yaml", f"kill_grace: {value}\n")
    detail = str(invalid(cfg_dir).detail)
    assert "base-60" in detail
    assert "line 1" in detail


def test_tagged_ordinary_numbers_still_load(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "retention_days: !!int 12\nkill_grace: !!float 1.5\n")
    cfg = load(cfg_dir)
    assert (cfg.retention_days, cfg.kill_grace) == (12, 1.5)


def test_two_merge_keys_are_refused(cfg_dir: Path) -> None:
    write(
        cfg_dir / "config.yaml",
        "me:\n  a: &a {x: 1}\n  b: &b {y: 2}\n  c: {<<: *a, <<: *b}\n",
    )
    detail = str(invalid(cfg_dir).detail)
    assert "duplicate merge key" in detail
    assert "line 4" in detail


def test_one_merge_key_with_several_sources_loads(cfg_dir: Path) -> None:
    write(
        cfg_dir / "config.yaml",
        "me:\n  a: &a {x: 1}\n  b: &b {y: 2}\n  c: {<<: [*a, *b], z: 3}\n",
    )
    assert load(cfg_dir).me.model_extra == {
        "a": {"x": 1},
        "b": {"y": 2},
        "c": {"x": 1, "y": 2, "z": 3},
    }


def test_ordinary_yaml_numbers_still_load(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "retention_days: 12\nkill_grace: 1.5\nlost_grace: 0x10\n")
    cfg = load(cfg_dir)
    assert (cfg.retention_days, cfg.kill_grace, cfg.lost_grace) == (12, 1.5, 16.0)


@pytest.mark.parametrize(
    ("text", "parent"),
    [
        ("kinds: {yes: {}}\n", "kinds"),
        ("kinds: {implement: {env: {set: {1: x}}}}\n", "kinds.implement.env.set"),
        ("1: x\n", "top level"),
    ],
)
def test_non_string_keys_are_refused(cfg_dir: Path, text: str, parent: str) -> None:
    write(cfg_dir / "config.yaml", text)
    detail = str(invalid(cfg_dir).detail)
    assert parent in detail
    assert "quote" in detail


# Values never echoed in details (R8)

SECRET = "SENTINEL0secret"


@pytest.mark.parametrize(
    "var",
    [
        "CSESSION_RETENTION_DAYS",
        "CSESSION_KILL_GRACE",
        "CSESSION_MIN_CLAUDE_VERSION",
        "CSESSION_DEFAULT_KIND",
        "CSESSION_CLAUDE_BIN",
    ],
)
def test_env_values_are_not_echoed(cfg_dir: Path, var: str) -> None:
    error = invalid(cfg_dir, {var: SECRET + "\x01"})
    assert var in str(error.detail)
    assert SECRET not in str(error.detail)
    assert SECRET not in error.message


@pytest.mark.parametrize(
    "text",
    [
        f"repos: {{'{SECRET}@example.com/x': {{}}}}\n",
        f"claude_bin: !{SECRET} x\n",
        f"me: !!python/name:{SECRET} x\n",
        f"kill_grace: {SECRET}\n",
        f"min_claude_version: {SECRET}\n",
        f"me: *{SECRET}\n",
    ],
)
def test_config_values_are_not_echoed(cfg_dir: Path, text: str) -> None:
    write(cfg_dir / "config.yaml", text)
    error = invalid(cfg_dir)
    assert SECRET not in str(error.detail)
    assert "config.yaml" in str(error.detail)


# YAML errors carry a fixed phrase and a position only (S4, S5)

YAML_PROBLEMS = {
    "invalid character",
    "invalid YAML syntax",
    "invalid alias or document structure",
    "unsupported or invalid tagged value",
    "invalid value",
    "duplicate key",
    "duplicate merge key",
    "base-60 numbers are not supported",
    "invalid YAML",
}


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("me: !!binary SECRET\u00e9\n", "unsupported or invalid tagged value"),
        ("me: !!binary 'can''t SECRET'\n", "unsupported or invalid tagged value"),
        (f"claude_bin: !{SECRET} x\n", "unsupported or invalid tagged value"),
        (f"me: !!python/name:{SECRET} x\n", "unsupported or invalid tagged value"),
        (f"me: *{SECRET}\n", "invalid alias or document structure"),
        (f"me: {{a: '{SECRET}\n", "invalid YAML syntax"),
        (f"me: [{SECRET}\n", "invalid YAML syntax"),
        ("me: {d: 2001-13-45}\n", "invalid value"),
        ("retention_days: " + "1" * 5000 + "\n", "invalid value"),
        ("a: 1\nb: 2\na: 3\n", "duplicate key"),
    ],
)
def test_yaml_errors_use_a_fixed_phrase(cfg_dir: Path, text: str, problem: str) -> None:
    path = write(cfg_dir / "config.yaml", text)
    detail = str(invalid(cfg_dir).detail)
    assert "SECRET" not in detail
    assert "\u00e9" not in detail
    assert "\\xe9" not in detail
    location, _, phrase = detail.removeprefix(str(path)).partition(": ")
    assert phrase == problem
    assert phrase in YAML_PROBLEMS
    assert re.fullmatch(r" at line [0-9]+, column [0-9]+", location)


def test_invalid_timestamp_is_an_invalid_value_with_its_position(cfg_dir: Path) -> None:
    write(cfg_dir / "config.yaml", "me:\n  d: 2001-13-45\n")
    detail = str(invalid(cfg_dir).detail)
    assert detail.endswith(": invalid value")
    assert "line 2, column 6" in detail
    assert "too long" not in detail


# Environment edge cases (R13, R14, R24)


@pytest.mark.parametrize("var", sorted(config.ENV_OVERRIDES))
def test_empty_env_override_is_invalid(cfg_dir: Path, var: str) -> None:
    assert var in str(invalid(cfg_dir, {var: ""}).detail)


@pytest.mark.parametrize("value", ["+5", " 5", "5 ", "5\n", "٥", "1_000", "0x10"])
def test_env_integers_are_plain_ascii_digits(cfg_dir: Path, value: str) -> None:
    assert "CSESSION_RETENTION_DAYS" in str(
        invalid(cfg_dir, {"CSESSION_RETENTION_DAYS": value}).detail
    )


@pytest.mark.parametrize("value", ["5s\n", "+5", "٥s", "1e3", "36501d"])
def test_env_durations_use_the_duration_grammar(cfg_dir: Path, value: str) -> None:
    assert "CSESSION_KILL_GRACE" in str(invalid(cfg_dir, {"CSESSION_KILL_GRACE": value}).detail)


@pytest.mark.parametrize("value", ["5s\n", "٥s", "+5s", "1:30"])
def test_yaml_durations_use_the_duration_grammar(cfg_dir: Path, value: str) -> None:
    write(cfg_dir / "config.yaml", f"kill_grace: {yaml.safe_dump(value).strip()}\n")
    assert "kill_grace" in str(invalid(cfg_dir).detail)


def test_env_default_kind_must_match_the_grammar(cfg_dir: Path) -> None:
    error = invalid(cfg_dir, {"CSESSION_DEFAULT_KIND": "Bad Kind"})
    assert "CSESSION_DEFAULT_KIND" in str(error.detail)


def test_builtin_defaults_are_not_mutated_by_loading(cfg_dir: Path) -> None:
    before = copy.deepcopy(defaults.BUILTIN)
    write(
        cfg_dir / "config.yaml",
        """
        hooks: {pre-launch: [{cmd: a}], post-state-change: [{cmd: b}]}
        kinds:
          triage: {timeout: 1m, env: {set: {A: "1"}}}
          implement: {env: {set: {B: "2"}, allow: [C]}}
        permission_profiles: {review: {permissions: {allow: [X]}}}
        """,
    )
    write(cfg_dir / "config.d" / "x.yaml", "hooks: {pre-launch: [{cmd: c}]}\n")
    load(cfg_dir)
    load(cfg_dir)
    assert defaults.BUILTIN == before


# Flags (R4, R18)


@pytest.mark.parametrize("value", ["36501d", 10**400, 3153600001])
def test_flag_durations_are_capped(cfg_dir: Path, value: object) -> None:
    kind = load(cfg_dir).kinds["implement"]
    with pytest.raises(UsageError):
        config.with_flags(kind, timeout=value)


def test_flag_duration_at_the_cap_is_accepted(cfg_dir: Path) -> None:
    kind = load(cfg_dir).kinds["implement"]
    assert config.with_flags(kind, timeout="36500d").timeout == 3153600000


@pytest.mark.parametrize(
    "key", ["kill_grace", "secret_env", "hooks", "kinds", "claude_bin", "repos", "me"]
)
def test_config_flags_are_allowlisted(cfg_dir: Path, key: str) -> None:
    with pytest.raises(TypeError):
        load(cfg_dir).with_flags(**{key: []})


@pytest.mark.parametrize("key", ["claude_args", "write_dirs", "implicit_done", "backstop", "cwd"])
def test_kind_flags_are_allowlisted(cfg_dir: Path, key: str) -> None:
    with pytest.raises(TypeError):
        config.with_flags(load(cfg_dir).kinds["implement"], **{key: None})


def test_kind_flag_keys_are_settable(cfg_dir: Path) -> None:
    kind = load(cfg_dir).kinds["implement"]
    flagged = config.with_flags(
        kind,
        timeout="5m",
        interactive_timeout="none",
        max_turns=3,
        model="m",
        permission_profile="review",
        sandbox="required",
        setting_sources=[],
        reads_third_party=True,
        mcp="none",
        env={"inherit": False, "allow": ["A"], "set": {}, "path_prepend": []},
        auto_exit=False,
    )
    assert flagged.sandbox == "required"
    assert flagged.env.allow == ["A"]
