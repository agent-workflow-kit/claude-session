"""Permission profiles: selection, file profiles, refused keys, variable expansion and snapshots
(interface.md, Permission profiles and Resume and fork carry-over)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import textwrap
from pathlib import Path
from typing import Any

import pytest

from claude_session import config, defaults, fsutil, kinds
from claude_session import permission_profiles as pp
from claude_session.errors import InvalidPermissionProfile, UsageError

ROOT = Path(__file__).resolve().parent.parent
SECRET = "s3cr3t-value-never-echoed"
VARS = {
    "CSESSION_DIR": "/state/sessions/cs-abcd2345",
    "CWD": "/work/repo",
    "HOME": "/home/someone",
    "GIT_DIR": "/work/repo/.git",
}
# The label pattern shared by the dry-run, status and pre-launch schemas.
# The session-writable path check of a run where nothing matches.
NEVER = lambda path: False  # noqa: E731

LABEL_PATTERNS = [
    json.loads((ROOT / "schemas" / rel).read_text(encoding="utf-8"))
    for rel in ("cli/exec-dry-run.json", "hooks/pre-launch.json")
]


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


def resolve(cfg: config.Config, kind: str, **kw: Any) -> kinds.Resolution:
    kw.setdefault("mode", "headless")
    kw.setdefault("origin_url", lambda cwd: None)
    return kinds.resolve(cfg, kind, **kw)


def select(cfg: config.Config, kind: str, **kw: Any) -> pp.Profile:
    writable = kw.pop("session_writable", NEVER)
    expected = kw.pop("expected_label", None)
    return pp.select(
        cfg, resolve(cfg, kind, **kw), session_writable=writable, expected_label=expected
    )


def refused(call: Any, *args: Any, **kw: Any) -> InvalidPermissionProfile:
    with pytest.raises(InvalidPermissionProfile) as info:
        call(*args, **kw)
    assert info.value.end_reason == "invalid_permission_profile"
    return info.value


def text_of(error: InvalidPermissionProfile) -> str:
    return f"{error.message} {error.detail}"


def profile_file(tmp_path: Path, content: Any, name: str = "p.json") -> Path:
    path = tmp_path / "profiles" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if name.endswith(".json"):
        path.write_text(json.dumps(content), encoding="utf-8")
    else:
        import yaml

        path.write_text(yaml.safe_dump(content), encoding="utf-8")
    return path


# Named profiles


@pytest.mark.parametrize(
    ("kind", "name"),
    [("implement", "default"), ("rank", "readonly"), ("review", "review"), ("respond", "respond")],
)
def test_builtin_kinds_select_their_builtin_profiles(cfg_dir: Path, kind: str, name: str) -> None:
    profile = select(load(cfg_dir), kind)
    assert profile.name == name
    assert profile.content == defaults.BUILTIN_PERMISSION_PROFILES[name]
    assert profile.sha256 is None
    assert profile.label == name
    assert not profile.is_file


def test_named_profile_from_config(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "permission_profiles: {mine: {permissions: {allow: [Read]}}}\n")
    profile = select(cfg, "implement", flags={"permission_profile": "mine"})
    assert (profile.name, profile.content) == ("mine", {"permissions": {"allow": ["Read"]}})


def test_config_profile_merges_over_the_builtin_one(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "permission_profiles: {review: {permissions: {defaultMode: plan}}}\n")
    content = select(cfg, "review").content
    assert content["permissions"]["defaultMode"] == "plan"
    assert (
        content["permissions"]["deny"]
        == defaults.BUILTIN_PERMISSION_PROFILES["review"]["permissions"]["deny"]
    )
    assert content["sandbox"] == {"network": {"allowedDomains": []}}


def test_selected_content_is_a_copy(cfg_dir: Path) -> None:
    cfg = load(cfg_dir)
    before = copy.deepcopy(defaults.BUILTIN_PERMISSION_PROFILES)
    profile = select(cfg, "review")
    profile.content["permissions"]["allow"].append("Bash")
    assert select(cfg, "review").content == before["review"]
    assert defaults.BUILTIN_PERMISSION_PROFILES == before


def test_unknown_name_in_a_resolution_is_refused(cfg_dir: Path) -> None:
    cfg = load(cfg_dir)
    resolution = resolve(cfg, "implement")
    profile = resolution.profile.model_copy(update={"permission_profile": "missing"})
    broken = kinds.Resolution(**{**resolution.__dict__, "profile": profile})
    assert "missing" in text_of(refused(pp.select, cfg, broken, session_writable=NEVER))


def test_unknown_flag_name_is_a_usage_error_before_selection(cfg_dir: Path) -> None:
    with pytest.raises(UsageError):
        select(load(cfg_dir), "implement", flags={"permission_profile": "missing"})


# Refused keys (interface.md, Permission profiles)

REFUSED = [
    ("sandbox", "enabled", True),
    ("sandbox", "enabled", False),
    ("sandbox", "failIfUnavailable", False),
    ("sandbox", "allowUnsandboxedCommands", True),
    ("sandbox", "allowUnsandboxedCommands", False),
    ("sandbox", "excludedCommands", ["docker"]),
    ("sandbox", "excludedCommands", []),
    ("permissions", "additionalDirectories", ["/elsewhere"]),
    ("permissions", "additionalDirectories", []),
    ("sandbox", "autoAllowBashIfSandboxed", True),
    ("sandbox", "autoAllowBashIfSandboxed", "true"),
    ("sandbox", "autoAllowBashIfSandboxed", 1),
    ("sandbox", "autoAllowBashIfSandboxed", None),
]


@pytest.mark.parametrize(("section", "key", "value"), REFUSED)
def test_refused_keys_fail_isolated_runs(section: str, key: str, value: Any) -> None:
    error = refused(pp.validate, {section: {key: value}}, isolated=True)
    assert f"{section}.{key}" in text_of(error)


@pytest.mark.parametrize(("section", "key", "value"), REFUSED)
def test_refused_keys_are_allowed_for_trusted_runs(section: str, key: str, value: Any) -> None:
    pp.validate({section: {key: value}}, isolated=False)


def test_auto_allow_false_is_allowed_for_isolated_runs() -> None:
    pp.validate({"sandbox": {"autoAllowBashIfSandboxed": False}}, isolated=True)


# Sandbox escape keys outside the isolated-run allowlist (review round 1, R1).
ESCAPES = [
    {"sandbox": {"filesystem": {"disabled": True}}},
    {"sandbox": {"allowNestedUserNamespaces": True}},
    {"sandbox": {"allowAppleEvents": True}},
    {"sandbox": {"network": {"allowMachLookup": ["com.apple.x"]}}},
    {"sandbox": {"enableWeakerNetworkIsolation": True}},
    {"sandbox": {"enableWeakerNestedSandbox": True}},
    {"sandbox": {"ignoreViolations": {"*": ["/"]}}},
    {"sandbox": {"network": {"allowUnixSockets": ["/var/run/docker.sock"]}}},
    {"sandbox": {"network": {"allowAllUnixSockets": True}}},
    {"sandbox": {"network": {"allowLocalBinding": True}}},
    {"sandbox": {"network": {"httpProxyPort": 8080}}},
    {"sandbox": {"network": {"socksProxyPort": 1080}}},
    {"sandbox": {"network": {"strictAllowlist": False}}},
    {"sandbox": {"someFutureKey": True}},
]


@pytest.mark.parametrize("content", ESCAPES)
def test_sandbox_keys_outside_the_allowlist_fail_isolated_runs(content: dict[str, Any]) -> None:
    error = refused(pp.validate, content, isolated=True)
    assert "not allowed for isolated runs" in text_of(error)


def test_allowlisted_sandbox_keys_pass_isolated_runs() -> None:
    pp.validate(
        {
            "sandbox": {
                "filesystem": {
                    "allowRead": ["/a"],
                    "denyRead": ["/b"],
                    "allowWrite": ["/c"],
                    "denyWrite": ["/d"],
                },
                "network": {"allowedDomains": ["pypi.org"], "deniedDomains": ["example.org"]},
                "autoAllowBashIfSandboxed": False,
            }
        },
        isolated=True,
    )


def test_denied_domains_must_be_a_list_of_strings() -> None:
    error = refused(pp.validate, {"sandbox": {"network": {"deniedDomains": "x"}}}, isolated=False)
    assert "sandbox.network.deniedDomains" in text_of(error)


def test_default_mode_is_allowed_for_isolated_runs() -> None:
    # Ignored there (dontAsk is forced by the generated settings), never refused.
    pp.validate({"permissions": {"defaultMode": "bypassPermissions"}}, isolated=True)


@pytest.mark.parametrize("name", sorted(defaults.BUILTIN_PERMISSION_PROFILES))
def test_builtin_profiles_pass_isolated_validation(name: str) -> None:
    pp.validate(defaults.BUILTIN_PERMISSION_PROFILES[name], isolated=True)


def test_configured_profile_with_a_refused_key_fails_an_isolated_kind(cfg_dir: Path) -> None:
    cfg = load(cfg_dir, "permission_profiles: {review: {sandbox: {enabled: false}}}\n")
    assert "sandbox.enabled" in text_of(refused(select, cfg, "review"))
    assert select(cfg, "implement", flags={"permission_profile": "review"}).name == "review"


def test_configured_nulls_are_kept_and_checked(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        "permission_profiles: {review: {sandbox: {autoAllowBashIfSandboxed: null}}}\n",
    )
    assert "sandbox.autoAllowBashIfSandboxed" in text_of(refused(select, cfg, "review"))
    profile = select(cfg, "implement", flags={"permission_profile": "review"})
    assert profile.content["sandbox"]["autoAllowBashIfSandboxed"] is None


def test_reads_third_party_flag_makes_refused_keys_apply(cfg_dir: Path) -> None:
    cfg = load(
        cfg_dir,
        "permission_profiles: {loose: {sandbox: {excludedCommands: [docker]}}}\n",
    )
    flags = {"permission_profile": "loose"}
    assert select(cfg, "implement", flags=flags).name == "loose"
    refused(select, cfg, "implement", flags={**flags, "reads_third_party": True})
    refused(select, cfg, "implement", flags={**flags, "sandbox": "preferred"})


def test_all_refused_keys_are_reported() -> None:
    error = refused(
        pp.validate,
        {"sandbox": {"enabled": True, "excludedCommands": []}, "permissions": {}},
        isolated=True,
    )
    assert "sandbox.enabled" in text_of(error) and "sandbox.excludedCommands" in text_of(error)


# Shape


@pytest.mark.parametrize(
    ("content", "key"),
    [
        ([], "top level"),
        ("x", "top level"),
        (None, "top level"),
        ({"other": {}}, "other"),
        ({"hooks": {}}, "hooks"),
        ({"permissions": []}, "permissions"),
        ({"permissions": None}, "permissions"),
        ({"sandbox": "on"}, "sandbox"),
        ({"permissions": {"allow": "Read"}}, "permissions.allow"),
        ({"permissions": {"deny": [1]}}, "permissions.deny.0"),
        ({"permissions": {"ask": [None]}}, "permissions.ask.0"),
        ({"permissions": {"defaultMode": 5}}, "permissions.defaultMode"),
        ({"sandbox": {"filesystem": []}}, "sandbox.filesystem"),
        ({"sandbox": {"filesystem": {"allowWrite": "/x"}}}, "sandbox.filesystem.allowWrite"),
        ({"sandbox": {"filesystem": {"denyRead": [True]}}}, "sandbox.filesystem.denyRead.0"),
        ({"sandbox": {"network": {"allowedDomains": "x"}}}, "sandbox.network.allowedDomains"),
    ],
)
@pytest.mark.parametrize("isolated", [True, False])
def test_shape_is_checked_for_every_run(content: Any, key: str, isolated: bool) -> None:
    assert key in text_of(refused(pp.validate, content, isolated=isolated))


def test_unknown_nested_keys_are_kept() -> None:
    content = {"permissions": {"future": {"x": 1}}, "sandbox": {"network": {"other": True}}}
    pp.validate(content, isolated=False)
    pp.validate({"permissions": {"future": {"x": 1}}}, isolated=True)


def test_empty_profile_is_valid() -> None:
    pp.validate({}, isolated=True)


def test_error_never_echoes_values() -> None:
    error = refused(
        pp.validate, {"permissions": {"allow": SECRET, "deny": [SECRET, 1]}}, isolated=True
    )
    assert SECRET not in text_of(error)


def test_unknown_top_level_key_is_shown_safely() -> None:
    error = refused(pp.validate, {"a\x1b[2Jb" + "k" * 200: {}}, isolated=False)
    assert "\x1b" not in text_of(error)
    assert len(text_of(error)) < 300


# Variable expansion


def test_expands_every_variable_in_every_string() -> None:
    content = {
        "permissions": {
            "allow": ["Read(/${CSESSION_DIR}/**)", "Edit(/${CWD}/**)", "Bash(ls ${HOME})"],
            "deny": ["Read(/${GIT_DIR}/config)", "Edit(/${CWD}/${CWD})"],
        },
        "sandbox": {"filesystem": {"allowWrite": ["${CWD}/out"]}},
    }
    assert pp.expand(content, VARS) == {
        "permissions": {
            "allow": [
                "Read(//state/sessions/cs-abcd2345/**)",
                "Edit(//work/repo/**)",
                "Bash(ls /home/someone)",
            ],
            "deny": ["Read(//work/repo/.git/config)", "Edit(//work/repo//work/repo)"],
        },
        "sandbox": {"filesystem": {"allowWrite": ["/work/repo/out"]}},
    }


def test_builtin_respond_profile_expands_to_absolute_rules() -> None:
    expanded = pp.expand(defaults.BUILTIN_PERMISSION_PROFILES["respond"], VARS)
    assert "Edit(//work/repo/**)" in expanded["permissions"]["allow"]
    assert "Write(//work/repo/**)" in expanded["permissions"]["allow"]


def test_expansion_leaves_the_input_unchanged() -> None:
    content = {"permissions": {"allow": ["Edit(/${CWD}/**)"]}}
    before = copy.deepcopy(content)
    pp.expand(content, VARS)
    assert content == before


def test_keys_and_non_strings_are_not_expanded() -> None:
    content = {"permissions": {"${CWD}": ["x"], "n": 3, "b": True, "z": None}}
    assert pp.expand(content, VARS) == content


@pytest.mark.parametrize(
    "rule", ["Bash(echo $HOME)", "Bash(echo $)", "Bash(a {b})", "Bash(${)", "Bash(x}${)"]
)
def test_other_dollar_forms(rule: str) -> None:
    if "${" in rule:
        refused(pp.expand, {"permissions": {"allow": [rule]}}, VARS)
    else:
        assert pp.expand({"permissions": {"allow": [rule]}}, VARS)["permissions"]["allow"] == [rule]


@pytest.mark.parametrize(
    "rule", ["Read(/${PATH}/x)", "Read(/${cwd}/x)", "Read(/${}/x)", "Read(/${HOME/x)", "${ CWD}"]
)
def test_unknown_or_malformed_variables_fail(rule: str) -> None:
    error = refused(pp.expand, {"permissions": {"deny": [rule]}}, VARS)
    assert "permissions.deny.0" in text_of(error)


@pytest.mark.parametrize("name", ["CSESSION_DIR", "CWD", "HOME", "GIT_DIR"])
@pytest.mark.parametrize("value", ["", None, "missing"])
def test_empty_expansion_fails(name: str, value: str | None) -> None:
    variables = dict(VARS)
    if value == "missing":
        del variables[name]
    else:
        variables[name] = value  # type: ignore[assignment]
    content = {"sandbox": {"filesystem": {"denyWrite": ["/x", "${" + name + "}/y"]}}}
    error = refused(pp.expand, content, variables)
    assert name in text_of(error)
    assert "sandbox.filesystem.denyWrite.1" in text_of(error)


def test_empty_variable_that_is_not_used_is_fine() -> None:
    content = {"permissions": {"allow": ["Edit(/${CWD}/**)"]}}
    assert pp.expand(content, {**VARS, "GIT_DIR": None})["permissions"]["allow"] == [
        "Edit(//work/repo/**)"
    ]


@pytest.mark.parametrize(
    "value",
    [
        "relative/dir",
        "/work/*",
        "/work/?",
        "/work/[ab]",
        "/work/{a,b}",
        "/work/a\\b",
        "/work/a\nb",
        "/work/a\x00b",
        "/work/a\x7fb",
        "/work/a\u0085b",
    ],
)
def test_unsafe_variable_values_fail_when_used(value: str) -> None:
    content = {"permissions": {"allow": ["Edit(/${CWD}/**)"]}}
    error = refused(pp.expand, content, {**VARS, "CWD": value})
    assert "CWD" in text_of(error)
    assert value not in text_of(error)
    # Unused, the same value is harmless.
    pp.expand({"permissions": {"allow": ["Read"]}}, {**VARS, "CWD": value})


def test_unicode_and_spaces_in_values_are_kept() -> None:
    content = {"permissions": {"allow": ["Edit(/${CWD}/**)"]}}
    expanded = pp.expand(content, {**VARS, "CWD": "/work/my repo/é"})
    assert expanded["permissions"]["allow"] == ["Edit(//work/my repo/é/**)"]


def test_expansion_error_never_echoes_the_rule() -> None:
    error = refused(pp.expand, {"permissions": {"allow": [f"Bash({SECRET} ${{X}})"]}}, VARS)
    assert SECRET not in text_of(error)


# Profile files: reading


@pytest.mark.parametrize("name", ["p.json", "p.yaml", "p.yml", "profile"])
def test_reads_json_and_yaml_files(tmp_path: Path, cfg_dir: Path, name: str) -> None:
    content = {"permissions": {"allow": ["Edit(/${CWD}/**)"]}, "sandbox": {}}
    path = profile_file(tmp_path, content, name)
    profile = select(load(cfg_dir), "review", flags={"permission_profile": str(path)})
    assert profile.is_file and profile.name is None
    assert profile.content == content  # unexpanded


def test_relative_file_is_read_from_the_current_directory(
    tmp_path: Path, cfg_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_file(tmp_path, {"permissions": {"allow": ["Read"]}})
    monkeypatch.chdir(tmp_path)
    profile = select(load(cfg_dir), "implement", flags={"permission_profile": "profiles/p.json"})
    assert profile.content == {"permissions": {"allow": ["Read"]}}


def test_file_replaces_the_kind_profile_entirely(tmp_path: Path, cfg_dir: Path) -> None:
    path = profile_file(tmp_path, {"permissions": {"allow": ["Read"]}})
    profile = select(load(cfg_dir), "review", flags={"permission_profile": str(path)})
    assert "sandbox" not in profile.content  # nothing merged from the review profile
    assert profile.content == {"permissions": {"allow": ["Read"]}}


@pytest.mark.parametrize(
    ("raw", "name", "problem"),
    [
        (b"", "p.json", "invalid JSON"),
        (b"", "p.yaml", "expected a mapping"),
        (b"[]", "p.json", "expected a mapping"),
        (b"{bad", "p.json", "invalid JSON"),
        (b'{"permissions": {}, "permissions": {}}', "p.json", "duplicate key"),
        (b'{"permissions": {"x": NaN}}', "p.json", "invalid JSON"),
        (b'{"permissions": {"x": Infinity}}', "p.json", "invalid JSON"),
        (b"\xff\xfe", "p.json", "not UTF-8"),
        (b"\xff\xfe", "p.yaml", "not UTF-8"),
        (b"permissions: {}\npermissions: {}\n", "p.yaml", "duplicate key"),
        (b"a: &a {x: 1}\npermissions: {<<: *a}\n", "p.yaml", "merge keys"),
        (b"permissions: {allow: [2001-01-01]}\n", "p.yaml", "timestamp"),
        (b"permissions: !!python/object:os.system {}\n", "p.yaml", "tagged"),
        (b"[" * 70 + b"]" * 70, "p.yaml", "nested"),
        (b'{"permissions": {"x": ' + b"[" * 70 + b"]" * 70 + b"}}", "p.json", "nested"),
        (b'{"permissions": {"x": ' + b"[" * 600 + b"]" * 600 + b"}}", "p.json", "nested"),
        (b"[" * 100000 + b"]" * 100000, "p.json", "invalid JSON"),
        (b'{"permissions": {"n": 1' + b"0" * 5000 + b"}}", "p.json", "invalid JSON"),
    ],
)
def test_invalid_files_fail(tmp_path: Path, raw: bytes, name: str, problem: str) -> None:
    path = tmp_path / name
    path.write_bytes(raw)
    error = refused(pp.read_file, str(path), isolated=True, session_writable=NEVER)
    assert problem in text_of(error)
    assert str(path) in text_of(error)


def test_file_content_is_never_echoed(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text('{"permissions": {"allow": "' + SECRET + '"}} trailing ' + SECRET)
    assert SECRET not in text_of(
        refused(pp.read_file, str(path), isolated=True, session_writable=NEVER)
    )
    path.write_text('{"permissions": {"allow": "' + SECRET + '"}}')
    assert SECRET not in text_of(
        refused(pp.read_file, str(path), isolated=True, session_writable=NEVER)
    )


def test_file_larger_than_the_limit_fails(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    filler = " " * (pp.MAX_FILE_BYTES - 1)
    path.write_text("{}" + filler)
    assert "larger than" in text_of(
        refused(pp.read_file, str(path), isolated=False, session_writable=None)
    )
    path.write_text("{}" + filler[:-1])
    assert pp.read_file(str(path), isolated=False, session_writable=None).content == {}


def test_missing_file_fails(tmp_path: Path) -> None:
    error = refused(
        pp.read_file, str(tmp_path / "missing.json"), isolated=False, session_writable=None
    )
    assert "cannot read" in text_of(error)


def test_directory_fails(tmp_path: Path) -> None:
    (tmp_path / "d.json").mkdir()
    refused(pp.read_file, str(tmp_path / "d.json"), isolated=False, session_writable=None)


def test_fifo_fails_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "p.json")
    assert "not a regular file" in text_of(
        refused(pp.read_file, str(tmp_path / "p.json"), isolated=False, session_writable=None)
    )


def test_nul_in_the_path_fails(tmp_path: Path) -> None:
    refused(pp.read_file, str(tmp_path) + "/p\x00.json", isolated=False, session_writable=None)


def test_unreadable_file_fails(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    path = profile_file(tmp_path, {})
    path.chmod(0)
    assert "cannot read" in text_of(
        refused(pp.read_file, str(path), isolated=False, session_writable=None)
    )


def test_symlink_is_followed_and_checked_at_its_target(tmp_path: Path) -> None:
    target = profile_file(tmp_path, {"permissions": {"allow": ["Read"]}})
    link = tmp_path / "link.json"
    link.symlink_to(target)
    seen: list[str] = []

    def writable(path: str) -> bool:
        seen.append(path)
        return False

    profile = pp.read_file(str(link), isolated=True, session_writable=writable)
    assert profile.content == {"permissions": {"allow": ["Read"]}}
    assert seen == [os.path.realpath(target)]


def test_file_under_a_session_writable_path_fails_isolated_runs(tmp_path: Path) -> None:
    path = profile_file(tmp_path, {})
    error = refused(pp.read_file, str(path), isolated=True, session_writable=lambda p: True)
    assert "session-writable" in text_of(error)


def test_session_writable_check_does_not_apply_to_trusted_runs(tmp_path: Path) -> None:
    def never(path: str) -> bool:
        raise AssertionError("not checked for trusted runs")

    pp.read_file(str(profile_file(tmp_path, {})), isolated=False, session_writable=never)


def test_session_writable_check_runs_before_the_file_is_read(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"
    error = refused(pp.read_file, str(path), isolated=True, session_writable=lambda p: True)
    assert "session-writable" in text_of(error)


def test_refused_keys_apply_to_files(tmp_path: Path, cfg_dir: Path) -> None:
    path = profile_file(tmp_path, {"sandbox": {"enabled": False}})
    refused(select, load(cfg_dir), "review", flags={"permission_profile": str(path)})
    profile = select(load(cfg_dir), "implement", flags={"permission_profile": str(path)})
    assert profile.content == {"sandbox": {"enabled": False}}


# Snapshots and carry-over


def test_label_is_a_sha256_prefix_of_the_snapshot(tmp_path: Path) -> None:
    profile = pp.read_file(
        str(profile_file(tmp_path, {"permissions": {"allow": ["Read"]}})),
        isolated=False,
        session_writable=None,
    )
    snapshot = profile.snapshot_bytes()
    digest = hashlib.sha256(snapshot).hexdigest()
    assert profile.sha256 == digest
    assert profile.label == "file:" + digest[: pp.HASH_PREFIX]
    assert json.loads(snapshot) == profile.content
    for schema in LABEL_PATTERNS:
        pattern = schema["properties"]["permission_profile"]["pattern"]
        assert re.fullmatch(pattern.strip("^$"), profile.label)


def test_snapshot_is_canonical_json(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    a.write_text("sandbox: {}\npermissions:\n  allow: [Read, Grep]\n")
    b = tmp_path / "b.json"
    b.write_text('{ "permissions" : { "allow" : [ "Read", "Grep" ] }, "sandbox": {} }\n')
    first = pp.read_file(str(a), isolated=False, session_writable=None)
    second = pp.read_file(str(b), isolated=False, session_writable=None)
    assert first.snapshot_bytes() == second.snapshot_bytes()
    assert first.sha256 == second.sha256


def test_snapshot_keeps_variables_unexpanded_and_unicode(tmp_path: Path) -> None:
    path = profile_file(tmp_path, {"permissions": {"allow": ["Edit(/${CWD}/é/**)"]}})
    snapshot = pp.read_file(str(path), isolated=False, session_writable=None).snapshot_bytes()
    assert "${CWD}" in snapshot.decode("utf-8")
    assert "é" in snapshot.decode("utf-8")


def test_write_snapshot_uses_the_injected_writer(tmp_path: Path) -> None:
    profile = pp.read_file(str(profile_file(tmp_path, {})), isolated=False, session_writable=None)
    written: list[tuple[str, bytes]] = []
    pp.write_snapshot(
        profile,
        "/private/runs/1/permission-profile.json",
        write=lambda p, d: written.append((p, d)),
    )
    assert written == [("/private/runs/1/permission-profile.json", profile.snapshot_bytes())]


def test_write_snapshot_defaults_to_a_private_no_follow_write(tmp_path: Path) -> None:
    profile = pp.read_file(str(profile_file(tmp_path, {})), isolated=False, session_writable=None)
    dest = tmp_path / "private" / "runs" / "1" / pp.SNAPSHOT_NAME
    dest.parent.mkdir(parents=True)
    pp.write_snapshot(profile, str(dest))
    assert dest.read_bytes() == profile.snapshot_bytes()
    assert dest.stat().st_mode & 0o777 == 0o600


def test_named_profiles_have_no_snapshot(cfg_dir: Path) -> None:
    profile = select(load(cfg_dir), "review")
    with pytest.raises(ValueError):
        profile.snapshot_bytes()
    with pytest.raises(ValueError):
        pp.write_snapshot(profile, "/x", write=lambda p, d: None)


def test_snapshot_is_carried_on_resume_and_the_file_is_never_re_read(
    tmp_path: Path, cfg_dir: Path
) -> None:
    cfg = load(cfg_dir)
    original = profile_file(tmp_path, {"permissions": {"allow": ["Edit(/${CWD}/**)"]}})
    run1 = select(cfg, "review", flags={"permission_profile": str(original)})
    snap1 = tmp_path / "private" / "runs" / "1" / pp.SNAPSHOT_NAME
    snap1.parent.mkdir(parents=True)
    pp.write_snapshot(run1, str(snap1))
    # The user changes (then removes) the file: later runs use the snapshot.
    original.write_text('{"permissions": {"allow": ["Bash"]}}')
    original.unlink()

    previous = kinds.Previous(repo=None, flags={"permission_profile": str(snap1)})
    resolution = resolve(cfg, "review", previous=previous)
    assert resolution.origins["permission_profile_file"] == "previous_run"
    run2 = pp.select(cfg, resolution, session_writable=NEVER, expected_label=run1.label)
    assert run2.content == run1.content
    assert run2.label == run1.label
    snap2 = tmp_path / "private" / "runs" / "2" / pp.SNAPSHOT_NAME
    snap2.parent.mkdir(parents=True)
    pp.write_snapshot(run2, str(snap2))
    assert snap2.read_bytes() == snap1.read_bytes()
    assert pp.expand(run2.content, VARS) == {"permissions": {"allow": ["Edit(//work/repo/**)"]}}


def test_carried_snapshot_is_validated_again(tmp_path: Path, cfg_dir: Path) -> None:
    cfg = load(cfg_dir)
    snapshot = tmp_path / pp.SNAPSHOT_NAME
    snapshot.write_text('{"sandbox": {"enabled": false}}')
    previous = kinds.Previous(repo=None, flags={"permission_profile": str(snapshot)})
    refused(select, cfg, "review", previous=previous)


def test_carried_snapshot_is_checked_for_session_writable_paths(
    tmp_path: Path, cfg_dir: Path
) -> None:
    snapshot = tmp_path / pp.SNAPSHOT_NAME
    snapshot.write_text("{}")
    previous = kinds.Previous(repo=None, flags={"permission_profile": str(snapshot)})
    refused(select, load(cfg_dir), "review", previous=previous, session_writable=lambda p: True)


def test_fsutil_is_the_default_writer() -> None:
    defaults_ = pp.write_snapshot.__kwdefaults__
    assert defaults_ is not None and defaults_["write"] is fsutil.write_private_file


# Review round 1: values a snapshot cannot hold, snapshot integrity, more refused keys


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        (b'{"permissions": {"x": 1e400}}', "p.json"),
        (b'{"permissions": {"x": [-1e400]}}', "p.json"),
        (b"permissions: {x: .inf}\n", "p.yaml"),
        (b"permissions: {x: -.inf}\n", "p.yaml"),
        (b"permissions: {x: .nan}\n", "p.yaml"),
        (b"permissions: {x: 1.0e+400}\n", "p.yaml"),
    ],
)
def test_non_finite_numbers_in_a_file_fail(tmp_path: Path, raw: bytes, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(raw)
    error = refused(pp.read_file, str(path), isolated=False, session_writable=None)
    assert "permissions.x" in text_of(error)
    assert "not a finite number" in text_of(error)


@pytest.mark.parametrize("number", [float("inf"), float("-inf"), float("nan")])
def test_validate_refuses_non_finite_numbers(number: float) -> None:
    error = refused(pp.validate, {"sandbox": {"network": {"x": [1, number]}}}, isolated=False)
    assert "sandbox.network.x.1: not a finite number" in text_of(error)


def test_canonical_json_refuses_non_finite_numbers() -> None:
    with pytest.raises(ValueError):
        pp._canonical({"x": float("inf")})


@pytest.mark.parametrize(
    "content",
    [
        {},
        {"permissions": {"allow": ["Read", "Edit(/${CWD}/é/**)"], "x": [1, 2.5, -0.0, None]}},
        {"sandbox": {"network": {"allowedDomains": []}, "y": {"z": True, "n": 10**30}}},
    ],
)
@pytest.mark.parametrize("name", ["p.json", "p.yaml"])
def test_every_accepted_snapshot_re_reads_to_the_same_label(
    tmp_path: Path, content: Any, name: str
) -> None:
    first = pp.read_file(
        str(profile_file(tmp_path, content, name)), isolated=False, session_writable=None
    )
    snapshot = tmp_path / pp.SNAPSHOT_NAME
    pp.write_snapshot(first, str(snapshot))
    again = pp.read_file(
        str(snapshot), isolated=False, session_writable=None, expected_label=first.label
    )
    assert again.label == first.label
    assert again.snapshot_bytes() == first.snapshot_bytes()


@pytest.mark.parametrize(
    "raw",
    [b'{"permissions": {"allow": ["\\ud800"]}}', b'{"permissions": {"\\udfff": []}}'],
    ids=["value", "key"],
)
def test_lone_surrogates_in_json_fail(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "p.json"
    path.write_bytes(raw)
    error = refused(pp.read_file, str(path), isolated=False, session_writable=None)
    assert error.detail == f"{path}: invalid string"


def test_unencodable_yaml_integer_fails(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_bytes(b"permissions: {x: 0x" + b"f" * 4000 + b"}\n")
    error = refused(pp.read_file, str(path), isolated=False, session_writable=None)
    assert error.detail == f"{path}: value cannot be snapshotted"


def test_snapshot_bytes_are_frozen_at_read_time(tmp_path: Path) -> None:
    path = profile_file(tmp_path, {"permissions": {"allow": ["Read"]}})
    profile = pp.read_file(str(path), isolated=False, session_writable=None)
    label = profile.label
    profile.content["permissions"]["allow"].append("Bash")
    snapshot = profile.snapshot_bytes()
    assert "file:" + hashlib.sha256(snapshot).hexdigest()[: pp.HASH_PREFIX] == label
    assert json.loads(snapshot) == {"permissions": {"allow": ["Read"]}}


def test_snapshot_matching_its_recorded_label_is_accepted(tmp_path: Path, cfg_dir: Path) -> None:
    profile = pp.read_file(
        str(profile_file(tmp_path, {"permissions": {"allow": ["Read"]}})),
        isolated=True,
        session_writable=NEVER,
    )
    snapshot = tmp_path / pp.SNAPSHOT_NAME
    pp.write_snapshot(profile, str(snapshot))
    previous = kinds.Previous(repo=None, flags={"permission_profile": str(snapshot)})
    carried = select(load(cfg_dir), "review", previous=previous, expected_label=profile.label)
    assert carried.label == profile.label


def test_edited_snapshot_is_refused(tmp_path: Path, cfg_dir: Path) -> None:
    profile = pp.read_file(
        str(profile_file(tmp_path, {"permissions": {"allow": ["Read"]}})),
        isolated=True,
        session_writable=NEVER,
    )
    snapshot = tmp_path / pp.SNAPSHOT_NAME
    pp.write_snapshot(profile, str(snapshot))
    snapshot.write_text('{"permissions":{"allow":["Bash","Read"]}}')
    previous = kinds.Previous(repo=None, flags={"permission_profile": str(snapshot)})
    error = refused(
        select, load(cfg_dir), "review", previous=previous, expected_label=profile.label
    )
    assert error.detail == f"{snapshot}: snapshot does not match its recorded hash"


NEW_REFUSED = [
    (("sandbox", "enableWeakerNestedSandbox"), True),
    (("sandbox", "ignoreViolations"), {"*": ["/"]}),
    (("sandbox", "network", "allowUnixSockets"), ["/var/run/docker.sock"]),
    (("sandbox", "network", "allowAllUnixSockets"), True),
    (("sandbox", "network", "allowLocalBinding"), True),
    (("sandbox", "network", "httpProxyPort"), 8080),
    (("sandbox", "network", "socksProxyPort"), 1080),
]


def nested(loc: tuple[str, ...], value: Any) -> dict[str, Any]:
    for part in reversed(loc):
        value = {part: value}
    return value


@pytest.mark.parametrize(("loc", "value"), NEW_REFUSED)
def test_sandbox_escape_keys_fail_isolated_runs(loc: tuple[str, ...], value: Any) -> None:
    error = refused(pp.validate, nested(loc, value), isolated=True)
    assert f"{'.'.join(loc)}: not allowed for isolated runs" in text_of(error)


@pytest.mark.parametrize(("loc", "value"), NEW_REFUSED)
def test_sandbox_escape_keys_are_allowed_for_trusted_runs(loc: tuple[str, ...], value: Any) -> None:
    pp.validate(nested(loc, value), isolated=False)


@pytest.mark.parametrize(
    "value",
    ["/home/u/../..", "/home/u/", "/home/u//.ssh", "//home/u", "/home/./u", "/home/u/."],
)
def test_non_normalized_variable_values_fail(value: str) -> None:
    content = {"permissions": {"allow": ["Read(/${HOME}/.ssh/**)"]}}
    error = refused(pp.expand, content, {**VARS, "HOME": value})
    assert "HOME" in text_of(error)
    assert value not in text_of(error)


def test_root_is_a_normalized_variable_value() -> None:
    expanded = pp.expand({"permissions": {"allow": ["Read(/${HOME}**)"]}}, {**VARS, "HOME": "/"})
    assert expanded == {"permissions": {"allow": ["Read(//**)"]}}


@pytest.mark.parametrize("call", ["select", "read_file"])
def test_isolated_runs_require_the_session_writable_check(
    tmp_path: Path, cfg_dir: Path, call: str
) -> None:
    path = profile_file(tmp_path, {})
    with pytest.raises(ValueError, match="session_writable"):
        if call == "read_file":
            pp.read_file(str(path), isolated=True, session_writable=None)
        else:
            cfg = load(cfg_dir)
            pp.select(cfg, resolve(cfg, "review"), session_writable=None)


def test_session_writable_is_a_required_keyword() -> None:
    for function in (pp.select, pp.read_file):
        kwdefaults = function.__kwdefaults__ or {}
        assert "session_writable" not in kwdefaults
