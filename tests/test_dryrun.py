"""The dry-run plan and the ``kinds`` report, in process (interface.md, Dry run and kinds):
the dry-run pipeline on a laid-out host with the documented isolated settings (goldens),
``claude --version`` parsing and probing, the read-only session-directory check, the
interactive TTY checks and the planned own directories of the path rules. The command line
end to end is in test_dryrun_cli.py."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from claude_session import (
    claude_version,
    config,
    dryrun,
    envbuild,
    kinds,
    launch,
    pathrules,
    sessiondir,
    settings_builder,
)
from claude_session.errors import LaunchValidationError, UsageError
from claude_session.sessiondir import SessionDirUnsafe

from .fake_gateway import FakeGateway, Response
from .golden import assert_golden
from .schemas import validate
from .test_isolated_settings import (
    CS_PATH,
    CSDIR,
    GIT_DIR,
    GOLDEN,
    HOME,
    ISOLATED_KINDS,
    PLUGIN,
    PYTHON,
    PYTHON_DIRS,
    REPO,
    SID,
    SYSTEM,
    WT_GIT_DIR,
)

DRY_RUN = "claude-session/cli/exec-dry-run.json"
KINDS = "claude-session/cli/kinds.json"
SECRET = "do-not-print-this-value-0123456789"
# The probe's session-writable paths: none (unit tests of the probe itself).
NOTHING = pathrules.SessionWritable([])
PATH_ONLY = {"PATH": "/usr/bin"}


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory) -> config.Config:
    home = tmp_path_factory.mktemp("xdg")
    return config.load(None, env={"XDG_CONFIG_HOME": str(home)})


def load_cfg(tmp_path: Path, text: str) -> config.Config:
    path = tmp_path / "cfg" / "claude-session" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "cfg")})


def request(kind: str, **fields: Any) -> launch.Request:
    values: dict[str, Any] = {
        "kind": kind,
        "mode": "headless",
        "skill": None,
        "prompt": b"Review it.\n",
        "inputs": (),
        "plugin_dirs": (PLUGIN,),
        "repo": None,
        "cwd": None,
        "labels": (),
        "flags": dict.fromkeys(kinds.FLAG_KEYS),
        "actor": "auto:unknown",
        "decision_ids": (),
        "hooks": (),
        "hook_timeouts": (),
        "outcome_file": None,
    }
    values.update(fields)
    return launch.Request(**values)


class GitGateway(FakeGateway):
    """A fake gateway whose ``git rev-parse`` prints ``git_dirs`` (None: not a repository);
    every other git call (origin remote, repository config) prints nothing."""

    def __init__(self, git_dirs: tuple[str, str] | None) -> None:
        super().__init__()
        self.git_dirs = git_dirs

    def _answer(self, argv: tuple[str, ...]) -> Response:
        if argv[0] != "git":
            return super()._answer(argv)
        if "rev-parse" in argv:
            if self.git_dirs is None:
                return Response(128, "", "fatal: not a git repository (or any parent)\n")
            return Response(0, "\n".join(self.git_dirs) + "\n")
        if "remote" in argv:
            return Response(2)
        return Response(0)


def executable_at(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def real_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> tuple[Any, Path]:
    """The plan ``dryrun.new_session_plan`` gives for a headless run of ``kind`` on a host
    laid out under ``<root>`` as the settings goldens' fixed inputs (tests/golden_settings)
    are: ``<root>/home/user``, its state directory, the tools, the plugin directory, the
    respond checkout ``<root>/work/repo`` and its git directories. The generated system and
    Python read entries are those inputs too (they depend on the machine)."""
    root = tmp_path / "root"
    home = root / HOME.lstrip("/")
    state = home / ".local" / "state" / "claude-session"
    state.mkdir(parents=True, mode=0o700)
    for name in ("bwrap", "socat"):
        executable_at(root / "usr" / "bin" / name)
    executable_at(root / "usr" / "local" / "bin" / "git")
    real_claude = executable_at(root / "opt" / "claude" / "versions" / "2.1.314")
    (root / "usr" / "local" / "bin" / "claude").symlink_to(real_claude)
    for directory in (*SYSTEM, *PYTHON_DIRS, PLUGIN, "/opt/toolchain", REPO, WT_GIT_DIR):
        (root / directory.lstrip("/")).mkdir(parents=True, exist_ok=True)
    prefixed = lambda paths: tuple(f"{root}{p}" for p in paths)  # noqa: E731
    monkeypatch.setattr(settings_builder, "system_read_dirs", lambda: prefixed(SYSTEM))
    monkeypatch.setattr(settings_builder, "python_read_dirs", lambda **_: prefixed(PYTHON_DIRS))
    toolchain = f"{root}/opt/toolchain"
    cfg = load_cfg(
        tmp_path,
        "claude_bin: claude\nkinds:\n"
        + "".join(f"  {k}:\n    read_dirs: ['{toolchain}']\n" for k in ISOLATED_KINDS),
    )
    git = kind != "rank"
    gw = GitGateway((f"{root}{GIT_DIR}", f"{root}{WT_GIT_DIR}") if git else None)
    for name in ("bwrap", "socat"):
        gw.on([f"{root}/usr/bin/{name}"])
    environ = {
        "PATH": f"{root}/usr/bin:{root}/usr/local/bin",
        "HOME": str(home),
        "ANTHROPIC_API_KEY": SECRET,
    }
    proc = launch.Process(
        environ=environ,
        caller_cwd=f"{root}{REPO}",
        claude_session=f"{root}{CS_PATH}",
        interpreter=f"{root}{PYTHON}",
        state_root=str(state),
        gateway=gw,
    )
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Review it.\n")
    args = parsed(
        "exec",
        "--dry-run",
        "--json",
        "--kind",
        kind,
        "--mode",
        "headless",
        "--prompt-file",
        str(prompt),
        "--plugin-dir",
        f"{root}{PLUGIN}",
        "--actor",
        "auto:test",
    )
    plan, _ = dryrun.new_session_plan(args, cfg, proc)
    return plan, root


def normalized_plan(plan: Any, root: Path) -> Any:
    """The host paths of ``real_plan`` mapped back to the goldens' fixed inputs: ``<root>``
    removed and the per-test tmp root shown as ``/tmp/csession-1000``."""
    text = json.dumps(plan)
    text = text.replace(pathrules.tmp_parent(), "/tmp/csession-1000").replace(str(root), "")
    return json.loads(text)


# Goldens


@pytest.mark.parametrize("kind", ISOLATED_KINDS)
def test_dry_run_plan_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """``exec --dry-run --json`` of each isolated built-in kind through the dry-run
    pipeline: valid against exec-dry-run.json, and its settings are the documented ones
    (tests/golden_settings, whose session id is ``cs-abcd2345``)."""
    plan, root = real_plan(tmp_path, monkeypatch, kind)
    assert plan["would_fail"] is None, plan["warnings"]
    shown = normalized_plan(plan, root)
    assert_golden(shown, f"exec-dry-run-{kind}")
    documented = json.loads((GOLDEN / f"settings-{kind}.json").read_text(encoding="utf-8"))
    settings = json.loads(json.dumps(shown["settings"]).replace(dryrun.PLACEHOLDER_ID, SID))
    settings.pop("hooks")
    assert settings == documented


def test_the_plan_never_holds_an_environment_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = real_plan(tmp_path, monkeypatch, "review")
    text = json.dumps(plan)
    assert SECRET not in text
    assert "ANTHROPIC_API_KEY" in plan["env_names"]
    assert plan["profile"]["required_env_names"] == ["ANTHROPIC_API_KEY"]


def test_the_sandbox_lists_come_from_the_generated_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _ = real_plan(tmp_path, monkeypatch, "review")
    filesystem = plan["settings"]["sandbox"]["filesystem"]
    assert plan["sandbox"]["allow_read"] == filesystem["allowRead"]
    assert plan["sandbox"]["allow_write"] == filesystem["allowWrite"]
    assert plan["sandbox"]["deny_read"] == filesystem["denyRead"]
    assert plan["sandbox"]["deny_write"] == filesystem["denyWrite"]


def test_a_failed_plan_has_no_settings_argv_or_environment(cfg: config.Config) -> None:
    resolution = kinds.resolve(cfg, "review", mode="headless", origin_url=lambda _: None)
    found = dryrun.Outcome(would_fail="sandbox_unavailable", message="no bwrap")
    plan = dryrun.plan_json(
        cfg,
        request("review"),
        resolution,
        found,
        sandbox=dryrun.Sandbox(available=False, problems=("bwrap not found",)),
        preview=dryrun.Preview(path="/usr/bin", read_dirs=(), write_dirs=(), required_env_names=()),
        cwd=f"{CSDIR}/work",
    )
    validate(plan, DRY_RUN)
    assert plan["would_fail"] == "sandbox_unavailable"
    assert plan["settings"] == {} and plan["claude_argv"] == [] and plan["env_names"] == []
    assert all(plan["sandbox"][key] == [] for key in dryrun.SANDBOX_LISTS)
    assert plan["sandbox"]["problems"] == ["bwrap not found"]
    assert dryrun.exit_code(plan) == 12


def test_exit_codes() -> None:
    assert dryrun.exit_code({"would_refuse": None, "would_fail": None}) == 0
    assert dryrun.exit_code({"would_refuse": None, "would_fail": "unsafe_cwd"}) == 12
    assert dryrun.exit_code({"would_refuse": "live", "would_fail": "unsafe_cwd"}) == 20


# Origins


def test_origins_rename_env_set_and_follow_the_mode(cfg: config.Config) -> None:
    resolution = kinds.resolve(
        cfg, "implement", mode="interactive", flags={"timeout": "10m"}, origin_url=lambda _: None
    )
    origins = dryrun.origins_json(resolution, "interactive")
    assert "env.set" not in origins and origins["env.set_names"] == "builtin"
    assert origins["interactive_timeout"] == "flag"
    assert origins["timeout"] == "flag"  # the mode's key
    assert dryrun.origins_json(resolution, "headless")["timeout"] == "builtin"


# Hooks


HOOKS = """\
hooks:
  pre-launch:
    - cmd: only-review
      kinds: [review]
    - cmd: not-review
      exclude_kinds: [review]
      timeout: 5s
  post-state-change:
    - cmd: labelled
      labels: [team=a]
      states: [exited]
"""


def test_hooks_report_selection_and_source(tmp_path: Path) -> None:
    cfg = load_cfg(tmp_path, HOOKS)
    req = request("review", hooks=("pre-launch=echo hi",), hook_timeouts=("pre-launch=2m",))
    entries = dryrun.hooks_json(cfg, req, "review", ["kind=review", "team=a"])
    assert entries == [
        {
            "hook": "pre-launch",
            "cmd": "only-review",
            "timeout": 30.0,
            "source": "config",
            "selected": True,
        },
        {
            "hook": "pre-launch",
            "cmd": "not-review",
            "timeout": 5.0,
            "source": "config",
            "selected": False,
        },
        {
            "hook": "post-state-change",
            "cmd": "labelled",
            "timeout": 30.0,
            "source": "config",
            "selected": True,
        },
        {
            "hook": "pre-launch",
            "cmd": "echo hi",
            "timeout": 120.0,
            "source": "flag",
            "selected": True,
        },
    ]
    other = dryrun.hooks_json(cfg, request("implement"), "implement", ["kind=implement"])
    assert [e["selected"] for e in other] == [False, True, False]


# required_env_names


def test_required_env_names_are_auth_allow_and_set_names(tmp_path: Path) -> None:
    cfg = load_cfg(
        tmp_path,
        "kinds:\n  implement:\n    env:\n      allow: [LC_*, MY_VAR]\n      set: {SET_ME: x}\n",
    )
    resolution = kinds.resolve(
        cfg, "implement", mode="headless", flags={"env_allow": ["EXTRA"]}, origin_url=lambda _: None
    )
    session_env = envbuild.SessionEnv(env={}, auth_names=("ANTHROPIC_API_KEY",), secret_names=())
    assert dryrun.required_env_names(resolution, session_env) == (
        "ANTHROPIC_API_KEY",
        "LC_*",
        "MY_VAR",
        "EXTRA",
        "SET_ME",
    )


# claude --version


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("2.1.312 (Claude Code)", (2, 1, 312)),
        ("2.1.312", (2, 1, 312)),
        ("  10.0\n", (10, 0)),
        ("v2.1.312", None),
        ("2.1.312beta", None),
        ("", None),
        ("Claude Code 2.1.312", None),
        ("2..1", None),
    ],
)
def test_version_parse(text: str, parsed: tuple[int, ...] | None) -> None:
    assert claude_version.parse(text) == parsed


@pytest.mark.parametrize(
    ("text", "minimum", "ok"),
    [
        ("2.1.312 (Claude Code)", "2.1.312", True),
        ("2.1.313", "2.1.312", True),
        ("2.1.311", "2.1.312", False),
        ("2.2", "2.1.312", True),
        ("2.1", "2.1.0", True),
        ("2.1", "2.1.1", False),
        ("10.0.0", "9.99.99", True),
        (None, "2.1.312", None),
        ("garbage", "2.1.312", None),
    ],
)
def test_version_at_least(text: str | None, minimum: str, ok: bool | None) -> None:
    assert claude_version.at_least(text, minimum) is ok


def test_version_probe_runs_the_realpath_with_the_tool_name() -> None:
    gw = FakeGateway()
    gw.on(["claude", "--version"], stdout="2.1.314 (Claude Code)\nmore\n")
    tool = pathrules.ResolvedTool("/opt/claude/bin/claude-real", "claude")
    assert (
        claude_version.probe(tool, {"PATH": "/usr/bin"}, gw, writable=NOTHING)
        == "2.1.314 (Claude Code)"
    )
    assert gw.calls[0].executable == "/opt/claude/bin/claude-real"


@pytest.mark.parametrize(
    "answer",
    [
        {"returncode": 1, "stdout": "2.1.314"},
        {"stdout": ""},
        {"stdout": "\n\n"},
        {"error": FileNotFoundError("gone")},
        {"timeout": True},
    ],
)
def test_version_probe_failures_are_none(answer: dict[str, Any]) -> None:
    gw = FakeGateway()
    gw.on(["claude", "--version"], **answer)
    assert (
        claude_version.probe(
            pathrules.ResolvedTool("/x/claude", "claude"), PATH_ONLY, gw, writable=NOTHING
        )
        is None
    )


def test_version_probe_strips_control_characters_and_caps_length() -> None:
    gw = FakeGateway()
    gw.on(["claude"], stdout="\x1b[31m2.1.314" + "x" * 500 + "\n")
    text = claude_version.probe(
        pathrules.ResolvedTool("/x/claude", "claude"), PATH_ONLY, gw, writable=NOTHING
    )
    assert text is not None and "\x1b" not in text and len(text) == claude_version.MAX_TEXT


# The read-only session-directory check


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    return root


def test_check_dirs_accepts_a_new_session_and_creates_nothing(tmp_path: Path) -> None:
    root = state_root(tmp_path)
    sessiondir.check_dirs(str(root), dryrun.PLACEHOLDER_ID)
    assert tree(root) == []


def test_check_dirs_accepts_existing_real_directories(tmp_path: Path) -> None:
    root = state_root(tmp_path)
    with sessiondir.SessionDirs.open(str(root), dryrun.PLACEHOLDER_ID) as dirs:
        dirs.prepare_run(1)
    before = tree(root)
    sessiondir.check_dirs(str(root), dryrun.PLACEHOLDER_ID)
    assert tree(root) == before


@pytest.mark.parametrize(
    "where",
    [
        "sessions",
        "private",
        "sessions/cs-aaaaaaaa",
        "private/cs-aaaaaaaa",
        "sessions/cs-aaaaaaaa/work",
    ],
)
def test_check_dirs_refuses_a_symlink(tmp_path: Path, where: str) -> None:
    root = state_root(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    entry = root / where
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.symlink_to(target)
    with pytest.raises(SessionDirUnsafe):
        sessiondir.check_dirs(str(root), dryrun.PLACEHOLDER_ID)


def test_check_dirs_refuses_a_file(tmp_path: Path) -> None:
    root = state_root(tmp_path)
    (root / "sessions").write_text("x")
    with pytest.raises(SessionDirUnsafe) as exc:
        sessiondir.check_dirs(str(root), dryrun.PLACEHOLDER_ID)
    assert exc.value.end_reason == "session_dir_unsafe"


def test_check_dirs_resolves_the_state_root_once_like_open(tmp_path: Path) -> None:
    root = state_root(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(root)
    sessiondir.check_dirs(str(link), dryrun.PLACEHOLDER_ID)
    with pytest.raises(ValueError):
        sessiondir.check_dirs(str(root), "not-an-id")


def test_check_dirs_refuses_a_missing_state_root(tmp_path: Path) -> None:
    with pytest.raises(SessionDirUnsafe):
        sessiondir.check_dirs(str(tmp_path / "missing"), dryrun.PLACEHOLDER_ID)


# Planned own directories (path rules)


def rules(tmp_path: Path, *, planned: bool) -> tuple[pathrules.PathRules, str]:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    session = os.path.join(os.path.realpath(state), "sessions", dryrun.PLACEHOLDER_ID)
    host = pathrules.Host.from_environ(
        {"HOME": str(home), "PATH": "/usr/bin"}, state_root=os.path.realpath(state), path_prepend=[]
    )
    work = os.path.join(session, "work")
    built = pathrules.PathRules(
        host, session_dir=session, cwd=work, caller_cwd=str(tmp_path), planned=planned
    )
    return built, work


def test_a_missing_work_dir_is_refused_unless_planned(tmp_path: Path) -> None:
    unplanned, work = rules(tmp_path, planned=False)
    with pytest.raises(LaunchValidationError):
        unplanned.check_cwd()
    assert not unplanned.planned(work)
    planned, work = rules(tmp_path, planned=True)
    planned.check_cwd()
    assert planned.planned(work)
    assert planned.write_target_problem(os.path.join(work, "sub")) is None


def test_an_existing_symlinked_work_dir_gets_no_planned_exception(tmp_path: Path) -> None:
    _, work = rules(tmp_path, planned=True)
    os.makedirs(os.path.dirname(work))
    target = tmp_path / "state" / "private"  # a symlink into the state root
    target.mkdir()
    os.symlink(target, work)
    planned, _ = rules(tmp_path, planned=True)
    assert not planned.planned(work)
    with pytest.raises(LaunchValidationError):
        planned.check_cwd()


def test_planned_dirs_do_not_widen_other_state_paths(tmp_path: Path) -> None:
    planned, work = rules(tmp_path, planned=True)
    session = os.path.dirname(work)
    assert planned.write_target_problem(os.path.join(session, "secret")) is not None
    assert planned.write_target_problem(os.path.join(session, "work-x")) is not None


# kinds, in process


def process(tmp_path: Path, environ: dict[str, str], gw: FakeGateway) -> launch.Process:
    state = tmp_path / "kstate"
    state.mkdir(exist_ok=True)
    caller = tmp_path / "caller"
    caller.mkdir(exist_ok=True)
    return launch.Process(
        environ=environ,
        caller_cwd=str(caller),
        claude_session="/opt/claude-session/bin/claude-session",
        interpreter="/opt/claude-session/bin/python3",
        state_root=str(state),
        gateway=gw,
    )


def tool(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def normalized(report: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """The machine-dependent parts replaced: the test's paths, and each isolated kind's
    PATH and generated read entries (system, Python install)."""
    text = json.dumps(report).replace(str(tmp_path), "<tmp>")
    data = json.loads(text)
    for profile in data["kinds"]:
        if profile["sandbox"] != "off" or profile["reads_third_party"]:
            profile["read_dirs"] = ["<generated>"]
    return data


def test_kinds_golden(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    claude = tool(bin_dir, "claude")
    tool(bin_dir, "bwrap")
    tool(bin_dir, "socat")
    gw = FakeGateway()
    gw.on(["claude", "--version"], stdout="2.1.314 (Claude Code)\n")
    gw.on([str(bin_dir / "socat")])
    gw.on([str(bin_dir / "bwrap")])
    environ = {"PATH": str(bin_dir), "HOME": str(tmp_path / "home"), "SECRET_X": SECRET}
    report = dryrun.kinds_report(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        process(tmp_path, environ, gw),
        kind=None,
        repo=None,
        prerequisites=dryrun.Prerequisites(gw, platform="linux"),
    )
    validate(report, KINDS)
    assert report["claude_bin_resolved"] == str(claude)
    assert report["claude_version_ok"] is True
    assert SECRET not in json.dumps(report)
    # One probe pair for every sandboxed kind.
    assert [c.argv[0] for c in gw.calls].count(str(bin_dir / "bwrap")) == 1
    assert_golden(normalized(report, tmp_path), "kinds-builtin")


def test_kinds_reports_an_unavailable_sandbox_and_no_claude(tmp_path: Path) -> None:
    gw = FakeGateway()
    report = dryrun.kinds_report(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        process(tmp_path, {"PATH": str(tmp_path / "empty"), "HOME": str(tmp_path)}, gw),
        kind="review",
        repo=None,
        prerequisites=dryrun.Prerequisites(gw, platform="linux"),
    )
    validate(report, KINDS)
    assert report["claude_bin_resolved"] is None and report["claude_version"] is None
    assert report["claude_version_ok"] is None
    assert any("claude not found" in w for w in report["warnings"])
    (review,) = report["kinds"]
    assert review["sandbox_available"] is False and review["sandbox_problems"]
    assert any("a launch fails" in w for w in review["warnings"])
    assert gw.calls == []  # nothing to probe


def test_kinds_unknown_kind_falls_back_to_review(tmp_path: Path) -> None:
    gw = FakeGateway()
    report = dryrun.kinds_report(
        config.load(None, env={"XDG_CONFIG_HOME": str(tmp_path / "none")}),
        process(tmp_path, {"PATH": "", "HOME": str(tmp_path)}, gw),
        kind="mystery",
        repo=None,
        prerequisites=dryrun.Prerequisites(gw, platform="unsupported"),
    )
    validate(report, KINDS)
    (profile,) = report["kinds"]
    assert (profile["name"], profile["fallback"], profile["builtin"]) == ("mystery", True, False)
    assert profile["permission_profile"] == "review"
    assert any("unknown kind 'mystery'" in w for w in report["warnings"])
    assert any("unknown kind 'mystery'" in w for w in profile["warnings"])


def test_kinds_applies_repo_overrides_only_when_given(tmp_path: Path) -> None:
    cfg = load_cfg(
        tmp_path,
        "repos:\n  example.org/team/app:\n    kinds:\n      implement:\n        model: big\n",
    )
    gw = FakeGateway()
    proc = process(tmp_path, {"PATH": "", "HOME": str(tmp_path)}, gw)
    probes = dryrun.Prerequisites(gw, platform="unsupported")
    given = dryrun.kinds_report(
        cfg, proc, kind="implement", repo="example.org/team/app", prerequisites=probes
    )
    plain = dryrun.kinds_report(cfg, proc, kind="implement", repo=None, prerequisites=probes)
    assert given["repo"] == "example.org/team/app" and given["kinds"][0]["model"] == "big"
    assert plain["repo"] is None and plain["kinds"][0]["model"] is None


def test_kinds_shows_session_variables_as_written(tmp_path: Path) -> None:
    opt = tmp_path / "opt"
    opt.mkdir()
    cfg = load_cfg(
        tmp_path,
        f"kinds:\n  implement:\n    read_dirs: ['${{CWD}}/x', '{opt}/../opt']\n"
        "    write_dirs: ['${CSESSION_DIR}/work']\n",
    )
    gw = FakeGateway()
    proc = process(tmp_path, {"PATH": "", "HOME": str(tmp_path)}, gw)
    report = dryrun.kinds_report(
        cfg, proc, kind="implement", repo=None, prerequisites=dryrun.Prerequisites(gw, "linux")
    )
    (profile,) = report["kinds"]
    assert profile["read_dirs"] == ["${CWD}/x", os.path.realpath(opt)]
    assert profile["write_dirs"] == ["${CSESSION_DIR}/work"]


@pytest.mark.parametrize(
    ("root", "platform", "warned"),
    [
        ("/mnt/c/Users/x/state", "wsl2", True),
        ("/mnt/c/Users/x/state", "wsl1", True),
        ("/mnt/c/Users/x/state", "linux", False),
        ("/home/x/.local/state/claude-session", "wsl2", False),
        ("/mntx/state", "wsl2", False),
    ],
)
def test_state_dir_warnings(root: str, platform: str, warned: bool) -> None:
    assert bool(dryrun.state_dir_warnings(root, platform)) is warned


# The claude --version probe's environment and process group (R1)


def test_version_probe_gets_a_minimal_environment_in_its_own_group() -> None:
    gw = FakeGateway()
    gw.on(["claude", "--version"], stdout="2.1.314 (Claude Code)\n")
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "ANTHROPIC_API_KEY": SECRET,
        "MY_SECRET": SECRET,
    }
    claude_version.probe(
        pathrules.ResolvedTool("/x/claude", "claude"), environ, gw, writable=NOTHING
    )
    (call,) = gw.calls
    assert call.env == {"PATH": "/usr/bin", "HOME": "/home/x", "LANG": "C.UTF-8", "LC_ALL": "C"}
    assert call.cwd == "/"
    assert call.new_process_group and call.kill_grace is not None
    assert call.kill_group_on_exit


def test_version_probe_path_drops_relative_and_session_writable_entries(tmp_path: Path) -> None:
    """S5: ``claude`` may be ``#!/usr/bin/env node``: the probe's ``PATH`` has no entry a
    session could write (checked hop by hop), so a planted interpreter is never found."""
    safe, unsafe = tmp_path / "safe", tmp_path / "unsafe"
    safe.mkdir()
    (unsafe / "bin").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(unsafe / "bin")
    gw = FakeGateway()
    gw.on(["claude", "--version"], stdout="2.1.314 (Claude Code)\n")
    environ = {"PATH": f"rel:{unsafe / 'bin'}::{link}:{safe}"}
    writable = pathrules.SessionWritable([str(unsafe)])
    claude_version.probe(
        pathrules.ResolvedTool("/x/claude", "claude"), environ, gw, writable=writable
    )
    (call,) = gw.calls
    assert call.env is not None and call.env["PATH"] == str(safe)


def test_version_probe_with_no_safe_path_entry_runs_nothing(tmp_path: Path) -> None:
    gw = FakeGateway()
    writable = pathrules.SessionWritable([str(tmp_path)])
    found = pathrules.ResolvedTool("/x/claude", "claude")
    assert claude_version.probe(found, {"PATH": str(tmp_path)}, gw, writable=writable) is None
    assert gw.calls == []


def _gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rpartition(")")[2].split()[0] == "Z"
    except FileNotFoundError:
        return True


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc")
def test_a_hanging_version_probe_leaves_no_process(tmp_path: Path) -> None:
    from claude_session.gateway import SystemGateway

    pid_file = tmp_path / "pid"
    claude = tool(tmp_path / "bin", "claude")
    claude.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {pid_file}\nwait\n")
    environ = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    found = pathrules.ResolvedTool(str(claude), "claude")
    assert (
        claude_version.probe(found, environ, SystemGateway(), writable=NOTHING, timeout=0.5) is None
    )
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(pid)


# The session directories as a launch opens them (R4, R5)


@pytest.mark.parametrize("top", ["sessions", "private"])
@pytest.mark.parametrize("mode", [0o000, 0o600])
def test_check_dirs_accepts_a_mode_the_launch_repairs(tmp_path: Path, top: str, mode: int) -> None:
    root = state_root(tmp_path)
    with sessiondir.SessionDirs.open(str(root), dryrun.PLACEHOLDER_ID) as dirs:
        dirs.prepare_run(1)
    (root / top).chmod(mode)
    try:
        sessiondir.check_dirs(str(root), dryrun.PLACEHOLDER_ID)
        assert stat.S_IMODE((root / top).lstat().st_mode) == mode  # nothing repaired
        # The launch repairs it and succeeds.
        with sessiondir.SessionDirs.open(str(root), dryrun.PLACEHOLDER_ID) as dirs:
            dirs.prepare_run(1)
    finally:
        (root / top).chmod(0o700)


def launch_error(root: Path) -> str:
    with pytest.raises(SessionDirUnsafe) as exc:
        with sessiondir.SessionDirs.open(str(root), dryrun.PLACEHOLDER_ID) as dirs:
            dirs.prepare_run(1)
    return str(exc.value)


@pytest.mark.parametrize(
    "where",
    [
        "sessions",
        "private",
        "sessions/cs-aaaaaaaa",
        "private/cs-aaaaaaaa",
        "sessions/cs-aaaaaaaa/work",
        "sessions/cs-aaaaaaaa/inputs",
    ],
)
@pytest.mark.parametrize("entry", ["symlink", "file"])
def test_check_dirs_fails_with_the_launch_message(tmp_path: Path, where: str, entry: str) -> None:
    root = state_root(tmp_path)
    path = root / where
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if entry == "symlink":
        target = tmp_path / "elsewhere"
        target.mkdir()
        path.symlink_to(target)
    else:
        path.write_text("x")
    with pytest.raises(SessionDirUnsafe) as exc:
        sessiondir.check_dirs(str(root), dryrun.PLACEHOLDER_ID)
    assert str(exc.value) == launch_error(root)


# Interactive TTY checks (R10)


class Stream:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def parsed(*argv: str) -> Any:
    from claude_session import cli

    return cli.build_parser().parse_args(list(argv))


@pytest.mark.parametrize(
    ("argv", "stdin", "stdout", "refused"),
    [
        (["exec", "--dry-run"], False, False, True),
        (["exec", "--dry-run"], True, False, True),
        (["exec", "--dry-run"], True, True, False),
        (["start", "--dry-run", "--json"], False, False, False),
        (["exec", "--dry-run", "--prompt-file", "-"], True, True, True),
        (["start", "--dry-run", "--json", "--prompt-file", "-"], True, False, True),
    ],
)
def test_interactive_tty_checks(
    monkeypatch: pytest.MonkeyPatch,
    cfg: config.Config,
    argv: list[str],
    stdin: bool,
    stdout: bool,
    refused: bool,
) -> None:
    monkeypatch.setattr("sys.stdin", Stream(stdin))
    monkeypatch.setattr("sys.stdout", Stream(stdout))
    args = parsed(*argv)
    if refused:
        with pytest.raises(UsageError):
            dryrun.request(args, cfg)
    else:
        assert dryrun.request(args, cfg).mode == "interactive"


def test_a_headless_dry_run_needs_no_terminal(
    monkeypatch: pytest.MonkeyPatch, cfg: config.Config, tmp_path: Path
) -> None:
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    monkeypatch.setattr("sys.stdin", Stream(False))
    args = parsed("exec", "--dry-run", "--mode", "headless", "--prompt-file", str(prompt))
    assert dryrun.request(args, cfg).mode == "headless"


# origins of env.allow (R11)


@pytest.mark.parametrize(
    ("flags", "origin"),
    [
        ({}, "builtin"),
        ({"env_allow": ["EXTRA"]}, "flag"),
        ({"env_allowlist_only": ["ONLY"]}, "flag"),
    ],
)
def test_env_allow_origin_follows_the_flags(
    cfg: config.Config, flags: dict[str, Any], origin: str
) -> None:
    resolution = kinds.resolve(
        cfg, "implement", mode="headless", flags=flags or None, origin_url=lambda _: None
    )
    assert dryrun.origins_json(resolution, "headless")["env.allow"] == origin


# JSON output (R12)


def test_printable_escapes_strings_that_are_not_utf8() -> None:
    odd = os.fsdecode(b"/x/odd\xff")
    shown = dryrun.printable({"cwd": odd, "list": [odd, 1, None], "ok": "fine"})
    assert shown == {"cwd": "/x/odd\\udcff", "list": ["/x/odd\\udcff", 1, None], "ok": "fine"}
    json.dumps(shown, ensure_ascii=False).encode("utf-8")
