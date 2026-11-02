"""The sandbox rows of design.md (C, D, E1) and #59/#60 items. They need Claude Code's sandbox
(bubblewrap and socat on Linux/WSL2, Seatbelt on macOS), so they skip where it is absent;
`test_a_missing_sandbox_fails_the_launch_under_fail_if_unavailable` needs the opposite and runs
on Linux without any installed tool. The settings written here are hand-built from design.md,
not the builder's output: the builder's own output is exercised by test_e2e.py."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from claude_session import gateway

from .harness import LiveEnv, sandbox_available, transcript_text

needs_sandbox = pytest.mark.skipif(not sandbox_available(), reason="no Claude Code sandbox here")
SYSTEM_DIRS = ["/usr", "/bin", "/lib", "/lib64", "/sbin", "/etc"]
SECRET = "SECRET-MARKER-4471"


def real_dirs(*paths: str) -> list[str]:
    """Each path that exists, plus its realpath (a usr-merged distro links /bin to /usr/bin)."""
    out: list[str] = []
    for path in paths:
        if os.path.exists(path):
            for candidate in (path, os.path.realpath(path)):
                if candidate not in out:
                    out.append(candidate)
    return out


def tool_dirs() -> list[str]:
    return real_dirs(*(os.path.dirname(p) for n in ("bwrap", "socat") if (p := shutil.which(n))))


def sandbox_settings(
    live: LiveEnv, work: Path, *, extra_read: Sequence[str] = (), deny_write: Sequence[str] = (),
    allow: Sequence[str] = (), auto_allow: bool = False,
) -> dict[str, Any]:  # fmt: skip
    """Deny-root reads with the minimal allowlist and the private TMPDIR in `allowWrite` (E1)."""
    read = real_dirs(*SYSTEM_DIRS) + tool_dirs() + [str(work), *extra_read]
    return {
        "permissions": {"defaultMode": "dontAsk", "allow": list(allow)},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": auto_allow,
            "network": {"allowedDomains": []},
            "filesystem": {
                "denyRead": ["/"],
                "allowRead": read,
                "allowWrite": [str(live.root / "tmp")],
                "denyWrite": list(deny_write),
            },
        },
    }


@pytest.mark.live
@needs_sandbox
def test_deny_root_hides_files_outside_the_allowlist_and_tools_still_work(live: LiveEnv) -> None:
    """E1 and #59 items 4, 7: markers outside the allowlist are hidden from Bash while `ls`,
    `cat`, `sh -c` and writes in the cwd work with TMPDIR only in `allowWrite`."""
    work = live.dir("work")
    outside = live.dir("outside")
    (outside / "marker.txt").write_text(SECRET, encoding="utf-8")
    (work / "visible.txt").write_text("visible-content\n", encoding="utf-8")
    settings = live.write_json(
        "s.json",
        sandbox_settings(
            live, work,
            allow=["Bash(cat:*)", "Bash(ls:*)", "Bash(sh:*)", "Bash(echo:*)"],
            auto_allow=True,
        ),
    )  # fmt: skip
    run = live.headless(
        f"With the Bash tool run, one per call: `cat {outside / 'marker.txt'}`, `cat visible.txt`,"
        " `sh -c 'echo shell-ok'`. Report what each printed or the error it gave. Then reply DONE.",
        "--setting-sources", "", "--settings", str(settings), cwd=work,
    )  # fmt: skip
    text = transcript_text(live.projects, run.session_id)
    assert "visible-content" in text and "shell-ok" in text
    # The command line holds the marker's path; the content must never be returned.
    assert SECRET not in text.replace(f"cat {outside}", "")


@pytest.mark.live
@needs_sandbox
def test_auto_allow_bash_if_sandboxed_false_denies_a_write_no_rule_allows(live: LiveEnv) -> None:
    """D3, with the default (`true`) as the control: it writes the file without a rule."""
    work = live.dir("work")
    prompt = "Run `echo hi > out.txt` with the Bash tool. Do not retry. Then reply DONE."
    common = ("--setting-sources", "")
    on = live.write_json("on.json", sandbox_settings(live, work, auto_allow=True))
    live.headless(prompt, *common, "--settings", str(on), cwd=work)
    assert (work / "out.txt").exists(), "control: the default should auto-allow sandboxed Bash"
    (work / "out.txt").unlink()
    off = live.write_json("off.json", sandbox_settings(live, work, auto_allow=False))
    live.headless(prompt, *common, "--settings", str(off), cwd=work)
    assert not (work / "out.txt").exists()


@pytest.mark.live
@needs_sandbox
@pytest.mark.parametrize("linked", [False, True], ids=["main-checkout", "linked-worktree"])
def test_a_denied_git_dir_is_read_only_for_sandboxed_git(live: LiveEnv, linked: bool) -> None:
    """D1, D2: reads work, writes (commit, branch) fail, in a main checkout and a worktree."""
    main = live.dir("main")
    env = live.env()
    git = [
        shutil.which("git") or "/usr/bin/git",
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
    ]
    gw = gateway.SystemGateway()

    def run_git(cwd: Path, *args: str) -> str:
        proc = gw.run([*git, *args], env=env, cwd=str(cwd), timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    run_git(main, "init", "-q")
    (main / "f.txt").write_text("x\n", encoding="utf-8")
    run_git(main, "add", "f.txt")
    run_git(main, "commit", "-q", "-m", "first")
    work = main
    if linked:
        work = live.root / "linked"
        run_git(main, "worktree", "add", "-q", str(work))
    git_dir = main / ".git"
    settings = live.write_json(
        "s.json",
        sandbox_settings(
            live, work, extra_read=[str(git_dir)], deny_write=[str(git_dir)],
            allow=["Bash(git:*)"], auto_allow=True,
        ),
    )  # fmt: skip
    live.headless(
        "With the Bash tool run these, one per call, and do not retry a failure: `git status`, "
        "`git branch newbranch`, `git commit --allow-empty -m second`. Then reply DONE.",
        "--setting-sources", "", "--settings", str(settings), cwd=work,
    )  # fmt: skip
    assert "newbranch" not in run_git(main, "branch", "--list")
    assert run_git(main, "rev-list", "--count", "HEAD").strip() == "1"


@pytest.mark.live
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Seatbelt is always present")
def test_a_missing_sandbox_fails_the_launch_under_fail_if_unavailable(live: LiveEnv) -> None:
    """#59 item 3: where no sandbox is installed, `failIfUnavailable: true` must end the launch
    with no answer. Skipped where one is installed (it would simply run)."""
    if sandbox_available():
        pytest.skip("a sandbox is installed here; the empty PATH may not hide it")
    settings = live.write_json("s.json", {"sandbox": {"enabled": True, "failIfUnavailable": True}})
    proc = live.claude_run(
        ["-p", "--model", live.model, "--setting-sources", "", "--settings", str(settings),
         "--", "Reply with exactly the single word PONG."],
        timeout=120,
    )  # fmt: skip
    assert proc.returncode != 0 and "PONG" not in proc.stdout
    live.observe("failIfUnavailable message", (proc.stdout + proc.stderr).strip()[:300])
