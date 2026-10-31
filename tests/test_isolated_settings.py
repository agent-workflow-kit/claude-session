"""Generated settings for isolated runs (interface.md, Isolation and Generated settings;
design.md, Generated settings and Git directory (read-only))."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from claude_session import config, kinds, permission_profiles, settings_builder
from claude_session.errors import InvalidPermissionProfile, LaunchValidationError
from claude_session.settings_builder import Isolation, Launch, Paths, Tools

ROOT = Path(__file__).resolve().parent.parent
DRY_RUN_SCHEMA = ROOT / "schemas" / "cli" / "exec-dry-run.json"
AUDIT_SCHEMA = ROOT / "schemas" / "audit.json"
# Generated settings, not CLI output: kept apart from tests/golden (validated against schemas/cli).
GOLDEN = Path(__file__).resolve().parent / "golden_settings"

HOME = "/home/user"
STATE = f"{HOME}/.local/state/claude-session"
SID = "cs-abcd2345"
CSDIR = f"{STATE}/sessions/{SID}"
WORK = f"{CSDIR}/work"
PRIVATE = f"{STATE}/private"
TMP = f"/tmp/csession-1000/{SID}"
CS_PATH = "/opt/claude-session/bin/claude-session"
PYTHON = "/opt/claude-session/bin/python3"
CLAUDE = "/usr/local/bin/claude"
REPO = "/work/repo"
GIT_DIR = "/work/main/.git"
WT_GIT_DIR = "/work/main/.git/worktrees/repo"
PLUGIN = "/opt/plugins/review-kit"
# system_read_dirs() on a usr-merged system: each literal path, then its realpath.
SYSTEM = (
    "/usr",
    "/bin",
    "/usr/bin",
    "/sbin",
    "/usr/sbin",
    "/lib",
    "/usr/lib",
    "/lib64",
    "/usr/lib64",
    "/etc",
)
TOOL_DIRS = ("/usr/bin", "/usr/local/bin", "/opt/claude/versions")
PYTHON_DIRS = ("/opt/claude-session", "/opt/python/3.12")

PATHS = Paths(state_root=STATE, csession_id=SID, run=1)
TOOLS = Tools(interpreter=PYTHON, claude_session=CS_PATH, claude=CLAUDE)
ISOLATED_KINDS = ["review", "gate-check", "respond", "rank"]
THIRD_PARTY_KINDS = ["review", "gate-check", "respond"]
CLAUDE_CONFIG_FORMS = [
    ".claude/**",
    "**/.claude/**",
    ".mcp.json",
    "**/.mcp.json",
    "CLAUDE.md",
    "**/CLAUDE.md",
    "CLAUDE.local.md",
    "**/CLAUDE.local.md",
]
CLAUDE_CONFIG_TOP = [".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md"]
PROTECTED_CONCRETE = [
    "secret",
    "prompt.md",
    "prompts",
    "instructions.md",
    "run.json",
    "inputs",
    "gh-empty",
    "empty-gitconfig",
]
HOME_DENIES = [
    ".ssh/**",
    ".aws/**",
    ".gnupg/**",
    ".kube/**",
    ".docker/**",
    ".config/**",
    ".claude/**",
    ".netrc",
    ".git-credentials",
    ".local/state/**",
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


def resolve(cfg: config.Config, kind: str = "review", **flags: Any) -> kinds.Resolution:
    return kinds.resolve(cfg, kind, mode="headless", flags=flags or None, origin_url=lambda _: None)


def cwd_of(kind: str) -> str:
    return REPO if kind == "respond" else WORK


def isolation(**kw: Any) -> Isolation:
    kw.setdefault("home", HOME)
    kw.setdefault("tmpdir", TMP)
    kw.setdefault("system_dirs", SYSTEM)
    kw.setdefault("tool_dirs", TOOL_DIRS)
    kw.setdefault("python_dirs", PYTHON_DIRS)
    return Isolation(**kw)


def expanded_profile(
    cfg: config.Config, resolution: kinds.Resolution, cwd: str, git_dir: str | None = None
) -> dict[str, Any]:
    selected = permission_profiles.select(cfg, resolution, session_writable=lambda _: False)
    variables = {"CSESSION_DIR": CSDIR, "CWD": cwd, "HOME": HOME, "GIT_DIR": git_dir}
    return permission_profiles.expand(selected.content, variables)


def settings(
    cfg: config.Config,
    kind: str = "review",
    *,
    perm: Any = None,
    cwd: str | None = None,
    plugin_dirs: tuple[str, ...] = (),
    resolution: kinds.Resolution | None = None,
    **iso: Any,
) -> dict[str, Any]:
    resolution = resolution or resolve(cfg, kind)
    cwd = cwd or cwd_of(kind)
    if perm is None:
        perm = expanded_profile(cfg, resolution, cwd, iso.get("git_dir"))
    return settings_builder.build_settings(
        resolution,
        perm,
        PATHS,
        TOOLS,
        cwd=cwd,
        plugin_dirs=plugin_dirs,
        isolation=isolation(**iso),
    )


def sandbox_fs(data: dict[str, Any]) -> dict[str, list[str]]:
    return data["sandbox"]["filesystem"]


def end_reason(exc: pytest.ExceptionInfo[Any]) -> str:
    return exc.value.end_reason


# Isolation boundary


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_isolated_kinds_get_settings(cfg: config.Config, kind: str) -> None:
    data = settings(cfg, kind)
    assert set(data) == {"permissions", "sandbox", "hooks"}


def test_isolated_run_without_isolation_inputs_is_a_programming_error(cfg: config.Config) -> None:
    with pytest.raises(ValueError, match="isolation"):
        settings_builder.build_settings(resolve(cfg), {}, PATHS, TOOLS, cwd=WORK)


@pytest.mark.parametrize(
    "flags", [{"reads_third_party": True}, {"sandbox": "preferred"}, {"sandbox": "required"}]
)
def test_flags_make_a_trusted_kind_isolated(cfg: config.Config, flags: dict[str, Any]) -> None:
    data = settings(cfg, resolution=resolve(cfg, "implement", **flags), perm={}, cwd=REPO)
    assert data["permissions"]["defaultMode"] == "dontAsk"
    assert "sandbox" in data


def test_trusted_runs_ignore_isolation_inputs(cfg: config.Config) -> None:
    data = settings_builder.build_settings(
        resolve(cfg, "implement"), {}, PATHS, TOOLS, cwd=REPO, isolation=isolation()
    )
    assert set(data) == {"permissions", "hooks"}


# Permission mode and generated permission keys


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_permission_mode_is_dont_ask(cfg: config.Config, kind: str) -> None:
    assert settings(cfg, kind)["permissions"]["defaultMode"] == "dontAsk"


@pytest.mark.parametrize("mode", ["bypassPermissions", "acceptEdits", "default", "auto", "plan"])
def test_profile_mode_is_ignored_for_isolated_runs(cfg: config.Config, mode: str) -> None:
    data = settings(cfg, perm={"permissions": {"defaultMode": mode}})
    assert data["permissions"]["defaultMode"] == "dontAsk"


def test_profile_mode_must_still_be_valid(cfg: config.Config) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, perm={"permissions": {"defaultMode": "yolo"}})
    assert end_reason(info) == "invalid_permission_profile"


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_reads_are_blocked_outside_working_directories(cfg: config.Config, kind: str) -> None:
    assert settings(cfg, kind)["permissions"]["blockReadsOutsideWorkingDirectories"] is True


def test_profile_cannot_unblock_reads(cfg: config.Config) -> None:
    perm = {"permissions": {"blockReadsOutsideWorkingDirectories": False}}
    assert settings(cfg, perm=perm)["permissions"]["blockReadsOutsideWorkingDirectories"] is True


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_every_run_rules_are_kept(cfg: config.Config, kind: str) -> None:
    perms = settings(cfg, kind)["permissions"]
    assert "Bash(claude-session report:*)" in perms["allow"]
    assert f"Bash({CS_PATH} report:*)" in perms["allow"]
    assert f"Read(/{CSDIR}/**)" in perms["allow"]
    for tool in ("Edit", "Write"):
        assert f"{tool}(/{PRIVATE}/**)" in perms["deny"]
        for sub in ("artifacts", "outbox"):
            assert f"{tool}(/{CSDIR}/{sub}/**)" in perms["allow"]


def test_hooks_are_injected(cfg: config.Config) -> None:
    hooks = settings(cfg)["hooks"]
    assert set(hooks) == {
        "SessionStart",
        "UserPromptSubmit",
        "PermissionRequest",
        "Notification",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }


# Profile content


def test_respond_profile_rules_are_kept_and_expanded(cfg: config.Config) -> None:
    perms = settings(cfg, "respond")["permissions"]
    assert f"Edit(/{REPO}/**)" in perms["allow"]
    assert f"Write(/{REPO}/**)" in perms["allow"]
    assert "Bash(git push:*)" in perms["deny"]
    assert "WebFetch" in perms["deny"]


def test_profile_ask_rules_are_kept(cfg: config.Config) -> None:
    perms = settings(cfg, perm={"permissions": {"ask": ["Bash(make:*)"]}})["permissions"]
    assert perms["ask"] == ["Bash(make:*)"]


@pytest.mark.parametrize(
    "content",
    [
        {"sandbox": {"enabled": False}},
        {"sandbox": {"failIfUnavailable": False}},
        {"sandbox": {"allowUnsandboxedCommands": True}},
        {"sandbox": {"excludedCommands": ["git"]}},
        {"sandbox": {"autoAllowBashIfSandboxed": True}},
        {"sandbox": {"enableWeakerNestedSandbox": True}},
        {"sandbox": {"ignoreViolations": {"*": ["/"]}}},
        {"sandbox": {"network": {"allowUnixSockets": ["/var/run/docker.sock"]}}},
        {"sandbox": {"network": {"allowAllUnixSockets": True}}},
        {"sandbox": {"network": {"allowLocalBinding": True}}},
        {"sandbox": {"network": {"httpProxyPort": 8080}}},
        {"sandbox": {"network": {"socksProxyPort": 1080}}},
        {"sandbox": {"filesystem": {"disabled": True}}},
        {"sandbox": {"allowNestedUserNamespaces": True}},
        {"sandbox": {"allowAppleEvents": True}},
        {"sandbox": {"network": {"allowMachLookup": ["com.apple.x"]}}},
        {"sandbox": {"enableWeakerNetworkIsolation": True}},
        {"sandbox": {"network": {"strictAllowlist": False}}},
        {"permissions": {"additionalDirectories": ["/"]}},
    ],
)
def test_refused_profile_keys_fail_even_past_profile_selection(
    cfg: config.Config, content: dict[str, Any]
) -> None:
    with pytest.raises(InvalidPermissionProfile):
        settings(cfg, perm=content)


def test_auto_allow_false_in_a_profile_is_accepted(cfg: config.Config) -> None:
    data = settings(cfg, perm={"sandbox": {"autoAllowBashIfSandboxed": False}})
    assert data["sandbox"]["autoAllowBashIfSandboxed"] is False


def test_profile_input_is_not_mutated(cfg: config.Config) -> None:
    perm = {
        "permissions": {"allow": ["Bash(make:*)"], "deny": ["Bash(rm:*)"]},
        "sandbox": {"network": {"allowedDomains": ["example.org"]}, "filesystem": {}},
    }
    before = copy.deepcopy(perm)
    settings(cfg, perm=perm)
    settings(cfg, perm=perm)
    assert perm == before


def test_config_permission_profile_objects_are_accepted(cfg: config.Config) -> None:
    perm = config.PermissionProfile(permissions={"allow": ["Bash(make:*)"]})
    assert "Bash(make:*)" in settings(cfg, perm=perm)["permissions"]["allow"]


# Sandbox keys


@pytest.mark.parametrize(
    ("kind", "fail"), [("review", True), ("gate-check", True), ("respond", True), ("rank", False)]
)
def test_sandbox_enabled_and_fail_if_unavailable(cfg: config.Config, kind: str, fail: bool) -> None:
    sandbox = settings(cfg, kind)["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is fail


def test_preferred_sandbox_does_not_fail_if_unavailable(cfg: config.Config) -> None:
    sandbox = settings(cfg, resolution=resolve(cfg, "review", sandbox="preferred"))["sandbox"]
    assert sandbox["enabled"] is True and sandbox["failIfUnavailable"] is False


def test_sandbox_off_third_party_run_keeps_the_permission_layer(cfg: config.Config) -> None:
    data = settings(cfg, resolution=resolve(cfg, "review", sandbox="off"), tmpdir=None)
    sandbox = data["sandbox"]
    # Explicitly off: user or managed settings cannot enable a sandbox without the tmpdir.
    assert sandbox["enabled"] is False and "failIfUnavailable" not in sandbox
    assert sandbox["autoAllowBashIfSandboxed"] is False
    assert sandbox["allowUnsandboxedCommands"] is False
    assert data["permissions"]["defaultMode"] == "dontAsk"
    assert data["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert TMP not in sandbox_fs(data)["allowWrite"]


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_sandbox_never_auto_allows_bash_or_runs_unsandboxed(cfg: config.Config, kind: str) -> None:
    sandbox = settings(cfg, kind)["sandbox"]
    assert sandbox["autoAllowBashIfSandboxed"] is False
    assert sandbox["allowUnsandboxedCommands"] is False
    assert "excludedCommands" not in sandbox


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_sandbox_escape_keys_are_never_emitted(cfg: config.Config, kind: str) -> None:
    sandbox = settings(cfg, kind)["sandbox"]
    assert "ignoreViolations" not in sandbox
    for key in (
        "allowUnixSockets",
        "allowAllUnixSockets",
        "allowLocalBinding",
        "httpProxyPort",
        "socksProxyPort",
    ):
        assert key not in sandbox["network"]


@pytest.mark.parametrize(
    ("kind", "sandbox_mode"),
    [*((kind, None) for kind in ISOLATED_KINDS), *((kind, "off") for kind in THIRD_PARTY_KINDS)],
)
def test_sandbox_escape_keys_are_explicitly_false(
    cfg: config.Config, kind: str, sandbox_mode: str | None
) -> None:
    # --settings outranks user settings: a user setting cannot turn these on.
    resolution = resolve(cfg, kind, **({"sandbox": sandbox_mode} if sandbox_mode else {}))
    data = settings(cfg, kind, resolution=resolution, tmpdir=None if sandbox_mode else TMP)
    sandbox = data["sandbox"]
    for key in (
        "allowNestedUserNamespaces",
        "allowAppleEvents",
        "enableWeakerNestedSandbox",
        "enableWeakerNetworkIsolation",
    ):
        assert sandbox[key] is False
    assert sandbox["filesystem"]["disabled"] is False
    assert sandbox["network"]["strictAllowlist"] is True


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_unattended_modes_are_disabled(cfg: config.Config, kind: str) -> None:
    perm = {"permissions": {"disableAutoMode": "allow", "disableBypassPermissionsMode": "x"}}
    permissions = settings(cfg, kind, perm=perm)["permissions"]
    assert permissions["disableAutoMode"] == "disable"
    assert permissions["disableBypassPermissionsMode"] == "disable"


def test_trusted_runs_do_not_disable_modes(cfg: config.Config) -> None:
    data = settings_builder.build_settings(resolve(cfg, "implement"), {}, PATHS, TOOLS, cwd=REPO)
    assert "disableAutoMode" not in data["permissions"]
    assert "disableBypassPermissionsMode" not in data["permissions"]


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_network_allowlist_is_empty_by_default(cfg: config.Config, kind: str) -> None:
    assert settings(cfg, kind)["sandbox"]["network"]["allowedDomains"] == []


def test_profile_network_allowlist_is_kept(cfg: config.Config) -> None:
    perm = {"sandbox": {"network": {"allowedDomains": ["pypi.org"]}}}
    assert settings(cfg, perm=perm)["sandbox"]["network"]["allowedDomains"] == ["pypi.org"]


def test_sandboxed_run_needs_a_private_tmpdir(cfg: config.Config) -> None:
    with pytest.raises(ValueError, match="tmpdir"):
        settings(cfg, tmpdir=None)


# Deny-root reads and the allowRead list


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_deny_root_reads(cfg: config.Config, kind: str) -> None:
    assert sandbox_fs(settings(cfg, kind))["denyRead"] == ["/"]


def test_profile_deny_read_is_kept(cfg: config.Config) -> None:
    perm = {"sandbox": {"filesystem": {"denyRead": ["/srv/secret"]}}}
    assert sandbox_fs(settings(cfg, perm=perm))["denyRead"] == ["/srv/secret", "/"]


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_allow_read_list(cfg: config.Config, kind: str) -> None:
    allow_read = sandbox_fs(
        settings(
            cfg,
            kind,
            plugin_dirs=(PLUGIN,),
            read_dirs=("/opt/toolchain",),
            path_prepend=("/opt/extra/bin",),
            git_dir=GIT_DIR,
            worktree_git_dir=WT_GIT_DIR,
        )
    )["allowRead"]
    expected = [
        cwd_of(kind),
        CSDIR,
        *SYSTEM,
        *TOOL_DIRS,
        *PYTHON_DIRS,
        "/opt/toolchain",
        PLUGIN,
        "/opt/extra/bin",
        GIT_DIR,
        WT_GIT_DIR,
    ]
    assert allow_read == list(dict.fromkeys(expected))


def test_allow_read_has_no_duplicates(cfg: config.Config) -> None:
    allow_read = sandbox_fs(settings(cfg, read_dirs=("/usr", CSDIR), tool_dirs=("/usr/bin",)))[
        "allowRead"
    ]
    assert len(allow_read) == len(set(allow_read))


def test_allow_read_without_git_dir_has_no_git_entry(cfg: config.Config) -> None:
    allow_read = sandbox_fs(settings(cfg))["allowRead"]
    assert not any(".git" in entry for entry in allow_read)


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_tmpdir_is_writable_but_never_readable_by_allow_read(cfg: config.Config, kind: str) -> None:
    fs = sandbox_fs(settings(cfg, kind))
    assert TMP in fs["allowWrite"]
    assert not any(e == TMP or e.startswith(TMP + "/") for e in fs["allowRead"])


@pytest.mark.parametrize("entry", [TMP, f"{TMP}/sub"])
def test_read_dirs_inside_the_tmpdir_are_refused(cfg: config.Config, entry: str) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, read_dirs=(entry,))
    assert end_reason(info) == "invalid_permission_profile"


def test_profile_allow_read_inside_the_tmpdir_is_refused(cfg: config.Config) -> None:
    perm = {"sandbox": {"filesystem": {"allowRead": [TMP]}}}
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, perm=perm)
    assert end_reason(info) == "invalid_permission_profile"


@pytest.mark.parametrize("field", ["python_dirs", "tool_dirs", "system_dirs", "path_prepend"])
def test_generated_read_entries_inside_the_tmpdir_are_refused(
    cfg: config.Config, field: str
) -> None:
    with pytest.raises(LaunchValidationError) as info:
        iso: dict[str, Any] = {field: (f"{TMP}/x",)}
        settings(cfg, **iso)
    assert end_reason(info) == "unsafe_read_dir"


def test_profile_allow_read_is_kept(cfg: config.Config) -> None:
    perm = {"sandbox": {"filesystem": {"allowRead": ["/opt/data"]}}}
    assert "/opt/data" in sandbox_fs(settings(cfg, perm=perm))["allowRead"]


# allowWrite: the session-writable paths only


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_allow_write_list(cfg: config.Config, kind: str) -> None:
    allow_write = sandbox_fs(settings(cfg, kind, write_dirs=("/scratch/out",)))["allowWrite"]
    expected = [
        cwd_of(kind),
        f"{CSDIR}/outbox",
        f"{CSDIR}/artifacts",
        "/scratch/out",
        TMP,
    ]
    assert allow_write == list(dict.fromkeys(expected))


def test_work_dir_is_writable_only_when_it_is_the_cwd(cfg: config.Config) -> None:
    assert WORK in sandbox_fs(settings(cfg, "review"))["allowWrite"]
    assert WORK not in sandbox_fs(settings(cfg, "respond"))["allowWrite"]


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_allow_write_never_opens_state_private_git_or_home(cfg: config.Config, kind: str) -> None:
    allow_write = sandbox_fs(settings(cfg, kind, git_dir=GIT_DIR))["allowWrite"]
    for entry in allow_write:
        assert entry not in (CSDIR, STATE, PRIVATE, HOME, "/", GIT_DIR)
        assert not entry.startswith(PRIVATE)
        assert not entry.startswith(GIT_DIR)


def test_profile_allow_write_is_kept(cfg: config.Config) -> None:
    perm = {"sandbox": {"filesystem": {"allowWrite": ["/scratch/extra"]}}}
    assert "/scratch/extra" in sandbox_fs(settings(cfg, perm=perm))["allowWrite"]


# denyWrite and Edit/Write denies


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
@pytest.mark.parametrize("name", PROTECTED_CONCRETE)
def test_protected_session_files_are_sandbox_write_denied(
    cfg: config.Config, kind: str, name: str
) -> None:
    assert f"{CSDIR}/{name}" in sandbox_fs(settings(cfg, kind))["denyWrite"]


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_private_directory_is_sandbox_write_denied(cfg: config.Config, kind: str) -> None:
    assert PRIVATE in sandbox_fs(settings(cfg, kind))["denyWrite"]


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_deny_write_entries_are_concrete_paths(cfg: config.Config, kind: str) -> None:
    # Live C2: single-* globs in denyWrite are silently ignored, so none is ever emitted.
    data = settings(cfg, kind, plugin_dirs=(PLUGIN,), git_dir=GIT_DIR, worktree_git_dir=WT_GIT_DIR)
    for entry in sandbox_fs(data)["denyWrite"]:
        assert os.path.isabs(entry) and not any(c in entry for c in "*?[]{}"), entry


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_plugin_dirs_are_read_only(cfg: config.Config, kind: str) -> None:
    data = settings(cfg, kind, plugin_dirs=(PLUGIN, "/opt/plugins/other"))
    perms = data["permissions"]
    for plugin in (PLUGIN, "/opt/plugins/other"):
        assert plugin in sandbox_fs(data)["allowRead"]
        assert plugin in sandbox_fs(data)["denyWrite"]
        assert plugin not in sandbox_fs(data)["allowWrite"]
        assert f"Read(/{plugin}/**)" in perms["allow"]
        for tool in ("Edit", "Write"):
            assert f"{tool}(/{plugin}/**)" in perms["deny"]


def dot_git_denied(data: dict[str, Any], cwd: str) -> bool:
    deny = data["permissions"]["deny"]
    rules = [f"{tool}(/{cwd}/.git{tail})" for tool in ("Edit", "Write") for tail in ("", "/**")]
    in_rules = all(rule in deny for rule in rules)
    in_sandbox = f"{cwd}/.git" in sandbox_fs(data)["denyWrite"]
    assert in_rules == in_sandbox
    return in_rules


@pytest.mark.parametrize("kind", THIRD_PARTY_KINDS)
def test_cwd_dot_git_is_always_write_denied_on_third_party_runs(
    cfg: config.Config, kind: str
) -> None:
    for git_dir in (None, GIT_DIR):
        assert dot_git_denied(settings(cfg, kind, git_dir=git_dir), cwd_of(kind))


SANDBOXED_TRUSTED = [("rank", {}), ("implement", {"sandbox": "preferred"})]


@pytest.mark.parametrize(("kind", "flags"), SANDBOXED_TRUSTED)
def test_sandboxed_trusted_run_without_git_dir_denies_dot_git(
    cfg: config.Config, kind: str, flags: dict[str, Any]
) -> None:
    data = settings(cfg, resolution=resolve(cfg, kind, **flags), perm={}, cwd=REPO)
    assert dot_git_denied(data, REPO)


@pytest.mark.parametrize(("kind", "flags"), SANDBOXED_TRUSTED)
def test_sandboxed_trusted_run_with_git_dir_can_commit(
    cfg: config.Config, kind: str, flags: dict[str, Any]
) -> None:
    # design.md, Git directory: read-only only on third-party-reading runs (Q28).
    data = settings(
        cfg,
        resolution=resolve(cfg, kind, **flags),
        perm={},
        cwd=REPO,
        git_dir=GIT_DIR,
        worktree_git_dir=WT_GIT_DIR,
    )
    assert not dot_git_denied(data, REPO)
    for git_dir in (GIT_DIR, WT_GIT_DIR):
        assert git_dir not in sandbox_fs(data)["denyWrite"]
        assert not any(git_dir in rule for rule in data["permissions"]["deny"])


@pytest.mark.parametrize("kind", THIRD_PARTY_KINDS)
def test_git_directory_is_read_only(cfg: config.Config, kind: str) -> None:
    data = settings(cfg, kind, git_dir=GIT_DIR, worktree_git_dir=WT_GIT_DIR)
    fs = sandbox_fs(data)
    for git_dir in (GIT_DIR, WT_GIT_DIR):
        assert git_dir in fs["allowRead"]
        assert git_dir in fs["denyWrite"]
        for tool in ("Edit", "Write"):
            assert f"{tool}(/{git_dir}/**)" in data["permissions"]["deny"]


def test_main_checkout_git_dir_without_worktree_gitdir(cfg: config.Config) -> None:
    data = settings(cfg, "respond", git_dir=f"{REPO}/.git")
    fs = sandbox_fs(data)
    assert f"{REPO}/.git" in fs["allowRead"] and f"{REPO}/.git" in fs["denyWrite"]
    assert fs["denyWrite"].count(f"{REPO}/.git") == 1


def test_worktree_gitdir_needs_a_git_dir(cfg: config.Config) -> None:
    with pytest.raises(ValueError, match="worktree"):
        settings(cfg, worktree_git_dir=WT_GIT_DIR)


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_no_git_rules_without_a_git_dir(cfg: config.Config, kind: str) -> None:
    data = settings(cfg, kind)
    cwd_git = f"{cwd_of(kind)}/.git"
    for rule in data["permissions"]["deny"]:
        if "/.git/" in rule or rule.endswith("/.git)"):
            assert cwd_git in rule, rule


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
@pytest.mark.parametrize("form", CLAUDE_CONFIG_FORMS)
def test_claude_config_denied_under_the_session_dir(
    cfg: config.Config, kind: str, form: str
) -> None:
    deny = settings(cfg, kind)["permissions"]["deny"]
    for tool in ("Edit", "Write"):
        assert f"{tool}(/{CSDIR}/{form})" in deny


@pytest.mark.parametrize("form", CLAUDE_CONFIG_FORMS)
def test_claude_config_denied_under_every_plugin_dir(cfg: config.Config, form: str) -> None:
    deny = settings(cfg, plugin_dirs=(PLUGIN,))["permissions"]["deny"]
    for tool in ("Edit", "Write"):
        assert f"{tool}(/{PLUGIN}/{form})" in deny


@pytest.mark.parametrize("kind", THIRD_PARTY_KINDS)
@pytest.mark.parametrize("form", CLAUDE_CONFIG_FORMS)
def test_claude_project_config_denied_in_the_cwd(cfg: config.Config, kind: str, form: str) -> None:
    deny = settings(cfg, kind, cwd=REPO)["permissions"]["deny"]
    for tool in ("Edit", "Write"):
        assert f"{tool}(/{REPO}/{form})" in deny


@pytest.mark.parametrize("kind", THIRD_PARTY_KINDS)
@pytest.mark.parametrize("name", CLAUDE_CONFIG_TOP)
def test_claude_project_config_sandbox_denies_are_concrete(
    cfg: config.Config, kind: str, name: str
) -> None:
    fs = sandbox_fs(settings(cfg, kind, cwd=REPO))
    assert f"{REPO}/{name}" in fs["denyWrite"]
    assert f"{CSDIR}/{name}" in fs["denyWrite"]


def test_rank_does_not_deny_claude_config_in_a_caller_cwd(cfg: config.Config) -> None:
    data = settings(cfg, "rank", cwd=REPO)
    assert not any(f"/{REPO}/CLAUDE.md" in rule for rule in data["permissions"]["deny"])
    assert f"{REPO}/CLAUDE.md" not in sandbox_fs(data)["denyWrite"]


def test_reads_third_party_flag_adds_cwd_claude_config_denies(cfg: config.Config) -> None:
    resolution = resolve(cfg, "implement", reads_third_party=True)
    deny = settings(cfg, resolution=resolution, perm={}, cwd=REPO)["permissions"]["deny"]
    assert f"Edit(/{REPO}/**/CLAUDE.md)" in deny


# Home-directory Read denies


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
@pytest.mark.parametrize("target", [t for t in HOME_DENIES if t != ".local/state/**"])
def test_home_credential_reads_are_denied(cfg: config.Config, kind: str, target: str) -> None:
    assert f"Read(/{HOME}/{target})" in settings(cfg, kind)["permissions"]["deny"]


def test_private_directory_reads_are_denied(cfg: config.Config) -> None:
    assert f"Read(/{PRIVATE}/**)" in settings(cfg)["permissions"]["deny"]


def test_read_denies_containing_the_session_dir_are_not_generated(cfg: config.Config) -> None:
    # The default state root is under ~/.local/state: denying it would hide $CSESSION_DIR.
    deny = settings(cfg)["permissions"]["deny"]
    assert f"Read(/{HOME}/.local/state/**)" not in deny
    assert f"Read(/{STATE}/**)" not in deny
    for rule in deny:
        if rule.startswith("Read("):
            target = rule[len("Read(/") : -1].removesuffix("/**")
            assert not (CSDIR + "/").startswith(target.rstrip("/") + "/"), rule


def test_state_root_read_deny_when_it_does_not_contain_the_session(cfg: config.Config) -> None:
    other_home = "/home/other"
    deny = settings(cfg, home=other_home)["permissions"]["deny"]
    assert f"Read(/{other_home}/.local/state/**)" in deny


def test_read_deny_containing_the_cwd_is_not_generated(cfg: config.Config) -> None:
    cwd = f"{HOME}/.config/project"
    deny = settings(cfg, "rank", cwd=cwd)["permissions"]["deny"]
    assert f"Read(/{HOME}/.config/**)" not in deny
    assert f"Read(/{HOME}/.ssh/**)" in deny


def test_home_at_root_still_denies_credentials(cfg: config.Config) -> None:
    deny = settings(cfg, home="/")["permissions"]["deny"]
    assert "Read(//.ssh/**)" in deny


# Rule-safe inputs


@pytest.mark.parametrize("char", ["*", "?", "[", "]", "{", "}", "(", ")", "\\", "\n", "\x7f"])
def test_unsafe_cwd_is_refused(cfg: config.Config, char: str) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, "respond", perm={}, cwd=f"/work/re{char}po")
    assert end_reason(info) == "unsafe_cwd"


@pytest.mark.parametrize("cwd", ["relative/repo", "/work/../repo", "/work/repo/", "//work/repo"])
def test_unnormalized_cwd_is_refused(cfg: config.Config, cwd: str) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, "respond", perm={}, cwd=cwd)
    assert end_reason(info) == "unsafe_cwd"


def test_unsafe_plugin_dir_is_refused(cfg: config.Config) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, plugin_dirs=("/opt/plugins/x*",))
    assert end_reason(info) == "unsafe_plugin_dir"


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("read_dirs", "invalid_permission_profile"),
        ("write_dirs", "invalid_permission_profile"),
        ("python_dirs", "unsafe_read_dir"),
        ("tool_dirs", "unsafe_read_dir"),
        ("system_dirs", "unsafe_read_dir"),
        ("path_prepend", "unsafe_read_dir"),
    ],
)
@pytest.mark.parametrize("value", ["/opt/x*", "/opt/[x]", "relative", "/opt/x/../y", "/opt/\x00"])
def test_unsafe_list_entries_are_refused(
    cfg: config.Config, field: str, reason: str, value: str
) -> None:
    with pytest.raises(LaunchValidationError) as info:
        iso: dict[str, Any] = {field: (value,)}
        settings(cfg, **iso)
    assert end_reason(info) == reason


@pytest.mark.parametrize("field", ["home", "git_dir", "tmpdir"])
@pytest.mark.parametrize("value", ["", "relative", "/a/../b", "/a/*", "/a\nb"])
def test_unsafe_single_paths_are_refused(cfg: config.Config, field: str, value: str) -> None:
    with pytest.raises(LaunchValidationError) as info:
        iso: dict[str, Any] = {field: value}
        settings(cfg, perm={}, **iso)
    expected = {"tmpdir": "tmpdir_unsafe", "git_dir": "unsafe_read_dir", "home": "unsafe_path"}
    assert end_reason(info) == expected[field]


def test_paths_with_spaces_and_unicode_are_accepted(cfg: config.Config) -> None:
    cwd = "/work/my repo/zoë"
    data = settings(cfg, "respond", perm={}, cwd=cwd, read_dirs=("/opt/tool chain",))
    assert cwd in sandbox_fs(data)["allowRead"]
    assert f"Edit(/{cwd}/.git)" in data["permissions"]["deny"]


# Output shape


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_settings_match_the_dry_run_schema(cfg: config.Config, kind: str) -> None:
    schema = json.loads(DRY_RUN_SCHEMA.read_text(encoding="utf-8"))
    resolution = resolve(cfg, kind)
    perm = expanded_profile(cfg, resolution, cwd_of(kind))
    generated = settings_builder.build(
        resolution,
        perm,
        PATHS,
        TOOLS,
        Launch(mode="headless", cwd=cwd_of(kind), prompt_path=f"{CSDIR}/prompt.md"),
        isolation=isolation(),
    )
    Draft202012Validator(schema["properties"]["settings"]).validate(generated.settings)
    Draft202012Validator(schema["properties"]["claude_argv"]).validate(generated.argv)


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_settings_are_deterministic_json(cfg: config.Config, kind: str) -> None:
    first = settings_builder.settings_json(settings(cfg, kind))
    assert first == settings_builder.settings_json(settings(cfg, kind))
    assert json.loads(first) == settings(cfg, kind)


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_rule_paths_use_the_double_slash_absolute_form(cfg: config.Config, kind: str) -> None:
    perms = settings(cfg, kind, plugin_dirs=(PLUGIN,), git_dir=GIT_DIR)["permissions"]
    for rule in perms["allow"] + perms["deny"]:
        tool, _, rest = rule.partition("(")
        if tool in ("Read", "Edit", "Write") and rest:
            assert rest.startswith("//") and not rest.startswith("///"), rule


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_golden_settings(cfg: config.Config, kind: str) -> None:
    """The full generated settings of each isolated built-in kind, for review: a change here
    is a change to the documented isolation (interface.md, Generated settings)."""
    data = settings(
        cfg,
        kind,
        plugin_dirs=(PLUGIN,),
        git_dir=GIT_DIR if kind != "rank" else None,
        worktree_git_dir=WT_GIT_DIR if kind != "rank" else None,
        read_dirs=("/opt/toolchain",),
    )
    data.pop("hooks")  # covered by the trusted-run tests
    golden = GOLDEN / f"settings-{kind}.json"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    assert json.loads(golden.read_text(encoding="utf-8")) == data


# argv


@pytest.mark.parametrize("kind", THIRD_PARTY_KINDS)
def test_third_party_argv(cfg: config.Config, kind: str) -> None:
    built = settings_builder.build_argv(
        resolve(cfg, kind),
        PATHS,
        TOOLS,
        Launch(
            mode="headless",
            cwd=cwd_of(kind),
            prompt_path=f"{CSDIR}/prompt.md",
            plugin_dirs=(PLUGIN,),
        ),
    )
    argv = built.argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    add_dirs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"]
    assert add_dirs == [CSDIR, PLUGIN]
    assert argv[argv.index("--plugin-dir") + 1] == PLUGIN


def test_reads_third_party_flag_implies_strict_argv(cfg: config.Config) -> None:
    argv = settings_builder.build_argv(
        resolve(cfg, "implement", reads_third_party=True),
        PATHS,
        TOOLS,
        Launch(mode="headless", cwd=REPO, prompt_path=f"{CSDIR}/prompt.md"),
    ).argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv


# --reads-third-party implications and sandbox lowering


def test_reads_third_party_implications(cfg: config.Config) -> None:
    resolution = resolve(cfg, "implement", reads_third_party=True)
    profile = resolution.profile
    assert (profile.sandbox, profile.setting_sources, profile.mcp, profile.env.inherit) == (
        "required",
        [],
        "none",
        False,
    )
    for key in ("sandbox", "setting_sources", "mcp", "env.inherit"):
        assert resolution.origins[key] == "flag-implied"
    assert not resolution.sandbox_lowered()


@pytest.mark.parametrize(
    ("kind", "flags", "lowered"),
    [
        ("review", {}, False),
        ("gate-check", {}, False),
        ("respond", {}, False),
        ("rank", {}, False),  # preferred, but not third-party-reading
        ("rank", {"sandbox": "off"}, False),
        ("review", {"sandbox": "preferred"}, True),
        ("review", {"sandbox": "off"}, True),
        ("implement", {}, False),
        ("implement", {"reads_third_party": True}, False),
        ("implement", {"reads_third_party": True, "sandbox": "off"}, True),
        ("implement", {"reads_third_party": True, "sandbox": "preferred"}, True),
    ],
)
def test_sandbox_lowered(
    cfg: config.Config, kind: str, flags: dict[str, Any], lowered: bool
) -> None:
    resolution = resolve(cfg, kind, **flags)
    assert resolution.sandbox_lowered() is lowered
    warnings = settings_builder.isolation_warnings(resolution)
    assert any("sandbox_lowered" in warning for warning in warnings) is lowered


def test_sandbox_lowered_by_config(tmp_path: Path) -> None:
    cfg = load_cfg(tmp_path, 'kinds:\n  review:\n    sandbox: "off"\n')
    resolution = resolve(cfg, "review")
    assert resolution.origins["sandbox"] == "config"
    assert resolution.sandbox_lowered()


def test_sandbox_lowered_carried_from_the_previous_run(cfg: config.Config) -> None:
    previous = kinds.Previous(repo=None, flags={"sandbox": "off"})
    resolution = kinds.resolve(cfg, "review", mode="headless", previous=previous)
    assert resolution.sandbox_lowered()


def test_unsandboxed_third_party_run_warns(cfg: config.Config) -> None:
    warnings = settings_builder.isolation_warnings(resolve(cfg, "review", sandbox="off"))
    assert any("without a sandbox" in warning for warning in warnings)
    assert settings_builder.isolation_warnings(resolve(cfg, "review")) == ()


@pytest.mark.parametrize("to", ["preferred", "off"])
def test_sandbox_lowered_audit_record_validates(cfg: config.Config, to: str) -> None:
    resolution = resolve(cfg, "review", sandbox=to)
    inputs = settings_builder.sandbox_lowered_inputs(resolution, run=3)
    assert inputs == {"run_kind": "review", "run": 3, "sandbox_from": "required", "sandbox_to": to}
    record = {
        "ts": "2026-10-30T12:00:00Z",
        "actor": "human:user",
        "decision_id": [],
        "target": SID,
        "action": "sandbox_lowered",
        "inputs": inputs,
        "result": "ok",
    }
    schema = json.loads(AUDIT_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(record)


def test_sandbox_lowered_inputs_refuse_a_run_that_is_not_lowered(cfg: config.Config) -> None:
    with pytest.raises(ValueError):
        settings_builder.sandbox_lowered_inputs(resolve(cfg, "review"), run=1)


# Interpreter check (design.md, Security notes)


def writable_under(*roots: str) -> Any:
    def check(path: str) -> bool:
        return any(path == root or path.startswith(root + "/") for root in roots)

    return check


def test_interpreter_outside_writable_paths_passes(tmp_path: Path) -> None:
    settings_builder.check_interpreter(
        "/opt/venv/bin/python",
        session_writable=writable_under(str(tmp_path / "cwd")),
        writable_paths=(str(tmp_path / "cwd"),),
        prefixes=("/opt/venv", "/usr"),
        package_dir="/opt/venv/lib/python3.12/site-packages/claude_session",
        link_dirs=(),
    )


@pytest.mark.parametrize("where", ["interpreter", "realpath", "prefix", "package"])
def test_interpreter_inside_a_writable_path_is_refused(tmp_path: Path, where: str) -> None:
    cwd = tmp_path / "cwd"
    venv = cwd / ".venv"
    (venv / "bin").mkdir(parents=True)
    outside = tmp_path / "python3"
    outside.write_text("")
    inside_target = venv / "bin" / "python3.12"
    inside_target.write_text("")
    interpreter = str(tmp_path / "bin-python")
    prefixes: tuple[str, ...] = ("/opt/base",)
    package = "/opt/site/claude_session"
    if where == "interpreter":
        interpreter = str(venv / "bin" / "python")
        os.symlink(outside, interpreter)
    elif where == "realpath":
        os.symlink(inside_target, interpreter)
    else:
        os.symlink(outside, interpreter)
        if where == "prefix":
            prefixes = ("/opt/base", str(venv))
        else:
            package = str(cwd / "src" / "claude_session")
    with pytest.raises(LaunchValidationError) as info:
        settings_builder.check_interpreter(
            interpreter,
            session_writable=writable_under(str(cwd)),
            writable_paths=(str(cwd),),
            prefixes=prefixes,
            package_dir=package,
            sys_path=(),
            site_dirs=(),
            link_dirs=(),
        )
    assert end_reason(info) == "unsafe_path"


def test_interpreter_check_defaults_to_this_python() -> None:
    seen: list[str] = []

    def record(path: str) -> bool:
        seen.append(path)
        return False

    settings_builder.check_interpreter(
        sys.executable, session_writable=record, writable_paths=(), link_dirs=()
    )
    assert sys.prefix in seen and sys.exec_prefix in seen
    assert os.path.dirname(settings_builder.__file__) in seen


# Read-list inputs (these read the file system)


def test_system_read_dirs_skip_missing_and_resolve_symlinks(tmp_path: Path) -> None:
    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "etc").mkdir()
    (tmp_path / "bin").symlink_to(tmp_path / "usr" / "bin")
    (tmp_path / "sbin").symlink_to(tmp_path / "usr" / "bin")
    dirs = settings_builder.system_read_dirs(
        (
            str(tmp_path / "usr"),
            str(tmp_path / "bin"),
            str(tmp_path / "sbin"),
            str(tmp_path / "lib32"),
            str(tmp_path / "etc"),
        )
    )
    # A symlinked (usr-merged) system dir yields both its literal path and its target.
    assert dirs == (
        str(tmp_path / "usr"),
        str(tmp_path / "bin"),
        str(tmp_path / "usr" / "bin"),
        str(tmp_path / "sbin"),
        str(tmp_path / "etc"),
    )


def test_system_read_dirs_default_list() -> None:
    assert settings_builder.SYSTEM_READ_DIRS == (
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib32",
        "/lib64",
        "/etc",
    )
    assert "/opt" not in settings_builder.system_read_dirs()


def test_tool_dirs_are_the_resolved_directories(tmp_path: Path) -> None:
    real = tmp_path / "versions" / "1.0"
    real.mkdir(parents=True)
    (real / "claude").write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").symlink_to(real / "claude")
    (bindir / "git").write_text("")
    dirs = settings_builder.tool_dirs((str(bindir / "claude"), str(bindir / "git"), None))
    assert dirs == (str(real), str(bindir))


def python_layout(tmp_path: Path) -> dict[str, Any]:
    venv = tmp_path / "venv"
    base = tmp_path / "base"
    site = venv / "lib" / "site-packages"
    package = site / "claude_session"
    package.mkdir(parents=True)
    (base / "lib" / "python3.12").mkdir(parents=True)
    extra = tmp_path / "extra-path"
    extra.mkdir()
    caller = tmp_path / "caller"
    (caller / "src").mkdir(parents=True)
    return {
        "executable_dir": str(venv / "bin"),
        "package_dir": str(package),
        "prefixes": (str(venv), str(venv), str(base), str(base)),
        "sys_path": (
            "",
            "relative/dir",
            str(caller),
            str(caller / "src"),
            str(base / "lib" / "python3.12"),
            str(site),
            str(extra),
            str(tmp_path / "missing"),
        ),
        "user_site": None,
    }


def test_python_read_dirs(tmp_path: Path) -> None:
    layout = python_layout(tmp_path)
    (tmp_path / "venv" / "bin").mkdir()
    dirs = settings_builder.python_read_dirs(**layout)
    assert dirs == (
        str(tmp_path / "venv" / "bin"),
        str(tmp_path / "venv" / "lib" / "site-packages" / "claude_session"),
        str(tmp_path / "venv"),
        str(tmp_path / "base"),
        str(tmp_path / "caller"),
        str(tmp_path / "caller" / "src"),
        str(tmp_path / "extra-path"),
    )


def test_python_read_dirs_drop_empty_and_relative_entries(tmp_path: Path) -> None:
    dirs = settings_builder.python_read_dirs(**python_layout(tmp_path))
    assert "" not in dirs and "relative/dir" not in dirs
    assert all(os.path.isabs(d) for d in dirs)


def test_python_read_dirs_keep_entries_inside_the_caller_cwd(tmp_path: Path) -> None:
    # Not dropped silently: check_interpreter refuses one inside a session-writable path,
    # and #24's generated-entry checks refuse the caller's cwd.
    dirs = settings_builder.python_read_dirs(**python_layout(tmp_path))
    assert str(tmp_path / "caller" / "src") in dirs


def test_python_read_dirs_include_the_user_site_when_used(tmp_path: Path) -> None:
    layout = python_layout(tmp_path)
    user_site = tmp_path / "home" / ".local" / "lib" / "site-packages"
    user_site.mkdir(parents=True)
    layout["user_site"] = str(user_site)
    assert str(user_site) in settings_builder.python_read_dirs(**layout)


def test_python_read_dirs_fall_back_when_base_prefix_is_missing(tmp_path: Path) -> None:
    layout = python_layout(tmp_path)
    managed = tmp_path / "managed" / "cpython-3.12"
    (managed / "bin").mkdir(parents=True)
    (managed / "bin" / "python3.12").write_text("")
    (tmp_path / "venv" / "bin").mkdir()
    (tmp_path / "venv" / "bin" / "python").symlink_to(managed / "bin" / "python3.12")
    venv = str(tmp_path / "venv")
    missing = str(tmp_path / "gone")
    layout["prefixes"] = (venv, venv, missing, missing)
    dirs = settings_builder.python_read_dirs(**layout)
    assert str(managed) in dirs
    assert missing not in dirs


def test_python_read_dirs_resolve_symlinks(tmp_path: Path) -> None:
    layout = python_layout(tmp_path)
    link = tmp_path / "link-to-extra"
    link.symlink_to(tmp_path / "extra-path")
    layout["sys_path"] = (str(link),)
    dirs = settings_builder.python_read_dirs(**layout)
    assert str(tmp_path / "extra-path") in dirs and str(link) not in dirs


def test_python_read_dirs_defaults_to_this_interpreter() -> None:
    dirs = settings_builder.python_read_dirs()
    assert os.path.realpath(sys.prefix) in dirs
    assert os.path.realpath(os.path.dirname(settings_builder.__file__)) in dirs


# Setting sources (review round 1, R2)


@pytest.mark.parametrize("kind", THIRD_PARTY_KINDS)
@pytest.mark.parametrize("source", ["user", "project", "local"])
def test_third_party_runs_refuse_setting_sources(
    cfg: config.Config, kind: str, source: str
) -> None:
    resolution = resolve(cfg, kind, setting_sources=[source])
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, kind, resolution=resolution)
    assert end_reason(info) == "unsafe_setting_sources"


def test_reads_third_party_flag_with_explicit_setting_sources_is_refused(
    cfg: config.Config,
) -> None:
    resolution = resolve(cfg, "implement", reads_third_party=True, setting_sources=["user"])
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, resolution=resolution, perm={}, cwd=REPO)
    assert end_reason(info) == "unsafe_setting_sources"


@pytest.mark.parametrize("kind", THIRD_PARTY_KINDS)
def test_third_party_runs_without_setting_sources_pass(cfg: config.Config, kind: str) -> None:
    settings(cfg, kind, resolution=resolve(cfg, kind, setting_sources=[]))
    assert settings_builder.isolation_warnings(resolve(cfg, kind)) == ()


def test_sandboxed_trusted_run_with_setting_sources_warns(cfg: config.Config) -> None:
    resolution = resolve(cfg, "implement", sandbox="preferred", setting_sources=["user"])
    settings(cfg, resolution=resolution, perm={}, cwd=REPO)  # not refused
    warnings = settings_builder.isolation_warnings(resolution)
    assert any("setting sources" in warning and "merge" in warning for warning in warnings)


def test_sandboxed_trusted_run_without_setting_sources_does_not_warn(cfg: config.Config) -> None:
    resolution = resolve(cfg, "implement", sandbox="preferred", setting_sources=[])
    assert settings_builder.isolation_warnings(resolution) == ()


def test_trusted_run_with_setting_sources_does_not_warn(cfg: config.Config) -> None:
    resolution = resolve(cfg, "implement", setting_sources=["user", "project"])
    assert settings_builder.isolation_warnings(resolution) == ()


# Entries that contain the state root, the private root or the tmp parent (R3)


def test_cwd_containing_the_state_root_is_refused(cfg: config.Config) -> None:
    paths = Paths(state_root="/srv/me/state", csession_id=SID, run=1)
    with pytest.raises(LaunchValidationError) as info:
        settings_builder.build_settings(
            resolve(cfg, "respond"),
            {},
            paths,
            TOOLS,
            cwd="/srv/me",
            isolation=isolation(tmpdir=TMP),
        )
    assert end_reason(info) == "unsafe_cwd"


@pytest.mark.parametrize(
    ("iso", "reason"),
    [
        ({"read_dirs": ("/tmp",)}, "invalid_permission_profile"),
        ({"read_dirs": ("/tmp/csession-1000",)}, "invalid_permission_profile"),
        ({"read_dirs": (HOME,)}, "invalid_permission_profile"),
        ({"write_dirs": (f"{HOME}/.local",)}, "invalid_permission_profile"),
        ({"python_dirs": (HOME,)}, "unsafe_read_dir"),
        ({"python_dirs": ("/",)}, "unsafe_read_dir"),
        ({"tool_dirs": (f"{HOME}/.local/state",)}, "unsafe_read_dir"),
        ({"path_prepend": ("/tmp",)}, "unsafe_read_dir"),
        ({"system_dirs": (STATE,)}, "unsafe_read_dir"),
        ({"git_dir": PRIVATE}, "unsafe_read_dir"),
    ],
)
def test_entries_containing_the_state_root_or_tmp_parent_are_refused(
    cfg: config.Config, iso: dict[str, Any], reason: str
) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, **iso)
    assert end_reason(info) == reason


def test_plugin_dir_containing_the_state_root_is_refused(cfg: config.Config) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, plugin_dirs=(f"{HOME}/.local",))
    assert end_reason(info) == "unsafe_plugin_dir"


@pytest.mark.parametrize("key", ["allowRead", "allowWrite"])
@pytest.mark.parametrize("value", ["/", "/tmp", f"{HOME}/.local/state", PRIVATE])
def test_profile_entries_containing_the_state_root_are_refused(
    cfg: config.Config, key: str, value: str
) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, perm={"sandbox": {"filesystem": {key: [value]}}})
    assert end_reason(info) == "invalid_permission_profile"


def test_own_session_dir_entries_stay_allowed(cfg: config.Config) -> None:
    data = settings(cfg, read_dirs=(CSDIR,), write_dirs=(f"{CSDIR}/outbox/x",))
    assert CSDIR in sandbox_fs(data)["allowRead"]


# Rule-path end reasons and ordering (R12)


def test_bad_cwd_is_unsafe_cwd_before_any_profile_problem(cfg: config.Config) -> None:
    perm = {"sandbox": {"enabled": False}, "permissions": {"defaultMode": "yolo"}}
    with pytest.raises(LaunchValidationError) as info:
        settings(cfg, "respond", perm=perm, cwd="/work/re*po")
    assert end_reason(info) == "unsafe_cwd"


@pytest.mark.parametrize("cwd", ["/work/re*po", "relative", "/a/../b"])
def test_check_cwd(cwd: str) -> None:
    with pytest.raises(LaunchValidationError) as info:
        settings_builder.check_cwd(cwd)
    assert end_reason(info) == "unsafe_cwd"
    settings_builder.check_cwd("/work/my repo")


def test_bad_cwd_is_unsafe_cwd_even_when_the_profile_does_not_use_it(cfg: config.Config) -> None:
    # The launch path runs check_cwd before permission_profiles.expand: an unused ${CWD}
    # would never be checked there.
    content = {"permissions": {"allow": ["Bash(make:*)"]}}
    permission_profiles.expand(content, {"CWD": "/work/re*po"})  # unused: no failure
    with pytest.raises(LaunchValidationError) as info:
        settings_builder.check_cwd("/work/re*po")
    assert end_reason(info) == "unsafe_cwd"


# Interpreter check: sys.path, .pth, two-way containment, hard links (R4-R6)


def interpreter_layout(tmp_path: Path) -> dict[str, Any]:
    base = tmp_path / "base"
    (base / "lib" / "python3.12").mkdir(parents=True)
    venv = tmp_path / "venv"
    site_dir = venv / "lib" / "python3.12" / "site-packages"
    package = site_dir / "claude_session"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (venv / "bin").mkdir()
    python = venv / "bin" / "python"
    python.write_text("")
    return {
        "interpreter": str(python),
        "prefixes": (str(venv), str(venv), str(base), str(base)),
        "package_dir": str(package),
        "stdlib_dirs": (str(base / "lib" / "python3.12"),),
        "sys_path": (str(base / "lib" / "python3.12"), str(site_dir)),
        "site_dirs": (str(site_dir),),
        "link_dirs": (str(package), str(site_dir)),
    }


def check(layout: dict[str, Any], *writable: str) -> None:
    layout = dict(layout)
    interpreter = layout.pop("interpreter")
    settings_builder.check_interpreter(
        interpreter,
        session_writable=writable_under(*writable),
        writable_paths=writable,
        **layout,
    )


def refused_reason(layout: dict[str, Any], *writable: str) -> str:
    with pytest.raises(LaunchValidationError) as info:
        check(layout, *writable)
    return end_reason(info)


def test_interpreter_layout_passes(tmp_path: Path) -> None:
    (tmp_path / "cwd").mkdir()
    check(interpreter_layout(tmp_path), str(tmp_path / "cwd"))


def test_sys_path_entry_inside_a_writable_path_is_refused(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    cwd = tmp_path / "cwd"
    (cwd / "src").mkdir(parents=True)
    layout["sys_path"] = (*layout["sys_path"], str(cwd / "src"))
    assert refused_reason(layout, str(cwd)) == "unsafe_path"


def test_pth_entry_pointing_into_a_writable_path_is_refused(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    cwd = tmp_path / "cwd"
    (cwd / "src").mkdir(parents=True)
    site_dir = Path(layout["site_dirs"][0])
    (site_dir / "_editable.pth").write_text(f"# editable\nimport os\n{cwd / 'src'}\n")
    assert refused_reason(layout, str(cwd)) == "unsafe_path"


def test_relative_pth_entry_is_resolved_against_its_directory(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    site_dir = Path(layout["site_dirs"][0])
    (site_dir / "extra").mkdir()
    (site_dir / "rel.pth").write_text("extra\n")
    assert refused_reason(layout, str(site_dir / "extra")) == "unsafe_path"


def test_pth_import_lines_are_not_paths(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    site_dir = Path(layout["site_dirs"][0])
    (site_dir / "hook.pth").write_text(f"import sys; sys.path.append('{cwd}')\n")
    check(layout, str(cwd))


def test_writable_venv_site_packages_is_refused(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    site_dir = layout["site_dirs"][0]
    assert refused_reason(layout, site_dir) == "unsafe_path"


def test_writable_base_stdlib_dir_is_refused(tmp_path: Path) -> None:
    # Only the reverse containment of the stdlib dir catches this one (not in sys_path here).
    layout = interpreter_layout(tmp_path)
    stdlib = str(tmp_path / "base" / "lib" / "python3.12")
    layout["sys_path"] = ()
    with pytest.raises(LaunchValidationError) as info:
        settings_builder.check_interpreter(
            layout.pop("interpreter"),
            session_writable=lambda _path: False,
            writable_paths=(stdlib,),
            **layout,
        )
    assert end_reason(info) == "unsafe_path"
    assert "contains a session-writable path" in info.value.message


def test_writable_path_under_a_python_prefix_passes(tmp_path: Path) -> None:
    # V1: a cwd like /usr/src/app sits under the system prefix but outside every code dir.
    layout = interpreter_layout(tmp_path)
    app = tmp_path / "base" / "src" / "app"
    app.mkdir(parents=True)
    check(layout, str(app))


def test_writable_dir_inside_a_stdlib_dir_is_refused(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    layout["sys_path"] = ()
    inner = tmp_path / "base" / "lib" / "python3.12" / "json"
    inner.mkdir()
    with pytest.raises(LaunchValidationError) as info:
        settings_builder.check_interpreter(
            layout.pop("interpreter"),
            session_writable=lambda _path: False,
            writable_paths=(str(inner),),
            **layout,
        )
    assert end_reason(info) == "unsafe_path"


def test_writable_dir_inside_a_site_dir_is_refused(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    inner = Path(layout["site_dirs"][0]) / "somepkg"
    inner.mkdir()
    assert refused_reason(layout, str(inner)) == "unsafe_path"


def test_interpreter_check_requires_writable_paths() -> None:
    # V2: no default that would silently skip the reverse check.
    with pytest.raises(TypeError):
        settings_builder.check_interpreter(  # pyright: ignore[reportCallIssue]
            sys.executable, session_writable=lambda _path: False, link_dirs=()
        )


def test_interpreter_check_defaults_include_stdlib_dirs() -> None:
    import sysconfig

    seen: list[str] = []

    def record(path: str) -> bool:
        seen.append(path)
        return False

    settings_builder.check_interpreter(
        sys.executable, session_writable=record, writable_paths=(), link_dirs=()
    )
    assert sysconfig.get_paths()["stdlib"] in seen


def test_dangling_pth_symlink_is_skipped(tmp_path: Path) -> None:
    # V3: site.addpackage skips a .pth file it cannot open.
    layout = interpreter_layout(tmp_path)
    site_dir = Path(layout["site_dirs"][0])
    (site_dir / "gone.pth").symlink_to(tmp_path / "missing.pth")
    check(layout)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_unreadable_pth_is_skipped(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    site_dir = Path(layout["site_dirs"][0])
    pth = site_dir / "secret.pth"
    pth.write_text(f"{tmp_path / 'cwd'}\n")
    pth.chmod(0)
    try:
        check(layout, str(tmp_path / "cwd"))
    finally:
        pth.chmod(0o600)


def test_symlinked_pth_into_a_writable_dir_is_refused(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = cwd / "evil.pth"
    target.write_text("# nothing yet\n")
    site_dir = Path(layout["site_dirs"][0])
    (site_dir / "evil.pth").symlink_to(target)
    with pytest.raises(LaunchValidationError) as info:
        check(layout, str(cwd))
    assert end_reason(info) == "unsafe_path"
    assert ".pth" in info.value.message


def test_symlinked_readable_pth_entries_are_still_checked(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    cwd = tmp_path / "cwd"
    (cwd / "src").mkdir(parents=True)
    store = tmp_path / "store"
    store.mkdir()
    (store / "real.pth").write_text(f"{cwd / 'src'}\n")
    site_dir = Path(layout["site_dirs"][0])
    (site_dir / "linked.pth").symlink_to(store / "real.pth")
    assert refused_reason(layout, str(cwd)) == "unsafe_path"


def test_interpreter_check_defaults_include_base_prefixes_and_sys_path() -> None:
    seen: list[str] = []

    def record(path: str) -> bool:
        seen.append(path)
        return False

    settings_builder.check_interpreter(
        sys.executable, session_writable=record, writable_paths=(), link_dirs=()
    )
    for path in (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix):
        assert path in seen
    assert all(entry in seen for entry in sys.path if entry and os.path.isabs(entry))


def test_hard_linked_file_is_refused(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    module = Path(layout["package_dir"]) / "__init__.py"
    cache = tmp_path / "cache"
    cache.mkdir()
    os.link(module, cache / "archived.py")
    with pytest.raises(LaunchValidationError) as info:
        check(layout)
    assert end_reason(info) == "unsafe_path"
    assert "--link-mode=copy" in info.value.message


def test_copied_file_passes_the_hard_link_check(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    module = Path(layout["package_dir"]) / "__init__.py"
    (tmp_path / "copy.py").write_bytes(module.read_bytes())
    check(layout)


def test_hard_link_check_ignores_symlinks(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    (Path(layout["package_dir"]) / "link.py").symlink_to(tmp_path / "base")
    check(layout)


def test_interpreter_through_a_symlink_hop_in_a_writable_path_is_refused(tmp_path: Path) -> None:
    layout = interpreter_layout(tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "hop").symlink_to(layout["interpreter"])
    link = tmp_path / "python-link"
    link.symlink_to(cwd / "hop")
    layout["interpreter"] = str(link)
    assert refused_reason(layout, str(cwd)) == "unsafe_path"


def test_system_read_dirs_are_shared_with_pathrules() -> None:
    from claude_session import pathrules

    assert settings_builder.SYSTEM_READ_DIRS == pathrules.SYSTEM_READ_DIRS
