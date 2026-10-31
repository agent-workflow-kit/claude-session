"""Session environment builder (interface.md, Environment variables; design.md, Environment)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from claude_session import config, envbuild, kinds, secretindex
from claude_session.errors import UsageError

SID = "cs-k3v7q2xa"
SDIR = "/state/claude-session/sessions/cs-k3v7q2xa"
CWD = "/work/repo"
EXE_DIR = "/opt/claude-session/bin"
SECRET = "s3cr3t-value-never-echoed"

CALLER: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/user",
    "USER": "user",
    "LOGNAME": "user",
    "SHELL": "/bin/sh",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LC_CTYPE": "C.UTF-8",
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "TZ": "UTC",
    "EDITOR": "vi",
    "NPM_TOKEN_EXTRA": "x",
}


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    return tmp_path / "claude-session"


def load(cfg_dir: Path, text: str | None = None) -> config.Config:
    path = cfg_dir / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return config.load(None, env={"XDG_CONFIG_HOME": str(cfg_dir.parent)})


def resolve(cfg: config.Config, kind: str, **kw: Any) -> kinds.Resolution:
    kw.setdefault("mode", "headless")
    return kinds.resolve(cfg, kind, **kw)


def build(
    cfg: config.Config,
    kind: str = "implement",
    environ: dict[str, str] | None = None,
    *,
    flags: dict[str, Any] | None = None,
    repo: str | None = None,
    **kw: Any,
) -> envbuild.SessionEnv:
    resolution = resolve(cfg, kind, flags=flags, repo=repo)
    kw.setdefault("csession_id", SID)
    kw.setdefault("csession_dir", SDIR)
    kw.setdefault("cwd", CWD)
    kw.setdefault("exe_dir", EXE_DIR)
    return envbuild.build(resolution, cfg, CALLER if environ is None else environ, **kw)


def build_unvalidated(cfg: config.Config, **env_update: Any) -> envbuild.SessionEnv:
    """Build ``implement`` with ``env`` fields that bypass config validation (the backstop)."""
    profile = cfg.kinds["implement"]
    bad = profile.model_copy(update={"env": profile.env.model_copy(update=env_update)}, deep=True)
    resolution = kinds.Resolution(
        kind="implement", builtin=True, fallback=False, repo=None, profile=bad, origins={}
    )
    return envbuild.build(
        resolution, cfg, CALLER, csession_id=SID, csession_dir=SDIR, cwd=CWD, exe_dir=EXE_DIR
    )


def env_of(*args: Any, **kw: Any) -> dict[str, str]:
    return build(*args, **kw).env


def path_of(env: dict[str, str]) -> list[str]:
    return env["PATH"].split(":")


# 1. Inherit vs allowlist


def test_inherit_passes_the_whole_caller_environment(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir), "implement")
    for name, value in CALLER.items():
        if name != "PATH":
            assert env[name] == value
    assert env["EDITOR"] == "vi"


def test_allowlist_passes_only_the_builtin_allowlist(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir), "rank")
    expected = {k: v for k, v in CALLER.items() if k not in ("EDITOR", "NPM_TOKEN_EXTRA", "PATH")}
    rest = {k: v for k, v in env.items() if k != "PATH" and not k.startswith("CSESSION_")}
    assert rest == expected


def test_allowlist_glob_lc_matches_prefix_only(cfg_dir: Path) -> None:
    environ = {**CALLER, "XLC_FOO": "1", "LC": "1", "lc_all": "1"}
    env = env_of(load(cfg_dir), "rank", environ)
    assert "LC_ALL" in env and "LC_CTYPE" in env
    assert "XLC_FOO" not in env and "LC" not in env and "lc_all" not in env


def test_allowlist_mode_does_not_add_absent_variables(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir), "rank", {"HOME": "/h"})
    assert "USER" not in env and "TZ" not in env
    assert env["HOME"] == "/h"


def test_config_env_allow_names_and_globs(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          rank:
            env: {inherit: false, allow: [EDITOR, NPM_*], set: {}, path_prepend: []}
        """,
    )
    environ = {**CALLER, "NPM_CONFIG": "c", "NPMX": "n"}
    env = env_of(cfg, "rank", environ)
    assert env["EDITOR"] == "vi"
    assert env["NPM_CONFIG"] == "c" and env["NPM_TOKEN_EXTRA"] == "x"
    assert "NPMX" not in env


@pytest.mark.parametrize("pattern", ["*", "A*B", "*FOO", "1BAD_*", "BAD-*"])
def test_malformed_allow_patterns_are_refused_at_load(cfg_dir: Path, pattern: str) -> None:
    with pytest.raises(UsageError) as info:
        load(
            cfg_dir,
            f"""
            kinds:
              rank:
                env: {{inherit: false, allow: ["{pattern}"], set: {{}}, path_prepend: []}}
            """,
        )
    assert "kinds.rank.env.allow.0" in str(info.value.detail)


@pytest.mark.parametrize("pattern", ["*", "A*B", "*FOO", "1BAD_*", "BAD-*"])
def test_malformed_allow_patterns_match_nothing_beyond_the_literal(
    cfg_dir: Path, pattern: str
) -> None:
    # Backstop: a pattern that bypassed config validation still matches no other name.
    cfg = load(cfg_dir)
    profile = cfg.kinds["rank"]
    bad = profile.model_copy(
        update={"env": profile.env.model_copy(update={"allow": [pattern]})}, deep=True
    )
    resolution = kinds.Resolution(
        kind="rank", builtin=True, fallback=False, repo=None, profile=bad, origins={}
    )
    environ = {"EDITOR": "vi", "AXB": "1", "AFOO": "1", "1BAD_X": "1", "BAD-X": "1"}
    env = envbuild.build(
        resolution, cfg, environ, csession_id=SID, csession_dir=SDIR, cwd=CWD, exe_dir=EXE_DIR
    ).env
    assert set(env) - {"PATH"} == {"CSESSION_ID", "CSESSION_DIR", "CSESSION_KIND"}


def test_env_allow_flag_adds_names_in_allowlist_mode(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir), "rank", flags={"env_allow": ["EDITOR"]})
    assert env["EDITOR"] == "vi"
    assert "NPM_TOKEN_EXTRA" not in env


def test_env_allowlist_only_forces_allowlist_and_replaces_env_allow(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          implement:
            env: {inherit: true, allow: [NPM_*], set: {}, path_prepend: []}
        """,
    )
    env = env_of(cfg, "implement", flags={"env_allowlist_only": ["EDITOR"]})
    assert env["EDITOR"] == "vi"
    assert "NPM_TOKEN_EXTRA" not in env
    assert env["HOME"] == CALLER["HOME"]


def test_env_allowlist_only_empty_keeps_only_builtin_allowlist(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir), "implement", flags={"env_allowlist_only": [""]})
    assert "EDITOR" not in env
    assert env["USER"] == "user"


def test_reads_third_party_flag_implies_allowlist(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir), "implement", flags={"reads_third_party": True})
    assert "EDITOR" not in env


def test_caller_environ_is_not_mutated(cfg_dir: Path) -> None:
    environ = dict(CALLER, CSESSION_ID="cs-aaaaaaaa", GH_TOKEN="t")
    before = dict(environ)
    build(load(cfg_dir), "review", environ)
    assert environ == before


# 2. claude_auth_env


AUTH = {
    "ANTHROPIC_API_KEY": "key-value-123",
    "ANTHROPIC_AUTH_TOKEN": "token-value-123",
    "ANTHROPIC_BASE_URL": "https://proxy.example.com",
    "ANTHROPIC_MODEL": "model-x",
}


def test_builtin_auth_names_pass_in_allowlist_mode(cfg_dir: Path) -> None:
    result = build(load(cfg_dir), "rank", {**CALLER, **AUTH})
    for name, value in AUTH.items():
        assert result.env[name] == value
    assert set(result.auth_names) == set(AUTH)


def test_auth_names_absent_from_caller_are_not_added(cfg_dir: Path) -> None:
    result = build(load(cfg_dir), "rank", CALLER)
    assert not any(name in result.env for name in AUTH)
    assert result.auth_names == ()


def test_cloud_variables_without_provider_flags_are_not_passed(cfg_dir: Path) -> None:
    environ = {
        **CALLER,
        "AWS_ACCESS_KEY_ID": "a",
        "AWS_SECRET_ACCESS_KEY": "b",
        "GOOGLE_APPLICATION_CREDENTIALS": "c",
        "CLOUD_ML_REGION": "r",
        "ANTHROPIC_VERTEX_PROJECT_ID": "p",
    }
    env = env_of(load(cfg_dir), "rank", environ)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUD_ML_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
    ):
        assert name not in env


def test_bedrock_passes_aws_variables(cfg_dir: Path) -> None:
    environ = {
        **CALLER,
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "p",
        "AWSX": "no",
        "GOOGLE_CLOUD_PROJECT": "no",
    }
    result = build(load(cfg_dir), "rank", environ)
    assert result.env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert result.env["AWS_REGION"] == "us-east-1" and result.env["AWS_PROFILE"] == "p"
    assert "AWSX" not in result.env and "GOOGLE_CLOUD_PROJECT" not in result.env
    assert {"AWS_REGION", "AWS_PROFILE", "CLAUDE_CODE_USE_BEDROCK"} <= set(result.auth_names)


def test_vertex_passes_google_and_vertex_variables(cfg_dir: Path) -> None:
    environ = {
        **CALLER,
        "CLAUDE_CODE_USE_VERTEX": "1",
        "GOOGLE_APPLICATION_CREDENTIALS": "/c.json",
        "CLOUD_ML_REGION": "r",
        "ANTHROPIC_VERTEX_PROJECT_ID": "p",
        "AWS_REGION": "no",
    }
    env = env_of(load(cfg_dir), "rank", environ)
    assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/c.json"
    assert env["CLOUD_ML_REGION"] == "r" and env["ANTHROPIC_VERTEX_PROJECT_ID"] == "p"
    assert "AWS_REGION" not in env


def test_empty_provider_flag_is_not_set(cfg_dir: Path) -> None:
    environ = {
        **CALLER,
        "CLAUDE_CODE_USE_BEDROCK": "",
        "CLAUDE_CODE_USE_VERTEX": "",
        "AWS_REGION": "x",
        "CLOUD_ML_REGION": "r",
    }
    env = env_of(load(cfg_dir), "rank", environ)
    assert "AWS_REGION" not in env and "CLOUD_ML_REGION" not in env


def test_config_claude_auth_env_extends_builtin_list_by_exact_name(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "claude_auth_env: [MY_PROXY_TOKEN, OTHER]\n")
    environ = {**CALLER, "MY_PROXY_TOKEN": "t", "OTHER_X": "o", "ANTHROPIC_API_KEY": "k"}
    result = build(cfg, "rank", environ)
    assert result.env["MY_PROXY_TOKEN"] == "t" and result.env["ANTHROPIC_API_KEY"] == "k"
    assert "OTHER_X" not in result.env
    assert set(result.auth_names) == {"MY_PROXY_TOKEN", "ANTHROPIC_API_KEY"}


@pytest.mark.parametrize("key", ["claude_auth_env", "secret_env"])
@pytest.mark.parametrize("entry", ["OTHER_*", "TOK ", "A=B", ""])
def test_name_lists_refuse_globs_and_invalid_names(cfg_dir: Path, key: str, entry: str) -> None:
    with pytest.raises(UsageError) as info:
        load(cfg_dir, f"{key}: [{entry!r}]\n")
    assert f"{key}.0" in str(info.value.detail)
    assert "config.yaml" in str(info.value.detail)


# R4: a provider is selected only by a true flag value as passed.


@pytest.mark.parametrize("value", ["0", "false", "", "no", "off", "  ", "2", "False "])
def test_false_provider_flags_select_nothing(cfg_dir: Path, value: str) -> None:
    environ = {
        **CALLER,
        "CLAUDE_CODE_USE_BEDROCK": value,
        "CLAUDE_CODE_USE_VERTEX": value,
        "AWS_SECRET_ACCESS_KEY": SECRET,
        "GOOGLE_APPLICATION_CREDENTIALS": "/c.json",
        "CLOUD_ML_REGION": "r",
    }
    for kind, flags in (("rank", None), ("implement", {"reads_third_party": True})):
        result = build(load(cfg_dir), kind, environ, flags=flags)
        assert "AWS_SECRET_ACCESS_KEY" not in result.env
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in result.env
        assert "CLOUD_ML_REGION" not in result.env


@pytest.mark.parametrize("value", ["1", "TRUE", " 1 ", "yes", "On", "true\n"])
def test_true_provider_flags_select_the_provider(cfg_dir: Path, value: str) -> None:
    environ = {
        **CALLER,
        "CLAUDE_CODE_USE_BEDROCK": value,
        "CLAUDE_CODE_USE_VERTEX": value,
        "AWS_REGION": "us-east-1",
        "CLOUD_ML_REGION": "r",
    }
    result = build(load(cfg_dir), "rank", environ)
    assert result.env["AWS_REGION"] == "us-east-1" and result.env["CLOUD_ML_REGION"] == "r"
    assert {"AWS_REGION", "CLOUD_ML_REGION"} <= set(result.auth_names)


def test_provider_flag_in_secret_env_selects_nothing(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "secret_env: [CLAUDE_CODE_USE_BEDROCK, CLAUDE_CODE_USE_VERTEX]\n")
    environ = {
        **CALLER,
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "AWS_REGION": "us-east-1",
        "CLOUD_ML_REGION": "r",
    }
    env = env_of(cfg, "rank", environ)
    assert "CLAUDE_CODE_USE_BEDROCK" not in env and "AWS_REGION" not in env
    assert "CLAUDE_CODE_USE_VERTEX" not in env and "CLOUD_ML_REGION" not in env


def test_provider_flag_unlocked_from_secret_env_selects(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        secret_env: [CLAUDE_CODE_USE_BEDROCK]
        kinds:
          rank:
            env: {inherit: false, allow: [CLAUDE_CODE_USE_BEDROCK], set: {}, path_prepend: []}
        """,
    )
    environ = {**CALLER, "CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1"}
    assert env_of(cfg, "rank", environ)["AWS_REGION"] == "us-east-1"


@pytest.mark.parametrize("kind", ["implement", "rank", "review"])
def test_provider_flag_false_in_env_set_selects_nothing(cfg_dir: Path, kind: str) -> None:
    # The flag is read as passed: env.set "0" overrides the caller's "1".
    cfg = load(
        cfg_dir,
        f"""
        kinds:
          {kind}:
            env:
              set: {{CLAUDE_CODE_USE_BEDROCK: "0", CLAUDE_CODE_USE_VERTEX: "off"}}
        """,
    )
    environ = {
        **CALLER,
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "AWS_REGION": "us-east-1",
        "CLOUD_ML_REGION": "r",
    }
    result = build(cfg, kind, environ)
    assert result.env["CLAUDE_CODE_USE_BEDROCK"] == "0"
    assert result.env["CLAUDE_CODE_USE_VERTEX"] == "off"
    assert "AWS_REGION" not in result.auth_names and "CLOUD_ML_REGION" not in result.auth_names
    if kind != "implement":  # inherit mode passes them, but not as auth variables
        assert "AWS_REGION" not in result.env and "CLOUD_ML_REGION" not in result.env


@pytest.mark.parametrize("kind", ["implement", "rank", "review"])
def test_provider_flag_true_in_env_set_selects(cfg_dir: Path, kind: str) -> None:
    # The caller has no flag; env.set's " TRUE " (trimmed, lowercased) selects both providers.
    cfg = load(
        cfg_dir,
        f"""
        kinds:
          {kind}:
            env:
              set: {{CLAUDE_CODE_USE_BEDROCK: " TRUE ", CLAUDE_CODE_USE_VERTEX: "yes"}}
        """,
    )
    environ = {**CALLER, "AWS_REGION": "us-east-1", "CLOUD_ML_REGION": "r"}
    result = build(cfg, kind, environ)
    assert result.env["AWS_REGION"] == "us-east-1" and result.env["CLOUD_ML_REGION"] == "r"
    assert {"AWS_REGION", "CLOUD_ML_REGION"} <= set(result.auth_names)


def test_provider_flag_in_per_repo_env_set_is_read(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        repos:
          github.com/o/r:
            kinds:
              review:
                env:
                  set: {CLAUDE_CODE_USE_BEDROCK: "1"}
        """,
    )
    environ = {**CALLER, "AWS_REGION": "us-east-1"}
    assert build(cfg, "review", environ, repo="github.com/o/r").env["AWS_REGION"] == "us-east-1"
    assert "AWS_REGION" not in build(cfg, "review", environ).env


def test_stripped_provider_flag_set_by_env_set_selects(cfg_dir: Path) -> None:
    # secret_env strips the caller's flag, but env.set's value is what the session gets.
    cfg = load(
        cfg_dir,
        """
        secret_env: [CLAUDE_CODE_USE_BEDROCK]
        kinds:
          rank:
            env: {set: {CLAUDE_CODE_USE_BEDROCK: "1"}}
        """,
    )
    environ = {**CALLER, "AWS_REGION": "us-east-1"}
    assert env_of(cfg, "rank", environ)["AWS_REGION"] == "us-east-1"


# R10: auth_names come from the final environment.


def test_auth_names_include_auth_variables_from_env_set(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        claude_auth_env: [MY_PROXY_TOKEN]
        kinds:
          rank:
            env:
              inherit: false
              allow: []
              set: {ANTHROPIC_BASE_URL: "https://proxy", MY_PROXY_TOKEN: "proxy-token-1"}
              path_prepend: []
        """,
    )
    result = build(cfg, "rank", CALLER)
    assert set(result.auth_names) == {"ANTHROPIC_BASE_URL", "MY_PROXY_TOKEN"}


def test_auth_names_exclude_unselected_provider_globs(cfg_dir: Path) -> None:
    environ = {**CALLER, "AWS_REGION": "x", "ANTHROPIC_API_KEY": "k"}
    result = build(load(cfg_dir), "implement", environ)
    assert result.env["AWS_REGION"] == "x"  # inherited, but not an auth variable
    assert set(result.auth_names) == {"ANTHROPIC_API_KEY"}


# 3. secret_env


def test_secret_env_is_stripped_in_inherit_mode(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "secret_env: [WEBHOOK_URL]\n")
    result = build(cfg, "implement", {**CALLER, "WEBHOOK_URL": SECRET})
    assert "WEBHOOK_URL" not in result.env
    assert SECRET not in result.env.values()
    assert result.secret_names == ("WEBHOOK_URL",)


def test_secret_env_names_only_those_present(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "secret_env: [WEBHOOK_URL, TICKET_TOKEN]\n")
    result = build(cfg, "implement", {**CALLER, "TICKET_TOKEN": SECRET})
    assert result.secret_names == ("TICKET_TOKEN",)


def test_secret_env_beats_claude_auth_env(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "secret_env: [ANTHROPIC_API_KEY]\n")
    result = build(cfg, "rank", {**CALLER, "ANTHROPIC_API_KEY": SECRET})
    assert "ANTHROPIC_API_KEY" not in result.env
    assert "ANTHROPIC_API_KEY" not in result.auth_names


def test_secret_env_unlocked_by_exact_config_env_allow(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        secret_env: [WEBHOOK_URL]
        kinds:
          implement:
            env: {inherit: true, allow: [WEBHOOK_URL], set: {}, path_prepend: []}
          rank:
            env: {inherit: false, allow: [WEBHOOK_URL], set: {}, path_prepend: []}
        """,
    )
    for kind in ("implement", "rank"):
        assert env_of(cfg, kind, {**CALLER, "WEBHOOK_URL": SECRET})["WEBHOOK_URL"] == SECRET


def test_secret_env_unlocked_by_per_repo_env_allow(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        secret_env: [WEBHOOK_URL]
        repos:
          example.com/group/project:
            kinds:
              implement:
                env: {allow: [WEBHOOK_URL]}
        """,
    )
    environ = {**CALLER, "WEBHOOK_URL": SECRET}
    assert env_of(cfg, "implement", environ, repo="example.com/group/project")["WEBHOOK_URL"]
    assert "WEBHOOK_URL" not in env_of(cfg, "implement", environ, repo="example.com/other/x")


def test_secret_env_not_unlocked_by_config_glob(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        secret_env: [WEBHOOK_URL]
        kinds:
          rank:
            env: {inherit: false, allow: [WEBHOOK_*], set: {}, path_prepend: []}
        """,
    )
    env = env_of(cfg, "rank", {**CALLER, "WEBHOOK_URL": SECRET, "WEBHOOK_ID": "1"})
    assert "WEBHOOK_URL" not in env
    assert env["WEBHOOK_ID"] == "1"


@pytest.mark.parametrize("kind", ["implement", "rank"])
def test_env_allow_flag_never_unlocks_secret(cfg_dir: Path, kind: str) -> None:
    cfg = load(cfg_dir, "secret_env: [WEBHOOK_URL]\n")
    env = env_of(cfg, kind, {**CALLER, "WEBHOOK_URL": SECRET}, flags={"env_allow": ["WEBHOOK_URL"]})
    assert "WEBHOOK_URL" not in env


def test_env_allowlist_only_flag_never_unlocks_secret(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "secret_env: [WEBHOOK_URL]\n")
    flags = {"env_allowlist_only": ["WEBHOOK_URL"]}
    env = env_of(cfg, "implement", {**CALLER, "WEBHOOK_URL": SECRET}, flags=flags)
    assert "WEBHOOK_URL" not in env


def test_carried_env_allow_never_unlocks_secret(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "secret_env: [WEBHOOK_URL]\n")
    previous = kinds.Previous(repo=None, flags={"env_allow": ["WEBHOOK_URL"]})
    resolution = kinds.resolve(cfg, "rank", mode="headless", previous=previous)
    result = envbuild.build(
        resolution,
        cfg,
        {**CALLER, "WEBHOOK_URL": SECRET},
        csession_id=SID,
        csession_dir=SDIR,
        cwd=CWD,
        exe_dir=EXE_DIR,
    )
    assert "WEBHOOK_URL" not in result.env


def test_allowlist_only_mode_disables_config_unlock(cfg_dir: Path) -> None:
    # --env-allowlist-only replaces the kind's env.allow, so nothing is unlocked by it even
    # when the flag names the variable (conservative reading; see envbuild docstring).
    cfg = load(
        cfg_dir,
        """
        secret_env: [WEBHOOK_URL]
        kinds:
          implement:
            env: {inherit: true, allow: [WEBHOOK_URL], set: {}, path_prepend: []}
        """,
    )
    flags = {"env_allowlist_only": ["WEBHOOK_URL"]}
    env = env_of(cfg, "implement", {**CALLER, "WEBHOOK_URL": SECRET}, flags=flags)
    assert "WEBHOOK_URL" not in env


def test_secret_env_set_by_config_env_set_is_kept(cfg_dir: Path) -> None:
    # env.set is applied after stripping: an explicit config value is the user's choice.
    cfg = load(
        cfg_dir,
        """
        secret_env: [WEBHOOK_URL]
        kinds:
          implement:
            env: {inherit: true, allow: [], set: {WEBHOOK_URL: "https://hook"}, path_prepend: []}
        """,
    )
    env = env_of(cfg, "implement", {**CALLER, "WEBHOOK_URL": SECRET})
    assert env["WEBHOOK_URL"] == "https://hook"


# 4. Third-party-reading runs

THIRD_PARTY_NAMES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITLAB_TOKEN",
    "GL_TOKEN",
    "TMUX",
    "TMUX_PANE",
    "SSH_AUTH_SOCK",
)


def third_party_environ() -> dict[str, str]:
    return {**CALLER, **{name: "v" for name in THIRD_PARTY_NAMES}, "GH_CONFIG_DIR": "/gh"}


@pytest.mark.parametrize("kind", ["review", "gate-check", "respond"])
def test_third_party_kinds_drop_tokens_and_empty_gh_config(cfg_dir: Path, kind: str) -> None:
    cfg = load(
        cfg_dir,
        f"""
        kinds:
          {kind}:
            env: {{inherit: true, allow: [], set: {{}}, path_prepend: []}}
        """,
    )
    env = env_of(cfg, kind, third_party_environ())
    for name in THIRD_PARTY_NAMES:
        assert name not in env
    assert env["GH_CONFIG_DIR"] == f"{SDIR}/gh-empty"
    assert env["GLAB_CONFIG_DIR"] == f"{SDIR}/gh-empty"


def test_reads_third_party_flag_applies_extras(cfg_dir: Path) -> None:
    flags = {"reads_third_party": True, "env_allowlist_only": ["GH_TOKEN"]}
    env = env_of(load(cfg_dir), "implement", third_party_environ(), flags=flags)
    assert "GH_TOKEN" not in env
    assert env["GH_CONFIG_DIR"] == f"{SDIR}/gh-empty"


def test_trusted_kinds_keep_tokens(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir), "implement", third_party_environ())
    for name in THIRD_PARTY_NAMES:
        assert env[name] == "v"
    assert env["GH_CONFIG_DIR"] == "/gh"
    assert "GLAB_CONFIG_DIR" not in env


def test_third_party_drop_applies_to_config_env_allow(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          review:
            env: {inherit: false, allow: [GH_TOKEN, SSH_AUTH_SOCK], set: {}, path_prepend: []}
        """,
    )
    env = env_of(cfg, "review", third_party_environ())
    assert "GH_TOKEN" not in env and "SSH_AUTH_SOCK" not in env


THIRD_PARTY_SET = """
kinds:
  review:
    env:
      inherit: false
      allow: []
      set: {GH_TOKEN: lit-token, GH_ENTERPRISE_TOKEN: e, GH_CONFIG_DIR: /gh, GLAB_CONFIG_DIR: /gl,
            SSH_AUTH_SOCK: /s, WEBHOOK_URL: "https://hook"}
      path_prepend: []
  implement:
    env:
      inherit: true
      allow: []
      set: {GH_TOKEN: lit-token, GLAB_CONFIG_DIR: /gl}
      path_prepend: []
secret_env: [WEBHOOK_URL]
"""


@pytest.mark.parametrize(
    ("kind", "flags"), [("review", None), ("implement", {"reads_third_party": True})]
)
def test_third_party_removals_win_over_env_set(
    cfg_dir: Path, kind: str, flags: dict[str, Any] | None
) -> None:
    env = env_of(load(cfg_dir, THIRD_PARTY_SET), kind, third_party_environ(), flags=flags)
    for name in THIRD_PARTY_NAMES:
        assert name not in env
    assert env["GH_CONFIG_DIR"] == f"{SDIR}/gh-empty"
    assert env["GLAB_CONFIG_DIR"] == f"{SDIR}/gh-empty"


def test_env_set_secret_name_is_kept_on_third_party_runs(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir, THIRD_PARTY_SET), "review", {**CALLER, "WEBHOOK_URL": SECRET})
    assert env["WEBHOOK_URL"] == "https://hook"


def test_trusted_runs_keep_env_set_tokens(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir, THIRD_PARTY_SET), "implement", third_party_environ())
    assert env["GH_TOKEN"] == "lit-token" and env["GLAB_CONFIG_DIR"] == "/gl"


# 5. env.set, path_prepend, the executable directory


def test_respond_env_set_is_expanded(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir), "respond")
    assert env["GIT_CONFIG_GLOBAL"] == f"{SDIR}/empty-gitconfig"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_env_set_expands_the_four_variables(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          implement:
            env:
              inherit: true
              allow: []
              set:
                A: "${CSESSION_DIR}/x"
                B: "${CWD}:${HOME}:${GIT_DIR}"
                C: "plain $HOME $ } {x} ${HOME}}"
                D: "${CWD}${CWD}"
              path_prepend: []
        """,
    )
    env = env_of(cfg, "implement", git_dir="/work/repo/.git")
    assert env["A"] == f"{SDIR}/x"
    assert env["B"] == f"{CWD}:{CALLER['HOME']}:/work/repo/.git"
    assert env["C"] == f"plain $HOME $ }} {{x}} {CALLER['HOME']}}}"
    assert env["D"] == CWD + CWD


def test_env_set_git_dir_without_git_dir_expands_empty(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          implement:
            env: {inherit: true, allow: [], set: {G: "[${GIT_DIR}]"}, path_prepend: []}
        """,
    )
    assert env_of(cfg, "implement")["G"] == "[]"


def test_env_set_expansion_is_single_pass(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          implement:
            env: {inherit: true, allow: [], set: {X: "${CWD}"}, path_prepend: []}
        """,
    )
    env = env_of(cfg, "implement", cwd="/w/${HOME}")
    assert env["X"] == "/w/${HOME}"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("$${HOME}{", "$/home/user{"), ("${HOME}${", None), ("$}{${CWD}}", "$}{/work/repo}")],
)
def test_env_set_literal_dollar_and_braces_around_references(
    cfg_dir: Path, value: str, expected: str | None
) -> None:
    # Every '${' must start an allowed reference; a lone '$', '{' or '}' is literal, and the
    # single-pass expansion never forms a new reference from its result.
    text = f"""
        kinds:
          implement:
            env: {{inherit: true, allow: [], set: {{X: "{value}"}}, path_prepend: []}}
        """
    if expected is None:
        with pytest.raises(UsageError):
            load(cfg_dir, text)
        with pytest.raises(UsageError):
            build_unvalidated(load(cfg_dir), set={"X": value})
        return
    assert env_of(load(cfg_dir, text), "implement")["X"] == expected
    assert build_unvalidated(load(cfg_dir), set={"X": value}).env["X"] == expected


@pytest.mark.parametrize("ref", ["${USER}", "${${HOME}}", "${HOME", "${}"])
def test_env_set_bad_reference_is_refused_at_load(cfg_dir: Path, ref: str) -> None:
    with pytest.raises(UsageError) as info:
        load(
            cfg_dir,
            f"""
            kinds:
              implement:
                env: {{inherit: true, allow: [], set: {{TOKEN: "{SECRET}{ref}"}}, path_prepend: []}}
            """,
        )
    detail = str(info.value.detail)
    assert "kinds.implement.env.set.TOKEN" in detail and "config.yaml" in detail
    assert SECRET not in detail and SECRET not in str(info.value)


@pytest.mark.parametrize("ref", ["${USER}", "${${HOME}}", "${HOME"])
def test_env_set_bad_reference_backstop(cfg_dir: Path, ref: str) -> None:
    with pytest.raises(UsageError) as info:
        build_unvalidated(load(cfg_dir), set={"TOKEN": SECRET + ref})
    assert "env.set.TOKEN" in str(info.value)
    assert SECRET not in str(info.value)


@pytest.mark.parametrize(
    "key", ["", "A=SENTINEL", "1SENTINEL", "SENTINEL B", "SENTINEL\x00", "SENTINEL\n\x1b"]
)
@pytest.mark.parametrize("value", ["v", "${USER}"])
def test_env_set_invalid_name_is_refused(cfg_dir: Path, key: str, value: str) -> None:
    # Backstop; the name is checked (and never echoed) before the value is expanded.
    with pytest.raises(UsageError) as info:
        build_unvalidated(load(cfg_dir), set={key: value})
    assert "SENTINEL" not in str(info.value)
    assert "variable name" in str(info.value)


def test_env_set_nul_in_value_is_refused(cfg_dir: Path) -> None:
    with pytest.raises(UsageError, match="env.set.X"):
        build_unvalidated(load(cfg_dir), set={"X": "a\x00b"})
    with pytest.raises(UsageError) as info:
        load(
            cfg_dir,
            """
            kinds:
              implement:
                env: {inherit: true, allow: [], set: {X: "a\\0b"}, path_prepend: []}
            """,
        )
    assert "kinds.implement.env.set.X" in str(info.value.detail)


def test_env_set_non_ascii_name_is_refused(cfg_dir: Path) -> None:
    with pytest.raises(UsageError) as info:
        load(
            cfg_dir,
            """
            kinds:
              implement:
                env: {inherit: true, allow: [], set: {"UNICODE_\u00e9": x}, path_prepend: []}
            """,
        )
    assert "kinds.implement.env.set.<invalid variable name>" in str(info.value.detail)


def test_env_set_overrides_caller_values_with_unicode(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          implement:
            env: {inherit: true, allow: [], set: {EDITOR: "n\u00e4no \u2603"}, path_prepend: []}
        """,
    )
    assert env_of(cfg, "implement")["EDITOR"] == "n\u00e4no \u2603"


def test_path_is_exe_dir_then_path_prepend_then_kind_path(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          implement:
            env: {inherit: true, allow: [], set: {}, path_prepend: [/opt/a/bin, /opt/b/bin]}
        """,
    )
    env = env_of(cfg, "implement")
    assert path_of(env) == [EXE_DIR, "/opt/a/bin", "/opt/b/bin", *CALLER["PATH"].split(":")]


def test_path_from_env_set_is_the_kind_path(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          implement:
            env: {inherit: true, allow: [], set: {PATH: "/only/bin"}, path_prepend: [/p]}
        """,
    )
    assert path_of(env_of(cfg, "implement")) == [EXE_DIR, "/p", "/only/bin"]


@pytest.mark.parametrize("caller_path", [None, ""])
def test_missing_or_empty_path_adds_no_empty_entry(cfg_dir: Path, caller_path: str | None) -> None:
    environ = {k: v for k, v in CALLER.items() if k != "PATH"}
    if caller_path is not None:
        environ["PATH"] = caller_path
    env = env_of(load(cfg_dir), "implement", environ)
    assert env["PATH"] == EXE_DIR


@pytest.mark.parametrize("entry", ["", "relative/bin", "~/bin", "/a:/b", "/a\x00b"])
def test_path_prepend_bad_entries_are_refused(cfg_dir: Path, entry: str) -> None:
    with pytest.raises(UsageError, match="path_prepend"):
        build_unvalidated(load(cfg_dir), path_prepend=[entry])


@pytest.mark.parametrize("exe_dir", ["", "bin", "/a:/b"])
def test_exe_dir_must_be_a_single_absolute_path(cfg_dir: Path, exe_dir: str) -> None:
    with pytest.raises(ValueError):
        build(load(cfg_dir), "implement", exe_dir=exe_dir)


# 8. CSESSION_* and the private temporary directory


def test_inherited_csession_variables_are_cleared_and_set(cfg_dir: Path) -> None:
    environ = {
        **CALLER,
        "CSESSION_ID": "cs-aaaaaaaa",
        "CSESSION_DIR": "/elsewhere",
        "CSESSION_KIND": "implement",
        "CSESSION_HOOK": "pre-launch",
        "CSESSION_OBJECT_TYPE": "run",
        "CSESSION_DEFAULT_KIND": "review",
        "CSESSION_": "x",
    }
    env = env_of(load(cfg_dir), "implement", environ)
    assert {k: v for k, v in env.items() if k.startswith("CSESSION_")} == {
        "CSESSION_ID": SID,
        "CSESSION_DIR": SDIR,
        "CSESSION_KIND": "implement",
    }


def test_csession_variables_in_env_set_are_refused_at_load(cfg_dir: Path) -> None:
    with pytest.raises(UsageError) as info:
        load(
            cfg_dir,
            """
            kinds:
              implement:
                env: {inherit: true, allow: [], set: {CSESSION_POLL_INTERVAL: "1"}}
            """,
        )
    assert "kinds.implement.env.set.CSESSION_POLL_INTERVAL" in str(info.value.detail)


def test_csession_variables_from_env_set_are_cleared(cfg_dir: Path) -> None:
    # Backstop: a CSESSION_* name that bypassed config validation is still cleared (step 8).
    env = build_unvalidated(load(cfg_dir), set={"CSESSION_POLL_INTERVAL": "1"}).env
    assert "CSESSION_POLL_INTERVAL" not in env


def test_csession_kind_is_the_kind_as_given_on_fallback(cfg_dir: Path) -> None:
    env = env_of(load(cfg_dir), "my-new-kind")
    assert env["CSESSION_KIND"] == "my-new-kind"
    assert "EDITOR" not in env  # the review profile's allowlist mode


def test_tmpdir_is_set_only_when_given(cfg_dir: Path) -> None:
    environ = {**CALLER, "TMPDIR": "/tmp", "CLAUDE_CODE_TMPDIR": "/tmp/cc"}
    env = env_of(load(cfg_dir), "implement", environ)
    assert env["TMPDIR"] == "/tmp" and env["CLAUDE_CODE_TMPDIR"] == "/tmp/cc"
    env = env_of(load(cfg_dir), "review", environ, tmpdir="/tmp/csession-1000/cs-k3v7q2xa")
    assert env["TMPDIR"] == "/tmp/csession-1000/cs-k3v7q2xa"
    assert env["CLAUDE_CODE_TMPDIR"] == "/tmp/csession-1000/cs-k3v7q2xa"


def test_tmpdir_wins_over_env_set(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        """
        kinds:
          review:
            env: {inherit: false, allow: [], set: {TMPDIR: /shared}, path_prepend: []}
        """,
    )
    assert env_of(cfg, "review", tmpdir="/private")["TMPDIR"] == "/private"


# Hostile inputs


def test_huge_and_unicode_values_pass_through_unchanged(cfg_dir: Path) -> None:
    big = "v" * 1_000_000
    environ = {**CALLER, "BIG": big, "UNI": "\u00e9\u2603\U0001f600", "INJ": "$(rm -rf /); `x`"}
    env = env_of(load(cfg_dir), "implement", environ)
    assert env["BIG"] == big and env["UNI"] == environ["UNI"] and env["INJ"] == environ["INJ"]


def test_values_are_never_expanded_from_the_caller(cfg_dir: Path) -> None:
    environ = {**CALLER, "X": "${CSESSION_DIR}", "HOME": "${CWD}"}
    cfg = load(
        cfg_dir,
        """
        kinds:
          implement:
            env: {inherit: true, allow: [], set: {Y: "${HOME}"}, path_prepend: []}
        """,
    )
    env = env_of(cfg, "implement", environ)
    assert env["X"] == "${CSESSION_DIR}"
    assert env["Y"] == "${CWD}"


def test_repr_does_not_show_values(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "secret_env: [WEBHOOK_URL]\n")
    environ = {**CALLER, "ANTHROPIC_API_KEY": SECRET, "WEBHOOK_URL": "hook-value-123"}
    result = build(cfg, "implement", environ)
    assert result.env["ANTHROPIC_API_KEY"] == SECRET
    assert SECRET not in repr(result) and SECRET not in str(result)
    assert "ANTHROPIC_API_KEY" in repr(result)  # names are fine


@pytest.mark.parametrize("kind", ["implement", "rank"])
def test_unpassable_environ_entries_are_dropped(cfg_dir: Path, kind: str) -> None:
    environ = {
        **CALLER,
        "": "empty-name",
        "A=B": "eq",
        "LC_X=Y": "eq-glob",
        "A\x00B": "nul-name",
        "LC_NUL": "a\x00b",
        "ANTHROPIC_API_KEY": "k\x00ey",
        "GOOD": "ok",
    }
    result = build(load(cfg_dir), kind, environ)
    for name in ("", "A=B", "LC_X=Y", "A\x00B", "LC_NUL", "ANTHROPIC_API_KEY"):
        assert name not in result.env
    assert all("\x00" not in value for value in result.env.values())
    assert "ANTHROPIC_API_KEY" not in result.auth_names
    if kind == "implement":
        assert result.env["GOOD"] == "ok"


def test_repeated_builds_are_identical(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "secret_env: [WEBHOOK_URL]\n")
    environ = {**CALLER, "WEBHOOK_URL": SECRET, "ANTHROPIC_API_KEY": "k"}
    assert build(cfg, "review", environ) == build(cfg, "review", environ)


# secret_values: the contains_secret and redaction set (#26)

AUTH_PASSED = "auth-value-as-passed-123"
AUTH_CALLER = "auth-value-of-the-caller"
VAULT = "vault-token-0123456789"
RUN_SECRET = "ab" * 32


def session_env(env: dict[str, str], auth: tuple[str, ...], secret: tuple[str, ...]) -> Any:
    return envbuild.SessionEnv(env=env, auth_names=auth, secret_names=secret)


def test_secret_values_take_auth_from_the_session_env_and_secrets_from_the_caller() -> None:
    built = session_env(
        {"ANTHROPIC_API_KEY": AUTH_PASSED, "PLAIN": "literal-env-set-value"},
        ("ANTHROPIC_API_KEY",),
        ("VAULT_TOKEN",),
    )
    caller = {"ANTHROPIC_API_KEY": AUTH_CALLER, "VAULT_TOKEN": VAULT}
    values = envbuild.secret_values(built, caller, run_secret=RUN_SECRET)
    assert values == {AUTH_PASSED, VAULT, RUN_SECRET}


def test_secret_values_drop_short_and_absent_values() -> None:
    built = session_env({"ANTHROPIC_API_KEY": "short"}, ("ANTHROPIC_API_KEY",), ("GONE",))
    assert envbuild.secret_values(built, {}) == frozenset()
    assert envbuild.secret_values(built, {"GONE": "1234567"}) == frozenset()
    assert envbuild.secret_values(built, {"GONE": "12345678"}) == {"12345678"}


def test_secret_values_feed_an_index_that_catches_them(tmp_path: Path) -> None:
    built = session_env({"ANTHROPIC_API_KEY": AUTH_PASSED}, ("ANTHROPIC_API_KEY",), ("V",))
    values = envbuild.secret_values(built, {"V": VAULT}, run_secret=RUN_SECRET)
    index = secretindex.parse(secretindex.build(values))
    for value in (AUTH_PASSED, VAULT, RUN_SECRET):
        assert index.contains(f"x {value} y".encode())
    assert not index.contains(AUTH_CALLER.encode())
