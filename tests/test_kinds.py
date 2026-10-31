"""Kind profile resolution with origins (interface.md, Launch flags, Config and Resume and fork
carry-over)."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from claude_session import cli, config, defaults, kinds
from claude_session.errors import UsageError
from claude_session.gateway import SystemGateway

from .fake_gateway import FakeGateway

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.yaml"
DRY_RUN_SCHEMA = ROOT / "schemas" / "cli" / "exec-dry-run.json"
REPO = "example.com/group/project"
SECRET = "s3cr3t-value-never-echoed"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    return tmp_path / "claude-session"


def load(cfg_dir: Path, text: str | None = None) -> config.Config:
    if text is not None:
        write(cfg_dir / "config.yaml", text)
    return config.load(None, env={"XDG_CONFIG_HOME": str(cfg_dir.parent)})


def no_origin(cwd: str) -> str | None:
    raise AssertionError("the origin must not be looked up")


def resolve(cfg: config.Config, kind: str | None = None, **kw: Any) -> kinds.Resolution:
    kw.setdefault("mode", "headless")
    kw.setdefault("origin_url", no_origin)
    return kinds.resolve(cfg, kind, **kw)


# Built-in kinds


def test_builtin_kinds_are_the_single_defaults_table() -> None:
    assert kinds.BUILTIN_KINDS is defaults.BUILTIN_KINDS
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert kinds.BUILTIN_KINDS == example["kinds"]


def test_builtin_kind_names_are_the_documented_ones() -> None:
    assert set(kinds.BUILTIN_KINDS) == {
        "implement",
        "draft-ticket",
        "audit",
        "improve",
        "rank",
        "review",
        "gate-check",
        "respond",
    }


@pytest.mark.parametrize("name", ["implement", "draft-ticket", "audit", "improve"])
def test_trusted_builtin_kinds(cfg_dir: Path, name: str) -> None:
    profile = resolve(load(cfg_dir), name).profile
    assert profile.sandbox == "off"
    assert profile.reads_third_party is False
    assert profile.cwd == "caller"
    assert profile.implicit_done is True
    assert profile.permission_profile == "default"


def test_rank_kind(cfg_dir: Path) -> None:
    profile = resolve(load(cfg_dir), "rank").profile
    assert profile.permission_profile == "readonly"
    assert profile.sandbox == "preferred"
    assert profile.env.inherit is False
    assert profile.cwd == "session-dir"
    assert profile.implicit_done is False


@pytest.mark.parametrize(("name", "implicit_done"), [("review", True), ("gate-check", False)])
def test_review_and_gate_check_kinds(cfg_dir: Path, name: str, implicit_done: bool) -> None:
    profile = resolve(load(cfg_dir), name).profile
    assert profile.reads_third_party is True
    assert profile.sandbox == "required"
    assert profile.cwd == "session-dir"
    assert profile.implicit_done is implicit_done
    assert profile.setting_sources == []
    assert profile.mcp == "none"


def test_respond_kind(cfg_dir: Path) -> None:
    profile = resolve(load(cfg_dir), "respond").profile
    assert profile.reads_third_party is True
    assert profile.sandbox == "required"
    assert profile.cwd == "caller"
    assert profile.permission_profile == "respond"
    assert profile.env.set["GIT_CONFIG_NOSYSTEM"] == "1"


def test_builtin_permission_profiles_set_no_default_mode() -> None:
    assert set(defaults.BUILTIN_PERMISSION_PROFILES) == {"default", "readonly", "review", "respond"}
    for name in ("readonly", "review", "respond"):
        permissions = defaults.BUILTIN_PERMISSION_PROFILES[name]["permissions"]
        assert "defaultMode" not in permissions


def test_default_kind_is_used_without_a_kind(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir))
    assert result.kind == "implement"
    assert result.builtin is True
    assert result.fallback is False
    assert result.warnings == ()


def test_configured_default_kind_is_used(cfg_dir: Path) -> None:
    assert resolve(load(cfg_dir, "default_kind: review\n")).kind == "review"


# Unknown kinds


def test_unknown_kind_gets_the_review_profile_and_a_warning(cfg_dir: Path) -> None:
    cfg = load(cfg_dir)
    result = resolve(cfg, "my-new-kind")
    assert result.kind == "my-new-kind"
    assert result.fallback is True
    assert result.builtin is False
    assert result.profile == cfg.kinds["review"]
    assert len(result.warnings) == 1
    assert "my-new-kind" in result.warnings[0]
    assert "review" in result.warnings[0]


def test_unknown_kind_uses_the_configured_review_profile(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "kinds: {review: {timeout: 5m}}\n")
    result = resolve(cfg, "nope")
    assert result.profile.timeout == 300.0
    assert result.origins["timeout"] == "config"
    assert result.origins["sandbox"] == "builtin"


def test_unknown_kind_resolves_like_review_including_its_repo_override(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        f"""\
        kinds: {{review: {{sandbox: preferred}}}}
        repos: {{{REPO}: {{kinds: {{review: {{sandbox: required, model: r}}}}}}}}
        """,
    )
    result = resolve(cfg, "typo", repo=REPO)
    assert result.kind == "typo"
    assert result.fallback is True
    assert len(result.warnings) == 1
    assert (result.profile.sandbox, result.origins["sandbox"]) == ("required", "repo")
    assert (result.profile.model, result.origins["model"]) == ("r", "repo")
    assert result.profile == resolve(cfg, "review", repo=REPO).profile
    assert result.repo == REPO


def test_unknown_kind_with_review_override_still_takes_flags(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, f"repos: {{{REPO}: {{kinds: {{review: {{model: r}}}}}}}}\n")
    result = resolve(cfg, "typo", repo=REPO, flags={"model": "m1"})
    assert (result.profile.model, result.origins["model"]) == ("m1", "flag")


def test_unknown_kind_still_takes_flags(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir), "nope", flags={"model": "m1"})
    assert result.profile.model == "m1"
    assert result.origins["model"] == "flag"


def test_configured_kind_is_not_a_fallback(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir, "kinds: {mine: {sandbox: preferred}}\n"), "mine")
    assert result.fallback is False
    assert result.builtin is False
    assert result.warnings == ()
    assert result.profile.sandbox == "preferred"
    assert result.origins["sandbox"] == "config"
    assert result.origins["reads_third_party"] == "builtin"  # from the built-in review kind


@pytest.mark.parametrize(
    "name", ["", "Review", "-x", "_x", "a b", "a/b", "x" * 65, "reviеw", "a\n", SECRET + "!"]
)
def test_invalid_kind_names_are_usage_errors(cfg_dir: Path, name: str) -> None:
    with pytest.raises(UsageError) as info:
        resolve(load(cfg_dir), name)
    assert info.value.exit_code == 2


def test_unknown_default_kind_is_an_invalid_config(cfg_dir: Path) -> None:
    with pytest.raises(UsageError) as info:
        load(cfg_dir, "default_kind: nope\n")
    assert "default_kind" in str(info.value.detail)
    assert "config.yaml" in str(info.value.detail)
    assert "nope" not in str(info.value.detail)


def test_unknown_default_kind_from_the_environment_names_the_variable(cfg_dir: Path) -> None:
    with pytest.raises(UsageError) as info:
        config.load(
            None,
            env={"XDG_CONFIG_HOME": str(cfg_dir.parent), "CSESSION_DEFAULT_KIND": "nope"},
        )
    assert "CSESSION_DEFAULT_KIND" in str(info.value.detail)


@pytest.mark.parametrize("kind", ["nope", "typo"])
def test_per_repo_override_of_an_unknown_kind_is_an_invalid_config(
    cfg_dir: Path, kind: str
) -> None:
    with pytest.raises(UsageError) as info:
        load(cfg_dir, f"repos: {{{REPO}: {{kinds: {{{kind}: {{sandbox: 'off'}}}}}}}}\n")
    assert info.value.exit_code == 2
    assert info.value.message == "invalid config"
    detail = str(info.value.detail)
    assert f"repos.{REPO}.kinds.{kind}" in detail
    assert "names no built-in or configured kind" in detail
    assert "config.yaml" in detail


def test_per_repo_override_of_a_configured_kind_is_valid(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir, f"kinds: {{mine: {{}}}}\nrepos: {{{REPO}: {{kinds: {{mine: {{model: r}}}}}}}}\n"
    )
    assert resolve(cfg, "mine", repo=REPO).profile.model == "r"


def test_default_kind_may_name_a_configured_kind(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "default_kind: mine\nkinds: {mine: {}}\n")
    assert resolve(cfg).kind == "mine"


# Layers and origins


def test_origins_cover_every_field(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir), "implement")
    expected = set(config.Kind.model_fields) - {"env"}
    expected |= {"env.inherit", "env.allow", "env.set", "env.path_prepend"}
    assert set(result.origins) == expected
    assert set(result.origins.values()) == {"builtin"}


def test_origin_values_match_the_dry_run_schema(cfg_dir: Path) -> None:
    schema = json.loads(DRY_RUN_SCHEMA.read_text(encoding="utf-8"))
    allowed = set(schema["properties"]["origins"]["additionalProperties"]["enum"])
    assert set(kinds.Origin.__args__) == allowed  # type: ignore[attr-defined]
    cfg = load(cfg_dir, f"kinds: {{review: {{model: m}}}}\nrepos: {{{REPO}: {{}}}}\n")
    result = resolve(cfg, "review", repo=REPO, flags={"reads_third_party": True, "max_turns": 3})
    assert set(result.origins.values()) <= allowed


def test_config_origin_for_changed_fields_only(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "kinds: {implement: {timeout: 30m, model: m1, sandbox: 'off'}}\n")
    origins = resolve(cfg, "implement").origins
    assert origins["timeout"] == "config"
    assert origins["model"] == "config"
    # A config value equal to the built-in one keeps origin builtin.
    assert origins["sandbox"] == "builtin"
    assert origins["max_turns"] == "builtin"


def test_config_env_parts_have_their_own_origins(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "kinds: {implement: {env: {set: {A: b}}}}\n")
    origins = resolve(cfg, "implement").origins
    assert origins["env.set"] == "config"
    assert origins["env.inherit"] == "builtin"
    assert origins["env.allow"] == "builtin"


def test_per_repo_override_applies_over_config(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        f"""\
        kinds: {{respond: {{timeout: 30m, model: m1}}}}
        repos: {{{REPO}: {{kinds: {{respond: {{timeout: 10m, env: {{allow: [X]}}}}}}}}}}
        """,
    )
    result = resolve(cfg, "respond", repo=REPO)
    assert result.profile.timeout == 600.0
    assert result.profile.model == "m1"
    assert result.profile.env.allow == ["X"]
    assert result.origins["timeout"] == "repo"
    assert result.origins["model"] == "config"
    assert result.origins["env.allow"] == "repo"
    assert result.origins["env.set"] == "builtin"
    assert result.origins["repo"] == "flag"


def test_per_repo_override_for_another_repo_does_not_apply(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, f"repos: {{{REPO}: {{kinds: {{respond: {{timeout: 10m}}}}}}}}\n")
    result = resolve(cfg, "respond", repo="example.com/other")
    assert result.profile.timeout == 3600.0
    assert result.origins["timeout"] == "builtin"


def test_per_repo_explicit_null_is_a_repo_value(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, f"repos: {{{REPO}: {{kinds: {{implement: {{timeout: null}}}}}}}}\n")
    result = resolve(cfg, "implement", repo=REPO)
    assert result.profile.timeout is None
    assert result.origins["timeout"] == "repo"


def test_previous_run_flags_apply_over_the_repo(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, f"repos: {{{REPO}: {{kinds: {{implement: {{model: r}}}}}}}}\n")
    previous = kinds.Previous(repo=REPO, flags={"model": "p", "sandbox": "preferred"})
    result = resolve(cfg, "implement", previous=previous)
    assert result.profile.model == "p"
    assert result.profile.sandbox == "preferred"
    assert result.origins["model"] == "previous_run"
    assert result.origins["sandbox"] == "previous_run"
    assert result.repo == REPO
    assert result.origins["repo"] == "previous_run"


def test_flags_now_replace_previous_run_flags(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"model": "p", "max_turns": 4})
    result = resolve(load(cfg_dir), "implement", previous=previous, flags={"model": "now"})
    assert result.profile.model == "now"
    assert result.origins["model"] == "flag"
    assert result.profile.max_turns == 4
    assert result.origins["max_turns"] == "previous_run"


def test_full_precedence_chain(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        f"""\
        kinds: {{implement: {{timeout: 50m, max_turns: 5, model: c, auto_exit: false}}}}
        repos: {{{REPO}: {{kinds: {{implement: {{max_turns: 6, model: r}}}}}}}}
        """,
    )
    previous = kinds.Previous(repo=REPO, flags={"model": "p", "auto_exit": True})
    result = resolve(cfg, "implement", previous=previous, flags={"auto_exit": False})
    profile, origins = result.profile, result.origins
    assert (profile.backstop, origins["backstop"]) == (True, "builtin")
    assert (profile.timeout, origins["timeout"]) == (3000.0, "config")
    assert (profile.max_turns, origins["max_turns"]) == (6, "repo")
    assert (profile.model, origins["model"]) == ("p", "previous_run")
    assert (profile.auto_exit, origins["auto_exit"]) == (False, "flag")


def test_none_flag_values_mean_not_given(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"model": "p"})
    result = resolve(load(cfg_dir), "implement", previous=previous, flags={"model": None})
    assert result.profile.model == "p"
    assert result.origins["model"] == "previous_run"


def test_unknown_flag_key_is_a_programming_error(cfg_dir: Path) -> None:
    with pytest.raises(TypeError):
        resolve(load(cfg_dir), "implement", flags={"cwd": "x"})
    with pytest.raises(TypeError):
        resolve(load(cfg_dir), "implement", previous=kinds.Previous(None, {"kind": "x"}))


def test_resolution_does_not_mutate_config(cfg_dir: Path) -> None:
    cfg = load(cfg_dir)
    before = cfg.model_dump()
    resolve(
        cfg,
        "respond",
        flags={"env_allow": ["X"], "reads_third_party": True, "setting_sources": "user"},
    )
    assert cfg.model_dump() == before


# Individual flags


@pytest.mark.parametrize(
    ("mode", "field_name", "other"),
    [
        ("headless", "timeout", "interactive_timeout"),
        ("interactive", "interactive_timeout", "timeout"),
    ],
)
def test_timeout_flag_sets_the_bound_of_the_mode(
    cfg_dir: Path, mode: kinds.Mode, field_name: str, other: str
) -> None:
    result = resolve(load(cfg_dir), "implement", mode=mode, flags={"timeout": "90"})
    assert getattr(result.profile, field_name) == 90.0
    assert result.origins[field_name] == "flag"
    assert result.origins[other] == "builtin"
    assert result.timeout(mode) == 90.0


def test_timeout_none_flag_removes_the_bound(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir), "implement", flags={"timeout": "none"})
    assert result.profile.timeout is None
    assert result.timeout("headless") is None


def test_default_bounds_per_mode(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir), "implement")
    assert result.timeout("headless") == 3600.0
    assert result.timeout("interactive") is None


@pytest.mark.parametrize(
    ("flags", "flag"),
    [
        ({"timeout": "0"}, "--timeout"),
        ({"timeout": "soon"}, "--timeout"),
        ({"timeout": "36501d"}, "--timeout"),
        ({"max_turns": 0}, "--max-turns"),
        ({"max_turns": 9007199254740992}, "--max-turns"),
        ({"model": "-x"}, "--model"),
        ({"model": "a\x1bb"}, "--model"),
        ({"sandbox": "maybe"}, "--sandbox"),
        ({"setting_sources": "user,bogus"}, "--setting-sources"),
        ({"setting_sources": "user,user"}, "--setting-sources"),
        ({"setting_sources": ","}, "--setting-sources"),
        ({"permission_profile": "-x"}, "--permission-profile"),
        ({"auto_exit": "yes"}, "--[no-]auto-exit"),
    ],
)
def test_invalid_flag_values_are_usage_errors_naming_the_flag(
    cfg_dir: Path, flags: dict[str, Any], flag: str
) -> None:
    with pytest.raises(UsageError) as info:
        resolve(load(cfg_dir), "implement", flags=flags)
    assert info.value.exit_code == 2
    assert info.value.message == "invalid flag value"
    assert flag in str(info.value.detail)
    assert "(in " in str(info.value.detail) and "flags" in str(info.value.detail)


def test_invalid_previous_value_names_the_previous_run(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"sandbox": "maybe"})
    with pytest.raises(UsageError) as info:
        resolve(load(cfg_dir), "implement", previous=previous)
    assert "previous run" in str(info.value.detail)


def test_invalid_flag_value_is_not_echoed(cfg_dir: Path) -> None:
    with pytest.raises(UsageError) as info:
        resolve(load(cfg_dir), "implement", flags={"sandbox": SECRET})
    assert SECRET not in str(info.value.detail)
    assert SECRET not in info.value.message


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", []), ("user", ["user"]), ("project,local", ["project", "local"]), (["local"], ["local"])],
)
def test_setting_sources_flag_forms(cfg_dir: Path, value: Any, expected: list[str]) -> None:
    result = resolve(load(cfg_dir), "implement", flags={"setting_sources": value})
    assert result.profile.setting_sources == expected
    assert result.origins["setting_sources"] == "flag"


def test_permission_profile_flag(cfg_dir: Path) -> None:
    # TODO(#22): profile existence and files are validated by the permission-profile work.
    result = resolve(load(cfg_dir), "implement", flags={"permission_profile": "readonly"})
    assert result.profile.permission_profile == "readonly"
    assert result.origins["permission_profile"] == "flag"
    assert result.permission_profile_file is None
    assert "permission_profile_file" not in result.origins


@pytest.mark.parametrize("value", ["/x/p.json", "p.yaml", "p.yml", "dir/p", "a/b"])
def test_permission_profile_file_is_carried_raw(cfg_dir: Path, value: str) -> None:
    result = resolve(load(cfg_dir), "implement", flags={"permission_profile": value})
    assert result.permission_profile_file == value
    assert result.origins["permission_profile_file"] == "flag"
    assert result.profile.permission_profile == "default"
    assert result.origins["permission_profile"] == "builtin"


def test_carried_permission_profile_file_has_origin_previous_run(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"permission_profile": "/x/p.json"})
    result = resolve(load(cfg_dir), "implement", previous=previous)
    assert result.permission_profile_file == "/x/p.json"
    assert result.origins["permission_profile_file"] == "previous_run"


def test_permission_profile_name_now_replaces_a_carried_file(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"permission_profile": "/x/p.json"})
    result = resolve(
        load(cfg_dir), "implement", previous=previous, flags={"permission_profile": "readonly"}
    )
    assert result.permission_profile_file is None
    assert (result.profile.permission_profile, result.origins["permission_profile"]) == (
        "readonly",
        "flag",
    )


# --env-allow / --env-allowlist-only


def test_env_allow_is_kept_apart_from_the_kind_allowlist(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "kinds: {implement: {env: {allow: [A]}}}\n")
    result = resolve(cfg, "implement", flags={"env_allow": ["B", "A", "B"]})
    assert result.profile.env.allow == ["A"]
    assert result.origins["env.allow"] == "config"
    assert result.env_allow == ("B", "A")
    assert result.origins["env_allow"] == "flag"
    assert result.env_allowlist_only is None
    assert "env_allowlist_only" not in result.origins
    assert result.env_allowlist() == ["A", "B"]
    assert result.profile.env.inherit is True
    assert result.origins["env.inherit"] == "builtin"


def test_no_env_flags(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir), "implement")
    assert result.env_allow == ()
    assert result.env_allowlist_only is None
    assert "env_allow" not in result.origins


def test_env_allowlist_only_replaces_the_list_and_stops_inheritance(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "kinds: {implement: {env: {allow: [A]}}}\n")
    result = resolve(cfg, "implement", flags={"env_allowlist_only": ["C"]})
    assert result.profile.env.allow == ["A"]  # the kind's list, never flag names
    assert result.origins["env.allow"] == "config"
    assert result.env_allowlist_only == ("C",)
    assert result.origins["env_allowlist_only"] == "flag"
    assert result.env_allowlist() == ["C"]
    assert result.profile.env.inherit is False
    assert result.origins["env.inherit"] == "flag"


def test_repo_env_allow_keeps_origin_repo_with_env_allow_flag(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, f"repos: {{{REPO}: {{kinds: {{implement: {{env: {{allow: [S]}}}}}}}}}}\n")
    result = resolve(cfg, "implement", repo=REPO, flags={"env_allow": ["T"]})
    assert (result.profile.env.allow, result.origins["env.allow"]) == (["S"], "repo")
    assert result.env_allow == ("T",)


@pytest.mark.parametrize(
    ("names", "expected"), [([""], ()), (["", "X"], ("X",)), (["X", ""], ("X",))]
)
def test_empty_allowlist_only_name(
    cfg_dir: Path, names: list[str], expected: tuple[str, ...]
) -> None:
    result = resolve(load(cfg_dir), "implement", flags={"env_allowlist_only": names})
    assert result.env_allowlist_only == expected
    assert result.env_allowlist() == list(expected)
    assert result.profile.env.inherit is False


def test_env_allow_adds_to_a_replaced_list(cfg_dir: Path) -> None:
    flags = {"env_allowlist_only": ["C"], "env_allow": ["D"]}
    result = resolve(load(cfg_dir), "implement", flags=flags)
    assert result.env_allowlist() == ["C", "D"]


def test_env_flags_are_carried_separately(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"env_allowlist_only": ["C"], "env_allow": ["D"]})
    result = resolve(load(cfg_dir), "implement", previous=previous, flags={"env_allow": ["E"]})
    assert result.env_allowlist() == ["C", "E"]  # env_allow now replaces the stored one only
    assert result.origins["env_allow"] == "flag"
    assert result.origins["env_allowlist_only"] == "previous_run"
    assert result.profile.env.inherit is False  # allowlist-only mode persists
    assert result.origins["env.inherit"] == "previous_run"


@pytest.mark.parametrize(
    ("key", "names"),
    [
        ("env_allow", [""]),
        ("env_allow", ["A=B"]),
        ("env_allow", ["A\0"]),
        ("env_allowlist_only", ["A=B"]),
        ("env_allow", "A"),  # a string, not a list
        ("env_allow", [1]),
        ("env_allow", ["*"]),
        ("env_allow", ["GH_*"]),
        ("env_allow", ["a b"]),
        ("env_allow", ["$(x)"]),
        ("env_allow", ["1X"]),
        ("env_allow", ["é"]),
        ("env_allowlist_only", ["*"]),
        ("env_allowlist_only", ["GH_*"]),
        ("env_allowlist_only", ["a b"]),
        ("env_allowlist_only", ["1X"]),
        ("env_allowlist_only", ["é"]),
    ],
)
def test_invalid_env_names_are_usage_errors(cfg_dir: Path, key: str, names: Any) -> None:
    with pytest.raises(UsageError) as info:
        resolve(load(cfg_dir), "implement", flags={key: names})
    assert "--" + key.replace("_", "-") in info.value.message


def test_invalid_carried_env_name_is_a_usage_error(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"env_allow": ["GH_*"]})
    with pytest.raises(UsageError) as info:
        resolve(load(cfg_dir), "implement", previous=previous)
    assert "previous run" in info.value.message


@pytest.mark.parametrize("name", ["A", "_x", "GH_TOKEN_2", "a1"])
def test_valid_env_names(cfg_dir: Path, name: str) -> None:
    assert resolve(load(cfg_dir), "implement", flags={"env_allow": [name]}).env_allow == (name,)


# --reads-third-party


def test_reads_third_party_implies_isolation(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir), "implement", flags={"reads_third_party": True})
    profile, origins = result.profile, result.origins
    assert profile.reads_third_party is True
    assert origins["reads_third_party"] == "flag"
    assert (profile.sandbox, origins["sandbox"]) == ("required", "flag-implied")
    assert (profile.setting_sources, origins["setting_sources"]) == ([], "flag-implied")
    assert (profile.env.inherit, origins["env.inherit"]) == (False, "flag-implied")
    assert (profile.mcp, origins["mcp"]) == ("none", "flag-implied")


def test_explicit_flags_in_the_same_call_beat_the_implications(cfg_dir: Path) -> None:
    flags = {"reads_third_party": True, "sandbox": "preferred", "setting_sources": "user"}
    result = resolve(load(cfg_dir), "implement", flags=flags)
    assert (result.profile.sandbox, result.origins["sandbox"]) == ("preferred", "flag")
    assert result.profile.setting_sources == ["user"]
    assert result.origins["setting_sources"] == "flag"
    assert result.origins["mcp"] == "flag-implied"  # mcp has no flag
    assert result.origins["env.inherit"] == "flag-implied"


def test_reads_third_party_now_beats_a_carried_sandbox(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"sandbox": "off"})
    result = resolve(
        load(cfg_dir), "implement", previous=previous, flags={"reads_third_party": True}
    )
    assert (result.profile.sandbox, result.origins["sandbox"]) == ("required", "flag-implied")


def test_carried_explicit_settings_never_beat_carried_reads_third_party(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"reads_third_party": True, "sandbox": "off"})
    result = resolve(load(cfg_dir), "implement", previous=previous)
    assert (result.profile.sandbox, result.origins["sandbox"]) == ("required", "flag-implied")
    assert result.origins["setting_sources"] == "flag-implied"
    assert result.origins["reads_third_party"] == "previous_run"
    assert any("--sandbox" in w and "--reads-third-party" in w for w in result.warnings)


def test_three_run_chain_keeps_the_implications(cfg_dir: Path) -> None:
    cfg = load(cfg_dir)
    carried = {"sandbox": "off", "setting_sources": "user,project,local"}
    # Run 2: the stored flags of run 1 are carried; --reads-third-party is given now.
    run2 = resolve(
        cfg, "implement", previous=kinds.Previous(None, carried), flags={"reads_third_party": True}
    )
    assert (run2.profile.sandbox, run2.origins["sandbox"]) == ("required", "flag-implied")
    assert run2.profile.setting_sources == []
    # Run 3: everything is carried; nothing is given now.
    run3 = resolve(
        cfg, "implement", previous=kinds.Previous(None, {**carried, "reads_third_party": True})
    )
    assert (run3.profile.sandbox, run3.origins["sandbox"]) == ("required", "flag-implied")
    assert (run3.profile.setting_sources, run3.origins["setting_sources"]) == (
        [],
        "flag-implied",
    )
    assert run3.origins["reads_third_party"] == "previous_run"
    assert any("--setting-sources" in w for w in run3.warnings)


def test_sandbox_given_now_does_not_beat_carried_reads_third_party(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"reads_third_party": True})
    result = resolve(load(cfg_dir), "implement", previous=previous, flags={"sandbox": "off"})
    assert (result.profile.sandbox, result.origins["sandbox"]) == ("required", "flag-implied")
    assert any("--sandbox" in w and "flags" in w for w in result.warnings)


def test_sandbox_and_reads_third_party_both_now_give_off(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"reads_third_party": True})
    flags = {"reads_third_party": True, "sandbox": "off"}
    result = resolve(load(cfg_dir), "implement", previous=previous, flags=flags)
    assert (result.profile.sandbox, result.origins["sandbox"]) == ("off", "flag")
    assert result.warnings == ()


def test_matching_carried_value_gives_no_warning(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None, flags={"sandbox": "required", "env_allowlist_only": ["A"]})
    result = resolve(
        load(cfg_dir), "implement", previous=previous, flags={"reads_third_party": True}
    )
    assert result.origins["sandbox"] == "flag-implied"
    assert result.origins["env.inherit"] == "flag-implied"
    assert result.warnings == ()


def test_kind_reads_third_party_lets_flags_set_the_profile(cfg_dir: Path) -> None:
    # Without --reads-third-party there are no implications: the kind's values apply, and
    # explicit flags (given now or carried) replace them as usual.
    now = resolve(load(cfg_dir), "review", flags={"sandbox": "off"})
    assert (now.profile.sandbox, now.origins["sandbox"]) == ("off", "flag")
    carried = resolve(load(cfg_dir), "review", previous=kinds.Previous(None, {"sandbox": "off"}))
    assert (carried.profile.sandbox, carried.origins["sandbox"]) == ("off", "previous_run")


@pytest.mark.parametrize("value", [False, 0, "", "yes"])
def test_only_true_reads_third_party_counts_as_given(cfg_dir: Path, value: Any) -> None:
    cfg = load(cfg_dir)
    now = resolve(cfg, "review", flags={"reads_third_party": value})
    assert (now.profile.reads_third_party, now.origins["reads_third_party"]) == (True, "builtin")
    previous = kinds.Previous(repo=None, flags={"reads_third_party": value})
    carried = resolve(cfg, "review", previous=previous)
    assert carried.profile.reads_third_party is True
    assert carried.origins["reads_third_party"] == "builtin"
    plain = resolve(cfg, "implement", flags={"reads_third_party": value})
    assert plain.profile.sandbox == "off"
    assert "flag-implied" not in plain.origins.values()


def test_allowlist_only_in_the_same_call_sets_inherit(cfg_dir: Path) -> None:
    flags = {"reads_third_party": True, "env_allowlist_only": ["A"]}
    result = resolve(load(cfg_dir), "implement", flags=flags)
    assert (result.profile.env.inherit, result.origins["env.inherit"]) == (False, "flag")


# --repo


def test_repo_is_derived_from_the_origin_for_caller_cwd_kinds(
    cfg_dir: Path, tmp_path: Path
) -> None:
    calls: list[str] = []

    def origin(cwd: str) -> str | None:
        calls.append(cwd)
        return "git@Example.COM:group/project.git"

    cfg = load(cfg_dir, f"repos: {{{REPO}: {{kinds: {{implement: {{model: r}}}}}}}}\n")
    result = resolve(cfg, "implement", cwd=str(tmp_path), origin_url=origin)
    assert calls == [str(tmp_path)]
    assert result.repo == REPO
    assert result.origins["repo"] == "derived"
    assert (result.profile.model, result.origins["model"]) == ("r", "repo")


@pytest.mark.parametrize("kind", ["review", "gate-check", "rank", "nope"])
def test_repo_is_never_derived_for_session_dir_kinds(cfg_dir: Path, kind: str) -> None:
    result = resolve(load(cfg_dir), kind, cwd="/somewhere", origin_url=no_origin)
    assert result.repo is None
    assert "repo" not in result.origins


def test_session_dir_kind_takes_an_explicit_repo(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir), "review", repo=REPO, cwd="/x")
    assert result.repo == REPO
    assert result.origins["repo"] == "flag"


def test_respond_derives_its_repo(cfg_dir: Path) -> None:
    result = resolve(
        load(cfg_dir), "respond", cwd="/x", origin_url=lambda cwd: "https://example.com/a/b"
    )
    assert result.repo == "example.com/a/b"


def test_no_cwd_derives_nothing(cfg_dir: Path) -> None:
    assert resolve(load(cfg_dir), "implement").repo is None


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "/local/path/repo.git",
        "file:///srv/repo.git",
        "ftp://example.com/x",
        "https://example.com/",
        "https://exämple.com/a/b",
        "https://example.com/a/b\n",  # more than git's one trailing newline
        "https://example.com/a/b\nhttps://other.example/c",
        "https://example.com/a/../b",
        "https://example.com/a b",
    ],
)
def test_unusable_origins_give_no_repo(cfg_dir: Path, url: str | None) -> None:
    result = resolve(load(cfg_dir), "implement", cwd="/x", origin_url=lambda cwd: url)
    assert result.repo is None
    assert "repo" not in result.origins


def test_explicit_repo_wins_and_skips_derivation(cfg_dir: Path) -> None:
    result = resolve(load(cfg_dir), "implement", repo=REPO, cwd="/x", origin_url=no_origin)
    assert result.repo == REPO
    assert result.origins["repo"] == "flag"


def test_previous_repo_is_carried_without_derivation(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo=None)
    result = resolve(load(cfg_dir), "implement", previous=previous, cwd="/x")
    assert result.repo is None
    assert "repo" not in result.origins


def test_repo_given_now_replaces_the_carried_repo(cfg_dir: Path) -> None:
    previous = kinds.Previous(repo="example.com/old")
    result = resolve(load(cfg_dir), "implement", repo=REPO, previous=previous)
    assert result.repo == REPO
    assert result.origins["repo"] == "flag"


@pytest.mark.parametrize(
    "repo", ["", "Example.com/x", "example.com", "example.com/../x", "example.com/x\n", SECRET]
)
def test_invalid_repo_ids_are_usage_errors(cfg_dir: Path, repo: str) -> None:
    with pytest.raises(UsageError) as info:
        resolve(load(cfg_dir), "implement", repo=repo)
    assert "--repo" in info.value.message
    assert SECRET not in info.value.message
    with pytest.raises(UsageError):
        resolve(load(cfg_dir), "implement", previous=kinds.Previous(repo=repo))


# git origin lookup


GIT = "/usr/bin/git"


def origin(gw: FakeGateway, cwd: str = "/w") -> str | None:
    return kinds.git_origin_url(cwd, git=GIT, gw=gw)


def test_git_origin_url_runs_hardened_read_only_git() -> None:
    gw = FakeGateway()
    gw.on([GIT], stdout="git@example.com:a/b.git\n")
    assert origin(gw, "/work/-dir") == "git@example.com:a/b.git"
    (call,) = gw.calls
    argv = list(call.argv)
    assert argv[0] == GIT
    assert argv[-5:] == ["-C", "/work/-dir", "remote", "get-url", "origin"]
    for setting in (
        "core.fsmonitor=",
        "core.sshCommand=",
        "credential.helper=",
        "core.hooksPath=/dev/null",
        "diff.external=",
        "core.pager=cat",
    ):
        assert argv[argv.index(setting) - 1] == "-c"
    assert call.timeout is not None and call.timeout > 0


GIT_DISCOVERY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_KEY_12",
    "GIT_CONFIG_VALUE_12",
)


def test_git_origin_url_ignores_the_callers_git_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in GIT_DISCOVERY_VARIABLES:
        monkeypatch.setenv(name, "/elsewhere")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    monkeypatch.setenv("LC_ALL", "de_DE.ISO-8859-1")
    monkeypatch.setenv("CSESSION_KEEP_ME", "1")
    gw = FakeGateway()
    gw.on([GIT], stdout="https://example.com/a/b\n")
    origin(gw)
    (call,) = gw.calls
    env = call.env
    assert env is not None
    for name in GIT_DISCOVERY_VARIABLES:
        assert name not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["LC_ALL"] == "C"
    assert env["CSESSION_KEEP_ME"] == "1"


def test_real_git_lookup_ignores_git_dir(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos = {}
    for name, url in (
        ("work", "https://example.com/group/project"),
        ("other", "https://example.com/x/y"),
    ):
        path = tmp_path / name
        path.mkdir()
        git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(path)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "remote", "add", "origin", url], check=True)
        repos[name] = path
    monkeypatch.setenv("GIT_DIR", str(repos["other"] / ".git"))
    real_git = shutil.which("git")
    assert real_git is not None
    got = kinds.git_origin_url(str(repos["work"]), git=real_git, gw=SystemGateway())
    assert got == "https://example.com/group/project"


def test_real_git_origin_is_decoded_as_utf8_with_replacement(tmp_path: Path) -> None:
    """Through the real gateway: a non-UTF-8 origin is decoded with replacement (never
    silently "no origin"), and a carriage return is normalized, whatever the locale."""
    work = tmp_path / "work"
    work.mkdir()
    git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(work)]
    subprocess.run([*git, "init", "-q"], check=True)
    config_file = work / ".git" / "config"
    with config_file.open("ab") as fh:
        fh.write(b'[remote "origin"]\n\turl = https://example.com/caf\xe9\r/x\n')
    real_git = shutil.which("git")
    assert real_git is not None
    got = kinds.git_origin_url(str(work), git=real_git, gw=SystemGateway())
    assert got is not None
    assert "\ufffd" in got and "\r" not in got


def test_git_origin_url_trims_only_one_newline() -> None:
    gw = FakeGateway()
    gw.on([GIT], stdout="https://example.com/a\n\n")
    assert origin(gw) == "https://example.com/a\n"


@pytest.mark.parametrize(
    "response",
    [
        {"returncode": 2},
        {"returncode": 128, "stdout": "fatal: not a git repository\n"},
        {"error": FileNotFoundError("git")},
        {"timeout": True},
    ],
)
def test_git_origin_url_failures_give_none(response: dict[str, Any]) -> None:
    gw = FakeGateway()
    gw.on([GIT], **response)
    assert origin(gw) is None


def test_default_lookup_without_git_on_path_gives_no_repo(
    cfg_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    result = kinds.resolve(load(cfg_dir), "implement", mode="headless", cwd=str(tmp_path))
    assert result.repo is None


def test_default_lookup_derives_from_a_real_repository(cfg_dir: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(work)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run(
        [*git, "remote", "add", "origin", "ssh://git@example.com:22/group/project.git"],
        check=True,
    )
    result = kinds.resolve(load(cfg_dir), "implement", mode="headless", cwd=str(work))
    assert result.repo == REPO
    assert result.origins["repo"] == "derived"


def test_default_lookup_outside_a_repository_gives_no_repo(cfg_dir: Path, tmp_path: Path) -> None:
    result = kinds.resolve(load(cfg_dir), "implement", mode="headless", cwd=str(tmp_path))
    assert result.repo is None


# --config


def test_cli_load_config_reads_only_the_named_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "xdg"
    write(home / "claude-session" / "config.yaml", "default_kind: review\n")
    write(home / "claude-session" / "config.d" / "a.yaml", "default_kind: audit\n")
    named_dir = tmp_path / "named"
    write(named_dir / "c.yaml", "default_kind: rank\n")
    write(named_dir / "config.d" / "b.yaml", "default_kind: improve\n")  # never read
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.delenv("CSESSION_DEFAULT_KIND", raising=False)
    parser = cli.build_parser()
    assert cli.load_config(cli.parse(parser, ["kinds"])).default_kind == "audit"
    named = cli.parse(parser, ["kinds", "--config", str(named_dir / "c.yaml")])
    assert cli.load_config(named).default_kind == "rank"


def test_cli_load_config_requires_the_named_file(tmp_path: Path) -> None:
    args = cli.parse(cli.build_parser(), ["kinds", "--config", str(tmp_path / "missing.yaml")])
    with pytest.raises(UsageError) as info:
        cli.load_config(args)
    assert info.value.exit_code == 2
