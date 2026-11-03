"""The sandbox rows of design.md (C, D, E1) and #59/#60 items. They need Claude Code's sandbox
(bubblewrap and socat on Linux/WSL2, Seatbelt on macOS), so they skip where it is absent;
`test_a_missing_sandbox_fails_the_launch_under_fail_if_unavailable` needs the opposite and runs
on Linux without any installed tool (as does the `preferred`-style fallback test). Without
bubblewrap and socat every `needs_sandbox` test skips. The settings written here are hand-built
from design.md, not the builder's output: the builder's own output is exercised by test_e2e.py.

Assertions on what a command printed read the transcript's `tool_result` blocks only, never the
prompt (which names the commands and their expected output)."""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from claude_session import gateway

from .harness import LiveEnv, sandbox_available, session_tool_results, tool_path

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
    `cat`, `sh -c` and writes in the cwd work with TMPDIR only in `allowWrite`. `/bin/sh` by
    its literal path (dynamically linked, so the ELF loader must resolve too) is the #42 check
    of the system dirs; it proves the usr-merged case only on a usr-merged host, so whether
    `/bin` is a symlink here is recorded."""
    work = live.dir("work")
    outside = live.dir("outside")
    (outside / "marker.txt").write_text(SECRET, encoding="utf-8")
    (work / "visible.txt").write_text("visible-content\n", encoding="utf-8")
    settings = live.write_json(
        "s.json",
        sandbox_settings(
            live, work,
            allow=["Bash(cat:*)", "Bash(ls:*)", "Bash(sh:*)", "Bash(/bin/sh:*)", "Bash(echo:*)"],
            auto_allow=True,
        ),
    )  # fmt: skip
    run = live.headless(
        f"With the Bash tool run, one per call: `cat {outside / 'marker.txt'}`, `cat visible.txt`,"
        " `sh -c 'echo shell-$((40+2))'`, `/bin/sh -c 'echo bin-sh-$((6*7))'`. Report what each"
        " printed or the error it gave. Then reply DONE.",
        "--setting-sources", "", "--settings", str(settings), cwd=work,
    )  # fmt: skip
    text = session_tool_results(live.projects, run.session_id)
    live.observe("/bin is a symlink (usr-merged host)", os.path.islink("/bin"))
    # The shells print computed values, which neither the command lines nor the prompt contain;
    # they are looked for in what the tools returned, not in what the model said.
    assert "visible-content" in text and "shell-42" in text
    assert "bin-sh-42" in text, "/bin/sh (or its ELF loader) did not resolve under deny-root"
    assert SECRET not in text


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
        tool_path("git", live.env()) or "/usr/bin/git",
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
    run = live.headless(
        "With the Bash tool run these, one per call, and do not retry a failure: `git status`, "
        "`git branch newbranch`, `git commit --allow-empty -m second`. Then reply DONE.",
        "--setting-sources", "", "--settings", str(settings), cwd=work,
    )  # fmt: skip
    results = session_tool_results(live.projects, run.session_id)
    assert "On branch" in results, "git status did not run (no positive control)"
    live.observe(f"D1/D2 git write errors ({'linked' if linked else 'main'})", results[-400:])
    assert re.search(
        r"read-only|permission denied|operation not permitted|unable to|cannot lock", results,
        re.IGNORECASE,
    ), "no write error was reported"  # fmt: skip
    assert "newbranch" not in run_git(main, "branch", "--list")
    assert run_git(main, "rev-list", "--count", "HEAD").strip() == "1"


@pytest.mark.live
@needs_sandbox
def test_a_deny_write_of_a_missing_path_is_honoured_for_bash_and_the_write_tool(
    live: LiveEnv,
) -> None:
    """C1: `denyWrite` of a path that does not exist yet stops Bash from creating it, and with a
    `Write(...)` deny rule the Write tool too. The control file next to them is written."""
    work = live.dir("work")
    by_bash, by_tool, free = work / "denied-bash.txt", work / "denied-tool.txt", work / "free.txt"
    settings = sandbox_settings(
        live, work, deny_write=[str(by_bash), str(by_tool)], allow=["Write"], auto_allow=True
    )
    settings["permissions"]["deny"] = [f"Write(/{by_tool})"]
    path = live.write_json("s.json", settings)
    live.headless(
        f"Do these one per call and do not retry a failure: with the Bash tool run "
        f"`echo x > {free}`; with the Bash tool run `echo x > {by_bash}`; with the Write tool "
        f"create {by_tool} with the content x. Then reply DONE.",
        "--setting-sources", "", "--settings", str(path), cwd=work,
    )  # fmt: skip
    assert free.exists(), "control: the sandboxed Bash write in the cwd failed"
    assert not by_bash.exists() or by_bash.stat().st_size == 0  # a /dev/null placeholder at most
    assert not by_tool.exists() or by_tool.stat().st_size == 0


@pytest.mark.live
@needs_sandbox
def test_a_nested_deny_inside_an_allowed_parent_protects_the_file(live: LiveEnv) -> None:
    """C3 (protected session files): a `denyWrite` file inside an `allowWrite` directory stays
    unwritable for sandboxed Bash while its sibling is written (the control)."""
    work = live.dir("work")
    parent = live.dir("parent")
    protected, sibling = parent / "protected.txt", parent / "sibling.txt"
    protected.write_text("original\n", encoding="utf-8")
    settings = sandbox_settings(
        live, work, extra_read=[str(parent)], deny_write=[str(protected)], auto_allow=True
    )
    settings["sandbox"]["filesystem"]["allowWrite"].append(str(parent))
    path = live.write_json("s.json", settings)
    live.headless(
        f"With the Bash tool, one command per call, do not retry a failure: "
        f"`echo x > {sibling}`; `echo changed >> {protected}`. Then reply DONE.",
        "--setting-sources", "", "--settings", str(path), cwd=work,
    )  # fmt: skip
    assert sibling.exists(), "control: the write in the allowed parent failed"
    assert protected.read_text(encoding="utf-8") == "original\n"


@pytest.mark.live
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Seatbelt is always present")
def test_a_missing_sandbox_fails_the_launch_under_fail_if_unavailable(live: LiveEnv) -> None:
    """#59 item 3: where no sandbox is installed, `failIfUnavailable: true` must end the launch
    with no answer. Skipped where one is installed (it would simply run)."""
    if sandbox_available():
        pytest.skip("a sandbox is installed here; the empty PATH may not hide it")
    settings = live.write_json("s.json", {"sandbox": {"enabled": True, "failIfUnavailable": True}})
    proc = live.claude_run(
        ["-p", "--model", live.model, *live.budget(), "--setting-sources", "",
         "--settings", str(settings), "--", "Reply with exactly the single word PONG."],
        timeout=120,
    )  # fmt: skip
    assert proc.returncode != 0 and "PONG" not in proc.stdout
    live.observe("failIfUnavailable message", (proc.stdout + proc.stderr).strip()[:300])


@pytest.mark.live
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Seatbelt is always present")
def test_without_a_sandbox_enabled_without_fail_if_unavailable_runs_unsandboxed(
    live: LiveEnv,
) -> None:
    """#60 (`preferred` fallback): with prerequisites missing, claude-session keeps
    `sandbox.enabled: true` without `failIfUnavailable`; Claude Code must then answer
    (unsandboxed, with a warning, which is recorded), not fail. Skipped where a sandbox exists."""
    if sandbox_available():
        pytest.skip("a sandbox is installed here")
    settings = live.write_json("s.json", {"sandbox": {"enabled": True}})
    run = live.headless(
        "Reply with exactly the single word PONG.",
        "--setting-sources", "", "--settings", str(settings),
    )  # fmt: skip
    live.observe("sandbox unavailable without failIfUnavailable: stderr", run.proc.stderr[:300])
    assert run.proc.returncode == 0, run.proc.stderr[:500]
    assert "PONG" in run.text
