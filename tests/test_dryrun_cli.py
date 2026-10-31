"""``exec``/``start --dry-run`` and ``kinds`` end to end through the ``claude-session`` entry
point (interface.md, Dry run and kinds): schema-valid output, exit codes, environment names
only, and no side effects (state root, tmp root, ``HOME`` and the cwd unchanged).

The sandbox prerequisites are fake ``bwrap`` and ``socat`` scripts that exit 0, so the
results do not depend on the machine."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from .fakeclaude import claude_bin, write_json
from .schemas import validate
from .test_exec_headless import claude_session_cmd

DRY_RUN = "claude-session/cli/exec-dry-run.json"
KINDS = "claude-session/cli/kinds.json"
ERROR = "claude-session/cli/error.json"
SECRET = "sk-test-secret-value-0123456789abcdef"
SET_VALUE = "set-value-never-printed-42"
ISOLATED = ["review", "gate-check", "respond", "rank"]


@pytest.fixture
def state(_private_state_dir: Path) -> Path:
    return _private_state_dir / "claude-session"


@pytest.fixture
def tools(tmp_path: Path) -> Path:
    """A directory with fake ``claude``, ``bwrap`` and ``socat``."""
    directory = tmp_path / "tools"
    claude_bin(directory, write_json(tmp_path / "scenario.json", {"steps": []}))
    for name in ("bwrap", "socat"):
        path = directory / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    return directory


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    (path / "prompt.md").write_text("Do it.\n", encoding="utf-8")
    return path


def git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        timeout=30,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def cli(
    args: Sequence[str],
    *,
    cwd: Path,
    tools: Path,
    tmp_path: Path,
    config: str = "",
    env: Mapping[str, str] | None = None,
    with_config: bool = True,
) -> subprocess.CompletedProcess[str]:
    cfg = tmp_path / "config.yaml"
    default = "" if "claude_bin:" in config else "claude_bin: claude\n"
    cfg.write_text(default + config, encoding="utf-8")
    environ = {**os.environ, "PATH": f"{tools}:/usr/bin:/bin", **(env or {})}
    return subprocess.run(
        [*claude_session_cmd(), *args, *(["--config", str(cfg)] if with_config else [])],
        cwd=cwd,
        env=environ,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def dry_run(
    kind: str, *extra: str, command: str = "exec", **kw: Any
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    proc = cli(
        [
            command,
            "--dry-run",
            "--json",
            "--kind",
            kind,
            "--mode",
            "headless",
            "--prompt-file",
            "prompt.md",
            *extra,
        ],
        **kw,
    )
    assert proc.stdout, proc.stderr
    plan = json.loads(proc.stdout)
    validate(plan, DRY_RUN)
    return proc, plan


# Side effects


def snapshot(root: Path) -> dict[str, Any]:
    """Every entry under ``root`` (type, mode, size of regular files; database rows for
    ``state.db``)."""
    if not root.exists():
        return {}
    found: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        rel = str(path.relative_to(root))
        if path.name in ("state.db-wal", "state.db-shm"):
            continue
        if path.name == "state.db":
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                found[rel] = list(db.iterdump())
            finally:
                db.close()
        elif path.is_symlink():
            found[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            found[rel] = ("dir", info.st_mode)
        else:
            found[rel] = ("file", info.st_mode, info.st_size)
    return found


@pytest.mark.parametrize("kind", ISOLATED + ["implement"])
def test_a_dry_run_has_no_side_effects(
    kind: str, tmp_path: Path, state: Path, tools: Path, work: Path, private_tmp_root: Path
) -> None:
    git_repo(work)
    # The schema check of any command creates the state root and state.db (launch step 1).
    first = cli(["list", "--json"], cwd=work, tools=tools, tmp_path=tmp_path)
    assert first.returncode == 0, first.stderr
    roots = [state, private_tmp_root, Path(os.environ["HOME"]), work]
    before = [snapshot(root) for root in roots]
    proc, plan = dry_run(kind, cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 0, plan["warnings"]
    assert plan["would_fail"] is None and plan["would_refuse"] is None
    assert [snapshot(root) for root in roots] == before
    assert not (state / "audit.jsonl").exists()  # log.jsonl: unchanged (snapshot)


def test_snapshot_sees_a_new_session_row(
    tmp_path: Path, state: Path, tools: Path, work: Path
) -> None:
    """The side-effect check can fail: a real launch's allocation shows up."""
    cli(["list", "--json"], cwd=work, tools=tools, tmp_path=tmp_path)
    before = snapshot(state)
    real = cli(
        ["exec", "--json", "--mode", "headless", "--prompt-file", "prompt.md", "--cwd", "/"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )
    assert real.returncode != 0
    assert snapshot(state) != before


# The plans of the isolated kinds


@pytest.mark.parametrize("kind", ISOLATED)
def test_isolated_kind_plans(kind: str, tmp_path: Path, tools: Path, work: Path) -> None:
    git_repo(work)
    proc, plan = dry_run(kind, cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 0, plan["warnings"]
    assert plan["csession_id"] is None and plan["run"] == 1
    assert (plan["launch_kind"], plan["run_kind"]) == (kind, kind)
    settings = plan["settings"]
    assert settings["permissions"]["defaultMode"] == "dontAsk"
    assert settings["sandbox"]["enabled"] is True
    assert plan["sandbox"]["available"] is True
    assert plan["sandbox"]["allow_read"] == settings["sandbox"]["filesystem"]["allowRead"]
    assert plan["claude_argv"][-2] == "--"
    assert "-p" in plan["claude_argv"]
    assert plan["taints"] is (kind != "rank")
    session = plan["claude_argv"][plan["claude_argv"].index("--add-dir") + 1]
    assert session.endswith("/sessions/cs-aaaaaaaa")  # the placeholder id
    if kind == "respond":
        assert plan["cwd"] == str(work.resolve())
        assert "GIT_CONFIG_GLOBAL" in plan["env_names"]
    else:
        assert plan["cwd"] == f"{session}/work"
    tmp = settings["sandbox"]["filesystem"]["allowWrite"][-1]
    assert tmp.endswith(f"/csession-{os.geteuid()}/cs-aaaaaaaa")
    assert {"CSESSION_ID", "CSESSION_DIR", "CSESSION_KIND", "TMPDIR"} <= set(plan["env_names"])


def test_start_dry_run_is_the_same_plan(tmp_path: Path, tools: Path, work: Path) -> None:
    _, exec_plan = dry_run("review", cwd=work, tools=tools, tmp_path=tmp_path)
    proc, start_plan = dry_run("review", command="start", cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 0
    assert start_plan == exec_plan


def test_an_interactive_dry_run_uses_the_interactive_timeout(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    # start: an interactive exec without a terminal exits 2 (test_interactive_tty_checks).
    proc = cli(
        ["start", "--dry-run", "--json", "--timeout", "5m"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )
    plan = json.loads(proc.stdout)
    validate(plan, DRY_RUN)
    assert proc.returncode == 0, plan["warnings"]
    assert plan["mode"] == "interactive" and plan["timeout"] == 300.0
    assert plan["profile"]["interactive_timeout"] == 300.0
    assert plan["origins"]["timeout"] == "flag"
    assert "-p" not in plan["claude_argv"]
    assert plan["has_prompt"] is False and plan["claude_argv"][-1] != "--"


# Environment values


def test_a_dry_run_never_prints_environment_values(tmp_path: Path, tools: Path, work: Path) -> None:
    config = f"kinds:\n  implement:\n    env:\n      set: {{SET_ME: {SET_VALUE}}}\n"
    for json_flag in (["--json"], []):
        proc = cli(
            [
                "exec",
                "--dry-run",
                *json_flag,
                "--mode",
                "headless",
                "--prompt-file",
                "prompt.md",
                "--label",
                "team=a",
                "--env-allow",
                "EXTRA_NAME",
            ],
            cwd=work,
            tools=tools,
            tmp_path=tmp_path,
            config=config,
            env={"ANTHROPIC_API_KEY": SECRET, "EXTRA_NAME": SECRET, "OTHER": SECRET},
        )
        assert proc.returncode == 0, proc.stderr
        for stream in (proc.stdout, proc.stderr):
            assert SECRET not in stream and SET_VALUE not in stream
        if json_flag:
            plan = json.loads(proc.stdout)
            assert {"ANTHROPIC_API_KEY", "SET_ME", "OTHER"} <= set(plan["env_names"])
            assert plan["profile"]["required_env_names"] == [
                "ANTHROPIC_API_KEY",
                "EXTRA_NAME",
                "SET_ME",
            ]
            assert plan["profile"]["env"]["set_names"] == ["SET_ME"]
            assert plan["origins"]["env.set_names"] == "config"
            assert plan["labels"] == ["kind=implement", "team=a"]


# would_fail


def test_respond_outside_git_would_fail_no_git_cwd(tmp_path: Path, tools: Path, work: Path) -> None:
    proc, plan = dry_run("respond", cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 12
    assert plan["would_fail"] == "no_git_cwd" and plan["would_refuse"] is None
    assert plan["settings"] == {} and plan["claude_argv"] == [] and plan["env_names"] == []
    assert any(w.startswith("a launch would fail (no_git_cwd)") for w in plan["warnings"])


def test_an_isolated_run_in_home_would_fail_unsafe_cwd(tmp_path: Path, tools: Path) -> None:
    home = Path(os.environ["HOME"])
    (home / "prompt.md").write_text("x", encoding="utf-8")
    proc, plan = dry_run(
        "implement", "--sandbox", "preferred", cwd=home, tools=tools, tmp_path=tmp_path
    )
    assert proc.returncode == 12
    assert plan["would_fail"] == "unsafe_cwd"


def test_a_required_sandbox_without_prerequisites_would_fail(tmp_path: Path, work: Path) -> None:
    bare = tmp_path / "bare"
    claude_bin(bare, write_json(tmp_path / "s.json", {"steps": []}))
    proc = cli(
        [
            "exec",
            "--dry-run",
            "--json",
            "--kind",
            "review",
            "--mode",
            "headless",
            "--prompt-file",
            "prompt.md",
        ],
        cwd=work,
        tools=bare,
        tmp_path=tmp_path,
        env={"PATH": str(bare)},
    )
    plan = json.loads(proc.stdout)
    validate(plan, DRY_RUN)
    assert proc.returncode == 12
    assert plan["would_fail"] == "sandbox_unavailable"
    assert plan["sandbox"]["available"] is False and plan["sandbox"]["problems"]


def test_a_missing_claude_would_fail(tmp_path: Path, tools: Path, work: Path) -> None:
    proc, plan = dry_run(
        "implement", cwd=work, tools=tools, tmp_path=tmp_path, config="claude_bin: no-such-claude\n"
    )
    assert proc.returncode == 12 and plan["would_fail"] == "claude_not_found"


def test_a_human_dry_run_summarizes_and_escapes(tmp_path: Path, tools: Path) -> None:
    odd = tmp_path / "odd\x1b[31mdir"
    odd.mkdir()
    (odd / "prompt.md").write_text("x", encoding="utf-8")
    proc = cli(
        ["exec", "--dry-run", "--mode", "headless", "--prompt-file", "prompt.md"],
        cwd=odd,
        tools=tools,
        tmp_path=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "\x1b" not in proc.stdout and "odd\\x1b[31mdir" in proc.stdout
    assert "dry run: kind implement, mode headless" in proc.stdout
    assert "result: a launch would pass" in proc.stdout


def test_a_human_dry_run_names_the_failure(tmp_path: Path, tools: Path, work: Path) -> None:
    proc = cli(
        [
            "exec",
            "--dry-run",
            "--kind",
            "respond",
            "--mode",
            "headless",
            "--prompt-file",
            "prompt.md",
        ],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )
    assert proc.returncode == 12
    assert "result: a launch would fail (no_git_cwd)" in proc.stdout


# Usage, refusal and not-yet-implemented paths


def error_of(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    envelope = json.loads(proc.stdout)
    validate(envelope, ERROR)
    return envelope


def test_a_dry_run_still_applies_usage_checks(tmp_path: Path, tools: Path, work: Path) -> None:
    proc = cli(
        ["exec", "--dry-run", "--json", "--mode", "headless"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )
    assert proc.returncode == 2 and error_of(proc)["error"]["code"] == "usage"
    proc = cli(
        ["exec", "--dry-run", "--json", "--cwd", str(tmp_path / "missing")],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )
    assert proc.returncode == 2
    proc = cli(["start", "--dry-run"], cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 2  # start requires --json


@pytest.mark.parametrize(
    "args",
    [
        ["resume", "cs-abcd2345", "--dry-run", "--json"],
        ["start", "--resume", "cs-abcd2345", "--dry-run", "--json"],
        ["exec", "--session-id", "cs-abcd2345", "--dry-run", "--json"],
    ],
)
def test_dry_runs_of_existing_sessions_are_not_implemented_yet(
    args: list[str], tmp_path: Path, tools: Path, work: Path
) -> None:
    adoption = "--session-id" in args  # accepts no --config
    proc = cli(args, cwd=work, tools=tools, tmp_path=tmp_path, with_config=not adoption)
    assert proc.returncode == 1
    assert error_of(proc)["error"]["code"] == "internal"


# kinds


def test_kinds_json(tmp_path: Path, tools: Path, work: Path) -> None:
    proc = cli(["kinds", "--json"], cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    validate(report, KINDS)
    assert report["claude_bin_resolved"] == str((tools / "claude").resolve())
    assert report["claude_version"] == "2.1.312 (Claude Code)"
    assert report["claude_version_ok"] is True
    names = [k["name"] for k in report["kinds"]]
    assert names == [
        "implement",
        "draft-ticket",
        "audit",
        "improve",
        "rank",
        "review",
        "gate-check",
        "respond",
    ]
    by_name = {k["name"]: k for k in report["kinds"]}
    assert by_name["implement"]["sandbox_available"] is None
    assert by_name["review"]["sandbox_available"] is True


def test_kinds_reports_an_old_claude(tmp_path: Path, tools: Path, work: Path) -> None:
    # The probe gets a minimal environment, so the fake's version comes from its scenario.
    write_json(tmp_path / "scenario.json", {"steps": [], "version": "2.0.1 (Claude Code)"})
    proc = cli(
        ["kinds", "--json", "--kind", "review"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )
    report = json.loads(proc.stdout)
    validate(report, KINDS)
    assert report["claude_version"] == "2.0.1 (Claude Code)"
    assert report["claude_version_ok"] is False
    assert [k["name"] for k in report["kinds"]] == ["review"]


def test_kinds_human_output(tmp_path: Path, tools: Path, work: Path) -> None:
    proc = cli(["kinds", "--kind", "mystery"], cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "mystery: profile review, sandbox required available" in proc.stdout
    assert "unknown kind 'mystery'" in proc.stdout


def test_kinds_has_no_side_effects(
    tmp_path: Path, state: Path, tools: Path, work: Path, private_tmp_root: Path
) -> None:
    cli(["list", "--json"], cwd=work, tools=tools, tmp_path=tmp_path)
    roots = [state, private_tmp_root, Path(os.environ["HOME"]), work]
    before = [snapshot(root) for root in roots]
    proc = cli(["kinds", "--json"], cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert [snapshot(root) for root in roots] == before


def test_secret_values_too_large_to_index_are_an_invalid_config(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    big = "k" * 100_000
    proc = cli(
        ["exec", "--dry-run", "--json", "--mode", "headless", "--prompt-file", "prompt.md"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        env={
            "ANTHROPIC_API_KEY": "a" + big,
            "ANTHROPIC_AUTH_TOKEN": "b" + big,
            "ANTHROPIC_MODEL": "c" + big,
        },
    )
    assert proc.returncode == 2
    assert error_of(proc)["error"]["code"] == "usage"
    assert big not in proc.stdout and big not in proc.stderr


def test_a_profile_file_is_shown_by_its_hash(tmp_path: Path, tools: Path, work: Path) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text('{"permissions": {"allow": ["Read"]}}', encoding="utf-8")
    proc, plan = dry_run(
        "implement", "--permission-profile", str(profile), cwd=work, tools=tools, tmp_path=tmp_path
    )
    assert proc.returncode == 0, plan["warnings"]
    assert plan["permission_profile"].startswith("file:")
    assert plan["profile"]["permission_profile"] == plan["permission_profile"]
    assert plan["origins"]["permission_profile_file"] == "flag"
    assert "Read" in plan["settings"]["permissions"]["allow"]


# Read-only probes never run a binary a launch would refuse (R1)


def plant(directory: Path, marker: Path) -> None:
    """``claude``, ``bwrap`` and ``socat`` that record that they ran."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "bwrap", "socat"):
        path = directory / name
        path.write_text(f"#!/bin/sh\necho {name} >> {marker}\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)


def test_kinds_never_runs_a_binary_under_the_caller_cwd(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    marker = tmp_path / "ran"
    plant(work / "bin", marker)
    proc = cli(
        ["kinds", "--json"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        env={"PATH": f"{work / 'bin'}:{tools}:/usr/bin:/bin", "ANTHROPIC_API_KEY": SECRET},
    )
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
    report = json.loads(proc.stdout)
    validate(report, KINDS)
    assert report["claude_bin_resolved"] is None and report["claude_version_ok"] is None
    assert any("session-writable" in w and "not run" in w for w in report["warnings"])
    review = next(k for k in report["kinds"] if k["name"] == "review")
    assert review["sandbox_available"] is False
    assert any("not run" in p for p in review["sandbox_problems"])


def test_a_failing_dry_run_never_runs_a_planted_sandbox_tool(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    marker = tmp_path / "ran"
    plant(work / "bin", marker)
    proc, plan = dry_run(
        "respond",
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        env={"PATH": f"{work / 'bin'}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 12 and plan["would_fail"] is not None
    assert not marker.exists()
    assert plan["sandbox"]["available"] is False
    assert any("not run" in p for p in plan["sandbox"]["problems"])


def logging_tools(directory: Path, log: Path) -> None:
    for name in ("bwrap", "socat"):
        path = directory / name
        path.write_text(f"#!/bin/sh\necho {name} >> {log}\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)


def test_a_passing_dry_run_probes_the_sandbox_once(tmp_path: Path, tools: Path, work: Path) -> None:
    """The plan's sandbox state comes from the launch's self-check: no second probe."""
    log = tmp_path / "probes"
    logging_tools(tools, log)
    proc, plan = dry_run("review", cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 0, plan["warnings"]
    assert plan["sandbox"]["available"] is True
    assert sorted(log.read_text(encoding="utf-8").split()) == ["bwrap", "socat"]


# Tools under the permission profile's write paths (V1) and trusted kinds (V2)


def wide_profile(path: Path, scratch: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "sandbox": {"filesystem": {"allowWrite": [str(scratch)]}},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("in_git", [False, True])
def test_a_failing_dry_run_never_runs_a_tool_under_a_profile_write_path(
    in_git: bool, tmp_path: Path, tools: Path, work: Path
) -> None:
    """A launch counts the profile's ``allowWrite`` as session-writable and refuses a tool
    found under it; the dry run's probes never run it (no git: ``no_git_cwd``; in git: the
    launch's own ``unsafe_path``)."""
    if in_git:
        git_repo(work)
    marker = tmp_path / "ran"
    scratch = tmp_path / "scratch"
    plant(scratch / "bin", marker)
    profile = wide_profile(tmp_path / "profile.json", scratch)
    proc, plan = dry_run(
        "respond",
        "--permission-profile",
        str(profile),
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        env={"PATH": f"{scratch / 'bin'}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 12
    assert plan["would_fail"] == ("unsafe_path" if in_git else "no_git_cwd")
    assert not marker.exists()
    assert plan["sandbox"]["available"] is False
    assert any("a launch refuses it" in p for p in plan["sandbox"]["problems"])


def test_an_unread_profile_file_leaves_the_sandbox_not_probed(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    log = tmp_path / "probes"
    logging_tools(tools, log)
    proc, plan = dry_run(
        "review",
        "--permission-profile",
        str(tmp_path / "missing.json"),
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )
    assert proc.returncode == 12 and plan["would_fail"] == "invalid_permission_profile"
    assert not log.exists()
    assert plan["sandbox"]["available"] is False
    assert any("not probed by a dry run" in p for p in plan["sandbox"]["problems"])
    assert not any("sandbox 'required' but unavailable" in w for w in plan["warnings"])


def test_kinds_never_runs_a_tool_under_a_configured_profile_write_path(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    marker = tmp_path / "ran"
    scratch = tmp_path / "scratch"
    plant(scratch / "bin", marker)
    config = (
        "permission_profiles:\n  wide:\n    sandbox:\n      filesystem:\n"
        f"        allowWrite: ['{scratch}']\n"
        "kinds:\n  review:\n    permission_profile: wide\n"
    )
    proc = cli(
        ["kinds", "--json", "--kind", "review"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=config,
        env={"PATH": f"{scratch / 'bin'}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
    report = json.loads(proc.stdout)
    validate(report, KINDS)
    assert report["claude_bin_resolved"] is None
    assert any("a launch refuses it" in w for w in report["warnings"])
    (review,) = report["kinds"]
    assert review["sandbox_available"] is False
    assert any("a launch refuses it" in p for p in review["sandbox_problems"])


def test_kinds_words_a_trusted_kinds_unrun_claude_as_not_probed(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    """A launch of a trusted kind checks no session-writable path, so it would not refuse a
    ``claude`` under a trusted kind's ``write_dirs``; ``kinds`` still never runs it, and says
    so."""
    marker = tmp_path / "ran"
    planted = home() / "td" / "bin"
    plant(planted, marker)
    proc = cli(
        ["kinds", "--json", "--kind", "implement"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config="kinds:\n  implement:\n    write_dirs: ['~/td']\n",
        env={"PATH": f"{planted}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
    report = json.loads(proc.stdout)
    validate(report, KINDS)
    claude = [w for w in report["warnings"] if w.startswith("claude:")]
    assert claude and all("not probed by a dry run" in w for w in claude), claude
    assert not any("a launch refuses it" in w for w in claude)


def test_kinds_refuses_a_claude_under_any_isolated_kinds_cwd(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    """S4: ``claude_bin`` under the caller's cwd is refused by a launch of an isolated kind
    whose cwd is the caller's (``respond``), also when ``--kind`` names a trusted kind."""
    marker = tmp_path / "ran"
    plant(work / "bin", marker)
    proc = cli(
        ["kinds", "--json", "--kind", "implement"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        env={"PATH": f"{work / 'bin'}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
    report = json.loads(proc.stdout)
    assert any("a launch refuses it" in w for w in report["warnings"]), report["warnings"]


# Tools under a ${GIT_DIR} write path (G1, G2)


def separate_git_dir(work: Path, git_dir: Path) -> None:
    """``work`` as a repository whose git directory is ``git_dir`` (outside the cwd)."""
    subprocess.run(
        ["git", "init", "-q", f"--separate-git-dir={git_dir}", str(work)],
        check=True,
        timeout=30,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )


# A ${GIT_DIR}/tools write entry in each place a run's write paths come from: the config
# (write_dirs) and the permission profile (sandbox allowWrite, an Edit allow rule).
GIT_DIR_ENTRIES = ["write_dirs", "allowWrite", "edit_rule"]


def git_dir_profile(entry: str) -> dict[str, Any]:
    if entry == "allowWrite":
        return {
            "permissions": {"allow": ["Read"]},
            "sandbox": {"filesystem": {"allowWrite": ["${GIT_DIR}/tools"]}},
        }
    return {"permissions": {"allow": ["Read", "Edit(/${GIT_DIR}/tools/**)"]}}


def git_dir_config(kind: str, entry: str) -> str:
    """Config giving ``kind`` the ``entry`` form of a ``${GIT_DIR}/tools`` write path."""
    if entry == "write_dirs":
        return f"kinds:\n  {kind}:\n    write_dirs: ['${{GIT_DIR}}/tools']\n"
    profile = json.dumps(git_dir_profile(entry))
    return (
        f"permission_profiles:\n  gitdir: {profile}\n"
        f"kinds:\n  {kind}:\n    permission_profile: gitdir\n"
    )


@pytest.mark.parametrize("entry", ["profile_file", *GIT_DIR_ENTRIES])
def test_a_failing_dry_run_never_runs_a_tool_under_a_git_dir_write_path(
    entry: str, tmp_path: Path, tools: Path, work: Path
) -> None:
    """The launch adds the ``${GIT_DIR}`` entries once git found the git directory and
    refuses a tool under them (``unsafe_path``); the dry run's probes never run it."""
    git_dir = tmp_path / "gd"
    separate_git_dir(work, git_dir)
    marker = tmp_path / "ran"
    plant(git_dir / "tools" / "bin", marker)
    extra: list[str] = []
    config = ""
    if entry == "profile_file":
        profile = tmp_path / "profile.json"
        profile.write_text(json.dumps(git_dir_profile("allowWrite")), encoding="utf-8")
        extra = ["--permission-profile", str(profile)]
    else:
        config = git_dir_config("respond", entry)
    proc, plan = dry_run(
        "respond",
        *extra,
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=config,
        env={"PATH": f"{git_dir / 'tools' / 'bin'}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 12
    assert plan["would_fail"] == "unsafe_path", plan["warnings"]
    assert not marker.exists()
    assert plan["sandbox"]["available"] is False
    assert any("a launch refuses it" in p for p in plan["sandbox"]["problems"])


@pytest.mark.parametrize("entry", GIT_DIR_ENTRIES)
def test_kinds_never_runs_a_tool_under_a_git_dir_write_path(
    entry: str, tmp_path: Path, tools: Path, work: Path
) -> None:
    """``kinds`` never knows the git directory: a ``${GIT_DIR}`` write entry leaves the
    kind's session-writable paths unknown, so neither ``claude`` nor the sandbox
    prerequisites are probed."""
    git_dir = tmp_path / "gd"
    separate_git_dir(work, git_dir)
    marker = tmp_path / "ran"
    plant(git_dir / "tools" / "bin", marker)
    proc = cli(
        ["kinds", "--json", "--kind", "respond"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=git_dir_config("respond", entry),
        env={"PATH": f"{git_dir / 'tools' / 'bin'}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
    report = json.loads(proc.stdout)
    validate(report, KINDS)
    assert report["claude_bin_resolved"] is None and report["claude_version"] is None
    assert any("not probed by a dry run" in w for w in report["warnings"])
    (respond,) = report["kinds"]
    assert respond["sandbox_available"] is False
    assert any("not probed by a dry run" in p for p in respond["sandbox_problems"])


@pytest.mark.parametrize("entry", GIT_DIR_ENTRIES)
@pytest.mark.parametrize("kind", ISOLATED)
def test_a_git_dir_write_path_never_lets_a_tool_run(
    kind: str, entry: str, tmp_path: Path, tools: Path, work: Path
) -> None:
    """The general rule, for every isolated kind and every source of a write path: while
    the git directory is not known (``kinds``), a ``${GIT_DIR}`` write entry means nothing
    is probed; a dry run (which knows it once git ran) never runs a tool under it."""
    git_dir = tmp_path / "gd"
    separate_git_dir(work, git_dir)
    marker = tmp_path / "ran"
    plant(git_dir / "tools" / "bin", marker)
    path = f"{git_dir / 'tools' / 'bin'}:{tools}:/usr/bin:/bin"
    config = git_dir_config(kind, entry)
    proc = cli(
        ["kinds", "--json", "--kind", kind],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=config,
        env={"PATH": path},
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    validate(report, KINDS)
    assert report["claude_bin_resolved"] is None
    (row,) = report["kinds"]
    assert any("not probed by a dry run" in p for p in row["sandbox_problems"])
    assert not marker.exists()
    run = cli(
        ["exec", "--dry-run", "--json", "--kind", kind, "--mode", "headless"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=config,
        env={"PATH": path},
    )
    assert run.returncode in (2, 12), run.stdout
    assert not marker.exists()


@pytest.mark.parametrize("entry", GIT_DIR_ENTRIES)
def test_a_dry_run_failing_before_git_probes_no_tool_under_a_git_dir_write_path(
    entry: str, tmp_path: Path, tools: Path
) -> None:
    """A dry run whose checks fail before git runs (``unsafe_cwd``: the cwd is ``$HOME``)
    does not know the git directory: nothing is probed."""
    home = Path(os.environ["HOME"])
    (home / "prompt.md").write_text("Do it.\n", encoding="utf-8")
    marker = tmp_path / "ran"
    plant(tmp_path / "gd" / "tools" / "bin", marker)
    proc, plan = dry_run(
        "respond",
        cwd=home,
        tools=tools,
        tmp_path=tmp_path,
        config=git_dir_config("respond", entry),
        env={"PATH": f"{tmp_path / 'gd' / 'tools' / 'bin'}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 12 and plan["would_fail"] == "unsafe_cwd"
    assert not marker.exists()
    assert plan["sandbox"]["available"] is False
    assert any("not probed by a dry run" in p for p in plan["sandbox"]["problems"])


# permission_profile when a later check fails (R2)


def test_a_read_profile_file_is_kept_when_the_sandbox_check_fails(
    tmp_path: Path, work: Path
) -> None:
    bare = tmp_path / "bare"
    claude_bin(bare, write_json(tmp_path / "s.json", {"steps": []}))
    profile = tmp_path / "profile.json"
    profile.write_text('{"permissions": {"allow": ["Read"]}}', encoding="utf-8")
    proc, plan = dry_run(
        "review",
        "--permission-profile",
        str(profile),
        cwd=work,
        tools=bare,
        tmp_path=tmp_path,
        env={"PATH": str(bare)},
    )
    assert proc.returncode == 12 and plan["would_fail"] == "sandbox_unavailable"
    assert plan["permission_profile"].startswith("file:")
    assert plan["profile"]["permission_profile"] == plan["permission_profile"]
    assert plan["origins"]["permission_profile"] == "flag"


def test_a_read_profile_file_is_kept_when_there_is_no_git_cwd(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text('{"permissions": {"allow": ["Read"]}}', encoding="utf-8")
    proc, plan = dry_run(
        "respond", "--permission-profile", str(profile), cwd=work, tools=tools, tmp_path=tmp_path
    )
    assert proc.returncode == 12 and plan["would_fail"] == "no_git_cwd"
    assert plan["permission_profile"].startswith("file:")
    assert plan["profile"]["permission_profile"] == plan["permission_profile"]


def test_an_unreadable_profile_file_names_no_builtin_profile(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    missing = str(tmp_path / "missing.json")
    proc, plan = dry_run(
        "review", "--permission-profile", missing, cwd=work, tools=tools, tmp_path=tmp_path
    )
    assert proc.returncode == 12 and plan["would_fail"] == "invalid_permission_profile"
    assert plan["permission_profile"] is None
    assert plan["profile"]["permission_profile"] == missing
    assert plan["origins"]["permission_profile"] == "flag"


# claude_argv (R6)


def test_claude_argv_starts_with_the_bare_name(tmp_path: Path, tools: Path, work: Path) -> None:
    proc, plan = dry_run("review", cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 0, plan["warnings"]
    assert plan["claude_argv"][0] == "claude"


# --outcome-file (R7)


def test_a_dry_run_writes_only_a_usage_error_to_the_outcome_file(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    usage = tmp_path / "usage.json"
    proc = cli(
        ["exec", "--dry-run", "--json", "--mode", "headless", "--outcome-file", str(usage)],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
    )
    assert proc.returncode == 2
    envelope = json.loads(usage.read_text(encoding="utf-8"))
    validate(envelope, ERROR)
    assert envelope["error"]["code"] == "usage"
    planned = tmp_path / "plan.json"
    proc, plan = dry_run(
        "review", "--outcome-file", str(planned), cwd=work, tools=tools, tmp_path=tmp_path
    )
    assert proc.returncode == 0, plan["warnings"]
    assert not planned.exists()


# Warnings before a failure (R8)


def test_warnings_before_a_failure_are_kept(tmp_path: Path, work: Path) -> None:
    git_repo(work)
    subprocess.run(
        ["git", "-C", str(work), "config", "imap.pass", "x"],
        check=True,
        timeout=30,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    bare = tmp_path / "bare"
    claude_bin(bare, write_json(tmp_path / "s.json", {"steps": []}))
    git = subprocess.run(
        ["sh", "-c", "command -v git"], capture_output=True, text=True, timeout=15, check=True
    ).stdout.strip()
    (bare / "git").symlink_to(git)
    proc, plan = dry_run(
        "respond", cwd=work, tools=bare, tmp_path=tmp_path, env={"PATH": str(bare)}
    )
    assert proc.returncode == 12 and plan["would_fail"] == "sandbox_unavailable"
    warnings = plan["warnings"]
    credential = next((i for i, w in enumerate(warnings) if "imap.pass" in w), None)
    assert credential is not None, warnings
    failure = next(i for i, w in enumerate(warnings) if w.startswith("a launch would fail"))
    assert credential < failure


# Environment values (R9): profile.path is the only one shown


def test_kinds_prints_no_environment_value_but_the_path(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    config = f"kinds:\n  implement:\n    env:\n      set: {{SET_ME: {SET_VALUE}}}\n"
    proc = cli(
        ["kinds", "--json", "--kind", "implement"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=config,
        env={"ANTHROPIC_API_KEY": SECRET, "OTHER": SECRET},
    )
    assert proc.returncode == 0, proc.stderr
    assert SECRET not in proc.stdout and SET_VALUE not in proc.stdout
    (profile,) = json.loads(proc.stdout)["kinds"]
    assert str(tools) in profile["path"].split(":")


# Interactive TTY checks (R10)


def test_an_interactive_exec_dry_run_without_a_terminal_is_a_usage_error(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    proc = cli(["exec", "--dry-run", "--json"], cwd=work, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 2
    assert error_of(proc)["error"]["code"] == "usage"


# A cwd that is not valid UTF-8 (R12)


@pytest.fixture
def odd_cwd(tmp_path: Path) -> Path:
    path = tmp_path / os.fsdecode(b"odd\xff")
    path.mkdir()
    (path / "prompt.md").write_text("Do it.\n", encoding="utf-8")
    return path


def test_a_launch_in_a_non_utf8_cwd_fails_unsafe_cwd(
    tmp_path: Path, tools: Path, odd_cwd: Path
) -> None:
    proc = cli(
        ["exec", "--json", "--mode", "headless", "--prompt-file", "prompt.md"],
        cwd=odd_cwd,
        tools=tools,
        tmp_path=tmp_path,
    )
    assert proc.returncode == 12, proc.stderr
    outcome = json.loads(proc.stdout)
    assert outcome["end_reason"] == "unsafe_cwd"


def test_a_dry_run_in_a_non_utf8_cwd_would_fail_with_valid_json(
    tmp_path: Path, tools: Path, odd_cwd: Path
) -> None:
    proc, plan = dry_run("implement", cwd=odd_cwd, tools=tools, tmp_path=tmp_path)
    assert proc.returncode == 12 and plan["would_fail"] == "unsafe_cwd"
    assert plan["cwd"].endswith("odd\\udcff")
    human = cli(
        ["exec", "--dry-run", "--mode", "headless", "--prompt-file", "prompt.md"],
        cwd=odd_cwd,
        tools=tools,
        tmp_path=tmp_path,
    )
    assert human.returncode == 12, human.stderr
    assert "a launch would fail (unsafe_cwd)" in human.stdout


# Planted binaries under session-writable paths (S1-S7): a marker-writing binary planted on
# PATH under a path a session could write never runs.


def plant_git(directory: Path, marker: Path) -> None:
    """A ``git`` that records that it ran (and fails, as a broken git would)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "git"
    path.write_text(f'#!/bin/sh\necho "git $*" >> {marker}\nexit 1\n', encoding="utf-8")
    path.chmod(0o755)


def home() -> Path:
    return Path(os.environ["HOME"])


def headless_exec(**kw: Any) -> subprocess.CompletedProcess[str]:
    return cli(
        ["exec", "--json", "--kind", "respond", "--mode", "headless", "--prompt-file", "prompt.md"],
        **kw,
    )


REPO_OVERRIDE = (
    "repos:\n  example.com/o/r:\n    kinds:\n      respond:\n        write_dirs: ['~/rw']\n"
)


@pytest.mark.parametrize(
    ("config", "where"),
    [
        ("kinds:\n  respond:\n    write_dirs: ['~/wd']\n", "wd"),
        (REPO_OVERRIDE, "rw"),
        (
            "permission_profiles:\n  wide: {sandbox: {filesystem: {allowWrite: ['~/aw']}}}\n"
            "kinds:\n  respond:\n    permission_profile: wide\n",
            "aw",
        ),
    ],
)
@pytest.mark.parametrize("launch", [False, True])
def test_step2_never_runs_a_git_under_the_kinds_write_paths(
    config: str, where: str, launch: bool, tmp_path: Path, tools: Path, work: Path
) -> None:
    """S1: the git that derives the repo at step 2 is checked against the kind's
    ``write_dirs`` and profile write paths (every per-repo override's too: the repo is not
    known yet), in a dry run and in a real launch."""
    git_repo(work)
    marker = tmp_path / "ran"
    planted = home() / where / "bin"
    plant_git(planted, marker)
    env = {"PATH": f"{planted}:{tools}:/usr/bin:/bin"}
    if launch:
        proc = headless_exec(cwd=work, tools=tools, tmp_path=tmp_path, config=config, env=env)
        assert proc.returncode == 12, proc.stdout + proc.stderr
    else:
        _, plan = dry_run(
            "respond", cwd=work, tools=tools, tmp_path=tmp_path, config=config, env=env
        )
        assert plan["repo"] is None
        assert any("repo not derived" in w for w in plan["warnings"]), plan["warnings"]
    ran = marker.read_text(encoding="utf-8") if marker.exists() else ""
    assert "get-url" not in ran  # step 2 never ran it
    if where != "rw":
        # The run's own write path: step 5 refuses it too. (A per-repo override's path is
        # not the repo-less run's: step 5 checks only the run's own set.)
        assert ran == ""


@pytest.mark.parametrize("entry", GIT_DIR_ENTRIES)
def test_step2_derives_no_repo_with_a_git_dir_write_entry(
    entry: str, tmp_path: Path, tools: Path, work: Path
) -> None:
    """S1: a ``${GIT_DIR}`` write entry is not known at step 2: no repo is derived, with a
    warning (git is not run then)."""
    git_repo(work)
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", "https://example.com/o/r.git"],
        check=True,
        timeout=30,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    _, plan = dry_run(
        "respond", cwd=work, tools=tools, tmp_path=tmp_path, config=git_dir_config("respond", entry)
    )
    assert plan["repo"] is None
    assert any("repo not derived" in w for w in plan["warnings"]), plan["warnings"]


def test_step2_still_derives_the_repo_when_git_is_safe(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    git_repo(work)
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", "https://example.com/o/r.git"],
        check=True,
        timeout=30,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    _, plan = dry_run(
        "respond",
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config="kinds:\n  respond:\n    write_dirs: ['~/wd']\n" + REPO_OVERRIDE,
    )
    assert plan["repo"] == "example.com/o/r"
    assert plan["origins"]["repo"] == "derived"


def secret_logging_git(directory: Path, log: Path) -> None:
    """A ``git`` that logs its environment, then runs the real git."""
    real = subprocess.run(
        ["sh", "-c", "command -v git"], capture_output=True, text=True, timeout=15, check=True
    ).stdout.strip()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "git"
    path.write_text(f'#!/bin/sh\nenv >> {log}\nexec {real} "$@"\n', encoding="utf-8")
    path.chmod(0o755)


def test_git_gets_a_minimal_environment(tmp_path: Path, tools: Path, work: Path) -> None:
    """S1: every git call gets ``PATH``, ``HOME``, ``LC_ALL=C`` and
    ``GIT_CONFIG_NOSYSTEM=1`` only: no secret of the caller reaches it."""
    git_repo(work)
    log = tmp_path / "git-env"
    secret_logging_git(tmp_path / "gitbin", log)
    _, plan = dry_run(
        "respond",
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        env={"PATH": f"{tmp_path / 'gitbin'}:{tools}:/usr/bin:/bin", "MY_TOKEN": SECRET},
    )
    assert plan["would_fail"] is None, plan["warnings"]
    text = log.read_text(encoding="utf-8")
    assert SECRET not in text
    names = {line.partition("=")[0] for line in text.splitlines() if "=" in line}
    # sh itself adds PWD (and may add SHLVL, _).
    assert names - {"PWD", "SHLVL", "_", "OLDPWD"} == {
        "PATH",
        "HOME",
        "LC_ALL",
        "GIT_CONFIG_NOSYSTEM",
    }


def linked_worktree(main: Path, work: Path) -> None:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(main)], check=True, timeout=30, env=env)
    subprocess.run(
        ["git", "-C", str(main), "-c", "user.email=a@b", "-c", "user.name=a"]
        + ["commit", "-q", "--allow-empty", "-m", "x"],
        check=True,
        timeout=30,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(work)],
        check=True,
        timeout=30,
        env=env,
    )
    (work / "prompt.md").write_text("Do it.\n", encoding="utf-8")


@pytest.mark.parametrize("layout", ["separate", "worktree", "bare_parent"])
@pytest.mark.parametrize("launch", [False, True])
def test_step5_never_runs_a_git_under_a_git_dir_write_path(
    layout: str, launch: bool, tmp_path: Path, tools: Path
) -> None:
    """S2: with a ``${GIT_DIR}`` write entry, the candidate git directories are found without
    running git (``.git`` directories and files, ``commondir``) and git is resolved against
    them: a git planted there is refused (``unsafe_path``), never run."""
    work = tmp_path / "work"
    if layout == "separate":
        work.mkdir()
        (work / "prompt.md").write_text("Do it.\n", encoding="utf-8")
        git_dir = tmp_path / "gd"
        separate_git_dir(work, git_dir)
    elif layout == "worktree":
        linked_worktree(tmp_path / "main", work)
        git_dir = tmp_path / "main" / ".git"
    else:  # the cwd is inside a bare repository's directory
        git_dir = tmp_path / "bare.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(git_dir)],
            check=True,
            timeout=30,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        work = git_dir / "work"
        work.mkdir()
        (work / "prompt.md").write_text("Do it.\n", encoding="utf-8")
    marker = tmp_path / "ran"
    planted = git_dir / "tools" / "bin"
    plant_git(planted, marker)
    config = git_dir_config("respond", "write_dirs")
    env = {"PATH": f"{planted}:{tools}:/usr/bin:/bin"}
    if launch:
        proc = headless_exec(cwd=work, tools=tools, tmp_path=tmp_path, config=config, env=env)
        assert proc.returncode == 12, proc.stdout + proc.stderr
        assert json.loads(proc.stdout)["end_reason"] == "unsafe_path"
    else:
        _, plan = dry_run(
            "respond", cwd=work, tools=tools, tmp_path=tmp_path, config=config, env=env
        )
        assert plan["would_fail"] == "unsafe_path", plan["warnings"]
    assert not marker.exists()


def test_step5_runs_no_git_when_the_git_dir_cannot_be_found_without_it(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    """S2: a ``.git`` file that names no git directory leaves the candidates unknown: no git
    runs and the launch fails ``unsafe_cwd`` (git itself could not determine it either)."""
    (work / ".git").write_text("not a gitfile\n", encoding="utf-8")
    log = tmp_path / "git-env"
    secret_logging_git(tmp_path / "gitbin", log)
    _, plan = dry_run(
        "respond",
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=git_dir_config("respond", "write_dirs"),
        env={"PATH": f"{tmp_path / 'gitbin'}:{tools}:/usr/bin:/bin"},
    )
    assert plan["would_fail"] == "unsafe_cwd", plan["warnings"]
    assert not log.exists()


def test_kinds_without_repo_never_runs_a_tool_under_a_repo_override_write_path(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    """S3: without ``--repo`` every per-repo override's write paths count."""
    marker = tmp_path / "ran"
    planted = home() / "rw" / "bin"
    plant(planted, marker)
    env = {"PATH": f"{planted}:{tools}:/usr/bin:/bin"}
    proc = cli(
        ["kinds", "--json", "--kind", "respond"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=REPO_OVERRIDE,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    validate(report, KINDS)
    assert report["claude_bin_resolved"] is None
    (respond,) = report["kinds"]
    assert respond["sandbox_available"] is False
    assert any("a launch refuses it" in p for p in respond["sandbox_problems"])
    assert not marker.exists()
    # Every kind: claude is checked against respond's override too. (bwrap and socat are
    # checked against each row's own kind, so only claude is planted here.)
    for name in ("bwrap", "socat"):
        (planted / name).unlink()
    proc = cli(
        ["kinds", "--json"], cwd=work, tools=tools, tmp_path=tmp_path, config=REPO_OVERRIDE, env=env
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["claude_bin_resolved"] is None
    assert not marker.exists()


def test_kinds_checks_claude_against_every_kind(tmp_path: Path, tools: Path, work: Path) -> None:
    """S4: ``claude_bin`` is shared by every kind: it is checked against every kind's write
    paths, whatever ``--kind`` names."""
    marker = tmp_path / "ran"
    planted = home() / "wd" / "bin"
    plant(planted, marker)
    proc = cli(
        ["kinds", "--json", "--kind", "implement"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config="kinds:\n  respond:\n    write_dirs: ['~/wd']\n",
        env={"PATH": f"{planted}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["claude_bin_resolved"] is None
    assert any("a launch refuses it" in w for w in report["warnings"]), report["warnings"]
    assert not marker.exists()


def test_kinds_probes_nothing_when_any_kinds_paths_are_unknown(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    """S4: one kind with unknowable write paths (``${GIT_DIR}``) means ``claude`` is not
    run, also for ``--kind`` naming another kind."""
    log = tmp_path / "claude-ran"
    (tools / "claude").write_text(f"#!/bin/sh\necho claude >> {log}\nexit 0\n", encoding="utf-8")
    proc = cli(
        ["kinds", "--json", "--kind", "implement"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=git_dir_config("respond", "write_dirs"),
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["claude_bin_resolved"] is None
    assert any("not probed by a dry run" in w for w in report["warnings"])
    assert not log.exists()


def test_the_version_probe_never_runs_an_interpreter_from_a_writable_path(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    """S5: ``claude`` may be a ``#!/usr/bin/env node`` script: the probe's ``PATH`` holds no
    entry under a session-writable path, so a planted ``node`` never runs."""
    marker = tmp_path / "ran"
    planted = home() / "wd" / "bin"
    planted.mkdir(parents=True)
    node = planted / "node"
    node.write_text(f"#!/bin/sh\necho node >> {marker}\necho '9.9.9'\n", encoding="utf-8")
    node.chmod(0o755)
    (tools / "claude").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    proc = cli(
        ["kinds", "--json"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config="kinds:\n  respond:\n    write_dirs: ['~/wd']\n",
        env={"PATH": f"{planted}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["claude_bin_resolved"] == str(tools / "claude")
    assert not marker.exists()


@pytest.mark.parametrize(
    ("config", "where"),
    [
        (
            "permission_profiles:\n  wide: {sandbox: {filesystem: {allowWrite: ['~/aw']}}}\n"
            "kinds:\n  implement:\n    permission_profile: wide\n",
            "aw",
        ),
        (
            "permission_profiles:\n  wide: {permissions: {allow: ['Edit(~/ed/**)']}}\n"
            "kinds:\n  implement:\n    permission_profile: wide\n",
            "ed",
        ),
    ],
)
def test_a_trusted_kinds_profile_write_paths_count(
    config: str, where: str, tmp_path: Path, tools: Path, work: Path
) -> None:
    """S6: a trusted kind's set holds its profile write paths, as ``git_writable`` computes
    them."""
    marker = tmp_path / "ran"
    planted = home() / where / "bin"
    plant(planted, marker)
    proc = cli(
        ["kinds", "--json", "--kind", "implement"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=config,
        env={"PATH": f"{planted}:{tools}:/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["claude_bin_resolved"] is None
    assert not marker.exists()


def test_a_trusted_kinds_git_dir_write_entry_probes_nothing(
    tmp_path: Path, tools: Path, work: Path
) -> None:
    """S6: a trusted kind's ``${GIT_DIR}`` write entry leaves its paths unknown."""
    log = tmp_path / "claude-ran"
    (tools / "claude").write_text(f"#!/bin/sh\necho claude >> {log}\nexit 0\n", encoding="utf-8")
    proc = cli(
        ["kinds", "--json", "--kind", "implement"],
        cwd=work,
        tools=tools,
        tmp_path=tmp_path,
        config=git_dir_config("implement", "write_dirs"),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["claude_bin_resolved"] is None
    assert not log.exists()
