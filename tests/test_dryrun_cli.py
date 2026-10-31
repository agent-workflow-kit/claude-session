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
